# -*- coding: utf-8 -*-
"""
test_pool.py —— 离线单测：S2 选腿（成交额 / 相关性封顶 / 贪心 / 黑名单）
====================================================================
被测：`工具/复盘/exec4/pool.py`（纯函数，无网络、无 tqsdk、无状态）

锁死的契约（每条都对应一次真实踩坑或一条四期拍板）：

1. **流动性用成交额，不用手数**
   手数跨品种不可比（JM 1 手 = 60 吨、AU 1 手价值最高但手数很少）。
   实测同一批数据下两套榜单有 7/20 个品种不同，且正是「SA 靠手数混进池子」的原因。

2. **相关性用日收益率，不用价格**
   两个同趋势不同量级的品种，价格相关性虚高。

3. **|ρ| ≤ 0.5 封顶**（拍板①，0.5 出自 RB↔JM = 0.508）
   新腿必须与**所有**已选腿都不冲突，任一超阈值即拒绝。

4. **贪心不枚举**（拍板①，禁 C(50,5) = 211 万种）
   先到先得：按成交额降序，能加就加，加满 n_legs 就停。

5. **EB 黑名单**（拍板③，僵尸腿：无台账、心跳停 2026-10-09）永不入选。

6. **算不出的相关性按保守处理**（默认拒绝）
   算不出 ≠ 不相关。

7. **只用已收盘、且只用窗口内数据**
   改窗口之前的历史不该影响结果（否则说明偷看了更早的数据或没用窗口）。

跑法：`python test_pool.py`（.venv_viz 即可，无需 tqsdk）
"""
from __future__ import annotations

import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "exec4")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pool


# ---------------------------------------------------------------- 造数据
# ★ 坑：不能用「恒定日收益」造序列。日收益恒定 ⇒ 收益率序列零方差 ⇒
#   皮尔逊相关系数**无定义**（会返回 None），测试会以一个假原因"通过"。
#   必须用有波动的序列。
_PAT = [0.010, -0.006, 0.013, -0.004, 0.008, -0.011, 0.005, 0.002, -0.009, 0.007]


def _series(n, base=100.0, pat=None, scale=1.0, sign=1):
    """按确定性波动模式构造收盘序列（n 根）。

    pat   : 日收益（收益率的"形状"决定相关性）
    scale : 幅度缩放（不改变相关系数 —— 相关性对幅度不敏感）
    sign  : 1=原样，-1=逐日反向（用于构造 ρ≈-1）
    """
    pat = pat or _PAT
    out = [base]
    for i in range(n - 1):
        r = pat[i % len(pat)] * scale * sign
        out.append(out[-1] * (1.0 + r))
    return out


def _mk(close, vol=1000.0):
    return {"close": list(close), "volume": [vol] * len(close)}


def _alt_series(n, base=100.0):
    """与 _PAT 正交的波动模式（用于造"低相关"的另一条序列）。"""
    pat = [-0.004, 0.011, -0.007, -0.009, 0.012, 0.003, -0.010, 0.006, 0.001, -0.005]
    return _series(n, base=base, pat=pat)


def _rand_series(n, seed, base=100.0):
    """带种子的伪随机序列：不同 seed 之间相关性≈0（造"互不相关的一批品种"用）。"""
    import random
    rng = random.Random(seed)
    out = [base]
    for _ in range(n - 1):
        out.append(out[-1] * (1.0 + rng.uniform(-0.02, 0.02)))
    return out


# ---------------------------------------------------------------- 1. 成交额
def test_turnover_uses_multiplier():
    """同样的手数与价格，乘数不同 ⇒ 成交额必须不同（手数口径会算出同一个数）。"""
    d = _mk(_series(30, base=100.0), vol=100.0)
    t10 = pool.turnover_20d(d, mult=10)
    t60 = pool.turnover_20d(d, mult=60)
    assert t10 is not None and t60 is not None
    assert abs(t60 / t10 - 6.0) < 1e-9, f"成交额未按乘数放大：{t10} vs {t60}"
    # 期望值：100 手 × 均价 × 乘数（close 非恒定，故只验比例与量级）
    assert t10 > 0
    print("  PASS  test_turnover_uses_multiplier")


def test_turnover_insufficient_data_returns_none():
    """数据不足必须返回 None（让调用方跳过），不能拿 0 顶替去排序。"""
    d = _mk(_series(5), vol=100.0)
    assert pool.turnover_20d(d, mult=10) is None
    assert pool.turnover_20d(_mk([]), mult=10) is None
    assert pool.turnover_20d(_mk(_series(30)), mult=0) is None
    print("  PASS  test_turnover_insufficient_data_returns_none")


# ---------------------------------------------------------------- 2. 相关性
def test_corr_uses_returns_not_prices():
    """同一收益形状、不同量级 ⇒ ρ=1（相关性对幅度不敏感）；反向 ⇒ ρ≈-1。"""
    a = _series(30, base=100.0)
    b = _series(30, base=10000.0, scale=3.0)      # 同样的形状，放大 3 倍 + 量级差 100 倍
    daily = {"A": _mk(a), "B": _mk(b)}
    corr = pool.corr_matrix(daily, ["A", "B"])
    r = pool.rho_of(corr, "A", "B")
    assert r is not None and abs(r - 1.0) < 1e-9, f"同步序列应 ρ=1，实际 {r}"
    # 反向序列
    c = _series(30, base=100.0, sign=-1)
    d2 = {"A": _mk(a), "C": _mk(c)}
    corr2 = pool.corr_matrix(d2, ["A", "C"])
    r2 = pool.rho_of(corr2, "A", "C")
    assert r2 is not None and r2 < -0.99, f"反向序列应 ρ≈-1，实际 {r2}"
    print("  PASS  test_corr_uses_returns_not_prices")


def test_corr_symmetric_and_self():
    """矩阵必须对称；同品种恒 1.0。"""
    daily = {"A": _mk(_series(30)), "B": _mk(_alt_series(30))}
    corr = pool.corr_matrix(daily, ["A", "B"])
    ab = pool.rho_of(corr, "A", "B")
    ba = pool.rho_of(corr, "B", "A")
    assert ab is not None and ab == ba, "相关矩阵不对称"
    assert pool.rho_of(corr, "A", "A") == 1.0
    print("  PASS  test_corr_symmetric_and_self")


# ---------------------------------------------------------------- 3+4. 贪心
def test_greedy_rejects_high_corr():
    """高相关的新腿必须被拒，且拒绝理由要指向冲突对象与该 ρ。"""
    a = _series(30)
    b = _series(30, scale=2.0)      # 同一形态 ⇒ ρ≈1
    c = _alt_series(30)             # 另一形态 ⇒ 低相关，应可入选
    daily = {"A": _mk(a), "B": _mk(b), "C": _mk(c)}
    corr = pool.corr_matrix(daily, ["A", "B", "C"])
    assert (pool.rho_of(corr, "A", "B") or 0) > 0.99, "测试数据构造失败：A/B 应高度相关"
    out = pool.greedy_legs(["A", "B", "C"], corr, n_legs=2, max_rho=0.5)
    assert out["legs"][0] == "A"
    assert "B" not in out["legs"], f"高相关腿 B 未被拒：{out}"
    assert "C" in out["legs"], f"低相关腿 C 应入选：{out}"
    rej = {k: (o, r) for k, o, r in out["rejected"]}
    assert "B" in rej and rej["B"][0] == "A", f"拒绝理由不对：{rej}"
    assert rej["B"][1] is not None and abs(rej["B"][1]) > 0.5
    print("  PASS  test_greedy_rejects_high_corr")


def test_greedy_respects_n_legs():
    """加满 n_legs 就停，绝不多加。"""
    keys = list("ABCDEF")
    daily = {k: _mk(_rand_series(40, seed=i * 7 + 1, base=100.0 + i))
             for i, k in enumerate(keys)}
    corr = pool.corr_matrix(daily, keys)
    out = pool.greedy_legs(keys, corr, n_legs=3, max_rho=0.5)
    assert len(out["legs"]) == 3, f"应正好 3 腿，实际 {out['legs']}（{out['rejected']}）"
    print("  PASS  test_greedy_respects_n_legs")


def test_greedy_is_order_driven():
    """贪心是「先到先得」：换顺序结果就该不同（证明没做全局优化/枚举）。"""
    daily = {"A": _mk(_series(30)),
             "B": _mk(_series(30, scale=2.0)),      # 与 A 高相关
             "C": _mk(_alt_series(30))}             # 与 A 低相关
    corr = pool.corr_matrix(daily, ["A", "B", "C"])
    r1 = pool.greedy_legs(["A", "B", "C"], corr, n_legs=2, max_rho=0.5)["legs"]
    r2 = pool.greedy_legs(["C", "A", "B"], corr, n_legs=2, max_rho=0.5)["legs"]
    assert r1 != r2, f"顺序变了结果没变 ⇒ 可能偷偷做了全局最优：{r1} vs {r2}"
    print("  PASS  test_greedy_is_order_driven")


# ---------------------------------------------------------------- 5. 黑名单
def test_blacklist_never_selected():
    """EB 必须在最前面就被挡掉，即使成交额第一。"""
    daily = {"EB": _mk(_series(30), vol=1e9),
             "A": _mk(_series(30)), "B": _mk(_alt_series(30))}
    specs = {"EB": {"mult": 5}, "A": {"mult": 10}, "B": {"mult": 10}}
    r = pool.select_pool(daily, specs, n_legs=3)
    assert "EB" not in r["legs"], f"黑名单 EB 混进腿：{r['legs']}"
    reasons = {k: o for k, o, _ in r["rejected"]}
    assert reasons.get("EB") == "BLACKLIST", f"EB 未被标黑名单：{r['rejected']}"
    print("  PASS  test_blacklist_never_selected")


# ---------------------------------------------------------------- 6. 缺失保守
def test_missing_rho_is_blocked_by_default():
    """相关系数算不出 ⇒ 默认拒绝（算不出 ≠ 不相关）。"""
    out = pool.greedy_legs(["A", "B"], {}, n_legs=2, max_rho=0.5)
    assert out["legs"] == ["A"], f"缺少 ρ 时应保守只留首腿，实际 {out['legs']}"
    assert out["rejected"] and out["rejected"][0][2] is None
    # 显式放宽时允许加入
    out2 = pool.greedy_legs(["A", "B"], {}, n_legs=2, max_rho=0.5,
                            treat_none_as_block=False)
    assert out2["legs"] == ["A", "B"]
    print("  PASS  test_missing_rho_is_blocked_by_default")


# ---------------------------------------------------------------- 7. 窗口
def test_only_window_bars_matter():
    """只改「窗口之前」的历史，结果必须不变（证明没用到窗口外的数据）。"""
    keys = ["A", "B", "C"]
    vals = {"A": _series(80, base=100.0),
            "B": _series(80, base=101.0, scale=2.0),
            "C": _alt_series(80, base=102.0)}
    d1 = {k: _mk(v) for k, v in vals.items()}
    # 把前 50 根改得面目全非（窗口是后 20 根，前 50 根在窗口之外）
    d2 = {}
    for k, v in vals.items():
        v2 = [x * 3.0 for x in v[:50]] + v[50:]
        d2[k] = _mk(v2)
    specs = {k: {"mult": 10} for k in keys}
    r1 = pool.select_pool(d1, specs, n_legs=2, vol_window=20, corr_window=20)
    r2 = pool.select_pool(d2, specs, n_legs=2, vol_window=20, corr_window=20)
    assert r1["legs"] == r2["legs"], f"改动窗口外历史却改变了结果：{r1['legs']} vs {r2['legs']}"
    assert r1["ranked"] == r2["ranked"], "排序被窗口外数据影响"
    print("  PASS  test_only_window_bars_matter")


# ---------------------------------------------------------------- 波动因子
def test_vol_factor_normalized():
    """归一化后必须落在 (0,1]；不归一化则 ≥1。"""
    daily = {"A": _mk(_rand_series(40, seed=1)),
             "B": _mk(_rand_series(40, seed=99))}
    corr = pool.corr_matrix(daily, ["A", "B"])
    vf = pool.vol_factor(["A", "B"], corr)
    raw = pool.vol_factor(["A", "B"], corr, normalize=False)
    assert vf is not None and 0 < vf <= 1.0, f"归一化波动因子应在 (0,1]，实际 {vf}"
    assert raw is not None and raw >= 1.0, f"未归一化应 ≥1，实际 {raw}"
    assert abs(raw / 2 - vf) < 1e-12, "归一化必须正好除以腿数"
    print(f"  PASS  test_vol_factor_normalized（归一化 {vf:.4f} / 原始 {raw:.4f}）")


def test_vol_factor_zero_when_hedged():
    """完全负相关（ρ≈-1）等权两腿 ⇒ 组合波动因子 = 0，且**不能返回复数**。

    这是浮点真实坑：wᵀΣw 理论为 0、实际算成 -1e-17 ⇒ `** 0.5` 返回复数，
    下游 `f"{x:.4f}"` 直接 TypeError。
    """
    daily = {"A": _mk(_series(30)), "B": _mk(_series(30, sign=-1))}
    corr = pool.corr_matrix(daily, ["A", "B"])
    vf = pool.vol_factor(["A", "B"], corr)
    assert vf is not None and not isinstance(vf, complex), f"返回了复数：{vf}"
    assert abs(vf) < 1e-6, f"ρ=-1 的两腿波动因子应 ≈0，实际 {vf}"
    print(f"  PASS  test_vol_factor_zero_when_hedged（{vf:.2e}）")


# ---------------------------------------------------------------- 粒度闸门
def test_min_lot_gate_excludes_too_coarse():
    """1 手名义 > notional_cap 的品种必须被排除，并给出倍数。

    这是 AU/CU 事件的固化：AU 1 手 = 90 万 = 2.25× cap，
    但 sizer 的 `max(hand_min, 名义手数)` 兜底会让它硬开 1 手 ⇒
    单腿就吃掉 0.91×账户权益。判据来自策略自己的风控定义，不引入新数值。
    """
    # A：1 手 = 100 × 10 = 1000 元，远小于 cap（400000）⇒ 正常入选
    # B：1 手 = 1000000 × 1000 = 1e9 元 ⇒ 远超 cap，应被排除
    daily = {"A": _mk(_rand_series(40, seed=1), vol=100.0),
             "B": _mk([1_000_000.0] * 40, vol=100.0)}
    specs = {"A": {"mult": 10}, "B": {"mult": 1000}}
    r = pool.select_pool(daily, specs, n_legs=5)
    assert "A" in r["legs"], f"正常品种未入选：{r['legs']}"
    assert "B" not in r["legs"], f"粒度太粗的 B 未被排除：{r['legs']}"
    drop = {k: (ml, ratio) for k, ml, ratio in r["dropped_toolarge"]}
    assert "B" in drop, f"B 未进 dropped_toolarge：{r['dropped_toolarge']}"
    assert drop["B"][0] == 1e9 and drop["B"][1] > 1.0
    print(f"  PASS  test_min_lot_gate_excludes_too_coarse（B 1手={drop['B'][0]:.0f} "
          f"= {drop['B'][1]:.0f}× cap）")


def test_min_lot_gate_boundary():
    """正好等于 cap 的应保留（判据是 >，不是 >=）。"""
    cap = pool.DEFAULT_NOTIONAL_CAP
    # 价格 400 × 乘数 1000 = 400000 = cap ⇒ 恰好不超，应保留
    daily = {"A": _mk([cap / 1000.0] * 40, vol=100.0)}
    specs = {"A": {"mult": 1000}}
    r = pool.select_pool(daily, specs, n_legs=1)
    assert "A" in r["legs"], f"恰好等于 cap 的品种被误排除：{r['dropped_toolarge']}"
    # 稍微超一点 ⇒ 排除
    daily2 = {"A": _mk([cap / 1000.0 * 1.01] * 40, vol=100.0)}
    r2 = pool.select_pool(daily2, specs, n_legs=1)
    assert "A" not in r2["legs"], "略超 cap 的品种未被排除"
    print("  PASS  test_min_lot_gate_boundary")


def main() -> int:
    print("=" * 72)
    print("S2 选腿离线单测（成交额 / 相关性 / 贪心 / 黑名单 / 窗口）")
    print("=" * 72)
    test_turnover_uses_multiplier()
    test_turnover_insufficient_data_returns_none()
    test_corr_uses_returns_not_prices()
    test_corr_symmetric_and_self()
    test_greedy_rejects_high_corr()
    test_greedy_respects_n_legs()
    test_greedy_is_order_driven()
    test_blacklist_never_selected()
    test_missing_rho_is_blocked_by_default()
    test_only_window_bars_matter()
    test_vol_factor_normalized()
    test_vol_factor_zero_when_hedged()
    test_min_lot_gate_excludes_too_coarse()
    test_min_lot_gate_boundary()
    print("=" * 72)
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
