# -*- coding: utf-8 -*-
"""
拉「指数合约」日线（KQ.i@）—— 用于长持有检验
================================================
为什么不用主连（KQ.m@）：
    tqsdk 期货**不支持复权**（`api.py:667` 明写 adj_type 只对股票/基金有效），
    `KQ.m@` 是主力合约的硬拼接，换月处直接跳到新合约价格。
    实测（`_诊断_换月污染.py`）：26,049 根日线里 129 个换月点、**平均跳空 8.7%**
    （JD 最大 34.4%、LH 34.3%、SA 23.6%），约 3~8 倍日线 ATR。
    hold=12 时 5.8% 的交易跨换月（噪声可控）；**hold=60 时 25.8%、hold=120 时 44.9%**
    ⇒ 长持有用主连跑必然被拼接噪声支配。

指数合约（`KQ.i@`）是所有合约按持仓量加权，**价格连续、无换月跳空**：
    实测 max|ret| M 7.64% / MA 10.61% / JD 6.39%（主连对应 14.5% / 9.3% / 34.4%）。
代价：不可直接交易，收益 ≈ 主力收益（主力权重最大），成本按主力合约的元/手近似。

用法：
    python 拉数据_指数.py            # 全部 12 品种
    python 拉数据_指数.py MA M       # 指定品种
产物：
    数据_指数/{key}_1440min_2016-01-01_2025-12-31.csv
"""
import os
import sys
import time

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tqsdk import TqApi, TqAuth  # noqa: E402
import config_account  # noqa: E402

OUT_DIR = os.path.join(_HERE, "数据_指数")
START = "2016-01-01"
END = "2025-12-31"

# 指数合约代码（交易所前缀 + 品种代码；郑商所大写、其余小写）
CODES = {
    "M":  "KQ.i@DCE.m",
    "MA": "KQ.i@CZCE.MA",
    "RB": "KQ.i@SHFE.rb",
    "V":  "KQ.i@DCE.v",
    "SA": "KQ.i@CZCE.SA",
    "EB": "KQ.i@DCE.eb",
    "JD": "KQ.i@DCE.jd",
    "JM": "KQ.i@DCE.jm",
    "P":  "KQ.i@DCE.p",
    "RU": "KQ.i@SHFE.ru",
    "SR": "KQ.i@CZCE.SR",
    "LH": "KQ.i@DCE.lh",
}


def pull_all(keys):
    os.makedirs(OUT_DIR, exist_ok=True)
    api = TqApi(auth=TqAuth(config_account.USER, config_account.PASSWORD))
    try:
        for i, k in enumerate(keys, 1):
            code = CODES[k]
            t0 = time.time()
            kl = api.get_kline_serial(code, 86400, data_length=8964)
            df = kl.copy()
            df["datetime"] = pd.to_datetime(df["datetime"])
            n0 = len(df)
            df = df[(df["datetime"] >= pd.Timestamp("2005-01-01")) & (df["close"] > 0)].copy()
            df = df[df["datetime"] <= pd.Timestamp(END) + pd.Timedelta(days=1)]
            df = df[["datetime", "open", "high", "low", "close"]].reset_index(drop=True)
            if len(df) == 0:
                print("[跳过] %s(%s) 无有效数据" % (k, code), flush=True)
                continue
            out = os.path.join(OUT_DIR, "%s_1440min_%s_%s.csv" % (k, START, END))
            df.to_csv(out, index=False, encoding="utf-8-sig")
            # 退化检查（O==H==L==C 占比）
            deg = ((df["open"] == df["high"]) & (df["high"] == df["low"]) &
                   (df["low"] == df["close"])).mean() * 100
            print("[%2d/%2d] %-3s %-16s n=%4d  %s ~ %s  退化=%.2f%%  %.1fs" % (
                i, len(keys), k, code, len(df),
                df["datetime"].min().strftime("%Y-%m-%d"),
                df["datetime"].max().strftime("%Y-%m-%d"), deg, time.time() - t0), flush=True)
            if i == 1:
                print("      （首条 3 行预览）")
                print(df.head(3).to_string(index=False))
    finally:
        api.close()


if __name__ == "__main__":
    keys = [x.upper() for x in sys.argv[1:]] or list(CODES.keys())
    pull_all(keys)
