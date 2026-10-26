// 以模块实际地址定位服务根目录，兼容根路径及反向代理剥离前缀的部署。
const serviceRoot = new URL('../', import.meta.url);
const servicePrefix = serviceRoot.pathname.replace(/\/$/, '');
const sessionKey = servicePrefix ? `data-service-session:${servicePrefix}` : 'data-service-session';
export function servicePath(path) {
  if (typeof path !== 'string' || !path.startsWith('/') || path.startsWith('//') || path.includes('\\')) throw new Error('服务路径无效');
  return servicePrefix + path;
}
export const serviceUrl = path => new URL(servicePath(path), serviceRoot.origin).href;
export const state = { token: sessionStorage.getItem(sessionKey) || '', datasets: [], status: null };
export const esc = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
export const text = value => value == null ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value);
export const number = value => Number(value || 0).toLocaleString('zh-CN');
export const dateTime = value => value ? new Date(value).toLocaleString('zh-CN', {hour12:false}) : '—';
export function day(offset = 0) { const parts = new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date()); const value = type => parts.find(part=>part.type===type).value; const date = new Date(`${value('year')}-${value('month')}-${value('day')}T00:00:00Z`); date.setUTCDate(date.getUTCDate()+offset); return date.toISOString().slice(0,10); }
export const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true">${({overview:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',tables:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>',sources:'<path d="M8 3v5M16 3v5M5 8h14v3a7 7 0 0 1-14 0V8M12 18v4"/>',services:'<path d="M4 5h16M7 12h10M10 19h4M4 3v4M20 3v4M7 10v4M17 10v4M10 17v4M14 17v4"/>',keys:'<circle cx="8" cy="9" r="5"/><path d="m12 13 8 8m-4-4 3-3m-1 5 3-3"/>',llm:'<rect x="4" y="5" width="16" height="15" rx="3"/><path d="M9 11h.01M15 11h.01M9 16h6M12 2v3"/>',sync:'<path d="M20 8A8 8 0 0 0 6 5L3 8m0-5v5h5M4 16a8 8 0 0 0 14 3l3-3m0 5v-5h-5"/>',plus:'<path d="M12 5v14M5 12h14"/>',close:'<path d="m6 6 12 12M6 18 18 6"/>',menu:'<path d="M4 6h16M4 12h16M4 18h16"/>',arrow:'<path d="M5 12h14m-5-5 5 5-5 5"/>',copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',logout:'<path d="M9 4H4v16h5m4-13 5 5-5 5m-5-5h11"/>'})[name] || ''}</svg>`;
export function setToken(token) { state.token = token; if(token) sessionStorage.setItem(sessionKey, token); else sessionStorage.removeItem(sessionKey); }
export async function api(path, options = {}) {
  const token = state.token;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeout || 125000);
  try {
    const response = await fetch(servicePath(path), {method:options.method || 'GET',headers:{...(options.body !== undefined ? {'Content-Type':'application/json'} : {}),...(token ? {Authorization:`Bearer ${token}`} : {})},body:options.body === undefined ? undefined : JSON.stringify(options.body),signal:controller.signal,credentials:'same-origin',cache:'no-store'});
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      if(response.status === 401 && token === state.token && path !== '/api/v1/auth/login') { setToken(''); window.dispatchEvent(new Event('session-expired')); }
      const error = new Error(payload?.message || `请求失败（${response.status}）`);
      error.status = response.status; error.details = payload?.details; error.code = payload?.error_code; throw error;
    }
    return payload?.data ?? payload;
  } catch(error) { if(error.name === 'AbortError') throw new Error('请求超时，请缩小范围后重试。'); throw error; }
  finally { clearTimeout(timeout); }
}
export async function refreshDatasets() { state.datasets = await api('/api/v1/datasets'); return state.datasets; }
export function showError(element, error) { element.textContent = `${error.message || error}${error.details ? '\n' + JSON.stringify(error.details) : ''}`; element.hidden = false; }
export function toast(message, error = false) { const node = document.createElement('div'); node.className = `toast${error ? ' error' : ''}`; node.textContent = message; $('#notifications').append(node); setTimeout(() => node.remove(), 6000); }
export async function busy(button, action) { if(button.disabled) return; const label = button.innerHTML, modal = button.closest('dialog'), generation = modal?.dataset.generation; const controls = modal ? $$('button',modal).map(control=>[control,control.disabled]) : [[button,button.disabled]]; controls.forEach(([control])=>control.disabled=true); if(modal)modal.dataset.busy='true';button.setAttribute('aria-busy','true'); try{return await action();}finally{controls.forEach(([control,disabled])=>control.disabled=disabled);button.removeAttribute('aria-busy');button.innerHTML=label;if(modal && modal.dataset.generation===generation)delete modal.dataset.busy;} }
export function badge(label, variant = '') { return `<span class="badge ${esc(variant)}"><span class="badge-dot"></span>${esc(label)}</span>`; }
export function empty(title, description, action = '') { return `<div class="empty"><div class="empty-symbol">${icon('tables')}</div><strong>${esc(title)}</strong><p>${esc(description)}</p>${action}</div>`; }
export function panel(title, content, action='') { return `<section class="panel"><div class="panel-heading"><h2>${title}</h2>${action}</div>${content}</section>`; }
export function table(headers, rows) { return `<div class="table-wrap"><table><thead><tr>${headers.map(item=>`<th>${item}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(cell=>`<td>${cell}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`; }
export function field(label, control, hint='', full=false) { return `<label class="field${full?' full':''}"><span>${esc(label)}</span>${control}${hint?`<small>${esc(hint)}</small>`:''}</label>`; }
export function input(name, value='', extra='') { return `<input name="${esc(name)}" value="${esc(value)}" ${extra}>`; }
export function select(name, options, value='', extra='') { return `<select name="${esc(name)}" ${extra}>${options.map(option=>{const [key,label] = Array.isArray(option)?option:[option,option];return `<option value="${esc(key)}"${String(key)===String(value)?' selected':''}>${esc(label)}</option>`;}).join('')}</select>`; }
export function check(name, label, checked=false, extra='') { return `<label class="check"><input type="checkbox" name="${esc(name)}"${checked?' checked':''} ${extra}>${esc(label)}</label>`; }
export function datasetOptions(blank=false) { return [...(blank?[['','请选择数据表']]:[]),...state.datasets.map(dataset=>[dataset.slug,`${dataset.name} · ${dataset.slug}`])]; }
export function fieldOptions(slug, blank=false) { return [...(blank?[['','请选择字段']]:[]),...(state.datasets.find(dataset=>dataset.slug===slug)?.definition.fields || []).map(item=>[item.name,`${item.name} · ${item.type}`])]; }
export function val(form,name) { return form.elements.namedItem(name)?.value?.trim() ?? ''; }
export function checked(form,name) { return !!form.elements.namedItem(name)?.checked; }
export function int(form,name) { return Number(val(form,name)); }
export function dialog(title, subtitle, body, footer='') {
  const node = $('#editor'); if(node.open) node.close();
  node.dataset.generation=String(Number(node.dataset.generation || 0)+1);delete node.dataset.busy;
  node.innerHTML = `<div class="dialog-head"><div><h2 id="dialog-title">${esc(title)}</h2><p>${esc(subtitle)}</p></div><button type="button" class="btn icon-only" data-close aria-label="关闭">${icon('close')}</button></div><div class="dialog-body">${body}</div><div class="dialog-foot">${footer || '<span></span><button type="button" class="btn" data-close>关闭</button>'}</div>`;
  $$('[data-close]',node).forEach(button=>button.addEventListener('click',()=>node.close()));node.oncancel=event=>{if(node.dataset.busy)event.preventDefault();};node.onclose=()=>{if(!node.open){node.innerHTML='';delete node.dataset.busy;}};node.showModal(); return node;
}
export function formFooter(formId,label='保存配置',hint='') { return `<span class="subtle">${esc(hint)}</span><div class="actions"><button type="button" class="btn" data-close>取消</button><button class="btn primary" type="submit" form="${esc(formId)}">${esc(label)}</button></div>`; }
export function resultView(result) {
  const columns = result.columns || Object.keys(result.rows?.[0] || {});
  return `<div class="result-meta"><span>共 ${esc(number(result.total ?? result.rows?.length))} 行</span><span>本次返回 ${esc(result.rows?.length || 0)} 行</span>${result.coverage ? badge(result.coverage.complete?'日期完整':'存在缺失日期',result.coverage.complete?'good':'warn') : ''}</div>${result.coverage?.missing_dates?.length?`<div class="notice warning">缺失日期：${esc(result.coverage.missing_dates.join('、'))}</div>`:''}${result.rows?.length ? table(columns.map(esc),result.rows.map(row=>columns.map(column=>`<span title="${esc(text(row[column]))}">${esc(text(row[column]))}</span>`))) : empty('查询结果为空','请核对日期范围、筛选条件和已发布的数据。')}`;
}
export async function copyText(value) { try { await navigator.clipboard.writeText(value); toast('已复制'); } catch { toast('浏览器未允许复制，请选中文本后手动复制。',true); } }
export function bindRows(container, addButton, makeRow) {
  addButton.addEventListener('click',()=>container.insertAdjacentHTML('beforeend',makeRow()));
  container.addEventListener('click',event=>{const remove=event.target.closest('[data-remove]');if(remove) remove.closest('[data-row]').remove();});
}
export const removeButton = () => `<button type="button" class="btn icon-only danger" data-remove aria-label="删除此行">${icon('close')}</button>`;
