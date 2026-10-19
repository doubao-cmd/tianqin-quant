// 启动入口：加载数据 → 注册渲染 → 绑定事件 → 首屏渲染
import { loadData } from './data/index.js';
import { registerRender, registerModeChange, setContext, state } from './core/state.js';
import { $ } from './core/fmt.js';
import { renderAll, applyLayout, setTab, renderMain } from './view/index.js';
import { startReplay, exitReplay, stepReplay, setPlay, jumpToNextTrade, seekTo, bindScrub } from './replay/engine.js';
import { bookAct, bookCancelPending, simSuggest } from './replay/book.js';
import { openMarkPanel, closeMarkPanel, confirmMark, delCurrentMark, exportMarksCsv, pickVerdict } from './replay/marks.js';
import { flushSession, exportSessionsCsv } from './replay/ledger.js';
import { toast, f2, num0 } from './core/fmt.js';
import { closeSymPicker, safeResize, toggleLeft, applyLeft } from './view/topbar.js';

// ★ P5-2：窗口 resize 走 safeResize（尺寸指纹比对 + rAF），并 debounce 100ms。
//   原来每次 resize 事件都无条件 resize 两张图，拖窗口时每帧都在重算。
let _rszT = null;
window.addEventListener('resize', () => {
  if (_rszT) clearTimeout(_rszT);
  _rszT = setTimeout(() => { _rszT = null; safeResize(); }, 100);
});
// ★ P2-3：关页面/刷新 = 会话结束。只落账不弹下载（浏览器会拦 unload 里的下载），
//   记录进 localStorage，下次「导出台账」一定含这一行 —— 任何路径都不丢会话。
window.addEventListener('beforeunload', () => { try { flushSession('unload'); } catch (e) {} });
$('tabT').onclick = () => setTab('trade');
$('tabS').onclick = () => setTab('stats');
// ★ T2.3：第 5 个 tab「标记」。训练模式下 syncMarkTab 会把它 display:none（不渲染内容）
{ const tm = $('tabM'); if (tm) tm.onclick = () => setTab('marks'); }
// ★ T1.3：左栏折叠按钮（Ctrl+← 亦可）
{ const lb = $('lcolBtn'); if (lb) lb.onclick = toggleLeft; }
applyLeft();
// ★ P2-5：第 4 个 tab「对照」（与实盘对照）。按钮可能不存在于旧 index.html ⇒ 防御性绑定
{ const tc = $('tabC'); if (tc) tc.onclick = () => setTab('cmp'); }
setTab('trade');
// ★ T1.1 回放条折叠区：状态记 localStorage（刷新后保持）。
//   为什么不做 CSS media query 隐藏：那会"看不见但还在"，Tab 键仍能摸到不可见的按钮。
const _RB_KEY = 'rt_rb_more';
function _setRbMore(open) {
  const box = $('rbMoreBox'), btn = $('rbMore');
  if (!box) return;
  box.style.display = open ? 'flex' : 'none';
  if (btn) {
    btn.textContent = open ? '更多 ▴' : '更多 ▾';
    btn.classList.toggle('on', !!open);
    btn.title = open ? '收起更多操作' : '更多操作：选起播日 / 速度 / 自动暂停 / 退出 / 下单 / 标记';
  }
  try { localStorage.setItem(_RB_KEY, open ? '1' : '0'); } catch (e) {}
}
{
  let open = false;
  try { open = localStorage.getItem(_RB_KEY) === '1'; } catch (e) {}
  _setRbMore(open);
  const b = $('rbMore');
  if (b) b.onclick = () => _setRbMore($('rbMoreBox').style.display === 'none');
}
$('rstart').onclick = startReplay;
$('rprev').onclick = () => stepReplay(-1);
$('rnext').onclick = () => stepReplay(1);
$('rjprev').onclick = () => jumpToNextTrade(-1);
$('rjnext').onclick = () => jumpToNextTrade(1);
$('rplay').onclick = () => setPlay(!(state.replay && state.replay.playing));
$('rexit').onclick = exitReplay;
// P1-2：速度改为"每根停留毫秒"，tick 每帧读 R.stepMs，故改速度只需更新 stepMs，无需重启播放
// ★ 2026-10-18：记住上次选的速度（回放要"思考时间"，选完 3s/5s 每次都重选很烦）。
const _SPEED_KEY = 'rt_speed_ms';
try {
  const saved = +localStorage.getItem(_SPEED_KEY);
  if (saved && $('rspeed').querySelector(`option[value="${saved}"]`)) {
    $('rspeed').value = String(saved);
  }
} catch (e) { /* 隐私模式/localStorage 不可用 ⇒ 用 HTML 里的默认值 */ }
$('rspeed').onchange = () => {
  const ms = +$('rspeed').value;
  if (state.replay) state.replay.stepMs = ms;
  try { localStorage.setItem(_SPEED_KEY, String(ms)); } catch (e) {}
};
bindScrub();   // P1-6：进度条（input 预览 / change 跳转 / 拖动即暂停）

// ══ P2-1 模拟下单 ══
let _simDir = null, _simRef = null;
function openSimPanel(dir) {
  if (!state.replay) return;               // 非回放静默忽略（快捷键 b/s 不弹提示）
  const sug = simSuggest(dir);
  if (!sug) return;
  _simDir = dir; _simRef = sug.ref;
  $('spTitle').textContent = `模拟开仓 · ${dir}`;
  $('spDir').textContent = dir;
  $('spDir').style.color = dir === '多' ? '#ff6b6b' : '#4ade80';
  $('spQty').value = sug.qty;
  $('spStop').value = sug.stopPx;
  $('spTarget').value = '';
  $('spHint').textContent = `参考价 ${sug.ref} ｜ ATR14 ${f2(sug.atr)} ｜ 权益(20万+已实现) ${num0(sug.equity)} ｜ 默认止损 = 收盘 ∓ 1.5×ATR`;
  $('simpanel').style.display = 'block';
  $('spQty').focus();
}
function closeSimPanel() { $('simpanel').style.display = 'none'; }
function confirmSimPanel() {
  if (!_simDir) { closeSimPanel(); return; }
  const qty = Math.round(+$('spQty').value);
  const stop = +$('spStop').value;
  const tgtRaw = $('spTarget').value.trim();
  const tgt = tgtRaw === '' ? null : +tgtRaw;
  if (!(qty >= 1)) { toast('手数必须 ≥ 1'); return; }
  if (!(stop > 0)) { toast('止损价无效'); return; }
  if (_simDir === '多' && !(stop < _simRef)) { toast(`多单止损须低于参考价 ${_simRef}`); return; }
  if (_simDir === '空' && !(stop > _simRef)) { toast(`空单止损须高于参考价 ${_simRef}`); return; }
  if (tgt != null) {
    if (_simDir === '多' && !(tgt > _simRef)) { toast(`多单止盈须高于参考价 ${_simRef}`); return; }
    if (_simDir === '空' && !(tgt < _simRef)) { toast(`空单止盈须低于参考价 ${_simRef}`); return; }
  }
  if (bookAct('open', { dir: _simDir, qty, stopPx: stop, targetPx: tgt })) closeSimPanel();
}
$('bBuy').onclick = () => openSimPanel('多');
$('bSell').onclick = () => openSimPanel('空');
$('bClose').onclick = () => { closeSimPanel(); bookAct('close'); };
$('spOk').onclick = confirmSimPanel;
$('spCancel').onclick = closeSimPanel;
$('spX').onclick = closeSimPanel;
$('tabB').onclick = () => setTab('book');
// 报价条「待成交」chip 的点击撤销（事件委托，qstrip 每根重绘也有效）
$('qstrip').onclick = e => {
  const el = e.target && e.target.closest && e.target.closest('[data-act="cancel"]');
  if (el) bookCancelPending();
};
// ══ P2-2 决策标记 ══
$('mMark').onclick = () => openMarkPanel();
$('mkOk').onclick = confirmMark;
$('mkCancel').onclick = closeMarkPanel;
$('mkX').onclick = closeMarkPanel;
$('mkDel').onclick = delCurrentMark;
$('mkExport').onclick = exportMarksCsv;
$('mkLedger').onclick = exportSessionsCsv;   // P2-3：导出台账（累计全部会话）
['mkBuy', 'mkSell', 'mkNo', 'mkUnsure'].forEach(id => {
  $(id).onclick = () => pickVerdict($(id).dataset.v);
});
document.addEventListener('keydown', e => {
  const tag = (document.activeElement || {}).tagName || '';
  if (['INPUT', 'SELECT', 'TEXTAREA'].includes(tag)) return;
  const k = e.key.toLowerCase();
  if (!state.replay) return;               // 模拟下单/标记快捷键仅回放中生效
  if (k === 'b') { e.preventDefault(); openSimPanel('多'); }
  else if (k === 's') { e.preventDefault(); openSimPanel('空'); }
  else if (k === 'c') { e.preventDefault(); closeSimPanel(); bookAct('close'); }
  else if (k === 'm') { e.preventDefault(); openMarkPanel(); }
  else if (k === 'escape') { closeSimPanel(); }
});
document.addEventListener('keydown', e => {
  const tag = (document.activeElement || {}).tagName || '';
  if (['INPUT','SELECT','TEXTAREA'].includes(tag)) return;
  // ★ T1.3：Ctrl+← / Cmd+← 折叠左栏。必须在这里先截住，否则会同时触发下面的"上一根"
  if ((e.ctrlKey || e.metaKey) && e.key === 'ArrowLeft') { e.preventDefault(); toggleLeft(); return; }
  if (e.ctrlKey || e.metaKey) return;
  if (e.key === ' ' && tag === 'BUTTON') return;   // 焦点在按钮上时空格不双重触发
  if (e.key === 'ArrowLeft') { e.preventDefault(); if (state.replay) stepReplay(e.shiftKey ? -10 : -1); }
  else if (e.key === 'ArrowRight') { e.preventDefault(); if (state.replay) stepReplay(e.shiftKey ? 10 : 1); }
  else if (e.key === ' ') { e.preventDefault(); if (state.replay) setPlay(!(state.replay.playing)); }
  else if (e.key === 'Home') { if (state.replay) { e.preventDefault(); seekTo(state.replay.startAbs); } }
  else if (e.key === 'End') { if (state.replay) { e.preventDefault(); seekTo(1e9); } }   // seekTo 夹紧到 N-1
  else if (e.key === 'Escape') { if (state.replay) exitReplay(); }
});
document.getElementById('ind').onchange = e => { state.ind = e.target.value; renderMain(); };
document.getElementById('tbtn').onclick = () => { state.showTrades = !state.showTrades; applyLayout(); };
// 点击 picker 外部关闭品种弹层
document.addEventListener('click', e => {
  const w = document.getElementById('watch');
  if (w && !w.contains(e.target)) closeSymPicker();
});

async function boot() {
  await loadData();
  registerRender(renderAll);
  // ★ P2-3：切训练/复看模式 = 会话结束（理由 mode）。必须在 setMode 清 replay 前能调到，
  //   故注册的是 flushSession 本体（它只读 _cur 快照，不依赖 state.replay）。
  registerModeChange(r => flushSession(r));
  renderAll();
  applyLayout();
}
boot();
