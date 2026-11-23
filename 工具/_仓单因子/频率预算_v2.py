# -*- coding: utf-8 -*-
"""
仓单变化率因子 —— 信号频率预算 v2（2026-11-17，复活评估用）

背景
----
2026-10-12 首版频率预算用 akshare SHFE+CZCE 数据（2018-11~2022-01），结论：
  · 9 品种里 5 个拿不到（M/V/JD/JM/P 全在大商所，被瑞数反爬封锁）
  · 可取的 5 个里 4 个 rc 退化（触发天数占比 64%~88% = 信号常开）
  ⇒ 收档。收档理由一半是「数据封锁」，一半是「比率定义对小基数不稳健」。

2026-11-17，ifx.duckdb 已有 dce 仓单 1565 天 / 23 品种（走真浏览器过 JS 挑战），
第一条理由失效，故重做频率预算。**本脚本只看因子值的分布，不碰任何收益数据。**

纪律声明（重要）
----------------
1. **不动定义**：rc = s_t / s_{t-K} - 1，K=20，THRESH=±10%，与 2026-10-12 卡完全一致。
2. **不调参**：不扫描 K / THRESH，不按品种单独标定。
3. **不新增否决阈值**：卡片要求「先把『不得常开』写进 gate 再跑」，但从未给出量化门槛。
   本脚本因此**只把「触发天数占比」作为判决输入显式列出**，并与 10-12 已判退化的
   4 个样本（MA 75.9% / RB 88.1% / SA 83.1% / SR 64.1%，最低 64.1%）并列对照，
   由人裁决是否属于同一区间；脚本自己不下「通过/不通过」的最终结论。
4. **不碰收益**：全程无价格数据，因此**不构成样本污染**，可以放心用全样本。

数据口径（本次新发现的坑，与 10-12 版本不同）
-------------------------------------------
· czce：每个交易日列出**所有**交割仓库（无仓单的记 0）⇒ 一般不缺日。
· dce ：页面**只列有仓单的仓库**，品种仓单归零时**整个品种从页面消失**
        ⇒ 「该品种当日无行」多数情况 = **仓单 0**，不是采集缺失。
        实测取证：2026-08-18 dce 有 18 个品种，JD 不在其中；JD 08-05 最后一条 qty=0.0。
· **但短段不是 0，是月初发布延迟**（本版第二处修正）：
        M 缺 14 天 = 9 段，最长仅 3 天，日期为 2020-12-01~03 / 2024-04-01~03 / 2022-04-01；
        V 缺 5 天 = 4 段，全部落在月初月末（2021-04-01 / 2022-03-31~04-01 / 2023-04-03）。
        M 中位仓单 2 万，物理上不可能三天归零 ⇒ 填 0 会制造 +∞ / −100% 的假信号。
        故按段长分流：**连续缺失 ≥5 天 ⇒ 判 0（真归零）；≤4 天 ⇒ ffill（发布延迟）**。
        误差是有界的：真归零被 ffill 只是抹平一次小波动；采集抖动被填 0 则是爆炸性假信号。
· 整所缺日（如 dce 2026-10-28~10-31 全无数据）⇒ 记为 NaN，绝不用 ffill 伪造。

依赖：仅 duckdb + 标准库（本机 pandas/numpy 版本互斥，故意不引入）
运行：python 频率预算_v2.py
产物：频率预算_结果_v2.txt / 频率预算_v2.csv
"""
import csv
import os
import sys

try:
    import duckdb
except ImportError:
    sys.exit("需要 duckdb")

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.abspath(os.path.join(
    HERE, "..", "..", "..", "信息源抓取", "工具", "data", "db", "ifx.duckdb"))
OUT = os.path.join(HERE, "频率预算_结果_v2.txt")
OUT_CSV = os.path.join(HERE, "频率预算_v2.csv")

K = 20          # 与 2026-10-12 卡一致，不调
TH = 0.10       # 与 2026-10-12 卡一致，不调

# 原卡 9 品种池。SHFE 两个（RB/RU）仓单只 60 天，无法参与。
POOL = ["MA", "M", "RB", "V", "JD", "JM", "P", "RU", "SR"]


# ---------- 统计小工具（对齐 pandas 默认口径） ----------
def q(sorted_vals, p):
    """线性插值分位数，等价 pandas Series.quantile(p)。"""
    if not sorted_vals:
        return float("nan")
    n = len(sorted_vals)
    if n == 1:
        return float(sorted_vals[0])
    pos = (n - 1) * p
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)


def stdev(vals):
    """样本标准差 ddof=1。"""
    n = len(vals)
    if n < 2:
        return float("nan")
    m = sum(vals) / n
    return (sum((x - m) ** 2 for x in vals) / (n - 1)) ** 0.5


def median(vals):
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return float("nan")
    return float(s[n // 2]) if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def episodes(flags):
    """连续 True 段数。段内多日算一次。"""
    n = 0
    prev = False
    for f in flags:
        if f and not prev:
            n += 1
        prev = f
    return n


def yearly(cal, rc):
    """按自然年切分：{年: (有效日, 触发日)}。纯描述统计，用于看「常开」是否分段稳定。"""
    out = {}
    for d, x in zip(cal, rc):
        if x is None:
            continue
        y = d[:4]
        a, b = out.get(y, (0, 0))
        out[y] = (a + 1, b + (1 if abs(x) > TH else 0))
    return out


# ---------- 取数 ----------
def load(con):
    rows = con.execute("""
        SELECT exchange, trade_date, upper(variety_id) AS v, sum(qty) AS qty
        FROM fact_receipt
        WHERE exchange IN ('czce','dce')
        GROUP BY 1,2,3
        ORDER BY 1,2
    """).fetchall()

    # ex -> {date: {v: qty}}
    by_ex = {}
    for ex, d, v, qty in rows:
        by_ex.setdefault(ex, {}).setdefault(d, {})[v] = float(qty or 0)

    series, diag = {}, []
    for ex, days in by_ex.items():
        cal = sorted(days)
        # 该所每个交易日有几个品种有行（识别整所缺日）
        depth = {d: len(days[d]) for d in cal}
        for v in POOL:
            present = [d for d in cal if v in days[d]]
            if not present:
                continue
            # 每日状态：P=有行  A=缺行但该所正常(候选0)  N=该所整体缺数
            state = []
            for d in cal:
                if v in days[d]:
                    state.append("P")
                elif depth[d] >= 3:
                    state.append("A")
                else:
                    state.append("N")
            vals = [days[d][v] if st == "P" else None for d, st in zip(cal, state)]

            # 按连续段分流：>=5 天判 0，<=4 天 ffill（月初发布延迟）
            n_miss = n_zero = n_ff = 0
            i = 0
            while i < len(cal):
                if state[i] != "A":
                    i += 1
                    continue
                j = i
                while j < len(cal) and state[j] == "A":
                    j += 1
                seg = j - i
                n_miss += seg
                if seg >= 5:
                    for k in range(i, j):
                        vals[k] = 0.0
                    n_zero += seg
                else:
                    prev = None
                    for k in range(i - 1, -1, -1):
                        if state[k] == "P":
                            prev = vals[k]
                            break
                    for k in range(i, j):
                        vals[k] = prev
                    n_ff += seg
                i = j
            n_nan = sum(1 for x in vals if x is None)
            series[v] = (cal, vals)
            diag.append((v, ex, len(cal), len(present), n_miss, n_zero, n_ff, n_nan))
    return series, diag


def main():
    if not os.path.exists(DB):
        sys.exit(f"找不到库：{DB}")
    con = duckdb.connect(DB, read_only=True)
    series, diag = load(con)
    con.close()

    lines = []

    def P(s=""):
        print(s)
        lines.append(s)

    P("=" * 96)
    P("仓单变化率因子 · 信号频率预算 v2（2026-11-17 复活评估）")
    P("数据 ifx.duckdb  fact_receipt  czce+dce  全样本 2020-06-01~2026-11-17")
    P(f"rc = receipt_t / receipt_(t-{K}) − 1   |  触发 |rc| > {TH:.0%}  |  无收益数据，不构成污染")
    P("=" * 96)
    P()
    P("【数据对齐诊断】缺行如何被解释（dce「无行=仓单0」是本版关键修正）")
    h = (f"{'品种':<5}{'所':<6}{'日历日':>7}{'有行日':>7}{'缺行':>7}"
         f"{'判为0':>7}{'ffill':>7}{'仍NaN':>7}")
    P(h)
    P("-" * 80)
    for v, ex, ncal, npres, nmiss, nzero, nff, nnan in diag:
        P(f"{v:<5}{ex:<6}{ncal:>7}{npres:>7}{nmiss:>7}{nzero:>7}{nff:>7}{nnan:>7}")
    P()

    res, bad = [], []
    for v in POOL:
        if v not in series:
            bad.append((v, "shfe 仓单仅 60 天，本库不覆盖"))
            continue
        cal, s = series[v]
        rc, valid = [], []
        for i in range(len(s)):
            if i < K or s[i] is None or s[i - K] is None or s[i] <= 0 or s[i - K] <= 0:
                rc.append(None)
                continue
            rc.append(s[i] / s[i - K] - 1.0)
            valid.append(rc[-1])

        n_total = len(s)
        n_valid = len(valid)
        if n_valid < 60:
            bad.append((v, f"有效样本不足（{n_valid}/{n_total}）"))
            continue

        trig = [(x is not None and abs(x) > TH) for x in rc]
        n_trig = sum(1 for t in trig if t)
        pos = sorted(x for x in s if x is not None and x > 0)
        absrc = sorted(abs(x) for x in valid)

        res.append({
            "品种": v,
            "有效日": n_valid,
            "有效占比": round(100 * n_valid / n_total, 1),
            "仓单中位": int(median(pos)),
            "仓单均值": int(sum(pos) / len(pos)),
            "归零日": sum(1 for x in s if x == 0),
            "rc_std": round(100 * stdev(valid), 1),
            "rc_p05": round(100 * q(sorted(valid), 0.05), 1),
            "rc_p50": round(100 * q(sorted(valid), 0.50), 1),
            "rc_p95": round(100 * q(sorted(valid), 0.95), 1),
            "absrc_med": round(100 * q(absrc, 0.50), 1),
            "触发天数": n_trig,
            "触发占比": round(100 * n_trig / n_valid, 1),
            "触发段": episodes(trig),
            "多段": episodes([(x is not None and x < -TH) for x in rc]),
            "空段": episodes([(x is not None and x > TH) for x in rc]),
            "_cal": (cal[0], cal[-1]),
            "_year": yearly(cal, rc),
        })

    P("【主表】全样本描述统计（不看收益）")
    hdr = (f"{'品种':<6}{'有效日':>7}{'有效%':>7}{'仓单中位':>11}{'归零日':>7}"
           f"{'rc_std%':>10}{'|rc|中位%':>10}{'触发日':>7}{'触发%':>8}{'段':>5}{'多':>4}{'空':>4}")
    P(hdr)
    P("-" * len(hdr))
    for r in res:
        P(f"{r['品种']:<6}{r['有效日']:>7}{r['有效占比']:>7}{r['仓单中位']:>11,}{r['归零日']:>7}"
          f"{r['rc_std']:>10,.1f}{r['absrc_med']:>10,.1f}{r['触发天数']:>7}{r['触发占比']:>8}"
          f"{r['触发段']:>5}{r['多段']:>4}{r['空段']:>4}")
    P()
    if bad:
        P("不可测：")
        for v, why in bad:
            P(f"  {v}: {why}")
        P()

    P("【rc 分位】看是否爆量（10-12 版 MA 曾出现 rc = +887400%）")
    h2 = f"{'品种':<6}{'p05%':>13}{'p50%':>10}{'p95%':>13}{'std%':>13}"
    P(h2)
    P("-" * len(h2))
    for r in sorted(res, key=lambda x: x["rc_std"]):
        P(f"{r['品种']:<6}{r['rc_p05']:>13,.1f}{r['rc_p50']:>10,.1f}{r['rc_p95']:>13,.1f}{r['rc_std']:>13,.1f}")
    P()

    P("【分段稳定性】逐年触发天数占比（%）。若各年都高 ⇒ 结构性缺陷，不是某段行情偶然")
    years = sorted({y for r in res for y in r["_year"]})
    h3 = f"{'品种':<6}" + "".join(f"{y:>8}" for y in years)
    P(h3)
    P("-" * len(h3))
    for r in res:
        cells = []
        for y in years:
            nv, nt = r["_year"].get(y, (0, 0))
            cells.append(f"{round(100 * nt / nv, 0):>8.0f}" if nv >= 20 else f"{'--':>8}")
        P(f"{r['品种']:<6}" + "".join(cells))
    P("（'--' = 该年有效样本 <20 天，不足以判断）")
    P()

    P("【gate】判定输入（脚本不下最终结论，按卡片纪律由人裁决）")
    P("  —— 卡片要求：『不得常开』须先写进 gate 再跑，此处显式化为第 1 条。")
    P()
    P("  ① 不得常开（卡片强制项）：单品种触发天数占比")
    P("     参照系 = 2026-10-12 已判退化的 4 个样本：MA 75.9% / RB 88.1% / SA 83.1% / SR 64.1%")
    P("     ⚠ 原卡未给量化门槛，故只列数字与参照对比，不新增阈值。")
    for r in sorted(res, key=lambda x: -x["触发占比"]):
        mark = "  ← 落在已知退化区间(≥64.1%)" if r["触发占比"] >= 64.1 else ""
        P(f"       {r['品种']:<4}{r['触发占比']:>7}%   ({r['触发天数']}/{r['有效日']}){mark}")
    P()
    P("  ② 信号量下界（卡片 §五-6）：触发段数 ≥ 2")
    for r in res:
        P(f"       {r['品种']:<4}{r['触发段']:>5} 段  {'OK' if r['触发段'] >= 2 else 'FAIL 段数过少'}")
    P()
    P("  ③ 品种覆盖（卡片 §五-5）：需 9 个")
    P(f"       可测 {len(res)} / 9；缺口：{', '.join(v for v, _ in bad)}")
    P()
    P("  ④ 有效仓单日占比（10-12 版可测前提 ≥50%）")
    for r in res:
        P(f"       {r['品种']:<4}{r['有效占比']:>7}%  {'OK' if r['有效占比'] >= 50 else 'FAIL 归零过多'}")
    P()
    P("=" * 96)
    P("⚠ 本脚本不输出「通过/不通过」。判据 ① 无量化门槛（原卡未定），须人对照参照系裁决。")
    P("=" * 96)

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n已写入 {OUT}")

    if res:
        cols = [k for k in res[0] if not k.startswith("_")]
        with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in res:
                w.writerow(r)
        print(f"已写入 {OUT_CSV}")


if __name__ == "__main__":
    main()
