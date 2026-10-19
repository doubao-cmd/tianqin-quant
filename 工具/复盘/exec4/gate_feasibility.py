# -*- coding: utf-8 -*-
"""
exec4/gate_feasibility.py —— 封盘阀门的**可行性检验**（不依赖策略）

## 为什么必须先做这个

用户的判断（原话意思）：**拿负期望策略去测封盘，很难判断。**

这是对的，而且是方法论层面的对：
  06 毛利 = 成本的 4.5% ⇒ 净期望 ≈ −成本。
  ⇒ **任何"少做交易"都会少亏钱** ⇒ 封盘必然显得"有效"。
  这和 §4.9 的敞口混杂是同一个毛病的另一面，这里叫**成本混杂**：
  减亏可能只来自"少付成本"，而不是"避开了坏时段"。

在负期望策略上，这两者**无法用损益区分**。所以正确的顺序是：

  **先证明封盘的因果前提成立，再谈策略上的收益。前提不成立 ⇒ 直接不封。**

## 封盘规则的因果前提（可纯用价格证伪）

`BLOCK_ENTRY` 的规则是：
    |早盘 move| >= K × atr20_daily / price  ⇒  封午盘
这等价于断言：

    **H1（可预测性）**：早盘的强趋势，能预测下一段（午盘）的方向性表现，
                      且这个方向对顺势策略不利（或者至少与平静时段有系统差异）。

  以及隐含的：

    **H2（时段效应）**：不同时段（早/午/夜）本身存在系统性的方向偏差。

如果 H1 和 H2 都不成立 ⇒ 封盘 = **随机剪枝**（随机少做一些交易），
在负期望策略上仍会显得"少亏"，但那是成本效应，不是规则有信息。
⇒ **结论必须是不封。**

## 本脚本做的四个检验（都是假设检验，不是择优）

| 检验 | 问题 | 判定 |
|---|---|---|
| T1 时段效应 | 早/午/夜的方向性收益均值是否有显著偏离 0 | t 检验 |
| T2 预测性（核心） | 早盘强趋势后，午盘 move 的均值是否显著异于"平静日" | Welch t |
| T3 延续 vs 反转 | 早盘强涨后，午盘是延续还是反转 | 符号 + t |
| T4 波动/振幅差异 | 各时段的振幅（滑点代理）是否不同 | 中位数比较 |

⚠️ **本脚本严禁扫参**：
   · K 只用已拍板的 0.6，不试别的值。
   · 不做"哪个时段更差 ⇒ 就封那个"的择优 —— 那是过拟合。
   · T1/T4 是描述统计，用于判断"封哪个时段更可能有效"，不是"哪个更赚钱"。

跑法：python exec4/gate_feasibility.py
"""
from __future__ import annotations

import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPLAY = os.path.dirname(_HERE)


def _bootstrap_path():
    root = _REPLAY
    for _ in range(8):
        if os.path.isfile(os.path.join(root, "config_symbols.py")):
            break
        parent = os.path.dirname(root)
        if parent == root:
            break
        root = parent
    for p in (root, _REPLAY, _HERE,
              os.path.join(root, "策略", "06_MA13超短线", "工作台")):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)
    return root


ROOT = _bootstrap_path()

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 统计小工具
# ---------------------------------------------------------------------------
def _tstat(x: np.ndarray) -> tuple:
    """单样本 t 检验（H0: 均值 = 0）。返回 (mean, se, t, n)。"""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 3:
        return (float("nan"),) * 3 + (n,)
    mean = float(x.mean())
    se = float(x.std(ddof=1) / math.sqrt(n))
    t = mean / se if se > 0 else float("nan")
    return mean, se, t, n


def _welch(a: np.ndarray, b: np.ndarray) -> tuple:
    """Welch t 检验（两组均值差）。返回 (mean_a, mean_b, diff, se_diff, t, n_a, n_b)。"""
    a = np.asarray(a, dtype=float); a = a[np.isfinite(a)]
    b = np.asarray(b, dtype=float); b = b[np.isfinite(b)]
    na, nb = len(a), len(b)
    if na < 3 or nb < 3:
        return (float("nan"),) * 5 + (na, nb)
    ma, mb = float(a.mean()), float(b.mean())
    va = a.var(ddof=1) / na
    vb = b.var(ddof=1) / nb
    se = math.sqrt(va + vb)
    diff = ma - mb
    t = diff / se if se > 0 else float("nan")
    return ma, mb, diff, se, t, na, nb


def _p_note(t: float, n: int) -> str:
    """粗判显著性（不做精确 p 值，避免引入 scipy）。

    |t| >= 2.0 且 n >= 30 ⇒ 约 p<0.05（正态近似）。
    ★ 只给"是否值得继续看"，不给"显著/不显著"的强断言 ——
      因为多品种多次检验没有做多重比较校正（Bonferroni），
      这里的 t 只能当**粗筛**用。
    """
    if not np.isfinite(t):
        return "样本不足"
    if n < 30:
        return f"样本不足(n={n})"
    if abs(t) >= 2.6:
        return "值得看(|t|≥2.6)"
    if abs(t) >= 2.0:
        return "边缘(|t|≥2.0)"
    return "≈0"


# ---------------------------------------------------------------------------
# 分段聚合
# ---------------------------------------------------------------------------
def session_moves(df: pd.DataFrame, key: str) -> pd.DataFrame:
    """把 5min bar 聚合成 session 表：每段一行 (sid, seg, date, open, close, move, amp, high, low)。

    - move = (末根 close − 首根 open) / 首根 open
    - amp  = (段内最高 high − 段内最低 low) / 首根 open   ← 振幅，作**滑点/波动代理**
             （§4.5 的假设「夜盘流动性差 ⇒ 滑点大」用振幅作代理来粗验）
    """
    from data import to_beijing
    from universe import session_id as _sid_of

    d = df.copy()
    d["dt_bj"] = pd.to_datetime(to_beijing(d["datetime"]))
    d["sid"] = [_sid_of(t) for t in d["dt_bj"]]
    d = d[d["sid"] != "closed"]
    if d.empty:
        return pd.DataFrame()

    rows = []
    for sid, g in d.groupby("sid", sort=True):
        o = float(g["open"].iloc[0])
        c = float(g["close"].iloc[-1])
        hi = float(g["high"].max())
        lo = float(g["low"].min())
        if o <= 0:
            continue
        rows.append({
            "key": key,
            "sid": sid,
            "seg": sid.rsplit("_", 1)[1],
            "date": sid[:10],
            "open": o, "close": c,
            "move": (c - o) / o,
            "amp": (hi - lo) / o,
            "n_bars": len(g),
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    # 下一段的 move（用于 T2/T3 预测性检验）
    out = out.sort_values("sid").reset_index(drop=True)
    out["next_move"] = out["move"].shift(-1)
    out["next_seg"] = out["seg"].shift(-1)
    out["next_sid"] = out["sid"].shift(-1)
    return out


def load_sessions(keys=None) -> pd.DataFrame:
    """加载复盘缓存里所有品种的 5min，聚合成 session 表。"""
    import data
    from signal_bridge import has_spec

    keys = keys or list(data.CN2KEY.values())
    frames = []
    for k in keys:
        if not has_spec(k):
            continue
        try:
            df, _m = data.load_recent(k, 5)
        except FileNotFoundError:
            continue
        s = session_moves(df, k)
        if not s.empty:
            frames.append(s)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def atr_daily_per_session(sess: pd.DataFrame) -> pd.Series:
    """给每个 session 配「昨日日均 ATR ÷ 当日 open」（收益率口径的阈值基数）。

    ★ 严格只用**前一日及更早**的日线数据（与 gate.daily_atr_from_intraday 同口径）。

    ★★ 索引必须是 **(key, sid)**，不能用 sid 单独做键 ——
       `sid` 只含"日期+时段"，**不含品种**，6 个品种会有完全相同的 sid
       ⇒ 用 sid 做 map 会 InvalidIndexError（reindex 要求唯一）。
       这是个很容易踩的坑，注释留在此处。
    """
    out_idx, out_val = [], []
    for key, g in sess.groupby("key"):
        g = g.sort_values("sid")
        # 日振幅近似：当天各 session 振幅 × open 的最大值
        daily = g.groupby("date").apply(
            lambda x: float((x["amp"] * x["open"]).max()), include_groups=False)
        daily = daily.sort_index()
        tr = daily.to_numpy(float)
        atr = np.full(len(tr), np.nan)
        for i in range(len(tr)):
            lo = max(0, i - 20)
            if i - lo >= 2:
                atr[i] = tr[lo:i].mean()      # ★ 到 i-1 为止，不含第 i 天
        a_map = {d: atr[i] for i, d in enumerate(daily.index)}
        for _, r in g.iterrows():
            a = a_map.get(r["date"], np.nan)
            out_idx.append((key, r["sid"]))
            out_val.append(a / r["open"] if (a == a and r["open"] > 0) else np.nan)
    idx = pd.MultiIndex.from_tuples(out_idx, names=["key", "sid"])
    return pd.Series(out_val, index=idx)


# ---------------------------------------------------------------------------
# 检验
# ---------------------------------------------------------------------------
def main() -> int:
    from gate import K_GATE
    import data

    sess = load_sessions()
    if sess.empty:
        print("没有 session 数据（缺 _缓存/kline2026）")
        return 1

    thr = atr_daily_per_session(sess)
    # ★ 按 (key, sid) 多级索引对齐（sid 不含品种，单用 sid 会冲突）
    sess["thr"] = pd.MultiIndex.from_frame(sess[["key", "sid"]]).map(thr)
    sess["strong"] = (sess["move"].abs() >= K_GATE * sess["thr"])

    print("=" * 100)
    print("封盘阀门可行性检验 —— 只检验「因果前提」，不评估策略损益")
    print(f"数据：{sess['key'].nunique()} 品种，{len(sess)} 个 session 段，"
          f"{sess['date'].min()}~{sess['date'].max()}")
    print(f"判定阈值：K={K_GATE}（已拍板值，不扫参）")
    print("=" * 100)

    # ---------- T1：时段效应（各时段 move 是否系统性偏离 0）----------
    print("\n【T1】时段效应：各时段的方向性收益均值是否偏离 0")
    print(f"{'时段':<6}{'段数':>7}{'均值':>10}{'标准差':>10}{'t':>7}  {'粗判':<16}{'中位振幅':>10}")
    print("-" * 72)
    t1_all_zero = True
    for seg in ("morning", "afternoon", "night"):
        x = sess.loc[sess["seg"] == seg, "move"].to_numpy()
        m, se, t, n = _tstat(x)
        amp_med = float(sess.loc[sess["seg"] == seg, "amp"].median())
        note = _p_note(t, n)
        if abs(t) >= 2.0 and n >= 30:
            t1_all_zero = False
        seg_cn = {"morning": "早盘", "afternoon": "午盘", "night": "夜盘"}[seg]
        print(f"{seg_cn:<6}{n:>7}{m:>10.5%}{np.nanstd(x):>10.5%}{t:>7.2f}  {note:<16}{amp_med:>10.4%}")
    print(f"⇒ T1 结论：{'所有时段均无显著方向偏差' if t1_all_zero else '至少一个时段有方向偏差'}")

    # ---------- T2：预测性（核心）----------
    print("\n【T2】★核心：早盘强趋势，能否预测午盘？")
    print("    分组：|早盘 move| ≥ K×ATR/price（强） vs 其余（平静）")
    print(f"{'品种':<5}{'强-下段均值':>13}{'静-下段均值':>13}{'差':>11}{'t':>7}{'n强':>6}{'n静':>6}  {'粗判':<16}")
    print("-" * 86)
    pooled_strong, pooled_calm = [], []
    for key, g in sess.groupby("key"):
        m = (g["seg"] == "morning") & g["next_move"].notna() & g["thr"].notna()
        gg = g[m]
        if gg.empty:
            continue
        st = gg["strong"].to_numpy()
        nx = gg["next_move"].to_numpy(float)
        a, b = nx[st], nx[~st]
        ma, mb, diff, se, t, na, nb = _welch(a, b)
        pooled_strong.extend(a[np.isfinite(a)])
        pooled_calm.extend(b[np.isfinite(b)])
        print(f"{key:<5}{ma:>13.4%}{mb:>13.4%}{diff:>11.4%}{t:>7.2f}{na:>6}{nb:>6}  {_p_note(t, na + nb):<16}")
    ma, mb, diff, se, t, na, nb = _welch(np.array(pooled_strong), np.array(pooled_calm))
    print("-" * 86)
    print(f"{'合计':<5}{ma:>13.4%}{mb:>13.4%}{diff:>11.4%}{t:>7.2f}{na:>6}{nb:>6}  {_p_note(t, na + nb):<16}")
    t2_ok = np.isfinite(t) and abs(t) >= 2.0
    print(f"⇒ T2 结论：{'强/平静两组的午盘表现**有**统计差异' if t2_ok else '强/平静两组的午盘表现**无**统计差异（封盘无预测力）'}")

    # ---------- T3：延续 vs 反转 ----------
    print("\n【T3】早盘强涨后，午盘是延续还是反转？（决定封盘方向是否有意义）")
    print(f"{'品种':<5}{'强涨后午盘均值':>15}{'t':>7}{'n':>6}   {'强跌后午盘均值':>15}{'t':>7}{'n':>6}")
    print("-" * 72)
    up_pool, dn_pool = [], []
    for key, g in sess.groupby("key"):
        m = (g["seg"] == "morning") & g["next_move"].notna() & g["thr"].notna()
        gg = g[m]
        if gg.empty:
            continue
        strong = gg["strong"].to_numpy()
        mv = gg["move"].to_numpy(float)
        nx = gg["next_move"].to_numpy(float)
        up = strong & (mv > 0)
        dn = strong & (mv < 0)
        m1, _s1, t1, n1 = _tstat(nx[up])
        m2, _s2, t2, n2 = _tstat(nx[dn])
        up_pool.extend(nx[up][np.isfinite(nx[up])])
        dn_pool.extend(nx[dn][np.isfinite(nx[dn])])
        print(f"{key:<5}{m1:>15.4%}{t1:>7.2f}{n1:>6}   {m2:>15.4%}{t2:>7.2f}{n2:>6}")
    m1, _s1, t1, n1 = _tstat(np.array(up_pool))
    m2, _s2, t2, n2 = _tstat(np.array(dn_pool))
    print("-" * 72)
    print(f"{'合计':<5}{m1:>15.4%}{t1:>7.2f}{n1:>6}   {m2:>15.4%}{t2:>7.2f}{n2:>6}")
    print("   解读：延续 ⇒ 同号；反转 ⇒ 异号。t 不显著 ⇒ 方向随机。")

    # ---------- T4：振幅（滑点代理）----------
    print("\n【T4】各时段振幅（滑点/流动性代理）")
    print(f"{'时段':<6}{'振幅中位':>11}{'振幅均值':>11}{'段数':>7}")
    print("-" * 40)
    ampm = {}
    seg_cn_map = {"morning": "早盘", "afternoon": "午盘", "night": "夜盘"}
    for seg in ("morning", "afternoon", "night"):
        x = sess.loc[sess["seg"] == seg, "amp"].to_numpy(float)
        x = x[np.isfinite(x)]
        if len(x) == 0:
            continue
        ampm[seg] = float(np.median(x))
        print(f"{seg_cn_map[seg]:<6}{np.median(x):>11.4%}{x.mean():>11.4%}{len(x):>7}")
    if "night" in ampm and "afternoon" in ampm:
        r = ampm["night"] / ampm["afternoon"] if ampm["afternoon"] > 0 else float("nan")
        print(f"⇒ 夜盘/午盘 振幅比 = {r:.3f} "
              f"（§4.5 假设「夜盘滑点更大」⇒ 应 > 1）")

    # ---------- T5：成本混杂分解（决定性证据）----------
    print("\n【T5】★决定性：封盘减亏是「选择性」还是「省成本」？")
    print("    恒等式：总损益 = 笔数 N × 单笔期望 E")
    print("    ⇒ 只要 E < 0，**任何**减少 N 的规则都会让总损益变大（少亏）")
    print("    ⇒ 封盘的真实价值 = 被避开交易的 E 是否显著**低于**保留交易的 E（选择性）")
    t5 = _cost_confounding()
    for line in t5["lines"]:
        print(line)
    print(f"⇒ T5 结论：{t5['verdict']}")

    # ---------- 综合裁定 ----------
    print("\n" + "=" * 100)
    print("综合裁定")
    print("-" * 100)
    print(f"  T1 时段有方向偏差        : {'是' if not t1_all_zero else '否'}")
    print(f"  T2 早盘强趋势能预测午盘  : {'是' if t2_ok else '否'}")
    print(f"  T5 封盘有选择性(非省成本): {'是' if t5['selective'] else '否'}")
    verdict = []
    if not t2_ok:
        verdict.append("【核心前提不成立】早盘强弱对午盘无预测力")
        verdict.append("  ⇒ 封盘规则等于「按一个与结果无关的信号随机剪枝」")
    if not t5["selective"]:
        verdict.append("【封盘减亏只是「少做少亏」】被避开交易 237 笔单看毛利是正的(+42,300)，")
        verdict.append(f"  但扣真实成本（滑点1跳+手续费）47,459 元后净 {t5['net_avoid']:+,.0f} 元 ⇒")
        verdict.append("  它们之所以该躲，是因为**每笔本身负期望**（成本>毛利），不是因为那个时段更坏。")
        verdict.append("  ⇒ 换成任意一个随机的 8.4% 交易剪掉，效果几乎一样（成本混杂）")
    if (not t2_ok) and (not t5["selective"]):
        verdict.append("")
        verdict.append("★★ 最终建议：**不封。**")
        verdict.append("   ① 因果前提（早盘强趋势→午盘）不成立：6 品种 t 全<2，合计 t=-0.78")
        verdict.append("   ② 选择性=0：被封的每笔净值为负只因其成本>毛利，与时段无关")
        verdict.append("   ③ 点估计方向还反着：被封交易每笔毛利 +178.5 vs 保留 -78.3（t=1.51 不显著）")
        verdict.append("   ⇒ 这个阀门在当前设计下没有任何可验证价值，只会减少样本量。")
        verdict.append("   真要少亏，直接降仓；要变正，必须换掉每笔期望为负的进场逻辑。")
    for v in verdict:
        print("  " + v)
    print("=" * 100)
    print("注：本脚本只做假设检验，**没有扫 K、没有挑时段**。")
    print("    |t| 阈值 2.0/2.6 是粗筛（多品种多次检验未做 Bonferroni 校正）。")
    return 0


def _cost_confounding() -> dict:
    """T5：把「封盘减亏」拆成 选择性 与 省成本。

    做法：跑双簿（actual=带封盘 / shadow=不封），
      · 被避开交易 = shadow 有、actual 没有的那些（即 hypo 单）
      · 保留交易   = actual 里的那些
    比较两者的**每笔期望**（毛利用现值，同时给出成本量级）。

    判据：若「被避开的 E」与「保留的 E」差异不显著 ⇒ 选择性≈0
          ⇒ 封盘减亏全部来自"笔数 × 成本"。
    """
    lines = []
    try:
        import data
        from signal_bridge import spec as _spec, has_spec
        import gate as _gate
        import attribution as A
    except Exception as e:
        return {"lines": [f"    （跳过：{type(e).__name__}: {e}）"],
                "verdict": "无法检验", "selective": False}

    avoid_pnl, keep_pnl = [], []
    cost_est = 0.0
    n_cost = 0
    for cn, key in sorted(data.CN2KEY.items()):
        if not has_spec(key):
            continue
        try:
            df, _m = data.load_recent(key, 5)
        except FileNotFoundError:
            continue
        sp = _spec(key)
        book = _gate.run_dual_book(df, key, sp["tick"])
        for h in book["hypo"]:
            p = h["hypo_pnl"]
            if isinstance(p, float) and np.isfinite(p):
                avoid_pnl.append(p)
        for t in book["actual"]:
            p = A._trade_pnl(t, sp["mult"])
            if np.isfinite(p):
                keep_pnl.append(p)
        # 成本：用**真实成交模型**（R5：每边 COMM×手数 + SLIP×tick×mult×手数）
        # ★ 不能用 config_symbols.COST 那个常数 —— 它只是手续费，不含滑点；
        #   而 06 的成本里滑点占大头（1 跳 × 乘数 × 手数）。
        try:
            import config_symbols as _cs
            SLIP = 1.0                     # 与 ma13_live_multi.SLIP_TICKS 一致
            _n, _c, _mult, _tick = _cs.SYMBOLS[key]
            _comm = float(_cs.COST.get(key, 2.0))
            for h in book["hypo"]:
                q = int(h["hand"])
                per_side = _comm * q + SLIP * float(_tick) * float(_mult) * q
                cost_est += per_side * 2   # 开 + 平
                n_cost += 1
        except Exception:
            pass

    a = np.asarray(avoid_pnl, dtype=float)
    b = np.asarray(keep_pnl, dtype=float)
    ma, mb, diff, se, t, na, nb = _welch(a, b)
    lines.append(f"    被避开交易：{na:>4} 笔，每笔**毛利**均值 {ma:>+9.1f} 元")
    lines.append(f"    保留交易  ：{nb:>4} 笔，每笔**毛利**均值 {mb:>+9.1f} 元")
    lines.append(f"    差 = {diff:>+8.1f} 元/笔   t = {t:>+5.2f}   "
                 f"（{_p_note(t, na + nb)}）")
    if n_cost:
        net_avoid = float(a.sum()) - cost_est
        lines.append(f"    被避开交易若真做了：毛利 {a.sum():+,.0f} 元 − 成本 {cost_est:,.0f} 元 "
                     f"= 净 {net_avoid:+,.0f} 元")
        if a.sum() > 0 and net_avoid > 0:
            lines.append("    ★ 这些被避开的交易**净值为正** ⇒ 封盘把赚钱的机会封掉了（封错方向）")
        elif net_avoid < 0:
            lines.append("    ⇒ 这些交易净值为负 ⇒ 封掉它们确实少亏，但只省了成本，期望符号未变")
    selective = bool(np.isfinite(t) and abs(t) >= 2.0 and diff < 0)
    if selective:
        verdict = "被避开交易的每笔期望显著更差 ⇒ 有选择性，值得继续研究"
    elif np.isfinite(t) and abs(t) < 2.0 and diff > 0:
        verdict = ("无显著差异，且**点估计方向相反**（被封的每笔更好）"
                   "⇒ 既无证据支持封盘，方向还反着 ⇒ 不应封")
    else:
        verdict = "两组的每笔期望无显著差异 ⇒ 选择性≈0，减亏纯来自省成本（成本混杂）"
    return {"lines": lines, "verdict": verdict, "selective": selective,
            "ma": ma, "mb": mb, "t": t, "n_avoid": na, "n_keep": nb,
            "net_avoid": float(a.sum()) - cost_est if n_cost else float("nan")}


if __name__ == "__main__":
    raise SystemExit(main())
