# -*- coding: utf-8 -*-
"""
模拟盘账户分析报告生成器（只读台账，产出单文件 HTML，双击即开、零外部依赖）

用法：直接运行本脚本，会把「模拟盘账户分析.html」写到桌面。
更新周期不定：想更新时重跑一次即可（台账每分钟落一条权益，报告取最新）。

数据源：策略/06_MA13超短线/模拟盘/台账/*_成交流水.csv 里的 EQUITY 行。
口径：权益 = TqKq 快期模拟账户真实权益（已含手续费 + 浮动盈亏），一分钟一条。
     期初 = 名义初始资金 1,000,000（账户于 2026-10-08 由 20 万切到 100 万）。

不碰策略代码、不写台账、纯只读。
"""
import csv, glob, io, os, re, math, datetime, html as _html

LEDGER = r"C:\Users\cc\Desktop\天勤量化\策略\06_MA13超短线\模拟盘\台账"
OUT = r"C:\Users\cc\Desktop\模拟盘账户分析.html"
INIT = 1_000_000.0          # 名义初始资金
CAP_THRESHOLD = 500_000.0   # 权益超过它 = 100 万账户段（20 万旧账户段被排除）


def load_equity():
    rows = []
    for fp in glob.glob(os.path.join(LEDGER, "*_成交流水.csv")):
        with io.open(fp, encoding="utf-8-sig", errors="replace", newline="") as f:
            for r in csv.reader(f):
                if not r or r[0] != "EQUITY" or len(r) < 8:
                    continue
                m = re.search(r"权益=([0-9.]+)", r[7])
                if not m:
                    continue
                p = re.search(r"持仓=(\d+)", r[7])
                rows.append((r[1], float(m.group(1)), int(p.group(1)) if p else 0))
    # 按分钟聚合（每分钟保留最后一条），吸收跨文件写盘的秒级差异
    by_min = {}
    for t, v, p in rows:
        by_min[t[:16]] = (t, v, p)
    return sorted(by_min.values(), key=lambda x: x[0])


def lttb(series, n):
    """Largest-Triangle-Three-Buckets 降采样，保形。series: [(t, v)]"""
    if len(series) <= n:
        return series
    out = [series[0]]
    every = (len(series) - 2) / (n - 2)
    a = 0
    for i in range(1, n - 1):
        avg_x = avg_y = 0.0
        start = int(math.floor(i * every)) + 1
        end = int(math.floor((i + 1) * every)) + 1
        end = min(end, len(series) - 1)
        cnt = end - start
        if cnt <= 0:
            out.append(series[start])
            a = end
            continue
        for j in range(start, end):
            avg_x += j
            avg_y += series[j][1]
        avg_x /= cnt
        avg_y /= cnt
        range_off = math.floor((i - 1) * every) + 1
        range_to = math.floor((i + 1) * every) + 1
        pa = series[a][1]
        # 选择桶内与「(平均点, 前锚点)」构成的三角形面积最大的点
        best = start
        best_area = -1.0
        for j in range(start, end):
            area = abs((a - avg_x) * (series[j][1] - pa) - (a - j) * (avg_y - pa))
            if area > best_area:
                best_area = area
                best = j
        out.append(series[best])
        a = best
    out.append(series[-1])
    return out


def main():
    mins = load_equity()
    # 100 万账户段
    big = [(t, v, p) for (t, v, p) in mins if v >= CAP_THRESHOLD]
    # 20 万旧账户段（仅作说明）
    small = [(t, v, p) for (t, v, p) in mins if v < CAP_THRESHOLD]

    t0 = big[0][0]
    t1 = big[-1][0]
    final = big[-1][1]

    # 日频（每个交易日最后一条，含首日）
    # ⚠️ 2026-11-13 修正：台账按「自然日」落点，周末也有心跳（10-08~11-13 共 8 个
    #    周末日，其中 7 天权益零变化）。原来直接按 t[:10] 分组，等于把周末当成
    #    交易日 ⇒ n 虚高 8 天、零收益日把日波动摊薄、夏普被系统性压低。
    #    实测：修正前 n=35 年化波动 16.17% 夏普 0.648；修正后 n=27 18.60% 0.731。
    #    新口径：只认周一~周五；周末记录并入前一交易日（周末若真有权益变动，
    #    通常是结算或手续费补扣，归属上一个交易日更合理，也不丢数据）。
    daily = {}
    last_wd = None
    for t, v, p in big:
        d = t[:10]
        if datetime.date.fromisoformat(d).weekday() < 5:
            last_wd = d
            daily[d] = (t, v)
        elif last_wd is not None:
            daily[last_wd] = (t, v)
    days = sorted(daily.items())
    day_ret = []          # [(date, ret), ...] 第一项相对 INIT
    prev = INIT
    for d, (t, v) in days:
        day_ret.append((d, (v - prev) / prev))
        prev = v
    rets = [r for d, r in day_ret]

    total_pnl = final - INIT
    total_ret = total_pnl / INIT
    n = len(rets)
    mu = sum(rets) / n
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / n) if n > 1 else 0.0
    ann_vol = sd * math.sqrt(252)
    sharpe = (mu / sd) * math.sqrt(252) if sd > 0 else 0.0

    # 最大回撤（rolling peak，起点含 INIT）
    peak = INIT
    peak_t = t0
    mdd = 0.0
    mdd_from = mdd_to = t0
    for t, v, p in big:
        if v > peak:
            peak = v
            peak_t = t
        dd = (peak - v) / peak
        if dd > mdd:
            mdd = dd
            mdd_from = peak_t
            mdd_to = t
    lo_v = min(v for t, v, p in big)
    up = sum(1 for r in rets if r > 1e-9)
    down = sum(1 for r in rets if r < -1e-9)

    # FILL 条数（开平各一条，约等于 2×回合数）
    fills = 0
    for fp in glob.glob(os.path.join(LEDGER, "*_成交流水.csv")):
        with io.open(fp, encoding="utf-8-sig", errors="replace", newline="") as f:
            for r in csv.reader(f):
                if r and r[0] == "FILL":
                    fills += 1

    html_doc = render(big, small, days, day_ret, {
        "t0": t0, "t1": t1, "final": final, "total_pnl": total_pnl,
        "total_ret": total_ret, "ann_vol": ann_vol, "sharpe": sharpe,
        "mdd": mdd, "mdd_from": mdd_from, "mdd_to": mdd_to, "lo_v": lo_v,
        "n": n, "up": up, "down": down, "fills": fills,
    })
    io.open(OUT, "w", encoding="utf-8", newline="\n").write(html_doc)
    print("已生成:", OUT, os.path.getsize(OUT), "字节")
    print(f"期初 1,000,000 -> 期末 {final:,.2f} | 盈亏 {total_pnl:+,.2f} | {total_ret*100:+.3f}%")
    print(f"最大回撤 {mdd*100:.2f}% ({mdd_from} -> {mdd_to})")
    print(f"年化波动 {ann_vol*100:.2f}% | 夏普 {sharpe:.3f} | 交易日 {n} | 涨{up}/跌{down} | FILL {fills}")


# ---------------- 渲染 ----------------

def esc(s):
    return _html.escape(str(s))


def render(big, small, days, day_ret, K):
    W, H = 1160, 400
    PL, PR, PT, PB = 70, 20, 20, 40
    iw, ih = W - PL - PR, H - PT - PB

    # 曲线数据：全 100 万段分钟级降采样到 1400 点
    pts = lttb([(t, v) for t, v, p in big], 1400)
    vs = [v for t, v in pts]
    vmin = min(vs)
    vmax = max(vs)
    # 留 8% 边距
    span = (vmax - vmin) or 1.0
    lo = vmin - span * 0.08
    hi = vmax + span * 0.08

    def X(t):
        # 时间线性映射：用整段分钟跨度
        import datetime as _dt
        a = _dt.datetime.strptime(K["t0"][:16], "%Y-%m-%d %H:%M")
        b = _dt.datetime.strptime(K["t1"][:16], "%Y-%m-%d %H:%M")
        tt = _dt.datetime.strptime(t[:16], "%Y-%m-%d %H:%M")
        return PL + (tt - a).total_seconds() / max((b - a).total_seconds(), 1) * iw

    def Y(v):
        return PT + (hi - v) / (hi - lo) * ih

    # 权益折线 path
    path = "M" + " L".join(f"{X(t):.1f},{Y(v):.1f}" for t, v in pts)
    # 面积填充到底部
    area = path + f" L{X(pts[-1][0]):.1f},{PT+ih:.1f} L{X(pts[0][0]):.1f},{PT+ih:.1f} Z"

    # Y 轴刻度（5 档）
    yticks = ""
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        yticks += (f'<text x="{PL-10}" y="{Y(v)+4:.1f}" text-anchor="end" '
                   f'class="ax">{v/10000:.1f}万</text>'
                   f'<line x1="{PL}" y1="{Y(v):.1f}" x2="{W-PR}" y2="{Y(v):.1f}" class="grid"/>')

    # X 轴刻度：取约 8 个交易日
    import datetime as _dt
    xticks = ""
    for i, (d, (t, v)) in enumerate(days):
        if len(days) <= 10 or i % max(1, (len(days) - 1) // 8) == 0 or i == len(days) - 1:
            xx = X(t)
            xticks += f'<text x="{xx:.1f}" y="{H-PB+18}" text-anchor="middle" class="ax">{d[5:]}</text>'

    # 回撤阴影（跌=绿）
    # 计算 rolling peak 序列，画回撤面积
    peak = INIT
    dd_pts = []
    for t, v in pts:
        if v > peak:
            peak = v
        dd_pts.append((t, v, peak))
    dd_area = ""
    if dd_pts:
        top = [f"{X(t):.1f},{Y(v):.1f}" for t, v, pk in dd_pts]
        bot = [f"{X(t):.1f},{Y(pk):.1f}" for t, v, pk in reversed(dd_pts)]
        dd_area = "M" + " L".join(top) + " L" + " L".join(bot) + " Z"

    # 日收益柱状
    BW, BH = 1160, 200
    bPL, bPR, bPT, bPB = 70, 20, 16, 32
    bw, bh = BW - bPL - bPR, BH - bPT - bPB
    rmax = max((abs(r) for d, r in day_ret), default=1e-6)
    bars = ""
    nbars = len(day_ret)
    step = bw / nbars
    barw = step * 0.62
    for i, (d, r) in enumerate(day_ret):
        hh = abs(r) / rmax * bh
        x = bPL + i * step + (step - barw) / 2
        y = bPT + (bh - hh) if r >= 0 else bPT + bh * 0.5
        color = "#ef4444" if r >= 0 else "#22c55e"
        if r >= 0:
            bars += f'<rect x="{x:.1f}" y="{y:.1f}" width="{barw:.1f}" height="{hh:.1f}" rx="2" fill="{color}"/>'
        else:
            bars += f'<rect x="{x:.1f}" y="{bPT+bh*0.5:.1f}" width="{barw:.1f}" height="{hh:.1f}" rx="2" fill="{color}"/>'
        if nbars <= 30:
            bars += f'<text x="{x+barw/2:.1f}" y="{BH-bPB+16}" text-anchor="middle" class="ax">{d[5:]}</text>'

    # 每日明细表
    rows_html = ""
    prev = INIT
    for i, (d, (t, v)) in enumerate(days):
        r = day_ret[i][1]
        pnl = v - prev
        prev = v
        cls = "pos" if r >= 0 else "neg"
        rows_html += (f'<tr><td>{d}</td><td class="num">{v:,.2f}</td>'
                      f'<td class="num {cls}">{pnl:+,.2f}</td>'
                      f'<td class="num {cls}">{r*100:+.2f}%</td></tr>')

    def card(label, val, sub, accent=""):
        return (f'<div class="card"><div class="cl">{esc(label)}</div>'
                f'<div class="cv" style="color:{accent}">{esc(val)}</div>'
                f'<div class="cs">{esc(sub)}</div></div>')

    total_color = "#ef4444" if K["total_pnl"] >= 0 else "#22c55e"

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>模拟盘账户分析</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0f1117;color:#e6e6e6;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;padding:28px}}
.wrap{{max-width:1220px;margin:0 auto}}
h1{{font-size:22px;font-weight:600}}
.sub{{color:#9aa3af;font-size:13px;margin:6px 0 22px}}
.sub b{{color:#e6e6e6}}
.cards{{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:24px}}
.card{{background:#1a1d27;border:1px solid #262a36;border-radius:10px;padding:14px 16px}}
.cl{{color:#9aa3af;font-size:12px;margin-bottom:6px}}
.cv{{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}}
.cs{{color:#9aa3af;font-size:12px;margin-top:4px}}
.panel{{background:#1a1d27;border:1px solid #262a36;border-radius:12px;padding:18px 20px;margin-bottom:20px}}
.panel h2{{font-size:15px;font-weight:600;margin-bottom:12px;color:#d7dbe3}}
.ax{{fill:#9aa3af;font-size:11px}}
.grid{{stroke:#262a36;stroke-width:1}}
.line{{fill:none;stroke:#5b9cff;stroke-width:1.8}}
.areafill{{fill:url(#g);opacity:.25}}
.ddfill{{fill:#22c55e;opacity:.12}}
.zeroline{{stroke:#3a4050;stroke-dasharray:4 4}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:7px 10px;text-align:right;border-bottom:1px solid #232733}}
th:first-child,td:first-child{{text-align:left}}
th{{color:#9aa3af;font-weight:500;font-size:12px}}
td.num{{font-variant-numeric:tabular-nums}}
.pos{{color:#ef4444}}.neg{{color:#22c55e}}
.foot{{color:#7a8494;font-size:12px;margin-top:20px;line-height:1.8}}
.foot b{{color:#b6bdc9}}
.tag{{display:inline-block;background:#262a36;border-radius:5px;padding:1px 8px;font-size:11px;color:#9aa3af;margin-left:6px}}
</style></head><body><div class="wrap">
<h1>模拟盘账户分析 <span class="tag">MA13 实时盘</span></h1>
<div class="sub">区间 <b>{esc(K["t0"])}</b> → <b>{esc(K["t1"])}</b> ｜ 账户 <b>TqKq 快期模拟</b> ｜ 数据 <b>本地台账（一分钟一条权益，含手续费+浮动）</b></div>

<div class="cards">
{card("期末权益", f"{K['final']:,.2f}", "初始 1,000,000")}
{card("总盈亏", f"{K['total_pnl']:+,.2f}", f"{K['total_ret']*100:+.3f}%", total_color)}
{card("最大回撤", f"{K['mdd']*100:.2f}%", f"{esc(K['mdd_from'][5:16])} → {esc(K['mdd_to'][5:16])}")}
{card("年化波动", f"{K['ann_vol']*100:.2f}%", "日收益×√252")}
{card("夏普比率", f"{K['sharpe']:.2f}", "年化，无风险=0")}
{card("交易/涨跌", f"{K['n']} 日", f"涨{K['up']} / 跌{K['down']} ｜ FILL {K['fills']}")}
</div>

<div class="panel"><h2>权益曲线（分钟级，回撤阴影为绿）</h2>
<svg viewBox="0 0 {W} {H}" width="100%">
<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="#5b9cff" stop-opacity=".5"/><stop offset="1" stop-color="#5b9cff" stop-opacity="0"/>
</linearGradient></defs>
{yticks}{xticks}
<path d="{dd_area}" class="ddfill"/>
<path d="{area}" class="areafill"/>
<path d="{path}" class="line"/>
</svg></div>

<div class="panel"><h2>每日收益（涨红跌绿）</h2>
<svg viewBox="0 0 {BW} {BH}" width="100%">
<line x1="{bPL}" y1="{bPT+bh*0.5:.1f}" x2="{bPL+bw}" y2="{bPT+bh*0.5:.1f}" class="zeroline"/>
{bars}
</svg></div>

<div class="panel"><h2>每日明细</h2>
<table><thead><tr><th>日期</th><th>期末权益</th><th>当日盈亏</th><th>当日收益率</th></tr></thead>
<tbody>{rows_html}</tbody></table></div>

<div class="foot">
<b>口径说明</b><br>
① 权益来自策略自建台账的 <b>EQUITY</b> 记录（每分钟一条，写盘时间戳随交易留痕），等于 TqKq 快期模拟账户真实权益，<b>已含手续费与浮动盈亏</b>，无需再扣成本。<br>
② 期初 = 名义初始资金 <b>100 万</b>（2026-10-08 由 20 万旧账户切至 100 万，切换前 20 万段未计入本报告）。<br>
③ 样本仅 <b>{K['n']} 个交易日</b>（只计周一~周五；台账在周末也有心跳记录，已并入前一交易日，不计作独立交易日），年化波动 / 夏普统计意义有限，只作参考。<br>
④ <b>这是自建台账，不是第三方托管结算单</b>。用于求职时应如实标注「模拟盘 + 本地留痕」，不冒充实盘业绩。
</div>
</div></body></html>"""


if __name__ == "__main__":
    main()
