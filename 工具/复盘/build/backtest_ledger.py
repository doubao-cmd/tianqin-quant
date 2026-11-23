# -*- coding: utf-8 -*-
"""回测成交源（R1，2026-11-07）：把 06_MA13超短线 的**逐笔成交 txt** 读成
与 `trades.load_trades()` **同列名** 的 DataFrame，供 build_data 直接替换成交源。

为什么要它
----------
复盘终端原先把「实盘台账」当成交流水。台账只有 2026-10-08 之后、8 个品种、
百余笔，样本太薄；而策略 06 已经跑过 2026 全年探针窗（每品种几百笔）。
把成交源换成回测成交，图上买卖点覆盖全年，才谈得上"练盘感"。

数据来源（只读，不重跑回测）
--------------------------
`回测结果/06_MA13超短线/{中文名}/{中文名}_MA13超短线_5min_探针窗2026-01-01_*.txt`
  第 1 行 `# 笔数=.. 胜率=..`（跳过）
  第 2 行 表头：方向,开仓时间,平仓时间,开仓价,平仓价,手数,盈亏(元),每手(点),离场原因
  ★ `盈亏(元)` 是**已扣双边成本**的净额（实测 PVC：-13 点 × 8 手 × 5 = -520 毛利，
    文件写 -544，差 24 = 8 手 × 1.5 元 × 2 边）。所以还原毛利要 **净额 + 手续费**。

口径对齐（与台账源逐字段一致）
------------------------------
  盈亏   = 净额 + 手续费        （毛利，未扣费 —— 与 trades.py 的 `盈亏` 同义）
  手续费 = (开腿 + 平腿) × 手数  （`data.comm_per_hand`，比例品种按各自成交价现算）
  净盈亏 = 文件里的原始净额
  止损/ATR/MFE/MAE = NaN（回测 txt 不记）→ 交给 build_data 用 5min K 线补算
  配对   = "回测"

⚠️ 覆盖缺口（不是 bug，是回测没跑）：乙二醇 EG / 燃料油 FU / 石油沥青 BU
   在 回测结果/06_MA13超短线/ 下**没有 2026 探针窗产物**。这三个品种 `load_trades`
   返回空表，build_data 会把它们标成「无回测成交」，不静默伪造。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

# 单独运行（python build/backtest_ledger.py）时 build/ 不在 sys.path 里；被 build_data 导入时已就位
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import data  # noqa: E402

# 项目根 = 工具/复盘/build → 上三级
BT_DIR = Path(__file__).resolve().parent.parent.parent.parent / "回测结果" / "06_MA13超短线"
ENCODING = "utf-8-sig"

# 只认「探针窗 2026-01-01 起」的产物；`_stopbar_close` / `_stoptick` 是压测变体，不能混进来
RE_NAME = re.compile(r"_探针窗2026-01-01_(\d{4}-\d{2}-\d{2})\.txt$")

COLUMNS = ["品种", "方向", "手数", "开仓时间", "开仓价", "平仓时间", "平仓价",
           "止损", "计划止损", "ATR", "MFE", "MAE", "盈亏", "手续费", "净盈亏",
           "原因", "配对", "持仓分钟"]


def find_file(cn: str) -> Path | None:
    """找该品种**结束日期最晚**的 2026 探针窗 txt（同品种常有 09-26 / 10-31 / 11-06 多份）。"""
    d = BT_DIR / cn
    if not d.is_dir():
        return None
    best, best_end = None, ""
    for p in d.glob(f"{cn}_MA13超短线_5min_探针窗2026-01-01_*.txt"):
        m = RE_NAME.search(p.name)
        if not m:
            continue
        if m.group(1) > best_end:
            best, best_end = p, m.group(1)
    return best


def has_2026(cn: str) -> bool:
    return find_file(cn) is not None


def load_trades(cn: str) -> pd.DataFrame:
    """读回测成交 → 与 trades.load_trades 同列。无产物时返回带列的空表。"""
    p = find_file(cn)
    if p is None:
        return pd.DataFrame(columns=COLUMNS)

    raw = pd.read_csv(p, encoding=ENCODING, skiprows=1, comment="#")
    raw.columns = [str(c).strip() for c in raw.columns]
    if raw.empty:
        return pd.DataFrame(columns=COLUMNS)

    key = data.CN2KEY.get(cn, cn.upper())
    mult = float(data.MULT.get(key, 1))

    df = pd.DataFrame()
    df["品种"] = [cn] * len(raw)
    df["方向"] = raw["方向"].astype(str).str.strip()
    df["手数"] = pd.to_numeric(raw["手数"], errors="coerce").fillna(0).astype(int)
    df["开仓时间"] = pd.to_datetime(raw["开仓时间"], errors="coerce")
    df["平仓时间"] = pd.to_datetime(raw["平仓时间"], errors="coerce")
    df["开仓价"] = pd.to_numeric(raw["开仓价"], errors="coerce")
    df["平仓价"] = pd.to_numeric(raw["平仓价"], errors="coerce")
    net = pd.to_numeric(raw["盈亏(元)"], errors="coerce")

    # 双边手续费：开腿按开仓价、平腿按平仓价（比例品种必须现算，不能拿常数近似）
    fee = []
    for ep, xp, q in zip(df["开仓价"], df["平仓价"], df["手数"]):
        if pd.isna(xp):
            fee.append(float("nan"))
            continue
        try:
            e0 = float(ep) if pd.notna(ep) else float(xp)
            fee.append((data.comm_per_hand(key, e0) + data.comm_per_hand(key, float(xp))) * float(q))
        except Exception:
            fee.append(float("nan"))
    df["手续费"] = fee
    df["净盈亏"] = net
    # 毛利 = 净额 + 手续费（文件里的盈亏是扣费后的，见模块头注释）
    df["盈亏"] = [n + f if (pd.notna(n) and pd.notna(f)) else n for n, f in zip(net, df["手续费"])]

    df["止损"] = float("nan")
    df["计划止损"] = float("nan")
    df["ATR"] = float("nan")
    df["MFE"] = float("nan")
    df["MAE"] = float("nan")
    df["原因"] = raw["离场原因"].astype(str).str.strip()
    df["配对"] = "回测"
    hold = (df["平仓时间"] - df["开仓时间"]).dt.total_seconds() / 60.0
    df["持仓分钟"] = hold

    # 合理性检查：文件自带「每手(点)」= 净额 / 手数 / 乘数，用它反查解析是否错列
    if "每手(点)" in raw.columns:
        chk = pd.to_numeric(raw["每手(点)"], errors="coerce")
        calc = df["净盈亏"] / df["手数"].replace(0, pd.NA) / mult
        bad = (chk - calc).abs() > 0.35          # 文件保留 1 位小数
        if bad.fillna(False).any():
            n = int(bad.fillna(False).sum())
            print(f"    ⚠ {cn}：{n}/{len(df)} 笔「每手(点)」与净额反算不符"
                  f"（多乘数/列错位），已照文件净额为准", flush=True)

    return df[COLUMNS].reset_index(drop=True)


def load_events(cn: str) -> pd.DataFrame:
    """由成交反推开/平事件（图上买卖点）。列名与 trades.load_events 一致。"""
    t = load_trades(cn)
    rows = []
    for _, r in t.iterrows():
        rows.append({"时间": r["开仓时间"], "类型": "开", "方向": r["方向"], "手数": r["手数"], "价格": r["开仓价"]})
        rows.append({"时间": r["平仓时间"], "类型": "平", "方向": r["方向"], "手数": r["手数"], "价格": r["平仓价"]})
    if not rows:
        return pd.DataFrame(columns=["时间", "类型", "方向", "手数", "价格"])
    return pd.DataFrame(rows).sort_values("时间").reset_index(drop=True)


def summary() -> list[tuple[str, str, int]]:
    """自检用：各品种命中的文件与笔数。"""
    out = []
    for key, (cn, _code) in data.RECENT_SPECS.items():
        p = find_file(cn)
        n = len(load_trades(cn)) if p else 0
        out.append((key, p.name if p else "—", n))
    return out


if __name__ == "__main__":
    print(f"回测成交目录：{BT_DIR}")
    tot = 0
    for key, fn, n in summary():
        tot += n
        print(f"  {key:3} {n:5} 笔   {fn}")
    print(f"合计 {tot} 笔")
