# -*- coding: utf-8 -*-
"""证伪边界：按「最小可检测效应 / 成本」把每一格分成有功效 / 功效不足。

原理
----
t = mean / SE  ⇒  SE = |mean| / |t|
MDE（双侧 alpha=0.05、power=0.80）= (1.96+0.84) × SE = 2.8 × SE
若 MDE < 往返成本，说明该格「有能力检出可覆盖成本的 edge」；
此时仍未检出正效应，才是「有功效的阴性结果」（可证伪）。
若 MDE > 成本，只能说「样本不足、未检出」，不可证伪。

输出：分桶后的正向/负向显著格数。
只读产物，不重跑。
"""
import io
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
MDE_K = 2.8


def mde_over_cost(seg):
    n = seg.get("n") or 0
    g = seg.get("L0_gross")
    t = seg.get("L0_t")
    cp = seg.get("cost_pts")
    if n < 100 or g is None or t is None or not cp or abs(t) < 1e-9:
        return None
    return MDE_K * abs(g) / (abs(t) * cp)


def bucket(segs, label):
    enough, weak = [], []
    for name, seg in segs:
        r = mde_over_cost(seg)
        if r is None:
            continue
        (enough if r < 1 else weak).append((name, r, seg))

    print("=" * 80)
    print(f"{label}：有效格 {len(enough) + len(weak)}，MDE<成本 {len(enough)}，MDE>成本 {len(weak)}")
    for tag, pool in (("有功效（MDE<成本）", enough), ("功效不足（MDE>成本）", weak)):
        if not pool:
            continue
        pos = [x for x in pool if x[2]["L0_t"] >= 2.0]
        neg = [x for x in pool if x[2]["L0_t"] <= -2.0]
        print(f"  {tag}: {len(pool)} 格 | 正向显著 {len(pos)} | 负向显著 {len(neg)} | 不显著 {len(pool) - len(pos) - len(neg)}")
        if pos:
            print("    正向显著的格：")
            for name, r, seg in pos[:10]:
                print(f"      {name} n={seg['n']} L0={seg['L0_gross']:+.2f} t={seg['L0_t']:+.2f} MDE/成本={r:.2f}")
    return enough, weak


def main():
    out = []

    def emit(s=""):
        print(s)
        out.append(s)

    d5 = json.load(io.open(os.path.join(_HERE, "体检结果_ALL_hold12.json"), encoding="utf-8"))
    segs5 = []
    for sig, v in d5.items():
        if isinstance(v, dict) and "rows" in v:
            for r in v["rows"]:
                segs5.append((f"{sig}×{r['key']}", r))

    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bucket(segs5, "5min（24 信号 × 12 品种，2021-2023）")
        d15 = json.load(io.open(os.path.join(_HERE, "体检结果_15min_ISOOS.json"), encoding="utf-8"))
        segs15 = [(f"{r['signal']}×{r['key']}", r["full"]) for r in d15["rows"]]
        bucket(segs15, "15min 全样本（24 信号 × 12 品种，2021-2025）")
        segs15o = [(f"{r['signal']}×{r['key']}", r["oos"]) for r in d15["rows"]]
        bucket(segs15o, "15min OOS（2024-2025，已污染）")

    txt = buf.getvalue()
    print(txt)
    p = os.path.join(_HERE, "功效_MDE_结果.txt")
    io.open(p, "w", encoding="utf-8").write(txt)
    print(f"[写入] {p}")


if __name__ == "__main__":
    main()
