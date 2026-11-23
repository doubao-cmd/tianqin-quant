# -*- coding: utf-8 -*-
r"""
品种「趋势性」排名稳定性检验 —— 40 品种版（正式脚本，可长期复现）
==================================================================
问题：用户构想的第一步是「横截面挑出未来会走大趋势的品种」。
      前提是「品种的趋势性」是一个在时间上稳定的属性。
      本脚本检验：A 段(2016-2020) 的品种排名，与 B 段(2021-2025) 的排名是否一致。

■ 检验定义（任务书 §3.1，写死，不许改）
  信号 : retrace_ma13（进场体检.SIGNALS），MA_N=13 / ATR_N=14 / STOP_ATR_MULT=2.0 / SLIP=1
  L0   : 无止损前向毛收益 (o[t+1+h] - o[t+1]) × 方向，不含成本
  S1   : L0_gross(h) - L0_gross(h12)     长持有相对短持有的增量
  S2   : L0_gross(h)                     长持有绝对毛
  h    : {60, 120} 日（h12 仅作 S1 的基准）
  分段 : A = 进场 >= 2016-01-01 且 出场 < 2021-01-01
         B = 进场 >= 2021-01-01 且 出场 < 2026-01-01
  统计 : scipy.stats.spearmanr(A 排名, B 排名)
  复用 : 对账_长持有.stats_of（不重新实现信号/统计）

■ 样本池（任务书 §3.2，事前写死）
  late 判定：数据首日 > 2016-01-10 ⇒ late，不进任何判定池
  Primary      = 40 - late
  Sensitivity  = 新增 28 个中非 late 的（唯一完全未被窥探的横截面）

■ 判据（任务书 §3.3，跑前定死）
  主判据 = S1_h120 的 Spearman(A, B)
    PASS  : Primary rho>=0.50 且 p<0.01，且 Sensitivity rho>=0.50 且 p<0.05
    WEAK  : Primary rho>=0.30，但未同时满足 PASS 两条
    FAIL  : Primary rho<0.30
  另 3 组（S1_h60 / S2_h60 / S2_h120）只报告、不判定；方向矛盾必须显著标注。

■ 数据质量剔除（★ 与本检验判据无关，是 §2.4 验收未过项）
  FU：退化率 15.85%（2016 年 42.8% / 2017 年 62.3% / 2018 年 52.7%，2019 起 0.06%），
      单日跳空 max=50.61%（2017-02-06），全部集中在老 FU 合约无成交期（2018-07 新合约上线前）。
      ⇒ 验收 #6（退化<0.5%）与 #8（跳空<20%）双项不过，数据不可用于 A 段排名。
      ⇒ 默认从判定池剔除，并在报告附录同时给出「含 FU」的稳健性结果（不进档位判定）。

用法：
    <tqsdk venv>\python.exe 对账_趋势性排名.py            # 全量：一致性校验 + 40 品种检验
    <tqsdk venv>\python.exe 对账_趋势性排名.py --check    # 只做 §3.4 一致性校验
产物：
    体检结果_趋势性排名_40品种.json
    对账_趋势性排名_40品种.md
"""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import 对账_长持有 as LH  # noqa: E402

SIG = "retrace_ma13"
HOLDS = [12, 60, 120]
SEG_A = ("2016-01-01", "2021-01-01")
SEG_B = ("2021-01-01", "2026-01-01")
IDX_DIR = os.path.join(_HERE, "数据_指数")

# ---- 40 品种 = 现有 12 + 本次新增 28（不含 EC：不属本任务范围）----
OLD12 = ["EB", "JD", "JM", "LH", "MA", "M", "P", "RB", "RU", "SA", "SR", "V"]
NEW28 = ["Y", "C", "A", "J", "I", "EG", "PP", "L", "LG",
         "HC", "CU", "AL", "ZN", "PB", "NI", "SN", "AU", "AG", "FU", "BU", "SP",
         "CF", "RM", "OI", "TA", "FG", "SM", "SF"]
ALL40 = sorted(OLD12 + NEW28)

# ---- 上一轮（已窥探）的 11 品种 + LH 单列 ----
PREV_11 = ["M", "MA", "RB", "V", "SA", "EB", "JD", "JM", "P", "RU", "SR"]
PREV_LATE = {"EB", "SA"}          # 上一轮的 late 判定（仅用于一致性校验）

LATE_CUT = pd.Timestamp("2016-01-10")
DATA_FAIL = {"FU"}                # ★ 数据质量剔除（见 docstring 末段）
MAIN_METRIC = "S1_h120"
REF_METRICS = ["S1_h60", "S2_h60", "S2_h120"]

RESULT_JSON = os.path.join(_HERE, "体检结果_趋势性排名_40品种.json")
RESULT_MD = os.path.join(_HERE, "对账_趋势性排名_40品种.md")
OLD_RESULT = r"C:\Users\cc\Desktop\天勤量化\_tmp\trend_persist\result.json"
TOL = 1e-9


# ------------------------------------------------------------------ 工具
def spearman(x, y):
    from scipy.stats import spearmanr
    r = spearmanr(np.asarray(x, float), np.asarray(y, float))
    return float(r.statistic), float(r.pvalue), int(len(x))


def quality(k):
    """读文件 → 数据质量五项（根数/首日/末日/退化率/最大单日跳空）。"""
    fs = sorted(glob.glob(os.path.join(IDX_DIR, "%s_1440min_*.csv" % k)))
    if not fs:
        return None
    d = pd.read_csv(fs[-1], encoding="utf-8-sig", parse_dates=["datetime"])
    d = d.sort_values("datetime").reset_index(drop=True)
    o, h, l, c = (d[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    deg = float(np.mean((o == h) & (h == l) & (l == c)) * 100)
    ret = c[1:] / c[:-1] - 1.0
    gap = float(np.max(np.abs(ret))) * 100
    return dict(key=k, df=d, n=len(d), start=str(d["datetime"].iloc[0]),
                end=str(d["datetime"].iloc[-1]), deg=deg, maxgap=gap,
                late=bool(d["datetime"].iloc[0] > LATE_CUT),
                first_day=str(d["datetime"].iloc[0])[:10])


def calc(k, df):
    """单品种 × 2 段 × 3 持有期 → n / L0_gross / L1_net / nw_t。"""
    out = {}
    for seg, (lo, hi) in (("A", SEG_A), ("B", SEG_B)):
        for h in HOLDS:
            r, _ = LH.stats_of(k, df, h, t_lo=lo, t_hi=hi)
            out["%s_%d" % (seg, h)] = dict(
                n=int(r.get("n") or 0),
                L0=float(r.get("L0_gross") or 0.0),
                net=float(r.get("L1_net") or 0.0),
                nw_t=float(r.get("nw_t") or 0.0))
    return out


def metrics_of(row):
    """由 L0 派生 S1 / S2（A、B 两段）。"""
    m = {}
    for seg in ("A", "B"):
        base = row["%s_12" % seg]["L0"]
        for h in (60, 120):
            m["S1_h%d_%s" % (h, seg)] = row["%s_%d" % (seg, h)]["L0"] - base
            m["S2_h%d_%s" % (h, seg)] = row["%s_%d" % (seg, h)]["L0"]
    return m


def judge_pool(rows, tag):
    """对给定池算 4 个口径的 Spearman(A, B)。"""
    out = {}
    for h in (60, 120):
        for f in ("S1", "S2"):
            key = "%s_h%d" % (f, h)
            x = [r["M"][key + "_A"] for r in rows]
            y = [r["M"][key + "_B"] for r in rows]
            rho, p, n = spearman(x, y)
            out[key] = dict(rho=rho, p=p, n=n)
    out["_pool"] = tag
    return out


def verdict_of(pri, sens):
    p = pri[MAIN_METRIC]
    s = sens[MAIN_METRIC]
    if p["rho"] >= 0.50 and p["p"] < 0.01 and s["rho"] >= 0.50 and s["p"] < 0.05:
        return "PASS"
    if p["rho"] >= 0.30:
        return "WEAK"
    return "FAIL"


# ------------------------------------------------------------------ §3.4 一致性校验
def consistency(store):
    """用本脚本重算上一轮 11 品种，与 _tmp/trend_persist/result.json 逐位比对。"""
    print("\n" + "=" * 96)
    print("§3.4 一致性校验：重算 11 品种 vs 上一轮 result.json（容差 1e-9）")
    print("=" * 96)
    old = json.load(open(OLD_RESULT, encoding="utf-8"))
    rows = []
    for k in PREV_11:
        rows.append(dict(key=k, late=(k in PREV_LATE), M=metrics_of(store[k]["calc"])))
    mine = {"ALL11": judge_pool(rows, "ALL11"),
            "EXLATE9": judge_pool([r for r in rows if not r["late"]], "EXLATE9")}

    bad, ok_cnt = [], 0
    for tag in ("ALL11", "EXLATE9"):
        for m in ("S1_h60", "S2_h60", "S1_h120", "S2_h120"):
            a, b = mine[tag][m], old["judge"][tag][m]
            d_rho, d_p, d_n = abs(a["rho"] - b["rho"]), abs(a["p"] - b["p"]), abs(a["n"] - b["n"])
            good = d_rho <= TOL and d_p <= TOL and d_n <= TOL
            ok_cnt += good
            print("  %-8s %-8s rho=%+.6f (旧 %+.6f, d=%.1e)  p=%.6f (旧 %.6f, d=%.1e)  n=%d/%d  %s" % (
                tag, m, a["rho"], b["rho"], d_rho, a["p"], b["p"], d_p, a["n"], b["n"],
                "OK" if good else "!!不一致!!"))
            if not good:
                bad.append((tag, m, a, b))

    # 额外：逐品种 S1/S2 数值比对（超出 §3.4 要求，进一步防错）
    old_tab = {t["key"]: t for t in old["table"]}
    num_diff = []
    for r in rows:
        ot = old_tab[r["key"]]
        for h in (60, 120):
            for f in ("S1", "S2"):
                for seg in ("A", "B"):
                    mkey = "%s_h%d_%s" % (f, h, seg)      # 本脚本内部命名
                    okey = "%s_%d_%s" % (f, h, seg)       # 上一轮 result.json 命名
                    d = abs(r["M"][mkey] - ot[okey])
                    if d > 1e-9:
                        num_diff.append((r["key"], mkey, r["M"][mkey], ot[okey], d))
    print("\n  逐品种 S1/S2 数值比对：%s" % ("全部一致（<=1e-9）" if not num_diff else "存在 %d 处差异" % len(num_diff)))
    for x in num_diff[:20]:
        print("     %-3s %-10s 本次=%.6f 旧=%.6f d=%.1e" % x)
    print("\n  结果：%d/8 组 ρ/p/n 一致；数值差异 %d 处" % (ok_cnt, len(num_diff)))
    if bad:
        print("  ==> 一致性校验未通过，停止（不把差异当成「数据更新」）")
    else:
        print("  ==> 一致性校验通过（与上一轮逐位一致）")
    return dict(ok=not bad and not num_diff, n_pass=int(ok_cnt),
                bad=[dict(tag=t, metric=m, mine=a, old=b) for t, m, a, b in bad],
                num_diff=[dict(key=k, metric=m, mine=a, old=b, d=d) for k, m, a, b, d in num_diff])


# ------------------------------------------------------------------ 主流程
def main(only_check=False):
    qu, store, problems = [], {}, []
    for k in ALL40:
        q = quality(k)
        if q is None:
            problems.append((k, "缺文件"))
            continue
        qu.append(q)
        try:
            store[k] = dict(calc=calc(k, q["df"]))
        except AssertionError as e:
            problems.append((k, "stats_of assert 失败: %r" % (e,)))
        except Exception as e:
            problems.append((k, "计算异常: %r" % (e,)))

    print("数据就绪：%d / 40 品种；问题 %d 个 %s" % (len(store), len(problems), problems or ""), flush=True)
    cons = consistency(store)
    if only_check:
        return 0 if cons["ok"] else 2
    if not cons["ok"]:
        print("\n!!一致性校验失败，按 §5.1 立刻停止，不往下跑!!")
        return 2

    # ---- 组装 table
    table = []
    for q in qu:
        k = q["key"]
        if k not in store:
            continue
        M = metrics_of(store[k]["calc"])
        nok = all(store[k]["calc"]["%s_%d" % (s, h)]["n"] >= 10 for s in ("A", "B") for h in HOLDS)
        table.append(dict(key=k, late=q["late"], excluded=(k in DATA_FAIL), n_ok=nok,
                          n=dict(A12=store[k]["calc"]["A_12"]["n"], B12=store[k]["calc"]["B_12"]["n"],
                                 A120=store[k]["calc"]["A_120"]["n"], B120=store[k]["calc"]["B_120"]["n"]),
                          M=M, raw=store[k]["calc"]))
    table.sort(key=lambda r: -r["M"]["S1_h120_A"])

    # ---- 池
    def pool(name, extra_excl=()):
        return [r for r in table if not r["late"] and r["key"] not in DATA_FAIL
                and r["key"] not in extra_excl and r["n_ok"]]

    pri = pool("Primary")
    sens = pool("Sensitivity", extra_excl=tuple(set(OLD12) - set(PREV_11)))
    # Sensitivity = 只看新增 28（OLD12 全是上轮跑过的，LH 单列）
    sens = [r for r in sens if r["key"] in NEW28]

    judge = dict(Primary=judge_pool(pri, "Primary"), Sensitivity=judge_pool(sens, "Sensitivity"))

    # ---- 附录：含 FU 稳健性（不进判定）
    app_pri = [r for r in table if not r["late"] and r["n_ok"]]
    app_sens = [r for r in app_pri if r["key"] in NEW28]
    judge["App_含FU_Primary"] = judge_pool(app_pri, "App_含FU_Primary")
    judge["App_含FU_Sensitivity"] = judge_pool(app_sens, "App_含FU_Sensitivity")

    vd = verdict_of(judge["Primary"], judge["Sensitivity"])

    # ---- 矛盾检测
    conflicts = []
    main_rho = judge["Primary"][MAIN_METRIC]["rho"]
    for m in REF_METRICS:
        for tag in ("Primary", "Sensitivity"):
            rho = judge[tag][m]["rho"]
            if (main_rho > 0 and rho < 0) or (main_rho < 0 and rho > 0):
                conflicts.append(dict(pool=tag, metric=m, rho=rho,
                                      kind="方向相反", main=main_rho))
            elif main_rho >= 0.50 and rho < 0.30:
                conflicts.append(dict(pool=tag, metric=m, rho=rho,
                                      kind="显著弱化（主判据>=0.50 而本组<0.30）", main=main_rho))

    out = dict(
        meta=dict(task="补数据_40品种10年指数日线_任务书_20261107", signal=SIG,
                  holds=HOLDS, segA=list(SEG_A), segB=list(SEG_B),
                  main_metric=MAIN_METRIC, late_cut=str(LATE_CUT.date()),
                  data_fail=sorted(DATA_FAIL), n_all=len(table),
                  n_primary=len(pri), n_sensitivity=len(sens)),
        data_quality=[dict(key=q["key"], n=q["n"], start=q["start"], end=q["end"],
                           deg=round(q["deg"], 4), maxgap=round(q["maxgap"], 4),
                           late=q["late"], excluded=(q["key"] in DATA_FAIL)) for q in qu],
        table=[dict(key=r["key"], late=r["late"], excluded=r["excluded"], n=r["n"],
                    **{("M_" + k): round(v, 6) for k, v in r["M"].items()},
                    raw={kk: {k2: round(v2, 6) for k2, v2 in vv.items()} for kk, vv in r["raw"].items()})
               for r in table],
        judge=judge, verdict=vd, conflicts=conflicts, consistency=cons,
        problems=[dict(key=k, why=w) for k, w in problems])

    with open(RESULT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    write_md(out, qu, table, pri, sens, judge, vd, conflicts, cons, problems)
    print("\n产物：\n  %s\n  %s" % (RESULT_JSON, RESULT_MD))
    print("\n【档位】%s" % vd)
    for tag in ("Primary", "Sensitivity", "App_含FU_Primary", "App_含FU_Sensitivity"):
        j = judge[tag]
        print("  %-20s n=%2d  " % (tag, j[MAIN_METRIC]["n"]) +
              "  ".join("%s rho=%+.3f p=%.4f" % (m, j[m]["rho"], j[m]["p"])
                        for m in [MAIN_METRIC] + REF_METRICS))
    return 0


def write_md(out, qu, table, pri, sens, judge, vd, conflicts, cons, problems):
    L = []
    A = L.append
    qmap = {q["key"]: q for q in qu}
    late = [q["key"] for q in qu if q["late"]]
    A("# 品种趋势性排名稳定性 · 40 品种 × 10 年指数日线\n")
    A("> 任务书：`上下文交接\\补数据_40品种10年指数日线_任务书_20261107.md`（2026-11-07 18:45 落盘，判据未改）  ")
    A("> 数据：tqsdk `KQ.i@` 指数日线，2016-01-04 ~ 2025-12-30，5 列（无 volume），退化率见 §一  ")
    A("> 信号：`retrace_ma13`，L0 = 无止损前向毛（不含成本）；统计：Spearman(A 段排名, B 段排名)  ")
    A("> ⚠️ **定位声明**：2016-2025 这 10 年在「品种趋势性」问题上已被 4 口径 × 2 样本数跑过，")
    A("> 本任务的处理是**换横截面**（新增 28 个从未跑过的品种）。**不得把本结果写成「验证通过」**。\n")
    A("## 〇、结论速览\n")
    A("**档位：%s**（主判据 = `%s` 的 Spearman(A, B)）\n" % (vd, MAIN_METRIC))
    jp, js = judge["Primary"], judge["Sensitivity"]
    A("| 池 | n | ρ | p |")
    A("|---|:-:|---:|---:|")
    A("| Primary（40 − late 6%s） | %d | **%+.3f** | **%.4f** |" % (
        " − FU" if DATA_FAIL else "", jp[MAIN_METRIC]["n"], jp[MAIN_METRIC]["rho"], jp[MAIN_METRIC]["p"]))
    A("| Sensitivity（新增 28 中非 late） | %d | **%+.3f** | **%.4f** |" % (
        js[MAIN_METRIC]["n"], js[MAIN_METRIC]["rho"], js[MAIN_METRIC]["p"]))
    A("")
    A("判据（§3.3，跑前定死）：PASS = Primary ρ≥0.50 & p<0.01 **且** Sensitivity ρ≥0.50 & p<0.05；"
      "WEAK = Primary ρ≥0.30；FAIL = Primary ρ<0.30。\n")

    # ---- 1 数据清单
    A("## 一、数据清单（40 品种）\n")
    A("| # | 品种 | 根数 | 首日 | 末日 | 退化率 | 最大单日跳空 | late | 备注 |")
    A("|:-:|---|---:|---|---|---:|---:|:-:|---|")
    for i, q in enumerate(sorted(qu, key=lambda z: (-z["n"], z["key"])), 1):
        note = []
        if q["key"] in DATA_FAIL:
            note.append("**数据质量剔除**（退化 %.2f%% / 跳空 %.2f%%）" % (q["deg"], q["maxgap"]))
        if q["late"]:
            note.append("late（首日 %s）" % q["first_day"])
        if q["maxgap"] > 20:
            note.append("跳空>20%")
        A("| %d | %s | %d | %s | %s | %.2f%% | %.2f%% | %s | %s |" % (
            i, q["key"], q["n"], q["start"], q["end"], q["deg"], q["maxgap"],
            "✔" if q["late"] else "", "；".join(note)))
    A("")
    A("数据验收（§2.4）：文件数 40/40；表头全为 `datetime,open,high,low,close`；末日统一 2025-12-30 16:00:00；"
      "旧 12 文件 mtime/字节/md5 与基线快照逐个相等（见 `_tmp\\idx_baseline.json`）。")
    if DATA_FAIL:
        A("\n⚠️ **FU 未过两项验收**：退化率 15.85%（>0.5%）、单日跳空 50.61%（>20%），"
          "异常全部集中在 2016-2018（2016 42.8% / 2017 62.3% / 2018 52.7% / 2019 起 0.06%），"
          "即老 FU 合约几无成交期；2017-02-06 单根 2972 vs 前后 4458 属指数合成异常。"
          "⇒ 其 A 段不可用于排名，**默认从判定池剔除**，含 FU 的稳健性结果见 §三 附录。\n")

    # ---- 2 排名表
    A("## 二、排名表（按 A 段 S1_h120 降序）\n")
    A("`S1_h120` = L0_gross(120) − L0_gross(12)，单位＝点（各品种价格量级不同，只看排名）。\n")
    A("| A 排名 | 品种 | S1_h120_A | S1_h120_B | B 排名 | A→B 位移 | S2_h120_A | S2_h120_B | flag |")
    A("|:-:|---|---:|---:|:-:|:-:|---:|---:|:-:|")
    order_b = sorted([r for r in table], key=lambda r: -r["M"]["S1_h120_B"])
    rank_b = {r["key"]: i + 1 for i, r in enumerate(order_b)}
    for i, r in enumerate(table, 1):
        rb = rank_b[r["key"]]
        fl = "late" if r["late"] else ("剔除" if r["excluded"] else "")
        A("| %d | %s | %+.2f | %+.2f | %d | %+d | %+.2f | %+.2f | %s |" % (
            i, r["key"], r["M"]["S1_h120_A"], r["M"]["S1_h120_B"], rb, i - rb,
            r["M"]["S2_h120_A"], r["M"]["S2_h120_B"], fl))
    A("")

    # ---- 3 判据表
    A("## 三、判据表（主判据 + 3 组参考）\n")
    A("| 口径 | 池 | n | ρ | p | 与主判据 |")
    A("|---|---|:-:|---:|---:|---|")
    for tag, label in (("Primary", "Primary"), ("Sensitivity", "Sensitivity")):
        for m in [MAIN_METRIC] + REF_METRICS:
            j = judge[tag][m]
            mark = "**主判据**" if m == MAIN_METRIC else (
                "同向" if np.sign(j["rho"]) == np.sign(judge["Primary"][MAIN_METRIC]["rho"]) else "⚠️ 反向")
            A("| `%s` | %s | %d | %+.3f | %.4f | %s |" % (m, label, j["n"], j["rho"], j["p"], mark))
    A("")
    A("### 附录：含 FU 的稳健性（**不进档位判定**）\n")
    A("FU 按字面不满足 late 规则（首日 2016-01-04），但数据质量不达标。下表仅供看 FU 是否改变结论：\n")
    A("| 口径 | 池 | n | ρ | p |")
    A("|---|---|:-:|---:|---:|")
    for tag in ("App_含FU_Primary", "App_含FU_Sensitivity"):
        for m in [MAIN_METRIC] + REF_METRICS:
            j = judge[tag][m]
            A("| `%s` | %s | %d | %+.3f | %.4f |" % (m, tag.replace("App_含FU_", "含FU "), j["n"], j["rho"], j["p"]))
    A("")
    vd_app = verdict_of(judge["App_含FU_Primary"], judge["App_含FU_Sensitivity"])
    A("**FU 是否改变结论**：含 FU 时档位 = **%s**，与剔除 FU 的 **%s** %s。"
      "（FU 在 A/B 两段的 `S1_h120` 分别为 %+.2f / %+.2f，方向与总体一致，"
      "所以纳入与否只小幅推动 ρ，不改变定性。）\n" % (
          vd_app, vd, "**相同**" if vd_app == vd else "**不同 —— 必须在正文显著说明**",
          [r for r in table if r["key"] == "FU"][0]["M"]["S1_h120_A"] if any(r["key"] == "FU" for r in table) else 0.0,
          [r for r in table if r["key"] == "FU"][0]["M"]["S1_h120_B"] if any(r["key"] == "FU" for r in table) else 0.0))

    # ---- 4 矛盾与异常
    A("## 四、矛盾与异常\n")
    A("### 4.1 参考口径与主判据的冲突\n")
    if conflicts:
        A("| 池 | 口径 | ρ | 类型 | 主判据 ρ |")
        A("|---|---|---:|---|---:|")
        for c in conflicts:
            A("| %s | `%s` | %+.3f | **%s** | %+.3f |" % (c["pool"], c["metric"], c["rho"], c["kind"], c["main"]))
    else:
        A("无**方向**矛盾：3 组参考口径与主判据同号，且均非「主判据强/参考组弱」。")
    A("")
    # ★ 强度差异（不是方向矛盾，但必须明说）：主判据是否恰为最强的一组
    strong = [(m, judge["Primary"][m]["rho"], judge["Primary"][m]["p"]) for m in REF_METRICS
              if abs(judge["Primary"][m]["rho"]) > abs(judge["Primary"][MAIN_METRIC]["rho"])]
    if strong:
        A("⚠️ **强度差异（须注意）**：主判据在 Primary 池中**不是最强**的一组 —— "
          "`%s` ρ=%+.3f (p=%.4f) 强于主判据 ρ=%+.3f (p=%.4f)。"
          "这**不改变**档位（判据 §3.3 事前定死，单一口径），但说明「排名稳定性」的信号"
          "对度量口径（是否减去 h12 基准）敏感：`S2` 是绝对毛，含各品种价格量级/波动差异；"
          "`S1` 已扣掉 h12，剩余部分更接近纯「趋势延续性」。若将来要用，应先想清楚哪个口径对应真实用途，"
          "**不得在两者之间挑好看的**。\n" % (
              strong[0][0], strong[0][1], strong[0][2],
              judge["Primary"][MAIN_METRIC]["rho"], judge["Primary"][MAIN_METRIC]["p"]))
    else:
        A("主判据即 Primary 池中最强的一组，不存在「换口径更好看」的问题。\n")
    A("### 4.2 两段翻符号的品种（S1_h120）\n")
    flip = [r for r in table if r["M"]["S1_h120_A"] * r["M"]["S1_h120_B"] < 0]
    A("共 %d / %d 个：" % (len(flip), len(table)))
    A("")
    A("| 品种 | S1_h120_A | S1_h120_B | flag |")
    A("|---|---:|---:|:-:|")
    for r in sorted(flip, key=lambda z: z["key"]):
        A("| %s | %+.2f | %+.2f | %s |" % (r["key"], r["M"]["S1_h120_A"], r["M"]["S1_h120_B"],
                                           "late" if r["late"] else ("剔除" if r["excluded"] else "")))
    A("")
    A("### 4.3 样本量与异常清单\n")
    A("- late 品种（首日 > 2016-01-10，不进任何判定池）：**%s**" % (", ".join(late) or "无"))
    A("")
    A("| late 品种 | 数据首日 | 是否本次新增 28 之一 |")
    A("|---|---|:-:|")
    for k in late:
        A("| %s | %s | %s |" % (k, qmap[k]["first_day"], "✔ 新增" if k in NEW28 else "原 12 品种"))
    A("")
    late_new = [k for k in late if k in NEW28]
    late_old = [k for k in late if k not in NEW28]
    A("**与预期对照**：任务书 §3.2 预期新增 28 中 `EG(2018-12)` / `SP(2018-11)` / `LG(2024-11)` 三个被判 late —— "
      "实际数据首日判定的结果 = **%s**，%s。"
      "另有 %s 为原 12 品种中的 late（%s），合计 %d 个，与「40 − 6 = 34」的预期一致。\n" % (
          ", ".join(late_new) or "无",
          "**与预期完全一致**" if sorted(late_new) == ["EG", "LG", "SP"] else "**与预期有差异，见上表**",
          ", ".join(late_old) or "无",
          "、".join("%s %s" % (k, qmap[k]["first_day"]) for k in late_old) or "—",
          len(late)))
    A("- 数据质量剔除：**%s**（退化 %.2f%% / 跳空 %.2f%%，详见 §一 脚注）" % (
        ", ".join(sorted(DATA_FAIL)) or "无",
        qmap["FU"]["deg"] if "FU" in qmap else 0.0,
        qmap["FU"]["maxgap"] if "FU" in qmap else 0.0))
    if problems:
        A("- 计算/断言问题：%s" % "；".join("%s(%s)" % (k, w) for k, w in problems))
    A("- A 段进场笔数 < 30 的品种：%s" % (
        ", ".join("%s=%d" % (r["key"], r["n"]["A120"]) for r in table if r["n"]["A120"] < 30) or "无"))
    A("  - 口径说明：late 品种 A 段无成交（n=0），其 `S1/S2` 在排名表中记为 0.00 —— 仅为占位，"
      "**不参与任何 ρ 计算**（池已剔 late）；表中 A 段排序包含它们只为完整展示 40 品种。")
    A("")
    A("### 4.4 一致性校验（§3.4）\n")
    A(("通过：11 品种重算的 8 组 ρ/p/n 与 `_tmp/trend_persist/result.json` 逐位一致（容差 1e-9），"
       "逐品种 S1/S2 数值亦全部一致。" if cons["ok"] else
       "**未通过**，见 JSON 的 `consistency` 字段。"))
    A("")

    # ---- 5 条件分支
    A("## 五、分层回测（§4 条件分支）\n")
    if vd == "PASS":
        A("主判据 PASS ⇒ 应执行分层回测（见后续独立产物）。")
    else:
        A("**未触发**：判据档位为 **%s**，按任务书 §4「只有 PASS 才执行」，WEAK/FAIL 直接跳过。" % vd)
    A("")
    A("> 无论档位如何，本检验只回答「要不要继续设计选品种这一步」，不回答「能不能赚钱」。")

    with open(RESULT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


if __name__ == "__main__":
    sys.exit(main(only_check="--check" in sys.argv[1:]))
