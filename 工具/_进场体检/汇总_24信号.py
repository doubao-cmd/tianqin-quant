# -*- coding: utf-8 -*-
"""汇总：把 24 个信号（17 存档 + 7 第三批）合成一张总表
============================================================================
背景
----
`体检结果_ALL.json`（10-14 生成）只含 17 个信号；2026-10-19 补跑 7 个
（boll_meanrev / ha_trend / macd / kdj / cci / roc / sar）后，需要一张 24 行的总表。

本脚本**只读**历史存档、**不改** `体检结果_ALL.json`，按清单 §2.5 另存：
  体检结果_ALL_24信号.json            （5min，2021-2023，已污染窗口）
  体检结果_ALL_24信号_1440min.json    （1440min，2016-2021，干净窗口）

一致性处理
----------
历史存档的 verdict 是 2026-10-18「三档分级」之前生成的（无 grade_A/B/C 字段）。
合并时不重算 rows（rows 是实测值，不动），只用当前 `verdict()` 由 rows **重新派生**
判词，使 24 个信号的判词口径一致；原判词保留在 `verdict_archive` 字段里。

用法：
  python 汇总_24信号.py
"""
import os
import json
import math
import importlib.util

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location("jctj", os.path.join(_HERE, "进场体检.py"))
m = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m)

BATCH3 = ["boll_meanrev", "ha_trend", "macd", "kdj", "cci", "roc", "sar"]


def _load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _cell_tag(r):
    return {"A": "❌", "B": "⚠️", "C": "✅"}[m.grade(r)]


# ---------------------------------------------------------------- 第 6 条门槛（2026-10-19 新增）
# 起因：1440min（2016-2021）这个窗口里，**零信息基线**「永远做多」就能拿到均 ratio 5.01、
#       9 个品种里 6 个过 ②③ ⇒ 门槛⑤（≥⌈品种数/2⌉）被一个无信号的东西直接通过。
#       在该窗口上 ratio 判据已失去区分能力，必须减去「最好的朴素方向基线」才算超额。
# 定义：base_best(品种) = max(L1 毛/成本 of 永远做多, 永远做空)
#       excess(格)      = 信号 ratio − base_best(该品种)
#       门槛⑥：excess > 0，且同时满足 ②③⑥ 的品种数 ≥ ⌈品种数/2⌉
# ⚠️ 这条**不改**原有五门槛的判词（保留在 verdict 里），只在旁边并列一个 verdict_6。
#    五门槛判词是 2026-10-18 冻结的口径，历史可比；⑥ 是新增层，两者并存便于对照。

def _always_long(o, h, l, c, atr, ma):
    return np.ones(len(c))


def _always_short(o, h, l, c, atr, ma):
    return -np.ones(len(c))


m.SIGNALS["__BASE_long__"] = ("基线·永远做多", _always_long)
m.SIGNALS["__BASE_short__"] = ("基线·永远做空", _always_short)


def _baselines(period, keys):
    """每品种：(ratio_long, ratio_short, ratio_best)。只在同周期/同口径下算一次。"""
    m.PERIOD = period
    out = {}
    for k in keys:
        df = m.load(k)
        if df is None or len(df) < 1000:
            continue
        rl = m.screen_one(k, "__BASE_long__", df)
        rs = m.screen_one(k, "__BASE_short__", df)
        vl = rl.get("ratio") if rl["n"] >= 30 else None
        vs = rs.get("ratio") if rs["n"] >= 30 else None
        best = max([v for v in (vl, vs) if v is not None], default=0.0)
        out[k] = {"long": vl, "short": vs, "best": best}
    return out


def _verdict6(rows):
    ok = [r for r in rows if r["n"] >= 30]
    need = math.ceil(len(ok) / 2) if ok else 1
    hard = [r for r in ok if (r.get("ratio") or -9) >= 2.0 and r["L1_net"] > 0 and (r.get("excess") or -9) > 0]
    better = [r for r in ok if (r.get("excess") or -9) > 0]
    if len(hard) >= need:
        vd = f"PASS⑥（{len(hard)}/{len(ok)} 品种同时过 ②③⑥，需 ≥{need}；仍需换干净样本）"
    elif len(hard) > 0:
        vd = f"FAIL⑥（过 ②③⑥ 的只 {len(hard)}/{len(ok)} < {need}）"
    else:
        vd = f"FAIL⑥（过 ②③⑥ 的 0 个；超额>0 的只有 {len(better)}/{len(ok)} 格）"
    return {"pass_hard_6": len(hard), "need_6": need, "n_sym": len(ok),
            "n_excess_pos": len(better), "verdict_6": vd}


def build(period, archive_name, batch3_suffix, out_name):
    arch = _load_json(os.path.join(_HERE, archive_name))
    merged = {}
    for sig, d in arch.items():
        merged[sig] = {"rows": d["rows"], "verdict_archive": d.get("verdict"), "src": "archive"}
    got = []
    for sig in BATCH3:
        p = os.path.join(_HERE, f"体检结果_{sig}{batch3_suffix}.json")
        if not os.path.exists(p):
            print(f"  [缺失] {os.path.basename(p)}")
            continue
        d = _load_json(p)[sig]
        merged[sig] = {"rows": d["rows"], "verdict_archive": d.get("verdict"), "src": "rerun_20261019"}
        got.append(sig)

    keys = sorted({r["key"] for d in merged.values() for r in d["rows"]})
    base = _baselines(period, keys)
    for d in merged.values():
        for r in d["rows"]:
            b = base.get(r["key"])
            r["base_best"] = None if b is None else round(b["best"], 3)
            r["excess"] = None if (b is None or r.get("ratio") is None) else round(r["ratio"] - b["best"], 3)

    L = []
    L.append("=" * 130)
    L.append(f"进场体检总表（24 信号）  ｜  周期={period}  ｜  HOLD={m.HOLD} 根  ｜  "
             f"止损={m.STOP_ATR_MULT}×ATR  ｜  滑点={m.SLIP}跳  ｜  1 手  ｜  "
             f"17 存档 + 7 第三批  ｜  含第⑥门槛（相对最好朴素方向的超额）")
    L.append("=" * 130)
    L.append(f"{'信号':<15}{'品种数':>7}{'有效格':>7}{'毛利>0':>7}{'均毛(点)':>10}{'均ratio':>9}"
             f"{'均超额':>9}{'A':>4}{'B':>4}{'C':>4}  判词(⑤门槛)")

    agg = {}
    for sig, d in merged.items():
        rows = d["rows"]
        ok = [r for r in rows if r["n"] >= 30]
        vd = m.verdict(rows)
        vd6 = _verdict6(rows)
        d["verdict"] = vd
        d["verdict_6"] = vd6
        pos = sum(1 for r in ok if r["L1_gross"] > 0)
        mg = float(np.mean([r["L1_gross"] for r in ok])) if ok else 0.0
        rat = [r["ratio"] for r in ok if r.get("ratio") is not None]
        mr = float(np.mean(rat)) if rat else 0.0
        exc = [r["excess"] for r in ok if r.get("excess") is not None]
        me = float(np.mean(exc)) if exc else 0.0
        agg[sig] = {"n_sym": len(ok), "pos": pos, "mean_gross": mg, "mean_ratio": mr,
                    "mean_excess": me, "v6": vd6,
                    "A": vd["grade_A"], "B": vd["grade_B"], "C": vd["grade_C"],
                    "verdict": vd["verdict"], "rows": rows}
        L.append(f"{sig:<15}{len(ok):>7}{len(ok):>7}{pos:>7}{mg:>10.3f}{mr:>9.3f}{me:>9.3f}"
                 f"{vd['grade_A']:>4}{vd['grade_B']:>4}{vd['grade_C']:>4}  {vd['verdict'][:26]}")
        if vd["verdict"].startswith("PASS") or vd6["pass_hard_6"] > 0:
            L.append(f"{'':15}{'':>7}{'':>7}{'':>7}{'':>10}{'':>9}{'':>9}{'':>4}{'':>4}{'':>4}  "
                     f"⑥ {vd6['verdict_6']}")

    # ---- 全局分布 ----
    allr = [r for d in merged.values() for r in d["rows"] if r["n"] >= 30]
    rats = [r["ratio"] for r in allr if r.get("ratio") is not None]
    L.append("-" * 118)
    L.append(f"有效格合计 {len(allr)} ｜ 毛利>0 {sum(1 for r in allr if r['L1_gross'] > 0)} "
             f"({sum(1 for r in allr if r['L1_gross'] > 0)/len(allr)*100:.1f}%) ｜ "
             f"毛利中位 {np.median([r['L1_gross'] for r in allr]):+.3f} 点 ｜ "
             f"ratio 中位 {np.median(rats):.3f}")
    for th in (0.5, 1.0, 2.0):
        sel = [r for r in allr if (r.get("ratio") or -9) >= th]
        L.append(f"  ratio ≥ {th:<4} : {len(sel):>3} 格 ｜ 其中 |t|≥2.0 的 {sum(1 for r in sel if abs(r['L1_t']) >= 2.0)} 格")
    excs = [r["excess"] for r in allr if r.get("excess") is not None]
    epos = sum(1 for x in excs if x > 0)
    hard6 = [r for r in allr if (r.get("ratio") or -9) >= 2.0 and r["L1_net"] > 0 and (r.get("excess") or -9) > 0]
    L.append(f"  第⑥门槛：超额 > 0 的格 {epos}/{len(excs)}（{epos/len(excs)*100:.1f}%） ｜ "
             f"同时过 ②③⑥ 的格 {len(hard6)} ｜ 超额中位 {np.median(excs):+.3f}")
    L.append(f"  基线（同口径零信息）：永远做多 均ratio "
             f"{np.mean([b['long'] for b in base.values() if b['long'] is not None]):.2f} ｜ "
             f"永远做空 均ratio {np.mean([b['short'] for b in base.values() if b['short'] is not None]):.2f} ｜ "
             f"取两者较优后中位 base_best {np.median([b['best'] for b in base.values()]):.2f}")
    L.append("-" * 130)
    L.append("⚠️ 24 信号一起看 = 24 次比较；本表只用于『一次性砍掉一批候选』，"
             "禁止挑最好的那一格宣称有效（规则 2 禁区）。")
    L.append("⚠️ 判词(⑤门槛) = 2026-10-18 冻结的原五门槛；⑥ = 2026-10-19 新增「相对最好朴素方向的超额」，"
             "两者并列，不改原判词。")

    txt = "\n".join(L)
    print(txt)
    with open(os.path.join(_HERE, out_name + ".txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    out = {k: {"verdict": v["verdict"], "verdict_6": v["verdict_6"],
               "verdict_archive": v["verdict_archive"],
               "src": v["src"], "rows": v["rows"]} for k, v in merged.items()}
    with open(os.path.join(_HERE, out_name + ".json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n[done] {out_name}.json / .txt   （新增信号 {len(got)} 个：{','.join(got)}）")
    return agg


if __name__ == "__main__":
    print("\n########## 5min ##########")
    a5 = build("5min", "体检结果_ALL.json", "_hold12", "体检结果_ALL_24信号")
    print("\n########## 1440min ##########")
    a1 = build("1440min", "体检结果_ALL_1440min.json", "_1440min_hold12", "体检结果_ALL_24信号_1440min")
