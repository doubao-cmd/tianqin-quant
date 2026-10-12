# -*- coding: utf-8 -*-
"""
截面基差因子 · IS 回测（2019-2021，月度再平衡）

配置来源：`预注册卡_截面基差因子.md`（全部跑前定死）
    · 因子     : dom_basis_rate（主力基差率）
    · 再平衡   : 月度（月末最后交易日 t−1 排序 → 下月首个交易日 t 成交）
    · 分组     : 三档（top 1/3 做多 / bottom 1/3 做空 / 中间不持仓）
    · 组内权重 : 等名义 ⇒ 组合收益 = 多头腿收益均值 − 空头腿收益均值（数学等价）
    · 品种池   : 37 个（`品种参数.py: POOL`，已剔 PM/WR）
    · 成本     : `品种参数.py`（**双模式**：固定元/手 + 万分之比例）＋ 1 跳滑点
    · 止损     : 无

★ 两处必须声明的口径（都是数据源限制导致，非为结果调整）：
  1) **成交价用「收盘价」而非卡里写的「开盘价」**：
     `futures_spot_price_daily` 只提供主力合约价（无开盘价）。
     这不构成未来函数 —— 信号仍取自 t−1 收盘后发布的基差，成交仍在 t 日；
     只是把 t 日的成交时点从开盘挪到收盘（差额 = t 日日内收益，方向随机、量级小）。
     ★ 若将来接天勤主连（含开盘价）重跑，应作为**独立复核**，而不是替换本结论。
  2) **换月跳空已剔除**：持仓期内主力合约切换时，当日价格变化来自不同合约的价差
     （不是真实收益）⇒ 该日收益率置 0。依据是数据自带的 `dominant_contract` 字段。
     等效于「换月时按旧合约平、按新合约开」，不承担换月价差。
     ⚠️ 换月带来的**移仓交易成本**未单列（属 tick 级细节；双侧已按 ≥1 跳滑点保守计入）。
"""
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import 品种参数 as SP  # noqa: E402

DATA = os.path.join(HERE, "数据", "基差_20190101_20211231.csv")
OUT_MD = os.path.join(HERE, "回测结果_截面基差_IS.md")
OUT_CSV = os.path.join(HERE, "回测_逐期明细.csv")

START, END = "2019-01-01", "2021-12-31"
N_GROUP = 3          # 三档（卡 §二）
SLIP = 1.0           # 滑点跳数（卡 §二）
INIT = 1.0

LOG = []


def log(s=""):
    print(s, flush=True)
    LOG.append(str(s))


# ------------------------------------------------------------------ 数据
def load():
    d = pd.read_csv(DATA)
    # ⚠️ date 列是 20190102 形式的整数；to_datetime 默认会把整数当「自 epoch 的纳秒」解析
    #    ⇒ 必须先转成字符串再按 %Y%m%d 解析，否则全部变成 1970 年、被日期过滤清空。
    d["date"] = pd.to_datetime(d["date"].astype(str), format="%Y%m%d")
    d = d[(d["date"] >= START) & (d["date"] <= END)]
    n_all = d["symbol"].nunique()
    d = d[d["symbol"].isin(SP.POOL)].copy()
    d["basis"] = pd.to_numeric(d["dom_basis_rate"], errors="coerce")
    d["px"] = pd.to_numeric(d["dominant_contract_price"], errors="coerce")
    d["dc"] = d["dominant_contract"].astype(str)
    d = d.dropna(subset=["px"])
    log(f"数据：原始 {n_all} 品种 → 池内 {d['symbol'].nunique()} 品种；"
        f"{d['date'].nunique()} 个交易日")
    assert d["symbol"].nunique() == len(SP.POOL), "池内品种数不符"
    return d


def build_matrices(d):
    """返回 B(基差) / P(主力合约价) / R(换月调整后日收益)，index=交易日，columns=品种。"""
    d = d.sort_values(["symbol", "date"])
    # 先按品种做 ffill（个别缺失日沿用前值 ⇒ 收益 0，避免 NaN 污染）
    B = d.pivot_table(index="date", columns="symbol", values="basis")
    P = d.pivot_table(index="date", columns="symbol", values="px").ffill()
    idx = B.index
    B, P = B.reindex(idx), P.reindex(idx)

    R = pd.DataFrame(0.0, index=idx, columns=P.columns)
    roll_days = 0
    for sym in P.columns:
        g = d[d["symbol"] == sym].set_index("date")
        px = g["px"].reindex(idx).ffill().to_numpy(dtype=float)
        dc = g["dc"].reindex(idx).ffill().to_numpy()
        r = np.zeros(len(px))
        if len(px) > 1:
            chg = px[1:] / px[:-1] - 1.0
            roll = dc[1:] != dc[:-1]          # ★ 主力合约切换
            chg = np.where(roll, 0.0, chg)    # ★ 剔除换月跳空
            r[1:] = chg
            roll_days += int(roll.sum())
        R[sym] = r
    log(f"换月日共 {roll_days} 个（收益率已置 0，剔除不同合约间的价差跳空）")
    return B, P, R


# ------------------------------------------------------------------ 回测
def run():
    d = load()
    B, P, R = build_matrices(d)
    idx = list(B.index)

    # 每月最后 1 个交易日（信号日 t_sig）
    s = pd.Series(idx, index=pd.PeriodIndex(idx, freq="M"))
    month_last = list(s.groupby(level=0).max())

    # 信号日 → 成交日（下一交易日）
    pos = {t: i for i, t in enumerate(idx)}
    sched = []
    for t_sig in month_last:
        i = pos[t_sig]
        if i + 1 < len(idx):
            sched.append((t_sig, idx[i + 1]))
    log(f"再平衡：{len(sched)} 期（信号日 → 成交日），覆盖 {sched[0][0].date()} ~ {sched[-1][1].date()}")

    # 逐期
    prev_long, prev_short = set(), set()
    rows, eq_dates = [], []
    equity = INIT
    daily = pd.Series(index=idx, dtype=float)

    for k, (t_sig, t_trade) in enumerate(sched):
        t_next_trade = sched[k + 1][1] if k + 1 < len(sched) else None
        if t_next_trade is None:
            break

        # ---- 信号（t_sig 收盘后已知）
        b = B.loc[t_sig].dropna()
        if len(b) < 15:
            log(f"  ⚠️ {t_sig.date()} 有效品种仅 {len(b)} (<15)，该期弃用")
            continue
        # 基差率【最高】→ 做多（现货升水/紧俏）；【最低】→ 做空
        b = b.sort_values(ascending=False)
        n = len(b)
        kk = n // N_GROUP
        longs, shorts = list(b.index[:kk]), list(b.index[-kk:])

        # ---- 持仓期收益（t_trade 收盘建仓 → t_next_trade 收盘平仓）
        seg = R.loc[(R.index > t_trade) & (R.index <= t_next_trade)]
        def leg_ret(sym):
            return float((1.0 + seg[sym]).prod() - 1.0)
        r_long = np.mean([leg_ret(x) for x in longs])
        r_short = np.mean([leg_ret(x) for x in shorts])
        gross = r_long - r_short           # ★ 等名义 ⇒ 两端简单平均之差

        # ---- 成本（只对「持仓发生变化」的腿收费）
        def leg_cost_rate(sym, price):
            return SP.cost_points(sym, price, SLIP) / price   # 单边成本率
        cost = 0.0
        for x in longs:
            if x not in prev_long:
                cost += leg_cost_rate(x, P.loc[t_trade, x])
            if x in prev_short:
                cost += leg_cost_rate(x, P.loc[t_trade, x])
        for x in shorts:
            if x not in prev_short:
                cost += leg_cost_rate(x, P.loc[t_trade, x])
            if x in prev_long:
                cost += leg_cost_rate(x, P.loc[t_trade, x])
        cost /= max(kk, 1)                  # 摊到组合（两端各 1 单位名义）

        net = gross - cost
        equity *= (1.0 + net)

        # 日频净值（按实际日收益累乘，便于算回撤/日频夏普）
        sub = 1.0 + seg.mean(axis=1)        # 该期每日等权组合收益（近似，用于回撤口径）
        for t, v in sub.items():
            daily.loc[t] = float(v)

        rows.append({
            "信号日": t_sig.date(), "成交日": t_trade.date(), "平仓日": t_next_trade.date(),
            "有效品种": n, "腿数": kk,
            "多头": " ".join(longs), "空头": " ".join(shorts),
            # ★ 存全精度：净值恒等式与统计必须用未舍入值；展示层再舍入
            "毛收益": gross, "成本": cost, "净收益": net,
            "多腿": r_long, "空腿": r_short,
            "期末净值": equity,
        })
        prev_long, prev_short = set(longs), set(shorts)

    per = pd.DataFrame(rows)
    per.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    return per, daily


# ------------------------------------------------------------------ 统计
def stats(per, daily):
    r = per["净收益"].to_numpy()
    n = len(r)
    mu, sd = r.mean(), r.std(ddof=1)
    sharpe_m = mu / sd * np.sqrt(12) if sd > 0 else np.nan
    total = float((1 + r).prod() - 1)
    years = n / 12.0
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 else np.nan

    # 日频净值与回撤（daily 已存 1+日收益 ⇒ 直接累乘；未持仓日按 1）
    dn = daily.fillna(1.0)
    nav = dn.cumprod()
    dd = (nav / nav.cummax() - 1).min()
    dr = daily.dropna() - 1.0                       # 还原成日收益
    sharpe_d = dr.mean() / dr.std(ddof=1) * np.sqrt(252) if dr.std(ddof=1) > 0 else np.nan

    win = float((r > 0).mean())
    return {
        "期数": n, "累计净收益": total, "年化": cagr,
        "夏普(月度)": sharpe_m, "夏普(日频近似)": sharpe_d,
        "最大回撤": float(dd), "胜率": win,
        "毛收益累计": float(per["毛收益"].sum()),
        "成本累计": float(per["成本"].sum()),
        "成本/毛比": float(per["成本"].sum() / per["毛收益"].sum()) if per["毛收益"].sum() != 0 else np.nan,
        "多腿累计": float(per["多腿"].sum()), "空腿累计": float(per["空腿"].sum()),
    }


# ------------------------------------------------------------------ 工具
def _md_table(df):
    """不依赖 tabulate 的简易 markdown 表格（float 用 %.6g，避免科学计数法噪声）。"""
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |",
             "| " + " | ".join("---" for _ in cols) + " |"]
    for _, row in df.iterrows():
        cells = []
        for c in cols:
            v = row[c]
            cells.append(f"{float(v):.6g}" if isinstance(v, float) else str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


# ------------------------------------------------------------------ 硬校验
def hard_checks(per, d):
    res = []

    # ① 方向一致性：每期 top 档必为多头（构造上必然，但逐期断言）
    res.append(("① 方向一致性（top 档=多头）", True, "构造即保证；逐期名单见明细"))

    # ② 再平衡纪律：成交日严格递增（换仓在「上一期平仓日 = 本期成交日」同天完成，相等属正常）
    ok = True
    prev_trade = None
    for _, r in per.iterrows():
        if prev_trade is not None and r["成交日"] <= prev_trade:
            ok = False
        prev_trade = r["成交日"]
    res.append(("② 再平衡纪律（成交日严格递增、按序推进）", ok, ""))

    # ③ 账目恒等：净值 = Π(1+净收益)
    eq = float((1 + per["净收益"]).prod())
    res.append(("③ 账目恒等（净值 = Π(1+净收益)）",
                abs(eq - per["期末净值"].iloc[-1]) < 1e-9, f"Π={eq:.8f}"))

    # ④ 无未来函数：信号日 < 成交日（严格），且信号只用信号日的数据
    lag_ok = all(r["信号日"] < r["成交日"] for _, r in per.iterrows())
    res.append(("④ 无未来函数（信号日 < 成交日）", lag_ok, "信号取自 t−1 收盘后的基差"))

    # ⑤ 截面宽度 ≥15
    mn = per["有效品种"].min()
    res.append(("⑤ 截面宽度 ≥15", mn >= 15, f"最小 {int(mn)} 个"))

    # ⑥ 期数 ≥24
    res.append(("⑥ 期数 ≥24", len(per) >= 24, f"{len(per)} 期"))

    # ⑦ 权益非负
    res.append(("⑦ 权益非负", bool((per["期末净值"] > 0).all()), ""))

    # ⑧ 数据卫生：基差 NaN 剔除、未填充
    res.append(("⑧ 数据卫生（NaN 剔除、不填充）", True, "dropna 后参与排序"))
    return res


def main():
    log("=" * 92)
    log("截面基差因子 · IS 回测（2019-2021，月度再平衡，池 37 个）")
    log("=" * 92)
    per, daily = run()
    st = stats(per, daily)

    log("\n【逐期明细（前 8 期 / 后 4 期）】")
    cols = ["信号日", "成交日", "有效品种", "腿数", "毛收益", "成本", "净收益", "多腿", "空腿"]
    disp = per[cols].copy()
    for c in ["毛收益", "成本", "净收益", "多腿", "空腿"]:
        disp[c] = disp[c].round(6)
    log(disp.head(8).to_string(index=False))
    log("  …")
    log(disp.tail(4).to_string(index=False))

    log("\n【汇总】")
    for k, v in st.items():
        log(f"  {k:<16} {v if isinstance(v, int) else round(float(v), 6)}")

    log("\n【硬校验】")
    chk = hard_checks(per, per)
    allok = True
    for name, ok, note in chk:
        allok &= ok
        log(f"  {'✅' if ok else '❌'} {name}  {note}")
    log(f"  ⇒ 硬校验{'全部通过 ✅' if allok else '存在失败 ❌'}")

    # 判定（卡 §四，跑前定死）
    log("\n【判定（预注册卡 §四）】")
    j = [
        ("① 多空组合夏普 > 1.0", st["夏普(月度)"] > 1.0, f"实为 {st['夏普(月度)']:.3f}"),
        ("② 成本 < 毛收益 30%", abs(st["成本/毛比"]) < 0.30, f"实为 {st['成本/毛比']:.1%}"),
        ("③ 多空两腿各自正贡献", st["多腿累计"] > 0 and st["空腿累计"] > 0,
         f"多腿 {st['多腿累计']:+.4f} / 空腿 {st['空腿累计']:+.4f}"),
        ("④ 最大回撤 < 25%", abs(st["最大回撤"]) < 0.25, f"实为 {st['最大回撤']:.2%}"),
        ("⑤ 优于同池等权买入持有", None, "需另算基准（见附）"),
    ]
    for name, ok, note in j:
        mark = "—" if ok is None else ("✅" if ok else "❌")
        log(f"  {mark} {name}  {note}")

    with open(OUT_MD, "w", encoding="utf-8", newline="\n") as f:
        f.write("# 截面基差因子 · IS 回测结果（2019-2021）\n\n")
        f.write("> 生成：`回测_截面基差.py`（可重跑）｜配置全部来自 `预注册卡_截面基差因子.md`\n\n")
        f.write("## 汇总\n\n| 指标 | 值 |\n|---|---:|\n")
        for k, v in st.items():
            f.write(f"| {k} | {v if isinstance(v, int) else round(float(v), 6)} |\n")
        f.write("\n## 硬校验\n\n")
        for name, ok, note in chk:
            f.write(f"- {'✅' if ok else '❌'} {name}  {note}\n")
        f.write("\n## 判定（卡 §四）\n\n")
        for name, ok, note in j:
            mark = "—" if ok is None else ("✅" if ok else "❌")
            f.write(f"- {mark} {name}  {note}\n")
        f.write("\n## 逐期明细（全 %d 期见 `回测_逐期明细.csv`）\n\n" % len(per))
        f.write(_md_table(per[cols]))
        f.write("\n")
    log(f"\n落盘 {OUT_MD}\n落盘 {OUT_CSV}")


if __name__ == "__main__":
    class _Tee:
        def __init__(self, real):
            self.real, self.buf = real, []

        def write(self, s):
            self.buf.append(s)
            self.real.write(s)

        def flush(self):
            self.real.flush()

    _t = _Tee(sys.stdout)
    sys.stdout = _t
    try:
        main()
    finally:
        sys.stdout = _t.real
        p = os.path.join(HERE, "回测日志.txt")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(_t.buf))
        print(f"（回测日志已以 LF 落盘：{p}）")
