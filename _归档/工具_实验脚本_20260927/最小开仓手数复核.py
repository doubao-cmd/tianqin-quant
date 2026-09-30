# -*- coding: utf-8 -*-
"""最小开仓手数复核 —— 「最小限价开仓 8 手」是全局规则还是个案？

2026-09-27 立项。起因：要按「20 万 · 1% 预算 · 多手」口径改引擎参数，
但项目记忆里有一条「合约最小限价开仓手数 8 手（DCE.v2605 / CZCE.MA605 实测）」，
如果它是**全市场规则**，那多手方案直接撞墙。这条必须先复核。

三条已查明的机制事实（读源码得来，不靠猜）：

  ① 警告出处 = `tqsdk/algorithm/twap.py:174` 与 `tqsdk/lib/target_pos_task.py:262`，
     读的都是 **`quote.open_min_limit_order_volume`** —— 一个**逐合约的交易所字段**
     （`ins_schema.py` 里与 `min_limit_order_volume` / `open_max_limit_order_volume`
     并列的 GraphQL 字段），**不是 tqsdk 的全局设定**。

  ② 静默拒单机制 = `tqsdk/lib/target_pos_task.py:154`：
     「当 offset_priority == '开' 并且内部计算的总下单手数 < open_min_limit_order_volume，
       会创建一个开仓追单任务，但是**不会发单**，并且**直接认为本轮调仓任务完成**。」
     ⇒ 手数不足时**不报错、不成交、也不重试**，正是项目里那个「幽灵止损」的成因。

  ③ 因此**唯有实测这个字段本身**才能判定。本脚本就干这件事。

★ 取数方式（踩坑两次后定稿）：
  · `query_quotes()` 默认 `expired=None` ⇒ **会把 28.3 万个已下市合约全返回**
    （其中「期货类」6 万个）。必须显式 `expired=False`、`ins_class="FUTURE"` ⇒ 936 个。
  · **不要用 `get_quote` + `wait_update`**：夜盘收盘后（如 00:50）无行情推送，
    `wait_update()` 会长时间阻塞（实测一个会话卡死 3 分钟以上）。
  · 改用 **`api.query_symbol_info(list)`** —— 一次批量静态查询，无订阅、无等待，
    且返回值里就直接带 `open_min_limit_order_volume` 等字段。

判据（跑前预注册）：
  · 若**只有个别合约** >1   ⇒ 个案，改用「按品种挑主连时避开」即可，多手方案可落地。
  · 若**同交易所同品种全为 8**  ⇒ 品种级规则，需要在品种池里加字段显式记录。
  · 若**全市场普遍 >1**    ⇒ 全局约束，多手方案必须重做。

⚠️ 本工具**不消耗试验次数 K**（量具，与 资金容量.py / 风控审计.py / 功效检验.py 同类）。
跑法： python 工具/最小开仓手数复核.py
"""

import os
import sys
import json

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config_account  # noqa: E402

输出目录 = os.path.join(_HERE, "_输出")
块大小 = 150        # query_symbol_info 每批查询的合约数

关注列 = [
    "open_min_limit_order_volume",     # ★ 主角：最小限价开仓手数
    "open_min_market_order_volume",    # 最小市价开仓手数
    "min_limit_order_volume",          # 最小限价单手数
    "min_market_order_volume",         # 最小市价单手数
    "open_max_limit_order_volume",     # 最大限价开仓手数（顺带看上限）
    "max_limit_order_volume",
    "open_limit",                      # 日内开仓限额
    "volume_multiple",
    "price_tick",
    "expired",
]


def 连():
    from tqsdk import TqApi, TqAuth
    user, pwd = config_account.check()
    return TqApi(auth=TqAuth(user, pwd), web_gui=False)


def 主流程():
    import pandas as pd

    os.makedirs(输出目录, exist_ok=True)
    print("== 建立会话：清单 + 批量静态查询 ==", flush=True)
    api = 连()
    try:
        全部 = sorted(set(api.query_quotes(ins_class="FUTURE", expired=False)))
        print("在交易期货合约 %d 个" % len(全部), flush=True)
        帧 = []
        for i in range(0, len(全部), 块大小):
            sub = 全部[i:i + 块大小]
            try:
                d = api.query_symbol_info(sub)
            except Exception as e:
                print("   x 静态查询失败 %s ~ %s: %s" % (sub[0], sub[-1], str(e)[:60]),
                      flush=True)
                continue
            帧.append(d)
            print("   静态查询 %d / %d" % (min(i + 块大小, len(全部)), len(全部)), flush=True)
        df = pd.concat(帧, ignore_index=True) if 帧 else pd.DataFrame()
    finally:
        try:
            api.close()
        except Exception:
            pass

    if df.empty:
        print("⛔ 没拿到任何合约信息，退出（不写空数据）", flush=True)
        return None

    print("\n返回 %d 行，列名：" % len(df), flush=True)
    print("  " + " | ".join(str(c) for c in df.columns), flush=True)

    原始 = os.path.join(输出目录, "_最小开仓手数_原始.csv")
    df.to_csv(原始, index=False, encoding="utf-8-sig")
    print("已写出原始表：%s" % 原始, flush=True)

    # ---- 精选列落盘 ----
    有列 = [c for c in 关注列 if c in df.columns]
    缺 = [c for c in 关注列 if c not in df.columns]
    if 缺:
        print("⚠️ 以下关注列不存在（tqsdk 版本差异）：%s" % "、".join(缺), flush=True)
    主 = "open_min_limit_order_volume"
    if 主 not in df.columns:
        print("⛔ 关键列 %s 不存在，无法复核" % 主, flush=True)
        return None

    精 = df[["instrument_id", "product_id", "exchange_id"] + 有列].copy()
    精路径 = os.path.join(输出目录, "_最小开仓手数.csv")
    精.to_csv(精路径, index=False, encoding="utf-8-sig")
    print("已写出精表：%s（%d 行）" % (精路径, len(精)), flush=True)
    return 精


def 汇总(精):
    """按「交易所.品种」聚合 + 全局分布。"""
    import pandas as pd
    主 = "open_min_limit_order_volume"
    d = 精.copy()
    d[主] = pd.to_numeric(d[主], errors="coerce")
    d["品种键"] = d["exchange_id"].astype(str) + "." + d["product_id"].astype(str)

    行 = []
    for k, g in d.groupby("品种键"):
        v = set(x for x in g[主].tolist() if x == x and x is not None)
        行.append({
            "品种键": k,
            "合约数": int(len(g)),
            "最小值": int(min(v)) if v else None,
            "最大值": int(max(v)) if v else None,
            "值集": "、".join(str(int(x)) for x in sorted(v)) or "无数据",
        })
    行.sort(key=lambda x: (-(x["最大值"] or 0), x["品种键"]))
    return 行, d


if __name__ == "__main__":
    import pandas as pd

    精 = 主流程()
    if 精 is None:
        sys.exit(1)

    汇总行, d = 汇总(精)
    主 = "open_min_limit_order_volume"

    print("\n" + "=" * 78, flush=True)
    print("一、全局分布（按合约数）", flush=True)
    print("=" * 78, flush=True)
    dist = {}
    for v in d[主].tolist():
        key = None if (v != v or v is None) else int(v)
        dist[key] = dist.get(key, 0) + 1
    for v in sorted(dist, key=lambda x: (x is None, x)):
        名 = "无数据/0" if v in (None, 0) else "%d 手" % v
        print("  %-10s %4d 个合约" % (名, dist[v]), flush=True)

    坏 = [r for r in 汇总行 if (r["最大值"] or 0) > 1]
    有值 = [r for r in 汇总行 if (r["最大值"] or 0) > 0]

    print("\n" + "=" * 78, flush=True)
    print("二、最小限价开仓手数 > 1 的品种（共 %d / %d 个有值品种）" % (len(坏), len(有值)),
          flush=True)
    print("=" * 78, flush=True)
    if not 坏:
        print("  （无）⇒ 该约束在本市场不成立", flush=True)
    else:
        print("  %-14s %6s  %s" % ("品种", "合约数", "最小限价开仓手数"))
        print("  " + "-" * 56, flush=True)
        for r in 坏:
            print("  %-14s %6d  %s" % (r["品种键"], r["合约数"], r["值集"]), flush=True)

    print("\n" + "=" * 78, flush=True)
    print("三、按交易所", flush=True)
    print("=" * 78, flush=True)
    ex = {}
    for r in 汇总行:
        k = r["品种键"].split(".", 1)[0]
        e = ex.setdefault(k, {"品种数": 0, "最大": 0, "坏": 0})
        e["品种数"] += 1
        e["最大"] = max(e["最大"], r["最大值"] or 0)
        if (r["最大值"] or 0) > 1:
            e["坏"] += 1
    for k in sorted(ex):
        print("  %-8s 品种 %2d 个   最大最小开仓手数 %-4s   >1 的品种 %d 个" % (
            k, ex[k]["品种数"], ex[k]["最大"] or "无数据", ex[k]["坏"]), flush=True)

    # ---- 结论（按预注册判据自动判定）----
    率 = (len(坏) / len(有值)) if 有值 else 0
    if 率 == 0:
        判定 = "个案/不存在 —— 多手方案可落地"
    elif 率 < 0.2:
        判定 = "少数品种级规则（%.0f%%）—— 需在品种池显式记录并避开" % (率 * 100)
    else:
        判定 = "普遍约束（%.0f%%）—— 多手方案必须重做" % (率 * 100)

    print("\n" + "=" * 78, flush=True)
    print("四、判定", flush=True)
    print("=" * 78, flush=True)
    print("  有效品种 %d 个，其中最小开仓手数 >1 的 %d 个（%.1f%%）" % (
        len(有值), len(坏), 率 * 100), flush=True)
    print("  ⇒ %s" % 判定, flush=True)

    jsonp = os.path.join(输出目录, "_最小开仓手数_结果.json")
    with open(jsonp, "w", encoding="utf-8") as f:
        json.dump({
            "合约总数": int(len(精)),
            "品种数": len(汇总行),
            "分布": {("无数据/0" if k is None else str(k)): v for k, v in dist.items()},
            "坏品种": 坏,
            "按交易所": ex,
            "判定": 判定,
        }, f, ensure_ascii=False, indent=2)
    print("\n已写出：%s" % jsonp, flush=True)
    print("已完成。", flush=True)
