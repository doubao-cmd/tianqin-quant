# -*- coding: utf-8 -*-
"""
工具/_np_data/check_receipt_vs_csv.py —— 仓单口径的**外部交叉核对**（任务书「与官网/独立源抽样核对 ≥10 条」）

为什么要有这个文件
===============================================================================
自己写的聚合逻辑，自己跑自检，是**同一套逻辑验同一套逻辑**。
真正抓住错误的是「另一条独立采集路径拿到的同一批数字」。

现有独立源：`工具/_仓单因子/数据/仓单_SHFE_CZCE_2018_2022.csv`
（5 个品种 × 2018-11 ~ 2022-01，由旧仓单因子工作另行采集，与 `_np_data` 无关）。

★ 这个文件在 2026-10-20 第一次跑就抓出一个 2× 错误：
  `czce_receipt` 的 MA（甲醇）**恒为真值的 2 倍**（2018-11-01 CSV=14 我们=28；
  2021-07-01 CSV=1667 我们=3334；2026-10-16 库里 13166、交易所总计 6583）。
  原因见 `fetch_receipt._czce_total` 的注释：MA 的 DataFrame 末尾有一行
  仓库编号=NaN、数量=总计值，被「剔除小计/总计后求明细」的旧口径当成明细加了进去。
  ⇒ 这就是为什么"交叉核对"必须是独立源，而不是自检。

两种用法
===============================================================================
    $PY check_receipt_vs_csv.py                # 用库里已入库的日期比对（不联网，最快）
    $PY check_receipt_vs_csv.py --live 6       # 现场抓 6 个日期比对（联网，用于回填前验证）
    $PY check_receipt_vs_csv.py --all-db       # 库里所有与 CSV 重叠的日期全比（慢些，最严）

退出码：0 全部一致 / 1 有不一致或无重叠日期可比
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import pandas as pd          # noqa: E402
import pit                   # noqa: E402

CSV_PATH = _HERE.parent / "_仓单因子" / "数据" / "仓单_SHFE_CZCE_2018_2022.csv"

# CSV 的 var → np_data 的 (source, symbol)
# CSV 只覆盖 SHFE + CZCE 两所；同代码在两所不重复，所以直接映射即可。
SRC_OF = {
    "RB": "shfe_receipt", "CU": "shfe_receipt", "AL": "shfe_receipt",
    "ZN": "shfe_receipt", "AU": "shfe_receipt", "AG": "shfe_receipt",
    "RU": "shfe_receipt", "BU": "shfe_receipt", "FU": "shfe_receipt",
    "HC": "shfe_receipt", "SP": "shfe_receipt", "NI": "shfe_receipt",
    "SN": "shfe_receipt", "PB": "shfe_receipt", "WR": "shfe_receipt",
    "MA": "czce_receipt", "TA": "czce_receipt", "SR": "czce_receipt",
    "CF": "czce_receipt", "RM": "czce_receipt", "OI": "czce_receipt",
    "FG": "czce_receipt", "SA": "czce_receipt", "UR": "czce_receipt",
    "SM": "czce_receipt", "SF": "czce_receipt", "AP": "czce_receipt",
    "CJ": "czce_receipt", "CY": "czce_receipt", "PF": "czce_receipt",
    "RS": "czce_receipt", "WH": "czce_receipt", "ZC": "czce_receipt",
}


def load_csv() -> pd.DataFrame:
    if not CSV_PATH.exists():
        raise SystemExit(f"独立源 CSV 不存在：{CSV_PATH}")
    df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
    df["date"] = df["date"].astype(str)
    df["var"] = df["var"].astype(str).str.upper()
    return df


def from_db(df: pd.DataFrame, all_db: bool, db_path=None) -> tuple:
    """库里已有的日期 vs CSV。返回 (比对条数, 不一致明细, 覆盖日期数)。"""
    conn = pit.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT source, symbol, period_end, value FROM np_data "
            "WHERE metric='receipt'").fetchall()
    finally:
        conn.close()
    db = {(r[0], r[1], str(r[2]).replace("-", "")[:8]): r[3] for r in rows}

    csv_dates = sorted(df["date"].unique())
    overlap = [d for d in csv_dates if any(k[2] == d.replace("-", "") for k in db)]
    if not all_db:
        overlap = overlap[:6]
    if not overlap:
        return 0, [], 0

    bad, n = [], 0
    for d in overlap:
        sub = df[df["date"] == d]
        for _, r in sub.iterrows():
            src = SRC_OF.get(r["var"])
            if not src:
                continue
            k = (src, r["var"], d.replace("-", ""))
            if k not in db:
                continue
            n += 1
            if abs(float(db[k]) - float(r["receipt"])) > 1e-6:
                bad.append((d, r["var"], float(r["receipt"]), float(db[k])))
    return n, bad, len(overlap)


def from_live(df: pd.DataFrame, ndays: int) -> tuple:
    """现场抓 n 个日期 vs CSV（联网）。"""
    import warnings
    warnings.filterwarnings("ignore")
    import store
    store.SAVE_RAW_ENABLED = False
    import fetch_receipt

    csv_dates = sorted(df["date"].unique())
    # 均匀抽样，避免只挑到某一段
    step = max(1, len(csv_dates) // max(1, ndays))
    sample = csv_dates[::step][:ndays]

    bad, n = [], 0
    for d in sample:
        got = {r["symbol"]: r["value"] for r in fetch_receipt.fetch(d.replace("-", ""))}
        sub = df[df["date"] == d]
        for _, r in sub.iterrows():
            src = SRC_OF.get(r["var"])
            if not src or src not in ("shfe_receipt", "czce_receipt"):
                continue
            if r["var"] not in got:
                continue
            n += 1
            if abs(float(got[r["var"]]) - float(r["receipt"])) > 1e-6:
                bad.append((d, r["var"], float(r["receipt"]), float(got[r["var"]])))
    return n, bad, len(sample)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="仓单口径外部交叉核对（独立源 CSV）")
    ap.add_argument("--live", type=int, default=0, help="现场抓 N 个日期比对（联网）")
    ap.add_argument("--all-db", action="store_true", help="库里所有重叠日期全比（默认 6 个）")
    ap.add_argument("--db", default=None)
    a = ap.parse_args(argv[1:])

    df = load_csv()
    print("=" * 78)
    print(f"独立源 {CSV_PATH.name}：{len(df)} 行 | 品种 {df['var'].nunique()} | "
          f"日期 {df['date'].min()} ~ {df['date'].max()}")
    print("=" * 78)

    if a.live:
        n, bad, nd = from_live(df, a.live)
        mode = f"现场抓取 {nd} 个日期"
    else:
        n, bad, nd = from_db(df, a.all_db, a.db)
        mode = f"库内 {nd} 个重叠日期"

    if n == 0:
        print(f"★ {mode}：**没有任何可比对的记录**（库还没回填到 CSV 覆盖的区间？用 --live 试）")
        return 1

    print(f"{mode}：比对 {n} 条")
    for d, v, want, got in bad[:20]:
        print(f"  ★ 不一致 {d} {v}: 独立源={want:.0f}  我们={got:.0f}")
    print("-" * 78)
    if bad:
        print(f"不一致 {len(bad)}/{n} 条 ⇒ 口径有问题，不许当成「差异可接受」（退出码 1）")
        return 1
    print(f"全部一致 {n}/{n} ✅（≥10 条即可满足任务书『抽样核对』，本次 {n} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
