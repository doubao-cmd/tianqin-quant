# -*- coding: utf-8 -*-
"""全市场「一手风险」扫描 —— 回答「小资金到底开得起哪些品种的 1 手」。

★ 不消耗试验次数（K）：本脚本只算**资金容量**（物理约束），不检验任何策略、
   不看任何收益，与 `工具/资金容量.py` 同类。

定义（与 `工具/资金容量.py` 同口径）：
    一手风险 = 止损倍数 × ATR(14) × 乘数        ATR 取近 ATR窗口 根日线的中位数
    够格权益 = 一手风险 ÷ 风险预算

输出：能开 1 手的品种，以及 5 万本金下每手的**真实单笔风险占比**。
"""
import os
import sys
import json
import time

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

ATR周期 = 14
日线根数 = 560
ATR窗口 = 500
倍数档 = (1.0, 2.0)
预算档 = (0.003, 0.01, 0.02)
本金 = 50000
每批 = 20                    # ★ 取数铁律：每 20 个轮换会话
输出目录 = os.path.join(_ROOT, "工具", "_输出")


def 连():
    from tqsdk import TqApi, TqAuth
    import config_account
    user, pwd = config_account.check()
    return TqApi(auth=TqAuth(user, pwd), web_gui=False)


def 主连清单(api):
    """全市场主连（`KQ.m@EX.code`）及其当前具体主力合约。

    ⚠️ `query_cont_quotes()` 返回的是**当前主力具体合约**（如 `DCE.c2611`），
       它只有挂牌至今的历史（可能仅 110~420 根，且含上市初期异常波动）
       ⇒ ATR 被系统性高估。必须转成 `KQ.m@DCE.c` 拼接主连才有 500+ 根连续历史。
       本函数返回 {主连代码: 当前主力合约}。
    """
    import re
    r = api.query_cont_quotes()
    out = {}
    for it in r:
        if isinstance(it, dict):
            s = it.get("instrument_id") or it.get("symbol") or ""
        else:
            s = it
        if not s or "." not in s:
            continue
        ex, code = s.split(".", 1)
        m = re.match(r"^([A-Za-z]+)", code)
        if not m:
            continue
        out["KQ.m@%s.%s" % (ex, m.group(1))] = s
    return out


def 算ATR(d, n=ATR周期):
    """TR 的 n 期简单均值（与 lib/engine._atr 同口径）。"""
    h = d["high"].astype(float).values
    l = d["low"].astype(float).values
    c = d["close"].astype(float).values
    if len(d) < n + 2:
        return np.array([])
    tr = np.zeros(len(d))
    tr[0] = h[0] - l[0]
    for i in range(1, len(d)):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    return pd.Series(tr).rolling(n).mean().values


def 拉一个(api, kq, 具体, 件):
    """拉一个**主连**的日线 + 当前主力合约的报价静态属性。失败静默跳过（不写空数据）。"""
    t0 = time.time()
    try:
        ks = api.get_kline_serial(kq, 86400, data_length=日线根数)
        d = ks[ks["datetime"] > 0].copy()
    except Exception as e:
        print("  x %-18s 异常 %s" % (kq, str(e)[:60]), flush=True)
        return False
    if d is None or len(d) < ATR周期 + 20:
        print("  x %-18s 有效根数不足 %d" % (kq, 0 if d is None else len(d)), flush=True)
        return False
    乘 = None
    一跳 = None
    保证金 = None
    try:
        q = api.get_quote(具体)
        v = float(getattr(q, "volume_multiple", 0) or 0)
        乘 = v if v == v and v > 0 else None
        t = float(getattr(q, "price_tick", 0) or 0)
        一跳 = t if t == t and t > 0 else None
        m = float(getattr(q, "margin", 0) or 0)
        保证金 = m if m == m and m > 0 else None
    except Exception:
        pass
    if 乘 is None and "volume_multiple" in d.columns:
        v = float(d["volume_multiple"].iloc[-1])
        if v == v and v > 0:
            乘 = v
    if 乘 is None:
        print("  x %-18s 拿不到乘数" % kq, flush=True)
        return False
    件[kq] = {"d": d, "乘数": 乘, "一跳": 一跳, "保证金": 保证金, "具体": 具体}
    print("  v %-18s %4d 根 乘数%-6.0f %.2fs" % (kq, len(d), 乘, time.time() - t0),
          flush=True)
    return True


def 主流程():
    os.makedirs(输出目录, exist_ok=True)
    件 = {}
    print("== 建立会话 1：拿全市场主连清单 ==", flush=True)
    api = 连()
    try:
        表 = 主连清单(api)          # {KQ.m@EX.code: 当前主力合约}
        键 = sorted(表)
        print("全市场主连 %d 个" % len(键), flush=True)
        for kq in 键[:每批]:
            拉一个(api, kq, 表[kq], 件)
    finally:
        try:
            api.close()
        except Exception:
            pass

    剩余 = 键[每批:]
    for i in range(0, len(剩余), 每批):
        块 = 剩余[i:i + 每批]
        print("== 会话 %d/%d（%s ~ %s）==" % (
            i // 每批 + 2, (len(剩余) + 每批 - 1) // 每批 + 1, 块[0], 块[-1]), flush=True)
        api = 连()
        try:
            for kq in 块:
                拉一个(api, kq, 表[kq], 件)
        finally:
            try:
                api.close()
            except Exception:
                pass

    print("\n== 汇总（成功 %d 个）==" % len(件), flush=True)
    行 = []
    for sym, v in 件.items():
        d = v["d"]
        atr = 算ATR(d)
        m = ~np.isnan(atr)
        if m.sum() < 100:
            continue
        用 = slice(max(0, len(d) - ATR窗口), len(d))
        a = atr[用]
        a = a[~np.isnan(a)]
        if len(a) == 0:
            continue
        atr中位 = float(np.median(a))
        px中位 = float(np.median(d["close"].astype(float).values[用]))
        vol中位 = float(np.median(d["volume"].astype(float).values[用]))
        乘 = v["乘数"]
        额中位 = vol中位 * px中位 * 乘          # 日成交额中位（元）
        行.append({
            "主连": sym,
            "合约": v.get("具体", ""),
            "品种": (v.get("具体", "") or sym).split(".")[-1] if "." in (v.get("具体") or sym) else sym,
            "交易所": sym.split(".")[0].split("@")[-1] if "." in sym else "",
            "根数": len(d),
            "价格": round(px中位, 1),
            "乘数": 乘,
            "一跳": v["一跳"],
            "ATR中位": round(atr中位, 2),
            "保证金": v["保证金"],
            "成交额亿": round(额中位 / 1e8, 2),
            "一手风险_1xATR": round(atr中位 * 乘),
            "一手风险_2xATR": round(2 * atr中位 * 乘),
            "够格权益_0.3%_2x": round(2 * atr中位 * 乘 / 0.003),
            "够格权益_1%_2x": round(2 * atr中位 * 乘 / 0.01),
            "够格权益_2%_2x": round(2 * atr中位 * 乘 / 0.02),
            "5万单笔风险%_1x": round(atr中位 * 乘 / 本金 * 100, 2),
            "5万单笔风险%_2x": round(2 * atr中位 * 乘 / 本金 * 100, 2),
        })
    df = pd.DataFrame(行)

    # 流动性过滤：日成交额 < 1 亿的剔除（ATR 无意义 / 无法成交）
    活 = df[df["成交额亿"] >= 1.0].copy()
    死 = df[df["成交额亿"] < 1.0].copy()
    活 = 活.sort_values("一手风险_2xATR").reset_index(drop=True)

    活.to_csv(os.path.join(输出目录, "_全市场一手风险.csv"),
              index=False, encoding="utf-8-sig")
    with open(os.path.join(输出目录, "_全市场一手风险.json"), "w", encoding="utf-8") as f:
        json.dump({"本金": 本金, "总品种": len(df), "活跃": len(活),
                   "行": 活.to_dict("records")}, f, ensure_ascii=False, indent=1)

    # ---- 控制台表 ----
    print("\n%-16s %9s %7s %9s %10s %11s %11s %8s" % (
        "主连", "价格", "乘数", "ATR中位", "日额(亿)", "1xATR风险", "2xATR风险", "5万2x%"))
    print("-" * 95)
    for _, r in 活.iterrows():
        print("%-16s %9.1f %7.0f %9.2f %10.2f %11.0f %11.0f %7.2f%%" % (
            r["主连"], r["价格"], r["乘数"], r["ATR中位"], r["成交额亿"],
            r["一手风险_1xATR"], r["一手风险_2xATR"], r["5万单笔风险%_2x"]))

    print("\n== 5 万本金能不能开 1 手（1xATR 止损口径 = 引擎实参）==", flush=True)
    for 档, 名 in ((0.003, "0.3%"), (0.01, "1%"), (0.02, "2%")):
        n = int((活["一手风险_1xATR"] / 档 <= 本金).sum())
        print("  {} 预算：5 万够格 {} / {} 个品种".format(名, n, len(活)))
    可2 = 活[活["一手风险_1xATR"] / 0.02 <= 本金]
    可1 = 活[活["一手风险_1xATR"] / 0.01 <= 本金]
    可03 = 活[活["一手风险_1xATR"] / 0.003 <= 本金]
    print("\n  2% 预算下 5 万可开：{}".format(
        "、".join(可2["主连"].tolist()) if len(可2) else "无"))
    print("  1% 预算下 5 万可开：{}".format(
        "、".join(可1["主连"].tolist()) if len(可1) else "无"))
    print("  0.3% 预算下 5 万可开：{}".format(
        "、".join(可03["主连"].tolist()) if len(可03) else "无"))

    print("\n剔除的冷门品种 {} 个：{}".format(
        len(死), "、".join(死["主连"].tolist()) if len(死) else "无"))
    print("\n已写出：{}".format(os.path.join(输出目录, "_全市场一手风险.csv")))
    print("已完成。", flush=True)


if __name__ == "__main__":
    主流程()
