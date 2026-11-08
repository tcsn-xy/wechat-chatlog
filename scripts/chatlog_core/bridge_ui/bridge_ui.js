'use strict';
const $ = id => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.has('token')) { sessionStorage.setItem('chatlog-bridge-token', fragment.get('token')); history.replaceState(null, '', location.pathname); }
let token = sessionStorage.getItem('chatlog-bridge-token') || '', config, catalog, catalogs={}, busy = false;
function el(tag, text, cls) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; }
function notice(text) { $('notice').textContent = text || ''; }
async function api(path, value) {
  const r = await fetch('/api/' + path, { method: value === undefined ? 'GET' : 'POST', headers: { Authorization: 'Bearer ' + token, ...(value === undefined ? {} : { 'Content-Type': 'application/json' }) }, body: value === undefined ? undefined : JSON.stringify(value) });
  const result = await r.json(); if (!r.ok) throw new Error(result.message || (r.status === 401 || r.status === 403 ? '需要本机授权。请运行 chatlog bridge open 重新打开管理页。' : result.error || '操作失败')); return result;
}
function label(text, control) { const l = el('label', text); l.append(control); return l; }
function check(text, value, change) { const c = document.createElement('input'); c.type = 'checkbox'; c.checked = value; c.addEventListener('change', () => change(c.checked)); const l = label(text, c); l.className = 'check'; l.prepend(c); return l; }
function option(value, text) { const e = el('option', text); e.value = value; return e; }
function render() {
  $('people').replaceChildren(); $('target').replaceChildren(option('', '新建联系人')); $('empty').hidden = config.principals.length > 0;
  for (const p of config.principals) {
    $('target').append(option(p.id, p.name));
    const card = el('article', undefined, 'card'), head = el('div', undefined, 'card-head');
    const name = document.createElement('input'); name.value = p.name; name.maxLength = 100; name.setAttribute('aria-label', '联系人显示名'); name.oninput = () => { p.name = name.value; };
    const remove = el('button', '移除联系人', 'remove'); remove.onclick = () => { config.principals = config.principals.filter(x => x !== p); render(); notice('尚未保存。点击“保存配置”应用修改。'); };
    head.append(name, remove); card.append(head);
    const engine=document.createElement('select');engine.append(option('codex','Codex'),option('dsh','DeepSeek Harness'));engine.value=p.backend||'codex';
    engine.onchange=()=>{p.targets=p.targets||{};if(p.thread_id)p.targets[p.backend||'codex']={workspace:p.workspace,session_id:p.thread_id};p.backend=engine.value;const target=p.targets[p.backend]||{};p.workspace=target.workspace||'';p.thread_id=target.session_id||'';render();};card.append(label('回答后端',engine));
    const current=catalogs[p.backend||'codex']||{workspaces:[],threads:[]};
    const grid = el('div', undefined, 'grid'), ws = document.createElement('select'), threads = document.createElement('select');
    ws.append(option('', '选择工作空间'));
    for (const path of current.workspaces) ws.append(option(path, path)); if(p.workspace&&!current.workspaces.includes(p.workspace))ws.append(option(p.workspace,p.workspace));ws.value = p.workspace || '';
    function threadOptions() {
      threads.replaceChildren(option('', '选择现有会话'));
      for (const t of current.threads.filter(t => t.workspace === ws.value)) threads.append(option(t.id, String(t.title).slice(0,80) + ' · ' + t.id));
      if(p.thread_id&&!current.threads.some(t=>t.id===p.thread_id))threads.append(option(p.thread_id,'已保存（当前不可用） · '+p.thread_id));
      threads.value = p.thread_id || '';
    }
    ws.onchange = () => { p.workspace = ws.value; p.thread_id = ''; threadOptions(); };
    threads.onchange = () => { p.thread_id = threads.value; }; threadOptions();
    grid.append(label('工作空间', ws), label('固定会话', threads)); card.append(grid);
    for (const i of p.identities) {
      const row = el('div', undefined, 'identity');
      row.append(check(i.platform === 'wechat' ? '微信' : '企业微信', i.enabled !== false, value => { i.enabled = value; p.identities_confirmed = false; render(); }));
      row.append(el('span', '联系人 ' + i.contact_id + ' / 本人账号 ' + i.account_key));
      const del = el('button', '移除此身份', 'remove'); del.onclick = () => { p.identities = p.identities.filter(x => x !== i); p.identities_confirmed = false; render(); }; row.append(del); card.append(row);
    }
    card.append(check('确认这些精确平台身份属于同一人', p.identities_confirmed, value => { p.identities_confirmed = value; }));
    card.append(check('允许此联系人调用指定会话', p.enabled, value => { p.enabled = value; }));
    $('people').append(card);
  }
}
function updateStatus(state) {
  $('connection').textContent = (state.config.paused ? '已暂停' : '正在监听') + ' · 回答后端 ' + (state.active_backends||['codex']).join(' / ');
  $('pause').textContent = state.config.paused ? '恢复全部调用' : '暂停全部调用'; $('pause').dataset.paused = String(state.config.paused);
  const names = new Map(state.config.principals.map(p => [p.id, p.name]));
  const labels = { desktop_queued: '已进入 Codex 原生队列', dsh_queued:'已进入 dsh 原生队列',unknown: '结果未知，不自动重试', desktop_not_running: '桌面 app 未运行', not_sent: '未发送', server_accepted: '回复已获服务端接受', submitted_unconfirmed: '回复已提交，未确认' };
  $('deliveries').replaceChildren();
  for (const d of state.deliveries) { const status = d.reply_status || d.status, row = el('tr'); for (const text of [new Date(d.created * 1000).toLocaleString('zh-CN'), names.get(d.principal) || d.principal, d.platform + ' · ' + d.chat_ref, labels[status] || status]) row.append(el('td', text)); $('deliveries').append(row); }
}
async function load() { const [state, c] = await Promise.all([api('state'), api('catalog')]); config = state.config; catalog = c;catalogs.codex=c;try{catalogs.dsh=await api('catalog?backend=dsh');}catch(_){catalogs.dsh={workspaces:[],threads:[]};}render(); updateStatus(state); }
$('save').onclick = async () => { if (busy) return; busy = true; $('save').disabled = true; try { config = await api('config', config); render(); updateStatus(await api('state')); notice('配置已保存。'); } catch (e) { notice(e.message); } finally { busy = false; $('save').disabled = false; } };
$('reload').onclick = () => load().catch(e => notice(e.message));
$('pause').onclick = async () => { try { config = await api('pause', { paused: $('pause').dataset.paused !== 'true' }); render(); updateStatus(await api('state')); notice('已更新总开关。'); } catch (e) { notice(e.message); } };
function addIdentity(identity, target) {
  if (config.principals.some(p => p.identities.some(i => i.platform === identity.platform && i.account_key === identity.account_key && i.contact_id === identity.contact_id))) return notice('这个精确平台身份已在列表中。');
  let p = config.principals.find(p => p.id === target);
  if (!p) { p = { id: crypto.randomUUID().replaceAll('-', ''), name: identity.name || '本人', enabled: false, identities_confirmed: false, identities: [], workspace: '', thread_id: '' }; config.principals.push(p); }
  p.identities.push({ platform: identity.platform, account_key: identity.account_key, contact_id: identity.contact_id, enabled: true }); p.identities_confirmed = false; render(); notice('身份已加入待保存配置，请核对 ID、工作空间与会话。');
}
$('search').onsubmit = async event => {
  event.preventDefault(); try { const rows = await api('contacts?q=' + encodeURIComponent($('query').value)); $('results').replaceChildren(); for (const identity of rows) { const row = el('div', undefined, 'result'), info = el('div', identity.name + ' · ' + identity.platform); info.append(el('small', identity.contact_id + ' / ' + identity.account_key)); const add = el('button', '添加此身份'); add.onclick = () => addIdentity(identity, $('target').value); row.append(info, add); $('results').append(row); } if (!rows.length) $('results').append(el('p', '当前通讯录中没有匹配结果。')); } catch (e) { notice(e.message); }
};
$('owner').onclick = () => { let target = ''; for (const a of catalog.accounts) { addIdentity({ ...a, contact_id: a.self_id, name: '本人' }, target); target = config.principals.find(p => p.identities.some(i => i.contact_id === a.self_id))?.id || ''; } };
load().catch(e => notice(e.message));
setInterval(async () => { try { updateStatus(await api('state')); } catch (_) {} }, 10000);
