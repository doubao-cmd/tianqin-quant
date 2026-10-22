# -*- coding: utf-8 -*-
"""
E4 · vol targeting OOS（预注册卡 2026-10-15；任务书 P1' 2026-10-20）
==============================================================
目的：在「干净样本外」上验证 (c) 波动率目标 / (d) 风险平价+vol目标 的方向稳定性。

★★ 数据污染裁定（执行前取证，见下方注释）：
   1. 展期_合约日线.csv 的 2022+ 段从未被任何 vol targeting 脚本读取：
      - E3 口径B稳健性 `load_B` 显式 `if d > "2021-12-31": continue`（源码 L44），只用 2016-2021；
      - E1/E2 用 主连 `*_1440min_*_2021-12-31.csv`，截止 2021-12-31；
      - `_vol_targeting/` 下无任何 OOS 结果文件。
   2. 本脚本是 2022-2026 OOS 的【第一次也是唯一一次】计算（四铁律③：OOS 只跑一次，看过即污染）。
   ⇒ 裁定：OOS 数据源【干净】，可盲跑。

★★ 两处已知偏离（判平从严，必须明示）：
   A. 篮子：任务书设想「9 品种」，但展期 csv 缺 MA/RB，仅 7 品种 {JD,JM,M,P,RU,SR,V} 可用。
   B. 口径：E1 的 IS 是口径A(主连)9品；本 OOS 用口径B(真实主力原始close,未扣换月成本)7品。
      ⇒ 故 IS↔OOS 不能比绝对夏普，只能在同一脚本内用【同口径同篮子】做 IS(2016-2021)→OOS(2022-2026) 相对对照。

冻结项（全外生，不扫）：target=0.15/√252，WIN=20，LEV_CAP=3，COST_RT=0.0003。
无未来函数：lev[t] 用 rp[t-WIN:t]（不含 t）。
"""
import csv, math, os
import numpy as np
import _roll_loader

LOAD_SYM = {}        # key -> {date: sym}  审计用
LOAD_SKIPPED = []    # [(key, date, reason, detail)]

ROLL_CSV = "工具/_数据/展期_合约日线.csv"
OUT_DIR = "工具/_vol_targeting"
KEYS = ["JD", "JM", "M", "P", "RU", "SR", "V"]   # 展期 csv 实际含的 7 个 QUAL 品种
TARGET = 0.15 / math.sqrt(252)
WIN = 20
LEV_CAP = 3.0
COST_RT = 0.0003
IS_END = "2021-12-31"
OOS_START = "2022-01-01"


def load(key):
    """返回 {date: (sym, close)}（口径B：真实主力原始 close，未扣换月成本）。
    主力 = 同日 (key,date) 内 volume 最大（缺失退 oi），由共享装载器实现；选中合约代码落盘审计。"""
    out, skipped = _roll_loader.load_roll(key)
    LOAD_SKIPPED.extend(skipped)
    return out


def split_is_oos(close_map):
    isd, osd = {}, {}
    for d, (sym, c) in close_map.items():
        if d <= IS_END:
            isd[d] = c
        elif d >= OOS_START:
            osd[d] = c
    return isd, osd


def build_returns(close_maps, keys):
    """对齐共同交易日，返回 (R (N,T), dates)。"""
    common = None
    for k in keys:
        s = set(close_maps[k].keys())
        common = s if common is None else (common & s)
    common = sorted(common)
    R = np.array([[close_maps[k][d] for d in common] for k in keys])
    R = R[:, 1:] / R[:, :-1] - 1.0
    return R, common[1:]


def weights(R):
    """返回 (w_a, w_b, lev) 三种权重/杠杆，全部基于 t-1 及之前（无未来函数）。"""
    N, T = R.shape
    w_a = np.full((N, T), 1.0 / N)
    MAXW = 0.40
    w_b = np.zeros((N, T))
    for t in range(T):
        if t < WIN:
            w_b[:, t] = 1.0 / N
            continue
        sig = np.array([R[i, t - WIN:t].std(ddof=1) for i in range(N)])
        floor = 0.5 * np.median(sig)
        sig = np.maximum(sig, floor)
        w = 1.0 / sig; w /= w.sum(); w = np.minimum(w, MAXW); w /= w.sum()
        w_b[:, t] = w
    rp = R.mean(axis=0)
    roll = np.full(T, np.nan)
    for t in range(T):
        if t >= WIN:
            roll[t] = rp[t - WIN:t].std(ddof=1)
    lev = np.clip(np.where(np.isnan(roll), 1.0, TARGET / roll), 0, LEV_CAP)
    return w_a, w_b, lev


def metrics(rets):
    rets = np.asarray(rets, float)
    n = len(rets)
    if n < 2:
        return dict(sharpe=np.nan, ann_vol=np.nan, mdd=np.nan, ann_geo=np.nan, n=n)
    eq = np.cumprod(1.0 + rets)
    peak = np.maximum.accumulate(eq)
    mdd = float((eq / peak - 1.0).min())
    vol = rets.std(ddof=1) * math.sqrt(252)
    sharpe = rets.mean() / rets.std(ddof=1) * math.sqrt(252)
    ann = eq[-1] ** (252.0 / n) - 1.0
    return dict(sharpe=sharpe, ann_vol=vol, mdd=mdd, ann_geo=ann, n=n)


def yearly(rets, dates):
    yrs = [d[:4] for d in dates]
    out = {}
    for y in sorted(set(yrs)):
        m = np.array([y == yy for yy in yrs])
        if m.sum() > 30:
            out[y] = metrics(rets[m])
    return out


def run_window(R, dates, label):
    """对给定窗口 R(N,T) 计算 (a)/(b)/(c)/(d) 毛夏普，返回结果 dict。"""
    N, T = R.shape
    w_a, w_b, lev = weights(R)
    rp = R.mean(axis=0)
    # (a) 等权
    ra = rp.copy()
    # (b) 风险平价
    rb = (w_b * R).sum(axis=0)
    # (c) 波动率目标（等权组合施加杠杆，lev[t-1] 用于 t）
    rc = np.zeros(T)
    for t in range(T):
        rc[t] = lev[t - 1] * rp[t] if t > 0 else rp[t]
    # (d) 风险平价 + 同一杠杆
    rd = np.zeros(T)
    for t in range(T):
        rd[t] = lev[t - 1] * rb[t] if t > 0 else rb[t]
    # 净（含 3bps 往返成本）
    def net(g, lev_seq):
        turn = np.zeros(T)
        for t in range(1, T):
            turn[t] = abs(lev_seq[t] - lev_seq[t - 1])
        return g - turn * COST_RT
    nc = net(rc, lev); nd = net(rd, lev)
    ma, mb, mc, md = metrics(ra)["sharpe"], metrics(rb)["sharpe"], metrics(rc)["sharpe"], metrics(rd)["sharpe"]
    nac, ndc = metrics(nc)["sharpe"], metrics(nd)["sharpe"]
    # 方向稳定性：逐年 (c)-(a) 毛夏普增量
    ya = yearly(ra, dates); yc = yearly(rc, dates)
    inc = {y: yc[y]["sharpe"] - ya[y]["sharpe"] for y in ya}
    pos = sum(1 for v in inc.values() if v >= 0)
    neg_bad = [y for y, v in inc.items() if v < -0.05]
    stable = (pos >= max(1, len(inc) - 1) and not neg_bad)
    # 含空头 Period：逐年 (a) 篮子年化
    yret_a = {y: ya[y]["ann_geo"] for y in ya}
    has_short = any(v < 0 for v in yret_a.values())
    return dict(label=label, N=N, T=T,
                sharpe=dict(a=ma, b=mb, c=mc, d=md, nc=nac, nd=ndc),
                inc=inc, yret_a=yret_a, stable=stable, has_short=has_short,
                pos=pos, neg_bad=neg_bad, ra=ra, rc=rc)


# ---- 装载审计 ----
def _audit_load(date_sym_map):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "E4_load_audit.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(["key", "date", "sym"])
        for k in KEYS:
            for d in sorted(date_sym_map[k]):
                w.writerow([k, d, date_sym_map[k][d]])
    with open(os.path.join(OUT_DIR, "E4_roll_summary.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(["key", "year", "roll_count", "roll_dates"])
        for k in KEYS:
            dm = date_sym_map[k]; dates = sorted(dm.keys()); per_year = {}; prev = None
            for d in dates:
                y = d[:4]
                if prev is not None and dm[d] != dm[prev]:
                    per_year.setdefault(y, []).append(d)
                prev = d
            for y in sorted(per_year):
                w.writerow([k, y, len(per_year[y]), ",".join(per_year[y])])
    with open(os.path.join(OUT_DIR, "_load_skipped.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(["key", "date", "reason", "detail"])
        for row in LOAD_SKIPPED:
            w.writerow(row)


# ---- 主流程 ----
maps_is, maps_os = {}, {}
for k in KEYS:
    raw = load(k)
    LOAD_SYM[k] = {d: s for d, (s, c) in raw.items()}
    isd, osd = split_is_oos(raw)
    maps_is[k] = isd; maps_os[k] = osd

# ---- 装载审计落盘（先于断言，确保审计产物落地）----
_audit_load(LOAD_SYM)
print(f"[审计] 选中合约/换月清单/剔除日已落盘 -> {OUT_DIR}/E4_load_audit.csv, E4_roll_summary.csv, _load_skipped.csv")

# ---- 断言 A5 / A6（派单 T2）----
_roll_loader.assert_selection(LOAD_SYM, label="E4")
_roll_loader.assert_roll_freq(LOAD_SYM)
print("[A5/A6] 主力选择一致性 + 换月频率 断言通过")

R_is, d_is = build_returns(maps_is, KEYS)
R_os, d_os = build_returns(maps_os, KEYS)

res_is = run_window(R_is, d_is, "IS 2016-2021")
res_os = run_window(R_os, d_os, "OOS 2022-2026")

print(f"篮子: {KEYS}（{len(KEYS)} 品种，口径B，缺 MA/RB）")
print(f"IS  对齐日: {d_is[0]}~{d_is[-1]}  T={res_is['T']}")
print(f"OOS 对齐日: {d_os[0]}~{d_os[-1]}  T={res_os['T']}\n")

print("=== 毛夏普 ===")
print(f"{'窗口':12s} {'等权(a)':>9s} {'风险平价(b)':>11s} {'vol目标(c)':>11s} {'叠加(d)':>10s}")
for r in (res_is, res_os):
    s = r["sharpe"]
    print(f"{r['label']:12s} {s['a']:9.3f} {s['b']:11.3f} {s['c']:11.3f} {s['d']:10.3f}")
print("\n=== 净夏普（含 3bps 往返成本）===")
print(f"{'窗口':12s} {'vol目标(c)':>11s} {'叠加(d)':>10s}")
for r in (res_is, res_os):
    s = r["sharpe"]
    print(f"{r['label']:12s} {s['nc']:11.3f} {s['nd']:10.3f}")

print("\n=== OOS 方向稳定性（逐年 (c)-(a) 毛夏普增量）===")
for y, v in sorted(res_os["inc"].items()):
    print(f"  {y}: {v:+.3f}  篮子(a)年化 {res_os['yret_a'][y]*100:+.1f}%")
print(f"  增量≥0 年: {res_os['pos']}/{len(res_os['inc'])}  翻车年(<-0.05): {res_os['neg_bad'] or '无'}")
print(f"  「方向稳定」判据: {'PASS' if res_os['stable'] else 'FAIL'}")
print(f"  OOS 含空头 Period（篮子有负年）: {'YES' if res_os['has_short'] else 'NO'}")

# IS→OOS 相对变化（同口径同篮子）
dc_is, dc_os = res_is["sharpe"]["c"], res_os["sharpe"]["c"]
dd_is, dd_os = res_is["sharpe"]["d"], res_os["sharpe"]["d"]
print(f"\n=== IS→OOS 相对（同口径同篮子）===")
print(f"  (c) 毛夏普: IS {dc_is:.3f} → OOS {dc_os:.3f}  (Δ {dc_os-dc_is:+.3f})")
print(f"  (d) 毛夏普: IS {dd_is:.3f} → OOS {dd_os:.3f}  (Δ {dd_os-dd_is:+.3f})")

# 三态结论
oos_c_pos = dc_os > 0
oos_stable = res_os["stable"]
if oos_c_pos and oos_stable and res_os["has_short"]:
    verdict = "PASS（OOS 正向且方向稳定，含空头 Period）"
elif oos_c_pos and not oos_stable:
    verdict = "PARTIAL（OOS 正向但方向不稳定）"
elif not oos_c_pos:
    verdict = "FAIL（OOS 夏普非正）"
else:
    verdict = "INCONCLUSIVE"
print(f"\n★★ 三态结论: {verdict}")

# V3 验收：修正后 OOS (a) 须在 -0.20 ~ -0.40，与 D1 独立重算(-0.246)差>0.1 须停手对账
oos_a = res_os["sharpe"]["a"]
if not (-0.40 <= oos_a <= -0.20):
    print(f"⚠️ [V3] 修正后 OOS (a)={oos_a:.4f} 落在 [-0.40,-0.20] 之外，与 D1 独立重算(-0.246)分歧过大，应停手对账！")
else:
    print(f"[V3] 修正后 OOS (a)={oos_a:.4f} ∈ [-0.40,-0.20]，与 D1(-0.246) 一致 ✅")

# 逐年明细落盘（E4_yearly_修正.csv）
ya = yearly(res_os["ra"], d_os)
yc = yearly(res_os["rc"], d_os)
with open(os.path.join(OUT_DIR, "E4_yearly_修正.csv"), "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    w.writerow(["year", "a_sharpe", "c_sharpe", "c_minus_a", "basket_a_annret_pct"])
    for y in sorted(ya):
        w.writerow([y, f"{ya[y]['sharpe']:.3f}", f"{yc[y]['sharpe']:.3f}",
                    f"{yc[y]['sharpe']-ya[y]['sharpe']:+.3f}", f"{res_os['yret_a'][y]*100:+.1f}"])
print(f"[审计] 逐年明细已落盘 -> {OUT_DIR}/E4_yearly_修正.csv")

# 存档（保留旧版并标 ⛔）
os.makedirs(OUT_DIR, exist_ok=True)
result_path = os.path.join(OUT_DIR, "E4_OOS_2022_2026结果.md")
if os.path.exists(result_path):
    with open(result_path, encoding="utf-8") as _fh:
        old_text = _fh.read()
    with open(os.path.join(OUT_DIR, "E4_OOS_2022_2026结果_旧版⛔.md"), "w", encoding="utf-8") as _fh:
        _fh.write(old_text)

with open(result_path, "w", encoding="utf-8") as f:
    f.write("# E4 · vol targeting OOS 结果（数据修正重算，2026-10-20）\n\n")
    f.write("> ⚠️ **本版为数据修正重算**：原 `load()` 用 `out[date]=close` 覆盖同日多合约，实际取到「当日最远月合约」而非主力（RU 2023-05-10 取到 ru2401=13355，而非 volume 最大 ru2309=12125）。\n")
    f.write("> OOS 已两轮可见（原 bug 版 + 本次修正版），**数字仅用于修正口径，不得用于调参**；vol targeting 已于 `01` §1 定 🚫，不得复活。\n\n")
    f.write("数据：展期_合约日线.csv（口径B 真实主力原始close，volume最大选主力，未扣换月成本，保留换月跳空），7 品种 {JD,JM,M,P,RU,SR,V}（缺 MA/RB）。\n")
    f.write("冻结项：target=15%/√252，WIN=20，LEV_CAP=3，COST_RT=0.0003。无未来函数。\n")
    f.write("污染裁定：OOS 源未被任何 vol targeting 脚本读取过（E3 跳过>2021-12-31；E1/E2 用主连截止2021）。本次为数据修正重算，非新一次 OOS 验证。\n\n")
    f.write(f"| 窗口 | 对齐日 | T | 等权(a) | 风险平价(b) | vol目标(c) | 叠加(d) | vol目标净 | 叠加净 |\n")
    for r in (res_is, res_os):
        s = r["sharpe"]
        f.write(f"| {r['label']} | {d_is[0] if r is res_is else d_os[0]}~{d_is[-1] if r is res_is else d_os[-1]} | {r['T']} | {s['a']:.3f} | {s['b']:.3f} | {s['c']:.3f} | {s['d']:.3f} | {s['nc']:.3f} | {s['nd']:.3f} |\n")
    f.write(f"\nOOS 逐年(c)-(a)增量：\n")
    for y, v in sorted(res_os["inc"].items()):
        f.write(f"- {y}: {v:+.3f}（篮子年化 {res_os['yret_a'][y]*100:+.1f}%）\n")
    f.write(f"\n方向稳定判据: {'PASS' if res_os['stable'] else 'FAIL'}；含空头Period: {'YES' if res_os['has_short'] else 'NO'}\n")
    f.write(f"IS→OOS (c): {dc_is:.3f}→{dc_os:.3f} (Δ{dc_os-dc_is:+.3f})；(d): {dd_is:.3f}→{dd_os:.3f} (Δ{dd_os-dd_is:+.3f})\n")
    f.write(f"\n★★ 三态结论: {verdict}\n")
    f.write("\n### 装载审计（A5/A6 断言已通过）\n")
    f.write(f"- 品种集：{len(KEYS)} 品种 {{{'|'.join(KEYS)}}}（口径B，缺 MA/RB）\n")
    f.write(f"- 价格口径：主力未复权、含换月跳空\n")
    f.write(f"- 窗口：{d_os[0]}~{d_os[-1]}，T={res_os['T']}\n")
    f.write(f"- 选中合约代码明细：E4_load_audit.csv（每日一行）\n")
    f.write(f"- 换月清单/次数·年：E4_roll_summary.csv（每品种每年，须在 8~16 内）\n")
    f.write(f"- 剔除日（vol/oi 皆缺）：{len(LOAD_SKIPPED)} 行，见 _load_skipped.csv\n")
    f.write("\n偏离声明：① 篮子=7 品种（非任务书9，csv 缺 MA/RB）；② 口径B（非 E1 的口径A 主连）。故 IS↔OOS 仅同口径同篮子相对对照，不可与 E1 绝对夏普直接比。\n")
    f.write("按任务书红线：未补数据/未调窗口；**不宣称 vol targeting 有效**（`01` §1 已定 🚫）。\n")
    f.write("\n> ⛔ 旧版（口径 bug）见 `E4_OOS_2022_2026结果_旧版⛔.md`，不可引用。\n")
print("\n结果已存档 -> 工具/_vol_targeting/E4_OOS_2022_2026结果.md")
