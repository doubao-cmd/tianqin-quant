# -*- coding: utf-8 -*-
"""
工具/_np_data/run_daily.py —— 每日一条命令抓全部四类（D6/D7 日常入口）

为什么要这个文件，而不是在 bat/shell 里串四条命令：
  1. `_last_update.json` 每次 `store.run()` 都会整体重写 —— 串行跑四个进程，最后只剩
     **最后一个源**的新鲜度，前三个等于没记录。
  2. 失败要**按源聚在一个文件**里才看得出"今天缺哪一类"；四个进程各写各的也可以
     （已经改成按源分文件），但一次跑完能拿到一张完整的结果表。

单源重跑仍然用各自的 `fetch_*.py YYYYMMDD` —— 它们只清自己那个失败标记，不会互相抹掉。

用法：
    python run_daily.py 20261016
    python run_daily.py            # 默认今天
退出码：0 全成功 / 1 至少一个源失败（并留下 _FAILED_{date}_{source}.txt）
"""
from __future__ import annotations

import sys
from datetime import date as _date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import store            # noqa: E402
import fetch_receipt    # noqa: E402
import fetch_rank       # noqa: E402
import fetch_spot       # noqa: E402
import fetch_inventory  # noqa: E402

SOURCES = {
    "receipt":   fetch_receipt.fetch,     # SHFE(+INE) / CZCE / GFEX
    "rank":      fetch_rank.fetch,        # 同上三所，合约级
    "spot":      fetch_spot.fetch,        # 100ppi 现货价 + 第三方基差
    "inventory": fetch_inventory.fetch,   # 99qh 库存（SHFE+CZCE+DCE，无 GFEX）
}


def main(argv: list) -> int:
    d = argv[1] if len(argv) > 1 else _date.today().strftime("%Y%m%d")
    print(f"[每日抓取] date={d}  源 {len(SOURCES)} 组：{','.join(SOURCES)}")
    return store.run(SOURCES, d)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
