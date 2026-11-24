# -*- coding: utf-8 -*-
"""
排列看板 · 四线同向版 + 手机推送
==============================================================
和 scan_live.py 的区别：多一条 MA60 同向约束。

  多头排列：MA5 > MA10 > MA20 > MA60
  空头排列：MA5 < MA10 < MA20 < MA60

只做筛选，不做策略、不回测、不下单。判据全部来自已收盘日线，
"今日新成立" = 昨日不满足、今日满足（状态转移）。

输出两份：
  out/watch_YYYYMMDD.md     完整表格（含 MA60、持续天数）
  out/watch_YYYYMMDD.txt    手机推送短文案（含免责声明）

推送渠道在 push_config.json 里配；没有该文件时只落盘、不推送。
用法：
  python scan_daily_push.py            # 扫描 + 按配置推送
  python scan_daily_push.py --no-push  # 只扫描落盘，不推送
"""
from __future__ import annotations
import os, re, sys, json, glob, time, datetime as dt
import urllib.request, urllib.parse
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import screen as S
import pool as P

ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "out")
CFG = os.path.join(HERE, "push_config.json")
NP_DB = os.path.abspath(os.path.join(HERE, "..", "_np_data", "np.db"))
DISCLAIMER = "仅为纯技术分析，不构成任何买卖建议。"
MA_N = 60
MIN_BARS = 159          # MA60 至少要有 100 个有效点，持续天数才有意义

# 侧证（只进 md，不进推送正文）：回答「这波排列是预期还是现实」
#   基差 = 期货 − 现货（np_data 的 dom_basis 就是这个口径，已实测校准）
#   基差朝排列方向走 = 期货领跑 = 预期型；背离 = 现货领跑 = 现实型
SIDE_MIN_JUMP_PP = 1.0   # 20 日基差率变化小于该值视为「无信息」，标中性
SIDE_Z_MIN = 1.0         # 20 日变化在自身历史里的 z 分数下限
SIDE_EXTREME = 0.10      # 基差率三年分位低于 10% / 高于 90% 视为极端

# 基差不可用的品种：定义在 pool.py 里（品种保留、信号照常，只是侧证不给方向）
BASIS_UNUSABLE = P.BASIS_UNUSABLE
# 推送里是否隐藏这些品种（用户 2026-11-24：带 × 的不要出现在手机上）
PUSH_HIDE = set(BASIS_UNUSABLE) if P.PUSH_HIDE_BASIS_UNUSABLE else set()


def cn_of(key: str) -> str:
    return P.cn_of(key)


def aligned(o: pd.DataFrame) -> pd.DataFrame:
    """四线同向：MA5>MA10>MA20>MA60 为多，反向为空。只在状态转移日记 trig。"""
    m5, m10, m20 = o["ma5"], o["ma10"], o["ma20"]
    ma60 = o["close"].rolling(MA_N).mean()
    o["ma60"] = ma60
    o["a60"] = np.where(
        (m5 > m10) & (m10 > m20) & (m20 > ma60), 1,
        np.where((m5 < m10) & (m10 < m20) & (m20 < ma60), -1, 0),
    )
    prev = o["a60"].shift(1).fillna(0)
    o["a60_trig"] = np.where((o["a60"] != 0) & (o["a60"] != prev), o["a60"], 0)
    return o


def streak(o: pd.DataFrame, i: int, direction: int) -> int:
    n = 0
    for j in range(i, -1, -1):
        if int(o.at[j, "a60"] or 0) == direction:
            n += 1
        else:
            break
    return n


def load_contracts() -> dict:
    p = os.path.join(DATA, "_contracts.json")
    if not os.path.exists(p):
        return {}
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 侧证：基差 + 仓单（只给 md 用，不参与信号判定）
# ---------------------------------------------------------------------------
def _np_series(con, metric: str, sym: str):
    """返回 (值Series, 合约Series)，按日期升序；无数据返回 (None, None)。"""
    r = con.execute(
        "select period_start, contract, value from np_data "
        "where metric=? and symbol=? order by period_start", (metric, sym)).fetchall()
    if not r:
        return None, None
    df = pd.DataFrame(r, columns=["d", "c", "v"])
    df["d"] = pd.to_datetime(df["d"])
    s = df.set_index("d")["v"].astype(float)
    c = df.set_index("d")["c"].astype(str)
    return s, c


def _basis_rate(con, sym: str):
    """基差率(%) = (期货-现货)/期货。剔除换月日，并剔除单日异常跳变。

    现货源（100ppi）偶尔换报价口径，基差率会在一天内跳好几个百分点，
    把 20 日变化的方向整个拽反（AG 2026-11-24：基差 +207 → −246）。
    清洗用稳健尺度（MAD），不用标准差——现货源频繁跳时，标准差会被自己撑大，
    反而漏掉异常（AG 用 3σ 时 z 刚好 3.02，剔不掉）。

    清洗会同时保留原始口径（orig_*）：只有当「清洗前后结论不同」时，
    才在判定层标「待核」。数据脏但没影响结论的，照常给方向，避免满屏待核。
    """
    if sym in BASIS_UNUSABLE:
        return {"unusable": True, "why": BASIS_UNUSABLE[sym]}
    b, bc = _np_series(con, "dom_basis", sym)
    sp, _ = _np_series(con, "spot_price", sym)
    if b is None or sp is None:
        return None
    fut = sp.reindex(b.index) + b          # dom_basis = 期货 - 现货
    rate = (b / fut * 100).replace([np.inf, -np.inf], np.nan)
    # contract 变化日 = 换月，基差不可比，剔除该日
    same = bc.reindex(rate.index) == bc.reindex(rate.index).shift(1)
    rate = rate[same].dropna().tail(1200)
    if len(rate) < 200:
        return None

    def pack(rr):
        """给定序列，算现值/分位/20日变化/z。清洗前后共用同一套口径。"""
        if len(rr) < 120:
            return None
        win = rr[rr.index >= rr.index[-1] - pd.Timedelta(days=1095)]
        if len(win) < 200:
            win = rr
        cur = float(rr.iloc[-1])
        pct = float((win < cur).mean())
        d20 = cur - float(rr.iloc[-21]) if len(rr) > 40 else float("nan")
        chg = (rr - rr.shift(20)).reindex(win.index).dropna()
        z = (d20 - chg.mean()) / chg.std() if len(chg) > 100 and chg.std() > 0 else float("nan")
        return dict(cur=cur, pct=pct, d20=d20, z=z, median=float(win.median()))

    orig = pack(rate)
    if orig is None:
        return None

    # 单日异常跳变清洗
    dif = rate.diff()
    dvalid = dif.dropna()
    suspect = False
    clean_src = rate
    if len(dvalid) > 100:
        med = float(dvalid.median())
        mad = float((dvalid - med).abs().median()) * 1.4826
        scale = mad if mad > 0 else float(dvalid.std())
        if scale > 0:
            bad = (dif - med).abs() > 3 * scale
            if len(bad):
                suspect = bool(bad.iloc[-1])
            clean_src = rate[~bad.fillna(False)]

    clean = pack(clean_src) or orig
    clean["suspect"] = suspect
    clean["orig_d20"] = orig["d20"]
    clean["orig_z"] = orig["z"]
    clean["orig_pct"] = orig["pct"]
    return clean


def _receipt(con, sym: str):
    """仓单：现值 + 六年分位。跨所单位不同，只看自身历史，不做跨品种比较。"""
    s, _ = _np_series(con, "receipt", sym)
    if s is None or len(s.dropna()) < 200:
        return None
    s = s.dropna()
    cur = float(s.iloc[-1])
    win = s[s.index >= s.index[-1] - pd.Timedelta(days=2190)]
    pct = float((win < cur).mean()) if len(win) > 200 else np.nan
    return dict(cur=cur, pct=pct)


def _verdict(d: int, d20: float, z: float, pct: float) -> str:
    """单一口径下的方向判定。清洗前后各算一次，用来比较结论是否被脏数据改变。"""
    if d20 != d20 or z != z:
        return "无数据"
    extreme = (d < 0 and pct <= SIDE_EXTREME) or (d > 0 and pct >= 1 - SIDE_EXTREME)
    if abs(z) < SIDE_Z_MIN and not extreme:
        return "中性"
    return "预期" if d20 * d > 0 else "现实"


def nature_of(d: int, basis: dict | None) -> tuple[str, str]:
    """判定「预期 / 现实 / 中性 / 待核」。

    基差朝排列方向走 => 期货领跑 => 预期；背离 => 现货领跑 => 现实。
    d=+1 多头 / -1 空头。仅用基差；仓单季节性太重，只展示、不参与判定。

    两个入选条件（满足其一才谈方向，否则算中性/无信息）：
      1) 20 日变化在自身历史里够异常（|z| >= 1）
      2) 贴水/升水已在三年极端位置（空头看低分位、多头看高分位）

    待核只在一种情况下出现：最新一天的基差跳变被剔除后，
    **结论从 A 变成了 B**。数据脏但结论没变的，照常给方向。
    """
    if not d:
        return "—", ""
    if not basis:
        return "无数据", ""
    if basis.get("unusable"):
        return "不可用", basis.get("why", "基差源不可用")
    v = _verdict(d, basis["d20"], basis["z"], basis["pct"])
    if basis.get("suspect"):
        vo = _verdict(d, basis.get("orig_d20", basis["d20"]),
                      basis.get("orig_z", basis["z"]),
                      basis.get("orig_pct", basis["pct"]))
        if vo != v:
            return "待核", "最新基差跳变，剔除前后结论不同"
    if v == "无数据":
        return "无数据", ""
    if v == "中性":
        return "中性", "基差没明显移动"
    if v == "预期":
        return "预期", "期货领跑，现货没跟"
    return "现实", "现货领跑，期货跟随"


def load_side(symbols: list[str]) -> dict:
    """批量读取侧证。np.db 不存在时整体降级为空，不影响看板本身。"""
    if not os.path.exists(NP_DB):
        return {}
    out = {}
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{NP_DB}?mode=ro", uri=True)
        try:
            for s in symbols:
                b = _basis_rate(con, s)
                r = _receipt(con, s)
                if b or r:
                    out[s] = dict(basis=b, receipt=r)
        finally:
            con.close()
    except Exception as e:
        print(f"[warn] 侧证读取失败（{type(e).__name__}: {e}），本次不含基差/仓单")
        return {}
    return out


def collect() -> tuple[list[dict], str, list[dict]]:
    """扫描全部日线。返回 (行, 数据日期, 因数据不足被跳过的品种)。"""
    rows, asof, thin = [], "", []
    cmap = load_contracts()
    for f in sorted(glob.glob(os.path.join(DATA, "*_1440min.csv"))):
        key = os.path.basename(f).split("_")[0]
        if key in P.EXCLUDED:                 # 已剔除品种，历史数据可能还在，忽略
            continue
        d = S.load(f)
        if d is None:
            continue
        if len(d) < MIN_BARS:
            thin.append(dict(key=key, n=len(d), need=MIN_BARS))
            continue
        o = aligned(S.indicators(d))
        i = len(o) - 1
        if not np.isfinite(o.at[i, "ma60"]):
            continue
        c = int(o.at[i, "a60"] or 0)
        pc = int(o.at[i - 1, "a60"] or 0)
        trig = int(o.at[i, "a60_trig"] or 0)
        close = float(o.at[i, "close"])
        atr = float(o.at[i, "atr"])
        date = str(o.at[i, "datetime"])[:10]
        asof = max(asof, date)
        m5, m10, m20, m60 = (float(o.at[i, k]) for k in ("ma5", "ma10", "ma20", "ma60"))
        pm5, pm10 = float(o.at[i - 1, "ma5"]), float(o.at[i - 1, "ma10"])
        up = (m5 > m10) and (pm5 <= pm10)
        dn = (m5 < m10) and (pm5 >= pm10)
        early = 1 if (up and m10 > m20 > m60) else (-1 if (dn and m10 < m20 < m60) else 0)
        row = dict(
            key=key, cn=cn_of(key), date=date, close=close,
            ma5=m5, ma10=m10, ma20=m20, ma60=m60,
            atr=atr, atr_pct=atr / close * 100 if close else 0.0,
            d=c, prev=pc, trig=trig, early=early,
            roll=1 if (cmap.get(key, {}) or {}).get("roll_bar_date") == date else 0,
            streak=streak(o, i, c) if c else 0,
        )
        rows.append(row)
    return rows, asof, thin


def short_text(rows: list[dict], asof: str, side: dict | None = None,
               warn: str = "") -> tuple[str, str]:
    """手机短文案 + 标题。当日变化在前，正在排列的全量在后。
    (预)=期货领跑、现货没跟；(现)=现货领跑、期货跟随；(核)=基差单日异动已剔除、暂不判定；
    不标=中性或基差无数据。基差不可用的品种（AG/AU/FU）不进推送，只在 md 里出现。"""
    side = side or {}
    vis = [r for r in rows if r["key"] not in PUSH_HIDE]   # 推送可见集合
    live = [r for r in vis if not r["roll"]]               # 换月当天的均线不可比，不出信号
    new_l = sorted([r for r in live if r["trig"] == 1], key=lambda x: -x["atr_pct"])
    new_s = sorted([r for r in live if r["trig"] == -1], key=lambda x: -x["atr_pct"])
    broken = [r for r in live if r["d"] == 0 and r["prev"] != 0]
    early_only = [r for r in live if r["early"] != 0 and r["d"] == 0]
    rolled = [r for r in vis if r["roll"]]
    hold_l = sorted([r for r in vis if r["d"] == 1], key=lambda x: -x["streak"])
    hold_s = sorted([r for r in vis if r["d"] == -1], key=lambda x: -x["streak"])

    def nm(r: dict) -> str:
        cn = r.get("cn") or ""
        return f"{cn}{r['key']}" if cn else r["key"]

    def tag(r: dict, d: int, with_cn: bool = True, with_streak: bool = False) -> str:
        nat, _ = nature_of(d, side.get(r["key"], {}).get("basis"))
        s = {"预期": "预", "现实": "现", "待核": "核"}.get(nat, "")
        base = nm(r) if with_cn else r["key"]
        if s:
            base = f"{base}({s})"
        if with_streak and r["streak"]:
            base = f"{base}{'空' if d < 0 else '多'}{r['streak']}"
        return base

    md = asof[5:] if asof else "?"
    title = f"排列看板 {md}"
    lines = []
    if warn:
        lines.append(warn)
    lines.append(f"【排列看板】数据截至 {asof}")
    lines.append(f"新多：{'、'.join(tag(r, 1) for r in new_l) if new_l else '无'}")
    lines.append(f"新空：{'、'.join(tag(r, -1) for r in new_s) if new_s else '无'}")
    if early_only:
        lines.append("苗头：" + "、".join(
            f"{nm(r)}(苗·{'多' if r['early'] > 0 else '空'})" for r in early_only))
    else:
        lines.append("苗头：无")
    if broken:
        lines.append("破坏：" + "、".join(
            f"{nm(r)}(原{'多' if r['prev'] > 0 else '空'})" for r in broken))
    else:
        lines.append("破坏：无")

    def hold_block(label: str, rs: list[dict], d: int) -> None:
        """全量名单：超过 6 个就分行，手机上更好扫。"""
        if not rs:
            lines.append(f"{label}(0)：无")
            return
        items = [tag(r, d, with_cn=False, with_streak=True) for r in rs]
        if len(items) <= 6:
            lines.append(f"{label}({len(items)})：{'、'.join(items)}")
        else:
            lines.append(f"{label}({len(items)})：")
            for k in range(0, len(items), 6):
                lines.append("  " + "、".join(items[k:k + 6]))

    hold_block("正在多", hold_l, 1)
    hold_block("正在空", hold_s, -1)
    if rolled:
        lines.append("换月跳过信号：" + "、".join(nm(r) for r in rolled))
    hidden = [r["key"] for r in rows if r["key"] in PUSH_HIDE and r["d"]]
    if hidden:
        lines.append(f"未列：{'、'.join(hidden)}")
    lines.append("（预）期货领跑、现货没跟；（现）现货领跑、期货跟随；"
                 "（核）基差单日异动已剔除、暂不判定；未标=中性")
    lines.append(DISCLAIMER)
    return title, "\n".join(lines)


def _st(r: dict) -> str:
    """持续天数，带方向：空15 / 多3。"""
    if not r.get("streak"):
        return "—"
    return f"{'空' if r.get('d', 0) < 0 else '多'}{r['streak']}"


def long_md(rows: list[dict], asof: str, side: dict | None = None,
            thin: list[dict] | None = None) -> str:
    side = side or {}
    thin = thin or []
    live = [r for r in rows if not r["roll"]]
    rolled = [r for r in rows if r["roll"]]
    lines = [
        f"# 排列看板（MA60 同向） · 数据截至 {asof}",
        "",
        "> 多头 MA5>MA10>MA20>MA60；空头 MA5<MA10<MA20<MA60。",
        "> 只看已收盘日线，今日新成立 = 昨日不满足、今日满足。",
        f"> {DISCLAIMER}",
        "",
    ]
    if rolled:
        lines.append("> ⚠️ 换月跳过信号：" + "、".join(
            f"{r['key']}（{r['date']} 起新合约）" for r in rolled) + "\n")

    # ---- 当日事件的侧证：回答「这波排列是预期还是现实」----
    events = [r for r in live if r["trig"] or (r["early"] and not r["d"]) or (r["d"] == 0 and r["prev"])]
    lines.append(f"\n## ⓪ 今日事件 · 侧证（{len(events)}）\n")
    if not events:
        lines.append("_今日无新成立/苗头/破坏，无侧证_\n")
    else:
        lines.append("| 品种 | 事件 | 收盘 | 基差率 | 3年分位 | 20日Δ | z | 仓单 | 仓单6年分位 | 性质 | 读法 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for r in events:
            sd = side.get(r["key"], {})
            b, rc = sd.get("basis"), sd.get("receipt")
            ev = ("新多" if r["trig"] == 1 else "新空" if r["trig"] == -1
                  else "破坏" if (r["d"] == 0 and r["prev"]) else
                  f"苗头·{'多' if r['early'] > 0 else '空'}")
            nat, why = nature_of(r["d"] or r["trig"] or r["early"], b)
            if b and not b.get("unusable"):
                btxt = f"{b['cur']:.1f}%"
                ptxt = f"{b['pct']:.0%}"
                dtxt = f"{b['d20']:+.1f}pp" if b["d20"] == b["d20"] else "—"
                ztxt = f"{b['z']:+.1f}" if b["z"] == b["z"] else "—"
            elif b:
                btxt = ptxt = dtxt = ztxt = "不可用"
            else:
                btxt = ptxt = dtxt = ztxt = "—"
            rtxt = f"{rc['cur']:.0f}" if rc else "无数据"
            rptxt = f"{rc['pct']:.0%}" if rc and rc["pct"] == rc["pct"] else "—"
            lines.append(
                f"| {r['key']} | {ev} | {r['close']:.2f} | {btxt} | {ptxt} | {dtxt} | {ztxt} | "
                f"{rtxt} | {rptxt} | {nat} | {why or '—'} |")
        lines.append("")
        lines.append("> 性质判据：基差朝排列方向走 = 期货领跑 = **预期**（现货没跟，赚情绪的钱）；"
                     "背离 = 现货领跑 = **现实**（有实货压着）。")
        lines.append("> **待核**：最新一天的基差被判定为异常跳变（|日变化| > 3×MAD），"
                     "剔掉后结论会翻转，因此不给方向。")
        lines.append("> 基差率 = (期货−现货)/期货，负值为期货贴水；分位越低表示贴水越极端。"
                     "z = 20 日变化在自身三年分布里的标准化值，|z|≥1 才算「动得够异常」。")
        lines.append("> 仓单跨所单位不同（吨/张），只看自身历史，不参与性质判定。")
        lines.append("")

    groups = [
        ("① 今日新成立 · 多", [r for r in live if r["trig"] == 1]),
        ("② 今日新成立 · 空", [r for r in live if r["trig"] == -1]),
        ("③ 今日苗头（MA5 刚穿 MA10，尚未成排列）",
         [r for r in live if r["early"] != 0 and r["d"] == 0]),
        ("④ 今日破坏", [r for r in live if r["d"] == 0 and r["prev"] != 0]),
        ("⑤ 当前多头排列", sorted([r for r in rows if r["d"] == 1], key=lambda x: -x["streak"])),
        ("⑥ 当前空头排列", sorted([r for r in rows if r["d"] == -1], key=lambda x: -x["streak"])),
    ]
    for idx, (name, rs) in enumerate(groups):
        lines.append(f"\n## {name}（{len(rs)}）\n")
        if not rs:
            lines.append("_无_\n")
            continue
        # ⑤⑥ 是全量长名单，加一列基差分位方便快速扫
        if idx >= 4:
            lines.append("| 品种 | 名称 | 持续 | 收盘 | MA5 | MA10 | MA20 | MA60 | ATR% | 基差分位 | 性质 |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
            for r in rs:
                b = side.get(r["key"], {}).get("basis")
                nat, _ = nature_of(r["d"], b)
                pct_txt = ("不可用" if b.get("unusable") else f"{b['pct']:.0%}") if b else "—"
                lines.append(
                    f"| {r['key']} | {r['cn']} | {_st(r)} | {r['close']:.2f} | "
                    f"{r['ma5']:.2f} | {r['ma10']:.2f} | {r['ma20']:.2f} | {r['ma60']:.2f} | "
                    f"{r['atr_pct']:.2f}% | {pct_txt} | {nat} |")
        else:
            lines.append("| 品种 | 名称 | 持续 | 收盘 | MA5 | MA10 | MA20 | MA60 | ATR% |")
            lines.append("|---|---|---|---|---|---|---|---|---|")
            for r in rs:
                lines.append(
                    f"| {r['key']} | {r['cn']} | {_st(r)} | {r['close']:.2f} | "
                    f"{r['ma5']:.2f} | {r['ma10']:.2f} | {r['ma20']:.2f} | {r['ma60']:.2f} | "
                    f"{r['atr_pct']:.2f}% |")

    # ---- 品种池说明：谁被剔了、为什么 ----
    lines.append("\n## ⓘ 品种池说明\n")
    lines.append(f"在扫 **{len(P.POOL)}** 个品种；已剔除 **{len(P.EXCLUDED)}** 个。\n")
    lines.append("### 已剔除（不进扫描）\n")
    lines.append("| 品种 | 名称 | 理由 |")
    lines.append("|---|---|---|")
    for k, (cn, why) in P.EXCLUDED.items():
        lines.append(f"| {k} | {cn} | {why} |")
    lines.append("")
    if thin:
        lines.append("### 本次因数据不足跳过\n")
        lines.append("| 品种 | 根数 | 门槛 |")
        lines.append("|---|---|---|")
        for t in thin:
            lines.append(f"| {t['key']} | {t['n']} | ≥{t['need']} |")
        lines.append("")
    lines.append("> 准入线：具体主力合约 ≥159 根（MA60 有 100 个有效点）、"
                 "20 日日均成交额 ≥10 亿元、基差率三年中位偏离 0 不超过 20%。\n")
    if PUSH_HIDE:
        lines.append("### 基差源不可用（品种保留、信号照常，侧证不给方向）\n")
        lines.append("| 品种 | 理由 |")
        lines.append("|---|---|")
        for k, why in BASIS_UNUSABLE.items():
            lines.append(f"| {k} | {why} |")
        lines.append("")
        lines.append("> 这些品种**不出现在手机推送的名单里**，只在本文件可见。"
                     "它们的均线信号仍然有效，只是没有基差侧证。\n")
    return "\n".join(lines)


def _sc_url(key: str) -> str:
    """Server酱两种 SendKey 的端点不同，按前缀自动选。

    - SCT 开头  → Server酱 Turbo，sctapi.ftqq.com
    - sctp 开头 → Server酱³，https://{uid}.push.ft07.com/send/{key}.send
                  uid 藏在 key 里：sctp123tXXXX → 123
    """
    k = key.strip()
    if k.lower().startswith("sctp"):
        m = re.match(r"^sctp(\d+)t", k, re.I)
        if not m:
            raise ValueError(f"Server酱³ SendKey 格式异常（应为 sctp{{uid}}t...）：{k[:8]}…")
        return f"https://{m.group(1)}.push.ft07.com/send/{k}.send"
    return f"https://sctapi.ftqq.com/{k}.send"


def send(cfg: dict, title: str, text: str) -> tuple[bool, str]:
    """返回 (是否真的送达, 描述)。

    ★ 不能只看 HTTP 200：企业微信/钉钉/Serve酱 在 key 失效时**照样返回 200**，
      错误码在 body 里（企微 errcode=93000 等）。旧版只看 200 会把"推送已死"
      当成功，然后更新 _last_asof.txt，从此连续几天静默失败而无人知道。
    """
    ch = str(cfg.get("channel", "none") or "none").lower()
    if ch in ("none", "", "off"):
        return False, "未推送（channel=none，未配置密钥）"
    # Markdown 渠道（Server酱 / 企微）里单个换行会被合并成一段，必须双换行
    md = text.replace("\n", "\n\n")
    try:
        if ch == "serverchan":
            url = _sc_url(cfg["serverchan_key"])
            data = urllib.parse.urlencode({"title": title, "desp": md}).encode("utf-8")
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        elif ch == "wecom":
            url = cfg["wecom_webhook"]
            body = {"msgtype": "markdown", "markdown": {"content": md}}
            req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        elif ch == "dingtalk":
            url = "https://oapi.dingtalk.com/robot/send?access_token=" + cfg["dingtalk_token"]
            body = {"msgtype": "text", "text": {"content": text}}
            req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        elif ch == "telegram":
            url = f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage"
            body = {"chat_id": cfg["telegram_chat_id"], "text": text}
            req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        else:
            return False, f"未知渠道 {ch}"

        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        # ---- 按各渠道的 body 判断是否真的成功 ----
        code, errmsg = 0, ""
        try:
            j = json.loads(raw)
            if ch == "wecom" or ch == "dingtalk":
                code = int(j.get("errcode", 0) or 0)
                errmsg = str(j.get("errmsg", "") or "")
            elif ch == "serverchan":
                code = int(j.get("code", 0) or 0)
                errmsg = str(j.get("message", "") or "")
            elif ch == "telegram":
                code = 0 if j.get("ok") else 1
                errmsg = str(j.get("description", "") or "")
        except Exception:
            pass
        if code:
            hint = ""
            if ch == "wecom" and code in (93000, 93002, 40001, 40014, 42001):
                hint = "（webhook 可能已失效或被禁用，需到群设置重新获取）"
            return False, f"{ch} 返回错误 errcode={code} {errmsg}{hint}"
        return True, f"{ch} 推送 OK"
    except Exception as e:
        return False, f"{ch} 推送失败：{type(e).__name__}: {e}"


FAIL_FILE = os.path.join(OUT, "_push_fail.json")
ALARM_FILE = os.path.join(OUT, "推送异常.txt")


def _load_fail() -> dict:
    try:
        return json.load(open(FAIL_FILE, encoding="utf-8"))
    except Exception:
        return {}


def _save_fail(d: dict) -> None:
    try:
        with open(FAIL_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def main() -> int:
    rows, asof, thin = collect()
    if not rows:
        print("[error] data/ 下无可用日线，先跑 fetch_daily.py")
        return 1
    os.makedirs(OUT, exist_ok=True)
    side = load_side([r["key"] for r in rows])

    # 上次失败 → 本次文案顶部补一行告警（webhook 挂了时，这条是唯一能自我暴露的方式）
    fail = _load_fail()
    warn = ""
    if int(fail.get("n") or 0) > 0:
        warn = (f"⚠️ 上次推送失败（{str(fail.get('last', ''))[:16]}，"
                f"连续 {fail['n']} 次）：{str(fail.get('reason', ''))[:48]}")

    title, text = short_text(rows, asof, side, warn=warn)
    stamp = asof.replace("-", "")
    p_md = os.path.join(OUT, f"watch_{stamp}.md")
    p_tx = os.path.join(OUT, f"watch_{stamp}.txt")
    with open(p_md, "w", encoding="utf-8") as f:
        f.write(long_md(rows, asof, side, thin))
    with open(p_tx, "w", encoding="utf-8") as f:
        f.write(text)

    print(text)
    print(f"\n落盘 {p_md}\n落盘 {p_tx}")

    if "--no-push" in sys.argv:
        print("推送已跳过（--no-push）")
        return 0

    cfg = json.load(open(CFG, encoding="utf-8")) if os.path.exists(CFG) else {}
    if str(cfg.get("channel", "none")).lower() in ("none", "", "off"):
        print("未配置推送渠道（channel=none），本次只落盘。")
        return 0

    # 节假日/数据未更新：同一批数据不重复推
    p_last = os.path.join(OUT, "_last_asof.txt")
    try:
        last = open(p_last, encoding="utf-8").read().strip()
    except Exception:
        last = ""
    if last == asof:
        print(f"数据未更新（最新仍是 {asof}），跳过推送。")
        return 0

    ok, msg = send(cfg, title, text)
    if not ok and "errcode" not in msg:      # 网络类失败重试一次；key 失效重试无意义
        time.sleep(3)
        ok, msg = send(cfg, title, text)
    print(msg)

    if ok:
        if int(fail.get("n") or 0) > 0:
            print(f"（补报上次失败：曾连续 {fail['n']} 次）")
        _save_fail({})
        with open(p_last, "w", encoding="utf-8") as f:
            f.write(asof)
        if os.path.exists(ALARM_FILE):
            try:
                os.remove(ALARM_FILE)
            except Exception:
                pass
        return 0

    # ---- 失败：记账 + 落一个显眼的本地告警文件 ----
    n = int(fail.get("n") or 0) + 1
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save_fail(dict(n=n, last=now, reason=msg, asof=asof,
                    last_ok_asof=fail.get("last_ok_asof") or ""))
    with open(ALARM_FILE, "w", encoding="utf-8") as f:
        f.write(f"排列看板 · 推送异常\n"
                f"{'='*40}\n"
                f"已连续失败：{n} 次\n"
                f"本次数据日期：{asof}\n"
                f"最后失败时间：{now}\n"
                f"错误：{msg}\n\n"
                f"排查顺序\n"
                f"1. push_config.json 里的 webhook / key 是否还有效\n"
                f"2. 手机上机器人是否被移出群、群是否解散\n"
                f"3. 企业微信后台是否禁用了该机器人\n"
                f"4. 网络（本机能否打开 https://qyapi.weixin.qq.com）\n\n"
                f"修好后双击「每日排列看板.bat」重跑，会自动补报这次失败。\n")
    print(f"[ALARM] 推送连续失败 {n} 次 → {ALARM_FILE}")
    return 1 if n < 3 else 2


if __name__ == "__main__":
    sys.exit(main())
