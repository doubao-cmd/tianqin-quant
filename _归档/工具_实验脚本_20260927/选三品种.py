# -*- coding: utf-8 -*-
"""
选三品种 —— 从 12 品种池里挑出「≤3 品种组合」的最优搭配（不耗 K）
====================================================================
（2026-09-26 新建，对应出路 C：加钱 + 多品种，用户限「不超过三个品种」）

【筛选顺序（2026-09-26 定稿，不可调换）】
  ① 够格权益（资金第一性约束）→ ② 相关性（能否真分散）→ ③ 成本效率

【口径】
  够格权益 = 一手风险 ÷ 风险预算。一手风险 = 止损倍数 × 中位ATR(14) × 乘数。
  默认风险预算取 0.30%（与 `品种池/*.py` 字段一致）。
  相关性 = 日线收益率 500 根样本的皮尔逊相关（来源 `_资金容量_相关.csv`）。

【为什么不用「历史策略表现」当筛选依据】
  12 品种里已被策略跑过的 5 个（V/SA/RB/MA/M）**大多数是负期望**，
  按表现选出来的必然是「过去最好」= 过拟合。所以本脚本**只看结构性指标**
  （够格权益、相关性、成本），不看任何回测收益。

用法：
    python 工具/选三品种.py              # 输出 top 15 三品种组合
    python 工具/选三品种.py --本金 400000
    python 工具/选三品种.py --止损 2.0    # 按 2×ATR 止损口径（默认 1.0）
"""

import os
import sys
import csv
import json
import itertools

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_输出 = os.path.join(_ROOT, "工具", "_输出")
表路径 = os.path.join(_输出, "_资金容量_表.csv")
相关路径 = os.path.join(_输出, "_资金容量_相关.csv")
报告路径 = os.path.join(_输出, "_选三品种_结果.md")
json路径 = os.path.join(_输出, "_选三品种_结果.json")

# 中文名（报告用）
中文名 = {
    "RB": "螺纹钢", "M": "豆粕", "V": "PVC", "MA": "甲醇", "SR": "白糖",
    "JD": "鸡蛋", "SA": "纯碱", "EB": "苯乙烯", "P": "棕榈油", "JM": "焦煤",
    "RU": "天然橡胶", "LH": "生猪",
}

腿数 = 3          # 用户限定「不超过三个品种」⇒ 直接算 3


def _读表(止损倍数):
    """读资金容量表，取出本档位的够格权益 / 一手风险 / 成本占比。"""
    rows = {}
    with open(表路径, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("有效", "").strip().lower() not in ("true", "1"):
                continue
            k = r["key"].strip()
            if not k:
                continue
            rows[k] = {
                "key": k,
                "中文": 中文名.get(k, k),
                "乘数": float(r["乘数"]),
                "中位ATR": float(r["中位ATR"]),
                "中位价": float(r["中位价"]),
                "保证金": float(r["保证金"]),
                "够格权益": float(r["够格权益%.1fATR" % 止损倍数]),
                "一手风险": float(r["一手风险%.1fATR" % 止损倍数]),
                "成本占比": float(r["成本占2ATR风险%"]) / 100.0,   # 表格已是百分数
            }
    return rows


def _读相关():
    """读相关矩阵 → {(a,b): r}，对称。"""
    with open(相关路径, "r", encoding="utf-8-sig", newline="") as f:
        rd = list(csv.reader(f))
    表头 = [c.strip() for c in rd[0][1:]]
    相关 = {}
    for row in rd[1:]:
        if not row:                    # 尾部空行
            continue
        a = (row[0] or "").strip()
        if not a:
            continue
        for j, b in enumerate(表头):
            try:
                相关[(a, b)] = float(row[j + 1])
            except Exception:
                pass
    return 相关, 表头


def _z(值表):
    """标准化（用于把量纲不同的指标合成一个分）。"""
    if not 值表:
        return {}
    均 = sum(值表.values()) / len(值表)
    方差 = sum((v - 均) ** 2 for v in 值表.values()) / max(1, len(值表) - 1)
    标准差 = 方差 ** 0.5 or 1.0
    return {k: (v - 均) / 标准差 for k, v in 值表.items()}


def 算(止损倍数=1.0, 本金=1000000.0):
    品种 = _读表(止损倍数)
    相关, _ = _读相关()
    keys = [k for k in 品种 if k in 中文名]

    组合 = []
    for 三 in itertools.combinations(sorted(keys), 腿数):
        rs = [相关.get((a, b), 相关.get((b, a), 0.0)) for a, b in itertools.combinations(三, 2)]
        平均相关 = sum(rs) / len(rs)
        最大相关 = max(rs)
        够格和 = sum(品种[k]["够格权益"] for k in 三)
        最贵 = max(品种[k]["够格权益"] for k in 三)
        成本占比 = sum(品种[k]["成本占比"] for k in 三) / len(三)
        保证金和 = sum(品种[k]["保证金"] for k in 三)
        组合.append({
            "品种": list(三),
            "中文": [品种[k]["中文"] for k in 三],
            "平均相关": 平均相关,
            "最大相关": 最大相关,
            "够格和": 够格和,
            "最贵够格": 最贵,
            "成本占比": 成本占比,
            "保证金和": 保证金和,
            # 是否「三腿全够格」：每腿都能按目标预算开出手数
            "全够格": bool(最贵 <= 本金),
            "够格数": sum(1 for k in 三 if 品种[k]["够格权益"] <= 本金),
            "可开手数": {k: max(1, int(本金 * 0.003 / 品种[k]["一手风险"])) for k in 三},
            "每手风险占本金": {k: 品种[k]["一手风险"] / 本金 for k in 三},
        })

    # ---- 综合打分：相关性越低越好、够格权益越小越好（留余地）、成本越低越好 ----
    z均 = _z({tuple(c["品种"]): c["平均相关"] for c in 组合})
    z最 = _z({tuple(c["品种"]): c["最大相关"] for c in 组合})
    z够 = _z({tuple(c["品种"]): c["够格和"] for c in 组合})
    z本 = _z({tuple(c["品种"]): c["成本占比"] for c in 组合})
    for c in 组合:
        t = tuple(c["品种"])
        # 全够格是硬门槛，不满足直接降权
        c["分"] = (z均[t] + z最[t] + z够[t] + z本[t]) + (0.0 if c["全够格"] else 3.0)
    组合.sort(key=lambda c: c["分"])
    return 组合, 品种


def _写报告(组合, 品种, 止损倍数, 本金):
    L = []
    A = L.append
    A("# 三品种组合 —— 选型结果（出路 C ｜ 模拟盘 %s 万）" % f"{本金/10000:.0f}")
    A("")
    A("> 2026-09-26 ｜ 工具 `工具/选三品种.py` ｜ 不耗 K（纯结构筛选，不含任何回测收益）")
    A("")
    A("## 口径")
    A("")
    A("- 止损倍数档：**%.1f × ATR(14)**（中位，近 500 个交易日）" % 止损倍数)
    A("- 风险预算：**0.30% / 笔**（与 `品种池/*.py` 字段一致）")
    A("- `够格权益 = 一手风险 ÷ 0.30%`；本金 < 够格权益 ⇒ 该腿的实际单笔风险 **超过** 0.30%")
    A("- `平均相关` = 三腿两两日线收益相关性的均值（越低越好）")
    A("- `成本占比` = 开平一次成本 ÷ 2×ATR 风险额（越低越好，衡量「成本吃掉多少钱」）")
    A("")
    A("## 一、12 品种单表（本档位）")
    A("")
    A("| 品种 | 中文 | 乘数 | 中位ATR | 保证金 | **够格权益** | 够格? | 成本占比 |")
    A("|---|---|---:|---:|---:|---:|:--:|---:|")
    for k, v in sorted(品种.items(), key=lambda x: x[1]["够格权益"]):
        够 = "✅" if v["够格权益"] <= 本金 else "❌"
        A("| %s | %s | %g | %.1f | %s | **%s 元** | %s | %.2f%% |" % (
            k, v["中文"], v["乘数"], v["中位ATR"], f"{v['保证金']:,.0f}",
            f"{v['够格权益']:,.0f}", 够, v["成本占比"] * 100))
    A("")
    A("> 结论：本档位下 **%d / %d** 个品种够格（够格权益 ≤ %s 元）。" % (
        sum(1 for v in 品种.values() if v["够格权益"] <= 本金), len(品种), f"{本金:,.0f}"))
    A("")
    A("## 二、Top 15 三品种组合")
    A("")
    A("| # | 组合 | 平均相关 | 最大相关 | 够格权益和 | 三腿全够格 | 若每腿 0.3% 可开手数 | 成本占比 |")
    A("|---:|---|---:|---:|---:|:--:|---|---:|")
    for i, c in enumerate(组合[:15], 1):
        手 = " / ".join("%s %d 手" % (k, v) for k, v in c["可开手数"].items())
        A("| %d | %s | %.3f | %.3f | %s 万 | %s | %s | %.2f%% |" % (
            i, " + ".join(c["品种"]), c["平均相关"], c["最大相关"],
            f"{c['够格和']/10000:.0f}", "✅" if c["全够格"] else "❌", 手,
            c["成本占比"] * 100))
    A("")
    A("## 三、注意")
    A("")
    A("1. 本表**只说明「结构上合不合适」**，不说明「能不能赚钱」。")
    A("   `E[组合] = ΣwᵢE[腿ᵢ]` —— 若三腿各自期望为负，组合期望**也是负**。")
    A("   本筛选能保证的是：三腿互不拖累（分散成立）、资金支持得住（预算不失效）。")
    A("2. `可开手数` 只是「预算算出来的理论手数」，不代表策略会开这么多。")
    A("3. 相关性是 **500 根日线的历史值**，会漂移；换区间会变。")
    A("")
    路径 = 报告路径
    os.makedirs(os.path.dirname(路径), exist_ok=True)
    with open(路径, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return 路径


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--本金", type=float, default=1000000.0)
    ap.add_argument("--止损", type=float, default=1.0)
    a = ap.parse_args()

    组合, 品种 = 算(止损倍数=a.止损, 本金=a.本金)

    print("=" * 78)
    print("三品种组合选型 ｜ 本金 %s 元 ｜ 止损 %.1f×ATR ｜ 风险预算 0.30%%" % (
        f"{a.本金:,.0f}", a.止损))
    print("=" * 78)
    print("\n【够格权益（元）】")
    for k, v in sorted(品种.items(), key=lambda x: x[1]["够格权益"]):
        够 = "✅" if v["够格权益"] <= a.本金 else "❌"
        print("  %-3s %-5s 乘数%-4g 够格 %9s  %s  成本占比 %.2f%%" % (
            k, v["中文"], v["乘数"], f"{v['够格权益']:,.0f}", 够, v["成本占比"] * 100))

    print("\n【Top 15 组合】（平均相关越低越好）")
    print("  %-3s %-22s %8s %8s %12s %6s %s" % (
        "#", "组合", "平均相关", "最大相关", "够格和(万)", "全够格", "成本占比"))
    for i, c in enumerate(组合[:15], 1):
        print("  %-3d %-22s %8.3f %8.3f %12s %6s %.2f%%" % (
            i, "+".join(c["品种"]), c["平均相关"], c["最大相关"],
            f"{c['够格和']/10000:.0f}", "✅" if c["全够格"] else "❌", c["成本占比"] * 100))

    最差 = sum(1 for c in 组合 if not c["全够格"])
    print("\n  共 %d 个组合，其中三腿全够格 %d 个、有腿超预算 %d 个。" % (
        len(组合), len(组合) - 最差, 最差))

    p = _写报告(组合, 品种, a.止损, a.本金)
    print("\n报告已生成：%s" % p)
    try:
        with open(json路径, "w", encoding="utf-8") as f:
            json.dump({"止损倍数": a.止损, "本金": a.本金,
                       "品种": 品种, "组合": 组合[:60]},
                      f, ensure_ascii=False, indent=1, default=str)
        print("数字已存：%s" % json路径)
    except Exception as e:
        print("[警告] json 落盘失败：%s" % e)
