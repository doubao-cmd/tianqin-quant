# -*- coding: utf-8 -*-
"""
tick 数据查询与体检 —— 用 DuckDB 直接 SQL 查 Parquet（不用导入、不用起服务）
================================================================================
这就是选 Parquet + DuckDB 的意义：**文件即数据库**。
  SELECT ... FROM read_parquet('tick/**/*.parquet', hive_partitioning=1)
DuckDB 会把 `date=.../sym=...` 目录自动识别成两个分区列。

用法：python tick查询.py [--dir tick] [--show 8]
"""
import os
import glob
import argparse

import duckdb

_HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(_HERE, "tick"))
    ap.add_argument("--show", type=int, default=8)
    a = ap.parse_args()

    if not os.path.isdir(a.dir):
        raise SystemExit(f"没有录制目录：{a.dir}（先跑 tick录制.py）")
    files = glob.glob(os.path.join(a.dir, "**", "*.parquet"), recursive=True)
    if not files:
        raise SystemExit(f"{a.dir} 下没有 parquet 文件")
    total_mb = sum(os.path.getsize(f) for f in files) / 1e6

    # ⚠️ 路径含中文/空格时用参数化列表最稳（避免 glob 里的特殊字符被吞）
    src = "read_parquet(?, hive_partitioning=1)"
    con = duckdb.connect()

    print("=" * 78)
    print(f"tick 数据体检  ｜ 文件 {len(files)} 个 ｜ 磁盘占用 {total_mb:.2f} MB")
    print("=" * 78)

    q = f"""
    SELECT count(*)                              AS 总条数,
           count(DISTINCT sym)                   AS 品种数,
           count(DISTINCT date)                  AS 天数,
           min(ts)                               AS 起,
           max(ts)                               AS 止,
           round(count(*)/nullif(count(DISTINCT sym),0), 0) AS 每品种条数
    FROM {src}
    """
    df = con.execute(q, [files]).df()
    print(df.to_string(index=False))
    print()

    print("── 逐品种 ──")
    df2 = con.execute(f"""
        SELECT sym AS 品种,
               count(*) AS 条数,
               min(ts)  AS 起,
               max(ts)  AS 止,
               round(avg(ask1 - bid1), 3) AS 平均买卖价差,
               round(100.0*avg(CASE WHEN abs(last) > 1e100 OR last IS NULL THEN 1 ELSE 0 END), 3) AS "last为空的占比%",
               round(100.0*avg(CASE WHEN bid1 IS NULL THEN 1 ELSE 0 END), 3)  AS "无买一占比%"
        FROM {src}
        GROUP BY sym ORDER BY 条数 DESC
    """, [files]).df()
    print(df2.to_string(index=False))
    print()

    print(f"── 样例（前 {a.show} 行）──")
    df3 = con.execute(f"""
        SELECT ts, sym, last, bid1, bidv1, ask1, askv1, volume, open_interest
        FROM {src} ORDER BY ts LIMIT {int(a.show)}
    """, [files]).df()
    print(df3.to_string(index=False))
    print()

    print("── 每分钟快照数（看 CTP 推送节奏是否 ≈2 笔/秒 = 120 笔/分钟）──")
    df4 = con.execute(f"""
        SELECT sym AS 品种,
               round(count(*)*1.0 / nullif(count(DISTINCT date_trunc('minute', ts)),0), 1) AS 每分钟笔数
        FROM {src}
        GROUP BY sym ORDER BY 每分钟笔数 DESC
    """, [files]).df()
    print(df4.to_string(index=False))
    print()
    print("⚠️ 解读：CTP 是**快照**（≈500ms 一笔），不是逐笔成交。")
    print("   每分钟 ~120 笔属正常；远低于此说明有丢包/断线，远高于此要看是否重复订阅。")

    con.close()


if __name__ == "__main__":
    main()
