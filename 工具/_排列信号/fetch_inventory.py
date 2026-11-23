# -*- coding: utf-8 -*-
"""
补「库存」信息源：东财期货库存（futures_inventory_em）。
覆盖大商所等仓单拿不到的品种；但历史只有约 67 个交易日（2026-08 起），
⇒ **只用于前瞻/台账，不能回测**。

输出 _inventory/<SYM>.csv  列：dt,inventory,change
"""
import os
import time
import warnings

import pandas as pd

warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "_inventory")

# 东财中文名 -> 我们的品种代码
NAME2SYM = {
    "沪金": "AU", "沪银": "AG", "沪铜": "CU", "沪铝": "AL", "沪锌": "ZN",
    "镍": "NI", "沪铅": "PB", "锡": "SN",
    "螺纹钢": "RB", "热卷": "HC", "铁矿石": "I", "焦炭": "J", "焦煤": "JM",
    "豆粕": "M", "豆油": "Y", "棕榈": "P", "玉米": "C", "玉米淀粉": "CS",
    "鸡蛋": "JD", "生猪": "LH", "塑料": "L", "聚丙烯": "PP", "PVC": "V",
    "乙二醇": "EG", "苯乙烯": "EB", "甲醇": "MA", "PTA": "TA",
    "玻璃": "FG", "纯碱": "SA", "尿素": "UR", "白糖": "SR", "郑棉": "CF",
    "菜粕": "RM", "菜油": "OI", "苹果": "AP", "红枣": "CJ",
    "硅铁": "SF", "锰硅": "SM", "燃油": "FU", "沥青": "BU",
    "橡胶": "RU", "纸浆": "SP", "不锈钢": "SS", "20号胶": "NR",
    "氧化铝": "AO", "烧碱": "SH", "对二甲苯": "PX", "瓶片": "PR", "多晶硅": "PS",
}


def main():
    import akshare as ak

    os.makedirs(OUT, exist_ok=True)
    ok, fail = [], []
    for name, sym in NAME2SYM.items():
        try:
            d = ak.futures_inventory_em(symbol=name)
        except Exception:
            fail.append(name)
            continue
        if d is None or len(d) == 0:
            fail.append(name)
            continue
        d = d.rename(columns={"日期": "dt", "库存": "inventory", "增减": "change"})
        d["dt"] = pd.to_datetime(d["dt"]).dt.strftime("%Y-%m-%d")
        d = d.drop_duplicates("dt").sort_values("dt")
        d.to_csv(os.path.join(OUT, f"{sym}.csv"), index=False, encoding="utf-8")
        nz = int((d["inventory"] > 0).sum())
        ok.append((sym, name, len(d), nz, d.dt.iloc[0], d.dt.iloc[-1]))
        time.sleep(0.2)

    print(f"成功 {len(ok)} / 失败 {len(fail)}")
    print(f"{'代码':5s}{'东财名':8s}{'行数':>5s}{'非零':>5s}  {'起':10s}{'止':10s}")
    for r in ok:
        print(f"{r[0]:5s}{r[1]:8s}{r[2]:5d}{r[3]:5d}  {r[4]}  {r[5]}")
    if fail:
        print("\n失败：", "、".join(fail))


if __name__ == "__main__":
    main()
