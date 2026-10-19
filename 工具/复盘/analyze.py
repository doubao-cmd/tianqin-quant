# -*- coding: utf-8 -*-
"""
复盘工具 · 台账统计层（阶段 1）
================================================================
职责：**只读台账 CSV**，产出 A2（MAE/MFE）与 A5（滑点）两卡片的原始统计。
边界（交接简报 §3 D5/D6）：
  · 不连网、不 import tqsdk、不 import 任何策略代码（连 config_symbols.py 也不 import）
  · 一切走 CSV 契约：信号/滑点/MAE-MFE 都以台账里**已经记下来**的字段为准，
    绝不在本文件复制第二套信号逻辑（复制 = 将来必然漂移）
  · 纯读盘 ⇒ 断网也能看（简报 §4 铁律：前端永不直连 tqsdk，阶段 1 更是零取数）

数据契约（§5.1，已逐条核对）：
  台账目录 `策略/06_MA13超短线/模拟盘/台账/`
    <品种>_成交流水.csv    编码 utf-8-sig（有 BOM）
      表头：类型,记录时间,K线时间,方向,手数,成交价,参考价,备注
    <品种>_运行心跳.csv    表头：记录时间,K线时间,现价,持仓手数,目标手数
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# 路径 / 常量
# ---------------------------------------------------------------------------
LEDGER_DIR = Path(r"C:\Users\cc\Desktop\天勤量化\策略\06_MA13超短线\模拟盘\台账")
FLOW_SUFFIX = "_成交流水.csv"
HEART_SUFFIX = "_运行心跳.csv"
ENCODING = "utf-8-sig"          # ★ 简报 §6·11：现有 CSV 带 BOM，必须 utf-8-sig

# A2：EXCURSION 备注 `持仓期间  最大浮盈=4960.00 元  最大浮亏=-2080.00 元`
#     group1 = 最大浮盈(MFE)，group2 = 最大浮亏(MAE，本来带负号)
RE_EXCURSION = re.compile(r"最大浮盈=(-?[\d.]+).*?最大浮亏=(-?[\d.]+)")

# A5：FILLX 备注 `多开 4手 决策价=9715.00 云端成交均价=9715.00 滑点≈+0.00元`
#     group1=方向 group2=手数 group3=决策价 group4=云端均价 group5=滑点(元)
#     另一形态：`多平 4手 决策价=9743.00 云端均价未就绪（未读到）` ⇒ 无法取云端价，剔除
RE_FILLX = re.compile(
    r"(多开|空开|多平|空平)\s*(\d+)\s*手\s*决策价=(-?[\d.]+)\s*"
    r"云端成交均价=(-?[\d.]+)\s*滑点≈([+-]?[\d.]+)\s*元"
)
# 只取方向/手数（未就绪行也能取，用于统计被剔除的构成）
RE_FILLX_DIR = re.compile(r"(多开|空开|多平|空平)\s*(\d+)\s*手")

HEARTBEAT_DEAD_HOURS = 24.0      # ★ §5.1：心跳超 24h 未更新 ⇒ 僵尸腿，页面标 DEAD


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def list_flow_files() -> list[Path]:
    """全部 <品种>_成交流水.csv（按文件名排序）。"""
    return sorted(LEDGER_DIR.glob(f"*{FLOW_SUFFIX}"))


def list_heartbeat_files() -> list[Path]:
    return sorted(LEDGER_DIR.glob(f"*{HEART_SUFFIX}"))


def load_ledger() -> pd.DataFrame:
    """读取全部成交流水，纵向拼接；新增 `品种` 列（取自文件名）。

    苯乙烯腿是僵尸腿（心跳停在 2026-10-09），其 csv 仍在盘上 ⇒ 也会被读进来，
    但它在有效区间内产出的 EXCURSION/FILLX 为 0，A2/A5 的数字不受影响。
    """
    frames = []
    for p in list_flow_files():
        sym = p.name.replace(FLOW_SUFFIX, "")
        d = pd.read_csv(p, encoding=ENCODING, dtype=str)
        d["品种"] = sym
        d["_来源文件"] = p.name
        frames.append(d)
    if not frames:
        return pd.DataFrame(
            columns=["类型", "记录时间", "K线时间", "方向", "手数", "成交价", "参考价", "备注", "品种"]
        )
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# A2 · MAE / MFE
# ---------------------------------------------------------------------------
def excursion_rows(df: pd.DataFrame) -> pd.DataFrame:
    """抽出可解析的 EXCURSION 行，返回列含：品种 / 记录时间 / MFE / MAE。

    MFE = 持仓期间最大浮盈（元，≥0 方向）
    MAE = 持仓期间最大浮亏（元，≤0 方向）
    """
    rows = df[df["类型"] == "EXCURSION"].copy()
    ext = rows["备注"].astype("string").str.extract(RE_EXCURSION)
    rows["MFE"] = pd.to_numeric(ext[0], errors="coerce")
    rows["MAE"] = pd.to_numeric(ext[1], errors="coerce")
    return rows.dropna(subset=["MFE", "MAE"]).reset_index(drop=True)


def closes_count(df: pd.DataFrame) -> int:
    """总平仓次数 = 类型 FILL 且 方向 以「平」结尾（实测 105）。这是 A2 覆盖率的**分母**。"""
    fills = df[df["类型"] == "FILL"]
    return int(fills["方向"].astype("string").str.endswith("平", na=False).sum())


def per_symbol_excursion(exc: pd.DataFrame) -> pd.DataFrame:
    """分品种中位数表（按 n 降序）。"""
    g = (
        exc.groupby("品种")
        .agg(n=("MFE", "size"), MFE中位=("MFE", "median"), MAE中位=("MAE", "median"))
        .reset_index()
        .sort_values("n", ascending=False)
    )
    return g


def excursion_stats(exc: pd.DataFrame, n_closes: int) -> dict:
    """A2 汇总：n、覆盖率、分位数、比例。所有比例都带 n（§6·13 纪律）。"""
    n = int(len(exc))
    mfe, mae = exc["MFE"], exc["MAE"]
    return {
        "n": n,
        "n_closes": int(n_closes),
        "coverage": (n / n_closes) if n_closes else float("nan"),
        "mfe_median": float(mfe.median()) if n else float("nan"),
        "mae_median": float(mae.median()) if n else float("nan"),
        "mfe_p25": float(mfe.quantile(0.25)) if n else float("nan"),
        "mfe_p75": float(mfe.quantile(0.75)) if n else float("nan"),
        "mae_p25": float(mae.quantile(0.25)) if n else float("nan"),
        "mae_p75": float(mae.quantile(0.75)) if n else float("nan"),
        "mfe_mean": float(mfe.mean()) if n else float("nan"),
        "mae_mean": float(mae.mean()) if n else float("nan"),
        "mfe_pos_n": int((mfe > 0).sum()),
        "mfe_pos_share": float((mfe > 0).mean()) if n else float("nan"),
        "mfe_ge1000_n": int((mfe >= 1000).sum()),
        "mae_le2000_n": int((mae <= -2000).sum()),
    }


# ---------------------------------------------------------------------------
# A5 · 滑点
# ---------------------------------------------------------------------------
def fillx_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """拆 FILLX 为 (可解析, 被剔除)。

    可解析：备注里含 `云端成交均价=… 滑点≈…元`
    被剔除：备注里是 `云端均价未就绪（未读到）`（§6·14，实测 79 条，几乎全是平仓腿）
    滑点符号语义（已实测核对）：**> 0 表示不利于我们的成本**（多开买贵 / 空开卖便宜）。
    """
    rows = df[df["类型"] == "FILLX"].copy()
    ext = rows["备注"].astype("string").str.extract(RE_FILLX)
    ok = ext[3].notna()

    good = rows[ok].copy()
    good["方向"] = ext.loc[ok, 0].to_numpy()
    good["手数"] = pd.to_numeric(ext.loc[ok, 1], errors="coerce").to_numpy()
    good["决策价"] = pd.to_numeric(ext.loc[ok, 2], errors="coerce").to_numpy()
    good["云端均价"] = pd.to_numeric(ext.loc[ok, 3], errors="coerce").to_numpy()
    good["滑点"] = pd.to_numeric(ext.loc[ok, 4], errors="coerce").to_numpy()
    good = good.dropna(subset=["滑点"]).reset_index(drop=True)

    bad = rows[~ok].copy()
    d = bad["备注"].astype("string").str.extract(RE_FILLX_DIR)
    bad["方向"] = d[0]
    bad["手数"] = pd.to_numeric(d[1], errors="coerce")
    bad = bad.reset_index(drop=True)
    return good, bad


def slippage_stats(good: pd.DataFrame, bad: pd.DataFrame) -> dict:
    """A5 汇总：可解析 n、剔除 n、中位数/均值、不利占比（带 n）。"""
    n = int(len(good))
    s = good["滑点"] if n else pd.Series(dtype="float64")
    return {
        "n_total": int(len(good) + len(bad)),
        "n_ok": n,
        "n_excluded": int(len(bad)),
        "median": float(s.median()) if n else float("nan"),
        "mean": float(s.mean()) if n else float("nan"),
        "p25": float(s.quantile(0.25)) if n else float("nan"),
        "p75": float(s.quantile(0.75)) if n else float("nan"),
        "pos_n": int((s > 0).sum()),
        "neg_n": int((s < 0).sum()),
        "zero_n": int((s == 0).sum()),
        "adverse_share": float((s > 0).mean()) if n else float("nan"),
    }


def per_symbol_slippage(good: pd.DataFrame) -> pd.DataFrame:
    g = (
        good.groupby("品种")
        .agg(n=("滑点", "size"), 滑点中位=("滑点", "median"), 滑点均值=("滑点", "mean"))
        .reset_index()
        .sort_values("n", ascending=False)
    )
    return g


def per_direction_slippage(good: pd.DataFrame) -> pd.DataFrame:
    g = (
        good.groupby("方向")
        .agg(n=("滑点", "size"), 滑点中位=("滑点", "median"), 滑点均值=("滑点", "mean"))
        .reset_index()
    )
    return g


# ---------------------------------------------------------------------------
# 心跳新鲜度（§5.1 僵尸腿标注）
# ---------------------------------------------------------------------------
def heartbeat_status(now: pd.Timestamp | None = None) -> pd.DataFrame:
    """每个品种最后一条心跳的 `记录时间` 与距今小时数；> 24h ⇒ DEAD。"""
    now = now or pd.Timestamp.now()
    out = []
    for p in list_heartbeat_files():
        sym = p.name.replace(HEART_SUFFIX, "")
        try:
            d = pd.read_csv(p, encoding=ENCODING, dtype=str)
        except Exception as e:  # 单文件坏了不影响其它品种
            out.append({"品种": sym, "最后心跳": None, "距今小时": float("nan"),
                        "状态": "READ-ERR", "备注": str(e)})
            continue
        if d.empty or "记录时间" not in d.columns:
            out.append({"品种": sym, "最后心跳": None, "距今小时": float("nan"),
                        "状态": "EMPTY", "备注": ""})
            continue
        last = pd.to_datetime(d["记录时间"].iloc[-1], errors="coerce")
        age_h = (now - last).total_seconds() / 3600.0 if pd.notna(last) else float("nan")
        dead = pd.notna(age_h) and age_h > HEARTBEAT_DEAD_HOURS
        out.append({
            "品种": sym,
            "最后心跳": last,
            "距今小时": round(age_h, 2) if pd.notna(age_h) else float("nan"),
            "状态": "DEAD" if dead else "OK",
            "备注": "心跳超 24h 未更新（僵尸腿）" if dead else "",
        })
    return pd.DataFrame(out)


# ---------------------------------------------------------------------------
# 自检入口：核对简报 §5.4 的基准值
# ---------------------------------------------------------------------------
def _selftest() -> None:
    df = load_ledger()
    exc = excursion_rows(df)
    good, bad = fillx_rows(df)
    n_closes = closes_count(df)
    st = excursion_stats(exc, n_closes)

    print("=" * 60)
    print(f"成交流水文件数: {len(list_flow_files())}   总行数: {len(df)}")
    print("-" * 60)
    print(f"[A2] EXCURSION 可解析条数 : {st['n']}   (基准 82)")
    print(f"     总平仓次数(分母)     : {st['n_closes']}   (基准 105)")
    print(f"     覆盖率               : {st['coverage'] * 100:.1f}%   (基准 78%)")
    print(f"     MFE 中位 / P25 / P75 : {st['mfe_median']:+.0f} / {st['mfe_p25']:+.0f} / {st['mfe_p75']:+.0f}"
          f"   (基准 +670 / 160 / 1750)")
    print(f"     MAE 中位 / P25 / P75 : {st['mae_median']:+.0f} / {st['mae_p25']:+.0f} / {st['mae_p75']:+.0f}"
          f"   (基准 -760 / -1680 / -490)")
    print(f"     MFE>0 占比           : {st['mfe_pos_n']}/{st['n']} = {st['mfe_pos_share'] * 100:.0f}%   (基准 87%)")
    print(f"     MFE>=1000 笔数       : {st['mfe_ge1000_n']}   (基准 34)")
    print("-" * 60)
    print("[A2] 分品种中位数（基准 豆粕+400/-600｜棕榈油+640/-920｜白糖+280/-490｜焦煤+1200/-1920｜鸡蛋+1920/-920）：")
    print(per_symbol_excursion(exc).to_string(index=False))
    print("-" * 60)
    sl = slippage_stats(good, bad)
    print(f"[A5] FILLX 总条数        : {sl['n_total']}   (基准 193)")
    print(f"     可解析(含滑点)       : {sl['n_ok']}")
    print(f"     剔除(云端均价未就绪) : {sl['n_excluded']}   (实测 79)")
    print(f"     滑点 中位/均值/P25/P75: {sl['median']:+.2f} / {sl['mean']:+.2f} / {sl['p25']:+.2f} / {sl['p75']:+.2f}")
    print(f"     不利(>0)/有利(<0)/零  : {sl['pos_n']} / {sl['neg_n']} / {sl['zero_n']}")
    print("-" * 60)
    print("[A5] 分品种：")
    print(per_symbol_slippage(good).to_string(index=False))
    print("[A5] 分方向：")
    print(per_direction_slippage(good).to_string(index=False))
    print("-" * 60)
    print("[心跳] 新鲜度：")
    print(heartbeat_status().to_string(index=False))
    print("=" * 60)


if __name__ == "__main__":
    _selftest()
