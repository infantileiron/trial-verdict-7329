// 前端渲染冒烟测试：用最小 DOM 垫片加载 ES modules，验证对账面板在各状态下可渲染
import {state} from '../frontend/src/stores/session.js';
import * as decisions from '../frontend/src/pages/decisions.js';

const snapshot={experiment:{id:1,code:'LJ-1115-A',name:'成熟对照'}};
state.user={email:'analyst@demo.local',role:'analyst'};
state.experiments=[{id:1,name:'成熟对照'}];
state.decisions=[
 {id:1,experiment_id:1,state:'done',snapshot,result:{rate_a:.2,rate_b:.3,difference:.1,ci_low:.05,ci_high:.15,z:3,p_value:.002,srm_p:1,srm_chi2:0,expected_a:600,expected_b:600,release_eligible:true,block_reasons:[],activated_a:120,n_a:600,activated_b:180,n_b:600},error:'',rationale:'',action:'',published_at:''},
 {id:2,experiment_id:1,state:'queued',snapshot,result:null,error:'',rationale:'',action:'',published_at:''},
];
state.selected=state.decisions[0];

// 无对账任务
let html=decisions.render();
assert(html.includes('裁决证据对账与修复')&&html.includes('发起对账并自动修复异常项'),'初始面板缺失');
assert(html.includes('下载冻结证据 JSON'),'下载按钮缺失');

// queued
state.reconciliations=[{id:1,state:'queued',report:null,error:'',created_at:new Date().toISOString()}];
state.reconSelected=state.reconciliations[0];
html=decisions.render();
assert(html.includes('排队中'),'queued 状态未展示');

// done + 全量报告
state.reconciliations=[{id:2,state:'done',created_at:new Date().toISOString(),error:'',report:{
 checked_at:new Date().toISOString(),
 summary:{done_decisions:2,intact:1,found_missing:1,found_content_mismatch:0,checksum_mismatch:0,repaired:1,unrepairable:0,repair_failed:0,orphan_files:1},
 items:[
  {decision_id:1,initial_status:'intact',status:'intact',action:'none',detail:'三者一致',file_sha256:'a'.repeat(64),quarantine_file:null},
  {decision_id:2,initial_status:'missing',status:'intact',action:'repaired',detail:'已依据库内原裁决结果补回缺失证据，修复后复核通过',file_sha256:'b'.repeat(64),quarantine_file:null},
 ],
 orphans:[{file:'trial-verdict-999.json',decision_id:999,reason:'no_decision',detail:'留存文件无所属裁决记录',file_sha256:'c'.repeat(64)}],
}}];
state.reconSelected=state.reconciliations[0];
html=decisions.render();
assert(html.includes('文件缺失')&&html.includes('已补回')&&html.includes('trial-verdict-999.json'),'done 报告差异未展示');
assert(html.includes('对账完成，所有留存证据与原裁决一致'),'完成结论缺失');

// failed（不可修复项）
state.reconciliations=[{id:3,state:'failed',created_at:new Date().toISOString(),error:'存在不可修复或修复失败的证据：不可修复 1 项',report:{
 checked_at:new Date().toISOString(),
 summary:{done_decisions:1,intact:0,found_missing:0,found_content_mismatch:0,checksum_mismatch:1,repaired:0,unrepairable:1,repair_failed:0,orphan_files:0},
 items:[{decision_id:4,initial_status:'checksum_mismatch',status:'checksum_mismatch',action:'unrepairable',detail:'结果校验和与冻结输入快照不一致',file_sha256:null,quarantine_file:null}],
 orphans:[]}}];
state.reconSelected=state.reconciliations[0];
html=decisions.render();
assert(html.includes('冻结校验不符')&&html.includes('不可修复')&&html.includes('不可修复或修复失败'),'failed 展示缺失');

// viewer 禁用发起按钮
state.user={email:'viewer@demo.local',role:'viewer'};
html=decisions.render();
assert(/<button class="primary" disabled>/.test(html),'viewer 未被禁用发起按钮');

console.log('前端渲染冒烟测试全部通过');
function assert(cond,msg){if(!cond)throw new Error(msg)}
