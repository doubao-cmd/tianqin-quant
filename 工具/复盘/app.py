# -*- coding: utf-8 -*-
"""
复盘终端 · 单屏版（裸K + 拖动复盘 + 悬停详情）
================================================================
用户 2026-10-18 02:05 定稿：
  · 删掉「窗口（根）」：整段加载，用鼠标拖/滚轮缩放看哪里都行；
    最多加载多少根由「区间」控制（近3日/1周/1月/全部）。
  · 删掉「回放位置」滑杆、⏮◀▶ 前进后退、播放、速度、价位/加水平线。
  · 鼠标悬停在 K 线上 → 弹出该根的详细价格（开高低收、涨跌、量、MA13、ATR14）。
  · 全深色；买卖点放大加光晕。
  · 点右侧成交行 → 图自动缩放到该笔并亮红。

运行：
  .venv_viz\\Scripts\\python.exe -m streamlit run 工具\\复盘\\app.py
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import charts
import data
import features
import sessions
import trades as T

st.set_page_config(page_title="复盘终端", layout="wide", initial_sidebar_state="collapsed")

st.markdown(
    """
    <style>
      [data-testid="stHeader"], [data-testid="stToolbar"], footer {display: none !important;}
      .stApp {background: #0f1117 !important;}
      .block-container {padding-top: .5rem !important; padding-bottom: .2rem !important; max-width: 100% !important;}
      [data-testid="stVerticalBlock"] {gap: .24rem;}
      [data-testid="stHorizontalBlock"] {gap: .32rem;}
      [data-testid="stMetricValue"] {font-size: 1.1rem; color: #e6e6e6 !important;}
      [data-testid="stMetricLabel"] {font-size: .7rem; color: #9aa3af !important;}
      div[data-testid="stTabs"] button {padding: .18rem .55rem; font-size: .82rem;}
      div[data-testid="stButton"] button {padding: .12rem .3rem; min-height: 1.6rem;}
      [data-testid="stWidgetLabel"] {font-size: 10px !important; margin-bottom: -2px !important; color: #9aa3af !important;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """<div style="display:flex;align-items:center;gap:8px;padding:3px 10px;margin-bottom:3px;
                    background:linear-gradient(90deg,#111827,#1f2b45);border-radius:6px;color:#e8edf5;">
      <span style="font-size:12px;font-weight:700;letter-spacing:.6px;">复盘终端</span>
      <span style="font-size:10px;opacity:.5;">REVIEW TERMINAL</span>
      <span style="margin-left:auto;font-size:10px;opacity:.5;">裸K · MA13 · 拖动复盘</span>
    </div>""",
    unsafe_allow_html=True,
)

SS = st.session_state
CHART_H = 640
PERIOD_LABEL = {5: "5 分钟", 15: "15 分钟", 1440: "日线"}
RANGE_OPTS = {"近3日": 3, "近1周": 7, "近1月": 30, "全部": None}

keys = list(data.RECENT_SPECS)
SS.setdefault("sym", keys[0])
SS.setdefault("sel_trade", None)
SS.setdefault("draw_mode", "无")
SS.setdefault("_draw_prev", "无")


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner="加载K线…")
def _load(key: str, period: int):
    df, meta = data.load_recent(key, period)
    if period == 1440:
        df = df.assign(x=df["datetime"].dt.normalize() + pd.Timedelta(days=1))
    else:
        df = df.assign(x=data.to_beijing(df["datetime"]))
    return features.build_custom(df), meta


@st.cache_data(show_spinner="读取台账…")
def _load_ledger(key: str):
    cn = data.RECENT_SPECS[key][0]
    return T.load_events(cn), T.load_trades(cn), cn


@st.cache_data(show_spinner=False)
def _load_1m(key: str):
    try:
        df, _ = data.load_recent(key, 1)
    except FileNotFoundError:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume", "x"])
    return df.assign(x=data.to_beijing(df["datetime"]))


@st.cache_data(show_spinner="加载分时…")
def _load_1m_labeled(key: str):
    df1 = _load_1m(key)
    if df1.empty:
        return df1
    return sessions.label_sessions(df1)


@st.cache_data(show_spinner=False)
def _day_closes(key: str):
    lab = _load_1m_labeled(key)
    if lab.empty:
        return pd.Series(dtype=float)
    return lab.groupby("交易日")["close"].last()


@st.cache_data(show_spinner="读取自选…")
def _last_prices() -> dict:
    out = {}
    for k in keys:
        try:
            df, _ = data.load_recent(k, 5)
            out[k] = float(df["close"].iloc[-1])
        except Exception:
            out[k] = float("nan")
    return out


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _fmt_t(v) -> str:
    return pd.Timestamp(v).strftime("%m-%d %H:%M") if pd.notna(v) else "—"


def _num(v, nd: int = 2) -> str:
    return f"{float(v):,.{nd}f}" if pd.notna(v) else "—"


def _sgn(v) -> str:
    return f"{float(v):+,.0f}" if pd.notna(v) else "—"


def _to_trading_day(v):
    return pd.Timestamp(sessions.trading_day_of_bj(v)) if pd.notna(v) else v


def _events_view(ev: pd.DataFrame, x0, x1, period: int) -> pd.DataFrame:
    if ev is None or ev.empty:
        return pd.DataFrame()
    e = ev.copy()
    if period == 1440:
        e["时间"] = e["时间"].map(_to_trading_day)
    return e[(e["时间"] >= x0) & (e["时间"] <= x1)]


def _trades_view(tr: pd.DataFrame, x0, x1, period: int) -> pd.DataFrame:
    if tr is None or tr.empty:
        return pd.DataFrame()
    t = tr.copy()
    if period == 1440:
        t["开仓时间"] = t["开仓时间"].map(_to_trading_day)
        t["平仓时间"] = t["平仓时间"].map(_to_trading_day)
    m = (t["开仓时间"] <= x1) & (t["平仓时间"].isna() | (t["平仓时间"] >= x0))
    return t[m]


def _selected(tr: pd.DataFrame, idx):
    if tr is None or tr.empty or idx is None:
        return None
    try:
        i = int(idx)
    except Exception:
        return None
    return tr.iloc[i].to_dict() if 0 <= i < len(tr) else None


def _points(state) -> list:
    try:
        return list(state.selection.points or [])
    except Exception:
        pass
    try:
        return list((state or {}).get("selection", {}).get("points", []) or [])
    except Exception:
        return []


def _sel_rows(state) -> list:
    try:
        return list(state.selection.rows)
    except Exception:
        pass
    try:
        return list((state or {}).get("selection", {}).get("rows", []) or [])
    except Exception:
        return []


def _set_sym(k: str):
    SS["sym"] = k
    SS["ident"] = None
    SS["sel_trade"] = None
    SS["draw_shapes"] = []
    SS["draw_pts"] = []
    SS["last_pt_sig"] = None


def _clear_sel():
    SS["sel_trade"] = None
    SS.pop("tt", None)


def _intraday(key: str, td):
    lab = _load_1m_labeled(key)
    td = pd.Timestamp(td)
    if lab.empty:
        return lab, float("nan")
    sub = lab[lab["交易日"] == td]
    before = lab[lab["交易日"] < td]
    prev = float(before["close"].iloc[-1]) if len(before) else float("nan")
    return sub, prev


def _intraday_td(key: str, want) -> tuple:
    sub, prev = _intraday(key, want)
    if not sub.empty:
        return sub, prev, pd.Timestamp(want)
    lab = _load_1m_labeled(key)
    if lab.empty:
        return sub, prev, pd.Timestamp(want)
    td = pd.Timestamp(lab["交易日"].iloc[-1])
    sub, prev = _intraday(key, td)
    return sub, prev, td


def _prev_close(key: str, cur_x, period: int) -> float:
    if cur_x is None or pd.isna(cur_x):
        return float("nan")
    closing = _day_closes(key).copy()
    if closing.empty:
        return float("nan")
    closing.index = pd.to_datetime(closing.index)
    td = (pd.Timestamp(cur_x).normalize() if int(period) == 1440
          else pd.Timestamp(sessions.trading_day_of_bj(cur_x)))
    i = int(closing.index.searchsorted(td, side="left")) - 1
    return float(closing.iloc[i]) if i >= 0 else float("nan")


def _zoom_range(feats: pd.DataFrame, i0: int, i1: int, sel, period: int) -> tuple:
    """初始视图：选中笔 → 那笔前后；否则 → 区间最后 200 根。"""
    if sel is not None:
        e0, e1 = sel.get("开仓时间"), sel.get("平仓时间")
        if int(period) == 1440:
            e0, e1 = _to_trading_day(e0), _to_trading_day(e1)
        if pd.notna(e0):
            j0 = int(feats["x"].searchsorted(pd.Timestamp(e0), side="right")) - 1
            j1 = (int(feats["x"].searchsorted(pd.Timestamp(e1), side="right")) - 1
                  if pd.notna(e1) else j0)
            j0 = max(i0, j0 - 40)
            j1 = min(i1, max(j1, j0) + 20)
            if j1 > j0:
                return (feats["x"].iloc[j0], feats["x"].iloc[j1])
    j0 = max(i0, i1 - 200 + 1)
    return (feats["x"].iloc[j0], feats["x"].iloc[i1])


# ---------------------------------------------------------------------------
# 顶部控制栏（只剩一行）
# ---------------------------------------------------------------------------
r1 = st.columns([1.45, 0.8, 0.8, 0.7, 0.55])
key = r1[0].selectbox("品种", keys, key="sym",
                      format_func=lambda k: f"{data.RECENT_SPECS[k][0]}（{k}）",
                      help="切换合约")
period = r1[1].selectbox("周期", [5, 15, 1440], format_func=lambda p: PERIOD_LABEL[p],
                         help="5 分钟 / 15 分钟 / 日线")
range_opt = r1[2].selectbox("区间", list(RANGE_OPTS), index=2,
                            help="最多加载多少根 K 线；拖动/缩放只在区间内")

with r1[3].popover("图层 ▾"):
    show_ma = st.checkbox("MA13", True)
    show_vol = st.checkbox("成交量", True)
    show_events = st.checkbox("买卖点", True)
    show_stops = st.checkbox("止损线", True)
    show_panel = st.checkbox("右侧面板", True)
    st.divider()
    draw_mode = st.selectbox("画线", ["无", "水平线", "趋势线", "通道"], key="draw_mode",
                             help="选非「无」后在图上点取点；选「无」时拖图不触发重算")
    if st.button("清空画线", use_container_width=True):
        SS["draw_shapes"] = []
        SS["draw_pts"] = []
        SS["last_pt_sig"] = None
    if SS.get("draw_pts"):
        st.caption(f"已取 {len(SS['draw_pts'])} 点")
    if SS.get("draw_shapes"):
        st.caption(f"已画 {len(SS['draw_shapes'])} 条")

with r1[4].popover("说明 ▾"):
    st.markdown(
        "- **品种 / 周期 / 区间**：区间决定**最多加载多少根 K 线**。\n"
        "- **拖动 / 滚轮**：图可以左右拖、滚轮缩放，想看哪里拉到哪里；右上角有缩放/复位按钮。\n"
        "- **鼠标悬停**：弹出该根详细价格（开高低收、涨跌、量、MA13、ATR14）。\n"
        "- **图层**：显示/隐藏指标、买卖点、止损线、右侧面板；画线也在这里。\n"
        "- **成交表**：点一行 → 图自动缩放到那笔并亮红。\n"
        "- **预热全部合约**：首次点一下，之后换合约/周期秒开。"
    )
    if st.button("预热全部合约", use_container_width=True, help="首次稍慢，之后切换合约/周期秒开"):
        jobs = [(k, p) for k in keys for p in (1, 5, 15, 1440)]
        prog = st.progress(0.0, text="预热中…")
        for i, (k, p) in enumerate(jobs):
            try:
                _load(k, int(p))
                if int(p) == 1:
                    _load_1m_labeled(k)
            except Exception:
                pass
            prog.progress((i + 1) / len(jobs),
                          text=f"{data.RECENT_SPECS[k][0]} {p}min  {i + 1}/{len(jobs)}")
        prog.progress(1.0, text="预热完成")
        st.success("已全部载入内存，切换合约/周期会快很多")

# ---------------------------------------------------------------------------
# 载入
# ---------------------------------------------------------------------------
try:
    feats, meta = _load(key, int(period))
    ev, tr, cn = _load_ledger(key)
except Exception as e:
    st.error(f"数据加载失败：{e}")
    st.stop()

if feats.empty:
    st.warning("该品种/周期没有数据。")
    st.stop()

ident = (key, int(period))
if SS.get("ident") != ident:
    SS["ident"] = ident
    SS["sel_trade"] = None
    SS["draw_shapes"] = []
    SS["draw_pts"] = []
    SS["last_pt_sig"] = None
    SS.pop("tt", None)
    SS.pop("chart", None)

# 区间 = 最多加载多少根
xmax = feats["x"].max()
days = RANGE_OPTS[range_opt]
i0 = 0 if days is None else int(feats["x"].searchsorted(xmax - pd.Timedelta(days=days), side="left"))
i1 = len(feats) - 1
i0 = max(0, min(i0, i1))

# ---------------------------------------------------------------------------
# 主体三栏
# ---------------------------------------------------------------------------
if show_panel:
    left, center, right = st.columns([0.78, 2.95, 1.0])
else:
    left, center = st.columns([0.78, 3.95])
    right = None

last = feats.iloc[i1]

# ---- 左：自选 + 分时 + 行情 ----
with left:
    prices = _last_prices()
    st.caption("自选合约")
    for k in keys:
        cn_k = data.RECENT_SPECS[k][0]
        px = prices.get(k, float("nan"))
        label = f"{cn_k} {k}  {px:,.0f}" if pd.notna(px) else f"{cn_k} {k}"
        st.button(label, key=f"wl_{k}", use_container_width=True,
                  type="primary" if k == key else "secondary",
                  on_click=_set_sym, args=(k,))

    sel_now = _selected(tr, SS.get("sel_trade"))
    if sel_now is not None:
        want_td = (_to_trading_day(sel_now["开仓时间"]) if int(period) == 1440
                   else pd.Timestamp(sessions.trading_day_of_bj(sel_now["开仓时间"])))
    elif int(period) == 1440:
        want_td = pd.Timestamp(last["x"]).normalize()
    else:
        # 最新交易日可能只有夜盘（夜盘归下一交易日）⇒ 取最后一个有早/午盘的交易日
        lab1 = _load_1m_labeled(key)
        day_rows = lab1[lab1["时段"].isin(["早盘", "午盘"])] if not lab1.empty else lab1
        want_td = (pd.Timestamp(day_rows["交易日"].iloc[-1]) if not day_rows.empty
                   else pd.Timestamp(sessions.trading_day_of_bj(last["x"])))
    sub1, prev_c, td_show = _intraday_td(key, want_td)
    st.plotly_chart(
        charts.build_intraday_figure(
            sub1, height=260,
            title=f"分时 {pd.Timestamp(td_show).strftime('%m-%d')}",
            prev_close=prev_c, template="plotly_white",
        ),
        use_container_width=True, theme=None,
        config={"displayModeBar": False}, key="intraday",
    )

    pc = _prev_close(key, last["x"], int(period))
    amp = ((float(last["high"]) - float(last["low"])) / pc) if (pd.notna(pc) and pc) else float("nan")
    amp_txt = f"{amp * 100:.1f}%" if pd.notna(amp) else "—"
    st.markdown(
        f"""<div style="border:1px solid #2a2f3a;border-radius:8px;padding:6px 9px;
                        background:#1a1d26;font-size:11px;color:#d6dae2;margin-top:2px;">
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:2px 8px;">
            <div>今开 <b>{float(last['open']):g}</b></div><div>最高 <b>{float(last['high']):g}</b></div>
            <div>最低 <b>{float(last['low']):g}</b></div><div>收盘 <b>{float(last['close']):g}</b></div>
            <div>昨收 <b>{_num(pc)}</b></div><div>振幅 <b>{amp_txt}</b></div>
          </div>
        </div>""",
        unsafe_allow_html=True,
    )
    if sel_now is not None:
        st.markdown(
            f"""<div style="border:1px solid #5a1a1a;border-radius:8px;padding:5px 9px;
                            background:#2a1414;font-size:11px;color:#ffb3b3;margin-top:3px;">
              <b>选中 {sel_now['方向']} {int(sel_now['手数'])}手</b><br>
              {_fmt_t(sel_now['开仓时间'])} @ {_num(sel_now['开仓价'])}<br>
              盈亏 <b>{_sgn(sel_now['盈亏'])}</b>
            </div>""",
            unsafe_allow_html=True,
        )


# ---- 中：行情条 + 整段K线（拖动/缩放）----
with center:
    pc = _prev_close(key, last["x"], int(period))
    chg = (float(last["close"]) - pc) if pd.notna(pc) else float("nan")
    pct = (chg / pc * 100.0) if (pd.notna(pc) and pc) else float("nan")
    qcolor = "#ef4444" if (pd.notna(chg) and chg >= 0) else "#22c55e"
    chg_txt = f"{chg:+.1f}  {pct:+.2f}%" if pd.notna(chg) else "—"
    seg = "日线" if int(period) == 1440 else sessions.seg_of_bj(last["x"])
    st.markdown(
        f"""<div style="display:flex;align-items:baseline;gap:12px;padding:5px 10px;
                        background:linear-gradient(90deg,#151d2e,#1f2d47);border-radius:6px;
                        color:#e8edf5;">
          <span style="font-size:15px;font-weight:700;">{cn} {key}</span>
          <span style="font-size:11px;opacity:.6;">{PERIOD_LABEL[int(period)]}</span>
          <span style="font-size:24px;font-weight:800;color:{qcolor};">{float(last['close']):,.1f}</span>
          <span style="font-size:13px;color:{qcolor};">{chg_txt}</span>
          <span style="font-size:11px;opacity:.7;">开 {float(last['open']):g} · 高 {float(last['high']):g} · 低 {float(last['low']):g} · 昨收 {_num(pc)}</span>
          <span style="margin-left:auto;font-size:11px;opacity:.65;">{seg}</span>
        </div>""",
        unsafe_allow_html=True,
    )

    # 整段数据（区间内全部加载），初始视图只显示最后 200 根 / 选中笔附近
    view = feats.iloc[i0: i1 + 1].copy()
    x0, x1 = view["x"].iloc[0], view["x"].iloc[-1]

    sel = _selected(tr, SS.get("sel_trade"))
    ev_v = _events_view(ev, x0, x1, int(period)) if show_events else None
    tr_v = _trades_view(tr, x0, x1, int(period)) if show_stops else None
    x_range = _zoom_range(feats, i0, i1, sel, int(period))

    # 画线：在画图前处理上一次点击（避免多跑一轮）
    if draw_mode != "无":
        if draw_mode != SS.get("_draw_prev"):
            SS["_draw_prev"] = draw_mode
            SS["draw_pts"] = []
            SS["last_pt_sig"] = None
        pts = _points(SS.get("chart")) if SS.get("chart") else []
        if pts:
            p = pts[-1]
            xv, yv = p.get("x"), p.get("y")
            if xv is not None and yv is not None:
                try:
                    xt, yt = pd.Timestamp(xv), float(yv)
                except Exception:
                    xt = None
                if xt is not None:
                    sig = (str(xt), round(yt, 6))
                    if sig != SS.get("last_pt_sig"):
                        SS["last_pt_sig"] = sig
                        SS.setdefault("draw_pts", []).append({"x": xt, "y": yt})
                        got = SS["draw_pts"]
                        if draw_mode == "水平线":
                            SS["draw_shapes"].append({"type": "hline", "y": got[-1]["y"]})
                            SS["draw_pts"] = []
                        elif draw_mode in ("趋势线", "通道") and len(got) >= 2:
                            a, b = got[-2], got[-1]
                            rec = {"x0": a["x"], "y0": a["y"], "x1": b["x"], "y1": b["y"]}
                            if draw_mode == "趋势线":
                                rec["type"] = "trend"
                            else:
                                rec["type"] = "channel"
                                try:
                                    rec["w"] = float(view["atr"].iloc[-1]) * 0.5
                                except Exception:
                                    rec["w"] = 10.0
                            SS["draw_shapes"].append(rec)
                            SS["draw_pts"] = []

    fig = charts.build_terminal_figure(
        view,
        show_ma=show_ma, show_boll=False, show_atr=False,
        show_vol=bool(show_vol and meta.get("has_volume")),
        events=ev_v, trades=tr_v, selected=sel,
        shade=None, shapes=SS.get("draw_shapes"),
        height=CHART_H,
        rangebreaks=sessions.rangebreaks(int(period)),
        template="plotly_white",
        ma_n=13, atr_n=14, mult=data.MULT.get(key, 1),
        show_events=show_events, show_stops=show_stops,
        x_range=x_range,
        hover_fmt="%Y-%m-%d" if int(period) == 1440 else "%m-%d %H:%M",
    )

    cfg = {"displayModeBar": True, "scrollZoom": True,
           "modeBarButtonsToRemove": ["select2d", "lasso2d"]}
    if draw_mode != "无":
        st.plotly_chart(fig, use_container_width=True, theme=None, config=cfg,
                        on_select="rerun", selection_mode="points", key="chart")
    else:
        st.plotly_chart(fig, use_container_width=True, theme=None, config=cfg, key="chart")

    stamp = pd.Timestamp(last["x"]).strftime("%Y-%m-%d" if int(period) == 1440 else "%Y-%m-%d %H:%M")
    st.caption(
        f"**{stamp}** ｜ 最新 开 {last['open']:g}  高 {last['high']:g}  低 {last['low']:g}  收 {last['close']:g}"
        f" ｜ MA13 {last['ma']:.1f} ｜ ATR14 {last['atr']:.2f}"
        f" ｜ 加载 {len(view)} 根 ｜ 拖动/滚轮缩放 · 悬停看该根详情"
    )


# ---- 右：成交 / 详情 / 状态 ----
if right is not None:
    with right:
        with st.container(height=CHART_H + 62):
            tab_tr, tab_dt, tab_st = st.tabs(["成交", "详情", "状态"])

            with tab_tr:
                left_n = int((tr["配对"] == "未平仓").sum()) if not tr.empty else 0
                st.caption(f"{cn} ｜ {len(tr)} 笔" + (f" ｜ 未平 {left_n}" if left_n else ""))
                show = tr[["方向", "手数", "开仓时间", "盈亏"]].copy()
                show["开仓时间"] = pd.to_datetime(show["开仓时间"]).dt.strftime("%m-%d %H:%M")
                show["盈亏"] = show["盈亏"].map(lambda v: f"{v:+.0f}" if pd.notna(v) else "—")

                sel_idx = SS.get("sel_trade")

                def _row_style(row, _si=sel_idx):
                    is_sel = _si is not None and int(row.name) == int(_si)
                    out = []
                    for col in row.index:
                        if is_sel:
                            out.append("background-color:#5a1a1a;color:#ffb3b3;font-weight:700")
                            continue
                        s = "background-color:#151821" if int(row.name) % 2 == 0 else ""
                        if col == "方向":
                            s += ";color:#ff6b6b" if row["方向"] == "多" else ";color:#4ade80"
                        elif col == "盈亏":
                            v = str(row["盈亏"])
                            s += (";color:#ff6b6b" if v.startswith("+")
                                  else (";color:#4ade80" if (v.startswith("-") or v.startswith("−"))
                                        else ";color:#9aa3af"))
                        out.append(s)
                    return out

                stt2 = st.dataframe(
                    show.style.apply(_row_style, axis=1),
                    use_container_width=True, hide_index=True,
                    height=CHART_H - 60,
                    on_select="rerun", selection_mode="single-row", key="tt",
                )
                rows = _sel_rows(stt2)
                if rows:
                    SS["sel_trade"] = int(rows[0])

            with tab_dt:
                sel = _selected(tr, SS.get("sel_trade"))
                if sel is None:
                    st.caption("点「成交」里某一行 → 图缩放到那笔并亮红")
                else:
                    st.markdown(f"**{sel['方向']} {int(sel['手数'])} 手** · `{sel.get('配对','')}`")
                    c = st.columns(2)
                    c[0].metric("盈亏（毛利）", _sgn(sel.get("盈亏")))
                    c[1].metric("持仓", f"{sel['持仓分钟']:.0f} 分" if pd.notna(sel.get("持仓分钟")) else "—")
                    st.markdown(
                        f"开 **{_fmt_t(sel.get('开仓时间'))}** @ {_num(sel.get('开仓价'))}  \n"
                        f"平 **{_fmt_t(sel.get('平仓时间'))}** @ {_num(sel.get('平仓价'))}  \n"
                        f"止损 **{_num(sel.get('止损'))}** ｜ ATR **{_num(sel.get('ATR'))}**  \n"
                        f"MFE **{_sgn(sel.get('MFE'))}** 元 ｜ MAE **{_sgn(sel.get('MAE'))}** 元  \n"
                        f"原因 **{sel.get('原因') or '—'}**  \n"
                        f"<span style='opacity:.55;font-size:.8em'>盈亏=价格×方向×手数×乘数，未扣手续费</span>",
                        unsafe_allow_html=True,
                    )
                    st.button("取消选中", use_container_width=True, on_click=_clear_sel)

            with tab_st:
                cur = feats.iloc[i1]
                st.metric("MA13", f"{cur['ma']:.1f}" if pd.notna(cur["ma"]) else "—")
                st.metric("斜率", f"{cur['slope']:+.2f}" if pd.notna(cur["slope"]) else "—")
                st.metric("ATR14", f"{cur['atr']:.2f}" if pd.notna(cur["atr"]) else "—")
                st.metric("距 MA（ATR 倍）", f"{cur['atr_dist']:+.2f}" if pd.notna(cur["atr_dist"]) else "—")
                seg = "日线" if int(period) == 1440 else sessions.seg_of_bj(cur["x"])
                st.caption(f"最新 {pd.Timestamp(cur['x'])} ｜ {seg}")

                sel = _selected(tr, SS.get("sel_trade"))
                if sel is not None and int(period) != 1440:
                    st.divider()
                    st.caption("选中笔 · **开仓时**")
                    sdf = feats[["x", "close", "ma", "slope", "atr", "atr_dist"]].rename(
                        columns={"x": "datetime"})
                    snap = T.state_at(sdf, sel.get("开仓时间"))
                    if snap:
                        st.caption(
                            f"收 {snap['close']:g} ｜ MA {snap['ma']:.1f} ｜ 斜率 {snap['slope']:+.2f}\n\n"
                            f"ATR {snap['atr']:.2f} ｜ 距MA {snap['atr_dist']:+.2f} ATR\n\n"
                            f"止损 {_num(sel.get('止损'))} ｜ 目标 {int(sel['手数'])} 手"
                        )
                    else:
                        st.caption("开仓时刻不在当前数据范围内")
