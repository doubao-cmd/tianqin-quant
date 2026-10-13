# -*- coding: utf-8 -*-
"""
未来函数对照（诊断性质，★ **故意违规**，不是策略）

背景：用户提出「过拟合其实可以有；我们现在所有价格策略都是**欠拟合**（连历史都抓不住）」。
本脚本回答一个可证伪的问题：

    如果允许「多知道一点未来」，收益能提升多少？
    ⇒ 从而量化「抓不住历史」缺的到底是「模型容量」还是「未来信息」。

★ 设计（**唯一变量 = 信息量**）
  固定进场点集合 = `retrace_ma13` 的信号点（同一批时点、同样 n、同样成本、同样 L1 逻辑）
  只改「方向 sgn」的信息来源：

    B0  随机方向   sgn = 随机 ±1（零信息基线；跑 B0_REPEATS 次取均值）
    B1  真实信号   sgn = retrace_ma13 方向（现状，无未来函数）
    F1  知下一根   sgn = sign(close[i+1] − open[i+1])        （1 步优势）
    F2  知期末     sgn = sign(close[i+H] − open[i+1])        （H 步优势，但不知路径）
    F3  知路径     F2 方向 + 只在「该方向不会先碰止损」时进场（完全未来）

⚠️ F1/F2/F3 **全部违反规则 1（禁未来函数）**，仅为对照实验，不得用于任何交易决策。

★ 内置对拍：B1 必须与 `进场体检.py --signal retrace_ma13` 的 ratio **逐品种一致**，
   否则说明本脚本的 L1 复现口径有误（项目纪律：两个脚本算同一件事必须对拍）。

用法：python 未来函数对照.py [--period 5min]
"""
import os
import sys
import glob
import math
import argparse

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import 进场体检 as es  # noqa: E402

SEED = 20261014
B0_REPEATS = 20          # 随机方向重复次数（取均值，降低单次噪声）
MIN_N = 30               # 与进场体检一致
COLS = ["档位", "说明", "n", "止损率", "L1毛均", "L1净均", "毛/成本", "胜率"]


def _symbols(period):
    fs = glob.glob(os.path.join(es.DATA_DIR, f"*_{period}_2021-01-01_2023-12-31.csv"))
    keys = []
    for f in fs:
        k = os.path.basename(f).split(f"_{period}_")[0]
        if k not in keys:
            keys.append(k)
    return sorted(keys)


def _t(x):
    x = np.asarray(x, float)
    if len(x) < 2:
        return 0.0
    sd = float(np.std(x, ddof=1))
    return float(np.mean(x) / (sd / math.sqrt(len(x)))) if sd > 1e-12 else 0.0


def run(key, period):
    df = es.load(key, period)
    if df is None:
        return None
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    N = len(c)
    atr = es._atr(h, l, c, es.ATR_N)
    ma = es._sma(c, es.MA_N)
    H = es.HOLD
    sm = es.STOP_ATR_MULT

    sig = es.SIGNALS["retrace_ma13"][1](o, h, l, c, atr, ma)
    bad = np.isnan(atr) | np.isnan(ma)
    sig = np.where(bad, 0.0, np.nan_to_num(sig, nan=0.0))

    valid = np.arange(0, N - H - 1)
    idx = valid[sig[valid] != 0]
    if len(idx) < MIN_N:
        return None

    entry = o[idx + 1]
    cp = es.cost_points(key)

    def l1(idx_, entry_, sgn_):
        """与 进场体检.screen_one 的 L1 逐行同口径（止损夹紧不利侧）。"""
        stop = np.where(sgn_ > 0, entry_ - sm * atr[idx_], entry_ + sm * atr[idx_])
        exit_px = o[idx_ + 1 + H].copy()
        hit = np.zeros(len(idx_), dtype=bool)
        for j, (e_i, st, s) in enumerate(zip(idx_, stop, sgn_)):
            for jj in range(e_i + 1, e_i + 1 + H):
                if s > 0:
                    if l[jj] <= st:
                        exit_px[j] = min(st, o[jj]); hit[j] = True; break
                else:
                    if h[jj] >= st:
                        exit_px[j] = max(st, o[jj]); hit[j] = True; break
        gross = (exit_px - entry_) * sgn_
        return gross, gross - cp, hit

    def pack(name, desc, idx_, sgn_):
        g, n_, hit = l1(idx_, o[idx_ + 1], sgn_)
        return {"档位": name, "说明": desc, "n": int(len(idx_)),
                "止损率": float(np.mean(hit)), "L1毛均": float(np.mean(g)),
                "L1净均": float(np.mean(n_)),
                "毛/成本": float(np.mean(g) / cp) if cp > 0 else None,
                "胜率": float(np.mean(n_ > 0)), "t": _t(n_)}

    rows = []

    # ---- B0 随机方向（零信息基线）
    rng = np.random.default_rng(SEED)
    acc = []
    for _ in range(B0_REPEATS):
        s0 = rng.choice([-1.0, 1.0], size=len(idx))
        g, n_, hit = l1(idx, entry, s0)
        acc.append((float(np.mean(g)), float(np.mean(n_)), float(np.mean(hit)), float(np.mean(n_ > 0))))
    a = np.mean(acc, axis=0)
    rows.append({"档位": "B0", "说明": "随机方向(零信息)", "n": int(len(idx)),
                 "止损率": float(a[2]), "L1毛均": float(a[0]), "L1净均": float(a[1]),
                 "毛/成本": float(a[0] / cp) if cp > 0 else None, "胜率": float(a[3]), "t": 0.0})

    # ---- B1 真实信号（应与 进场体检 一致）
    rows.append(pack("B1", "真实信号(现状)", idx, sig[idx]))

    # ---- F1 知下一根
    s1 = np.sign(c[idx + 1] - o[idx + 1]); s1[s1 == 0] = 1.0
    rows.append(pack("F1", "知下一根", idx, s1))

    # ---- F2 知持有期末方向
    s2 = np.sign(c[idx + H] - o[idx + 1]); s2[s2 == 0] = 1.0
    rows.append(pack("F2", "知期末方向", idx, s2))

    # ---- F3 知路径：只在「该方向不会先碰止损」时进
    _, _, hit2 = l1(idx, entry, s2)
    keep = ~hit2
    if keep.sum() >= MIN_N:
        idx3, s3 = idx[keep], s2[keep]
        rows.append(pack("F3", "知路径(避开止损)", idx3, s3))
    else:
        rows.append({"档位": "F3", "说明": "知路径(样本不足)", "n": int(keep.sum()),
                     "止损率": 0.0, "L1毛均": 0.0, "L1净均": 0.0, "毛/成本": None,
                     "胜率": 0.0, "t": 0.0})

    return {"key": key, "name_cn": es.cs.get(key)[0], "rows": rows,
            "base_ratio_screen_one": None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default=es.PERIOD)
    a = ap.parse_args()
    period = a.period

    keys = _symbols(period)
    if not keys:
        print(f"[err] 无 {period} 数据")
        return
    print(f"未来函数对照 ｜ 周期 {period} ｜ 进场点=retrace_ma13 ｜ HOLD={es.HOLD} ｜ 止损={es.STOP_ATR_MULT}ATR")
    print(f"品种 {len(keys)} 个 ｜ B0 重复 {B0_REPEATS} 次\n")

    res = {}
    for k in keys:
        r = run(k, period)
        if r is None:
            print(f"  [skip] {k} 样本不足")
            continue
        res[k] = r
        line = f"  {k:<4}{r['name_cn']:<6}"
        for row in r["rows"]:
            ratio = row["毛/成本"]
            line += f" {row['档位']}={('%.2f' % ratio) if ratio is not None else '—':>7}"
        print(line)

    # ---- 跨品种汇总
    print("\n" + "=" * 92)
    print("跨品种汇总（毛/成本 的品种均值）")
    print("=" * 92)
    tags = ["B0", "B1", "F1", "F2", "F3"]
    desc = {"B0": "随机方向(零信息)", "B1": "真实信号(现状)", "F1": "知下一根",
            "F2": "知期末方向", "F3": "知路径(避开止损)"}
    summ = {}
    for tg in tags:
        vals = [x["毛/成本"] for r in res.values() for x in r["rows"]
                if x["档位"] == tg and x["毛/成本"] is not None]
        stop = [x["止损率"] for r in res.values() for x in r["rows"] if x["档位"] == tg]
        summ[tg] = (float(np.mean(vals)) if vals else None, len(vals),
                    float(np.mean(stop)) if stop else None)
        m, cnt, st = summ[tg]
        print(f"  {tg}  {desc[tg]:<18} 毛/成本均值 = {m:+.3f}（{cnt} 品种）  止损率均值 = {st*100:.1f}%")

    # ---- 对拍：B1 vs 进场体检
    mism = []
    for k, r in res.items():
        df = es.load(k, period)
        ref = es.screen_one(k, "retrace_ma13", df)
        mine = [x for x in r["rows"] if x["档位"] == "B1"][0]
        if ref.get("ratio") is not None and mine["毛/成本"] is not None:
            if abs(ref["ratio"] - mine["毛/成本"]) > 0.02:
                mism.append((k, ref["ratio"], mine["毛/成本"]))
    print("\n" + "=" * 92)
    if mism:
        print("❌ 对拍失败（B1 与 进场体检 不一致）:")
        for k, x, y in mism:
            print(f"   {k}: screen_one={x}  本脚本={y}")
    else:
        print("✅ 对拍通过：B1（本脚本）与 进场体检 --signal retrace_ma13 逐品种一致")

    # ---- 落盘
    out = os.path.join(_HERE, f"未来函数对照结果{'' if period == '5min' else '_' + period}.md")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("# 未来函数对照（★ 故意违规的诊断实验，不是策略）\n\n")
        f.write(f"- 周期：**{period}**；进场点集合 = `retrace_ma13` 信号点；`HOLD={es.HOLD}`；"
                f"止损 `{es.STOP_ATR_MULT}×ATR`；成本口径与「进场体检」一致。\n")
        f.write(f"- B0 = 随机方向（{B0_REPEATS} 次均值，零信息基线）；F1/F2/F3 = 逐级注入未来信息。\n")
        f.write("- 对拍：" + ("❌ 失败 " + str(mism) if mism else "✅ B1 与 `进场体检.py --signal retrace_ma13` 一致") + "\n\n")
        f.write("## 跨品种汇总（毛/成本 = L1毛均 ÷ 往返成本）\n\n")
        f.write("| 档位 | 信息 | 毛/成本均值 | 品种数 | 止损率均值 |\n|---|---|---:|---:|---:|\n")
        for tg in tags:
            m, cnt, st = summ[tg]
            f.write(f"| **{tg}** | {desc[tg]} | {m:+.3f} | {cnt} | {st*100:.1f}% |\n")
        f.write("\n## 逐品种\n\n")
        f.write("| 品种 | " + " | ".join(t + "毛/成本" for t in tags) + " |\n")
        f.write("|---|" + "---:|" * len(tags) + "\n")
        for k, r in res.items():
            cells = []
            for tg in tags:
                v = [x["毛/成本"] for x in r["rows"] if x["档位"] == tg]
                cells.append(f"{v[0]:+.2f}" if v and v[0] is not None else "—")
            f.write(f"| {k} {r['name_cn']} | " + " | ".join(cells) + " |\n")
        f.write("\n## 各档明细（跨品种均值）\n\n")
        f.write("| 档位 | 止损率 | L1毛均 | L1净均 | 胜率 |\n|---|---:|---:|---:|---:|\n")
        for tg in tags:
            v = [x for r in res.values() for x in r["rows"] if x["档位"] == tg]
            if not v:
                continue
            f.write(f"| {tg} {desc[tg]} | {np.mean([x['止损率'] for x in v])*100:.1f}% | "
                    f"{np.mean([x['L1毛均'] for x in v]):+.2f} | "
                    f"{np.mean([x['L1净均'] for x in v]):+.2f} | "
                    f"{np.mean([x['胜率'] for x in v])*100:.1f}% |\n")
        f.write("\n---\n★ 说明：F1/F2/F3 全部使用未来信息（违反规则 1），"
                "本表**只用于量化「未来信息的价值」**，不构成任何策略建议。\n")
    print(f"\n[落盘] {out}")


if __name__ == "__main__":
    main()
