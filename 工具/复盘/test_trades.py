# -*- coding: utf-8 -*-
"""trades.py 回归断言（2026-10-24 修复：接管条件化 / VOID 消费 / 同秒排序 / 手续费）

跑法（在 工具/复盘 目录下）：
    python test_trades.py
全部通过返回码 0，任一失败返回 1。

设计：不依赖真实台账，全部用合成 flow 注入（monkeypatch T.load_flow）。
"""
from __future__ import annotations

import sys

import pandas as pd

sys.path.insert(0, ".")
import trades as T  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + ("  " + extra if extra else ""))
    if not cond:
        FAILS.append(name)


def make_flow(rows: list[dict]) -> pd.DataFrame:
    """rows: dict(类型, 记录时间, 方向, 手数, 成交价, 备注)"""
    d = pd.DataFrame(rows)
    for c in ("类型", "记录时间", "方向", "手数", "成交价", "备注"):
        if c not in d.columns:
            d[c] = ""
    d["时间"] = pd.to_datetime(d["记录时间"], errors="coerce")
    d["_seq"] = range(len(d))
    return d.sort_values(["时间", "_seq"], kind="mergesort").reset_index(drop=True)


def with_flow(rows: list[dict]):
    d = make_flow(rows)
    orig = T.load_flow
    T.load_flow = lambda _sym, _d=d: _d.copy()
    return orig


# ---------------------------------------------------------------- 用例 1
# 开 → RECOVER(净仓一致) → 平：必须保留真实开仓价
rows = [
    {"类型": "FILL", "记录时间": "2026-01-01 09:00:00", "方向": "空开", "手数": "1", "成交价": "100", "备注": "DCE.m2701"},
    {"类型": "RECOVER", "记录时间": "2026-01-01 10:00:00", "备注": "⚠️ 云端存在持仓 -1 手，本进程原以为空仓 → 已接管并补挂止损≈95（ATR=2.0）"},
    {"类型": "FILL", "记录时间": "2026-01-01 11:00:00", "方向": "空平", "手数": "1", "成交价": "90", "备注": "DCE.m2701"},
]
orig = with_flow(rows)
t = T.load_trades("豆粕")
T.load_flow = orig
check("1a 净仓一致 ⇒ 保留真实开仓价", t.iloc[0]["开仓价"] == 100.0, f"got={t.iloc[0]['开仓价']}")
check("1b 盈亏 == (90-100)×(-1)×1×10 = 100", abs(t.iloc[0]["盈亏"] - 100.0) < 1e-9, f"got={t.iloc[0]['盈亏']}")
check("1c 接管笔 == 0", int((t["开仓价"].isna() & t["平仓价"].notna()).sum()) == 0)

# ---------------------------------------------------------------- 用例 2
# 开 → RECOVER(净仓不一致) → 平：必须仍是 NaN（不许猜）
rows2 = [
    {"类型": "FILL", "记录时间": "2026-01-01 09:00:00", "方向": "空开", "手数": "1", "成交价": "100", "备注": "DCE.m2701"},
    {"类型": "RECOVER", "记录时间": "2026-01-01 10:00:00", "备注": "云端存在持仓 -2 手，本进程原以为空仓 → 已接管"},
    {"类型": "FILL", "记录时间": "2026-01-01 11:00:00", "方向": "空平", "手数": "1", "成交价": "90", "备注": "DCE.m2701"},
]
orig = with_flow(rows2)
t = T.load_trades("豆粕")
T.load_flow = orig
check("2a 净仓不一致 ⇒ 开仓价仍为 NaN", pd.isna(t.iloc[0]["开仓价"]), f"got={t.iloc[0]['开仓价']}")
check("2b 盈亏仍为 NaN", pd.isna(t.iloc[0]["盈亏"]))

# ---------------------------------------------------------------- 用例 3
# VOID 点名的事件必须被剔除
rows3 = [
    {"类型": "FILL", "记录时间": "2026-01-01 09:00:00", "方向": "空开", "手数": "1", "成交价": "100", "备注": "DCE.m2701"},
    {"类型": "VOID", "记录时间": "2026-01-01 09:05:00",
     "备注": "【作废标记】2026-01-01 09:00:00 的 RECOVER/FILL/STOPLOSS 为误判产物，统计与复盘请排除这三行"},
]
orig = with_flow(rows3)
ev = T.load_events("豆粕")
T.load_flow = orig
check("3a VOID 点名的事件被剔除", len(ev) == 0, f"事件数={len(ev)}")

# 仅 HH:MM:SS 的写法也要认（用 VOID 行自己的日期补全）
rows3b = [
    {"类型": "FILL", "记录时间": "2026-01-01 09:00:00", "方向": "空开", "手数": "1", "成交价": "100", "备注": "DCE.m2701"},
    {"类型": "VOID", "记录时间": "2026-01-01 09:05:00", "备注": "【作废标记】09:00:00 的 FILL 为误判产物，请排除"},
]
orig = with_flow(rows3b)
ev = T.load_events("豆粕")
T.load_flow = orig
check("3b 仅写 HH:MM:SS 也能剔除", len(ev) == 0, f"事件数={len(ev)}")
check("3c VOID 解析出 2 个时刻（鸡蛋实测）", len(T._void_stamps(T.load_flow("鸡蛋"))) == 2
      if T.flow_path("鸡蛋").exists() else True)

# ---------------------------------------------------------------- 用例 4
# 同秒「平 + 开」（文件里开在前）：必须按「先平后开」处理，开仓价不得被加权
rows4 = [
    {"类型": "FILL", "记录时间": "2026-01-01 09:00:00", "方向": "多开", "手数": "1", "成交价": "100", "备注": "DCE.m2701"},
    {"类型": "FILL", "记录时间": "2026-01-01 11:00:00", "方向": "空开", "手数": "1", "成交价": "110", "备注": "DCE.m2701"},
    {"类型": "FILL", "记录时间": "2026-01-01 11:00:00", "方向": "多平", "手数": "1", "成交价": "110", "备注": "DCE.m2701"},
]
orig = with_flow(rows4)
t = T.load_trades("豆粕")
T.load_flow = orig
first = t.iloc[0]
check("4a 同秒反手 ⇒ 多单按 100→110 平", abs(first["盈亏"] - 100.0) < 1e-9, f"got={first['盈亏']}")
check("4b 新空单开仓价 == 110（未被加权）", t.iloc[1]["开仓价"] == 110.0, f"got={t.iloc[1]['开仓价']}")

# ---------------------------------------------------------------- 用例 5
# 手续费：白银乘数 15、比例费率 5e-5
rows5 = [
    {"类型": "FILL", "记录时间": "2026-01-01 09:00:00", "方向": "空开", "手数": "2", "成交价": "13084", "备注": "SHFE.ag2612"},
    {"类型": "FILL", "记录时间": "2026-01-01 10:00:00", "方向": "空平", "手数": "2", "成交价": "13095", "备注": "SHFE.ag2612"},
]
orig = with_flow(rows5)
t = T.load_trades("白银")
T.load_flow = orig
exp_fee = (5e-5 * 13084 * 15 + 5e-5 * 13095 * 15) * 2
check("5a 白银乘数 15 ⇒ 毛利 -330", abs(t.iloc[0]["盈亏"] + 330.0) < 1e-6, f"got={t.iloc[0]['盈亏']}")
check("5b 手续费 ≈ 39.27", abs(t.iloc[0]["手续费"] - exp_fee) < 1e-6, f"got={t.iloc[0]['手续费']:.4f} 期望={exp_fee:.4f}")
check("5c 净盈亏 == 毛利 - 手续费", abs(t.iloc[0]["净盈亏"] - (t.iloc[0]["盈亏"] - t.iloc[0]["手续费"])) < 1e-9)

# ---------------------------------------------------------------- 用例 6
# 真实台账四个可完整重建品种的验收锚点（回归基线，改逻辑须同步改这里）
EXPECT = {"乙二醇": 6670.0, "甲醇": -1360.0, "石油沥青": -16890.0, "纯碱": -320.0}
for sym, exp in EXPECT.items():
    if not T.flow_path(sym).exists():
        continue
    tr = T.load_trades(sym)
    got = float(tr["盈亏"].sum())
    check(f"6 {sym} Σ毛利 == {exp:,.0f}", abs(got - exp) < 0.5, f"got={got:,.1f}")
    check(f"6 {sym} 接管笔 == 0", int((tr["开仓价"].isna() & tr["平仓价"].notna()).sum()) == 0)

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项失败: " + " / ".join(FAILS))
    sys.exit(1)
print("✅ 全部通过")
