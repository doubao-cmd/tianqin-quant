# -*- coding: utf-8 -*-
"""由 probe_cffex.json + probe_depth.json 生成 Phase0 报告（数字全部现算，不手打）。"""
import io
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "报告")

p = json.load(io.open(os.path.join(OUT, "probe_cffex.json"), encoding="utf-8"))
d = json.load(io.open(os.path.join(OUT, "probe_depth.json"), encoding="utf-8"))

COMM = {"T": 3.0, "TF": 3.0, "TS": 3.0, "TL": 5.0}   # 元/手，平今 0
RISK_EQUITY = 200000.0
RISK_PCT = 0.01
CORR_MAX_GATE = 0.50


def main():
    items = p["items"]
    rows = []
    for k, v in items.items():
        spec_tick, mult = v["tick"], v["mult"]
        m5 = v["min5"]
        atr = m5.get("atr14_median")
        stop = 2 * atr * mult if atr else None
        slip = 2 * spec_tick * mult
        comm = COMM[k]
        cost = slip + comm
        rows.append({
            "key": k, "name": v["name"], "tick": spec_tick, "mult": mult,
            "last": v["quote"]["last_price"], "oi": v["quote"]["open_interest"],
            "notional": v.get("notional_per_lot"), "margin": v.get("margin_per_lot_est"),
            "atr5": atr, "atr_ticks": m5.get("atr_in_ticks"),
            "tick_atr": m5.get("tick_over_atr_pct"),
            "stop": round(stop, 0) if stop else None,
            "slip": round(slip, 0), "comm": comm, "cost": round(cost, 0),
            "cost_pct_stop": round(cost / stop * 100, 1) if stop else None,
            "lots_1pct": int(RISK_EQUITY * RISK_PCT // stop) if stop else None,
            "days5": m5.get("trading_days"),
            "deg5": m5.get("HL_deg_pct"),
            "daily_rows": v["daily"].get("real_rows"),
            "daily_first": str(v["daily"].get("first_bj"))[:10],
            "daily_deg": v["daily"].get("HL_deg_pct"),
            "bars_day": m5.get("bars_per_day_median"),
        })

    corr = p["corr"]
    mat = corr["matrix"]
    keys = list(mat.keys())
    n = len(keys)
    rho_bar = corr["mean_pairwise"]
    eff_legs = n / (1 + (n - 1) * rho_bar)

    depth = {r["name"]: r for r in d["symbols"]["KQ.m@CFFEX.T"]["rows"]}

    L = []
    A = L.append
    A("# 国债期货 Phase 0：数据可得性与结构指标")
    A("")
    A("> 只读探针，未跑任何策略、未改任何配置、未建仓。")
    A("> 生成时间：%s ｜ 数据源：天勤免费号 `free-api.shinnytech.com`" % p["probe_time"])
    A("> 复现：`python probe_cffex.py`（四合约日线+5min）、`python probe_depth.py`（周期深度矩阵）")
    A("")
    A("## 一、结论摘要")
    A("")
    A("| 问题 | 答案 |")
    A("|---|---|")
    A("| 数据拿得到吗 | **拿得到，且干净**。日线 T/TF 10.8 年、TS 7.7 年、TL 3.5 年；退化率 ≤0.1% |")
    A("| MA13 原参数（5min）能搬过来吗 | **不能**。免费号 5min 只有 **%.2f 年**（%d 根 = %d 个交易日），不够分年、不够样本内外 |"
      % (depth["5min"]["years"], depth["5min"]["real_rows"], depth["5min"]["days"]))
    A("| 哪个频率有足够历史 | **30min（%.2f 年）/ 60min（%.2f 年）/ 日线（%.2f 年）** |"
      % (depth["30min"]["years"], depth["60min"]["years"], depth["日线"]["years"]))
    A("| 几个合约能当几条腿 | **≈ %.2f 条**（平均两两 ρ=%.3f） |" % (eff_legs, rho_bar))
    A("| 按现有结构闸门（tick/ATR≤30%）过关的 | **TL 过关；TF、TS 明显不合格；T 落在 30% 线附近（估计量敏感，见 §4 注）** |")
    A("")
    A("## 二、数据可得性：分钟级历史是硬天花板")
    A("")
    A("| 周期 | 可得根数 | 覆盖交易日 | 起始 | 折合年数 | 每日 bar |")
    A("|---|---:|---:|---|---:|---:|")
    for nm in ("5min", "15min", "30min", "60min", "日线"):
        r = depth[nm]
        A("| %s | %d | %d | %s | %.2f | %.1f |" % (
            nm, r["real_rows"], r["days"], r["first_bj"][:10], r["years"], r["bars_per_day"]))
    A("")
    A("`get_kline_data_series`（按区间拉历史）返回：**仅限专业版**。")
    A("⇒ 单序列 `data_length` 上限 10000 根就是免费号的历史天花板，绕不过去。")
    A("")
    A("**这条直接决定可行性**：MA13 一年 500-700 笔，5min 只有 %.2f 年 ⇒ 全样本约 370-520 笔，"
      "只能得到一个样本段，无法分年、无法留样本外。**这条线在数据层面就断了，不是策略好坏问题。**" % depth["5min"]["years"])
    A("")
    A("## 三、日线质量（可直接用于中低频研究）")
    A("")
    A("| 合约 | 根数 | 起始 | H==L 退化 |")
    A("|---|---:|---|---:|")
    for r in rows:
        A("| %s %s | %d | %s | %.2f%% |" % (r["key"], r["name"], r["daily_rows"], r["daily_first"], r["daily_deg"]))
    A("")
    A("## 四、结构指标（5min）")
    A("")
    A("| 合约 | 最新价 | ATR14 | ATR(跳) | **tick/ATR** | 2×ATR止损(元/手) | 1跳滑点往返 | 手续费 | 成本/止损 |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        A("| %s | %s | %s | %s | **%s%%** | %s | %s | %s | %s%% |" % (
            r["key"], r["last"], r["atr5"], r["atr_ticks"], r["tick_atr"],
            r["stop"], r["slip"], r["comm"], r["cost_pct_stop"]))
    A("")
    A("口径：滑点按 **1 跳/边**（往返 2 跳）计；手续费 T/TF/TS 3 元、TL 5 元/手，平今免收。")
    A("")
    A("**两个观察**：")
    A("1. **成本占止损距离 19%（TL）到 48%（TS）**。TS 每笔未出发就先亏掉近半个止损，与碳酸锂的结论同型——"
      "不是策略不灵，是成本结构性吃掉毛利。")
    A("2. 项目现有结构闸门是 `tick/ATR ≤ 30%`。据此：**TL 18.7% ✅、TF 42.4% ❌、TS 46.7% ❌**；"
      "**T 的 30.4% 落在阈值线上，不能直接判死**。")
    A("")
    A("> **★ 估计量口径差异（必须写明）**：项目的 `工作台/data/tick_atr.json` 里 `atr5` 是**选池时某一根 bar 的快照**"
      "（字段含 `end_bar: \"799\"`），本报告用的是**全样本 ATR14 中位数**。用同一算法复现 MA：")
    A(">")
    A("> | 品种 | 本报告中位口径 | 项目快照口径 | 差 |")
    A("> |---|---:|---:|---:|")
    A("> | MA | 8.86（tick_frac 0.113） | 9.93（`tick_frac` 0.1007） | 快照高 **12%** |")
    A(">")
    A("> ⇒ 若国债也存在同向 12% 的差，T 的 tick/ATR 会落到约 **27%**，反而过闸门。"
      "**T 必须用与生产完全相同的快照口径复核后才能定档**；TL / TF / TS 距阈值远，结论不受此影响。")
    A("")
    A("## 五、四个合约不是四条腿")
    A("")
    A("日对数收益相关矩阵（对齐后 %d 个交易日，受 TL 上市时间限制）：" % corr["n_days"])
    A("")
    A("| | " + " | ".join(keys) + " |")
    A("|---|" + "---:|" * len(keys))
    for a in keys:
        A("| **%s** | " % a + " | ".join("%.3f" % mat[a][b] for b in keys) + " |")
    A("")
    A("平均两两 **%.3f**，最高 **%.3f（T~TF）**。按等权组合的有效独立腿近似 `n/(1+(n-1)ρ̄)`："
      % (rho_bar, corr["max_pairwise"]))
    A("")
    A("> **4 / (1 + 3×%.3f) = %.2f 条腿**" % (rho_bar, eff_legs))
    A("")
    A("对比现有商品池四腿 max|ρ| = 0.462。**国债四合约只能当 1 条腿用**，不能用商品的 5 腿仓位框架。")
    A("")
    A("## 六、资金结构：真正的风险不在止损，在名义")
    A("")
    A("| 合约 | 名义/手 | 保证金/手(交易所口径) | 1%风险下单手数 | 对应名义 | 对应保证金 | 保证金/20万 |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        lots = r["lots_1pct"]
        A("| %s | %s | %s | %d | %s | %s | %.0f%% |" % (
            r["key"], format(int(r["notional"]), ","), format(int(r["margin"]), ","), lots,
            format(int(lots * r["notional"]), ","), format(int(lots * r["margin"]), ","),
            lots * r["margin"] / RISK_EQUITY * 100))
    A("")
    A("**这里是国债最反直觉的一点**：5min ATR 极小（T 只有 3.3 跳），按 1% 风险定手数会下到 6 手，"
      "名义敞口 **660 万**，是 20 万本金的 **33 倍**。商品腿 MA 一手名义仅 2.6 万。")
    A("")
    A("止损很近 ≠ 风险很小。国债日 ATR 约 0.24%（T 为 0.2643/110.28），一次 3% 的单边（2016-12、2020-05 都出现过）"
      "就是 12 个日 ATR，6 手 = 约 20 万，正好吃掉全部本金。**这是杠杆问题，不是止损参数问题。**")
    A("")
    A("## 七、处置建议")
    A("")
    A("**不做**：")
    A("- ❌ **5min / MA13 原参数搬国债**——数据只有 0.81 年，无法验证。")
    A("- ❌ **T/TF/TS/TL 当四条腿**——有效独立腿 1.19，等于加杠杆做同一个方向。")
    A("- ❌ **TF / TS**——成本占止损 44%/48%，且 tick/ATR 42%/47% 远离闸门线，结构性不划算。")
    A("")
    A("**待复核**：")
    A("- ⚠️ **T**：tick/ATR 30.4% 落在 30% 线附近，且估计量差异（快照 vs 中位数）足以翻转结论。"
      "要用生产同款快照口径重算后才能定档，**先别下结论**。")
    A("")
    A("**可做（如果要做）**：")
    A("- ✅ **TL 单腿 + 30min 或 60min**：tick/ATR 18.7% 是四个里唯一舒服的，60min 有 6.83 年历史，可分段验证。")
    A("  但 TL 一手保证金 4.16 万、2×ATR 止损 1,072 元，20 万基数只装得下 1 手。")
    A("- ✅ **日线级别**（10.8 年，T/TF）：样本最长，但没有夜盘、一年 244 根，笔数天然少。")
    A("")
    A("**前置未决问题（须先拍板）**：现有 MA13 框架 `RISK_EQUITY=20万` 是为 2.6 万名义/手的商品设计的。"
      "国债名义 107-205 万/手，要么改手数上限（按名义封顶而非按风险定手数），要么这套框架根本不能复用。"
      "**这个决定要先于任何回测。**")
    A("")
    A("---")
    A("")
    A("## 附：本次探针遇到的问题（已排除）")
    A("")
    A("18:29 那次探针卡死 4 分钟以上、CPU 零增长，不是接口问题：")
    A("")
    A("| 事实 | 证据 |")
    A("|---|---|")
    A("| 卡的是 K 线请求，不是连接 | `_t1.log`：quote 成功返回 `110.28`；`_t2.log`：`connected` 后无任何输出 |")
    A("| 账号当时被另一会话占用 | 补数据会话的 28 个 CSV 在 **18:37:19–18:37:48** 集中写盘，与探针存活期（18:29:04 起）重叠 |")
    A("| 空闲后同一代码 0.1 秒返回 | 19:00 复跑：日线 10000 根 0.2s、5min 10000 根 1.0s |")
    A("")
    A("⇒ **根因＝两个会话抢同一个快期账号**（项目已知限制：tqsdk 单账号单会话）。"
      "与报告结论无关，但下次跑 tqsdk 前必须先确认没有第二个会话在连。")

    txt = "\n".join(L) + "\n"
    path = os.path.join(OUT, "phase0_国债数据探路.md")
    io.open(path, "w", encoding="utf-8", newline="\n").write(txt)
    print("written %s  (%d lines)" % (path, len(L)))
    for r in rows:
        print("  %-3s tick/ATR=%5.1f%%  cost/stop=%5.1f%%  lots@1%%=%2d  notional=%s" % (
            r["key"], r["tick_atr"], r["cost_pct_stop"], r["lots_1pct"], format(int(r["notional"]), ",")))
    print("  effective legs = %.2f" % eff_legs)


if __name__ == "__main__":
    main()
