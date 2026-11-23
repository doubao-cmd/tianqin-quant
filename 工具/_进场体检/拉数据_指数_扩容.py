# -*- coding: utf-8 -*-
r"""
拉「指数合约」日线 —— 扩容脚本（12 → 40 品种）
=================================================
本脚本**独立于** `拉数据_指数.py`（11-04 产出原 12 个文件的历史脚本，不动它）。
口径与现有 12 个文件逐字一致：

  合约      KQ.i@<EX>/<code>（指数合约，非主连 KQ.m@）
  code 大小写  由 config_symbols.SYMBOLS 派生（CZCE 大写，其余小写）
  周期      86400 秒（日线）
  长度      data_length=8964
  时间窗    datetime >= 2016-01-01 且 <= 2025-12-31 23:59:59
  有效行    再剔 close <= 0
  列        只存 datetime,open,high,low,close（无 volume）
  文件名    {KEY}_1440min_2016-01-01_2025-12-31.csv
  编码      utf-8-sig
  排序      datetime 升序，index 不写入

安全设计：
  1) 默认**跳过已存在的文件**（保护现有 12 个，绝不覆盖）
  2) 单一 TqApi 连接批量订阅（不每品种开连接，避免撞连接限流）
  3) 每品种打印 key / code / 根数 / 首日 / 末日 / 退化率 / 耗时
  4) 退化率（O==H==L==C 占比）> 0.5% 显著报警

用法：
    python 拉数据_指数_扩容.py            # 拉全部缺失的 28 个
    python 拉数据_指数_扩容.py Y C A      # 只拉指定品种
    python 拉数据_指数_扩容.py --force    # 覆盖已存在（默认关闭，误用会毁掉基准数据）
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
import config_symbols as cs  # noqa: E402

OUT_DIR = os.path.join(_HERE, "数据_指数")
START = "2016-01-01"
END = "2025-12-31"
T_LO = pd.Timestamp("2016-01-01 00:00:00")
T_HI = pd.Timestamp("2025-12-31 23:59:59")
DATA_LENGTH = 8964
DEG_ALARM = 0.5  # %

# 待补的 28 个品种（任务书 §2.1）
TARGETS = [
    # DCE
    "Y", "C", "A", "J", "I", "EG", "PP", "L", "LG",
    # SHFE
    "HC", "CU", "AL", "ZN", "PB", "NI", "SN", "AU", "AG", "FU", "BU", "SP",
    # CZCE
    "CF", "RM", "OI", "TA", "FG", "SM", "SF",
]


def code_of(k):
    """由 config_symbols 派生指数合约代码：KQ.m@ → KQ.i@（不手打）。"""
    return cs.SYMBOLS[k][1].replace("KQ.m@", "KQ.i@")


def out_path(k):
    return os.path.join(OUT_DIR, "%s_1440min_%s_%s.csv" % (k, START, END))


def deg_ratio(df):
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    return float(((o == h) & (h == l) & (l == c)).mean() * 100.0)


def wait_ready(api, kl, timeout=90.0):
    """等首包数据到位（末行 datetime > 0）。返回是否就绪。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            last = float(kl["datetime"].iloc[-1])
        except Exception:
            last = 0.0
        if last == last and last > 0:      # 非 NaN 且 > 0
            return True
        try:
            api.wait_update(deadline=time.time() + 5.0)
        except Exception:
            pass
    return False


def pull(keys, force=False):
    os.makedirs(OUT_DIR, exist_ok=True)
    todo = []
    for k in keys:
        k = k.upper()
        if k not in cs.SYMBOLS:
            print("[跳过] %s 不在 config_symbols.SYMBOLS" % k, flush=True)
            continue
        p = out_path(k)
        if os.path.exists(p) and not force:
            print("[跳过] %-3s 已存在 %s" % (k, os.path.basename(p)), flush=True)
            continue
        todo.append(k)

    if not todo:
        print("没有需要拉的品种。", flush=True)
        return 0

    print("待拉 %d 个品种：" % len(todo), flush=True)
    for k in todo:
        print("    %-3s -> %s" % (k, code_of(k)), flush=True)

    api = TqApi(auth=TqAuth(config_account.USER, config_account.PASSWORD))
    stats, bad = [], []
    try:
        # ---- 单一连接批量订阅
        serials = {}
        for k in todo:
            code = code_of(k)
            try:
                serials[k] = api.get_kline_serial(code, 86400, data_length=DATA_LENGTH)
            except Exception as e:
                print("[失败] %-3s %-16s 订阅异常: %s" % (k, code, e), flush=True)
                bad.append((k, code, "subscribe: %s" % e))
        print("已批量订阅 %d 个序列，开始逐品种取数 ...\n" % len(serials), flush=True)

        for i, k in enumerate(todo, 1):
            if k not in serials:
                continue
            code = code_of(k)
            t0 = time.time()
            kl = serials[k]
            if not wait_ready(api, kl):
                print("[失败] %-3s %-16s datetime 末值一直为 0 / 等待超时，跳过" % (k, code), flush=True)
                bad.append((k, code, "datetime timeout"))
                continue
            try:
                df = kl.copy()
                df["datetime"] = pd.to_datetime(df["datetime"])
                n0 = len(df)
                df = df[df["close"] > 0]
                df = df[(df["datetime"] >= T_LO) & (df["datetime"] <= T_HI)]
                df = df[["datetime", "open", "high", "low", "close"]]
                df = df.sort_values("datetime").reset_index(drop=True)
                if len(df) == 0:
                    print("[失败] %-3s %-16s 时间窗内无有效行（原 %d 行）" % (k, code, n0), flush=True)
                    bad.append((k, code, "empty after filter"))
                    continue
                df.to_csv(out_path(k), index=False, encoding="utf-8-sig")
                deg = deg_ratio(df)
                dur = time.time() - t0
                print("[%2d/%2d] %-3s %-16s 根数=%4d  首日=%s  末日=%s  退化=%.2f%%  %.1fs" % (
                    i, len(todo), k, code, len(df),
                    df["datetime"].iloc[0].strftime("%Y-%m-%d %H:%M:%S"),
                    df["datetime"].iloc[-1].strftime("%Y-%m-%d %H:%M:%S"),
                    deg, dur), flush=True)
                stats.append((k, code, len(df), str(df["datetime"].iloc[0]),
                              str(df["datetime"].iloc[-1]), deg))
                if deg > DEG_ALARM:
                    print("    " + "!" * 70, flush=True)
                    print("    !! 退化率报警：%s = %.2f%% > %.1f%%（指数合约数据异常，需单独说明）"
                          % (k, deg, DEG_ALARM), flush=True)
                    print("    " + "!" * 70, flush=True)
                if len(df) < 200:
                    print("    ?? 提示：%s 仅 %d 根（若为晚上市属正常，非晚上市需报告）" % (k, len(df)), flush=True)
            except Exception as e:
                print("[失败] %-3s %-16s 处理异常: %r" % (k, code, e), flush=True)
                bad.append((k, code, "process: %r" % e))
    finally:
        api.close()

    print("\n================ 汇总 ================")
    print("成功 %d / 待拉 %d" % (len(stats), len(todo)))
    for s in stats:
        print("  %-3s %-16s n=%-4d %s ~ %s  退化=%.2f%%" % (s[0], s[1], s[2], s[3], s[4], s[5]))
    if bad:
        print("失败 %d：" % len(bad))
        for k, code, why in bad:
            print("  %-3s %-16s %s" % (k, code, why))
    return len(bad)


if __name__ == "__main__":
    args = sys.argv[1:]
    force = "--force" in args
    keys = [a for a in args if not a.startswith("--")] or list(TARGETS)
    sys.exit(0 if pull(keys, force=force) == 0 else 1)
