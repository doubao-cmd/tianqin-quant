# -*- coding: utf-8 -*-
"""
复盘工具 · 图表层（阶段 3）
================================================================
**无状态**：只吃已经算好指标的 DataFrame（`features.build_all()` 的输出），
返回 `plotly.graph_objects.Figure`。**不读文件、不连网、不缓存、无模块级可变状态**
（简报 §4 四层硬约束）。

主图（简报 §4 阶段 3 规定）：
  `make_subplots(rows=2, shared_xaxes=True)`
    · 上：K 线 + MA13 + 布林(20,2)
    · 下：ATR14
  x 轴用真实 datetime（不是序号）。`rangebreaks` 默认取 `sessions.rangebreaks()`（周末 + 日内空档，
  §6·8）；**调用方可显式传入** —— 日线（1440）须传 `sessions.rangebreaks(1440)`（只去周末），
  否则日线 bar 时刻恒为 16:00、会落进日内空档 [15,21) 被隐藏成空白图。

中文字体按 §6·12 统一 `Microsoft YaHei`；涨跌色按国内习惯（红涨绿跌）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from plotly.subplots import make_subplots
import plotly.graph_objects as go

import sessions

FONT = dict(family="Microsoft YaHei")
UP, DOWN = "#ef4444", "#22c55e"          # 红涨 / 绿跌（提亮，深色背景下更醒目）

_MA_COLOR = "#f2a900"                     # MA13 橙黄
_ATR_COLOR = "#7b61ff"                    # ATR 紫
_BOLL_COLOR = "rgba(120,120,120,0.55)"    # 布林灰（半透明，压在 K 线底下不抢视线）


def _x(df: pd.DataFrame):
    """x 轴：优先 datetime；缺失则退回整数序号（便于无时间戳的临时切片）。"""
    if "datetime" in df.columns:
        return pd.to_datetime(df["datetime"])
    return np.arange(len(df))


def make_kline_figure(df: pd.DataFrame, title: str = "",
                      show_ma: bool = True, show_boll: bool = True,
                      rollover_points: pd.DataFrame | None = None,
                      rangebreaks: list | None = None,
                      height: int = 620) -> go.Figure:
    """构造 2 行子图（上 K 线+MA13+布林，下 ATR14）。

    参数
    ----
    df : 需含 `open/high/low/close`；若含 `ma13/atr14/boll_mid/boll_upper/boll_lower`
         则一并画出（由 `features.build_all()` 产出）。缺列自动跳过对应图层。
    rollover_points : `data.rollover_points()` 的结果（列 日期/旧/新），画换月竖线
         防换月跳空被误读成"指标算错"（§6·3）。None = 不画（本层不读文件）。
    """
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.74, 0.26], vertical_spacing=0.04,
                        subplot_titles=("K 线 · MA13 · 布林(20,2)", "ATR14"))

    x = _x(df)

    # ---- 上：布林（先画，压在 K 线底下）----
    if show_boll and {"boll_upper", "boll_lower"}.issubset(df.columns):
        fig.add_trace(go.Scatter(x=x, y=df["boll_upper"], mode="lines",
                                 line=dict(color=_BOLL_COLOR, width=1),
                                 name="布林上轨", hoverinfo="skip", showlegend=False),
                      row=1, col=1)
        fig.add_trace(go.Scatter(x=x, y=df["boll_lower"], mode="lines",
                                 line=dict(color=_BOLL_COLOR, width=1),
                                 name="布林下轨", hoverinfo="skip", showlegend=False,
                                 fill="tonexty", fillcolor="rgba(120,120,120,0.08)"),
                      row=1, col=1)
        if "boll_mid" in df.columns:
            fig.add_trace(go.Scatter(x=x, y=df["boll_mid"], mode="lines",
                                     line=dict(color="rgba(120,120,120,0.9)", width=1, dash="dot"),
                                     name="布林中轨", showlegend=False),
                          row=1, col=1)

    # ---- 上：K 线 ----
    fig.add_trace(go.Candlestick(
        x=x, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name="K 线",
        increasing=dict(line=dict(color=UP, width=1), fillcolor=UP),
        decreasing=dict(line=dict(color=DOWN, width=1), fillcolor=DOWN),
    ), row=1, col=1)

    # ---- 上：MA13 ----
    if show_ma and "ma13" in df.columns:
        fig.add_trace(go.Scatter(x=x, y=df["ma13"], mode="lines",
                                 line=dict(color=_MA_COLOR, width=1.6), name="MA13"),
                      row=1, col=1)

    # ---- 下：ATR14 ----
    if "atr14" in df.columns:
        fig.add_trace(go.Scatter(x=x, y=df["atr14"], mode="lines",
                                 line=dict(color=_ATR_COLOR, width=1.4), name="ATR14"),
                      row=2, col=1)

    # ---- 换月竖线（§6·3：不画会被当成"指标算错"）----
    if rollover_points is not None and len(rollover_points):
        for _, r in rollover_points.iterrows():
            if x is not None and len(x):
                try:
                    if not (x.iloc[0] <= r["日期"] <= x.iloc[-1]):
                        continue
                except Exception:
                    pass
            label = f"换月 {r.get('旧', '')}→{r.get('新', '')}"
            fig.add_vline(x=r["日期"], line=dict(color="#999", width=1, dash="dash"),
                          row=1, col=1)
            fig.add_annotation(x=r["日期"], y=1.0, xref="x", yref="y domain",
                               text=label, showarrow=False, row=1, col=1,
                               font=dict(size=10, color="#666"),
                               yanchor="bottom")

    fig.update_layout(
        height=height,
        font=FONT,
        title=dict(text=title, font=dict(size=15)) if title else None,
        margin=dict(l=10, r=10, t=48 if title else 28, b=10),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0,
                    bgcolor="rgba(0,0,0,0)"),
        dragmode="pan",
    )
    fig.update_xaxes(rangeslider_visible=False)
    # §6·8：rangebreaks 不会自己算日内休市 ⇒ 显式给（前提：x 轴已是**北京时间**，
    #        由 app 的 `_bj_view()` 换算；原始 CSV 是 UTC，见 data.TZ_NOTE）
    fig.update_xaxes(rangebreaks=rangebreaks or sessions.rangebreaks())
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="ATR", row=2, col=1)
    return fig


def build_kline_atr(df: pd.DataFrame, **kw) -> go.Figure:
    """`make_kline_figure` 的别名（阶段 3 自检与 app 里的常用入口名）。"""
    return make_kline_figure(df, **kw)


def make_atr_dist_figure(df: pd.DataFrame, height: int = 380,
                         rangebreaks: list | None = None) -> go.Figure:
    """辅助图：距 MA13 的 ATR 倍数 (C−MA13)/ATR —— 06 进场条件本体（§6·4，必须画）。

    只画分布形态给"看"，**不输出任何"该不该开"的结论**（§8·5、§8·4红线）。
    """
    fig = go.Figure()
    if "atr_dist" in df.columns:
        x = _x(df)
        fig.add_trace(go.Scatter(x=x, y=df["atr_dist"], mode="lines",
                                 line=dict(color="#1f77b4", width=1.2),
                                 name="(C−MA13)/ATR"))
        fig.add_hline(y=0.5, line=dict(color="#bbb", width=1, dash="dot"),
                      annotation_text="+0.5", annotation_position="right")
        fig.add_hline(y=-0.5, line=dict(color="#bbb", width=1, dash="dot"),
                      annotation_text="−0.5", annotation_position="right")
    fig.update_layout(height=height, font=FONT, hovermode="x unified",
                      margin=dict(l=10, r=10, t=28, b=10),
                      yaxis_title="ATR 倍数", showlegend=False)
    fig.update_xaxes(rangebreaks=rangebreaks or sessions.rangebreaks())
    return fig


# ===========================================================================
# 复盘终端主图（单屏版）
# ===========================================================================
def _is_selected(t, selected) -> bool:
    """判断某笔交易是否就是当前选中笔（按开仓时间+方向）。"""
    if selected is None:
        return False
    try:
        return (t.get("开仓时间") == selected.get("开仓时间")
                and t.get("方向") == selected.get("方向"))
    except Exception:
        return False


def build_terminal_figure(df: pd.DataFrame, *,
                          title: str = "",
                          show_ma: bool = True, show_boll: bool = True,
                          show_atr: bool = True, show_vol: bool = False,
                          events: pd.DataFrame | None = None,
                          trades: pd.DataFrame | None = None,
                          selected=None,
                          shade: list | None = None,
                          shapes: list | None = None,
                          height: int = 600,
                          rangebreaks: list | None = None,
                          template: str = "plotly_white",
                          ma_n: int = 13, slope_n: int = 5, atr_n: int = 14,
                          boll_n: int = 20, mult: int = 10,
                          show_events: bool = True, show_stops: bool = True,
                          x_range=None, hover_fmt: str = "%m-%d %H:%M") -> go.Figure:
    """交易终端主图：K 线 + 指标 + 买卖点 + 止损线 + MAE/MFE + 时段底色 + 画线。

    `df` 需含 `x`（**显示时间**：日内=北京时间，日线=交易日）、OHLC、`ma/slope/atr/boll_*`。
    `events`：`trades.load_events()` 的输出（原始 FILL，图上买卖点的事实来源）。
    `trades`：`trades.load_trades()` 的输出（止损线 / 选中笔的 MAE-MFE）。
    `shade`：[(x0, x1, rgba), ...] 时段底色。
    `shapes`：用户在图上画的线（hline / trend / channel）。
    所有叠加层都按 `x.iloc[-1]` **物理截断**，回放时不泄露未来。
    """
    x = pd.to_datetime(df["x"]) if "x" in df.columns else _x(df)

    panes = 1 + int(bool(show_atr)) + int(bool(show_vol))
    if panes == 1:
        heights = [1.0]
    elif panes == 2:
        heights = [0.74, 0.26]
    else:
        heights = [0.62, 0.19, 0.19]
    titles = ["K 线"]
    if show_atr:
        titles.append(f"ATR{atr_n}")
    if show_vol:
        titles.append("成交量")

    fig = make_subplots(rows=panes, cols=1, shared_xaxes=True,
                        row_heights=heights, vertical_spacing=0.04)
    r_atr = 2 if show_atr else None
    r_vol = (2 + int(bool(show_atr))) if show_vol else None

    # ---- 布林（压在 K 线底下）----
    if show_boll and {"boll_upper", "boll_lower"}.issubset(df.columns):
        fig.add_trace(go.Scatter(x=x, y=df["boll_upper"], mode="lines",
                                 line=dict(color=_BOLL_COLOR, width=1),
                                 name="布林上轨", hoverinfo="skip", showlegend=False), row=1, col=1)
        fig.add_trace(go.Scatter(x=x, y=df["boll_lower"], mode="lines",
                                 line=dict(color=_BOLL_COLOR, width=1),
                                 name="布林下轨", hoverinfo="skip", showlegend=False,
                                 fill="tonexty", fillcolor="rgba(120,120,120,0.08)"), row=1, col=1)
        if "boll_mid" in df.columns:
            fig.add_trace(go.Scatter(x=x, y=df["boll_mid"], mode="lines",
                                     line=dict(color="rgba(120,120,120,0.85)", width=1, dash="dot"),
                                     name="布林中轨", showlegend=False), row=1, col=1)

    # ---- K 线（悬停详情用 customdata 数字列，比 1000+ 条长字符串轻得多）----
    cd, ht = None, None
    if {"volume", "ma", "atr"}.issubset(df.columns):
        c_arr = df["close"].to_numpy(float)
        prevc = np.roll(c_arr, 1)
        prevc[0] = c_arr[0]
        chg = c_arr - prevc
        with np.errstate(divide="ignore", invalid="ignore"):
            pct = np.where(prevc > 0, chg / prevc * 100.0, 0.0)
        cd = np.column_stack([
            pd.to_numeric(df["volume"], errors="coerce").fillna(0).to_numpy(float),
            df["ma"].to_numpy(float),
            df["atr"].to_numpy(float),
            chg,
            pct,
        ])
        ht = (
            "<b>%{x|" + hover_fmt + "}</b><br>"
            "开 %{open:.1f}　高 %{high:.1f}<br>"
            "低 %{low:.1f}　收 %{close:.1f}<br>"
            "涨跌 %{customdata[3]:+.1f}（%{customdata[4]:+.2f}%）<br>"
            "量 %{customdata[0]:,.0f}<br>"
            "MA13 %{customdata[1]:.1f}　ATR14 %{customdata[2]:.2f}"
            "<extra></extra>"
        )
    fig.add_trace(go.Candlestick(
        x=x, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name="K 线", increasing=dict(line=dict(color=UP, width=1), fillcolor=UP),
        decreasing=dict(line=dict(color=DOWN, width=1), fillcolor=DOWN),
        customdata=cd, hovertemplate=ht,
    ), row=1, col=1)

    # ---- MA ----
    if show_ma and "ma" in df.columns:
        fig.add_trace(go.Scatter(x=x, y=df["ma"], mode="lines",
                                 line=dict(color=_MA_COLOR, width=1.6),
                                 name=f"MA{ma_n}", hoverinfo="skip"), row=1, col=1)

    # ---- ATR ----
    if show_atr and "atr" in df.columns:
        fig.add_trace(go.Scatter(x=x, y=df["atr"], mode="lines",
                                 line=dict(color=_ATR_COLOR, width=1.3),
                                 name=f"ATR{atr_n}", showlegend=False), row=r_atr, col=1)

    # ---- 成交量 ----
    if show_vol and "volume" in df.columns:
        colors = [UP if c >= o else DOWN for o, c in zip(df["open"], df["close"])]
        fig.add_trace(go.Bar(x=x, y=df["volume"], marker_color=colors,
                             name="成交量", showlegend=False, opacity=0.55,
                             hoverinfo="skip"),
                      row=r_vol, col=1)

    last_x = x.iloc[-1]

    # ---- 买卖点（原始 FILL；物理截断）----
    if show_events and events is not None and len(events):
        ev = events[events["时间"] <= last_x]
        specs = [
            ("开", "多", UP, "triangle-up", 16, "多开", "多"),
            ("开", "空", DOWN, "triangle-down", 16, "空开", "空"),
            ("平", "多", "#ff9aa2", "x", 12, "多平", ""),
            ("平", "空", "#86efac", "x", 12, "空平", ""),
        ]
        for typ, dr, color, symbol, size, label, txt in specs:
            sub = ev[(ev["类型"] == typ) & (ev["方向"] == dr)]
            if sub.empty:
                continue
            # 光晕：大一圈半透明，深色背景上先抓住眼睛
            fig.add_trace(go.Scatter(
                x=sub["时间"], y=sub["价格"], mode="markers",
                marker=dict(symbol=symbol, size=size + 10, color=color, opacity=0.22,
                            line=dict(width=0)),
                hoverinfo="skip", showlegend=False,
            ), row=1, col=1)
            # 本体：白描边 + 开仓标注「多/空」
            fig.add_trace(go.Scatter(
                x=sub["时间"], y=sub["价格"],
                mode="markers+text" if txt else "markers",
                marker=dict(symbol=symbol, size=size, color=color,
                            line=dict(width=1.6, color="#ffffff")),
                text=([txt] * len(sub)) if txt else None,
                textposition="top center" if txt else None,
                textfont=dict(size=10, color=color),
                name=label, showlegend=False,
                customdata=[[int(q), float(p)] for q, p in zip(sub["手数"], sub["价格"])],
                hovertemplate=f"{label} %{{customdata[0]}}手 @%{{customdata[1]:g}}<extra></extra>",
            ), row=1, col=1)

    # ---- 止损线（每笔一条，选中笔加粗橙色）----
    if show_stops and trades is not None and len(trades):
        for _, t in trades.iterrows():
            # ★ 回放只能用「开仓时已知」的计划止损（SIGNAL stop≈），不能用出场时的 STOPLOSS 行
            stp = t.get("计划止损")
            if pd.isna(stp):
                stp = t.get("止损")
            if pd.isna(stp):
                continue
            x0 = t.get("开仓时间")
            x1 = t.get("平仓时间")
            if pd.isna(x0) or x0 > last_x:
                continue
            # ★ 必须裁到当前窗口：否则早期成交的止损线会把 X 轴撑到左边，留下一片空白
            x0t = max(x0, x.iloc[0])
            x1t = min(last_x if pd.isna(x1) else x1, last_x)
            if x1t <= x0t:
                continue
            sel = _is_selected(t, selected)
            sel_color = UP if (selected or {}).get("方向") == "多" else DOWN
            fig.add_shape(type="line", x0=x0t, x1=x1t, y0=float(stp), y1=float(stp),
                          line=dict(color=sel_color if sel else "rgba(190,190,190,0.55)",
                                    width=2.4 if sel else 1, dash="dot"),
                          row=1, col=1)

    # ---- 选中笔：持仓区间底色（多红/空绿）+ 高亮开仓箭头 + MAE/MFE 价位线 ----
    if selected is not None:
        e, q = selected.get("开仓价"), selected.get("手数")
        x0, x1 = selected.get("开仓时间"), selected.get("平仓时间")
        is_long = selected.get("方向") == "多"
        hi_color = UP if is_long else DOWN
        fill = "rgba(239,68,68,0.18)" if is_long else "rgba(34,197,94,0.18)"
        if pd.notna(x0) and x0 <= last_x:
            x1c = last_x if (pd.isna(x1) or x1 > last_x) else x1
            vx0, vx1 = max(x0, x.iloc[0]), min(x1c, last_x)
            if vx1 > vx0:
                fig.add_vrect(x0=vx0, x1=vx1, fillcolor=fill,
                              line_width=0, layer="below", row=1, col=1)
        # ★ 点表格里的这一笔 ⇒ 图上这只箭头放大 + 亮色描边（多单亮红 / 空单亮绿）
        if pd.notna(x0) and pd.notna(e) and x.iloc[0] <= x0 <= last_x:
            fig.add_trace(go.Scatter(
                x=[x0], y=[float(e)], mode="markers",
                marker=dict(symbol="triangle-up" if is_long else "triangle-down",
                            size=24, color=hi_color,
                            line=dict(width=3, color="#ffe082")),
                name="选中", showlegend=False,
                hovertemplate=f"选中 · {selected.get('方向')} {int(q)}手<extra></extra>",
            ), row=1, col=1)
        if pd.notna(e) and pd.notna(q) and float(q) > 0 and mult:
            d = 1 if is_long else -1
            for k, color, lab in (("MFE", "#2ca02c", "MFE"), ("MAE", "#d62728", "MAE")):
                v = selected.get(k)
                if pd.notna(v):
                    y = float(e) + d * float(v) / (float(q) * mult)
                    fig.add_hline(y=y, line=dict(color=color, width=1, dash="dot"),
                                  annotation_text=f"{lab} {float(v):+.0f}元",
                                  annotation_position="right", row=1, col=1,
                                  annotation_font=dict(size=9, color=color))

    # ---- 时段底色 ----
    if shade:
        for x0, x1, color in shade:
            fig.add_vrect(x0=x0, x1=x1, fillcolor=color, line_width=0,
                          layer="below", row=1, col=1)

    # ---- 用户画线 ----
    if shapes:
        for s in shapes:
            typ = s.get("type")
            col = s.get("color", "#ff9800")
            if typ == "hline":
                fig.add_hline(y=float(s["y"]), line=dict(color=col, width=1.5),
                              row=1, col=1,
                              annotation_text=f"{float(s['y']):g}",
                              annotation_position="right",
                              annotation_font=dict(size=9, color=col))
            elif typ == "trend":
                fig.add_shape(type="line", x0=s["x0"], y0=float(s["y0"]),
                              x1=s["x1"], y1=float(s["y1"]),
                              line=dict(color=col, width=1.5), row=1, col=1)
            elif typ == "channel":
                w = float(s.get("w", 0.0))
                for dy in (0.0, w, -w):
                    fig.add_shape(type="line", x0=s["x0"], y0=float(s["y0"]) + dy,
                                  x1=s["x1"], y1=float(s["y1"]) + dy,
                                  line=dict(color=col, width=1.2,
                                            dash="solid" if dy == 0 else "dot"),
                                  row=1, col=1)

    fig.update_layout(
        height=height, font=dict(family="Microsoft YaHei", color="#e6e6e6"),
        template=template,
        plot_bgcolor="#0f1117", paper_bgcolor="#0f1117",
        title=dict(text=title, font=dict(size=14)) if title else None,
        margin=dict(l=6, r=6, t=38 if title else 22, b=6),
        hovermode="closest", dragmode="pan", clickmode="event+select",
        hoverlabel=dict(bgcolor="#151d2e", bordercolor="#3a4350",
                        font=dict(color="#e6e6e6", size=11), align="left"),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0,
                    bgcolor="rgba(0,0,0,0)"),
    )
    fig.update_xaxes(rangeslider_visible=False)
    fig.update_xaxes(rangebreaks=rangebreaks or sessions.rangebreaks())
    # ★ X 轴：优先用调用方给的「初始视图范围」；否则钉死整段数据
    if x_range is not None:
        fig.update_xaxes(range=list(x_range))
    elif len(x) > 1:
        fig.update_xaxes(range=[x.iloc[0], x.iloc[-1]])
    fig.update_xaxes(gridcolor="#232733", zeroline=False,
                     tickfont=dict(color="#9aa3af"), linecolor="#2a2f3a")
    fig.update_yaxes(gridcolor="#232733", zeroline=False, title_text="价格", row=1, col=1,
                     tickfont=dict(color="#9aa3af"), linecolor="#2a2f3a",
                     title_font=dict(color="#9aa3af"))
    if r_atr:
        fig.update_yaxes(title_text=f"ATR{atr_n}", row=r_atr, col=1)
    if r_vol:
        fig.update_yaxes(title_text="量", row=r_vol, col=1)
    return fig


def build_intraday_figure(df: pd.DataFrame, *, height: int = 210, title: str = "",
                          prev_close=None, template: str = "plotly_white",
                          font_size: int = 9) -> go.Figure:
    """左侧小分时图：价格线（红涨绿跌）+ 均价线 + 昨收虚线 + 成交量。

    `df`：某**交易日**的 1min bar，需含 `x`（北京时间）、open/close/volume。
    `prev_close`：昨收；不传则用当日首根开盘。
    均价线 = VWAP（有成交量时），无成交量退化为累计均值。
    """
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.78, 0.22], vertical_spacing=0.03)
    if df is None or len(df) == 0:
        fig.update_layout(
            height=height, template=template,
            font=dict(family="Microsoft YaHei", size=font_size),
            margin=dict(l=2, r=2, t=18, b=2),
            annotations=[dict(text="无分时数据", showarrow=False, x=0.5, y=0.5,
                              xref="paper", yref="paper", font=dict(size=10, color="#999"))],
        )
        return fig

    x = pd.to_datetime(df["x"])
    c = df["close"].to_numpy(float)
    o = df["open"].to_numpy(float)
    if "volume" in df.columns:
        v = pd.to_numeric(df["volume"], errors="coerce").fillna(0).to_numpy(float)
    else:
        v = np.zeros(len(df), dtype=float)

    prev = float(prev_close) if (prev_close is not None and pd.notna(prev_close)) else float(o[0])
    color = UP if c[-1] >= prev else DOWN

    if v.sum() > 0:
        avg = np.cumsum(c * v) / np.maximum(np.cumsum(v), 1e-9)
    else:
        avg = np.cumsum(c) / np.arange(1, len(c) + 1)

    fig.add_trace(go.Scatter(x=x, y=c, mode="lines",
                             line=dict(color=color, width=1.5), name="价格",
                             hovertemplate="%{x|%H:%M}  %{y:.1f}<extra></extra>"),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=avg, mode="lines",
                             line=dict(color="#f2a900", width=1), name="均价",
                             hoverinfo="skip"), row=1, col=1)
    fig.add_hline(y=prev, line=dict(color="#999", width=1, dash="dot"), row=1, col=1)
    fig.add_trace(go.Bar(x=x, y=v,
                         marker_color=[UP if cc >= oo else DOWN for oo, cc in zip(o, c)],
                         showlegend=False, opacity=0.6,
                         hovertemplate="%{x|%H:%M}  量 %{y:.0f}<extra></extra>"),
                  row=2, col=1)

    fig.update_layout(
        height=height, template=template,
        font=dict(family="Microsoft YaHei", size=font_size, color="#e6e6e6"),
        plot_bgcolor="#0f1117", paper_bgcolor="#0f1117",
        margin=dict(l=2, r=2, t=20, b=2),
        title=dict(text=title, font=dict(size=11, color="#9aa3af"), x=0.01, y=0.99) if title else None,
        showlegend=False, hovermode="x unified", dragmode="pan",
    )
    fig.update_xaxes(rangeslider_visible=False, tickformat="%H:%M", nticks=4,
                     showgrid=False, rangebreaks=sessions.rangebreaks(1),
                     tickfont=dict(color="#9aa3af"), linecolor="#2a2f3a")
    fig.update_yaxes(side="right", nticks=4, showgrid=True,
                     gridcolor="rgba(255,255,255,0.06)",
                     tickfont=dict(color="#9aa3af"), linecolor="#2a2f3a", row=1, col=1)
    fig.update_yaxes(side="right", nticks=2, showgrid=False, row=2, col=1)
    return fig
