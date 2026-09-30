# -*- coding: utf-8 -*-
"""
模拟盘实测台账（月度）
==========================================================
读模拟盘跑出来的两个 CSV，生成【一份】会自己更新的 HTML 台账：

    实测记录/<策略目录>/<品种>/
        <品种>_成交流水.csv      <- 程序写（事件流：START/SIGNAL/FILL/STOPLOSS）
        <品种>_运行心跳.csv      <- 程序写（每 5 分钟一行，休市照写）
        <品种>_<策略>_台账.html   <- 本脚本写（覆盖更新，永远只有一份）

用法：
    python 工具/模拟盘台账.py V            # PVC
    python 工具/模拟盘台账.py 甲醇
    python 工具/模拟盘台账.py V 双均线      # 第二个参数是策略短名，默认双均线
（双击 看台账.bat 即可 —— 旧结构在 策略/01_双均线/模拟盘/<品种>/ 下，
  新结构由 组合/<策略>__<key>.py 跑模拟盘时自动写 实测记录/）

★ 这个台账回答两个别的报告回答不了的问题
--------------------------------------------------------------
① 「这段时间没有交易，是程序没开，还是程序开着但条件不够？」
   运行心跳**休市照写**。所以：
     · 心跳出现时间断层      -> 程序当时没在跑（停机 / 没开机）
     · 心跳连续、没有 SIGNAL -> 程序在跑，条件不够（策略本来就不该开仓）
   两者不再混成同一团「数据空洞」。
   再加一条判据区分「停机有没有代价」：断层前后的 K 线时间若**没有推进**，
   说明当时根本没有新 K 线收盘 -> 不会漏信号；推进了就标出来。

② 「回测假设的滑点（等效 2 跳）是真的吗？」
   每笔 FILL 与它前面那条信号/止损的参考价相减 = 实测滑点。
   这是回测永远给不出的数字（结论 15：滑点占总成本 78%）。

不做什么（有意为之）：不画收益率/回撤曲线、不算夏普/卡玛。
笔数少时这些指标纯属自欺；判据只认「每手点数」和「实测滑点」。
"""
import csv
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.strategy_dirs import 策略目录名          # noqa: E402
from config_symbols import SYMBOLS                 # noqa: E402

_FMT = "%Y-%m-%d %H:%M:%S"
GAP_FLOOR_SEC = 900      # 断层判定下限：15 分钟（与心跳间隔的 3 倍取大）
_MATCH_WIN = 3600        # 成交与其前面那条信号/止损的配对窗口（秒）


# ==================================================================
# 读取与解析
# ==================================================================
def _resolve(品种):
    """接受天勤代号（V）或中文名（PVC / 甲醇），返回 (key, 中文名, 乘数, 最小变动)。"""
    a = (品种 or "").strip()
    for k, (name, _code, mult, tick) in SYMBOLS.items():
        if a.upper() == k or a == name:
            return k, name, mult, tick
    raise SystemExit(f"品种写错了：{品种}\n可用代号：{', '.join(SYMBOLS)}\n"
                     f"或中文名：{', '.join(v[0] for v in SYMBOLS.values())}")


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if any((v or "").strip() for v in r.values())]


def _t(s):
    try:
        return datetime.strptime((s or "").strip(), _FMT)
    except Exception:
        return None


def _f(s, d=0.0):
    try:
        return float(str(s).strip())
    except Exception:
        return d


def _i(s, d=0):
    try:
        return int(float(str(s).strip()))
    except Exception:
        return d


def _dur(sec):
    sec = int(max(0, sec))
    if sec < 3600:
        return f"{sec // 60} 分"
    if sec < 86400:
        return f"{sec // 3600} 时 {sec % 3600 // 60} 分"
    return f"{sec // 86400} 天 {sec % 86400 // 3600} 时"


def _note_map(备注):
    out = {}
    for seg in (备注 or "").split("|"):
        seg = seg.strip()
        if not seg:
            continue
        parts = seg.split()
        if len(parts) >= 2:
            out[parts[0]] = " ".join(parts[1:])
    return out


def _month_range(k):
    y, mo = int(k[:4]), int(k[5:7])
    return datetime(y, mo, 1), datetime(y + (1 if mo == 12 else 0), (mo % 12) + 1, 1)


# ==================================================================
# 核心分析
# ==================================================================
def 分析(events, hearts, mult, tick):
    R = {"会话": [], "断层": [], "成交": [], "月": [], "滑点": [], "未成交": [],
         "异常事件": [],
         "心跳间隔": 0.0, "首": None, "末": None, "心跳数": len(hearts),
         "下单开关": "?", "参数": "", "手续费": 0.0, "合约": "",
         "覆盖秒": 0.0, "跨度秒": 0.0, "空洞秒": 0.0}

    starts = [e for e in events if e.get("类型") == "START"]
    if starts:
        s0 = starts[-1]
        R["参数"] = (s0.get("备注") or "").strip()
        nm = _note_map(R["参数"])
        R["下单开关"] = nm.get("下单开关", "?")
        R["合约"] = nm.get("合约", "")
        tail = R["参数"].split("手续费")[-1]
        R["手续费"] = _f(tail.split("元")[0].strip())

    # ---- 运行心跳 -> 会话 / 断层 ----
    hb = []
    for h in hearts:
        t = _t(h.get("记录时间"))
        if t:
            hb.append({"t": t, "bar": (h.get("K线时间") or "").strip()})
    hb.sort(key=lambda x: x["t"])
    if hb:
        R["首"], R["末"] = hb[0]["t"], hb[-1]["t"]
        deltas = [(hb[k]["t"] - hb[k - 1]["t"]).total_seconds()
                  for k in range(1, len(hb))]
        if deltas:
            ds = sorted(deltas)
            R["心跳间隔"] = ds[len(ds) // 2]
        gate = max(GAP_FLOOR_SEC, R["心跳间隔"] * 3)
        s = hb[0]["t"]
        for k in range(1, len(hb)):
            if deltas[k - 1] > gate:
                a, b = hb[k - 1], hb[k]
                R["会话"].append((s, a["t"]))
                R["断层"].append({"起": a["t"], "止": b["t"],
                                  "时长": (b["t"] - a["t"]).total_seconds(),
                                  "前K线": a["bar"], "后K线": b["bar"],
                                  "推进": a["bar"] != b["bar"]})
                s = b["t"]
        R["会话"].append((s, hb[-1]["t"]))
    R["覆盖秒"] = sum((b - a).total_seconds() for a, b in R["会话"])
    R["跨度秒"] = (R["末"] - R["首"]).total_seconds() if R["首"] else 0.0
    R["空洞秒"] = sum(g["时长"] for g in R["断层"])

    # ---- 信号 / 止损参考点 ----
    refs = []
    for e in events:
        if e.get("类型") in ("SIGNAL", "STOPLOSS"):
            t = _t(e.get("记录时间"))
            if not t:
                continue
            refs.append({"t": t, "kind": e["类型"],
                         "dir": (e.get("方向") or "").strip(),
                         "ref": _f(e.get("参考价")),
                         "bar": (e.get("K线时间") or "").strip()})
    refs.sort(key=lambda x: x["t"])
    signals = [r for r in refs if r["kind"] == "SIGNAL"]

    # ---- 成交配对（FIFO）----
    legs, cur = [], None
    fills = [e for e in events if e.get("类型") == "FILL"]
    for e in fills:
        d_cn = (e.get("方向") or "")[:1]
        off = (e.get("方向") or "")[1:]
        d = 1 if d_cn == "多" else (-1 if d_cn == "空" else 0)
        qty, px = _i(e.get("手数")), _f(e.get("成交价"))
        t = _t(e.get("记录时间"))
        if not d or qty <= 0:
            continue
        if "开" in off:
            if not legs:
                cur = {"开仓时间": t, "方向": d_cn, "手数": 0, "点数量": 0.0,
                       "金额": 0.0, "开仓价": px, "开仓手数": 0}
            legs.append((qty, px))
            cur["开仓手数"] += qty
        elif cur is not None:
            left = qty
            while left > 0 and legs:
                lq, lp = legs[0]
                use = min(left, lq)
                pts = (px - lp) * d
                cur["点数量"] += pts * use
                cur["手数"] += use
                cur["金额"] += pts * use * mult
                left -= use
                if use == lq:
                    legs.pop(0)
                else:
                    legs[0] = (lq - use, lp)
            cur["平仓时间"] = t
        # 滑点配对
        hit = None
        for r in reversed(refs):
            if r["t"] <= t and (t - r["t"]).total_seconds() <= _MATCH_WIN:
                hit = r
                break
        if hit:
            is_buy = (d_cn == "多" and "开" in off) or (d_cn == "空" and "开" not in off)
            pts = (px - hit["ref"]) * (1 if is_buy else -1)
            kind = ("止损" if hit["kind"] == "STOPLOSS"
                    else ("开仓" if "开" in off else "平仓"))
            R["滑点"].append({"时间": t, "类型": kind, "方向": d_cn + off,
                              "手数": qty, "成交价": px, "参考价": hit["ref"],
                              "点": pts, "跳": (pts / tick if tick else 0.0),
                              "滞后": (t - hit["t"]).total_seconds()})
        if cur is not None and not legs and cur["手数"] > 0:
            cur["平仓价"] = px
            cur["点/手"] = cur["点数量"] / cur["手数"]
            cur["手续费"] = R["手续费"] * cur["手数"] * 2
            cur["净额"] = cur["金额"] - cur["手续费"]
            cur["月"] = cur["开仓时间"].strftime("%Y-%m") if cur["开仓时间"] else "?"
            R["成交"].append(cur)
            cur = None

    # ---- 未成交：开仓类 SIGNAL 在窗口内没有 FILL 跟着（下单模式下才有意义）----
    if R["下单开关"] == "开":
        ft = [t for t in (_t(e.get("记录时间")) for e in fills) if t]
        for s in signals:
            if s["dir"] == "平":
                continue
            if not any(0 <= (t - s["t"]).total_seconds() <= _MATCH_WIN for t in ft):
                R["未成交"].append(s)

    # ---- 异常事件：DESYNC / STOPATTACH（2026-09-20 engine 新增的两类记录）----
    #   必须在这里单独拎出来、单独显示。理由：
    #     · DESYNC    = 目标仓位与实际持仓对不上（未成交 / 部分成交 / 平仓未成）。
    #                   那段时间「程序以为在做多、其实手数不符」，把它当成正常持仓，
    #                   会把实测滑点与成交率一起算歪 —— 它和「幽灵止损」同一个病。
    #     · STOPATTACH = 程序启动时已有持仓，补挂了一条止损。它的基准是当时的
    #                   收盘价、**不是真实开仓价**，所以那条线不能拿去核对
    #                   「单笔风险 ≈ 目标风险额」。
    #   这两类都不参与 refs（滑点配对），所以不会污染滑点统计。
    for _e in events:
        _k = (_e.get("类型") or "").strip()
        if _k in ("", "START", "STOP", "SIGNAL", "FILL", "STOPLOSS"):
            continue
        R["异常事件"].append({
            "类型": _k,
            "时间": _t(_e.get("记录时间")),
            "方向": (_e.get("方向") or "").strip(),
            "参考价": (_e.get("参考价") or "").strip(),
            "备注": (_e.get("备注") or "").strip()})
    R["异常事件"].sort(key=lambda x: x["时间"] or datetime.min)

    # ---- 月度 ----
    keys = set()
    for x in R["成交"]:
        keys.add(x["月"])
    for s in signals:
        keys.add(s["t"].strftime("%Y-%m"))
    for a, _b in R["会话"]:
        keys.add(a.strftime("%Y-%m"))
    for k in sorted(keys):
        ma, mb = _month_range(k)
        ts = [x for x in R["成交"] if x["月"] == k]
        sl = [x["跳"] for x in R["滑点"] if x["时间"].strftime("%Y-%m") == k]
        gs = [g for g in R["断层"]
              if g["起"].strftime("%Y-%m") == k or g["止"].strftime("%Y-%m") == k]
        sgs = [s for s in signals if s["t"].strftime("%Y-%m") == k]
        cov = sum(max(0.0, (min(b, mb) - max(a, ma)).total_seconds())
                  for a, b in R["会话"])
        R["月"].append({"月": k, "笔": len(ts), "信号": len(sgs),
                        "多": len([x for x in ts if x["方向"] == "多"]),
                        "空": len([x for x in ts if x["方向"] == "空"]),
                        "点合计": sum(x["点/手"] for x in ts),
                        "点/笔": (sum(x["点/手"] for x in ts) / len(ts)) if ts else 0.0,
                        "手数": sum(x["手数"] for x in ts),
                        "净额": sum(x["净额"] for x in ts),
                        "毛额": sum(x["金额"] for x in ts),
                        "滑点跳": (sum(sl) / len(sl)) if sl else None,
                        "空洞次": len(gs), "空洞秒": sum(g["时长"] for g in gs),
                        "覆盖秒": cov})
    return R


# ==================================================================
# HTML
# ==================================================================
CSS = """
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Microsoft YaHei", sans-serif;
         background:#f5f7fa; color:#222; margin:0; padding:24px; }
  .wrap { max-width:960px; margin:0 auto; background:#fff;
          border:1px solid #e3e8ef; border-radius:12px; padding:22px 26px; }
  h1 { font-size:19px; margin:0 0 3px; }
  h2 { font-size:14.5px; margin:22px 0 7px; padding-left:8px;
       border-left:4px solid #4c8bf5; }
  .sub { color:#788; font-size:12.5px; margin-bottom:16px; }
  .bar { display:flex; border:1px solid #eef2f7; border-radius:8px;
         background:#f8fafc; overflow:hidden; margin:10px 0 4px; }
  .bar .cell { flex:1; text-align:center; padding:9px 2px; border-right:1px solid #eef2f7; }
  .bar .cell:last-child { border-right:0; }
  .bar .k { font-size:11px; color:#8a94a6; margin-bottom:3px; }
  .bar .v { font-size:16px; font-weight:700; }
  .pos { color:#d4380d; } .neg { color:#198755; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; margin:8px 0 4px; }
  th { background:#f8fafc; color:#8a94a6; font-weight:700; font-size:11.5px;
       text-align:left; padding:6px 8px; border-bottom:1px solid #eef2f7;
       white-space:nowrap; }
  td { padding:6px 8px; border-bottom:1px solid #f6f8fb; }
  tr:last-child td { border-bottom:0; }
  td.n, th.n { text-align:right; font-variant-numeric:tabular-nums; }
  .mono { font-family:Consolas,"Courier New",monospace; font-size:12px; }
  .warnbox { font-size:12.5px; line-height:1.7; color:#8a5a00; background:#fffbf0;
             border:1px solid #f0d9a8; border-radius:8px; padding:8px 11px; margin:8px 0; }
  .okbox { font-size:12.5px; line-height:1.7; color:#166534; background:#f0fdf4;
           border:1px solid #bbe7c8; border-radius:8px; padding:8px 11px; margin:8px 0; }
  .gap { color:#c2790a; font-weight:700; }
  .hurt { color:#94a3b8; }
  .mhead { font-size:13px; font-weight:700; margin:14px 0 2px; color:#334; }
  .sum { font-size:12.5px; color:#556; line-height:1.75; background:#f8fafc;
         border-radius:7px; padding:6px 10px; margin:2px 0 4px; }
  .foot { margin-top:20px; font-size:11.5px; color:#aab; border-top:1px solid #eef2f7;
          padding-top:10px; line-height:1.85; }
  code { background:#f2f5f9; border-radius:4px; padding:0 4px; }
"""


def _sign(v):
    return "pos" if v > 0 else ("neg" if v < 0 else "")


def _fx(v, n=1, sign=True):
    s = (f"{v:+,.{n}f}" if sign else f"{v:,.{n}f}")
    return f'<span class="{_sign(v)}">{s}</span>'


def _pct(v):
    return f"{v:.0f}%"


def _正文(R, mult, tick, 策略):
    o = []

    # ---- 指标条 ----
    n = len(R["成交"])
    pt = sum(x["点/手"] for x in R["成交"])
    per = (pt / n) if n else 0.0
    net = sum(x["净额"] for x in R["成交"])
    sl = [x["跳"] for x in R["滑点"] if x["类型"] != "止损"]
    slm = (sum(sl) / len(sl)) if sl else 0.0
    cov = (R["覆盖秒"] / R["跨度秒"] * 100) if R["跨度秒"] else 0.0
    o.append('<div class="bar">')
    o.append(f'<div class="cell"><div class="k">已积累</div>'
             f'<div class="v">{n} / 30 笔</div></div>')
    o.append(f'<div class="cell"><div class="k">每手点数（累计）</div>'
             f'<div class="v {_sign(pt)}">{pt:+.1f}</div></div>')
    o.append(f'<div class="cell"><div class="k">每手点数（每笔）</div>'
             f'<div class="v {_sign(per)}">{per:+.2f}</div></div>')
    o.append(f'<div class="cell"><div class="k">实测滑点（均）</div>'
             f'<div class="v">{slm:.1f} 跳</div></div>')
    o.append(f'<div class="cell"><div class="k">运行覆盖</div>'
             f'<div class="v">{_pct(cov)}</div></div>')
    o.append('</div>')
    obs = ('（观察模式：只记信号不下单，所以没有成交与滑点）'
           if R["下单开关"] != "开" else '')
    o.append(f'<div class="sum">净额（估）'
             f'<b class="{_sign(net)}">{net:+,.0f} 元</b>'
             f' ｜ 已扣手续费 {sum(x["手续费"] for x in R["成交"]):,.0f} 元'
             f' ｜ 下单开关：<b>{R["下单开关"]}</b>{obs}</div>')

    # ---- ① 运行与空洞 ----
    o.append('<h2>① 运行与空洞（区分「没开机」和「没信号」）</h2>')
    if not R["心跳数"]:
        o.append('<div class="warnbox">还没有心跳数据。</div>')
    else:
        o.append(f'<div class="sum">程序累计运行 '
                 f'<b>{_dur(R["覆盖秒"])}</b>（{len(R["会话"])} 段会话），'
                 f'心跳间隔约 <b>{R["心跳间隔"]:.0f} 秒</b>；'
                 f'出现 <b>{len(R["断层"])}</b> 次时间断层，'
                 f'合计 <b>{_dur(R["空洞秒"])}</b>。</div>')
        if R["断层"]:
            o.append('<table><tr><th>断层起</th><th>断层止</th><th class="n">时长</th>'
                     '<th>前一K线时间</th><th>后一K线时间</th><th>这份断层的含义</th></tr>')
            for g in R["断层"]:
                if g["推进"]:
                    j = '<span class="gap">★ 期间有新 K 线收盘 → 可能漏掉信号</span>'
                else:
                    j = 'K 线未推进 → 当时没有新 K 线，不会漏信号'
                o.append(f'<tr><td class="mono">{g["起"]:%m-%d %H:%M:%S}</td>'
                         f'<td class="mono">{g["止"]:%m-%d %H:%M:%S}</td>'
                         f'<td class="n">{_dur(g["时长"])}</td>'
                         f'<td class="mono hurt">{g["前K线"][:16] or "—"}</td>'
                         f'<td class="mono hurt">{g["后K线"][:16] or "—"}</td>'
                         f'<td>{j}</td></tr>')
            o.append('</table>')
            o.append('<div class="warnbox">心跳是<b>休市照写</b>的，所以「时间断层」'
                     '只可能有一个原因：<b>程序当时没在跑</b>（停机 / 关机 / 崩溃）。'
                     '反过来，心跳连续、成交流水却没有 SIGNAL 行，就是'
                     '<b>程序在跑但条件不够</b> —— 这两种情况在本页里是完全分开的。</div>')
        else:
            o.append('<div class="okbox">运行无断层：从第一次启动到现在，心跳一直没有中断，'
                     '程序始终开着。<br>所以这期间「没有交易」可以确定是'
                     '<b>条件不够</b>，不是漏跑。</div>')

    # ---- ①b 异常事件（DESYNC / STOPATTACH）----
    if R["异常事件"]:
        n_ds = sum(1 for x in R["异常事件"] if x["类型"] == "DESYNC")
        n_sa = sum(1 for x in R["异常事件"] if x["类型"] == "STOPATTACH")
        o.append('<h2>①b 异常事件（目标≠实际持仓 / 启动补挂止损）</h2>')
        o.append('<div class="warnbox">'
                 f'共 <b>{len(R["异常事件"])}</b> 条：'
                 f'<b>DESYNC</b> {n_ds} 条 ｜ <b>STOPATTACH</b> {n_sa} 条。<br>'
                 '· <b>DESYNC</b> = 目标仓位与实际持仓对不上（一点没成交 / 只成交一部分 / '
                 '平仓单没成交）。<b>出现它的那段时间不能当成正常持仓</b> —— 程序当时'
                 '以为的仓位与实际不符，实测滑点与成交率都要绕开它看。'
                 '程序的处置：超时后以实际持仓为准，清零并等下一个信号'
                 '（与「限价挂不上就放弃」同一口径）。<br>'
                 '· <b>STOPATTACH</b> = 程序启动时已有持仓，补挂了一条止损。'
                 '它的基准是<b>当时那根已收盘K线的收盘价</b>、不是真实开仓价，'
                 '所以那条线<b>不能</b>拿去核对「单笔风险 ≈ 目标风险额」；'
                 '它只证明「从那一刻起有保护」。'
                 '</div>')
        o.append('<table><tr><th class="n">#</th><th>类型</th><th>时间</th>'
                 '<th>方向</th><th class="n">参考价</th><th>说明</th></tr>')
        for i, e in enumerate(R["异常事件"], 1):
            ts = f'{e["时间"]:%m-%d %H:%M:%S}' if e["时间"] else "—"
            o.append(f'<tr><td class="n">{i}</td>'
                     f'<td><b>{e["类型"]}</b></td>'
                     f'<td class="mono">{ts}</td>'
                     f'<td>{e["方向"] or "—"}</td>'
                     f'<td class="n">{e["参考价"] or "—"}</td>'
                     f'<td>{e["备注"] or ""}</td></tr>')
        o.append('</table>')

    # ---- ② 月度台账 ----
    o.append('<h2>② 月度台账</h2>')
    if not R["月"]:
        o.append('<div class="warnbox">还没有月度数据。</div>')
    else:
        o.append('<table><tr><th>月份</th><th class="n">开仓笔数</th>'
                 '<th class="n">信号数</th><th class="n">每手点数合计</th>'
                 '<th class="n">点/笔</th><th class="n">手数</th>'
                 '<th class="n">毛额(元)</th><th class="n">净额(元)</th>'
                 '<th class="n">滑点(跳)</th><th class="n">运行</th>'
                 '<th class="n">空洞</th></tr>')
        for m in R["月"]:
            slt = "—" if m["滑点跳"] is None else f'{m["滑点跳"]:.1f}'
            gt = "—" if not m["空洞次"] else f'{m["空洞次"]} 次'
            o.append(f'<tr><td class="mono">{m["月"]}</td>'
                     f'<td class="n">{m["笔"]}</td>'
                     f'<td class="n">{m["信号"]}</td>'
                     f'<td class="n">{_fx(m["点合计"])}</td>'
                     f'<td class="n">{_fx(m["点/笔"], 2)}</td>'
                     f'<td class="n">{m["手数"]}</td>'
                     f'<td class="n">{m["毛额"]:+,.0f}</td>'
                     f'<td class="n">{_fx(m["净额"], 0)}</td>'
                     f'<td class="n">{slt}</td>'
                     f'<td class="n">{_dur(m["覆盖秒"])}</td>'
                     f'<td class="n">{gt}</td></tr>')
        o.append('</table>')
        for m in R["月"]:
            if m["滑点跳"] is None:
                slt = "—"
            else:
                slt = f'{m["滑点跳"]:.1f} 跳'
            if m["笔"]:
                txt = (f'{m["笔"]} 笔（多 {m["多"]} / 空 {m["空"]}），'
                       f'每手 {m["点/笔"]:+.2f} 点，净额 {m["净额"]:+,.0f} 元，'
                       f'滑点 {slt}')
            elif m["空洞次"] == 0:
                # 心跳没断过 -> 程序一直在跑，所以「没开仓」= 条件不够
                txt = (f'没有开仓：程序全程在跑（{_dur(m["覆盖秒"])}，无断层），'
                       f'信号 {m["信号"]} 次 —— 属<b>条件不够</b>，不是漏跑')
            elif m["信号"]:
                txt = (f'没有开仓：信号 {m["信号"]} 次但都没成交；'
                       f'期间有 {m["空洞次"]} 次断层（共 {_dur(m["空洞秒"])}）')
            else:
                txt = (f'没有开仓：程序只运行 {_dur(m["覆盖秒"])}，'
                       f'且有 {m["空洞次"]} 次断层（共 {_dur(m["空洞秒"])}）'
                       f' —— 先看是不是没开机，不要当成「策略没信号」')
            o.append(f'<div class="sum"><b>{m["月"]}</b> · {txt}</div>')

    # ---- ③ 逐笔台账 ----
    o.append('<h2>③ 逐笔台账</h2>')
    if not R["成交"]:
        o.append('<div class="warnbox">还没有完成的成交。'
                 '（若下单开关是「关」，那就永远不会有 —— 观察模式只记信号。）</div>')
    else:
        for m in R["月"]:
            ts = [x for x in R["成交"] if x["月"] == m["月"]]
            if not ts:
                continue
            o.append(f'<div class="mhead">{m["月"]}（{len(ts)} 笔，'
                     f'每手 {m["点/笔"]:+.2f} 点）</div>')
            o.append('<table><tr><th>开仓时间</th><th>方向</th><th class="n">手数</th>'
                     '<th class="n">开仓价</th><th>平仓时间</th><th class="n">平仓价</th>'
                     '<th class="n">点数/手</th><th class="n">毛额</th>'
                     '<th class="n">净额</th></tr>')
            for x in ts:
                ot = f'{x["开仓时间"]:%m-%d %H:%M}' if x["开仓时间"] else "—"
                ct = f'{x["平仓时间"]:%m-%d %H:%M}' if x.get("平仓时间") else "—"
                o.append(f'<tr><td class="mono">{ot}</td>'
                         f'<td>{x["方向"]}</td><td class="n">{x["手数"]}</td>'
                         f'<td class="n mono">{x["开仓价"]:.2f}</td>'
                         f'<td class="mono">{ct}</td>'
                         f'<td class="n mono">{x.get("平仓价", 0):.2f}</td>'
                         f'<td class="n">{_fx(x["点/手"], 1)}</td>'
                         f'<td class="n">{_fx(x["金额"], 0)}</td>'
                         f'<td class="n">{_fx(x["净额"], 0)}</td></tr>')
            o.append('</table>')

    # ---- ④ 实测滑点 ----
    o.append('<h2>④ 实测滑点（回测给不了的那个数）</h2>')
    if not R["滑点"]:
        o.append('<div class="warnbox">还没有可配对的成交，暂时算不出滑点。'
                 '（滑点 = 成交价与信号根收盘价之差，所以必须真有成交。）</div>')
    else:
        by = {}
        for x in R["滑点"]:
            by.setdefault(x["类型"], []).append(x["跳"])
        rows = []
        for k in ("开仓", "平仓", "止损"):
            if by.get(k):
                rows.append(f'<tr><td>{k}</td><td class="n">{len(by[k])}</td>'
                            f'<td class="n">{sum(by[k]) / len(by[k]):+.2f} 跳</td>'
                            f'<td class="n">{max(by[k]):+.1f} / {min(by[k]):+.1f}</td></tr>')
        o.append('<table><tr><th>类型</th><th class="n">笔数</th><th class="n">均值</th>'
                 '<th class="n">最大 / 最小</th></tr>' + "".join(rows) + '</table>')
        o.append('<div class="sum">回测假设的是<b>等效 2 跳/次</b>'
                 '（TqSim 对价 1 跳 + 脚本 slip_ticks=1）。'
                 '上面是真实成交测出来的值 —— <b>正数 = 吃亏</b>。'
                 '这个数字跑出 10 笔就能看出趋势，不用等 30 笔。</div>')
        o.append('<table><tr><th>时间</th><th>类型</th><th>方向</th>'
                 '<th class="n">手数</th><th class="n">参考价</th>'
                 '<th class="n">成交价</th><th class="n">滑点</th>'
                 '<th class="n">滞后(秒)</th></tr>')
        for x in R["滑点"][-30:]:
            o.append(f'<tr><td class="mono">{x["时间"]:%m-%d %H:%M:%S}</td>'
                     f'<td>{x["类型"]}</td><td>{x["方向"]}</td>'
                     f'<td class="n">{x["手数"]}</td>'
                     f'<td class="n mono">{x["参考价"]:.2f}</td>'
                     f'<td class="n mono">{x["成交价"]:.2f}</td>'
                     f'<td class="n">{_fx(x["跳"], 1)}</td>'
                     f'<td class="n">{x["滞后"]:.0f}</td></tr>')
        o.append('</table>')

    # ---- ⑤ 信号-成交核对 ----
    if R["下单开关"] == "开":
        o.append('<h2>⑤ 信号-成交核对（有没有被交易所挡掉）</h2>')
        if R["未成交"]:
            o.append('<div class="warnbox">以下开仓信号在 1 小时内没有等到成交，'
                     '可能是被拒（如郑商所「最小开仓手数 8」）或挂单未成 —— '
                     '这正是模拟盘要抓的东西。</div>')
            o.append('<table><tr><th>信号时间</th><th>方向</th><th class="n">手数</th>'
                     '<th class="n">信号根收盘</th><th>备注</th></tr>')
            for s in R["未成交"]:
                o.append(f'<tr><td class="mono">{s["t"]:%m-%d %H:%M:%S}</td>'
                         f'<td>{s["dir"]}</td><td class="n">—</td>'
                         f'<td class="n mono">{s["ref"]:.2f}</td>'
                         f'<td class="hurt">未等到成交</td></tr>')
            o.append('</table>')
        else:
            o.append('<div class="okbox">所有开仓信号都在 1 小时内等到了成交，'
                     '没有被拒或挂单落空。</div>')
    return "\n".join(o)


def _副标题(R):
    """把 START 行那串备注整理成一行干净的副标题（别把原始备注整段倒出来）。"""
    d = {}
    for seg in (R["参数"] or "").split("|"):
        p = seg.strip().split(" ", 1)
        if len(p) == 2:
            d[p[0]] = p[1]
    bits = []
    if R["合约"]:
        bits.append(R["合约"])
    bits.append(f"下单开关 {R['下单开关']}")
    if d.get("波动率预算"):
        bits.append("波动率预算 " + d["波动率预算"])
    elif d.get("固定"):
        bits.append(d["固定"] + "（固定手数）")
    if R["手续费"]:
        bits.append(f"手续费 {R['手续费']:g} 元/手/单边")
    return " ｜ ".join(bits)


def 渲染(name, 策略, R, ev_path, hb_path, out_path):
    if not R["心跳数"] and not R["成交"]:
        body = ('<div class="warnbox">还没有数据。先双击同目录的 '
                '<code>模拟盘.bat</code> 把程序跑起来 —— 跑起来后心跳就一直在写'
                '（休市照写），本页随时能反映运行情况。</div>')
    else:
        body = _正文(R, R.get("_mult", 5), R.get("_tick", 1), 策略)
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{name} {策略} 模拟盘台账</title><style>{CSS}</style></head><body>
<div class="wrap">
<h1>{name} · {策略} 模拟盘实测台账</h1>
<div class="sub">{_副标题(R) or '（尚未启动过）'}
 ｜ 生成于 {datetime.now():%Y-%m-%d %H:%M:%S}</div>
{body}
<div class="foot">
  数据来源：<code>{os.path.relpath(ev_path, _ROOT)}</code>（事件流）＋
  <code>{os.path.relpath(hb_path, _ROOT)}</code>（运行心跳，休市照写）。<br>
  口径：「点数」一律是<b>单笔方向价差（点）</b>，把每笔直接相加、<b>不按手数加权</b>，
  所以它能直接对标样本内 +5.83 点/笔；而「毛额 / 净额」是按手数加权的金额，
  两者在每笔手数不同时会分岔（这是正常的，不是算错）。
  毛额 = 点数 × 合约乘数 {R.get('_mult', 5)} × 手数；净额 = 毛额 − 手续费
  （{R['手续费']:g} 元/手/单边 × 2）。滑点 = 成交价 − 信号根收盘价
  （正数 = 吃亏），1 跳 = {R.get('_tick', 1):g} 点。<br>
  有意不做：收益率 / 回撤曲线 / 夏普 / 卡玛 —— 笔数少时这些指标没有意义，
  判据只认每手点数与实测滑点。
</div>
</div></body></html>"""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write(doc)
    return out_path


def main():
    args = [a for a in sys.argv[1:]]
    if not args:
        print(__doc__)
        return 0
    品种 = args[0]
    策略 = args[1] if len(args) > 1 else "双均线"
    _key, name, mult, tick = _resolve(品种)
    d = os.path.join(_ROOT, "实测记录", 策略目录名(策略), name)
    ev = os.path.join(d, f"{name}_成交流水.csv")
    hb = os.path.join(d, f"{name}_运行心跳.csv")
    out = os.path.join(d, f"{name}_{策略}_台账.html")

    events = _read_csv(ev)
    hearts = _read_csv(hb)
    R = 分析(events, hearts, mult, tick)
    R["_mult"], R["_tick"] = mult, tick
    渲染(name, 策略, R, ev, hb, out)

    n = len(R["成交"])
    pt = sum(x["点/手"] for x in R["成交"])
    sl = [x["跳"] for x in R["滑点"] if x["类型"] != "止损"]
    print(f"[{name} {策略}] 台账已生成")
    print(f"  成交 {n} 笔 ｜ 每手点数 {pt:+.1f}"
          + (f"（每笔 {pt / n:+.2f} 点）" if n else ""))
    print(f"  实测滑点 {'暂无' if not sl else f'{sum(sl) / len(sl):.1f} 跳（{len(sl)} 笔）'}"
          f' ｜ 下单开关 {R["下单开关"]}')
    print(f"  运行 {_dur(R['覆盖秒'])} / {len(R['会话'])} 段会话"
          f" ｜ 断层 {len(R['断层'])} 次（共 {_dur(R['空洞秒'])}）"
          + (f" ｜ 未成交信号 {len(R['未成交'])} 个" if R["下单开关"] == "开" else ""))
    print(f"  -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
