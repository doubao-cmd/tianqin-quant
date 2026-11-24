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
CAL = os.path.join(DATA, "_calendar.json")   # fetch_daily.py 每天写，用来判断休市
NP_DB = os.path.abspath(os.path.join(HERE, "..", "_np_data", "np.db"))
DISCLAIMER = "仅为纯技术分析，不构成任何买卖建议。"

# 判据（aligned / streak / early_series / MA_N / MIN_BARS）放在 core.py，
# 与 review_events.py 共用同一份——两套判据会让回访统计对应不上看板。
from core import aligned, streak, early_series, MIN_BARS  # noqa: E402

# 推送里「正在多/正在空」只列最近 N 个交易日内成立的，更久的折叠成一行。
# 这是展示口径，不是策略参数——不影响任何信号判定，md 里始终是全量。
FRESH_DAYS = 5

# 数据不可信的品种占比超过这个值，整条推送退化成告警、不推名单。
# 为什么要有这条：名单的用处是「一屏看完谁在排列」，缺了三分之一还照样列，
# 用户会按那份名单做判断，而他不知道少了哪些——残缺名单比没有名单更危险。
# 0.4 不是策略参数，是「这份名单还叫不叫全市场」的底线，可以改但不要调着玩。
STALE_ABORT = 0.4

# 侧证（只进 md，不进推送正文）：回答「这波排列是预期还是现实」
#   基差 = 期货 − 现货（np_data 的 dom_basis 就是这个口径，已实测校准）
#   基差朝排列方向走 = 期货领跑 = 预期型；背离 = 现货领跑 = 现实型
SIDE_Z_MIN = 1.0         # 20 日变化在自身历史里的 z 分数下限
SIDE_EXTREME = 0.10      # 基差率三年分位低于 10% / 高于 90% 视为极端

# 基差不可用的品种：定义在 pool.py 里（品种保留、信号照常，只是侧证不给方向）
BASIS_UNUSABLE = P.BASIS_UNUSABLE
# 推送里是否隐藏这些品种（用户 2026-11-24：带 × 的不要出现在手机上）
PUSH_HIDE = set(BASIS_UNUSABLE) if P.PUSH_HIDE_BASIS_UNUSABLE else set()


def cn_of(key: str) -> str:
    return P.cn_of(key)


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
        return dict(cur=cur, pct=pct, d20=d20, z=z, median=float(win.median()),
                    date=str(rr.index[-1])[:10])

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
    # 时效要看**原始序列**的最后一根，不受清洗影响——清洗如果把最后一天当异常剔掉，
    # clean 的 date 会退到前一天，从而被误判成「基差停更 1 天」。
    clean["date"] = str(rate.index[-1])[:10]
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
    # ★ 基差停更就别判方向。「预/现」回答的是「这波谁在领跑」，用的是当日基差；
    #   拿三天前的基差去描述今天的排列，结论可能完全反过来，而手机上看不出差别。
    #   实测 36 个有基差的品种当天都是 lag=0，所以 lag>0 就是异常，不是正常延迟。
    if int(basis.get("lag") or 0) > 0:
        return "无数据", f"基差停在 {basis.get('date', '?')}（落后 {basis['lag']} 天）"
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


def load_side(symbols: list[str], asof: str = "") -> dict:
    """批量读取侧证。np.db 不存在时整体降级为空，不影响看板本身。

    asof 用来算基差的滞后天数（lag）——基差库由另一个采集任务写，
    它停了 scan 不会报错，只会悄悄拿旧基差去判「预期/现实」。
    """
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
                if b and asof and b.get("date"):
                    try:
                        b["lag"] = int((pd.Timestamp(asof) - pd.Timestamp(b["date"])).days)
                    except Exception:
                        b["lag"] = 0
                if b or r:
                    out[s] = dict(basis=b, receipt=r)
        finally:
            con.close()
    except Exception as e:
        print(f"[warn] 侧证读取失败（{type(e).__name__}: {e}），本次不含基差/仓单")
        return {}
    return out


def collect() -> tuple[list[dict], str, list[dict], list[dict], dict]:
    """扫描全部日线。返回 (行, 数据日期, 数据不足, 数据不可信, 信号活跃度)。

    ★ 数据不可信分两种，原因不同、处理相同（都**不参与任何判定**）：
      1. 末根日期落后于全池最新 —— 这次没拉到（tqsdk 掉线、合约停牌）
      2. 末根是最新，但近期缺了交易日 —— 拉到过，中间断了
    第 2 种只看末根日期是抓不到的，而它恰好是 `streak` 会跨过去继续数、
    `prev` 会跳到缺口前那一天的地方，正是最容易给出错误信号的情形。
    """
    rows, thin, loaded = [], [], []
    cmap = load_contracts()
    for f in sorted(glob.glob(os.path.join(DATA, "*_1440min.csv"))):
        key = os.path.basename(f).split("_")[0]
        if key in P.EXCLUDED:                 # 已剔除品种，历史数据可能还在，忽略
            continue
        d = S.load(f)
        if d is None:
            continue
        if len(d) < MIN_BARS:
            thin.append(dict(key=key, cn=cn_of(key), n=len(d), need=MIN_BARS))
            continue
        loaded.append((key, d))

    # ---- 基准交易日：取「多数品种都有」的日期 ----
    # 不用 data/_calendar.json：它只覆盖最近 7 天 + 未来 90 天，查不了历史缺口。
    # 40 个品种的日期并集本身就是最稳的交易日基准——任何一个源头出错，
    # 都盖不过其他 39 个；而单一数据源出错时会整段错，反而看不出来。
    from collections import Counter
    cnt: Counter = Counter()
    for key, d in loaded:
        cnt.update({str(x)[:10] for x in d["datetime"]})
    need = max(3, int(len(loaded) * 0.6))
    base = sorted(k for k, v in cnt.items() if v >= need)

    asof = max((str(d["datetime"].iloc[-1])[:10] for key, d in loaded), default="")

    stale = []
    for key, d in loaded:
        ds = [str(x)[:10] for x in d["datetime"]]
        last = ds[-1]
        if last < asof:
            why, why_s = f"末根停在 {last}", f"停{last[5:]}"
        else:
            recent = [t for t in base[-20:] if t <= last]
            miss = [t for t in recent if t not in set(ds)]
            if not miss:
                continue
            why = f"缺 {len(miss)} 个交易日（{'、'.join(miss[:5])}）"
            why_s = f"缺{len(miss)}天"
        stale.append(dict(key=key, cn=cn_of(key), date=last, why=why, why_s=why_s))
    stale_keys = {s["key"] for s in stale}

    # ---- 信号活跃度：每种事件在全部品种的全部历史上触发过几次 ----
    # ★ 为什么必须有这个：苗头从上线起就没触发过（两处条件互斥），
    #   但它每天在手机上显示「苗头：无」，和「今天真的没有」长得一模一样。
    #   一个死掉的字段显示「无」，是比缺功能更严重的误导——它给出了一个错误的否定。
    #   这里让每种事件类型自己证明它还活着；某个类型合计为 0 就说明判据失效了。
    alive = {"新多": 0, "新空": 0, "苗头多": 0, "苗头空": 0, "破坏": 0}

    for key, d in loaded:
        o = aligned(S.indicators(d))
        tr = o["a60_trig"].to_numpy()
        a6 = o["a60"].to_numpy()
        prev_a6 = np.concatenate(([0], a6[:-1]))
        alive["新多"] += int((tr == 1).sum())
        alive["新空"] += int((tr == -1).sum())
        alive["破坏"] += int(((a6 == 0) & (prev_a6 != 0)).sum())
        es = early_series(o)
        alive["苗头多"] += int((es == 1).sum())
        alive["苗头空"] += int((es == -1).sum())

        i = len(o) - 1
        if not np.isfinite(o.at[i, "ma60"]):
            continue
        c = int(o.at[i, "a60"] or 0)
        pc = int(o.at[i - 1, "a60"] or 0)
        trig = int(o.at[i, "a60_trig"] or 0)
        close = float(o.at[i, "close"])
        atr = float(o.at[i, "atr"])
        date = str(o.at[i, "datetime"])[:10]
        m5, m10, m20, m60 = (float(o.at[i, k]) for k in ("ma5", "ma10", "ma20", "ma60"))
        early = int(es[i])
        row = dict(
            key=key, cn=cn_of(key), date=date, close=close,
            ma5=m5, ma10=m10, ma20=m20, ma60=m60,
            atr=atr, atr_pct=atr / close * 100 if close else 0.0,
            # 发散度：MA5 距 MA60 的百分比（多头为正、空头为负）。
            # 只是标注，不参与任何判定——四线挤在一起和四线张开的排列，
            # 判据上完全等价，但被一根阳线打散的概率差很多。
            spread=(m5 - m60) / m60 * 100 if m60 else 0.0,
            d=c, prev=pc, trig=trig, early=early,
            roll=1 if (cmap.get(key, {}) or {}).get("roll_bar_date") == date else 0,
            streak=streak(o, i, c) if c else 0,
            stale=1 if key in stale_keys else 0,
        )
        rows.append(row)
    return rows, asof, thin, stale, alive


def short_text(rows: list[dict], asof: str, side: dict | None = None,
               warn: str = "", stale: list[dict] | None = None,
               alive: dict | None = None) -> tuple[str, str]:
    """手机短文案 + 标题。当日变化在前，正在排列的全量在后。
    (预)=期货领跑、现货没跟；(现)=现货领跑、期货跟随；(核)=基差单日异动已剔除、暂不判定；
    不标=中性或基差无数据。基差不可用的品种（FU）不进推送，只在 md 里出现。"""
    side = side or {}
    stale_keys = {s["key"] for s in (stale or [])}
    # ★ 数据不可信的品种不进任何名单（事件 / 正在排列 / 计数），只留顶部一行提示。
    #   理由：用停在三天前的 K 线、或跨着缺口的 K 线算出来的「新空」，
    #   在手机上和一个真的新空长得一模一样，但你没有任何办法分辨。这属于错报，
    #   不是弱信号——弱信号可以标一下，错报只能不出。
    vis = [r for r in rows if r["key"] not in PUSH_HIDE
           and r["key"] not in stale_keys]                 # 推送可见集合
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

    def tag(r: dict, d: int, with_cn: bool = True, with_streak: bool = False,
            with_spread: bool = False) -> str:
        """品种标记。with_spread 只给事件行用——正在排列的长名单不加，否则太挤。

        发散度格式 `4.8%` = MA5 距 MA60 的绝对值。为什么给数字不给「强/弱」：
        分档就要定阈值，阈值就是个参数，会被人拿去调；数字没有阈值，自己看。
        """
        nat, _ = nature_of(d, side.get(r["key"], {}).get("basis"))
        s = {"预期": "预", "现实": "现", "待核": "核"}.get(nat, "")
        base = nm(r) if with_cn else r["key"]
        marks = []
        if s:
            marks.append(s)
        if with_spread:
            marks.append(f"{abs(r.get('spread') or 0):.1f}%")
        if marks:
            base = f"{base}({' '.join(marks)})"
        if with_streak and r["streak"]:
            base = f"{base}{'空' if d < 0 else '多'}{r['streak']}"
        return base

    md = asof[5:] if asof else "?"
    title = f"排列看板 {md}"
    lines = []
    if warn:
        lines.append(warn)
    lines.append(f"【排列看板】数据截至 {asof}")
    if stale:
        items = "、".join(f"{s['key']}({s['why_s']})" for s in stale[:5])
        more = f" 等{len(stale)}个" if len(stale) > 5 else ""
        lines.append(f"⚠️ 数据不可信、今日不参与判定：{items}{more}")
    def none_of(names: tuple[str, ...]) -> str:
        """没信号时该写什么。★ 判据在全历史上一次都没触发过，就不能写「无」——
        「无」的意思是「今天没有」，而死字段的意思是「永远不会响」，两回事。
        苗头 2026-11-24 之前就处在这种状态，每天显示「无」，没人能看出来。"""
        a = alive or {}
        present = [n for n in names if n in a]
        if present and all(int(a.get(n) or 0) == 0 for n in present):
            return "判据全样本零触发（字段疑似失效，不要当成「今天没有」）"
        return "无"

    lines.append("新多：" + ("、".join(tag(r, 1, with_spread=True) for r in new_l)
                           if new_l else none_of(("新多",))))
    lines.append("新空：" + ("、".join(tag(r, -1, with_spread=True) for r in new_s)
                           if new_s else none_of(("新空",))))
    if early_only:
        lines.append("苗头：" + "、".join(
            f"{nm(r)}(苗·{'多' if r['early'] > 0 else '空'} {abs(r.get('spread') or 0):.1f}%)"
            for r in early_only))
    else:
        lines.append("苗头：" + none_of(("苗头多", "苗头空")))
    if broken:
        lines.append("破坏：" + "、".join(
            f"{nm(r)}(原{'多' if r['prev'] > 0 else '空'} {abs(r.get('spread') or 0):.1f}%)"
            for r in broken))
    else:
        lines.append("破坏：" + none_of(("破坏",)))

    def hold_block(label: str, rs: list[dict], d: int) -> None:
        """只列最近 FRESH_DAYS 个交易日内成立的；更久的折叠成一行。

        为什么不是「全列」：一个排列站了 16 天，昨天已经报过、明天还一样，
        每天重复推送没有新信息。为什么不是「全删」：站得越久说明趋势越顺，
        SN 空 18 天是全池最强的空头，删掉等于把最强的信号扔掉。
        所以折叠、不删——手机上只给近期，完整名单在 md。
        """
        if not rs:
            lines.append(f"{label}(0)：无")
            return
        fresh = [r for r in rs if (r["streak"] or 0) <= FRESH_DAYS]
        old = [r for r in rs if (r["streak"] or 0) > FRESH_DAYS]
        head = f"{label}({len(rs)})" + (f"·近{FRESH_DAYS}日{len(fresh)}" if old else "") + "："
        items = [tag(r, d, with_cn=False, with_streak=True) for r in fresh]
        if not items:
            lines.append(head + "无新增")
        elif len(items) <= 6:
            lines.append(head + "、".join(items))
        else:
            lines.append(head)
            for k in range(0, len(items), 6):
                lines.append("  " + "、".join(items[k:k + 6]))
        if old:
            olds = sorted(old, key=lambda x: -(x["streak"] or 0))
            tail = "、".join(f"{r['key']}{'空' if d < 0 else '多'}{r['streak']}" for r in olds)
            if len(olds) > 6:
                tail = "、".join(f"{r['key']}{'空' if d < 0 else '多'}{r['streak']}" for r in olds[:6])
                tail += f" 等{len(olds)}个"
            lines.append(f"  另{len(old)}个更久：{tail}")

    hold_block("正在多", hold_l, 1)
    hold_block("正在空", hold_s, -1)
    if rolled:
        lines.append("换月跳过信号：" + "、".join(nm(r) for r in rolled))
    # ★ 底部必须解释括号里那两个东西，否则就是让用户猜。
    #   「未标」以前写「中性」，但实际有三种情况（变化不显著 / 数据缺失 / 异动待核），
    #   写成「中性」会被读成「期货现货同步走」，那是四种里的一种，不是全部。
    lines.append("（预）期货领跑、现货没跟；（现）现货领跑、期货跟随；"
                 "未标=基差无明确方向")
    lines.append("（核）基差当日异动已剔除、暂不判定；"
                 "（x.x%）=MA5 距 MA60 幅度；破坏=排列散了，不等于反向")
    lines.append(DISCLAIMER)
    return title, "\n".join(lines)


def _st(r: dict) -> str:
    """持续天数，带方向：空15 / 多3。"""
    if not r.get("streak"):
        return "—"
    return f"{'空' if r.get('d', 0) < 0 else '多'}{r['streak']}"


def long_md(rows: list[dict], asof: str, side: dict | None = None,
            thin: list[dict] | None = None, stale: list[dict] | None = None,
            alive: dict | None = None) -> str:
    side = side or {}
    thin = thin or []
    stale = stale or []
    stale_keys = {s["key"] for s in stale}
    # 与推送保持同一口径：数据不可信的品种不进任何名单（否则 md 与手机对不上）
    live = [r for r in rows if not r["roll"] and r["key"] not in stale_keys]
    rolled = [r for r in rows if r["roll"] and r["key"] not in stale_keys]
    frozen = [r for r in rows if r["key"] in stale_keys]
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

    # ---- 信号活跃度：证明每种事件不是死字段 ----
    if alive:
        dead = [k for k, v in alive.items() if not v]
        lines.append("## 信号活跃度（全部品种、全部历史合计）\n")
        lines.append("| 事件 | 触发次数 |")
        lines.append("|---|---|")
        for k, v in alive.items():
            lines.append(f"| {k} | {v} |")
        lines.append("")
        if dead:
            lines.append(f"> ⚠️ **{'、'.join(dead)} 全样本零触发**，判据很可能已失效。"
                         "这种情况下推送不会写「无」，会直接把这个问题标出来。\n")
        else:
            lines.append("> 每种事件都有触发记录，说明判据是活的——"
                         "「今天没有」才真的等于今天没有。\n")

    # ---- 基差时效：停了就不给「预/现」，这里必须说清是哪几个、停在哪天 ----
    if side:
        lagging = [(k, int((v.get("basis") or {}).get("lag") or 0))
                   for k, v in side.items()
                   if (v.get("basis") or {}).get("date")
                   and not (v.get("basis") or {}).get("unusable")]
        lagging = [(k, l) for k, l in lagging if l > 0]
        if lagging:
            lst = "、".join(f"{k}({l}天)" for k, l in sorted(lagging, key=lambda x: -x[1])[:10])
            lines.append(f"> ⚠️ 基差数据停更：{lst}。这些品种**不给（预）/（现）方向**——"
                         "用几天前的基差判断今天的领跑关系会得出相反结论。\n")

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
        lines.append("> ★ **这是描述，不是预测**：818 个事件里能回算收益的 616 个，"
                     "标(预)与标(现)之后 10/20 日的收益没有显著差异"
                     "（20 日 +0.60% vs −1.59%，t≈−1.8，且样本高度重叠）。"
                     "它回答「这波谁先动」，不回答「谁走得远」，**不要拿它当筛选依据**。")
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
        # ⑤⑥ 是全量长名单，加板块 + 发散度 + 基差分位三列，方便快速扫
        if idx >= 4:
            lines.append("| 品种 | 名称 | 板块 | 持续 | 收盘 | 发散% | MA5 | MA10 | MA20 | MA60 | ATR% | 基差分位 | 性质 |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
            for r in sorted(rs, key=lambda x: (P.SECTOR_ORDER.index(P.sector_of(x["key"]))
                                               if P.sector_of(x["key"]) in P.SECTOR_ORDER else 9,
                                               -(x["streak"] or 0))):
                b = side.get(r["key"], {}).get("basis")
                nat, _ = nature_of(r["d"], b)
                pct_txt = ("不可用" if b.get("unusable") else f"{b['pct']:.0%}") if b else "—"
                lines.append(
                    f"| {r['key']} | {r['cn']} | {P.sector_of(r['key'])} | {_st(r)} | {r['close']:.2f} | "
                    f"{abs(r.get('spread') or 0):.1f}% | "
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
    lines.append(f"在扫 **{len(P.POOL)}** 个品种"
                 f"（农产品 {sum(1 for k in P.POOL if P.sector_of(k) == '农产品')} / "
                 f"化工 {sum(1 for k in P.POOL if P.sector_of(k) == '化工')} / "
                 f"黑色 {sum(1 for k in P.POOL if P.sector_of(k) == '黑色')}），"
                 f"已剔除 **{len(P.EXCLUDED)}** 个。\n")
    lines.append("### 已剔除（不进扫描）\n")
    lines.append("| 品种 | 名称 | 理由 |")
    lines.append("|---|---|---|")
    for k, (cn, why) in P.EXCLUDED.items():
        lines.append(f"| {k} | {cn} | {why} |")
    lines.append("")
    if stale:
        lines.append("### ⚠️ 数据不可信 · 已从上面所有名单排除\n")
        lines.append("| 品种 | 名称 | 原因 | 该品种最后一根的旧状态 |")
        lines.append("|---|---|---|---|")
        for s in stale:
            r = next((x for x in frozen if x["key"] == s["key"]), None)
            snap = (f"{_st(r)} 收盘 {r['close']:.2f}" if r and r.get("d") else
                    ("未成排列" if r else "—"))
            lines.append(f"| {s['key']} | {s['cn']} | {s['why']} | {snap} |")
        lines.append("")
        lines.append("> 这些品种**没有**进入今日的事件与正在排列统计（手机上也不显示），"
                     "上表只是它停更前那根 K 线的状态，不要当今天的结论用。"
                     "检查顺序：tqsdk 是否掉线 → 该合约是否停牌/换月 → 本地 csv 是否被截断。\n")
    if thin:
        lines.append("### 本次因数据不足跳过\n")
        lines.append("| 品种 | 根数 | 门槛 |")
        lines.append("|---|---|---|")
        for t in thin:
            lines.append(f"| {t['key']} | {t['n']} | ≥{t['need']} |")
        lines.append("")
    lines.append("> 准入三条线（数据层面）：具体主力合约 ≥159 根（MA60 有 100 个有效点）、"
                 "20 日日均成交额 ≥10 亿元、基差率三年中位偏离 0 不超过 20%。\n")
    lines.append("> 关注范围（主观边界）：农产品 / 化工（含橡胶、燃料油、纸浆）/ 黑色；"
                 "贵金属、有色、新能源金属、航运指数不看。\n")
    lines.append("> ★ 保证金不是准入线：一手占用高低与均线排列是否可信无关，"
                 "只按板块筛，不按钱筛。\n")
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


def _is_trading_day(d: dt.date) -> bool | None:
    """d 是不是交易日。None = 本地没有日历文件，判断不了（调用方自己退回工作日猜）。

    ★ 存在的意义：把「今天休市所以没数据」和「今天该有数据但没拉到」分开。
      旧版一律静默跳过，结果 10-19 那次定时任务连续四天没触发，
      手机上什么都没收到，看起来和「今天没信号」一模一样。
    """
    try:
        cal = json.load(open(CAL, encoding="utf-8"))
    except Exception:
        return None
    k = d.strftime("%Y-%m-%d")
    return bool(cal[k]) if k in cal else None


def main() -> int:
    rows, asof, thin, stale, alive = collect()
    if not rows:
        print("[error] data/ 下无可用日线，先跑 fetch_daily.py")
        return 1
    os.makedirs(OUT, exist_ok=True)
    side = load_side([r["key"] for r in rows if not r["stale"]], asof)

    # 上次失败 → 本次文案顶部补一行告警（webhook 挂了时，这条是唯一能自我暴露的方式）
    fail = _load_fail()
    warn = ""
    if int(fail.get("n") or 0) > 0:
        warn = (f"⚠️ 上次推送失败（{str(fail.get('last', ''))[:16]}，"
                f"连续 {fail['n']} 次）：{str(fail.get('reason', ''))[:48]}")

    n_total = len(rows)
    abort = bool(n_total) and len(stale) / n_total >= STALE_ABORT
    if abort:
        # 残缺的名单比没有名单更危险：用户会按它做判断，却不知道少了谁。
        title = f"排列看板 {asof[5:]} 数据不全"
        keys = "、".join(s["key"] for s in stale[:8]) + ("…" if len(stale) > 8 else "")
        text = "\n".join([
            f"【排列看板】数据截至 {asof}",
            f"⚠️ {len(stale)}/{n_total} 个品种数据不可信：{keys}",
            "名单已不完整，今天不推排列结果。",
            "检查顺序：tqsdk 是否登录 → 网络 → 定时任务是否运行。",
            "",
            DISCLAIMER,
        ])
    else:
        title, text = short_text(rows, asof, side, warn=warn, stale=stale, alive=alive)
    stamp = asof.replace("-", "")
    p_md = os.path.join(OUT, f"watch_{stamp}.md")
    p_tx = os.path.join(OUT, f"watch_{stamp}.txt")
    with open(p_md, "w", encoding="utf-8") as f:
        f.write(long_md(rows, asof, side, thin, stale, alive))
    with open(p_tx, "w", encoding="utf-8") as f:
        f.write(text)

    print(text)
    print(f"\n落盘 {p_md}\n落盘 {p_tx}")

    # 事件回访：每天顺带刷新一份（纯统计，失败不影响推送）
    try:
        import review_events
        review_events.main(quiet=True)
    except Exception as e:
        print(f"[warn] 事件回访失败（{type(e).__name__}: {e}），不影响本次推送")

    if "--no-push" in sys.argv:
        print("推送已跳过（--no-push）")
        return 0

    cfg = json.load(open(CFG, encoding="utf-8")) if os.path.exists(CFG) else {}
    if str(cfg.get("channel", "none")).lower() in ("none", "", "off"):
        print("未配置推送渠道（channel=none），本次只落盘。")
        return 0

    # ---- 数据没更新：可能只是休市，也可能是拉取失败。★ 别静默。----
    p_last = os.path.join(OUT, "_last_asof.txt")
    try:
        last = open(p_last, encoding="utf-8").read().strip()
    except Exception:
        last = ""
    t_now = dt.datetime.now()
    if last == asof:
        today_s = t_now.strftime("%Y-%m-%d")
        # ① 数据就是今天的，只是今天已经推过一次 → 静默跳过（防重复推送）
        if asof >= today_s:
            print(f"今天（{asof}）的数据已推送过，跳过。")
            return 0
        # ② 数据停在过去 → 先分清休市还是掉线
        tday = _is_trading_day(t_now.date())
        if tday is None:
            tday = t_now.weekday() < 5       # 没有日历文件就按工作日猜
        # 当天 bar 要等日盘 15:00 收盘才完整，17:00 之后才算「该有了」
        if not (tday and t_now.hour >= 17):
            print(f"数据未更新（最新仍是 {asof}）：今天休市或尚未收盘，跳过推送。")
            return 0
        # 该有却没有 → 推告警。同一个 asof 每天只推一条，避免整点重复刷屏。
        p_al = os.path.join(OUT, "_stale_alert.json")
        try:
            prev_al = json.load(open(p_al, encoding="utf-8"))
        except Exception:
            prev_al = {}
        if prev_al.get("day") == t_now.strftime("%Y-%m-%d") and prev_al.get("asof") == asof:
            print(f"数据仍未更新（{asof}），今天已告警过一次，不重复推。")
            return 0
        alarm = (f"【排列看板】数据没更新\n"
                 f"今天是交易日，但日线还停在 {asof}，"
                 f"{t_now.strftime('%m-%d')} 的 bar 没拉到。\n"
                 f"本次没有名单可看。\n"
                 f"检查：每日排列看板.bat 是否跑完 / tqsdk 是否登录成功 / 合约是否停牌。\n"
                 f"{DISCLAIMER}")
        ok, msg = send(cfg, f"排列看板 数据未更新 {asof}", alarm)
        print(msg)
        if ok:
            try:
                with open(p_al, "w", encoding="utf-8") as f:
                    json.dump({"day": t_now.strftime("%Y-%m-%d"), "asof": asof},
                              f, ensure_ascii=False)
            except Exception:
                pass
        return 0 if ok else 1

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
