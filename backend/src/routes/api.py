import os
from datetime import timezone,datetime
import redis
from fastapi import APIRouter,Depends,HTTPException,Response
from sqlalchemy.exc import IntegrityError
from src.config.database import db
from src.models.entities import User,Experiment,Event,Decision,ReconciliationTask,Audit
from src.schemas.business import Login,ExperimentInput,EventInput,RunInput,RationaleInput,PublishInput,ReconciliationInput
from src.middlewares.auth import current,editor,admin
from src.utils.security import verify,token
from src.services.business import audit,dump,utc
from src.services import evidence
from src.services.evidence import EvidenceError
from src.controllers.business import experiment,decision,validate_event,snapshot,publish
router=APIRouter()
@router.post('/auth/login')
def login(data:Login,s=Depends(db)):
 u=s.query(User).filter_by(email=data.email).first()
 if not u or not verify(data.password,u.password):raise HTTPException(401,'账号或密码错误')
 return {'token':token(u),'user':dump(u)}
@router.get('/me')
def me(u=Depends(current)):return dump(u)
@router.get('/experiments')
def experiments(u=Depends(current),s=Depends(db)):
 return [{**dump(e),'exposures':s.query(Event).filter_by(experiment_id=e.id,kind='exposure').count()} for e in s.query(Experiment).order_by(Experiment.id.desc())]
@router.post('/experiments')
def create_experiment(data:ExperimentInput,u=Depends(editor),s=Depends(db)):
 d=data.model_dump();d['starts_at']=utc(data.starts_at);d['ends_at']=utc(data.ends_at);r=Experiment(**d);s.add(r)
 try:s.commit()
 except IntegrityError:s.rollback();raise HTTPException(409,'实验编号已存在')
 audit(s,u,'建立实验 '+r.code);return dump(r)
@router.put('/experiments/{i}')
def edit_experiment(i:int,data:ExperimentInput,u=Depends(editor),s=Depends(db)):
 r=experiment(s,i)
 if s.query(Event).filter_by(experiment_id=i).first():raise HTTPException(409,'已有事件，禁止事后修改实验定义；请建立新实验')
 d=data.model_dump();d['starts_at']=utc(data.starts_at);d['ends_at']=utc(data.ends_at)
 for k,v in d.items():setattr(r,k,v)
 try:s.commit()
 except IntegrityError:s.rollback();raise HTTPException(409,'实验编号已存在')
 audit(s,u,'修订实验 '+str(i));return dump(r)
@router.get('/events')
def events(experiment_id:int|None=None,u=Depends(current),s=Depends(db)):
 q=s.query(Event)
 if experiment_id:q=q.filter_by(experiment_id=experiment_id)
 return [dump(e) for e in q.order_by(Event.id.desc())]
@router.post('/events')
def create_event(data:EventInput,u=Depends(editor),s=Depends(db)):
 stamp=validate_event(s,data);d=data.model_dump();d['occurred_at']=stamp;r=Event(**d);s.add(r)
 try:s.commit()
 except IntegrityError:s.rollback();raise HTTPException(409,'该用户事件已存在')
 audit(s,u,'采集事件 '+str(r.id));return dump(r)
@router.get('/decisions')
def decisions(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Decision).order_by(Decision.id.desc())]
@router.post('/decisions')
def run(data:RunInput,u=Depends(editor),s=Depends(db)):
 exp=experiment(s,data.experiment_id)
 if s.query(Decision).filter(Decision.state.in_(['queued','running'])).count()>=10:raise HTTPException(429,'队列已满，请等待任务完成')
 r=Decision(experiment_id=exp.id,snapshot=snapshot(s,exp));s.add(r);s.commit()
 try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush('trial:decisions',r.id)
 except redis.RedisError:r.state='failed';r.error='队列暂不可用，恢复后重新提交';s.commit();raise HTTPException(503,r.error)
 audit(s,u,'提交实验裁决 '+str(r.id));return dump(r)
@router.get('/decisions/{i}')
def detail(i:int,u=Depends(current),s=Depends(db)):return dump(decision(s,i))
@router.put('/decisions/{i}/rationale')
def rationale(i:int,data:RationaleInput,u=Depends(editor),s=Depends(db)):
 r=decision(s,i)
 if r.published_at:raise HTTPException(409,'已发布裁决不可覆盖，请重新创建裁决')
 if r.state!='done':raise HTTPException(409,'计算完成后再填写裁决理由')
 r.rationale=data.rationale;s.commit();audit(s,u,'保存裁决理由 '+str(i));return dump(r)
@router.post('/decisions/{i}/publish')
def release(i:int,data:PublishInput,u=Depends(admin),s=Depends(db)):
 r=publish(s,decision(s,i),data.action,u);audit(s,u,'发布裁决 '+str(i)+' '+data.action);return dump(r)
@router.get('/decisions/{i}/export')
def export(i:int,u=Depends(current),s=Depends(db)):
 r=decision(s,i)
 if r.state!='done':raise HTTPException(409,'尚无成功结果')
 try:
  data,digest=evidence.read_verified(r)
 except EvidenceError as e:
  raise HTTPException(409,f'证据核验未通过，已拒绝下载：{e}。请发起证据对账修复后再下载')
 # 直接回传留存文件原字节，不重新拼装；附带文件校验和供下载方复核
 return Response(content=data,media_type='application/json',headers={
  'Content-Disposition':f'attachment; filename="trial-verdict-{i}.json"',
  'X-Evidence-SHA256':digest,
  'X-Evidence-Version':(r.result or {}).get('version','')})
@router.post('/reconciliations')
def create_reconciliation(data:ReconciliationInput,u=Depends(editor),s=Depends(db)):
 active=s.query(ReconciliationTask).filter(ReconciliationTask.state.in_(['queued','running'])).count()
 if active:raise HTTPException(409,'已有证据对账任务在处理中，请等待其完成后再发起')
 if not s.query(Decision).filter_by(state='done').first():raise HTTPException(409,'暂无计算完成的裁决，没有可对账的证据')
 task=ReconciliationTask(state='queued',created_by=u.email,created_at=datetime.now(timezone.utc).isoformat())
 s.add(task);s.commit()
 try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush('trial:reconciliations',task.id)
 except redis.RedisError:
  task.state='failed';task.error='队列暂不可用，恢复后重新发起';s.commit();raise HTTPException(503,task.error)
 audit(s,u,'发起证据对账任务 '+str(task.id));return dump(task)
@router.get('/reconciliations')
def list_reconciliations(u=Depends(current),s=Depends(db)):
 return [dump(t) for t in s.query(ReconciliationTask).order_by(ReconciliationTask.id.desc()).limit(50).all()]
@router.get('/reconciliations/{i}')
def reconciliation_detail(i:int,u=Depends(current),s=Depends(db)):
 t=s.get(ReconciliationTask,i)
 if not t:raise HTTPException(404,'对账任务不存在')
 return dump(t)
@router.get('/audits')
def audits(u=Depends(admin),s=Depends(db)):return [dump(r) for r in s.query(Audit).order_by(Audit.id.desc()).limit(100)]
