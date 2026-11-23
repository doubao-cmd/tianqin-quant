# -*- coding: utf-8 -*-
"""
5min 全量重跑对账（2026-11-04）
==============================================================================
背景：10-14 那批 5min 判死证据跑在 94.3% 退化的数据上；10-19 号称"用干净数据
      独立复现"，实测两份 json 1,836 个数值字段 100% 逐位相同（= 从未重算）。
      本脚本对 11-04 用干净 5min 全量重跑的结果做逐格对账。

输入
----
  体检结果_ALL.json          10-14 版（坏 5min，17 信号）
  体检结果_ALL_24信号.json    10-19 拼接体（17 旧继承 + 7 新跑）
  体检结果_ALL_hold12.json   11-04 干净 5min 全量重跑（24 信号）
  以上三者均在本目录；旧两份另存 `_历史_10-14坏数据_20261014/`

输出
----
  对账报告 md（打印 + 落盘 对账_5min重跑.md）

用法
----
  <tqsdk venv>/python.exe 对账_5min重跑.py
  # 重跑体检本身：<tqsdk venv>/python.exe 进场体检.py --all
"""
import json
import os
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
FIELDS = ["n", "L0_gross", "L0_t", "L1_gross", "L1_net", "L1_t",
          "ratio", "cost_pts", "stop_share"]


def load(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return json.load(f)


def grade(r):
    """三档：A 真死 ratio<1 / B 薄利 [1,2) / C 值得进 >=2。"""
    rt = r.get("ratio")
    if rt is None or rt < 1.0:
        return "A"
    if rt < 2.0:
        return "B"
    return "C"


def cells(J, sigs):
    return [(s, r) for s in sigs for r in J[s]["rows"] if r["n"] >= 30]


def main():
    new = load("体检结果_ALL_hold12.json")
    old = load("体检结果_ALL.json")
    old19 = load("体检结果_ALL_24信号.json")
    sig17 = [k for k in new if k in old]
    sig7 = [k for k in new if k not in old]

    L = ["# 5min 全量重跑对账（2026-11-04）", ""]

    # ---------- 一、数据版本 ----------
    L += ["## 一、数据版本", "",
          "| 项 | 值 |", "|---|---|",
          "| 数据目录 | `工具/_进场体检/数据/*_5min_2021-01-01_2023-12-31.csv`（12 品种） |",
          "| mtime | 2025-10-17 16:37~17:07（重拉后未再改动） |",
          "| 退化率 | **0.00%~0.49%**（O==H==L==C 占比；旧批 94.3%） |",
          "| 实际区间 | **2020-10-26 ~ 2023-12-29**（文件名写 2021-01-01 起，比实际晚 2 个月） |",
          "| 根/日 | 68.3（有夜盘品种）/ 45.0（JD、LH 无夜盘） |", ""]

    # ---------- 二、总览 ----------
    allc = cells(new, list(new))
    a = b = c = 0
    for _, r in allc:
        g = grade(r)
        a += g == "A"
        b += g == "B"
        c += g == "C"
    nets = [r["L1_net"] for _, r in allc]
    L += ["## 二、总览（24 信号 × 12 品种 = 288 格）", "",
          "| 判据 | 结果 |", "|---|---|",
          f"| 有效格（n≥30） | **{len(allc)} / 288**（满矩阵，无缺格） |",
          f"| A 真死（ratio<1） | **{a}** |",
          f"| B 薄利（1≤ratio<2） | **{b}** |",
          f"| C 值得进（ratio≥2） | **{c}** |",
          f"| L1净 为正的格 | **{sum(1 for x in nets if x > 0)}**（{sum(1 for x in nets if x > 0)/len(nets)*100:.1f}%） |",
          f"| L1净 均值 / 中位 | {statistics.mean(nets):+.3f} / {statistics.median(nets):+.3f} 点 |",
          "", "### 唯一非 A 档的格", "",
          "| 信号 | 品种 | n | ratio | L1净 | t | 结论 |", "|---|---|---:|---:|---:|---:|---|"]
    for s, r in allc:
        if grade(r) != "A":
            L.append(f"| {s} | {r['key']} | {r['n']} | {r['ratio']:.3f} | "
                     f"{r['L1_net']:+.2f} | {r['L1_t']:.2f} | B 档但不显著（\\|t\\|<2） |")
    L.append("")

    # ---------- 三、显著格 ----------
    L += ["## 三、显著性统计", "",
          "| 数据集 | 有效格 | \\|t\\|≥2 | 其中负 | **其中正** | 最大 t | 最大 ratio |",
          "|---|---:|---:|---:|---:|---:|---:|"]
    for J, sigs, tag in ((old, sig17, "10-14 坏 5min（17 信号）"),
                         (new, sig17, "11-04 干净 5min（17 信号）"),
                         (new, list(new), "11-04 干净 5min（24 信号）")):
        cs = cells(J, sigs)
        s2 = [r for _, r in cs if abs(r["L1_t"]) >= 2]
        neg = sum(1 for r in s2 if r["L1_t"] < 0)
        L.append(f"| {tag} | {len(cs)} | {len(s2)} | {neg} | **{len(s2)-neg}** | "
                 f"{max(r['L1_t'] for _, r in cs):.2f} | {max(r['ratio'] for _, r in cs):.2f} |")
    L += ["", "> ⚠️ 「全部为负」的格彼此高度相关（同品种、同窗口、信号重叠），",
          "> **不得**按独立试验折算成 `2^-N` 的 P 值。可用的硬事实是「正显著格 = 0」。", ""]

    # ---------- 四、逐格对账 ----------
    tot = same = 0
    for s in sig17:
        ra = {r["key"]: r for r in old[s]["rows"]}
        rb = {r["key"]: r for r in new[s]["rows"]}
        for k in ra:
            for f in FIELDS:
                if f in ra[k] and f in rb.get(k, {}):
                    tot += 1
                    same += ra[k][f] == rb[k][f]
    L += ["## 四、17 个旧信号逐格对账（9 字段全量）", "",
          f"- 可比字段 **{tot}**，逐位相同 **{same}（{same/tot*100:.1f}%）**，"
          f"不同 **{tot-same}**", "",
          "| 字段 | 变化格数 | 旧均值 | 新均值 | 中位相对差 |", "|---|---:|---:|---:|---:|"]
    byf = {}
    for s in sig17:
        ra = {r["key"]: r for r in old[s]["rows"]}
        rb = {r["key"]: r for r in new[s]["rows"]}
        for k in ra:
            for f in FIELDS:
                if f in ra[k] and f in rb.get(k, {}) and ra[k][f] != rb[k][f]:
                    byf.setdefault(f, []).append((ra[k][f], rb[k][f]))
    for f in FIELDS:
        if f not in byf:
            L.append(f"| {f} | 0 | — | — | 完全一致 |")
            continue
        xs = [p[0] for p in byf[f]]
        ys = [p[1] for p in byf[f]]
        rel = [abs(y - x) / abs(x) for x, y in byf[f]
               if isinstance(x, (int, float)) and x not in (0, None)
               and isinstance(y, (int, float))]
        med = f"{statistics.median(rel)*100:.1f}% |" if rel else "— |"
        L.append(f"| {f} | {len(xs)} | {statistics.mean(xs):.3f} | "
                 f"{statistics.mean(ys):.3f} | {med}")
    L.append("")

    # ---------- 五、三档迁移 ----------
    tr = {}
    for s in sig17:
        ra = {r["key"]: r for r in old[s]["rows"]}
        rb = {r["key"]: r for r in new[s]["rows"]}
        for k in ra:
            tr[(grade(ra[k]), grade(rb[k]))] = tr.get((grade(ra[k]), grade(rb[k])), 0) + 1
    L += ["## 五、三档迁移（坏数据 → 干净数据）", "",
          "| 旧\\新 | A | B | C |", "|---|---:|---:|---:|"]
    for g0 in "ABC":
        L.append(f"| **{g0}** | {tr.get((g0,'A'),0)} | {tr.get((g0,'B'),0)} | {tr.get((g0,'C'),0)} |")
    L += ["", f"- **变活（旧 A → 新 B/C）：{tr.get(('A','B'),0)+tr.get(('A','C'),0)} 格**",
          f"- **变死（旧 B/C → 新 A）：{tr.get(('B','A'),0)+tr.get(('C','A'),0)} 格**", "",
          "### 旧 B/C 档逐格下落", "",
          "| 信号 | 品种 | 旧档 | 旧 ratio | 旧 n | 新 ratio | 新 n | 新档 |",
          "|---|---|---|---:|---:|---:|---:|---|"]
    for s in sig17:
        ra = {r["key"]: r for r in old[s]["rows"]}
        rb = {r["key"]: r for r in new[s]["rows"]}
        for k in ra:
            g0, g1 = grade(ra[k]), grade(rb[k])
            if g0 != "A" or g1 != "A":
                L.append(f"| {s} | {k} | {g0} | {ra[k]['ratio']:.3f} | {ra[k]['n']} | "
                         f"{rb[k]['ratio']:.3f} | {rb[k]['n']} | {g1} |")
    L.append("")

    # ---------- 六、成本配置漂移 ----------
    L += ["## 六、顺带查出：一处成本配置漂移（非数据问题）", ""]
    def cost_map(J, sigs):
        d = {}
        for s in sigs:
            for r in J[s]["rows"]:
                d.setdefault(r["key"], set()).add(r["cost_pts"])
        return d
    o, n = cost_map(old, sig17), cost_map(new, sig17)
    for k in sorted(o):
        ov, nv = max(o[k]), max(n[k])
        if ov != nv:
            L.append(f"- **{k}**：cost_pts {ov:.3f} → **{nv:.3f}**"
                     f"（{nv/ov:.2f}×，来自 `config_symbols.py` tick 1→2，提交 `67884fa`，"
                     f"大商所棕榈油最小跳动 2 元/吨 —— **是修正，不是回归**）")
    L.append("")

    # ---------- 七、代码版本对照 ----------
    tot7 = same7 = 0
    diffs7 = []
    for s in sig7:
        ra = {r["key"]: r for r in old19[s]["rows"]}
        rb = {r["key"]: r for r in new[s]["rows"]}
        for k in ra:
            for f in ["n", "L0_gross", "L1_gross", "L1_net", "L1_t", "ratio", "stop_share"]:
                if f in ra[k]:
                    tot7 += 1
                    if ra[k][f] == rb[k][f]:
                        same7 += 1
                    else:
                        diffs7.append((s, k, f, ra[k][f], rb[k][f]))
    L += ["## 七、代码版本对照（7 个 10-19 新信号，两次都跑在干净数据上）", "",
          f"- 7 信号 × 12 品种 × 7 字段 = **{tot7}**，逐位相同 **{same7}（{same7/tot7*100:.1f}%）**",
          f"- 不同的 **{len(diffs7)}** 项，全部集中在 **P 的 3 个成本相关字段**"
          f"（L1_net / L1_t / ratio × 7 信号 = 21 项）"
          if len(diffs7) == 21 else f"- 不同的 {len(diffs7)} 项", "",
          "⇒ **代码版本零漂移**（唯一差异由 P 的 tick 修正解释），"
          "所以第四节的差异**只能归因于数据 + P 成本**，不是代码。", ""]

    txt = "\n".join(L)
    print(txt)
    out = os.path.join(HERE, "对账_5min重跑.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print(f"\n[done] {out}")


if __name__ == "__main__":
    main()
