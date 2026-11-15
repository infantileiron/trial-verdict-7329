"""裁决证据对账与修复：只依据冻结快照与库内原裁决结果核对/恢复留存文件。

口径：
- 留存文件路径 ARTIFACT_DIR/trial-verdict-<decision_id>.json
- 文件内容必须与 worker 落盘口径完全一致：json.dumps(result, ensure_ascii=False)
- result.checksum 必须等于冻结快照的 SHA256，证明结果确属当时冻结输入
- 修复只从库内 result 重新落盘，不读取新增事件、不重新检验
- 原始损坏文件移动到隔离目录保留，供追查
"""
import fcntl
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone

ARTIFACT_DIR = os.environ.get('ARTIFACT_DIR', '/artifacts')
QUARANTINE_DIR = os.path.join(ARTIFACT_DIR, 'quarantine')
FILENAME_RE = re.compile(r'^trial-verdict-(\d+)\.json$')

# 状态：完整 / 缺失 / 内容不符 / 冻结校验不符（不可修复）
INTACT = 'intact'
MISSING = 'missing'
CONTENT_MISMATCH = 'content_mismatch'
CHECKSUM_MISMATCH = 'checksum_mismatch'
REPAIRABLE = {MISSING, CONTENT_MISMATCH}


class EvidenceError(Exception):
    """证据不完整或来源无法确认，调用方据此拒绝下载。"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def artifact_path(decision_id):
    return os.path.join(ARTIFACT_DIR, f'trial-verdict-{decision_id}.json')


def expected_bytes(result):
    # 必须与 src/worker.py 首次落盘口径逐字节一致
    return json.dumps(result, ensure_ascii=False).encode('utf-8')


def _sha256(data: bytes):
    return hashlib.sha256(data).hexdigest()


def _ensure_dirs():
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    os.makedirs(QUARANTINE_DIR, exist_ok=True)


def _atomic_write(path, data: bytes):
    """同目录临时文件 + os.replace，读者要么看到旧完整文件、要么看到新完整文件。"""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f'.tmp-{os.path.basename(path)}-{uuid.uuid4().hex}')
    with open(tmp, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class _DirLock:
    """进程间写锁，保证对账修复与 worker 首次落盘/重试互不覆盖。"""

    def __init__(self):
        _ensure_dirs()
        self._f = open(os.path.join(ARTIFACT_DIR, '.evidence.lock'), 'w')

    def __enter__(self):
        fcntl.flock(self._f.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)


def _read_file(path):
    try:
        with open(path, 'rb') as f:
            return f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise EvidenceError(f'留存证据无法读取：{e}')


def verify(row):
    """核对单条已完成裁决，返回状态明细。不写任何文件。"""
    from src.services.analysis import snapshot_checksum

    result = row.result or {}
    path = artifact_path(row.id)
    want = expected_bytes(result)
    item = {
        'decision_id': row.id,
        'status': INTACT,
        'detail': '',
        'expected_sha256': _sha256(want),
        'file_sha256': None,
        'action': 'none',
        'quarantine_file': None,
    }
    # 1) 冻结输入与结果校验和对账：确认库内结果确属当时冻结快照
    stored_checksum = result.get('checksum')
    if not stored_checksum or stored_checksum != snapshot_checksum(row.snapshot):
        item['status'] = CHECKSUM_MISMATCH
        item['detail'] = '结果校验和与冻结输入快照不一致，无法证明来自原裁决，禁止自动修复或下载'
        return item
    # 2) 留存文件对账
    data = _read_file(path)
    if data is None:
        item['status'] = MISSING
        item['detail'] = '留存证据文件缺失'
        return item
    item['file_sha256'] = _sha256(data)
    if data != want:
        item['status'] = CONTENT_MISMATCH
        item['detail'] = '留存文件内容与库内原裁决结果不一致（疑似被改写或拼装）'
        return item
    item['detail'] = '留存文件与冻结输入、原裁决结果三者一致'
    return item


def _quarantine(decision_id, broken_path):
    """原始损坏文件保留到隔离目录，不覆盖任何已留证文件。"""
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    base = f'trial-verdict-{decision_id}.corrupt-{stamp}.json'
    dest = os.path.join(QUARANTINE_DIR, base)
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(QUARANTINE_DIR, f'trial-verdict-{decision_id}.corrupt-{stamp}.{n}.json')
        n += 1
    os.replace(broken_path, dest)
    return os.path.relpath(dest, ARTIFACT_DIR)


def repair(row, item=None):
    """仅修复异常项；依据库内原裁决结果恢复，不重新计算。返回更新后的状态明细。"""
    if item is None:
        item = verify(row)
    if item['status'] == INTACT:
        item['action'] = 'none'
        return item
    if item['status'] == CHECKSUM_MISMATCH:
        item['action'] = 'unrepairable'
        return item
    path = artifact_path(row.id)
    with _DirLock():
        # 加锁后重新核对，避免与并发落盘重复生成
        fresh = verify(row)
        if fresh['status'] == INTACT:
            fresh['action'] = 'none'
            fresh['detail'] = '并发处理后证据已完整，跳过修复'
            return fresh
        if fresh['status'] == CHECKSUM_MISMATCH:
            fresh['action'] = 'unrepairable'
            return fresh
        try:
            if fresh['status'] == CONTENT_MISMATCH and os.path.exists(path):
                item['quarantine_file'] = _quarantine(row.id, path)
                item['action'] = 'quarantined_repaired'
            else:
                item['action'] = 'repaired'
            _atomic_write(path, expected_bytes(row.result))
        except OSError as e:
            item['action'] = 'repair_failed'
            item['detail'] = f'修复写入失败：{e}'
            return item
        # 修复后立即复核：只有完整且核验通过才算成功
        checked = verify(row)
        if checked['status'] != INTACT:
            item['status'] = checked['status']
            item['action'] = 'repair_failed'
            item['detail'] = f'修复后复核未通过：{checked["detail"]}'
            return item
        item['status'] = INTACT
        item['file_sha256'] = checked['file_sha256']
        item['detail'] = ('已隔离原损坏文件并依据原裁决恢复' if item['quarantine_file']
                          else '已依据库内原裁决结果补回缺失证据') + '，修复后复核通过'
        return item


def read_verified(row):
    """下载专用：只返回完整且核验通过的证据字节，否则拒绝。"""
    item = verify(row)
    if item['status'] != INTACT:
        raise EvidenceError(item['detail'] or '证据核验未通过，拒绝下载')
    data = _read_file(artifact_path(row.id))
    if data is None or _sha256(data) != item['file_sha256']:
        raise EvidenceError('证据在核验后发生变化，来源无法确认，拒绝下载')
    return data, item['file_sha256']


def scan_orphans(session, decision_model):
    """找出留存目录中无所属裁决（或裁决未完成）的证据文件。"""
    orphans = []
    if not os.path.isdir(ARTIFACT_DIR):
        return orphans
    ids = {d.id: d.state for d in session.query(decision_model).all()}
    for name in sorted(os.listdir(ARTIFACT_DIR)):
        m = FILENAME_RE.match(name)
        if not m:
            continue
        decision_id = int(m.group(1))
        state = ids.get(decision_id)
        if state is None:
            reason = 'no_decision'
            detail = '留存文件无所属裁决记录'
        elif state != 'done':
            reason = 'decision_not_done'
            detail = f'所属裁决尚未计算完成（{state}），留存文件来源无法确认'
        else:
            continue
        path = os.path.join(ARTIFACT_DIR, name)
        sha = _sha256(_read_file(path) or b'')
        orphans.append({'file': name, 'decision_id': decision_id, 'reason': reason,
                        'detail': detail, 'file_sha256': sha})
    return orphans
