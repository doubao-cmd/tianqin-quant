# -*- coding: utf-8 -*-
"""force_include / seed 单测（2026-10-27）。

背景：用户要求「保持五腿、每周都是五腿、四腿选、固定鸡蛋一腿」。
      S2（`工具/复盘/exec4/pool.py`）原本没有固定腿机制，JD 成交额排 19/27，
      n_legs=5 时永远轮不到它 ⇒ 增加 `seed` 参数。

用法：
    python 工具/test_选腿_force_include.py
全部断言通过则退出码 0。

★ 不依赖实时行情：主体用例用**构造的 ranked/corr**，最后一例用本地日线缓存做集成检查。
"""
import sys
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isfile(
        os.path.join(_ROOT, "config_symbols.py")):
    _ROOT = os.path.dirname(_ROOT)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "工具", "复盘", "exec4"))

import pool  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}  {detail}")


# ---------------------------------------------------------------- 构造用例
RANKED = ["EG", "MA", "FU", "BU", "P", "M", "JD"]


def _mk_corr(keys, default=0.10, overrides=None):
    """构造**完整**相关矩阵：未列出的对默认 0.10。

    ★ 必须完整：`greedy_legs(treat_none_as_block=True)` 会把「算不出 ρ」当冲突，
      缺对会让后面的腿全被挡掉，测出来的腿数不是 5（第一次写这测试就栽在这）。
    """
    c = {}
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            v = default
            if overrides and (a, b) in overrides:
                v = overrides[(a, b)]
            elif overrides and (b, a) in overrides:
                v = overrides[(b, a)]
            c[(a, b)] = v
            c[(b, a)] = v
    return c


CORR = _mk_corr(RANKED, default=0.10, overrides={
    ("EG", "MA"): 0.63,     # 强相关，不能同选
    ("FU", "BU"): 0.65,     # 强相关，不能同选
    ("EG", "P"): 0.27,
    ("EG", "M"): 0.13,
    ("JD", "EG"): -0.02,
    ("JD", "MA"): -0.10,
    ("JD", "P"): -0.07,
    ("JD", "FU"): 0.05,
    ("JD", "M"): 0.03,
})

print("=" * 70)
print("A. 不含 seed（旧行为基线）")
print("=" * 70)
base = pool.greedy_legs(RANKED, CORR, n_legs=5)
print("  腿:", base["legs"])
check("A1 旧行为：5 腿 = EG,FU,P,M,JD（MA/BU 被 ρ 拒）",
      base["legs"] == ["EG", "FU", "P", "M", "JD"], str(base["legs"]))
check("A2 不含 JD 的候选里 JD 靠排名也能进（本构造 JD 排末尾仍补进）",
      "JD" in base["legs"])
check("A3 不强相关同选",
      not ({"EG", "MA"} <= set(base["legs"])) and not ({"FU", "BU"} <= set(base["legs"])))

print()
print("=" * 70)
print("B. seed=['JD'] —— 鸡蛋固定占 1 席")
print("=" * 70)
r = pool.greedy_legs(RANKED, CORR, n_legs=5, seed=["JD"])
print("  腿:", r["legs"])
check("B1 JD 在池", "JD" in r["legs"])
check("B2 腿数恒为 5", len(r["legs"]) == 5, str(len(r["legs"])))
check("B3 JD 只出现一次", r["legs"].count("JD") == 1)
check("B4 JD 作为种子排在首位（占名额顺序）", r["legs"][0] == "JD", str(r["legs"]))
check("B5 种子不破坏 ρ 约束", not ({"EG", "MA"} <= set(r["legs"])))

print()
print("=" * 70)
print("C. 边界：重复 / 不在 ranked / 超员")
print("=" * 70)
r2 = pool.greedy_legs(RANKED, CORR, n_legs=5, seed=["JD", "JD"])
check("C1 重复种子自动去重", r2["legs"].count("JD") == 1, str(r2["legs"]))
check("C2 去重后与单种子结果一致", r2["legs"] == r["legs"])

r3 = pool.greedy_legs(RANKED, CORR, n_legs=5, seed=["XX"])
check("C3 种子不在 ranked → 不抛异常且腿数仍 5", len(r3["legs"]) == 5, str(r3["legs"]))
check("C4 不在 ranked 的种子被记入 rejected",
      ("XX", "SEED_NOT_IN_RANKED", None) in r3["rejected"], str(r3["rejected"]))
check("C5 落空种子不影响其余选腿（等同基线）", r3["legs"] == base["legs"], str(r3["legs"]))

r4 = pool.greedy_legs(RANKED[:3], CORR, n_legs=2, seed=["EG", "MA", "FU"])
check("C6 种子数 > n_legs 时截断到 n_legs", len(r4["legs"]) == 2, str(r4["legs"]))

r5 = pool.greedy_legs(RANKED, CORR, n_legs=5, seed=[])
r6 = pool.greedy_legs(RANKED, CORR, n_legs=5)
check("C7 seed=[] 与不传 seed 逐位一致（回归）", r5["legs"] == r6["legs"] == base["legs"])

print()
print("=" * 70)
print("D. 集成：本地日线缓存跑真实 select_pool")
print("=" * 70)
try:
    import json

    WB = os.path.join(_ROOT, "策略", "06_MA13超短线", "工作台")
    if WB not in sys.path:
        sys.path.insert(0, WB)
    import data_feed as D
    import universe as U
    import config_symbols as CS

    cfg = json.load(open(os.path.join(WB, "workbench.json"), encoding="utf-8"))
    sc = cfg["selection"]
    exc = {str(x).upper() for x in sc["exclude"]}
    daily = D.build_universe_daily(U.all_keys(), use_cache=True, fetch=False)
    daily = {k: v for k, v in daily.items() if k.upper() not in exc}
    specs = {k: {"mult": float(CS.SYMBOLS[k.upper()][2])}
             for k in daily if k.upper() in CS.SYMBOLS}
    n = int(sc["ret_top_n"])

    no_seed = pool.select_pool(daily, specs, n_legs=n, vol_window=int(sc["vol_window_days"]))
    with_seed = pool.select_pool(daily, specs, n_legs=n, vol_window=int(sc["vol_window_days"]),
                                 seed=["JD"])
    print("  n_legs =", n, "| 剔除后参与排序 =", len(daily), "个")
    print("  无 seed :", no_seed["legs"], "| JD 在池:", "JD" in no_seed["legs"])
    print("  有 seed :", with_seed["legs"], "| max|ρ| =",
          None if with_seed["max_rho_used"] is None else round(with_seed["max_rho_used"], 3))
    check("D1 有 seed 时 JD 必在池", "JD" in with_seed["legs"], str(with_seed["legs"]))
    check("D2 有 seed 时腿数 = n_legs", len(with_seed["legs"]) == n, str(len(with_seed["legs"])))
    check("D3 有 seed 时 max|ρ| ≤ 0.5",
          with_seed["max_rho_used"] is None or abs(with_seed["max_rho_used"]) <= pool.MAX_RHO)
    check("D4 无 seed 时不含 JD（证明光靠配置固定不住）",
          "JD" not in no_seed["legs"], str(no_seed["legs"]))
except Exception as e:  # 缓存缺失时不算失败，只提示
    print(f"  [SKIP] 集成用例跳过：{type(e).__name__}: {e}")

print()
print("=" * 70)
print(f"结果：PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    print("失败项：", FAIL)
    sys.exit(1)
print("全部通过 ✅")
