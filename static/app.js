const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="sync-token"]').content;
const fields = ['host', 'port', 'username', 'local', 'remote'];
let trusted = '', busy = false, running = false, lastLogs = '', lastConflicts = '';
try { const saved = JSON.parse(localStorage.getItem('folder-link-config') || '{}'); fields.forEach(k => { if (saved[k]) $(k).value = saved[k]; }); } catch {}
function config() { return Object.fromEntries([...fields, 'password'].map(k => [k, $(k).value]).concat([['trusted', trusted]])); }
function save() { try { const data = config(); delete data.password; delete data.trusted; localStorage.setItem('folder-link-config', JSON.stringify(data)); } catch {} }
async function api(path, data) {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Sync-Token': token}, body: JSON.stringify(data || {})});
  const result = await response.json();
  if (!response.ok) throw result;
  return result;
}
function notice(text, error = false) { $('connection-result').hidden = false; $('connection-result').textContent = text; $('connection-result').classList.toggle('error', error); }
function controls() {
  [...fields, 'password', 'choose-folder', 'show-password'].forEach(k => $(k).disabled = running || busy);
  ['test', 'once', 'start'].forEach(k => $(k).disabled = running || busy);
  $('stop').hidden = !running; $('start').hidden = running; $('once').hidden = running;
}
async function trustHost(error) {
  $('trust-host').textContent = error.host;
  $('trust-fingerprint').textContent = error.fingerprint;
  const dialog = $('trust-dialog');
  return new Promise(resolve => {
    dialog.onclose = () => resolve(dialog.returnValue === 'trust');
    $('trust-confirm').onclick = () => dialog.close('trust');
    $('trust-cancel').onclick = () => dialog.close('cancel');
    dialog.returnValue = ''; dialog.showModal();
  });
}
async function testConnection() {
  try { return await api('/api/test', config()); }
  catch (e) {
    if (!e.fingerprint || !(await trustHost(e))) throw e;
    trusted = e.fingerprint;
    return await api('/api/test', config());
  }
}
async function action(kind) {
  if (busy || running) return;
  if (kind !== 'test' && !$('sync-form').reportValidity()) return;
  busy = true; controls(); notice('正在连接服务器…');
  try {
    const result = await testConnection();
    notice(`连接成功 · 远程主目录 ${result.home}`); save();
    if (kind !== 'test') { await api('/api/start', {...config(), automatic: kind === 'auto'}); await refresh(); }
  } catch (e) { notice(e.error || e.message || '连接失败，请重试。', true); }
  finally { busy = false; controls(); }
}
$('test').onclick = () => action('test');
$('once').onclick = () => action('once');
$('sync-form').onsubmit = e => { e.preventDefault(); action('auto'); };
$('stop').onclick = async () => { try { await api('/api/stop'); notice('正在停止同步…'); } catch (e) { notice(e.error || '停止失败', true); } };
$('show-password').onclick = () => { const shown = $('password').type === 'password'; $('password').type = shown ? 'text' : 'password'; $('show-password').textContent = shown ? '隐藏' : '显示'; $('show-password').setAttribute('aria-label', shown ? '隐藏密码' : '显示密码'); };
['host', 'port'].forEach(k => $(k).addEventListener('input', () => { trusted = ''; }));
$('choose-folder').onclick = async () => {
  $('choose-folder').disabled = true;
  try { const result = await api('/api/folder'); if (result.path) $('local').value = result.path; }
  catch (e) { notice(e.error || '请选择或填写本地路径', true); }
  finally { controls(); }
};
async function refresh() {
  try {
    const response = await fetch('/api/status'); if (!response.ok) throw new Error();
    const state = await response.json(); running = state.running; controls();
    $('uploaded').textContent = state.uploaded; $('downloaded').textContent = state.downloaded; $('skipped').textContent = state.skipped;
    $('last-sync').textContent = state.last_sync ? `上次完成 ${state.last_sync}` : '等待同步';
    const badge = $('status-badge');
    badge.textContent = state.error ? '同步失败' : state.conflicts.length ? '需要处理冲突' : running ? (state.mode === 'auto' ? '自动同步中' : '同步中') : state.last_sync ? '已完成 / 已停止' : '未开始';
    badge.className = 'status-badge' + (state.error ? ' error' : running ? ' active' : '');
    const conflictData = JSON.stringify(state.conflicts);
    if (conflictData !== lastConflicts) {
      lastConflicts = conflictData; $('conflicts').replaceChildren(); $('conflicts').hidden = !state.conflicts.length;
      state.conflicts.forEach(conflict => {
        const row = document.createElement('div'); row.className = 'conflict-row';
        const label = document.createElement('span'); label.textContent = '两端版本不同 · ' + conflict.path; row.append(label);
        ['local', 'remote'].forEach(choice => {
          const button = document.createElement('button'); button.type = 'button'; button.className = 'button secondary';
          button.textContent = choice === 'local' ? '保留本地' : '保留远程';
          button.onclick = async () => {
            button.disabled = true;
            try { await api('/api/resolve', {path: conflict.path, choice}); notice('已选择保留版本，等待下一轮同步…'); if (!running) await action('once'); }
            catch(e) { notice(e.error || '处理失败', true); }
            finally { button.disabled = false; }
          }; row.append(button);
        }); $('conflicts').append(row);
      });
    }
    const serialized = JSON.stringify(state.logs);
    if (state.logs.length && serialized !== lastLogs) {
      lastLogs = serialized; $('logs').replaceChildren();
      state.logs.forEach(log => { const row = document.createElement('div'); row.className = `log-row ${log.level}`; const time = document.createElement('time'); time.textContent = log.time; const text = document.createElement('span'); text.textContent = log.message; row.append(time, text); $('logs').append(row); });
      $('logs').scrollTop = $('logs').scrollHeight;
    }
  } catch { $('status-badge').textContent = '本地服务未连接'; $('status-badge').className = 'status-badge error'; }
}
async function poll() { await refresh(); setTimeout(poll, 1200); }
poll();
