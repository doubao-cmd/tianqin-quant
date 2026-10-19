// 顶栏（品种/周期/区间按钮）+ 右侧面板显隐 + tab 切换
import { state, PERIODS, RANGES, setContext, setMode, canSeeAnswers } from '../core/state.js';
import { isDaily } from '../core/time.js';
import { $ } from '../core/fmt.js';
import { DB } from '../data/index.js';

// ★ T1.3 左栏折叠：状态记 localStorage（刷新后保持）。
//   注意"折叠"不是"缩小"——#left 直接 display:none，否则 0 宽元素仍占 grid gap 并继续被 Tab 摸到。
const _LEFT_KEY = 'rt_left_off';
try { state._leftOff = localStorage.getItem(_LEFT_KEY) === '1'; } catch (e) { state._leftOff = false; }

export function applyLeft() {
  const g = $('grid');
  if (!g) return;
  g.classList.toggle('left-off', !!state._leftOff);
  const b = $('lcolBtn');
  if (b) {
    b.classList.toggle('off', !!state._leftOff);
    b.textContent = state._leftOff ? '▶ 分时' : '◀ 分时';
    b.title = state._leftOff
      ? '展开左栏（分时图 + 行情卡）· Ctrl+←'
      : '折叠左栏，主图多 220px（看 1min/5min 时分时图与主图重复）· Ctrl+←';
  }
}

export function toggleLeft() {
  state._leftOff = !state._leftOff;
  try { localStorage.setItem(_LEFT_KEY, state._leftOff ? '1' : '0'); } catch (e) {}
  applyLeft();
  safeResize();          // ★ 必须走 safeResize（尺寸指纹比对），不能直接 Plotly.Plots.resize
}

// ★ S4/A-freshness：数据新鲜度角标
//   数据来自 bundle 的 `_meta.freshness`（构建期读 `_缓存/pool/_last_update.json`）。
//   ★ 关键取舍：**如果这条信息缺失，就显示红色告警**，绝不留空。
//     方案 §2.6 的原话是「让失败可见」—— 一个空白的角标等于失败隐身。
export function renderFreshness() {
  const el = $('fresh');
  if (!el) return;                      // 旧 index.html 可能没这个节点 ⇒ 静默跳过
  const f = (DB.meta && DB.meta.freshness) || null;
  const arch = f && f.archive_total ? ` · archive ${Number(f.archive_total).toLocaleString()} 行` : '';
  if (!f) {
    el.textContent = '数据新鲜度未知（bundle 缺 _meta.freshness）· 请重跑 build_data.py';
    el.className = 'fresh stale';
    return;
  }
  el.textContent = f.text + arch;
  el.className = 'fresh' + (f.stale ? ' stale' : '');
  el.title = f.stale
    ? '数据已过期（>24h）或上次更新失败。\n处置：跑 工具/复盘/每日数据更新.bat，'
      + '失败原因见 _缓存/pool/_FAILED_*.txt'
    : '数据新鲜度正常（来源：_缓存/pool/_last_update.json）';
}

export function renderTop() {
  renderFreshness();
  const w = $('watch'); w.innerHTML = '';
  // ★ 品种 = 单个按钮，点开弹出 picker（用户 2026-10-18）
  const btn = document.createElement('button');
  btn.id = 'symBtn'; btn.className = 'sym-btn';
  const cur = DB.data[state.sym];
  btn.textContent = (cur ? cur.name : state.sym) + ' ▾';
  btn.onclick = (e) => { e.stopPropagation(); toggleSymPicker(); };
  w.appendChild(btn);
  const picker = document.createElement('div');
  picker.id = 'symPicker'; picker.className = 'sym-picker';
  picker.style.display = (w.dataset.open === '1') ? 'block' : 'none';
  Object.keys(DB.data).forEach(k => {
    const d = DB.data[k];
    const b = document.createElement('button');
    b.className = 'sym-item' + (k === state.sym ? ' on' : '');
    b.textContent = d.name + (state.replay ? '' : '  ' + d.last.toFixed(0));
    b.onclick = (e) => {
      e.stopPropagation();
      setContext({sym:k});
      w.dataset.open = '0'; picker.style.display = 'none';
    };
    picker.appendChild(b);
  });
  w.appendChild(picker);
  // 数据截至 / 刷新提示
  const di = $('dataInfo');
  if (di) {
    const m = DB.meta;
    di.textContent = m ? `数据截至 ${m.data_end} · 收盘后运行「刷新数据.bat」更新` : '';
  }
  // ★ P1-5：训练/复看模式切换按钮
  const ms = $('modeSlot'); ms.innerHTML = '';
  const mbtn = document.createElement('button');
  mbtn.className = 'mode-btn' + (state.mode === 'train' ? ' train' : '');
  mbtn.textContent = state.mode === 'train' ? '训练模式' : '复看模式';
  mbtn.title = '切换训练/复看模式（训练模式隐藏成交与统计，防止开卷考试）';
  mbtn.onclick = () => setMode(state.mode === 'train' ? 'review' : 'train');
  ms.appendChild(mbtn);
  const p = $('periods'); p.innerHTML = '';
  PERIODS.forEach(([k,label]) => {
    const b = document.createElement('button'); b.textContent = label;
    if (k === state.period) b.className = 'on';
    b.onclick = () => setContext({period:k});
    p.appendChild(b);
  });
  const r = $('ranges'); r.innerHTML = '';
  // 日线只有 20 来根，分段没意义 → 按钮置灰，一律全显示
  const rangeLocked = isDaily();
  RANGES.forEach(([k,label]) => {
    const b = document.createElement('button'); b.textContent = label;
    if (k === (rangeLocked ? 'ALL' : state.range)) b.className = 'on';
    b.disabled = rangeLocked;
    if (rangeLocked) b.title = '日线只有 20 来根，不需要分段';
    b.onclick = () => { if (rangeLocked) return; setContext({range:k}); };
    r.appendChild(b);
  });
  $('ind').value = state.ind;
}

// ★ P5-2：resize 只在「容器尺寸真的变了」时才做。
//   原来是 applyLayout 里 setTimeout(60) 盲等 —— 布局未必已完成，会先按错误尺寸算一次，
//   再被 Plotly `responsive:true` 的 ResizeObserver 触发第二次 ⇒ 一次切换 resize 2~3 次。
//   改成：rAF 等一帧（布局已定）+ 尺寸指纹比对（没变就跳过）+ 关掉 responsive 自己管。
let _rszPending = false;
export function safeResize() {
  if (_rszPending) return;
  _rszPending = true;
  requestAnimationFrame(() => {
    _rszPending = false;
    const a = $('main'), b = $('intra');
    if (!a || !b) return;
    const key = [a.clientWidth, a.clientHeight, b.clientWidth, b.clientHeight].join('x');
    if (state._rszKey === key) return;          // 尺寸指纹没变 ⇒ 不 resize（省掉一次全量重算）
    state._rszKey = key;
    try { Plotly.Plots.resize('main'); } catch (e) {}
    try { Plotly.Plots.resize('intra'); } catch (e) {}
  });
}

export function applyLayout() {
  const g = $('grid');
  g.classList.toggle('with-right', state.showTrades);
  $('tbtn').classList.toggle('on', state.showTrades);
  applyLeft();                 // ★ T1.3：左栏折叠与"显示/隐藏右栏"互不干扰，各自一个 class
  safeResize();
}

export function setTab(t) {
  state._tab = t;
  $('tabT').classList.toggle('on', t === 'trade');
  $('tabS').classList.toggle('on', t === 'stats');
  $('tabB').classList.toggle('on', t === 'book');
  // ★ P2-5：第 4 个 tab（对照）。id 用 tabC，避免与已有 tabB/tabS 撞名
  const tc = $('tabC'); if (tc) tc.classList.toggle('on', t === 'cmp');
  // ★ T2.3：第 5 个 tab（标记列表）
  const tm = $('tabM'); if (tm) tm.classList.toggle('on', t === 'marks');
  $('paneTrade').style.display = t === 'trade' ? 'flex' : 'none';
  $('paneStats').style.display = t === 'stats' ? 'flex' : 'none';
  $('paneBook').style.display = t === 'book' ? 'flex' : 'none';
  const pc = $('paneCmp'); if (pc) pc.style.display = t === 'cmp' ? 'flex' : 'none';
  const pm = $('paneMk'); if (pm) pm.style.display = t === 'marks' ? 'flex' : 'none';
}

// ★ T2.3：标记 tab 在训练模式**不渲染**（不是 disabled）——渲染了就能从 DOM/source 里读到
//   自己的历史判断，训练时那等于半个答案。切回复看模式时恢复。
export function syncMarkTab() {
  const tm = $('tabM');
  if (!tm) return;
  const show = canSeeAnswers();
  tm.style.display = show ? '' : 'none';
  if (!show && state._tab === 'marks') setTab('trade');
}


export function toggleSymPicker() {
  const w = $('watch'); const p = $('symPicker');
  const open = w.dataset.open === '1';
  w.dataset.open = open ? '0' : '1';
  p.style.display = open ? 'none' : 'block';
}

export function closeSymPicker() {
  const w = $('watch'); const p = $('symPicker');
  if (w) w.dataset.open = '0';
  if (p) p.style.display = 'none';
}
