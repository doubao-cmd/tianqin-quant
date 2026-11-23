// 启动入口：加载数据 → 注册渲染 → 绑定事件 → 首屏渲染
import { loadData } from './data/index.js';
import { registerRender, registerModeChange, setContext, state } from './core/state.js';
import { $ } from './core/fmt.js';
import { pd } from './core/time.js';
import { renderAll, applyLayout, setTab, renderMain, pickPrice, pickCancel, renderAcct } from './view/index.js';
import { startReplay, startRandomReplay, exitReplay, submitSession, stepReplay, setPlay, jumpToNextTrade, seekTo, bindScrub } from './replay/engine.js';
import { bookAct, bookCancelPending, simSuggest, simCfg, bookState, stopByAtr, targetByR, roundToTick } from './replay/book.js';
import { openMarkPanel, closeMarkPanel, confirmMark, delCurrentMark, exportMarksCsv, pickVerdict } from './replay/marks.js';
import { flushSession, exportSessionsCsv } from './replay/ledger.js';
import { toast, f2, num0 } from './core/fmt.js';
import { closeSymPicker, safeResize } from './view/topbar.js';

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
// ★ 2026-11-07：左栏（分时）已恢复为常驻栏，不再有 lcolBtn / applyLeft
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
$('rrand').onclick = startRandomReplay;   // ★ 随机一局（R4）
$('rsubmit').onclick = submitSession;     // ★ 交卷：结算 + 切复看
// ★ 「再来一局」钩子：评分卡（view/report.js）不 import engine（会成环），改由 boot 注入。
window.__rtNextRound = () => startRandomReplay();
$('rprev').onclick = () => stepReplay(-1);
$('rnext').onclick = () => stepReplay(1);
$('rjprev').onclick = () => jumpToNextTrade(-1);
$('rjnext').onclick = () => jumpToNextTrade(1);
$('rplay').onclick = () => {
  // ★ 下单不匆忙：面板开着时不放行播放，逼用户先处理完这一单
  if ($('simpanel').style.display === 'block') { toast('下单面板开着 —— 先确认或取消，K 线不会动'); return; }
  setPlay(!(state.replay && state.replay.playing));
};
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

// ══ P2-1 模拟下单（★ 2026-11-07 重做）══
//   用户要求：去掉策略写死的 ATR 止损口径，改成自己设 —— ①ATR 快捷（0.5/1/1.5/2）
//   ②R 倍快捷止盈 ③图上点一下取价（画线止损/止盈/挂单价）④时间止损可设
//   ⑤下单面板一开就暂停 K 线（不匆忙）。
let _simDir = null, _simRef = null, _simMode = 'market', _simAtrMult = 1;
let _simLineT = null;

function _num(id) { const v = ($(id) && $(id).value || '').trim(); return v === '' ? null : +v; }

// ★ 基准价：限价挂单用挂单价，市价单用当前收盘 —— 止损/止盈/手数风险都按这个基准算，
//   否则"挂单在 20030、止损却在 20270"这种方向反了的值会莫名其妙地报错。
function _basePx() {
  const lim = _num('spLimit');
  return (_simMode === 'limit' && lim > 0) ? lim : _simRef;
}

function _redrawSimLines() {
  if (_simLineT) clearTimeout(_simLineT);
  _simLineT = setTimeout(() => { _simLineT = null; try { renderMain(); } catch (e) {} }, 160);
}

// 面板里所有派生提示 + 图上预览线（止损红点线 / 止盈绿点线 / 挂单蓝虚线）
function refreshSimNotes(redraw) {
  if (!state.replay || !_simDir) return;
  const d = pd();
  const ref0 = d.c[state.replay.abs];        // 当前 bar 收盘（"参考价"显示用）
  const base = _basePx();                    // 方向/距离/R 的计算基准（限价 = 挂单价）
  const atr = d.atr[state.replay.abs] || 0;
  const dir = _simDir;
  const cfg = simCfg(state.sym);
  const stop = _num('spStop'), tgt = _num('spTarget'), lim = _num('spLimit'), tb = _num('spTime');
  const qty = Math.max(1, Math.round(+$('spQty').value) || 1);

  // 手数 × 每手风险
  const perHand = (stop > 0) ? Math.abs(base - stop) * cfg.mult : 0;
  const equity = cfg.principal + bookState().realized;
  $('spQtyHint').textContent = perHand > 0
    ? `${qty} 手共 ¥${(perHand * qty).toFixed(0)}｜${(perHand * qty / equity * 100).toFixed(2)}% 权益`
    : '';
  $('spStopNote').textContent = stop > 0
    ? `距 ${Math.abs(base - stop).toFixed(1)} 点｜${atr > 0 ? (Math.abs(base - stop) / atr).toFixed(2) : '—'}×ATR｜每手 ¥${perHand.toFixed(0)}`
    : '未设（必须设）';
  // 方向反了（多单止损在上/空单止损在下）也要标黄 —— 否则取价点错方向看不出来
  const stopBad = !(stop > 0) || (dir === '多' ? stop >= base : stop <= base);
  $('spStopNote').classList.toggle('warn', stopBad);

  // 止盈 R 倍数
  if (tgt > 0 && stop > 0) {
    const risk = Math.abs(base - stop), rew = Math.abs(tgt - base);
    $('spTargetNote').textContent = risk > 0
      ? `${(rew / risk).toFixed(2)}R｜盈亏比 ${(rew / risk).toFixed(1)}:1` : '';
  } else {
    $('spTargetNote').textContent = tgt > 0 ? '已设（无止损基准）' : '不设（一直拿到平仓）';
  }
  const tgtBad = tgt > 0 && (dir === '多' ? tgt <= base : tgt >= base);
  $('spTargetNote').classList.toggle('warn', tgtBad);

  // 限价提示
  if (_simMode === 'limit' && lim > 0) {
    const diff = lim - ref0;
    $('spLimitHint').textContent = `${diff >= 0 ? '高' : '低'}于现价 ${Math.abs(diff).toFixed(1)} 点`;
  } else if (_simMode === 'limit') {
    $('spLimitHint').textContent = '未设价';
  }

  // 时间止损
  $('spTimeHint').textContent = tb > 0
    ? `= ${tb * (+state.period || 5)} 分钟` : '';

  // 快捷按钮高亮（值相等就点亮，手输成 1×ATR 也会亮）
  document.querySelectorAll('.sp-q[data-stop]').forEach(b => {
    const v = stopByAtr(dir, +b.dataset.stop, base);
    b.classList.toggle('on', stop > 0 && v != null && Math.abs(v - stop) < 1e-6);
  });
  document.querySelectorAll('.sp-q[data-tgt]').forEach(b => {
    const r = +b.dataset.tgt;
    if (r === 0) { b.classList.toggle('on', !(tgt > 0)); return; }
    const v = (stop > 0) ? targetByR(dir, r, stop, base) : null;
    b.classList.toggle('on', tgt > 0 && v != null && Math.abs(v - tgt) < 1e-6);
  });

  // 图上预览线
  state._simLines = {
    stop: stop > 0 ? stop : null,
    target: tgt > 0 ? tgt : null,
    limit: (_simMode === 'limit' && lim > 0) ? lim : null,
  };
  if (redraw !== false) _redrawSimLines();
}

function setSimMode(m) {
  _simMode = (m === 'limit') ? 'limit' : 'market';
  document.querySelectorAll('#spMode button').forEach(b =>
    b.classList.toggle('on', b.dataset.m === _simMode));
  $('spLimitRow').style.display = (_simMode === 'limit') ? 'flex' : 'none';
  $('spOk').textContent = (_simMode === 'limit') ? '确认挂单（到价成交）' : '确认下单（下一根开盘）';
  if (_simMode === 'limit' && !$('spLimit').value) {
    $('spLimit').value = roundToTick(_simRef);      // 默认 = 现价，用户自己往有利方向改
  }
  refreshSimNotes();
}

function openSimPanel(dir) {
  if (!state.replay) return;               // 非回放静默忽略（快捷键 b/s 不弹提示）
  // ★ 下单不匆忙：面板一开就停住 K 线，想继续按「播放」会先提示你处理完面板
  if (state.replay.playing) setPlay(false);
  const sug = simSuggest(dir, 1);
  if (!sug) return;
  _simDir = dir; _simRef = sug.ref; _simAtrMult = 1;
  $('spTitle').textContent = `模拟开仓 · ${dir}`;
  $('spLive').textContent = '⏸ K 线已暂停';
  $('spDir').textContent = dir;
  $('spDir').style.color = dir === '多' ? '#ff6b6b' : '#4ade80';
  $('spQty').value = sug.qty;
  $('spStop').value = sug.stopPx;
  $('spTarget').value = '';
  $('spLimit').value = '';
  $('spTime').value = '';
  $('spHint').textContent = `参考价 ${sug.ref}｜ATR14 ${f2(sug.atr)}｜权益 ${num0(sug.equity)}｜默认止损 1×ATR`;
  setSimMode('market');
  $('simpanel').style.display = 'block';
  refreshSimNotes();
  $('spQty').focus();
}
function closeSimPanel() {
  $('simpanel').style.display = 'none';
  _simDir = null;
  state._simLines = null;
  try { renderMain(); } catch (e) {}
}
function confirmSimPanel() {
  if (!_simDir) { closeSimPanel(); return; }
  const qty = Math.round(+$('spQty').value);
  const stop = +$('spStop').value;
  const tgtRaw = $('spTarget').value.trim();
  const tgt = tgtRaw === '' ? null : +tgtRaw;
  const lim = _simMode === 'limit' ? _num('spLimit') : null;
  const tb = _num('spTime');
  const base = (lim != null && lim > 0) ? lim : _simRef;     // 限价单按挂单价校验方向
  if (!(qty >= 1)) { toast('手数必须 ≥ 1'); return; }
  if (!(stop > 0)) { toast('止损价必填（保护自己，不允许裸单）'); return; }
  if (_simMode === 'limit' && !(lim > 0)) { toast('限价挂单必须填挂单价'); return; }
  if (_simDir === '多' && !(stop < base)) { toast(`多单止损须低于${lim ? '挂单价' : '参考价'} ${base}`); return; }
  if (_simDir === '空' && !(stop > base)) { toast(`空单止损须高于${lim ? '挂单价' : '参考价'} ${base}`); return; }
  if (tgt != null) {
    if (_simDir === '多' && !(tgt > base)) { toast(`多单止盈须高于${lim ? '挂单价' : '参考价'} ${base}`); return; }
    if (_simDir === '空' && !(tgt < base)) { toast(`空单止盈须低于${lim ? '挂单价' : '参考价'} ${base}`); return; }
  }
  if (bookAct('open', { dir: _simDir, qty, stopPx: stop, targetPx: tgt, limitPx: lim, timeStopBars: tb })) closeSimPanel();
}
$('bBuy').onclick = () => openSimPanel('多');
$('bSell').onclick = () => openSimPanel('空');
$('bClose').onclick = () => { closeSimPanel(); bookAct('close'); };
$('spOk').onclick = confirmSimPanel;
$('spCancel').onclick = closeSimPanel;
$('spX').onclick = closeSimPanel;
$('tabB').onclick = () => setTab('book');
// ★ R5：账户页（跨会话累计）—— 切过去时强制重渲染（renderAll 里只在激活时渲染，避免每根都画 canvas）
{ const ta = $('tabA'); if (ta) ta.onclick = () => { setTab('acct'); renderAcct(true); }; }
// 市价 / 限价 切换
document.querySelectorAll('#spMode button').forEach(b => { b.onclick = () => setSimMode(b.dataset.m); });
// 止损快捷（ATR 倍数）—— 基准价：限价单取挂单价，市价单取收盘
document.querySelectorAll('.sp-q[data-stop]').forEach(b => {
  b.onclick = () => {
    _simAtrMult = +b.dataset.stop;
    const v = stopByAtr(_simDir, _simAtrMult, _basePx());
    if (v == null) { toast('该根 ATR 不可用（数据不足）'); return; }
    if (!(v > 0)) { toast('按该 ATR 倍数算出的止损价 ≤ 0，换小一点的倍数'); return; }
    $('spStop').value = v;
    refreshSimNotes();
  };
});
// 止盈快捷（R 倍数；0 = 不设）
document.querySelectorAll('.sp-q[data-tgt]').forEach(b => {
  b.onclick = () => {
    const r = +b.dataset.tgt;
    if (r === 0) { $('spTarget').value = ''; refreshSimNotes(); return; }
    const stop = _num('spStop');
    if (!(stop > 0)) { toast('先设止损，再按 R 倍推止盈'); return; }
    const v = targetByR(_simDir, r, stop, _basePx());
    if (v == null) { toast('算不出止盈价（止损价离基准价太近）'); return; }
    $('spTarget').value = v;
    refreshSimNotes();
  };
});
// ★ 改挂单价后，若止损/止盈方向反了就按原 ATR 倍数从新挂单价重算 ——
//   否则「挂 20030、止损 20270」这种组合会一直卡在校验里，用户还得自己反推。
// ★ 2026-11-07 修：抽成函数是因为**图上取价**也要走这条路。
//   给 input.value 赋值不会触发 change，只有人手敲才会 ⇒ 「图上取」取的挂单价
//   原来不会带动止损重算：挂 3390、空单止损还留在 3387（在下方的错方向），
//   点确认只弹一句"空单止损须高于挂单价"，用户得自己再点一次快捷档。
function afterLimitChange() {
  if (_simMode !== 'limit' || !_simDir) return;
  const lim = _num('spLimit');
  if (!(lim > 0)) { refreshSimNotes(); return; }
  const stop = _num('spStop');
  const badStop = _simDir === '多' ? !(stop < lim) : !(stop > lim);
  if (badStop) {
    const v = stopByAtr(_simDir, _simAtrMult, lim);
    if (v != null && v > 0) {
      $('spStop').value = v;
      toast(`止损已按 ${_simAtrMult}×ATR 从挂单价重算 → ${v}`);
    }
  }
  const tgt = _num('spTarget');
  if (tgt > 0 && (_simDir === '多' ? !(tgt > lim) : !(tgt < lim))) {
    const v2 = targetByR(_simDir, 2, _num('spStop'), lim);
    $('spTarget').value = (v2 != null && v2 > 0) ? v2 : '';
  }
  refreshSimNotes();
}
$('spLimit').addEventListener('change', afterLimitChange);
// 图上取价：点按钮 → 主图十字光标 → 点一下 → 吸附 tick 填入
document.querySelectorAll('.sp-pick').forEach(b => {
  b.onclick = async () => {
    const field = b.dataset.pick;
    b.classList.add('on');
    const v = await pickPrice();
    b.classList.remove('on');
    if (v == null) return;
    const px = roundToTick(v);
    if (field === 'stop') { $('spStop').value = px; refreshSimNotes(); }
    else if (field === 'target') { $('spTarget').value = px; refreshSimNotes(); }
    else { $('spLimit').value = px; afterLimitChange(); }   // ★ 取挂单价要连带重算止损/止盈
  };
});
['spStop', 'spTarget', 'spLimit', 'spTime', 'spQty'].forEach(id => {
  if ($(id)) $(id).addEventListener('input', () => refreshSimNotes());
});
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
  else if (k === 'escape') {
    // ★ 2026-11-07：Esc 三级回退 —— 拾取态 → 关下单面板 → 退出回放（原来两个 keydown 都处理 Esc，
    //   按一次会既关面板又退回放）。这里统一收口，下面那个 handler 不再处理 Esc。
    e.preventDefault();
    if (pickCancel()) return;
    if ($('simpanel').style.display === 'block') { closeSimPanel(); return; }
    exitReplay();
  }
});
document.addEventListener('keydown', e => {
  const tag = (document.activeElement || {}).tagName || '';
  if (['INPUT','SELECT','TEXTAREA'].includes(tag)) return;
  // ★ 2026-11-07 盘感练习：分时图已删，Ctrl+← 折叠快捷键一并移除（否则能把左栏调回来）
  if (e.ctrlKey || e.metaKey) return;
  if (e.key === ' ' && tag === 'BUTTON') return;   // 焦点在按钮上时空格不双重触发
  if (e.key === 'ArrowLeft') { e.preventDefault(); if (state.replay) stepReplay(e.shiftKey ? -10 : -1); }
  else if (e.key === 'ArrowRight') { e.preventDefault(); if (state.replay) stepReplay(e.shiftKey ? 10 : 1); }
  else if (e.key === ' ') {
    e.preventDefault();
    if (!state.replay) return;
    if ($('simpanel').style.display === 'block') { toast('下单面板开着 —— 先确认或取消'); return; }
    setPlay(!state.replay.playing);
  }
  else if (e.key === 'Home') { if (state.replay) { e.preventDefault(); seekTo(state.replay.startAbs); } }
  else if (e.key === 'End') { if (state.replay) { e.preventDefault(); seekTo(1e9); } }   // seekTo 夹紧到 N-1
  // ★ Escape 统一在上面的 handler 处理（拾取 → 关面板 → 退出），这里不再重复绑定
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
  // ★ 2026-11-07 盘感练习：打开即随机开一局（训练模式）。
  //   以前打开看到的是"最新行情"= 先把答案看完了再练。
  //   想先看数据：点「退出回放」；想换一局：点「随机」。
  setTimeout(() => { try { if (!state.replay) startRandomReplay(); } catch (e) {} }, 80);
}
boot();
