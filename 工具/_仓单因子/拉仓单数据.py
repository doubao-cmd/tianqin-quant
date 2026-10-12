# -*- coding: utf-8 -*-
"""
拉取「注册仓单」历史数据（仅 SHFE + CZCE，因 DCE 被瑞数反爬封锁）。

用途：仓单变化率因子的「信号频率预算」（离线研究，不进交易链路）。
- 只取项目 12 品种池中交易所可得的 5 个：MA / RB / SA / RU / SR
- 区间 2018-11-01 ~ 2022-01-31（覆盖 IS 2019-2021 + 20 日预热 + 缓冲）
- 直接调用 akshare 内部按日抓取器（get_shfe_receipt_2 / get_czce_receipt_3），
  绕过 get_receipt 里会抛错的 DCE 分支
- 多线程加速（akshare 走交易所网页，非 tqsdk，线程安全可用）

落盘：工具/_仓单因子/数据/仓单_SHFE_CZCE_2018_2022.csv
"""
import datetime
import os
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

warnings.filterwarnings("ignore")

from akshare.futures import cons
from akshare.futures.receipt import get_shfe_receipt_2, get_czce_receipt_3

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "数据")
os.makedirs(OUT_DIR, exist_ok=True)
OUT_CSV = os.path.join(OUT_DIR, "仓单_SHFE_CZCE_2018_2022.csv")

START = datetime.date(2018, 11, 1)
END = datetime.date(2022, 1, 31)

SHFE_VARS = ["RB", "RU"]
CZCE_VARS = ["MA", "SA", "SR"]

calendar = cons.get_calendar()
_lock = threading.Lock()
_rows = []
_done = [0]
_err = []


def _fetch_one(d: datetime.date):
    """抓单日两所数据，返回 DataFrame 列表；失败抛异常由上层记录。"""
    ds = d.strftime("%Y%m%d")
    out = []
    t1 = get_shfe_receipt_2(d, SHFE_VARS)
    if t1 is not None and len(t1):
        out.append(t1)
    t2 = get_czce_receipt_3(d, CZCE_VARS)
    if t2 is not None and len(t2):
        out.append(t2)
    return out


def main():
    days = []
    d = START
    while d <= END:
        if d.strftime("%Y%m%d") in calendar:
            days.append(d)
        d += datetime.timedelta(days=1)

    print(f"交易日 {len(days)} 天，区间 {START} ~ {END}，线程 6", flush=True)

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_fetch_one, d): d for d in days}
        for fut in as_completed(futs):
            d = futs[fut]
            try:
                for t in fut.result():
                    with _lock:
                        _rows.append(t)
            except Exception as e:
                _err.append((d.isoformat(), f"{type(e).__name__}: {str(e)[:80]}"))
            with _lock:
                _done[0] += 1
                if _done[0] % 50 == 0:
                    print(f"  进度 {_done[0]}/{len(days)}  已收 {len(_rows)} 段  失败 {len(_err)}",
                          flush=True)

    if not _rows:
        print("!! 无任何数据，检查网络/接口", flush=True)
        return

    df = pd.concat(_rows, ignore_index=True)
    # 统一 date 为 YYYY-MM-DD 字符串，排序去重
    df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d").dt.strftime("%Y-%m-%d")
    df = df.drop_duplicates(subset=["var", "date"]).sort_values(["var", "date"]).reset_index(drop=True)
    df = df[["var", "date", "receipt", "receipt_chg"]]
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    print(f"\n落盘 {OUT_CSV}", flush=True)
    print(f"行数 {len(df)}  品种 {sorted(df['var'].unique())}", flush=True)
    print(f"日期 {df['date'].min()} ~ {df['date'].max()}", flush=True)
    print("\n每品种覆盖：", flush=True)
    print(df.groupby("var")["date"].agg(["min", "max", "count"]).to_string(), flush=True)
    if _err:
        print(f"\n失败 {len(_err)} 天（前 10）：", flush=True)
        for x in _err[:10]:
            print("  ", x, flush=True)


if __name__ == "__main__":
    main()
