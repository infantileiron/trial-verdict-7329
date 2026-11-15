"""证据对账任务执行器：被 worker 调用，全程不读取新增事件、不重新检验。"""
from datetime import datetime, timezone

from src.models.entities import Decision, ReconciliationTask, Audit
from src.services import evidence


def _now():
    return datetime.now(timezone.utc).isoformat()


def run_reconciliation(session, task_id):
    task = session.get(ReconciliationTask, task_id)
    if not task or task.state != 'running':
        return
    try:
        rows = session.query(Decision).filter_by(state='done').order_by(Decision.id).all()
        items = []
        for row in rows:
            item = evidence.verify(row)
            item['initial_status'] = item['status']  # 保留修复前发现的差异
            if item['status'] in evidence.REPAIRABLE:
                item = evidence.repair(row, item)
                item['initial_status'] = item.get('initial_status') or item['status']
            elif item['status'] == evidence.CHECKSUM_MISMATCH:
                item['action'] = 'unrepairable'
            items.append(item)
        orphans = evidence.scan_orphans(session, Decision)
        summary = {
            'done_decisions': len(rows),
            'intact': sum(1 for i in items if i['status'] == evidence.INTACT),
            'missing': sum(1 for i in items if i['status'] == evidence.MISSING),
            'content_mismatch': sum(1 for i in items if i['status'] == evidence.CONTENT_MISMATCH),
            'checksum_mismatch': sum(1 for i in items if i['status'] == evidence.CHECKSUM_MISMATCH),
            'found_missing': sum(1 for i in items if i.get('initial_status') == evidence.MISSING),
            'found_content_mismatch': sum(1 for i in items if i.get('initial_status') == evidence.CONTENT_MISMATCH),
            'repaired': sum(1 for i in items if i['action'] in ('repaired', 'quarantined_repaired')),
            'unrepairable': sum(1 for i in items if i['action'] == 'unrepairable'),
            'repair_failed': sum(1 for i in items if i['action'] == 'repair_failed'),
            'orphan_files': len(orphans),
        }
        report = {'checked_at': _now(), 'summary': summary, 'items': items, 'orphans': orphans}
        task.report = report
        failed = summary['unrepairable'] > 0 or summary['repair_failed'] > 0
        task.state = 'failed' if failed else 'done'
        if failed:
            task.error = ('存在不可修复或修复失败的证据：'
                          f"不可修复 {summary['unrepairable']} 项，修复失败 {summary['repair_failed']} 项；"
                          '可重新发起对账，已完整的证据不会重复生成')
        task.finished_at = _now()
        session.add(Audit(actor=task.created_by or 'worker',
                          action=f'证据对账任务 {task.id} {task.state}（修复 {summary["repaired"]} 项，孤立文件 {summary["orphan_files"]} 个）',
                          created=_now()))
        session.commit()
    except Exception as e:
        session.rollback()
        task = session.get(ReconciliationTask, task_id)
        if task:
            task.state = 'failed'
            task.error = f'对账任务执行异常：{e}；可重新发起，已完整证据不会重复生成'
            task.finished_at = _now()
            session.commit()
