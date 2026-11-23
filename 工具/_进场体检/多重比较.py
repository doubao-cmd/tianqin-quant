# -*- coding: utf-8 -*-
"""判死尺子 · 多重比较重算

回答一个问题：288 格体检里，达到 |t|>=2 的格子里正向/负向各多少，
在「信号无方向信息」的零假设下，这个分布的概率是多少。

输出：Bonferroni 阈值、FDR、符号检验 p 值。
只读产物，不重跑回测。
"""
import io
import json
import math
import os

from scipy.stats import binomtest, norm

_HERE = os.path.dirname(os.path.abspath(__file__))


def load_rows(fn):
    d = json.load(io.open(os.path.join(_HERE, fn), encoding="utf-8"))
    rows = []
    for sig, v in d.items():
        if not isinstance(v, dict) or "rows" not in v:
            continue
        for r in v["rows"]:
            r = dict(r)
            r["signal"] = sig
            rows.append(r)
    return rows


def report(rows, label, n_min=30):
    ok = [r for r in rows if r.get("n", 0) >= n_min]
    print("=" * 78)
    print(f"{label}：总格 {len(rows)}，n>={n_min} 有效格 {len(ok)}")

    for field in ("L0_t", "L1_t"):
        t = [r[field] for r in ok if r.get(field) is not None]
        pos = sum(1 for x in t if x >= 2.0)
        neg = sum(1 for x in t if x <= -2.0)
        nonsig = len(t) - pos - neg
        print(f"\n  [{field}]  |t|>=2 共 {pos + neg} 格：正 {pos} / 负 {neg} / 不显著 {nonsig}")
        if pos + neg > 0:
            p = binomtest(neg, pos + neg, 0.5, alternative="greater").pvalue
            print(f"    符号检验（H0 正负各半，单侧 P(负>=观测)）= {p:.3e}")
        # Bonferroni：双侧 alpha=0.05 / 有效格数
        if len(t) > 0:
            z_bonf = norm.ppf(1 - 0.025 / len(t))
            over = sum(1 for x in t if abs(x) >= z_bonf)
            print(f"    Bonferroni 阈值 |t|>={z_bonf:.2f}（alpha=0.05/{len(t)}），过线 {over} 格")
        # 期望假阳性
        print(f"    若信号无信息，|t|>=2 的期望格数 = {0.05 * len(t):.1f}")

    # 最优格（只作描述，不得据此择优）
    best = max(ok, key=lambda r: (r.get("ratio") or -9))
    print(f"\n  ratio 最高格（描述性，不得择优）：{best['signal']} × {best['key']} "
          f"n={best['n']} ratio={best.get('ratio')} L1_t={best.get('L1_t')} stop={best.get('stop_share')}")


def main():
    lines = []

    def cap(*a):
        s = " ".join(str(x) for x in a)
        print(s)
        lines.append(s)

    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r5 = load_rows("体检结果_ALL_hold12.json")
        report(r5, "5min（干净数据，24 信号 × 12 品种，2021-2023）")
        print("\n\n15min（干净数据，2021-2025，IS=2021-2023 / OOS=2024-2025）")
        d = json.load(io.open(os.path.join(_HERE, "体检结果_15min_ISOOS.json"), encoding="utf-8"))
        print("  agg:", json.dumps(d.get("agg"), ensure_ascii=False))
        print("  alive:", json.dumps(d.get("alive"), ensure_ascii=False))
    txt = buf.getvalue()
    print(txt)
    out = os.path.join(_HERE, "多重比较重算_结果.txt")
    io.open(out, "w", encoding="utf-8").write(txt)
    print(f"\n[写入] {out}")


if __name__ == "__main__":
    main()
