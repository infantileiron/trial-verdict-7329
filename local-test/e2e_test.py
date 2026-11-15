"""端到端验证：证据对账、修复、下载门禁、worker 回收与并发安全。
运行：/workspace/.venv/bin/python -I local-test/e2e_test.py
全程使用临时 SQLite 与假 Redis，不触碰仓库内应用代码以外的任何真实服务。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path('/workspace')
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT / 'local-test'))

TMP = Path(tempfile.mkdtemp(prefix='tv-e2e-'))
ART = TMP / 'artifacts'
DB = TMP / 'test.db'
ART.mkdir()
os.environ.update({
    'DATABASE_URL': f'sqlite:///{DB}',
    'JWT_SECRET': 'test-secret',
    'ARTIFACT_DIR': str(ART),
})

from fake_redis import FakeRedis  # noqa: E402

redis_srv = FakeRedis()
threading.Thread(target=redis_srv.serve_forever, daemon=True).start()
os.environ['REDIS_URL'] = redis_srv.url

from src.config.database import Base, engine, Session  # noqa: E402
from src.models.entities import User, Experiment, Event, Decision, ReconciliationTask  # noqa: E402
from src.services.analysis import calculate  # noqa: E402
from src.services import evidence  # noqa: E402
from src.utils.security import hash_password  # noqa: E402
import uvicorn  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  ok  ' if cond else ' FAIL ') + name + (f' — {detail}' if detail and not cond else ''))


def _blocked(session, did):
    try:
        evidence.read_verified(session.get(Decision, did))
        return False
    except evidence.EvidenceError:
        return True


def _blocked_reason(session, did):
    try:
        evidence.read_verified(session.get(Decision, did))
        return ''
    except evidence.EvidenceError as e:
        return str(e)


def _wait(pred, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    raise AssertionError('等待超时')


def _state(model, pk):
    with Session() as s:
        return s.get(model, pk).state


def _get(model, pk):
    with Session() as s:
        return s.get(model, pk)


def _new_task(s):
    t = ReconciliationTask(state='running', created_by='admin@demo.local',
                           created_at=datetime.now(timezone.utc).isoformat())
    s.add(t)
    s.commit()
    return t.id


def _wait_http(url, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url)
            return
        except Exception:
            time.sleep(0.3)
    raise AssertionError('HTTP 服务未就绪')


# ---------- 造数据 ----------
Base.metadata.create_all(engine)
from sqlalchemy import text  # noqa: E402
with engine.connect() as c:  # WAL：允许 worker 子进程与 API 进程并发访问
    c.execute(text('PRAGMA journal_mode=WAL'))
    c.commit()
with Session() as s:
    admin = User(email='admin@demo.local', role='admin', password=hash_password('Demo123!'))
    viewer = User(email='viewer@demo.local', role='viewer', password=hash_password('Demo123!'))
    s.add_all([admin, viewer])
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    exp = Experiment(code='T-E2E', name='端到端对账实验', starts_at=(today - timedelta(days=2)).isoformat(),
                     ends_at=(today - timedelta(days=1)).isoformat(), a_ratio=0.5, hypothesis='端到端验证假设不少于五个字')
    s.add(exp)
    s.flush()
    stamp = (today - timedelta(days=1, hours=12)).isoformat()
    for i in range(60):
        group = 'A' if i < 30 else 'B'
        s.add(Event(experiment_id=exp.id, user_key=f'U{i:03d}', group=group, kind='exposure', occurred_at=stamp))
        if (group == 'A' and i < 6) or (group == 'B' and 30 <= i < 15):
            s.add(Event(experiment_id=exp.id, user_key=f'U{i:03d}', group=group, kind='activation', occurred_at=stamp))
    s.commit()
    exp_id = exp.id

with Session() as s:
    exp = s.get(Experiment, exp_id)
    snap = {'experiment': {c.name: getattr(exp, c.name) for c in exp.__table__.columns},
            'events': []}
    for e in s.query(Event).filter_by(experiment_id=exp_id).order_by(Event.id):
        snap['events'].append({c.name: getattr(e, c.name) for c in e.__table__.columns})

# 两条已完成裁决（库内结果 + 留存文件），再加一条未完成的
results = []
for did in (1, 2):
    r = calculate(snap)
    results.append(r)
    with Session() as s:
        d = Decision(id=did, experiment_id=exp_id, state='done', snapshot=snap, result=r)
        s.add(d)
        s.commit()
    evidence._ensure_dirs()
    evidence._atomic_write(evidence.artifact_path(did), evidence.expected_bytes(r))
with Session() as s:
    s.add(Decision(id=3, experiment_id=exp_id, state='failed', snapshot=snap, result=None, error='样本不足模拟失败'))
    s.commit()

f1, f2 = Path(evidence.artifact_path(1)), Path(evidence.artifact_path(2))

# ---------- 1. 正常下载 ----------
print('\n[1] 完整证据下载与校验头')
with Session() as s:
    row = s.get(Decision, 1)
    data, sha = evidence.read_verified(row)
check('完整证据可下载', data == evidence.expected_bytes(results[0]))
check('返回 SHA256 与文件一致', sha == evidence._sha256(data))

# ---------- 2. 制造三类异常 ----------
print('\n[2] 制造缺失/内容不符/孤立文件/冻结校验不符')
os.remove(f1)  # 裁决1：文件缺失
f2.write_text('{"tampered": true}', encoding='utf-8')  # 裁决2：内容不符
orphan = ART / 'trial-verdict-999.json'  # 无所属裁决
orphan.write_text('{"orphan": 1}', encoding='utf-8')
orphan_running = ART / 'trial-verdict-3.json'  # 所属裁决未完成
orphan_running.write_text('{"early": 1}', encoding='utf-8')
with Session() as s:
    d4 = Decision(id=4, experiment_id=exp_id, state='done', snapshot=snap,
                  result=dict(results[0], checksum='0' * 64))  # 冻结校验不符
    s.add(d4)
    s.commit()

with Session() as s:
    i1 = evidence.verify(s.get(Decision, 1))
    i2 = evidence.verify(s.get(Decision, 2))
    i4 = evidence.verify(s.get(Decision, 4))
check('缺失被识别为 missing', i1['status'] == 'missing', i1['detail'])
check('篡改被识别为 content_mismatch', i2['status'] == 'content_mismatch', i2['detail'])
check('校验和不符被识别为 checksum_mismatch', i4['status'] == 'checksum_mismatch')
check('缺失/不符证据拒绝下载', _blocked(s, 1) and _blocked(s, 2) and _blocked(s, 4))
check('下载拒绝原因可读', _blocked_reason(s, 2).find('不一致') >= 0, _blocked_reason(s, 2))

# ---------- 3. 对账修复 ----------
print('\n[3] 对账任务执行（worker 同款调用）')
from src.services.reconciliation import run_reconciliation  # noqa: E402
with Session() as s:
    t = ReconciliationTask(state='running', created_by='admin@demo.local',
                           created_at=datetime.now(timezone.utc).isoformat())
    s.add(t)
    s.commit()
    tid = t.id
    run_reconciliation(s, tid)
    t = s.get(ReconciliationTask, tid)
    rep = t.report
check('存在不可修复项时任务 failed', t.state == 'failed', t.error)
sm = rep['summary']
# 报告汇总：终态与修复前发现分开记录
check('汇总计数正确', sm['done_decisions'] == 3 and sm['intact'] == 2
      and sm['found_missing'] == 1 and sm['found_content_mismatch'] == 1
      and sm['checksum_mismatch'] == 1 and sm['repaired'] == 2 and sm['unrepairable'] == 1
      and sm['repair_failed'] == 0 and sm['orphan_files'] == 2, str(sm))
check('明细保留了修复前差异记录', {i['decision_id']: (i['initial_status'], i['status'], i['action']) for i in rep['items']}[1]
      == ('missing', 'intact', 'repaired'))
check('缺失文件已补回且内容正确', f1.read_bytes() == evidence.expected_bytes(results[0]))
check('篡改文件已恢复为原裁决内容', f2.read_bytes() == evidence.expected_bytes(results[1]))
quar = list((ART / 'quarantine').glob('trial-verdict-2.corrupt-*.json'))
check('原始损坏文件保留在 quarantine', len(quar) == 1 and quar[0].read_text() == '{"tampered": true}')
check('孤立文件原样保留未删除', orphan.exists() and orphan_running.exists())
orphan_rows = {o['file']: o for o in rep['orphans']}
check('孤立原因区分无裁决/未完成',
      orphan_rows['trial-verdict-999.json']['reason'] == 'no_decision'
      and orphan_rows['trial-verdict-3.json']['reason'] == 'decision_not_done')
with Session() as s:
    row1, row2, row4 = s.get(Decision, 1), s.get(Decision, 2), s.get(Decision, 4)
    check('修复后裁决1/2可下载', evidence.read_verified(row1)[0] == evidence.expected_bytes(results[0])
          and evidence.read_verified(row2)[0] == evidence.expected_bytes(results[1]))
    check('冻结校验不符仍拒绝下载', _blocked(s, 4))
    # 已发布裁决的结论字段未被对账改动
    row2.action = 'publish_b'
    row2.published_by = 'admin@demo.local'
    row2.published_at = datetime.now(timezone.utc).isoformat()
    s.commit()
    before = (row2.action, row2.published_at, row2.result['checksum'])
    run_reconciliation(s, _new_task(s))
    row2 = s.get(Decision, 2)
    check('已发布裁决结论与留存依据不变', (row2.action, row2.published_at, row2.result['checksum']) == before)

# ---------- 4. 幂等：重跑不重复生成 ----------
print('\n[4] 失败后重试不重复生成证据')
qfiles_before = set(p.name for p in (ART / 'quarantine').iterdir())
with Session() as s:
    run_reconciliation(s, _new_task(s))
    tasks = s.query(ReconciliationTask).order_by(ReconciliationTask.id).all()
    last = tasks[-1]
    check('重试任务仍因不可修复项 failed', last.state == 'failed')
    check('完整项动作均为 none', all(i['action'] == 'none' for i in last.report['items'] if i['status'] == 'intact'))
    check('未新增隔离文件', set(p.name for p in (ART / 'quarantine').iterdir()) == qfiles_before)
    check('留存文件字节未变化', f1.read_bytes() == evidence.expected_bytes(results[0])
          and f2.read_bytes() == evidence.expected_bytes(results[1]))

# ---------- 5. 真实 worker 进程：队列、回收、并发 ----------
print('\n[5] worker 进程：启动回收 + 两队列 + 并发下载')
# 5a. 制造一个 running 状态的裁决任务（模拟 worker 崩溃），文件缺失
with Session() as s:
    d5 = Decision(id=5, experiment_id=exp_id, state='running', snapshot=snap, result=None)
    s.add(d5)
    # 一个 running 对账任务也应被回收
    tr = ReconciliationTask(id=20, state='running', created_by='admin@demo.local', created_at=datetime.now(timezone.utc).isoformat())
    s.add(tr)
    s.commit()
worker = subprocess.Popen([sys.executable, '-m', 'src.worker'], cwd=str(ROOT / 'backend'),
                          env={**os.environ}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    _wait(lambda: _state(Decision, 5) == 'done', timeout=30)
    check('崩溃 running 裁决被回收并重算完成', _state(Decision, 5) == 'done')
    check('worker 原子落盘且可核验', (ART / 'trial-verdict-5.json').exists()
          and evidence.verify(_get(Decision, 5))['status'] == 'intact')
    _wait(lambda: _state(ReconciliationTask, 20) in ('done', 'failed'), timeout=30)
    check('running 对账任务被回收执行', _state(ReconciliationTask, 20) in ('done', 'failed'))

    # 5b. 并发：发起新对账的同时持续下载，期间不得读到半成品
    import redis as redislib  # 已安装
    q = redislib.Redis.from_url(redis_srv.url)
    errors = []
    rejected = {'n': 0}
    stop = threading.Event()

    def downloader():
        while not stop.is_set():
            try:
                with Session() as s:
                    evidence.read_verified(s.get(Decision, 5))
            except evidence.EvidenceError:
                rejected['n'] += 1  # 明确拒绝是允许路径，关键是不会返回半成品字节
            except Exception as e:  # 其它异常才是缺陷
                errors.append(f'{type(e).__name__}: {e}')
            time.sleep(0.002)

    th = threading.Thread(target=downloader)
    th.start()
    # 删除文件制造缺失后入对账队列
    os.remove(ART / 'trial-verdict-5.json')
    with Session() as s:
        tnew = ReconciliationTask(state='queued', created_by='admin@demo.local',
                                  created_at=datetime.now(timezone.utc).isoformat())
        s.add(tnew)
        s.commit()
        new_id = tnew.id
    q.rpush('trial:reconciliations', new_id)
    _wait(lambda: _state(ReconciliationTask, new_id) in ('done', 'failed'), timeout=30)
    stop.set()
    th.join()
    check('并发期间所有下载要么成功核验、要么被明确拒绝，无半成品读取', errors == [], errors[:2])
    check('文件缺失窗口内下载请求被拒绝而非拼装返回', rejected['n'] > 0)
    check('对账修复后文件恢复完整', evidence.verify(_get(Decision, 5))['status'] == 'intact')
finally:
    worker.terminate()
    worker.wait(timeout=10)

# ---------- 6. HTTP 层：拒绝原因 / viewer 权限 ----------
print('\n[6] HTTP API 行为')
worker6 = subprocess.Popen([sys.executable, '-m', 'src.worker'], cwd=str(ROOT / 'backend'),
                           env={**os.environ}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
config = uvicorn.Config('src.main:app', host='127.0.0.1', port=18099, log_level='warning')
server = uvicorn.Server(config)
threading.Thread(target=server.run, daemon=True).start()
_wait_http('http://127.0.0.1:18099/health', timeout=20)


def http(method, path, token=None, body=None):
    req = urllib.request.Request('http://127.0.0.1:18099' + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Content-Type': 'application/json', **({'Authorization': 'Bearer ' + token} if token else {})})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def login(email):
    st, _, bd = http('POST', '/auth/login', body={'email': email, 'password': 'Demo123!'})
    return json.loads(bd)['token']


admin_tok = login('admin@demo.local')
viewer_tok = login('viewer@demo.local')
st, _, bd = http('GET', '/decisions/1/export', admin_tok)
check('完整证据 HTTP 下载 200 且带校验头', st == 200 and json.loads(bd) == results[0]
      and len(_.get('X-Evidence-SHA256', '')) == 64)
# 裁决4 冻结校验不符 → 409 并说明原因
st, _, bd = http('GET', '/decisions/4/export', admin_tok)
check('来源无法确认时 HTTP 拒绝下载 409', st == 409 and '拒绝下载' in json.loads(bd)['detail'], bd.decode())
st, _, bd = http('POST', '/reconciliations', viewer_tok, {'confirm': True})
check('viewer 不能发起对账', st == 403)
st, _, bd = http('GET', '/reconciliations', viewer_tok)
check('viewer 可查看对账任务与差异', st == 200 and len(json.loads(bd)) >= 3)
st, _, bd = http('POST', '/reconciliations', admin_tok, {'confirm': True})
new_task = json.loads(bd)
check('analyst/admin 可发起对账', st == 200 and new_task['state'] == 'queued')
_wait(lambda: _state(ReconciliationTask, new_task['id']) in ('done', 'failed'), timeout=30)
st, _, bd = http('GET', f"/reconciliations/{new_task['id']}", admin_tok)
detail = json.loads(bd)
check('页面可取对账差异与修复结果', st == 200 and detail['report']['summary']['done_decisions'] == 4)
# 重复发起：有活跃任务时 409
with Session() as s:
    busy = ReconciliationTask(state='queued', created_by='admin@demo.local', created_at=datetime.now(timezone.utc).isoformat())
    s.add(busy)
    s.commit()
    busy_id = busy.id
st, _, bd = http('POST', '/reconciliations', admin_tok, {'confirm': True})
check('已有活跃对账任务时拒绝重复发起', st == 409)
with Session() as s:
    s.get(ReconciliationTask, busy_id).state = 'failed'
    s.commit()
worker6.terminate()
worker6.wait(timeout=10)

print(f'\n==== 通过 {len(PASS)} 项，失败 {len(FAIL)} 项 ====')
if FAIL:
    print('失败项：', FAIL)
    sys.exit(1)
print('全部通过')
