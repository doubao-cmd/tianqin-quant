# -*- coding: utf-8 -*-
"""
exec4/pool.py —— 四期 S2：50 品种池扫描 + C 混合选腿 + 影子池
====================================================================
四期方案拍板的选品种逻辑（§11 / 拍板①）：
    「成交额降序 + |ρ| ≤ 0.5 封顶的**贪心加腿**」
    · 禁枚举 C(50,5) = 211 万种组合（算不完，也不该用来挑最优）
    · 0.5 这个阈值出自实测「RB↔JM = 0.508，绝不能同选」
    · 5 腿（拍板③）；EB 删掉（僵尸：无台账、心跳停 10-09）

★ 本模块修正了工作台 `selection.py` 的一个口径错误：
    `select_two_stage` 阶段1 用**近 20 日平均手数**排序，而四期方案要的是**成交额**。
    手数跨品种不可比（JM 1 手 = 60 吨，AU 1 手价值最高但手数很少）⇒
    实测同一批数据下两套榜单有 7/20 个品种不同：
        手数榜独有：SA FG RB RM C SR HC
        成交额榜独有：AU AG CU SN CF Y OI
    这正是「SA 靠手数混进池子」的原因。本模块一律用成交额。

★ 无未来函数：只用**已收盘**日线（调用方保证）；相关性用日收益率，不用绝对价。

★ 本模块是**纯函数层**：不联网、不读 tqsdk、不写状态。数据由调用方喂进来
  （生产用 `工作台/data_feed.build_universe_daily`），因此可完全离线单测。
"""
from __future__ import annotations

from typing import Iterable, Optional

# 拍板③：EB 是僵尸腿（无台账、心跳停 2026-10-09），永不入选
BLACKLIST = ("EB",)

# 拍板①：相关性封顶。0.5 来自实测 RB↔JM = 0.508（同品种链，绝不能同选）
MAX_RHO = 0.5

# 拍板③：5 腿
N_LEGS = 5

# S2 默认参数（冻结，不许回测调优 —— 铁律 2）
VOL_WINDOW = 20        # 成交额均值窗口（日）
CORR_WINDOW = 20       # 相关性窗口（日收益率根数）
MIN_BARS = 10          # 算相关性至少需要的收益率根数

# ---------------------------------------------------------------------------
# ★★ 粒度闸门（2026-10-18 新增，AU/CU 事件）
#
# 问题：`sizer.RiskParSizer` 的手数是
#     h = round(equity_base × risk_pct ÷ (atr × tick × mult))，夹 [hand_min, hand_max]
#     再 h = min(h, equity_base × notional_cap ÷ (price × mult))
#   但最后一步有 `max(hand_min, 名义手数)` 兜底 ⇒ **当 1 手就已经超 notional_cap 时，
#   它还是会硬开 1 手**。这不是 bug（不兜底就没法交易），但它意味着：
#   「这个品种的最小交易粒度，对这套资金量来说太粗了」。
#
# 实测（2026-10-18，notional_cap 绝对值 = 20万 × 2.0 = 40 万）：
#     AU  1 手 = 901,820 元 = 2.25× cap  ← 1 手就吃掉 0.91×账户权益
#     CU  1 手 = 549,300 元 = 1.37× cap
#     SN  1 手 = 376,860 元 = 0.94× cap  ← 勉强可交易
#   AU 进池后 5 腿同向可达 1.76×权益，加 CU 就到 2.16× ⇒ 撞 RiskGate 上限被拒开。
#
# ⇒ 判据：**1 手名义 > notional_cap 的品种不进池**。
#   理由来自策略自己的风控定义（equity_base × notional_cap），不引入新数值；
#   这是「可行性筛选」不是「策略过滤」—— 不改信号、不择优、不调参。
# ---------------------------------------------------------------------------
DEFAULT_NOTIONAL_CAP = 400_000.0    # = equity_base 200,000 × notional_cap 2.0


# ---------------------------------------------------------------------------
# 基础量
# ---------------------------------------------------------------------------
def turnover_20d(d: dict, mult: float,
                 window: int = VOL_WINDOW) -> Optional[float]:
    """近 window 日**平均成交额**（元/日）= mean(volume × close × mult)。

    `d` 是**单个品种**的日线字典 {"close": [...], "volume": [...]}（最新在末）。

    用成交额而不是手数：手数跨品种不可比（乘数 5~300、价格 1000~500000 都有）。
    返回 None 表示数据不足（调用方应跳过该品种，而不是拿 0 顶替）。
    """
    d = d or {}
    vol = d.get("volume") or []
    close = d.get("close") or []
    n = min(len(vol), len(close))
    if n < window or not mult:
        return None
    vs = vol[-window:]
    cs = close[-window:]
    try:
        tot = sum(float(v) * float(c) for v, c in zip(vs, cs))
    except (TypeError, ValueError):
        return None
    if tot <= 0:
        return None
    return tot / float(window) * float(mult)


def _returns(close: list, window: int) -> list:
    """近 window 个日收益率（长度 = window）。不足返回 []。"""
    if not close or len(close) < window + 1:
        return []
    w = close[-(window + 1):]
    out = []
    for i in range(len(w) - 1):
        a, b = w[i], w[i + 1]
        try:
            a, b = float(a), float(b)
        except (TypeError, ValueError):
            return []
        if a <= 0 or b <= 0:
            return []
        out.append(b / a - 1.0)
    return out


def min_lot_notional(d: dict, mult: float) -> Optional[float]:
    """**最小 1 手**的名义价值（元）= 最新价 × 乘数。

    用于粒度闸门：如果连 1 手都超过 notional_cap，这个品种对当前资金量来说太粗。
    返回 None 表示数据不足。
    """
    close = (d or {}).get("close") or []
    if not close or not mult:
        return None
    try:
        px = float(close[-1])
    except (TypeError, ValueError):
        return None
    if px <= 0:
        return None
    return px * float(mult)


def _pearson(x: list, y: list) -> Optional[float]:
    """皮尔逊相关系数。任一方零方差、长度不符、样本太少 ⇒ None。"""
    n = min(len(x), len(y))
    if n < MIN_BARS:
        return None
    x = [float(v) for v in x[:n]]
    y = [float(v) for v in y[:n]]
    mx = sum(x) / n
    my = sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 1e-18 or syy <= 1e-18:
        return None
    return sxy / (sxx ** 0.5 * syy ** 0.5)


def corr_matrix(daily: dict, keys: Iterable[str],
                window: int = CORR_WINDOW) -> dict:
    """日收益率相关性矩阵。返回 {(k1,k2): rho}（同时写入对称键），算不出的不出现。

    ★ 用**收益率**不是价格：两个同趋势不同量级的品种，价格相关性虚高。
    """
    keys = [k for k in keys if k in daily]
    rets = {}
    for k in keys:
        r = _returns((daily[k] or {}).get("close") or [], window)
        if r:
            rets[k] = r
    out = {}
    ks = sorted(rets)
    for i, a in enumerate(ks):
        for b in ks[i + 1:]:
            rho = _pearson(rets[a], rets[b])
            if rho is None:
                continue
            out[(a, b)] = rho
            out[(b, a)] = rho
    return out


def rho_of(corr: dict, a: str, b: str) -> Optional[float]:
    """取相关系数；同一品种恒 1.0；算不出的返回 None（调用方须按"宁可保守"处理）。"""
    if a == b:
        return 1.0
    return corr.get((a, b))


# ---------------------------------------------------------------------------
# 贪心加腿（拍板① 的实现）
# ---------------------------------------------------------------------------
def greedy_legs(ranked: list, corr: dict, n_legs: int = N_LEGS,
                max_rho: float = MAX_RHO, treat_none_as_block: bool = True) -> dict:
    """按 `ranked`（已按流动性降序）贪心加腿，任一新腿与**所有已选腿**的 |ρ| 都必须 ≤ max_rho。

    为什么贪心而不是枚举：C(50,5) = 2,118,760 种组合，枚举既算不完，
    也等于"在多组解里挑最优" —— 那就变成参数寻优了（铁律 2）。

    treat_none_as_block：相关系数算不出来时是否当作"冲突"而拒绝。
      ★ 默认 True（保守）。理由：算不出来 ≠ 不相关。宁可选一个已证明低相关的品种，
      也不要因为"不知道"就把两个可能高度同源的品种放进同一个池子。

    返回 {"legs": [...], "rejected": [(key, 冲突对象, rho)], "skipped_no_data": [...]}
    """
    legs: list = []
    rejected: list = []
    for k in ranked:
        if len(legs) >= n_legs:
            break
        if k in BLACKLIST:
            rejected.append((k, "BLACKLIST", None))
            continue
        # 与所有已选腿比一遍：取 |ρ| 最大的那个作为"冲突对象"
        worst_sym = None
        worst_rho = None
        blocked_sym = None          # 相关系数算不出 ⇒ 保守拒绝，记下是谁算不出
        for s in legs:
            r = rho_of(corr, k, s)
            if r is None:
                if treat_none_as_block:
                    blocked_sym = s
                    break
                continue
            if worst_rho is None or abs(r) > abs(worst_rho):
                worst_sym, worst_rho = s, r
        if blocked_sym is not None:
            rejected.append((k, blocked_sym, None))
        elif worst_rho is None or abs(worst_rho) <= max_rho:
            legs.append(k)          # 与全部已选腿都不冲突（或腿数为 0）
        else:
            rejected.append((k, worst_sym, worst_rho))
    return {"legs": legs, "rejected": rejected}


# ---------------------------------------------------------------------------
# 全流程
# ---------------------------------------------------------------------------
def select_pool(daily: dict, specs: dict, n_legs: int = N_LEGS,
                max_rho: float = MAX_RHO, vol_window: int = VOL_WINDOW,
                corr_window: int = CORR_WINDOW,
                require_spec: bool = True,
                notional_cap: float = DEFAULT_NOTIONAL_CAP) -> dict:
    """S2 主入口：粒度闸门 → 流动性闸门 → 相关性封顶贪心加腿。

    参数
    ----
    daily : {key: {"close": [...], "volume": [...]}}，**已收盘**日线，最新在末。
    specs : {key: {"mult": float, ...}} —— 乘数（算成交额必需）。
            生产用 `signal_bridge.spec(k)`；`require_spec=True` 时缺规格的品种被排除
            （乘数错 = 手数与盈亏全错，宁可少选，不可错选）。
    require_spec : 是否强制要求有核验规格（config_symbols.SYMBOLS）。
    notional_cap : 单腿名义上限（元）。`1 手名义 > 它` 的品种被排除（粒度太粗）。
                   默认 40 万 = equity_base 20万 × notional_cap 2.0。

    返回
    ----
    {
      "legs":       最终 5 腿（按流动性降序）,
      "ranked":     参与排序的全部品种（成交额降序）,
      "turnover":   {key: 日均成交额(元)},
      "corr":       相关性矩阵 {(a,b): rho},
      "max_rho_used": 入选腿两两之间的最大 |ρ|（质量指标，越小越分散）,
      "max_rho_pair": (a, b, rho) —— 最贴边的那一对，必须报出来,
      "rejected":   [(key, 冲突对象, rho)] —— 因相关性/黑名单被拒,
      "dropped_nospec":   缺核验规格被排除的,
      "dropped_nodata":   成交额算不出的（数据不足）,
      "dropped_toolarge": [(key, 1手名义, 倍数)] —— 粒度太粗（1 手就超 notional_cap）,
    }
    """
    spec_of = specs or {}
    turnover: dict = {}
    dropped_nodata: list = []
    dropped_nospec: list = []
    dropped_toolarge: list = []

    for k, d in (daily or {}).items():
        if not d:
            continue
        sp = spec_of.get(k) or {}
        mult = sp.get("mult")
        if require_spec and not mult:
            dropped_nospec.append(k)
            continue
        # ① 粒度闸门：1 手就超 notional_cap ⇒ 这套资金量做不了这个品种
        ml = min_lot_notional(d, mult)
        if ml is not None and notional_cap and ml > notional_cap:
            dropped_toolarge.append((k, ml, ml / notional_cap))
            continue
        # ② 流动性闸门：算不出日均成交额 ⇒ 数据不足，跳过（不拿 0 顶替）
        t = turnover_20d(d, mult, window=vol_window)
        if t is None:
            dropped_nodata.append(k)
            continue
        turnover[k] = t

    ranked = [k for k, _ in sorted(turnover.items(), key=lambda kv: kv[1], reverse=True)]
    corr = corr_matrix(daily, ranked, window=corr_window)
    g = greedy_legs(ranked, corr, n_legs=n_legs, max_rho=max_rho)

    legs = g["legs"]
    a, b, r = max_rho_pair(legs, corr)
    mx = abs(r) if r is not None else None

    return {
        "legs": legs,
        "ranked": ranked,
        "turnover": turnover,
        "corr": corr,
        "max_rho_used": mx,
        "max_rho_pair": (a, b, r),
        "rejected": g["rejected"],
        "dropped_nospec": sorted(dropped_nospec),
        "dropped_nodata": sorted(dropped_nodata),
        "dropped_toolarge": sorted(dropped_toolarge, key=lambda x: -x[1]),
    }


def vol_factor(legs: list, corr: dict, weights: Optional[dict] = None,
               normalize: bool = True) -> Optional[float]:
    """等权（或指定权重）组合的**波动因子** √(wᵀΣw) / Σw。

    Σ 用相关矩阵（假设各腿波动率相同）⇒ sqrt(Σ w_i w_j ρ_ij) / Σw。

    ★ normalize=True（默认）必须归一化，否则没法横向比较：
      不除以 n 时 5 腿 ≈ 2.99、6 腿 ≈ 3.0，看着"6 腿更差"其实只是没除法；
      除以 n 后 5 腿 ≈ 0.60 这一量级，才能和四期方案记录的
      「5 腿 0.5348 / 6 腿 0.5063（5→6 只降 5.34%，加腿不值）」直接对照。
    返回 None 表示有系数缺失。
    """
    if not legs:
        return None
    w = {k: float((weights or {}).get(k, 1.0)) for k in legs}
    tot = 0.0
    for a in legs:
        for b in legs:
            r = rho_of(corr, a, b)
            if r is None:
                return None
            tot += w[a] * w[b] * r
    # ★ 必须钳制：完全对冲（ρ=-1 等权两腿）时 wᵀΣw 的理论值是 0，
    #   浮点会算成 -1e-17 ⇒ `** 0.5` 直接返回**复数**（TypeError: '<' not supported
    #   between 'int' and 'complex'）。这里负数一律归 0。
    if tot < 0:
        tot = 0.0
    raw = tot ** 0.5
    if not normalize:
        return raw
    sw = sum(w.values())
    return raw / sw if sw else None


def max_rho_pair(legs: list, corr: dict):
    """入选腿里 |ρ| 最大的那一对 → (a, b, rho)；腿数 <2 或算不出返回 (None, None, None)。

    必须报出来：拍板① 的阈值 0.5 就取自「RB↔JM = 0.508 不能同选」，
    所以每次选完都要看一眼"最贴边的那一对是谁"，确认没有踩线。
    """
    best = (None, None, None)
    for i, a in enumerate(legs or []):
        for b in (legs or [])[i + 1:]:
            r = rho_of(corr, a, b)
            if r is None:
                continue
            if best[2] is None or abs(r) > abs(best[2]):
                best = (a, b, r)
    return best


# ---------------------------------------------------------------------------
# 影子池（A16）：记录"差一点没选上"的，用于事后对比
# ---------------------------------------------------------------------------
def shadow_pool(result: dict, n_top: int = 12) -> dict:
    """影子池 = 流动性前 n_top 里**没进最终腿**的品种，按被拒原因分组。

    用途（A16）：事后拿影子池的表现和正式腿比，验证「相关性封顶」是否真的在帮忙
    —— 如果影子池表现明显更好，说明封顶阈值该重新审视（但**不许**用此结论去调参，
    只用于判断"这条规则有没有伤到我们"）。
    """
    ranked = result.get("ranked") or []
    legs = set(result.get("legs") or [])
    rej = {k: (o, r) for k, o, r in (result.get("rejected") or [])}
    shadow = []
    for k in ranked[:n_top]:
        if k in legs:
            continue
        o, r = rej.get(k, (None, None))
        shadow.append({
            "key": k,
            "turnover": result["turnover"].get(k),
            "reason": ("blacklist" if o == "BLACKLIST"
                       else "rho" if o is not None else "n_legs_overflow"),
            "conflict_with": o,
            "rho": r,
        })
    return {"shadow": shadow, "legs": list(result.get("legs") or [])}


# ---------------------------------------------------------------------------
# 自检（离线：只用工作台本地日线缓存）
# ---------------------------------------------------------------------------
def _selftest() -> int:
    import os
    import sys

    # 向上找工程根（含 config_symbols.py），**不写死层级** —— 写死过就错过（少一级）。
    root = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        if os.path.isfile(os.path.join(root, "config_symbols.py")):
            break
        parent = os.path.dirname(root)
        if parent == root:
            break
        root = parent
    wb = os.path.join(root, "策略", "06_MA13超短线", "工作台")
    for _p in (wb, root, os.path.dirname(os.path.abspath(__file__))):
        if os.path.isdir(_p) and _p not in sys.path:
            sys.path.insert(0, _p)
    try:
        import data_feed as _df
        import universe as _u
        from signal_bridge import spec as _spec, has_spec as _has_spec
    except Exception as e:
        print(f"SKIP: 需要工作台环境（{type(e).__name__}: {e}）")
        print(f"      已尝试加入：{wb}")
        return 0

    daily = _df.build_universe_daily(_u.all_keys(), use_cache=True)
    if not daily:
        print("SKIP: 无日线缓存（先 `python 工作台/data_feed.py --fetch`）")
        return 0

    specs = {}
    for k in daily:
        if _has_spec(k):
            specs[k] = _spec(k)

    r = select_pool(daily, specs)
    notional_cap = DEFAULT_NOTIONAL_CAP
    print("=" * 78)
    print("S2 选品种（成交额降序 + |ρ|≤0.5 贪心加腿）")
    print("=" * 78)
    print(f"有数据 {len(daily)} | 有核验规格 {len(specs)} | "
          f"缺规格排除 {len(r['dropped_nospec'])} | 数据不足 {len(r['dropped_nodata'])}")
    if r["dropped_toolarge"]:
        print(f"\n粒度太粗被排除（1 手名义 > notional_cap {DEFAULT_NOTIONAL_CAP:,.0f}）：")
        for k, ml, ratio in r["dropped_toolarge"]:
            print(f"  {k:<4} 1手={ml:>12,.0f} 元 = {ratio:.2f}× cap")
    print(f"\n成交额前 15（亿元/日）：")
    for k in r["ranked"][:15]:
        print(f"  {k:<4} {r['turnover'][k] / 1e8:>8.2f}")
    print(f"\n最终 {len(r['legs'])} 腿：{', '.join(r['legs'])}")
    pa, pb, pr = r["max_rho_pair"]
    if pr is not None:
        print(f"入选腿两两最大 |ρ| = {abs(pr):.3f}  ← {pa}↔{pb}（阈值 {MAX_RHO}）")
    vf5 = vol_factor(r["legs"], r["corr"])
    print(f"组合波动因子（归一化）= {vf5:.4f}" if vf5 is not None else "波动因子：算不出")

    # 5 → 6 腿的边际分散效果（拍板③ 曾记「5→6 只降 5.34%，加腿不值」）
    # ★ 这里只做**中性报告**，不下"该不该加"的结论。理由有二：
    #   ① 归一化波动因子天然随腿数增加而下降（分母 n 变大 + 分散化效应恒正），
    #      所以"降多少"完全取决于第 6 腿与已有腿的相关性，不是稳定的常数；
    #   ② 拍板③ 的 5.34% 是在**当时的品种池**上算的，本次池子已变（加了粒度闸门），
    #      两者不可直接比 —— 写成"与拍板不一致"等于制造假警报。
    #   真正该看的是 max_rho_pair（最贴边那一对），那才是封顶规则的直接体检。
    r6 = select_pool(daily, specs, n_legs=6, notional_cap=notional_cap)
    vf6 = vol_factor(r6["legs"], r6["corr"])
    if vf5 and vf6:
        drop = (vf5 - vf6) / vf5 * 100
        sixth = r6["legs"][-1] if len(r6["legs"]) > 5 else "无"
        print(f"5→6 腿：{vf5:.4f} → {vf6:.4f}（降 {drop:.2f}%），第 6 腿 = {sixth}")
        print(f"  （仅供参考，不据此决定加腿：波动因子随腿数必然下降，"
              f"拍板③的 5.34% 基于当时池子，与本次不可直接比）")

    print(f"\n因相关性/黑名单被拒（前 8）：")
    if not r["rejected"]:
        print("  （无）")
    for k, o, rho in r["rejected"][:8]:
        if o == "BLACKLIST":
            print(f"  {k:<4} 黑名单（僵尸腿，永不入选）")
        elif rho is None:
            print(f"  {k:<4} 与 {o} 相关性无法计算（保守拒绝）")
        else:
            print(f"  {k:<4} 与 {o} ρ={rho:+.3f}（|ρ| > {MAX_RHO}）")
    sh = shadow_pool(r)
    print(f"\n影子池（前 12 里落选的 {len(sh['shadow'])} 个）：")
    for s in sh["shadow"]:
        print(f"  {s['key']:<4} {s['reason']:<14} "
              f"{('vs ' + str(s['conflict_with'])) if s['conflict_with'] else ''}"
              f"{' ρ=' + format(s['rho'], '+.3f') if s['rho'] is not None else ''}")
    print("=" * 78)
    mx = r["max_rho_used"]
    ok = bool(r["legs"]) and (mx is None or mx <= MAX_RHO)
    # 硬断言：黑名单品种绝不能出现在最终腿里（拍板③）
    assert not (set(r["legs"]) & set(BLACKLIST)), f"黑名单品种混进腿：{set(r['legs']) & set(BLACKLIST)}"
    assert len(r["legs"]) <= N_LEGS, f"腿数超限：{len(r['legs'])}"
    print("S2 自检：" + ("通过 ✅" if ok else "失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
