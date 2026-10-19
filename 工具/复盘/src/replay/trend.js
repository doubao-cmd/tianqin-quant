// 训练曲线数据（五期 T2.1）——纯函数，不碰 DOM / 不碰 Plotly
//
// 为什么要这层：`score.js` 每次都把 hitRate/avoidQ/overTrade 算出来了，
// 但只在退出回放时弹一次卡片。想看"这周是不是比上周准"只能导 CSV 开 Excel。
// 指标算对了却看不见 = 白算。
//
// ★★ 红线（写死在代码里，不是注释建议）：
//   1. `hitRate` **不是胜率**。它是「你的方向判断」与「后 N 根实际走势」的一致率。
//      两者的分母不同（胜率按已平仓笔算，命中率按标记算），混称会让人以为自己在赚钱。
//   2. 会话数 < 2 ⇒ 不画图（单点连线会假装成"趋势"）。
//   3. 样本 < 5 的会话点标记为空心（`circle-open`），从不隐藏——藏掉会让人以为没练过。
//   4. 缺字段的会话计数上报，不静默丢（否则"少了几次"会被误读成"退步"）。

export const TREND_MIN_SESSIONS = 2;
export const TREND_MIN_SAMPLE = 5;

function _trNum(v) { return (typeof v === 'number' && isFinite(v)) ? v : null; }

/**
 * @param {Array} list 会话台账（lgSessions() 的返回，已按 t0 升序）
 * @returns {{
 *   ok:boolean, why:string, n:number, missing:number, noSample:number,
 *   xs:number[], hit:(number|null)[], hitHollow:boolean[],
 *   avoid:(number|null)[], avoidHollow:boolean[],
 *   over:(number|null)[], manual:(number|null)[],
 *   labels:Array<{date:string,sym:string,period:string,hit:number|null,nHit:number|null}>
 * }}
 */
export function trendSeries(list) {
  const rows = Array.isArray(list) ? list.filter(r => r && r.id) : [];
  const out = {
    ok: false, why: '', n: 0, missing: 0, noSample: 0,
    xs: [], hit: [], hitHollow: [],
    avoid: [], avoidHollow: [],
    over: [], manual: [], labels: [],
  };
  // 只认"有命中率"的会话（老版本台账可能没这列）
  const use = rows.filter(r => _trNum(r.hitRate) != null);
  out.missing = rows.length - use.length;
  out.n = use.length;
  if (use.length < TREND_MIN_SESSIONS) {
    out.why = `至少完成 ${TREND_MIN_SESSIONS} 次复盘才能看趋势（当前 ${use.length} 次）`;
    return out;
  }

  use.forEach((r, i) => {
    const nHit = _trNum(r.nHitSample);
    const nAv = _trNum(r.nAvoidSample);
    out.xs.push(i + 1);
    out.hit.push(Math.round(_trNum(r.hitRate) * 1000) / 10);          // 0.62 → 62.0(%)
    // 样本数未知（老台账没有这列）也按空心显示：无法证明样本充足时，宁可保守
    out.hitHollow.push(nHit == null || nHit < TREND_MIN_SAMPLE);
    if (out.hitHollow[i]) out.noSample++;

    const av = _trNum(r.avoidQ);
    out.avoid.push(av == null ? null : Math.round(av * 1000) / 1000);
    out.avoidHollow.push(nAv == null || nAv < TREND_MIN_SAMPLE);

    const ov = _trNum(r.overTrade);
    out.over.push(ov == null ? null : Math.round(ov * 100) / 100);
    const ms = _trNum(r.manualShare);
    out.manual.push(ms == null ? null : Math.round(ms * 1000) / 10);  // 0.4 → 40.0(%)
    out.labels.push({
      date: (r.t0 ? new Date(r.t0) : null),
      sym: r.sym || '', period: r.period || '',
      hit: _trNum(r.hitRate), nHit,
    });
  });
  out.ok = true;
  out.why = '';
  return out;
}

// 图上 hover 文案（放在这里而不是 stats.js：便于单测格式）
export function trendHover(lab) {
  const d = lab && lab.date;
  const ymd = d ? `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}` : '—';
  const t = d ? `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}` : '';
  const hit = lab && lab.hit != null ? (lab.hit * 100).toFixed(0) + '%' : '—';
  const n = lab && lab.nHit != null ? lab.nHit : '未记录';
  return `${ymd} ${t}<br>${lab ? lab.sym : ''} · ${lab ? lab.period : ''}min<br>方向一致率 ${hit} · 样本 ${n}`;
}
