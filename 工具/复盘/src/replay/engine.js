// 回放控制引擎（P0-B/P0-C）
// 所有回放私有状态在 state.replay：abs(绝对索引) / win(固定窗口) / playing / stepMs。
// 本模块不持有散落的 i / playing 字段（验收：全文件 grep = 0）。
import { state, makeReplay, canSeeAnswers } from '../core/state.js';
import { pd, rangeStart, tmap } from '../core/time.js';
import { $, toast, esc } from '../core/fmt.js';
import { DB } from '../data/index.js';
import { renderAll, renderMain, renderIntraday, renderTable, renderStats, renderCompare } from '../view/index.js';
import { showReport } from '../view/report.js';
import { beginSession, endSession, flushSession, sessionActive, noteStep, lastScore } from './ledger.js';

let replayRAF = null;   // P1-2：requestAnimationFrame 句柄（替代旧 setInterval，避免回调堆积跳根）
let _scrubDrag = false; // ★ P1-6：进度条拖动中 → updateReplayUI 不反向写 value/文字，避免与用户打架

// 进度条旁文字：第 N/M 根 ｜ 2026-10-08 22:55（P1-6）
function _scrubText(abs) {
  const [d, , N] = replayNI0();
  const a = Math.max(0, Math.min(N - 1, abs));
  return `第 ${a + 1}/${N} 根 ｜ ${String(d.x[a]).slice(0, 16)}`;
}

// ═══════════════════════════════════════════════════════════════════════
// ★ T1.2 回放状态显著化：顶部 22px 横幅 + #app 左侧色条
//   为什么必须有：原来只有按钮 disabled，切出去看别的东西再回来，根本分不清
//   "这是回放的第 37 根"还是"这是最新行情"——而这两者的决策后果完全不同。
// ═══════════════════════════════════════════════════════════════════════
function _rbBanner() {
  const b = $('rbBanner'), app = $('app');
  const R = state.replay;
  if (!b) return;
  if (!R) {
    b.style.display = 'none';
    if (app) { app.classList.remove('replay-on'); app.classList.remove('train'); }
    const rs = $('rbResume'); if (rs) rs.style.display = 'none';
    return;
  }
  const [d, , N] = replayNI0();
  const a = Math.max(0, Math.min(N - 1, R.abs));
  const train = !canSeeAnswers();
  const sec = (R.stepMs || 600) / 1000;
  b.className = 'rb-banner' + (train ? ' train' : '');
  b.innerHTML = `回放中 · ${train ? '训练模式（隐藏成交/统计）' : '复看模式'}`
    + ` · <b>${esc(String(d.x[a]).slice(0, 16))}</b>`
    + ` · 第 <b>${a + 1}/${N}</b> 根`
    + ` · ${sec >= 1 ? sec.toFixed(1) : sec.toFixed(2)}s/根`
    + (R.playing ? ' · ▶ 播放中' : '');
  b.style.display = 'flex';
  if (app) { app.classList.add('replay-on'); app.classList.toggle('train', train); }
}

// ═══════════════════════════════════════════════════════════════════════
// ★ T2.4 复盘进度持久化
//   节流：每推进 10 根才写一次（每根都写 = 每秒最多 5 次 localStorage 同步写，卡主线程）。
//   只存 4 个标量（sym/period/abs/ts），不存任何行情或判断。
// ═══════════════════════════════════════════════════════════════════════
const _PROG_KEY = 'rt_progress';
function _saveProg() {
  const R = state.replay;
  if (!R) return;
  if (Math.abs(R.abs - (R._progAt == null ? -99 : R._progAt)) < 10) return;
  R._progAt = R.abs;
  try {
    localStorage.setItem(_PROG_KEY, JSON.stringify({
      sym: state.sym, period: state.period, abs: R.abs, ts: Date.now(),
    }));
  } catch (e) { /* 隐私模式：跳过，不影响回放 */ }
}

function _readProg() {
  try {
    const p = JSON.parse(localStorage.getItem(_PROG_KEY) || 'null');
    if (!p || !p.sym || p.abs == null || !p.ts) return null;
    if (Date.now() - p.ts > 7 * 864e5) return null;      // 超过 7 天不问（早忘了）
    return p;
  } catch (e) { return null; }
}

function _showResume() {
  const el = $('rbResume');
  if (!el) return;
  el.style.display = 'none';
  const p = _readProg();
  if (!p || !state.replay) return;
  if (p.sym !== state.sym || p.period !== state.period) return;
  const d = pd();
  if (p.abs >= d.x.length || Math.abs(p.abs - state.replay.abs) < 5) return;
  const days = Math.max(1, Math.round((Date.now() - p.ts) / 864e5));
  el.innerHTML = `上次看到 <b>${esc(String(d.x[p.abs]).slice(0, 16))}</b>`
    + `<span class="muted">（${days} 天前 · 第 ${p.abs + 1} 根）</span>`
    + `<span class="spacer"></span>`
    + `<button id="rbResumeGo">继续</button><button id="rbResumeNo">不用</button>`;
  el.style.display = 'flex';
  $('rbResumeGo').onclick = () => { el.style.display = 'none'; seekTo(p.abs); };
  $('rbResumeNo').onclick = () => { el.style.display = 'none'; };
}


// ★ P1-6 进度条：input 只预览文字（不重绘、不跳转）；change 才真正跳转；拖动即暂停播放
export function bindScrub() {
  const sc = $('rscrub');
  if (!sc) return;
  sc.addEventListener('input', () => {
    if (!state.replay) return;
    _scrubDrag = true;                                   // ★ 先置位：setPlay→updateReplayUI 见标即跳过
    if (state.replay.playing) setPlay(false);            // 拖动即暂停（防边拖边跳）
    $('rpos').textContent = _scrubText(+sc.value || 0);  // 只更新文字，不重绘
  });
  sc.addEventListener('change', () => {
    if (!state.replay) { _scrubDrag = false; return; }
    _scrubDrag = false;
    seekTo(+sc.value || 0);                              // 松手才跳转 + 整段重绘
  });
}

// 当前可见 bar 数 / 起播绝对索引 / 区间内 bar 数
export function replayNI0() {
  const d = pd();
  const i0 = rangeStart(d.x);
  return [d, i0, d.x.length - i0];
}

// ★ P2-3：全文件唯一的 R.abs 写入口（原来散在 5 处，改起来容易漏）。
//   夹紧语义与原各处逐字一致；额外多做一件事：向前推进时记一笔"推进根数"给会话台账。
function _setAbs(R, v) {
  const N = replayNI0()[2];
  const nv = Math.max(0, Math.min(N - 1, v));
  if (nv > R.abs) noteStep(nv - R.abs);
  R.abs = nv;
}

export function updateReplayUI() {
  // ★ P2-3 轮询保险：state.replay 被外部清掉（切品种/切周期/切区间/切模式）而会话还没收尾
  //   → 在这里补一刀落账（不弹下载；弹下载只发生在用户显式点"退出回放"）。
  if (!state.replay && sessionActive()) flushSession('switch');
  const [d, i0, N] = replayNI0();
  const R = state.replay;
  const on = !!R;
  $('rplay').textContent = (R && R.playing) ? '暂停 ⏸' : '播放 ▶';
  $('rprev').disabled = !on || R.abs <= 0;
  $('rnext').disabled = !on || R.abs >= N - 1;
  $('rplay').disabled = !on;
  $('rexit').disabled = !on;
  $('rspeed').disabled = !on;
  // ★ P1-5：逐笔跳转 / 自动暂停 仅在复看模式可用（训练模式禁用，防泄露答案）
  const canJump = on && canSeeAnswers();
  $('rjprev').disabled = !canJump;
  $('rjnext').disabled = !canJump;
  $('rauto').disabled = !canJump;
  // ★ P2-1：模拟下单按钮仅回放中可用；退出回放顺带收起下单面板
  ['bBuy', 'bSell', 'bClose'].forEach(id => { const el = $(id); if (el) el.disabled = !on; });
  if (!on) { const sp = $('simpanel'); if (sp) sp.style.display = 'none'; }
  // ★ P2-2：标记按钮同样仅回放中可用（标记要落到具体 bar）；退出回放收起标记面板
  { const el = $('mMark'); if (el) el.disabled = !on; }
  if (!on) { const mp = $('mkPanel'); if (mp) mp.style.display = 'none'; }
  // ★ P1-6：进度条（未回放 = 禁用；拖动中不反向写）
  const sc = $('rscrub');
  if (sc) {
    sc.disabled = !on;
    sc.max = String(Math.max(0, N - 1));
    if (!_scrubDrag) sc.value = String(R ? R.abs : 0);
  }
  if (!on) $('rpos').textContent = '';
  else if (!_scrubDrag) $('rpos').textContent = _scrubText(R.abs);
  _rbBanner();                       // ★ T1.2：横幅/色条与按钮状态同源更新，不会一个跳一个不跳
}

// 开仓 bar 落点列表（落点 = 开仓 bar 索引 - 1，即"决策点"；P1-5）
// ★ 2026-10-18 修正：索引改为「**包含该成交时刻的那根 bar**」（最后一个 x <= t）。
//   原写法「第一个 x >= t」在 15min / 日线上会把成交归到后一根（台账时间吸附在 5min 网格，
//   而 15min 网格是 :00/:15/:30/:45 ⇒ 成交 21:35 被推到 21:45 那根）。
//   5min 上两规则同解（吸附后正好是网格点），所以只在换周期时暴露。
//   ⇒ 与 replay/compare.js 的 _cmpAbsOf 同一口径，否则「下一笔」和「对照」会指向两根不同 bar。
function _openAbsList() {
  const d = pd();
  const dd = DB.data[state.sym];
  const list = [];
  (dd.trades || []).forEach(t => {
    if (!t.t) return;
    const key = tmap(t.t);
    let idx = -1;
    for (let k = 0; k < d.x.length; k++) {
      if (tmap(d.x[k]) <= key) idx = k; else break;   // d.x 升序，遇到更大即可停
    }
    if (idx > 0) list.push({ abs: idx - 1, i: t.i });
  });
  return list.sort((a, b) => a.abs - b.abs);
}

// ★ P1-5 逐笔跳转（复看模式专用；落点 = 开仓 bar - 1）
export function jumpToNextTrade(dir) {
  if (!canSeeAnswers()) { toast('训练模式禁用（会泄露答案）'); return; }
  if (!state.replay) { toast('请先进入回放'); return; }
  const list = _openAbsList();
  if (!list.length) { toast('无成交可跳转'); return; }
  const cur = state.replay.abs;
  let target = null;
  if (dir > 0) {
    for (const it of list) { if (it.abs > cur) { target = it; break; } }
    if (!target) target = list[0];
  } else {
    for (let k = list.length - 1; k >= 0; k--) { if (list[k].abs < cur) { target = list[k]; break; } }
    if (!target) target = list[list.length - 1];
  }
  _setAbs(state.replay, target.abs);
  state.sel = target.i;
  state._lastViewKey = null;
  setPlay(false);
  renderAll();
}

// 初始窗口宽度：按画布像素算，每根约 7px（P0-C 自适应）
export function autoWin() {
  const w = ($('main') || {}).clientWidth || 1200;
  return Math.max(60, Math.min(400, Math.round(w / 7)));
}

export function startReplay() {
  // 回放 = 整段：先锁 ALL，否则"近N日"会把前面的 K 整段丢掉
  state.range = 'ALL';
  state._lastViewKey = null;
  const [d, i0, N] = replayNI0();
  if (!N) return;
  const ds = $('rdate').value || String(d.x[i0]).slice(0,10);
  // 起播语义（P1-1）：正序找"该日第一根" = 第一个 日期 >= ds 的 bar。
  // 旧写法"从末尾倒找 ≤ ds"会落到当天 22:55，等于把选中的一整天整段跳过（早盘一根都看不到）。
  let j = d.x.findIndex(s => String(s).slice(0,10) >= ds);
  if (j < 0) j = d.x.length - 1;   // 选中日晚于全部数据（未来）→ 落最后一根，而非回首根
  state.replay = makeReplay(j, {win: autoWin()});
  // ★ 2026-10-18：沿用下拉框里选定的速度（boot.js 已从 localStorage 恢复上次选择）。
  //   makeReplay 的默认值 600 是"中速"，但用户如果想慢慢看，不该每次重进回放又被拉回 600。
  const sp = $('rspeed') && +$('rspeed').value;
  if (sp > 0) state.replay.stepMs = sp;
  state.sel = null;
  beginSession();                     // ★ P2-3：开一次会话台账（内部会先收尾上一次未收尾的）
  setPlay(false);
  renderAll();
  _showResume();                      // ★ T2.4：有 7 天内的旧进度就提示「继续？」（非阻塞）
}

export function exitReplay() {
  setPlay(false);
  state.replay = null;
  state.sel = null;
  const rec = endSession('exit');     // ★ P2-3：显式退出 = 会话结束 → 落账 + 有实质内容才弹下载
  renderAll();
  // ★ P2-4：有实质内容才弹评分报告（空会话不打扰）。放在 renderAll 之后，保证图表先复位
  const sc = lastScore();
  if (sc && (sc.nSim > 0 || sc.nMark > 0)) showReport(sc);
  return rec;
}

export function stepReplay(n) {
  if (!state.replay) return;
  setPlay(false);
  _setAbs(state.replay, state.replay.abs + n);   // 夹紧在 _setAbs 内做
  renderAll();
  _saveProg();
}

// 前进一根后的刷新面（P1-3/P1-4/P1-6）
function renderStep() {
  renderMain();                    // 主图 + 报价条 + 持仓状态(P1-4)
  renderIntraday();                // 分时
  renderTable({appendOnly: true}); // ★ P1-3 增量刷新成交列表（"已发生 N 笔"逐笔递增，不整表闪）
  renderStats();                   // ★ P1-6 回放内统计随进度滚动（只渲染回放块，廉价）
  renderCompare();                 // ★ P2-5 对照面板逐根刷新（台账部分有签名缓存，不重拼）
  updateReplayUI();
  _saveProg();                     // ★ T2.4：每 10 根记一次进度（节流在 _saveProg 内）
}

// 跳到任意绝对 bar（Home/End/进度条用）：先停播放，夹紧到 [0, N-1] 再整段重绘
export function seekTo(abs) {
  if (!state.replay) return;
  setPlay(false);
  _setAbs(state.replay, abs);                    // 夹紧在 _setAbs 内做
  renderAll();
  _saveProg();
}

export function setPlay(on) {
  if (!state.replay) return;
  const R = state.replay;
  R.playing = !!on;
  if (replayRAF) { cancelAnimationFrame(replayRAF); replayRAF = null; }
  if (R.playing) {
    // ★ P1-2：requestAnimationFrame + 每根停留 stepMs 毫秒（恒前进 1 根，绝不跳根）
    let last = performance.now();
    const tick = (now) => {
      // 守卫：回放被外部清掉（切换品种/退出）或已暂停 → 停
      if (!state.replay || !state.replay.playing) { replayRAF = null; return; }
      if (document.hidden) { setPlay(false); return; }        // ★ 切走标签页自动暂停，回来不跳根
      if (now - last >= R.stepMs) {
        last = now;
        const N = replayNI0()[2];
        if (R.abs >= N - 1) { setPlay(false); return; }
        const prev = R.abs;
        _setAbs(R, R.abs + 1);                                // ★ 恒为 1 根（旧 setInterval 每帧 speed 根会跳）
        // ★ P1-5 自动暂停：遇到开仓 bar（决策点）则暂停并高亮该笔（仅复看模式）
        const auto = $('rauto') && $('rauto').checked && canSeeAnswers();
        if (auto) {
          const hit = _openAbsList().find(it => it.abs > prev && it.abs <= R.abs);
          if (hit) { _setAbs(R, hit.abs); state.sel = hit.i; setPlay(false); renderAll(); return; }
        }
        renderStep();
        if (R.abs >= N - 1) { setPlay(false); return; }
      }
      replayRAF = requestAnimationFrame(tick);
    };
    replayRAF = requestAnimationFrame(tick);
  } else {
    try { renderTable(); } catch (e) {}   // 暂停/停止 → 同步一次右侧表格
  }
  updateReplayUI();
}
