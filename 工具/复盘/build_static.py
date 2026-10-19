# -*- coding: utf-8 -*-
"""
生成**纯前端单页**复盘终端：把所有数据 + Plotly 一次性嵌进一个 HTML。
双击打开即可，切换品种/周期/区间、拖动缩放全部在浏览器里完成，不再经过 Streamlit。

用法：
  .venv_viz\\Scripts\\python.exe 工具\\复盘\\build_static.py
输出：
  工具\\复盘\\复盘终端.html
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import analyze
import data
import features
import sessions
import trades as T

HERE = Path(__file__).resolve().parent
OUT = HERE / "复盘终端.html"
PERIODS = (5, 15, 1440)

# ★ 复盘只保留「模拟盘实际跑过」的窗口，不再装几个月/几年样本
#   指标在**全量**K线上算完再裁显示窗口，所以 5/15min 不需要预热；
#   日线保留 30 天是为了「看得到上一段行情」，不是为指标（指标同样全量算）。
LEAD_IN_DAYS = 1       # 5/15/1min：模拟盘首日再往前 1 天（夜盘归属次日，防止漏掉首日夜盘）
WARMUP_DAYS_D = 30     # 日线：模拟盘首日往前 30 天（约 21 个交易日）


def _x(s, period):
    s = pd.Timestamp(s)
    return s.strftime("%Y-%m-%d" if period == 1440 else "%Y-%m-%d %H:%M")


def _arr(vals, nd=2):
    return [None if pd.isna(v) else round(float(v), nd) for v in vals]


def _holiday_dates(x_list) -> list[str]:
    """从 K 线日期推导停市日：相邻 bar 间隔 > 1 天时，取中间**工作日**。

    周末不返回（已由 rangebreaks 的 `bounds:['sat','mon']` 处理，避免重复/误伤）；
    节假日不写死，明年/后年数据一换自动推导。
    """
    dates = sorted({pd.Timestamp(s).normalize() for s in x_list})
    out: list[str] = []
    for a, b in zip(dates[:-1], dates[1:]):
        gap = (b - a).days
        if gap <= 1:
            continue
        for k in range(1, gap):
            d = a + pd.Timedelta(days=k)
            if d.weekday() < 5:
                out.append(d.strftime("%Y-%m-%d"))
    return out


def _clean_nan(o):
    """递归把 NaN/Inf 换成 None（JSON 里是 null，前端统一显示 —）。"""
    if isinstance(o, dict):
        return {k: _clean_nan(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean_nan(v) for v in o]
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    return o


def build_stats(df: pd.DataFrame, exc: pd.DataFrame, n_closes: int,
                order: list[str] | None = None, with_by_symbol: bool = False) -> dict:
    """P1 统计（口径写死，不调数）：

    · `pnl` = 毛利，未扣手续费（trades.py 的契约）
    · `n_takeover` = **开仓价缺失且已平仓**（不含未平仓）
    · `n_settled` = `pnl` 非空；`n_open` = `平仓价` 为空
    · 超损 `slip = (平仓价 - 止损价) × 方向`：**正=优于止损价，负=差于止损价**
    · 超损样本 = 所有 `原因=价格止损`（含接管笔；§5.3 明确允许用 s/p）
    · `sum_amount = slip × 手数 × 合约乘数`
    """
    pnl = pd.to_numeric(df["盈亏"], errors="coerce")
    settled = pnl.notna()
    p_null = df["平仓价"].isna()
    e_null = df["开仓价"].isna()
    n_trades = int(len(df))
    n_settled = int(settled.sum())
    n_takeover = int((e_null & ~p_null).sum())
    n_open = int(p_null.sum())

    p = pnl[settled]
    win = int((p > 0).sum())
    loss = int((p < 0).sum())
    flat = int((p == 0).sum())
    avg_win = float(p[p > 0].mean()) if win else None
    avg_loss = float(p[p < 0].mean()) if loss else None
    payoff = (abs(avg_win / avg_loss)
              if (avg_win is not None and avg_loss not in (None, 0)) else None)
    h = pd.to_numeric(df.loc[settled, "持仓分钟"], errors="coerce").dropna()

    # 出场原因：只用已结算 91 笔；按合计升序（最亏的在前）
    st = df[settled]
    by_reason = []
    for r, g in st.groupby("原因"):
        gp = pd.to_numeric(g["盈亏"], errors="coerce")
        by_reason.append({
            "r": str(r), "n": int(len(g)),
            "sum": float(gp.sum()), "mean": float(gp.mean()),
            "win": int((gp > 0).sum()),
        })
    by_reason.sort(key=lambda x: x["sum"])

    # 超损：价格止损（含接管笔）
    ps = df[df["原因"] == "价格止损"].copy()
    sign = ps["方向"].map({"多": 1, "空": -1})
    slip = (pd.to_numeric(ps["平仓价"], errors="coerce")
            - pd.to_numeric(ps["止损"], errors="coerce")) * sign
    mult = ps["key"].map(data.MULT).fillna(1) if "key" in ps.columns else 1
    amount = slip * pd.to_numeric(ps["手数"], errors="coerce") * mult
    ok = slip.notna()
    good = pd.DataFrame({"滑点": slip[ok]})
    bad = ps[~ok]
    sl = analyze.slippage_stats(good, bad)
    sd = slip[ok]
    slippage = {
        "n_ok": int(sl["n_ok"]), "n_excluded": int(sl["n_excluded"]),
        "median": sl["median"], "mean": sl["mean"],
        "p10": float(sd.quantile(0.10)) if len(sd) else None,
        "p25": sl["p25"], "p75": sl["p75"],
        "min": float(sd.min()) if len(sd) else None,
        "worse_n": int((sd < 0).sum()),
        "worse_share": float((sd < 0).mean()) if len(sd) else None,
        "sum_amount": float(amount[ok].sum()) if len(sd) else None,
    }

    ex = analyze.excursion_stats(exc, n_closes) if exc is not None else {}
    excursion = {k: ex.get(k) for k in (
        "n", "n_closes", "coverage", "mfe_median", "mae_median",
        "mfe_p25", "mfe_p75", "mae_p25", "mae_p75", "mfe_mean", "mae_mean")}

    by_symbol = []
    if with_by_symbol and order:
        for k in order:
            sub = df[df["key"] == k] if "key" in df.columns else df.iloc[0:0]
            sp = pd.to_numeric(sub["盈亏"], errors="coerce")
            by_symbol.append({
                "key": k, "name": data.RECENT_SPECS[k][0],
                "n": int(len(sub)), "pnl_n": int(sp.notna().sum()),
                "sum": float(sp.sum()) if sp.notna().any() else 0.0,
            })

    return _clean_nan({
        "n_trades": n_trades, "n_settled": n_settled,
        "n_takeover": n_takeover, "n_open": n_open,
        "win": win, "loss": loss, "flat": flat,
        "pnl_sum": float(p.sum()) if len(p) else 0.0,
        "pnl_mean": float(p.mean()) if len(p) else None,
        "win_rate": (win / n_settled) if n_settled else None,
        "avg_win": avg_win, "avg_loss": avg_loss, "payoff": payoff,
        "hold_median": float(h.median()) if len(h) else None,
        "hold_mean": float(h.mean()) if len(h) else None,
        "by_reason": by_reason, "by_symbol": by_symbol,
        "slippage": slippage, "excursion": excursion,
    })


def build_data() -> dict:
    out = {}
    ledger = analyze.load_ledger()
    exc_all = analyze.excursion_rows(ledger)
    n_closes_all = analyze.closes_count(ledger)

    # ★ 模拟盘窗口：以台账 FILL 的最早/最晚为界（不含 START/RECOVER/EQUITY）
    fill_t = pd.to_datetime(
        ledger.loc[ledger["类型"] == "FILL", "记录时间"], errors="coerce").dropna()
    if len(fill_t):
        sim_start = fill_t.min().normalize()
        sim_end = fill_t.max().normalize()
    else:
        sim_start = pd.Timestamp.now().normalize() - pd.Timedelta(days=LEAD_IN_DAYS)
        sim_end = pd.Timestamp.now().normalize()
    start_i = sim_start - pd.Timedelta(days=LEAD_IN_DAYS)
    start_d = sim_start - pd.Timedelta(days=WARMUP_DAYS_D)
    print(f"[window] 模拟盘 {sim_start.date()} ~ {sim_end.date()} ｜ "
          f"5/15/1min ≥ {start_i.date()} ｜ 日线 ≥ {start_d.date()}", flush=True)

    all_tr = []
    for key in data.RECENT_SPECS:
        cn = data.RECENT_SPECS[key][0]
        print(f"  {cn} {key} …", flush=True)
        df5, _ = data.load_recent(key, 5)
        last_price = float(df5["close"].iloc[-1])

        periods = {}
        for p in PERIODS:
            df, _ = data.load_recent(key, p)
            if p == 1440:
                df = df.assign(x=df["datetime"].dt.normalize() + pd.Timedelta(days=1))
            else:
                df = df.assign(x=data.to_beijing(df["datetime"]))
            # ★ 先在全量上算指标（窗口首根 MA13/ATR14 就有值、不被预热吃掉），再裁显示窗口
            F = features.build_custom(df)
            cf = F["close"].astype(float)
            ema12 = cf.ewm(span=12, adjust=False).mean()
            ema26 = cf.ewm(span=26, adjust=False).mean()
            dif = ema12 - ema26
            dea = dif.ewm(span=9, adjust=False).mean()
            hist = (dif - dea) * 2
            delta = cf.diff()
            up = delta.clip(lower=0).rolling(14).mean()
            dn = (-delta.clip(upper=0)).rolling(14).mean()
            rs = up / dn.replace(0, np.nan)
            F["dif"], F["dea"], F["hist"] = dif, dea, hist
            F["rsi"] = 100 - 100 / (1 + rs)
            low9 = F["low"].astype(float).rolling(9).min()
            high9 = F["high"].astype(float).rolling(9).max()
            rsv = (cf - low9) / (high9 - low9).replace(0, np.nan) * 100
            F["k"] = rsv.ewm(com=2, adjust=False).mean()
            F["d"] = F["k"].ewm(com=2, adjust=False).mean()
            F["j"] = 3 * F["k"] - 2 * F["d"]
            f = (F[F["x"] >= start_d] if p == 1440
                 else F[F["x"].dt.normalize() >= start_i]).reset_index(drop=True)
            periods[str(p)] = {
                "x": [_x(v, p) for v in f["x"]],
                "o": _arr(f["open"], 1),
                "h": _arr(f["high"], 1),
                "l": _arr(f["low"], 1),
                "c": _arr(f["close"], 1),
                "v": [int(v) if pd.notna(v) else 0 for v in f["volume"]],
                "ma": _arr(f["ma"], 2),
                "atr": _arr(f["atr"], 2),
                "slope": _arr(f["slope"], 3),
                "atr_dist": _arr(f["atr_dist"], 3),
                "boll_u": _arr(f["boll_upper"], 2),
                "boll_m": _arr(f["boll_mid"], 2),
                "boll_l": _arr(f["boll_lower"], 2),
                "day": [pd.Timestamp(sessions.trading_day_of_bj(v)).strftime("%Y-%m-%d") for v in f["x"]],
                "dif": _arr(f["dif"], 2), "dea": _arr(f["dea"], 2), "hist": _arr(f["hist"], 2),
                "rsi": _arr(f["rsi"], 2), "k": _arr(f["k"], 2),
                "d": _arr(f["d"], 2), "j": _arr(f["j"], 2),
            }

        # ★ P0-2：把事件/成交时间吸附到 5min K 线网格（最后一个 ≤ 该时刻的 bar）
        #   不能简单 floor 到 5 分钟：10:15:xx / 夜间边界会落到不存在的 bar 上。
        grid5 = pd.DatetimeIndex(pd.to_datetime(periods["5"]["x"]))
        xs5_set = set(periods["5"]["x"])

        def _snap5(ts):
            ts = pd.Timestamp(ts)
            i = int(grid5.searchsorted(ts, side="right")) - 1
            if i < 0:
                i = 0
            if i >= len(grid5):
                i = len(grid5) - 1
            return grid5[i].strftime("%Y-%m-%d %H:%M")

        ev = T.load_events(cn)
        events = []
        for _, r in ev.iterrows():
            if pd.isna(r["价格"]):
                continue
            events.append({
                "t": _snap5(r["时间"]),
                "day": pd.Timestamp(sessions.trading_day_of_bj(r["时间"])).strftime("%Y-%m-%d"),
                "y": r["类型"], "d": r["方向"], "q": int(r["手数"]), "p": float(r["价格"]),
            })

        tr = T.load_trades(cn)
        trades = []
        for i, r in tr.iterrows():
            en = r["开仓时间"]
            ex = r["平仓时间"]
            trades.append({
                "i": int(i),
                "t": _snap5(en) if pd.notna(en) else None,
                "x": _snap5(ex) if pd.notna(ex) else None,
                "day": pd.Timestamp(sessions.trading_day_of_bj(en)).strftime("%Y-%m-%d") if pd.notna(en) else None,
                "xday": pd.Timestamp(sessions.trading_day_of_bj(ex)).strftime("%Y-%m-%d") if pd.notna(ex) else None,
                "d": r["方向"], "q": int(r["手数"]),
                "e": None if pd.isna(r["开仓价"]) else round(float(r["开仓价"]), 1),
                "p": None if pd.isna(r["平仓价"]) else round(float(r["平仓价"]), 1),
                "s": None if pd.isna(r["止损"]) else round(float(r["止损"]), 2),
                "ps": None if pd.isna(r["计划止损"]) else round(float(r["计划止损"]), 2),
                "mfe": None if pd.isna(r["MFE"]) else round(float(r["MFE"]), 0),
                "mae": None if pd.isna(r["MAE"]) else round(float(r["MAE"]), 0),
                "pnl": None if pd.isna(r["盈亏"]) else round(float(r["盈亏"]), 0),
                "r": None if pd.isna(r["原因"]) else str(r["原因"]),
                "k": None if pd.isna(r["配对"]) else str(r["配对"]),
                "h": None if pd.isna(r["持仓分钟"]) else round(float(r["持仓分钟"]), 0),
            })

        df1, _ = data.load_recent(key, 1)
        df1 = df1.assign(x=data.to_beijing(df1["datetime"]))
        lab_all = sessions.label_sessions(df1)
        # day_prev 用未裁剪的 1min 算（窗口首日的昨收要取窗口外那一天），算完再裁字典
        g = lab_all.groupby("交易日")["close"].last()
        g.index = pd.to_datetime(g.index)
        day_prev_all = {}
        for j, d in enumerate(list(g.index)):
            day_prev_all[d.strftime("%Y-%m-%d")] = round(float(g.iloc[j - 1] if j > 0 else g.iloc[j]), 1)
        keep_from = (start_i - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        day_prev = {k: v for k, v in day_prev_all.items() if k >= keep_from}
        lab = lab_all[lab_all["x"].dt.normalize() >= start_i].copy()
        m1 = {
            "x": [_x(v, 1) for v in lab["x"]],
            "o": _arr(lab["open"], 1),
            "c": _arr(lab["close"], 1),
            "v": [int(v) if pd.notna(v) else 0 for v in lab["volume"]],
            "day": [pd.Timestamp(v).strftime("%Y-%m-%d") for v in lab["交易日"]],
        }
        day_rows = lab[lab["时段"].isin(["早盘", "午盘"])]
        last_day = (pd.Timestamp(day_rows["交易日"].iloc[-1]).strftime("%Y-%m-%d")
                    if len(day_rows) else m1["day"][-1])

        # ★ P0-1：僵尸腿 trades 与对应 events 一起剔除
        #   诊断（EB）：9 条开仓 FILL → 8 笔 trades（首笔 6+7 合并成 13 手），
        #   所以 note 的“8 笔”与 events 的“9 条”不是矛盾，是两个口径（笔数 vs FILL 数）。
        zombie = [t for t in trades if t["k"] == "SYNC清零" and t["p"] is None]
        removed_events = []
        if zombie:
            trades = [t for t in trades if not (t["k"] == "SYNC清零" and t["p"] is None)]

            def _is_zombie_event(e):
                if e["y"] not in ("开", "平"):
                    return False
                for z in zombie:
                    if e["d"] != z["d"]:
                        continue
                    t0, t1 = z["t"], z["x"]
                    if t0 is None:
                        continue
                    if e["t"] == t0:
                        return True
                    if t1 is not None and t0 <= e["t"] <= t1:
                        return True
                return False

            removed_events = [e for e in events if _is_zombie_event(e)]
            events = [e for e in events if not _is_zombie_event(e)]
        note = (f"{'策略已停：' if key == 'EB' else ''}"
                f"{len(zombie)} 笔僵尸腿（对应 {len(removed_events)} 条开/平 FILL）已隐藏"
                if zombie else "")
        # ★ 僵尸腿过滤后重新编号：前端用 i 作为 DATA.trades 的数组下标
        for j, t in enumerate(trades):
            t["i"] = j

        holidays = _holiday_dates(periods["5"]["x"])
        holidays_d = _holiday_dates(periods["1440"]["x"])
        meta = dict(data.RECENT_META.get(
            key, {"has_1015_break": True, "night_end": 23.0}))

        bad5 = sum(1 for e in events if e["t"] not in xs5_set)
        print(f"    [P0-1] zombie trades={len(zombie)} events_removed={len(removed_events)}"
              f" | [P0-2] events={len(events)} not_on_5m={bad5}"
              f" | holidays={len(holidays)}/daily={len(holidays_d)} | meta={meta}", flush=True)

        # ★ P1：统计（per-symbol）；用 P0 清洗后的原始 tr，不用 JSON 里四舍五入的 trades
        zmask = (tr["配对"] == "SYNC清零") & tr["平仓价"].isna()
        tr_clean = tr[~zmask].copy()
        tr_clean["key"] = key
        all_tr.append(tr_clean)
        exc_sym = exc_all[exc_all["品种"] == cn] if len(exc_all) else exc_all
        led_sym = ledger[ledger["品种"] == cn]
        stats = build_stats(tr_clean, exc_sym, analyze.closes_count(led_sym))

        out[key] = {
            "name": cn, "last": round(last_price, 1), "note": note,
            "periods": periods, "events": events, "trades": trades,
            "m1": m1, "day_prev": day_prev, "last_day": last_day,
            "holidays": holidays, "holidays_d": holidays_d, "meta": meta,
            "stats": stats,
        }

    # ★ P1：全样本统计（107 笔口径），挂到每个品种下供统计 tab 使用
    all_df = pd.concat(all_tr, ignore_index=True)
    stats_all = build_stats(all_df, exc_all, n_closes_all,
                            order=list(data.RECENT_SPECS), with_by_symbol=True)
    for k in out:
        out[k]["stats_all"] = stats_all
    sl = stats_all["slippage"]
    print(f"[P1] n={stats_all['n_trades']} settled={stats_all['n_settled']} "
          f"takeover={stats_all['n_takeover']} open={stats_all['n_open']} "
          f"win/loss/flat={stats_all['win']}/{stats_all['loss']}/{stats_all['flat']} "
          f"pnl={stats_all['pnl_sum']:.0f}", flush=True)
    print(f"[P1] slip n={sl['n_ok']} worse={sl['worse_n']}/{sl['n_ok']} "
          f"median={sl['median']:.2f} sum={sl['sum_amount']:.0f}", flush=True)
    print("[P1] by_symbol: " + " | ".join(
        f"{x['key']} {x['n']}/{x['pnl_n']}/{x['sum']:.0f}"
        for x in stats_all["by_symbol"]), flush=True)
    return out


def main() -> int:
    # ★ 2026-10-18 防回退雷：本脚本是**旧版单体**生成器（v10 快照，无回放/模拟下单/决策标记），
    #   输出路径与现行 build/build_html.py 相同。误跑一次就会把 P0~P2-2 全部静默覆盖回旧版。
    #   ⇒ 只要 src/ 模块树存在（= 现行链路），默认拒绝写盘；确需旧版快照请显式 --force。
    if (HERE / "src").is_dir() and "--force" not in sys.argv:
        print("=" * 72)
        print("已拒绝执行：检测到 src/ 模块树 ⇒ 现行链路是 build/build_html.py。")
        print("本脚本（build_static.py）是旧版单体生成器，会覆盖 复盘终端.html，")
        print("把回放引擎 / 模拟下单(P2-1) / 决策标记(P2-2) 全部打回旧版。")
        print("如确需旧版快照，请显式加 --force；日常刷新数据请跑 刷新数据.bat。")
        print("=" * 72)
        return 2
    print("读取数据…", flush=True)
    payload = build_data()
    print("生成 HTML…", flush=True)

    from plotly.offline import get_plotlyjs
    plotly_js = get_plotlyjs().replace("</script>", "<\\/script>")
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    html = TEMPLATE.replace("__PLOTLY__", plotly_js).replace("__DATA__", data_json)
    OUT.write_text(html, encoding="utf-8")
    print(f"完成：{OUT}")
    print(f"大小：{OUT.stat().st_size / 1024 / 1024:.1f} MB")

    # ★ 桌面快捷方式指向全英文路径（.url 的 file:// 对中文路径会按 ANSI 解码失败）
    ascii_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ReviewTerminal"
    ascii_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(OUT, ascii_dir / "index.html")
    print(f"快捷方式目标：{ascii_dir / 'index.html'}")
    return 0


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>复盘终端</title>
<script>__PLOTLY__</script>
<style>
*{box-sizing:border-box}
body{margin:0;background:#0f1117;color:#e6e6e6;font-family:"Microsoft YaHei",Arial,sans-serif;font-size:12px}
#app{height:100vh;padding:8px;display:flex;flex-direction:column;gap:6px}
#top{display:flex;align-items:center;gap:8px;padding:6px 10px;background:linear-gradient(90deg,#111827,#1f2b45);border-radius:6px}
.brand{font-weight:700;font-size:14px;white-space:nowrap}
.brand span{font-size:10px;opacity:.5;margin-left:6px;font-weight:400}
.spacer{flex:1}
.btns{display:flex;gap:4px;flex-wrap:wrap}
button{background:#1a1d26;color:#d6dae2;border:1px solid #2a2f3a;border-radius:4px;padding:4px 8px;cursor:pointer;font-family:inherit;font-size:11px}
button:hover{border-color:#3a4350}
button.on{background:#e5484d;border-color:#e5484d;color:#fff;font-weight:700}
select{background:#1a1d26;color:#d6dae2;border:1px solid #2a2f3a;border-radius:4px;padding:4px 6px;font-family:inherit;font-size:11px;outline:none}
button:disabled{opacity:.4;cursor:not-allowed}
#replaybar{display:flex;align-items:center;gap:6px;padding:4px 8px;background:#151d2e;border:1px solid #2a2f3a;border-radius:6px}
#replaybar .rl{color:#9aa3af}
#replaybar .rsep{width:1px;height:16px;background:#2a2f3a;margin:0 2px}
input[type=date]{background:#0f1117;color:#d6dae2;border:1px solid #2a2f3a;border-radius:4px;padding:3px 5px;font-family:inherit;font-size:11px;color-scheme:dark}
#rpos{font-size:10px;color:#9aa3af}
#grid{flex:1;min-height:0;display:grid;grid-template-columns:200px 1fr;gap:8px}
#grid.with-right{grid-template-columns:200px 1fr 300px}
#left,#right{display:flex;flex-direction:column;gap:6px;min-height:0}
#grid #right{display:none}
#grid.with-right #right{display:flex}
#center{display:flex;flex-direction:column;gap:4px;min-width:0;min-height:0}
.card{background:#1a1d26;border:1px solid #2a2f3a;border-radius:8px;padding:6px}
.ct{font-size:10px;color:#9aa3af;margin-bottom:2px}
#intra{height:170px}
#qcard{font-size:11px;line-height:1.75;color:#d6dae2}
#qstrip{display:flex;align-items:baseline;gap:12px;padding:5px 10px;background:linear-gradient(90deg,#151d2e,#1f2d47);border-radius:6px;white-space:nowrap;overflow:hidden}
#qstrip .name{font-size:15px;font-weight:700}
#qstrip .px{font-size:24px;font-weight:800}
#qstrip .chg{font-size:13px}
#qstrip .ohlc{font-size:11px;opacity:.75}
#qstrip .seg{margin-left:auto;font-size:11px;opacity:.65}
#main{flex:1;min-height:0}
#cap{font-size:11px;color:#9aa3af;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#trades{flex:1;min-height:0;overflow:auto;padding:4px}
table{width:100%;border-collapse:collapse;font-size:11px}
th{position:sticky;top:0;background:#1a1d26;color:#9aa3af;font-weight:400;text-align:left;padding:3px 4px;border-bottom:1px solid #2a2f3a}
td{padding:3px 4px;border-bottom:1px solid #20242e}
tr{cursor:pointer}
tr:hover td{background:#20242e}
tr.on td{background:#5a1a1a;color:#ffb3b3;font-weight:700}
.up{color:#ff6b6b}.down{color:#4ade80}.muted{color:#9aa3af}
.note{font-size:10px;color:#f59e0b;padding:2px 4px 5px;line-height:1.45}
#detail{font-size:11px;line-height:1.7;color:#d6dae2;min-height:60px}
#detail b{color:#fff}
.tabs{display:flex;gap:4px}
.tabs button{flex:1}
.pane{flex:1;min-height:0;display:flex;flex-direction:column;gap:6px}
#paneStats{overflow:auto}
#stats{overflow:auto}
.stat-h{font-size:11px;font-weight:700;color:#cfd6e4;margin:6px 0 2px;border-top:1px solid #2a2f3a;padding-top:4px}
.stat-line{font-size:10.5px;line-height:1.6;color:#c7cedb}
.stat-line b{color:#fff}
.stat-note{font-size:9.5px;color:#9aa3af;margin:1px 0 2px}
.warn{font-size:9.5px;line-height:1.55;color:#f0b45a;background:#2a1f12;border:1px solid #5a3a1a;border-radius:6px;padding:5px 7px;margin-top:6px}
#stats table{font-size:9.5px;margin:2px 0 4px}
#stats th,#stats td{padding:2px 3px;white-space:nowrap}
</style>
</head>
<body>
<div id="app">
  <div id="top">
    <div class="brand">复盘终端<span>REVIEW TERMINAL</span></div>
    <div id="watch" class="btns"></div>
    <div class="spacer"></div>
    <div id="periods" class="btns"></div>
    <div id="ranges" class="btns"></div>
    <div class="btns">
      <select id="ind" title="副图指标">
        <option value="vol">副图：成交量</option>
        <option value="atr">副图：ATR14</option>
        <option value="macd">副图：MACD</option>
        <option value="rsi">副图：RSI14</option>
        <option value="kdj">副图：KDJ</option>
      </select>
      <button id="tbtn">成交</button>
    </div>
  </div>
  <div id="replaybar">
    <span class="rl">回放</span>
    <input type="date" id="rdate">
    <button id="rstart">从该日回放</button>
    <span class="rsep"></span>
    <button id="rprev">◀ 上一根</button>
    <button id="rplay">播放 ▶</button>
    <button id="rnext">下一根 ▶</button>
    <select id="rspeed"><option value="1">1×</option><option value="4">4×</option><option value="16">16×</option></select>
    <button id="rexit">退出回放</button>
    <span id="rpos"></span>
  </div>
  <div id="grid">
    <div id="left">
      <div class="card"><div class="ct" id="ititle">分时</div><div id="intra"></div></div>
      <div class="card" id="qcard"></div>
    </div>
    <div id="center">
      <div id="qstrip"></div>
      <div id="main"></div>
      <div id="cap"></div>
    </div>
    <div id="right">
      <div class="tabs">
        <button id="tabT" class="on">成交</button>
        <button id="tabS">统计</button>
      </div>
      <div id="paneTrade" class="pane">
        <div class="card" id="trades"><div class="ct" id="ttitle"></div><div id="tbody"></div></div>
        <div class="card" id="detail"></div>
      </div>
      <div id="paneStats" class="pane" style="display:none">
        <div class="card" id="stats"></div>
      </div>
    </div>
  </div>
</div>
<script>
const DATA = __DATA__;
const UP = '#ef4444', DOWN = '#22c55e';
const PERIODS = [['5','5 分钟'],['15','15 分钟'],['1440','日线']];
const RANGES = [['1D','近1日',1],['3D','近3日',3],['ALL','全部',null]];
const state = {sym:'M', period:'5', range:'3D', sel:null, ind:'vol', showTrades:true,
               _lastViewKey:null, i:null, playing:false, shapes:[], _shapeBound:false, _progShapes:[]};

const $ = id => document.getElementById(id);
const toDate = s => new Date(s.length <= 10 ? s + 'T00:00:00' : s.replace(' ', 'T'));
const esc = s => String(s == null ? '' : s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const f1 = v => v == null ? '—' : Number(v).toFixed(1);
const f2 = v => v == null ? '—' : Number(v).toFixed(2);
const sgn = v => v == null ? '—' : (v > 0 ? '+' : '') + Number(v).toFixed(0);
const num0 = v => (v == null || !isFinite(v)) ? '—' : Number(v).toLocaleString('en-US', {maximumFractionDigits: 0});
const num2 = v => (v == null || !isFinite(v)) ? '—' : Number(v).toFixed(2);
const money = v => (v == null || !isFinite(v)) ? '—' : (v > 0 ? '+' : '') + Number(v).toLocaleString('en-US', {maximumFractionDigits: 0});
const pct = (n, d) => d ? Math.round(n / d * 100) + '%（' + n + '/' + d + '）' : '—';

function pd() { return DATA[state.sym].periods[state.period]; }
function isDaily() { return state.period === '1440'; }
function tmap(s) { return isDaily() ? String(s).slice(0,10) : s; }
function tradeStart(t) { return isDaily() ? t.day : t.t; }
function tradeEnd(t) { return isDaily() ? (t.xday || t.day) : (t.x || t.t); }
function evTime(e) { return isDaily() ? e.day : e.t; }

function rangeStart(xs) {
  // 日线只有 ≈20 根，区间分段没有意义：一律全显示
  if (isDaily() || state.range === 'ALL') return 0;
  const days = RANGES.find(r => r[0] === state.range)[2];
  const last = toDate(xs[xs.length-1]).getTime();
  const cut = last - days * 86400000;
  const i = xs.findIndex(s => toDate(s).getTime() >= cut);
  return i < 0 ? 0 : i;
}

function breaks() {
  const d = DATA[state.sym];
  const hols = isDaily() ? (d.holidays_d || []) : (d.holidays || []);
  const out = [{bounds:['sat','mon']}];
  if (hols.length) out.push({values: hols, pattern:'day'});
  if (isDaily()) return out;
  const meta = d.meta || {has_1015_break:true, night_end:23};
  out.push({bounds:[11.5,13.5], pattern:'hour'});
  if (meta.has_1015_break !== false) out.push({bounds:[10.25,10.5], pattern:'hour'});
  if (!meta.night_end) {
    out.push({bounds:[15,9], pattern:'hour'});          // 无夜盘：15:00-次日 09:00
  } else {
    out.push({bounds:[15,21], pattern:'hour'});
    out.push({bounds:[meta.night_end, 9], pattern:'hour'});
  }
  return out;
}

function renderTop() {
  const w = $('watch'); w.innerHTML = '';
  Object.keys(DATA).forEach(k => {
    const b = document.createElement('button');
    b.textContent = DATA[k].name + ' ' + (state.i != null ? '—' : DATA[k].last.toFixed(0));
    if (k === state.sym) b.className = 'on';
    b.onclick = () => { state.sym = k; state.sel = null; state.i = null; setPlay(false); renderAll(); };
    w.appendChild(b);
  });
  const p = $('periods'); p.innerHTML = '';
  PERIODS.forEach(([k,label]) => {
    const b = document.createElement('button'); b.textContent = label;
    if (k === state.period) b.className = 'on';
    b.onclick = () => { state.period = k; state.sel = null; state.i = null; setPlay(false); renderAll(); };
    p.appendChild(b);
  });
  const r = $('ranges'); r.innerHTML = '';
  // 日线只有 20 来根，分段没意义 → 按钮置灰，实际一律全显示
  const rangeLocked = isDaily();
  RANGES.forEach(([k,label]) => {
    const b = document.createElement('button'); b.textContent = label;
    if (k === (rangeLocked ? 'ALL' : state.range)) b.className = 'on';
    b.disabled = rangeLocked;
    if (rangeLocked) b.title = '日线只有 20 来根，不需要分段';
    b.onclick = () => { if (rangeLocked) return;
      state.range = k; state.sel = null; state.i = null; setPlay(false); renderAll(); };
    r.appendChild(b);
  });
  $('ind').value = state.ind;
}

function applyLayout() {
  const g = $('grid');
  g.classList.toggle('with-right', state.showTrades);
  $('tbtn').classList.toggle('on', state.showTrades);
  setTimeout(() => { try { Plotly.Plots.resize('main'); Plotly.Plots.resize('intra'); } catch (e) {} }, 60);
}

function renderQuote(q) {
  // q = {o,h,l,c,prev,t,sel}：未选中时是最新一根；选中某笔时是该笔平仓那根
  const chg = q.c - q.prev, pct = q.prev ? chg/q.prev*100 : 0;
  const col = chg >= 0 ? UP : DOWN;
  const d = DATA[state.sym];
  const tag = q.sel ? '选中笔 · ' : '';
  $('qstrip').innerHTML =
    `<span class="name">${esc(d.name)} ${state.sym}</span>` +
    `<span class="ohlc">${tag}${isDaily()?'日线':state.period+' 分钟'}</span>` +
    `<span class="px" style="color:${col}">${q.c.toFixed(1)}</span>` +
    `<span class="chg" style="color:${col}">${(chg>=0?'+':'')}${chg.toFixed(1)}  ${(pct>=0?'+':'')}${pct.toFixed(2)}%</span>` +
    `<span class="ohlc">开 ${q.o.toFixed(1)} · 高 ${q.h.toFixed(1)} · 低 ${q.l.toFixed(1)} · 昨收 ${q.prev.toFixed(1)}</span>` +
    `<span class="seg" style="color:${q.sel?'#ffb3b3':'inherit'}">${String(q.t).slice(5)}</span>`;
  const amp = q.prev ? (q.h-q.l)/q.prev*100 : null;
  $('qcard').innerHTML =
    `<div>今开 <b>${q.o.toFixed(1)}</b>　最高 <b>${q.h.toFixed(1)}</b></div>` +
    `<div>最低 <b>${q.l.toFixed(1)}</b>　收盘 <b>${q.c.toFixed(1)}</b></div>` +
    `<div>昨收 <b>${q.prev.toFixed(1)}</b>　振幅 <b>${amp==null?'—':amp.toFixed(1)+'%'}</b></div>`;
}

function renderMain() {
  const d = pd(), n = d.c.length;
  const i0 = rangeStart(d.x);
  const rangeN = n - i0;
  const lim = state.i != null
    ? Math.max(0, Math.min(rangeN - 1, state.i))
    : rangeN - 1;
  const iEnd = i0 + lim;                 // 绝对索引：最后一根可见 bar
  const xs = d.x.slice(i0, iEnd + 1), o = d.o.slice(i0, iEnd + 1),
        h = d.h.slice(i0, iEnd + 1), l = d.l.slice(i0, iEnd + 1),
        c = d.c.slice(i0, iEnd + 1), v = d.v.slice(i0, iEnd + 1),
        ma = d.ma.slice(i0, iEnd + 1), atr = d.atr.slice(i0, iEnd + 1);
  // 昨收：回放时也要用「该 bar 所属交易日」的昨收，不能拿上一根 bar 的收盘冒充
  let prevBar;
  if (state.i != null) {
    const dp = (!isDaily() && d.day) ? DATA[state.sym].day_prev[d.day[iEnd]] : null;
    prevBar = (dp != null) ? dp : (iEnd > 0 ? d.c[iEnd - 1] : d.c[iEnd]);
  } else if (isDaily()) {
    prevBar = n > 1 ? d.c[n-2] : d.c[n-1];
  } else {
    prevBar = DATA[state.sym].day_prev[DATA[state.sym].last_day] != null
      ? DATA[state.sym].day_prev[DATA[state.sym].last_day]
      : (n > 1 ? d.c[n-2] : d.c[n-1]);
  }

  // ★ P0-6：选中某笔时，报价条改成「该笔平仓那根」；取消选中恢复最新
  let q = {o:o[o.length-1], h:h[h.length-1], l:l[l.length-1], c:c[c.length-1],
           prev:prevBar, t:xs[xs.length-1], sel:false};
  if (state.sel != null) {
    const st = DATA[state.sym].trades[state.sel];
    const et = tradeEnd(st) || st.t;
    let idx = d.x.length - 1;
    for (let k = d.x.length - 1; k >= 0; k--) {
      if (tmap(d.x[k]) <= tmap(et)) { idx = k; break; }
    }
    if (idx >= 0) {
      const qp = isDaily()
        ? (idx > 0 ? d.c[idx-1] : d.c[idx])
        : ((DATA[state.sym].day_prev[st.xday || st.day] != null)
            ? DATA[state.sym].day_prev[st.xday || st.day]
            : (idx > 0 ? d.c[idx-1] : d.c[idx]));
      q = {o:d.o[idx], h:d.h[idx], l:d.l[idx], c:d.c[idx],
           prev:qp, t:d.x[idx], sel:true};
    }
  }
  renderQuote(q);

  const cd = c.map((cc,i) => [v[i], ma[i], atr[i],
    i>0 ? +(cc-c[i-1]).toFixed(1) : 0,
    i>0 && c[i-1] ? +((cc-c[i-1])/c[i-1]*100).toFixed(2) : 0]);
  const fmt = isDaily() ? '%Y-%m-%d' : '%m-%d %H:%M';
  const traces = [{
    type:'candlestick', x:xs, open:o, high:h, low:l, close:c, name:'K线',
    increasing:{line:{color:UP,width:1}, fillcolor:UP},
    decreasing:{line:{color:DOWN,width:1}, fillcolor:DOWN},
    customdata:cd,
    hovertemplate:'<b>%{x|'+fmt+'}</b><br>开 %{open:.1f}　高 %{high:.1f}<br>低 %{low:.1f}　收 %{close:.1f}<br>'+
      '涨跌 %{customdata[3]:+.1f}（%{customdata[4]:+.2f}%）<br>量 %{customdata[0]:,.0f}<br>'+
      'MA13 %{customdata[1]:.1f}　ATR14 %{customdata[2]:.2f}<extra></extra>',
  },{
    type:'scatter', x:xs, y:ma, mode:'lines', name:'MA13',
    line:{color:'#f2a900', width:1.6}, hoverinfo:'skip',
  }];
  const ind = state.ind;
  if (ind === 'vol') {
    traces.push({type:'bar', x:xs, y:v, yaxis:'y2', opacity:.55, hoverinfo:'skip',
      marker:{color:c.map((cc,i)=>cc>=o[i]?UP:DOWN)}});
  } else if (ind === 'atr') {
    traces.push({type:'scatter', x:xs, y:atr, yaxis:'y2', mode:'lines',
      line:{color:'#f2a900', width:1.2}, hoverinfo:'skip'});
  } else if (ind === 'macd') {
    const hh = d.hist.slice(i0, iEnd + 1);
    traces.push({type:'bar', x:xs, y:hh, yaxis:'y2', opacity:.6, hoverinfo:'skip',
      marker:{color:hh.map(v=>v>=0?UP:DOWN)}});
    traces.push({type:'scatter', x:xs, y:d.dif.slice(i0, iEnd + 1), yaxis:'y2', mode:'lines',
      line:{color:'#f2a900', width:1.1}, hoverinfo:'skip'});
    traces.push({type:'scatter', x:xs, y:d.dea.slice(i0, iEnd + 1), yaxis:'y2', mode:'lines',
      line:{color:'#60a5fa', width:1.1}, hoverinfo:'skip'});
  } else if (ind === 'rsi') {
    traces.push({type:'scatter', x:xs, y:d.rsi.slice(i0, iEnd + 1), yaxis:'y2', mode:'lines',
      line:{color:'#a78bfa', width:1.3}, hoverinfo:'skip'});
  } else if (ind === 'kdj') {
    traces.push({type:'scatter', x:xs, y:d.k.slice(i0, iEnd + 1), yaxis:'y2', mode:'lines',
      line:{color:'#f2a900', width:1.1}, hoverinfo:'skip'});
    traces.push({type:'scatter', x:xs, y:d.d.slice(i0, iEnd + 1), yaxis:'y2', mode:'lines',
      line:{color:'#60a5fa', width:1.1}, hoverinfo:'skip'});
    traces.push({type:'scatter', x:xs, y:d.j.slice(i0, iEnd + 1), yaxis:'y2', mode:'lines',
      line:{color:'#c084fc', width:1.1}, hoverinfo:'skip'});
  }

  const lo = tmap(xs[0]), hi = tmap(xs[xs.length-1]);
  const evs = DATA[state.sym].events.filter(e => { const q = tmap(evTime(e)); return q >= lo && q <= hi; });
  const specs = [['开','多',UP,'triangle-up',16,'多'],['开','空',DOWN,'triangle-down',16,'空'],
                 ['平','多','#ff9aa2','x',12,''],['平','空','#86efac','x',12,'']];
  specs.forEach(([typ,dir,col,sym,size,txt]) => {
    const sub = evs.filter(e => e.y === typ && e.d === dir);
    if (!sub.length) return;
    const ex = sub.map(e => evTime(e)), ey = sub.map(e => e.p);
    traces.push({type:'scatter', x:ex, y:ey, mode:'markers',
      marker:{symbol:sym, size:size+10, color:col, opacity:.22, line:{width:0}},
      hoverinfo:'skip', showlegend:false});
    traces.push({type:'scatter', x:ex, y:ey, mode:txt?'markers+text':'markers',
      marker:{symbol:sym, size:size, color:col, line:{width:1.6, color:'#fff'}},
      text:txt?sub.map(()=>txt):undefined, textposition:'top center',
      textfont:{size:10, color:col}, showlegend:false,
      customdata:sub.map(e=>[e.q, e.p]),
      hovertemplate:typ+dir+' %{customdata[0]}手 @%{customdata[1]}<extra></extra>'});
  });

  const shapes = [];
  if (ind === 'rsi') {
    shapes.push({type:'line', x0:xs[0], x1:xs[xs.length-1], y0:30, y1:30, yref:'y2',
      line:{color:'#555', width:1, dash:'dot'}});
    shapes.push({type:'line', x0:xs[0], x1:xs[xs.length-1], y0:70, y1:70, yref:'y2',
      line:{color:'#555', width:1, dash:'dot'}});
  } else if (ind === 'macd') {
    shapes.push({type:'line', x0:xs[0], x1:xs[xs.length-1], y0:0, y1:0, yref:'y2',
      line:{color:'#555', width:1}});
  }
  // 不再画止损线：它会把价格 Y 轴撑宽，导致 K 线显矮

  // 默认视图 = 区间全显示（数据已裁到模拟盘窗口，段内根数不大；要细看就滚轮放大）
  let xr = [xs[0], xs[xs.length-1]];
  if (state.sel != null) {
    const t = DATA[state.sym].trades[state.sel];
    const isLong = t.d === '多', col = isLong ? UP : DOWN;
    const x0 = tradeStart(t), x1 = tradeEnd(t);
    if (x0 != null) {
      const vx0 = x0 < xs[0] ? xs[0] : x0;
      const vx1 = (!x1 || x1 > xs[xs.length-1]) ? xs[xs.length-1] : x1;
      if (vx1 > vx0) shapes.push({type:'rect', x0:vx0, x1:vx1, y0:0, y1:1, yref:'paper',
        fillcolor:isLong?'rgba(239,68,68,0.18)':'rgba(34,197,94,0.18)', line:{width:0}, layer:'below'});
      // ★ P3：只画选中这一笔的止损线（一条，用 shapes，不影响 autorange）
      const sVal = t.ps != null ? t.ps : t.s;
      if (sVal != null && tmap(vx1) > tmap(vx0)) {
        shapes.push({type:'line', x0:vx0, x1:vx1, y0:sVal, y1:sVal,
          line:{color:col, width:1.6, dash:'dot'}});
      }
      if (t.e != null && x0 >= xs[0] && x0 <= xs[xs.length-1]) {
        traces.push({type:'scatter', x:[x0], y:[t.e], mode:'markers',
          marker:{symbol:isLong?'triangle-up':'triangle-down', size:26, color:col,
                  line:{width:3, color:'#ffe082'}},
          hovertemplate:'选中 · '+t.d+' '+t.q+'手<extra></extra>', showlegend:false});
      }
      const k0 = xs.findIndex(s => tmap(s) >= tmap(x0));
      const k1 = x1 ? xs.findIndex(s => tmap(s) >= tmap(x1)) : xs.length - 1;
      if (k0 >= 0) {   // -1 = 该笔在当前区间之外，保持原视图，别算出塌缩的范围
        const j0 = Math.max(0, k0 - 40);
        const j1 = Math.min(xs.length - 1, (k1 >= 0 ? k1 : xs.length - 1) + 20);
        if (j1 > j0) xr = [xs[j0], xs[j1]];
      }
    }
  }

  // ★ P0-3：同一 sym/period/range/sel 下复用用户当前 x 视角；切指标不跳回 300 根
  //   （只靠 uirevision 不够：每次都会显式写 xaxis.range，会把用户视角覆盖掉）
  const viewKey = `${state.sym}|${state.period}|${state.range}|${state.sel}` +
    (state.i != null ? `|r${state.i}` : '');
  const mEl = $('main');
  if (viewKey === state._lastViewKey && mEl && mEl.layout &&
      mEl.layout.xaxis && mEl.layout.xaxis.range) {
    xr = mEl.layout.xaxis.range.slice();
  } else {
    state._lastViewKey = viewKey;
  }

  // ★ P3：用户画的线存 state.shapes，重绘时合并；回放时裁到当前 bar
  state._progShapes = shapes.slice();
  (state.shapes || []).forEach(s => {
    let s2 = s;
    if (state.i != null) {
      const hiBar = xs[xs.length-1];
      if (s2.x0 && tmap(s2.x0) > tmap(hiBar)) return;
      if (s2.x1 && tmap(s2.x1) > tmap(hiBar)) {
        s2 = Object.assign({}, s2, {x1: hiBar});
      }
    }
    shapes.push(s2);
  });

  // ★ 价格 Y 轴只按「当前可见窗口」取高低，K 线才会填满价格区
  const ai = Math.max(0, xs.findIndex(s => s >= xr[0]));
  const bi = Math.max(ai, Math.min(xs.length-1, xs.findIndex(s => s >= xr[1])));
  let plo = Infinity, phi = -Infinity;
  for (let i = ai; i <= bi; i++) {
    if (l[i] != null && l[i] < plo) plo = l[i];
    if (h[i] != null && h[i] > phi) phi = h[i];
  }
  if (!isFinite(plo) || !isFinite(phi)) { plo = 0; phi = 1; }
  const ppad = Math.max((phi - plo) * 0.06, 0.5);

  const layout = {
    margin:{l:8, r:8, t:8, b:8},
    paper_bgcolor:'#0f1117', plot_bgcolor:'#0f1117',
    font:{family:'Microsoft YaHei', color:'#e6e6e6', size:11},
    hovermode:'closest', dragmode:'pan',
    hoverlabel:{bgcolor:'#151d2e', bordercolor:'#3a4350', font:{color:'#e6e6e6', size:11}, align:'left'},
    showlegend:false, shapes:shapes,
    uirevision: state.sel != null
      ? `${state.sym}|${state.period}|${state.sel}`
      : `${state.sym}|${state.period}`,
    xaxis:{type:'date', range:xr, rangeslider:{visible:false},
      gridcolor:'#232733', tickfont:{color:'#9aa3af'}, linecolor:'#2a2f3a',
      rangebreaks:breaks()},
    yaxis:{domain:[0.30,1], range:[plo-ppad, phi+ppad],
      gridcolor:'#232733', tickfont:{color:'#9aa3af'},
      linecolor:'#2a2f3a', side:'right'},
    yaxis2:{domain:[0,0.22], gridcolor:'rgba(255,255,255,0.04)',
      tickfont:{color:'#9aa3af'}, showticklabels: state.ind !== 'vol',
      linecolor:'#2a2f3a', side:'right'},
  };
  Plotly.react('main', traces, layout, {
    displayModeBar:true, scrollZoom:true, responsive:true, displaylogo:false,
    modeBarButtonsToRemove:['select2d','lasso2d','toggleSpikelines'],
    modeBarButtonsToAdd:['drawline','drawrect','eraseshape'],
  });

  // ★ P3：捕获用户画的线（绘图工具会更新 layout.shapes）
  if (!state._shapeBound) {
    $('main').on('plotly_relayout', e => {
      if (e && e.shapes) {
        const nProg = (state._progShapes || []).length;
        state.shapes = e.shapes.slice(nProg);
      }
    });
    state._shapeBound = true;
  }

  const seg = isDaily() ? '日线' : (() => {
    const hh = parseInt(xs[xs.length-1].slice(11,13) || 0, 10);
    return hh >= 21 ? '夜盘' : (hh < 12 ? '早盘' : '午盘');
  })();
  $('cap').textContent = `${xs[xs.length-1]} ｜ 最新 开 ${o[o.length-1].toFixed(1)}  高 ${h[h.length-1].toFixed(1)}  低 ${l[l.length-1].toFixed(1)}  收 ${c[c.length-1].toFixed(1)} ｜ MA13 ${f2(ma[ma.length-1])} ｜ ATR14 ${f2(atr[atr.length-1])} ｜ ${seg} ｜ 加载 ${xs.length} 根 ｜ 拖动/滚轮缩放 · 悬停看该根详情`;
}

function renderIntraday() {
  const d = DATA[state.sym];
  const pdat = pd();
  const i0 = rangeStart(pdat.x);
  const hiIdx = state.i != null
    ? Math.min(pdat.x.length - 1, i0 + state.i)
    : pdat.x.length - 1;
  const hiBar = pdat.x[hiIdx];
  let day = state.i != null
    ? (pdat.day ? pdat.day[hiIdx] : d.last_day)
    : d.last_day;
  if (state.i == null && state.sel != null) {
    const t = d.trades[state.sel];
    day = t.day || day;
  }
  let idx = [];
  d.m1.day.forEach((v,i) => { if (v === day) idx.push(i); });
  if (!idx.length && state.i == null) {
    day = d.last_day;
    d.m1.day.forEach((v,i) => { if (v === day) idx.push(i); });
  }
  // ★ P2：回放时 1min 分时也截断到当前 bar 收盘，不能看当天未来
  if (state.i != null && idx.length) {
    const pmin = isDaily() ? 1440 : (+state.period || 5);
    const cut = new Date(toDate(hiBar).getTime() + pmin * 60000);
    idx = idx.filter(i => toDate(d.m1.x[i]) <= cut);
  }
  $('ititle').textContent = '分时 ' + day.slice(5);
  if (!idx.length) { Plotly.purge('intra'); return; }
  const x = idx.map(i => d.m1.x[i]), o = idx.map(i => d.m1.o[i]),
        c = idx.map(i => d.m1.c[i]), v = idx.map(i => d.m1.v[i]);
  let pv = 0, vv = 0;
  const avg = c.map((cc,i) => { pv += cc*v[i]; vv += v[i]; return vv ? pv/vv : cc; });
  const prev = d.day_prev[day] != null ? d.day_prev[day] : o[0];
  const col = c[c.length-1] >= prev ? UP : DOWN;
  const traces = [
    {type:'scatter', x:x, y:c, mode:'lines', name:'价格', line:{color:col, width:1.5},
     hovertemplate:'%{x|%H:%M}  %{y:.1f}<extra></extra>'},
    {type:'scatter', x:x, y:avg, mode:'lines', name:'均价', line:{color:'#f2a900', width:1}, hoverinfo:'skip'},
    {type:'bar', x:x, y:v, yaxis:'y2', showlegend:false, opacity:.6, hoverinfo:'skip',
     marker:{color:c.map((cc,i)=>cc>=o[i]?UP:DOWN)}},
  ];
  const layout = {
    height:200, margin:{l:2, r:2, t:20, b:2},
    paper_bgcolor:'#0f1117', plot_bgcolor:'#0f1117',
    font:{family:'Microsoft YaHei', color:'#e6e6e6', size:9},
    title:{text:'分时 '+day.slice(5), font:{size:11, color:'#9aa3af'}, x:0.01, y:0.99},
    showlegend:false, hovermode:'x unified',
    xaxis:{type:'date', tickformat:'%H:%M', nticks:4, showgrid:false,
      tickfont:{color:'#9aa3af'}, linecolor:'#2a2f3a', rangebreaks:breaks()},
    yaxis:{domain:[0.24,1], side:'right', nticks:4, gridcolor:'rgba(255,255,255,0.06)',
      tickfont:{color:'#9aa3af'}, linecolor:'#2a2f3a'},
    yaxis2:{domain:[0,0.2], side:'right', nticks:2, gridcolor:'rgba(0,0,0,0)',
      showticklabels:false, tickfont:{color:'#9aa3af'}, linecolor:'#2a2f3a'},
    shapes:[{type:'line', x0:x[0], x1:x[x.length-1], y0:prev, y1:prev,
      line:{color:'#888', width:1, dash:'dot'}}],
  };
  Plotly.react('intra', traces, layout, {displayModeBar:false, responsive:true, displaylogo:false});
}

function renderTable() {
  const d = DATA[state.sym];
  let rows = d.trades;
  if (state.i != null) {
    const p = pd(); const i0 = rangeStart(p.x);
    const hi = p.x[Math.min(p.x.length - 1, i0 + state.i)];
    rows = rows.filter(t => t.t && t.x && tmap(t.x) <= tmap(hi));
  }
  const openN = d.trades.filter(t => !t.x).length;
  $('ttitle').textContent = state.i != null
    ? `${d.name} ｜ 回放中 · 已发生 ${rows.length} 笔`
    : `${d.name} ｜ ${rows.length} 笔` + (openN ? ` ｜ 未平 ${openN}` : '');
  let html = (d.note ? `<div class="note">${esc(d.note)}</div>` : '') +
    '<table><thead><tr><th>方向</th><th>手数</th><th>开仓</th><th>盈亏</th></tr></thead><tbody>';
  rows.forEach(t => {
    const cls = t.d === '多' ? 'up' : 'down';
    const pc = t.pnl == null ? '' : (t.pnl > 0 ? 'up' : (t.pnl < 0 ? 'down' : ''));
    html += `<tr data-i="${t.i}" class="${state.sel===t.i?'on':''}">` +
      `<td class="${cls}">${t.d}</td><td>${t.q}</td>` +
      `<td>${t.t ? t.t.slice(5,16) : '—'}</td>` +
      `<td class="${pc}">${t.e == null ? '<span class="muted">接管</span>' : (t.pnl==null?'—':sgn(t.pnl))}</td></tr>`;
  });
  html += '</tbody></table>';
  $('tbody').innerHTML = html;
  document.querySelectorAll('#tbody tbody tr').forEach(tr => {
    tr.onclick = () => {
      // ★ 点成交 = “我要看这一笔”：若当前是“近1日/近3日”，该笔可能在区间外，
      //   强行按区间定位会算出 -1 索引 → 视图塌成一团且什么都看不到。切 ALL 再定位。
      state.sel = +tr.dataset.i;
      state.range = 'ALL';
      state._lastViewKey = null;
      renderAll();
    };
  });
}

function renderDetail() {
  if (state.sel == null) {
    $('detail').innerHTML = '<div class="muted">点「成交」里一行 → 图缩放到那笔并亮红</div>';
    return;
  }
  const t = DATA[state.sym].trades[state.sel];
  const d = pd();
  let slope = null, dist = null;
  if (t.t) {
    let bi = 0;
    for (let k = d.x.length - 1; k >= 0; k--) {
      if (tmap(d.x[k]) <= tmap(t.t)) { bi = k; break; }
    }
    slope = d.slope ? d.slope[bi] : null;
    dist = d.atr_dist ? d.atr_dist[bi] : null;
  }
  const openLine = t.e == null
    ? `<div>开 ${t.t ? t.t.slice(5,16) : '—'} @ <b>接管笔 · 开仓价未知</b></div>`
    : `<div>开 ${t.t ? t.t.slice(5,16) : '—'} @ ${f1(t.e)}</div>`;
  $('detail').innerHTML =
    `<div><b>${t.d} ${t.q} 手</b>　<span class="muted">${esc(t.k)}</span></div>` +
    openLine +
    `<div>平 ${t.x ? t.x.slice(5,16) : '—'} @ ${f1(t.p)}</div>` +
    `<div>止损 ${f2(t.s)}　计划 ${f2(t.ps)}</div>` +
    `<div>开仓时 MA13 斜率 ${num2(slope)} ｜ 距MA ${num2(dist)} ATR</div>` +
    `<div>MFE <span class="up">${sgn(t.mfe)}</span>　MAE <span class="down">${sgn(t.mae)}</span>　盈亏 <b>${sgn(t.pnl)}</b></div>` +
    `<div class="muted">${esc(t.r)}　持仓 ${t.h==null?'—':t.h+' 分'}</div>` +
    `<button id="clr" style="margin-top:4px;width:100%">取消选中</button>`;
  const b = $('clr'); if (b) b.onclick = () => { state.sel = null; renderAll(); };
}

function renderStats() {
  if (state.i != null) {
    $('stats').innerHTML = '<div class="warn">回放中：全样本统计包含未来数据，已暂时隐藏。退出回放后可查看。</div>';
    return;
  }
  const s = DATA[state.sym].stats_all;
  if (!s) { $('stats').innerHTML = '<div class="muted">统计未生成</div>'; return; }
  const byR = (s.by_reason || []).map(r =>
    `<tr><td>${esc(r.r)}</td><td>${r.n}</td>` +
    `<td class="${r.sum >= 0 ? 'up' : 'down'}">${money(r.sum)}</td>` +
    `<td>${num0(r.mean)}</td><td>${pct(r.win, r.n)}</td></tr>`).join('');
  const byS = (s.by_symbol || []).map(r =>
    `<tr class="${r.key === state.sym ? 'on' : ''}"><td>${esc(r.name)} ${r.key}</td>` +
    `<td>${r.n}</td><td>${r.pnl_n}</td>` +
    `<td class="${r.sum >= 0 ? 'up' : 'down'}">${money(r.sum)}</td></tr>`).join('');
  const sl = s.slippage || {};
  const ex = s.excursion || {};
  const cov = ex.coverage == null ? '—' : Math.round(ex.coverage * 100) + '%';
  const worse = sl.worse_share == null ? '—' : Math.round(sl.worse_share * 100) + '%';
  $('stats').innerHTML = `
    <div class="stat-h">全样本（6 品种）</div>
    <div class="stat-line">总交易 <b>${s.n_trades}</b> ｜ 已结算 <b>${s.n_settled}</b> ｜ 接管 <b>${s.n_takeover}</b>（未计） ｜ 未平 <b>${s.n_open}</b></div>
    <div class="stat-line">合计 <b class="${s.pnl_sum >= 0 ? 'up' : 'down'}">${money(s.pnl_sum)}</b> 元 ｜ 均值 ${num0(s.pnl_mean)} ｜ 胜率 <b>${pct(s.win, s.n_settled)}</b></div>
    <div class="stat-line">盈 ${s.win} / 亏 ${s.loss} / 平 ${s.flat} ｜ 均盈 ${money(s.avg_win)} ｜ 均亏 ${money(s.avg_loss)} ｜ 盈亏比 ${num2(s.payoff)}</div>
    <div class="stat-line">持仓中位 ${num0(s.hold_median)} 分 ｜ 均值 ${num0(s.hold_mean)} 分</div>
    <div class="stat-line muted">A2 覆盖 ${ex.n}/${ex.n_closes}（${cov}）｜ MFE 中位 ${money(ex.mfe_median)} ｜ MAE 中位 ${money(ex.mae_median)}</div>
    <div class="stat-h">出场原因（${s.n_settled} 笔已结算，按合计升序）</div>
    <table><thead><tr><th>原因</th><th>n</th><th>合计</th><th>均值</th><th>胜率</th></tr></thead><tbody>${byR}</tbody></table>
    <div class="stat-h">超损（${sl.n_ok} 笔价格止损）</div>
    <div class="stat-line">差于止损价 <b>${sl.worse_n}/${sl.n_ok} = ${worse}</b></div>
    <div class="stat-line">中位 <b>${num2(sl.median)}</b> ｜ 均值 ${num2(sl.mean)} ｜ P10 ${num2(sl.p10)} ｜ P25 ${num2(sl.p25)} ｜ P75 ${num2(sl.p75)}</div>
    <div class="stat-line">最差 ${num2(sl.min)} ｜ 金额合计 <b class="down">${money(sl.sum_amount)}</b> 元</div>
    <div class="stat-note">止损价 vs 实际成交价；负 = 差于止损价（超损）</div>
    <div class="stat-h">分品种</div>
    <table><thead><tr><th>品种</th><th>笔数</th><th>有盈亏</th><th>合计</th></tr></thead><tbody>${byS}</tbody></table>
    <div class="warn">时间止损赚、价格止损亏 —— <b>这不等于"放宽止损就能赚"</b>。<br>
    被止损的那 55 笔本来就是走势不利的那一批（事后分组，不是随机分组）。<br>
    06 策略毛利仅为成本的 4.5%，改出场在数学上不可能改变期望符号<br>
    （"提前出场家族"已 5 次失败，同一机制）。</div>`;
}

function renderAll() {
  if (state.sel != null && !DATA[state.sym].trades[state.sel]) state.sel = null;
  if (state.i == null) {
    const d0 = pd(); const j0 = rangeStart(d0.x);
    if (d0.x.length) {
      const el = $('rdate');
      // 可选日期 = 整段模拟盘（不受“近N日”限制），默认值 = 当前区间起点
      el.min = String(d0.x[0]).slice(0,10);
      el.max = String(d0.x[d0.x.length-1]).slice(0,10);
      if (!el.value || el.value < el.min || el.value > el.max) {
        el.value = String(d0.x[j0]).slice(0,10);
      }
    }
  }
  renderTop();
  renderMain();
  renderIntraday();
  renderTable();
  renderDetail();
  renderStats();
  updateReplayUI();
}

// ---------------- P2：回放（纯前端，未来数据物理截断） ----------------
let replayTimer = null;

function replayNI0() {
  const d = pd(); const i0 = rangeStart(d.x);
  return [d, i0, d.x.length - i0];
}

function updateReplayUI() {
  const [d, i0, N] = replayNI0();
  const on = state.i != null;
  $('rplay').textContent = state.playing ? '暂停 ⏸' : '播放 ▶';
  $('rprev').disabled = !on || state.i <= 0;
  $('rnext').disabled = !on || state.i >= N - 1;
  $('rplay').disabled = !on;
  $('rexit').disabled = !on;
  $('rspeed').disabled = !on;
  $('rpos').textContent = on ? `${state.i + 1} / ${N}` : '';
}

function startReplay() {
  // 回放对象 = 模拟盘整段：先切 ALL，否则在“近3日”下起播会把前几天整段丢掉
  state.range = 'ALL';
  state._lastViewKey = null;
  const [d, i0, N] = replayNI0();
  if (!N) return;
  const ds = $('rdate').value || String(d.x[i0]).slice(0,10);
  let j = i0;
  for (let k = d.x.length - 1; k >= i0; k--) {
    if (String(d.x[k]).slice(0,10) <= ds) { j = k; break; }
  }
  state.i = Math.max(0, j - i0);
  state.sel = null;
  setPlay(false);
  renderAll();
}

function exitReplay() {
  setPlay(false);
  state.i = null;
  state.sel = null;
  renderAll();
}

function stepReplay(n) {
  if (state.i == null) return;
  setPlay(false);
  const [d, i0, N] = replayNI0();
  state.i = Math.max(0, Math.min(N - 1, state.i + n));
  renderAll();
}

function setPlay(on) {
  state.playing = !!on;
  if (replayTimer) { clearInterval(replayTimer); replayTimer = null; }
  if (state.playing && state.i != null) {
    const speed = +($('rspeed').value || 1);
    replayTimer = setInterval(() => {
      const [d, i0, N] = replayNI0();
      if (state.i >= N - 1) { setPlay(false); return; }
      state.i = Math.min(N - 1, state.i + speed);
      renderMain();
      renderIntraday();
      updateReplayUI();
      if (state.i >= N - 1) setPlay(false);
    }, 80);
  }
  updateReplayUI();
  if (!state.playing && state.i != null) {
    try { renderTable(); } catch (e) {}
  }
}

window.addEventListener('resize', () => {
  try { Plotly.Plots.resize('main'); Plotly.Plots.resize('intra'); } catch (e) {}
});
function setTab(t) {
  $('tabT').classList.toggle('on', t === 'trade');
  $('tabS').classList.toggle('on', t === 'stats');
  $('paneTrade').style.display = t === 'trade' ? 'flex' : 'none';
  $('paneStats').style.display = t === 'stats' ? 'flex' : 'none';
}
$('tabT').onclick = () => setTab('trade');
$('tabS').onclick = () => setTab('stats');
setTab('trade');
$('rstart').onclick = startReplay;
$('rprev').onclick = () => stepReplay(-1);
$('rnext').onclick = () => stepReplay(1);
$('rplay').onclick = () => setPlay(!state.playing);
$('rexit').onclick = exitReplay;
$('rspeed').onchange = () => { if (state.playing) { setPlay(false); setPlay(true); } };
document.addEventListener('keydown', e => {
  const tag = (document.activeElement || {}).tagName || '';
  if (['INPUT', 'SELECT', 'TEXTAREA'].includes(tag)) return;
  // 焦点在按钮上时空格会先触发按钮点击，再被这里处理 → 双重触发（播放/暂停互相抵消）
  if (e.key === ' ' && tag === 'BUTTON') return;
  if (e.key === 'ArrowLeft') { e.preventDefault(); if (state.i != null) stepReplay(-1); }
  else if (e.key === 'ArrowRight') { e.preventDefault(); if (state.i != null) stepReplay(1); }
  else if (e.key === ' ') { e.preventDefault(); if (state.i != null) setPlay(!state.playing); }
  else if (e.key === 'Escape') { if (state.i != null) exitReplay(); }
});
document.getElementById('ind').onchange = e => { state.ind = e.target.value; renderMain(); };
document.getElementById('tbtn').onclick = () => { state.showTrades = !state.showTrades; applyLayout(); };
renderAll();
applyLayout();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
