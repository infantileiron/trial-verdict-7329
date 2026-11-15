import os,time,logging
from datetime import datetime,timezone
from pathlib import Path
import redis
from src.config.database import Session
from src.models.entities import Decision, ReconciliationTask
from src.services.analysis import calculate
from src.services import evidence
from src.services.reconciliation import run_reconciliation
r=redis.Redis.from_url(os.environ['REDIS_URL'])
DECISION_QUEUE='trial:decisions'
RECON_QUEUE='trial:reconciliations'
with Session() as recovery:
 # 启动回收：中断的任务退回队列，重试时先核对已有证据，不重复生成
 for pending in recovery.query(Decision).filter(Decision.state.in_(['queued','running'])).all():
  pending.state='queued';r.rpush(DECISION_QUEUE,pending.id)
 for pending in recovery.query(ReconciliationTask).filter(ReconciliationTask.state.in_(['queued','running'])).all():
  pending.state='queued';r.rpush(RECON_QUEUE,pending.id)
 recovery.commit()
def handle_decision(raw_id):
 with Session() as s:
  row=s.get(Decision,int(raw_id))
  if not row or row.state!='queued':return
  row.state='running';s.commit()
  try:
   row.result=calculate(row.snapshot);row.state='done'
   # 原子落盘：读者只能看到完整文件；重试时若文件已完整则不重复写
   item=evidence.verify(row)
   if item['status']!='intact':
    evidence._ensure_dirs()
    evidence._atomic_write(evidence.artifact_path(row.id),evidence.expected_bytes(row.result))
  except Exception as e:
   row.state='failed';row.error=str(e)
  s.commit()
def handle_reconciliation(raw_id):
 with Session() as s:
  task=s.get(ReconciliationTask,int(raw_id))
  if not task or task.state!='queued':return
  task.state='running';task.started_at=datetime.now(timezone.utc).isoformat();s.commit()
  run_reconciliation(s,task.id)
while True:
 Path('/tmp/heartbeat').touch()
 try:
  # 优先消费对账任务，两队列互不阻塞
  item=r.blpop([RECON_QUEUE,DECISION_QUEUE],timeout=3)
  if not item:continue
  queue,raw=item[0].decode() if isinstance(item[0],bytes) else item[0],item[1]
  if queue==RECON_QUEUE:handle_reconciliation(raw)
  else:handle_decision(raw)
 except Exception:
  logging.exception('worker');time.sleep(2)
