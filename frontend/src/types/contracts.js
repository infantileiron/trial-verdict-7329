/** @typedef {{id:number,code:string,name:string,starts_at:string,ends_at:string,a_ratio:number,hypothesis:string}} Experiment */
/** @typedef {{id:number,experiment_id:number,user_key:string,group:'A'|'B',kind:'exposure'|'activation',occurred_at:string}} TrialEvent */
/** @typedef {{id:number,experiment_id:number,state:string,result:object,snapshot:object,rationale:string,action:string,published_at:string}} Decision */
/** @typedef {{id:number,state:'queued'|'running'|'done'|'failed',report:null|{checked_at:string,summary:object,items:Array<object>,orphans:Array<object>},error:string,created_by:string,created_at:string,started_at:string,finished_at:string}} ReconciliationTask */
export const CONTRACT_VERSION='1.0';
