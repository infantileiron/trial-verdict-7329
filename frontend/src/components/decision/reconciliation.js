import {state} from '../../stores/session.js';
import {esc} from '../../utils/html.js';
import {badge} from '../common/status.js';
import {time} from '../../utils/format.js';

const STATUS_TEXT={intact:'完整',missing:'文件缺失',content_mismatch:'内容不符',checksum_mismatch:'冻结校验不符'};
const ACTION_TEXT={none:'无需处理',repaired:'已补回',quarantined_repaired:'已隔离并恢复',unrepairable:'不可修复',repair_failed:'修复失败'};
const ROW_CLASS={intact:'',missing:'warning',content_mismatch:'warning',checksum_mismatch:'error'};

export function render(){
 const tasks=state.reconciliations,t=state.reconSelected;
 return `<section class="recon"><h2>裁决证据对账与修复</h2>
 <p class="notice">对冻结输入（快照校验和）、库内原裁决结果与留存文件三方对账；仅修复缺失/内容不符项，依据原裁决恢复，不读取新增事件、不重新检验；损坏原件移入 quarantine 保留。下载只返回完整且核验通过的证据，来源无法确认时拒绝。</p>
 <div class="recon-head"><form id="reconcile"><button class="primary" ${state.user.role==='viewer'?'disabled':''}>发起对账并自动修复异常项</button></form>
 <button id="recon-refresh">刷新任务状态</button></div>
 ${tasks.length?`<div class="records">${tasks.map(x=>`<button data-recon="${x.id}" class="${x.id===t?.id?'active':''}">#${x.id} ${badge(x.state)} ${time(x.created_at)}</button>`).join('')}</div>`:'<p class="muted">暂无对账任务。</p>'}
 ${t?renderDetail(t):''}</section>`;
}
function renderDetail(t){
 if(t.state==='queued'||t.state==='running')return `<p class="notice">任务 ${t.state==='queued'?'排队中':'执行中'}…完成后可刷新查看差异与修复结果。</p>`;
 if(t.state==='failed'&&!t.report)return `<p class="notice warning">${esc(t.error||'任务失败，可重新发起；已完整的证据不会重复生成。')}</p>`;
 const r=t.report||{},sm=r.summary||{};
 return `<div class="notice ${t.state==='failed'?'warning':'good'}">${t.state==='failed'?esc(t.error):'对账完成，所有留存证据与原裁决一致。'} 对账时间 ${time(r.checked_at)}</div>
 <table><thead><tr><th>指标</th><th>已完成裁决</th><th>完整（终态）</th><th>发现缺失</th><th>发现内容不符</th><th>冻结校验不符</th><th>已修复</th><th>不可修复</th><th>无所属裁决文件</th></tr></thead>
 <tbody><tr><td>数量</td><td>${sm.done_decisions??0}</td><td>${sm.intact??0}</td><td>${sm.found_missing??0}</td><td>${sm.found_content_mismatch??0}</td><td>${sm.checksum_mismatch??0}</td><td>${sm.repaired??0}</td><td>${sm.unrepairable??0}</td><td>${sm.orphan_files??0}</td></tr></tbody></table>
 <h3>逐项差异与修复结果</h3>
 <table><thead><tr><th>裁决</th><th>原始差异</th><th>修复后状态</th><th>处理动作</th><th>说明</th><th>留存文件 SHA256（前16位）</th><th>隔离原件</th></tr></thead><tbody>
 ${(r.items||[]).map(i=>`<tr class="${ROW_CLASS[i.initial_status]||''}"><td>#${i.decision_id}</td><td>${esc(STATUS_TEXT[i.initial_status]||i.initial_status)}</td><td>${esc(STATUS_TEXT[i.status]||i.status)}</td><td>${esc(ACTION_TEXT[i.action]||i.action||'—')}</td><td>${esc(i.detail||'')}</td><td><code>${esc((i.file_sha256||'—').slice(0,16))}</code></td><td>${i.quarantine_file?esc(i.quarantine_file):'—'}</td></tr>`).join('')||'<tr><td colspan="7">无已完成裁决。</td></tr>'}
 </tbody></table>
 <h3>无所属裁决的留存文件</h3>
 ${(r.orphans||[]).length?`<table><thead><tr><th>文件</th><th>原因</th><th>说明</th><th>SHA256（前16位）</th></tr></thead><tbody>
 ${r.orphans.map(o=>`<tr class="warning"><td><code>${esc(o.file)}</code></td><td>${esc(o.reason)}</td><td>${esc(o.detail)}</td><td><code>${esc((o.file_sha256||'—').slice(0,16))}</code></td></tr>`).join('')}</tbody></table>
 <p class="muted">孤立文件不纳入下载、不自动删除，保留现场供追查。</p>`:'<p class="muted">未发现无所属裁决的文件。</p>'}`;
}
