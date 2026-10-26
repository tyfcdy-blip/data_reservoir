import { $, state, api, esc, icon, empty, table, field, input, select, val, dialog, formFooter, busy, showError, toast, datasetOptions, day, badge, dateTime, number } from './core.js';
export { keysPage, bindKeys } from './key-permissions.js';

export const statusLabels={pending:['待处理','warn'],running:['同步中','blue'],uploading:['上传中','blue'],succeeded:['已发布','good'],failed:['失败','bad'],cancelled:['已取消','']};
export function jobsTable(jobs,showDataset=true) {
  return table([...(showDataset?['数据表']:[]),'业务日期','状态','记录数','开始 / 创建时间','结果'],jobs.map(job=>[...(showDataset?[`<code>${esc(job.dataset_slug)}</code>`]:[]),esc(job.business_date),badge(...(statusLabels[job.status] || [job.status,''])),`${esc(number(job.row_count))}${job.status==='succeeded' && job.write_mode==='upsert' && job.published_row_count!=null?`<span class="secondary">合并后 ${esc(number(job.published_row_count))}</span>`:''}`,esc(dateTime(job.started_at || job.created_at)),job.error_message?`<span class="danger-text" title="${esc(job.error_message)}">${esc(job.error_message)}</span>`:job.status==='succeeded'?'数据已发布':job.status==='failed'?esc(job.error_code || '同步失败'):'—']));
}
export async function syncPage() {
  const status=await api('/api/v1/admin/status');state.status=status;
  return {status,html:`<div class="page-heading"><div><h1>同步记录</h1><p>查看任务进度、已发布日期与同步失败原因。页面每 15 秒更新。</p></div><div class="actions"><button class="btn" id="refresh-sync">${icon('sync')}刷新</button><button class="btn primary" id="manual-sync"${!status.configured_sources.length?' disabled':''}>手动同步</button></div></div><section class="panel"><div class="panel-heading"><h2>数据覆盖</h2><div class="actions">${workerBadge(status.workers)}</div></div>${status.coverage.length?table(['数据表','最早日期','最新日期','已发布天数','当前记录数'],status.coverage.map(coverage=>[`<code>${esc(coverage.dataset_slug)}</code>`,esc(coverage.first_date),esc(coverage.last_date),`${esc(number(coverage.published_days))} 天`,esc(number(coverage.row_count))])):empty('暂未发布数据','配置接口接入后，可从上方发起首次同步。')}</section><section class="panel"><div class="panel-heading"><h2>最近同步任务</h2><span class="subtle">最近 100 个任务；每个业务日期独立发布</span></div>${status.recent_batches.length?jobsTable(status.recent_batches):empty('暂无同步任务','手动同步或开启每日同步后，任务进度会显示在这里。')}</section>`};
}
export function workerBadge(workers) { return workers.some(worker=>worker.healthy)?badge('同步服务运行中','good'):badge('同步服务未就绪','warn'); }
export function bindSync(root,data,reload) {
  $('#refresh-sync',root).onclick=reload;$('#manual-sync',root).onclick=()=>syncDialog(data.status.configured_sources[0],reload,data.status.configured_sources);
}
export function syncDialog(slug,onComplete,sourceSlugs) {
  const allowed=sourceSlugs || [slug];
  const node=dialog('手动同步数据','所选范围按天生成任务，按来源配置的写入方式处理，成功后新数据才对外可见。',`<form id="sync-form" class="stack"><div class="form-error" role="alert"></div>${field('数据表',select('dataset_slug',datasetOptions().filter(([value])=>allowed.includes(value)),slug))}<div class="form-grid">${field('开始日期',input('start_date',day(-1),'type="date" required'))}${field('结束日期',input('end_date',day(-1),'type="date" required'))}</div><div class="notice">日期范围最多 180 天，且须在数据表保留期限内。按主键更新会保留未返回的旧记录，整日替换会删除未返回的旧记录；具体以来源配置为准。失败时保留上一份已发布数据。</div></form>`,formFooter('sync-form','开始同步'));
  const form=$('#sync-form',node);form.onsubmit=event=>{event.preventDefault();busy($('[form=sync-form]',node),async()=>{const error=$('.form-error',form);error.textContent='';try{await api(`/api/v1/admin/datasets/${encodeURIComponent(val(form,'dataset_slug'))}/sync`,{method:'POST',body:{start_date:val(form,'start_date'),end_date:val(form,'end_date')}});node.close();toast('同步任务已提交，可在同步记录查看进度');if(onComplete)onComplete();else location.hash='sync';}catch(exception){showError(error,exception);}});};
}
