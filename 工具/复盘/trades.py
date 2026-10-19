# -*- coding: utf-8 -*-
"""
复盘工具 · 台账成交解析（把成交流水还原成「图上买卖点」与「每笔交易」）
================================================================
设计原则
--------
1. **原始 FILL 是事实，配平是推断**：
   - `load_events()` 只做格式转换，一条 FILL 一个事件 —— 图上买卖点**永远忠实**。
   - `load_trades()` 用状态机做尽力配平（支持反转 / 加仓 / 部分平仓 / 重启接管 /
     云端清零），每条交易带 `配对` 字段标明来源，**不假装比台账更准**。
2. **不改策略、不复制信号逻辑**：只解析台账里已记下的字段（与简报 D6 一致）。
3. 时间口径：台账 `记录时间` 是**北京时间**（夜盘 21:xx 可证）；K 线原始 datetime 是
   UTC，图上匹配前须 `data.to_beijing()`。

台账实测出现的形态（2026-10-08 下午有十余次重启，必须处理）
  · 纯开→平
  · 反转：持多时直接 `空开`（隐性平多）→ 本模块按「反转平旧+开新」处理
  · 加仓：同向连续 `多开`
  · 部分平仓：`多平 4` 只平掉一部分
  · `RECOVER`：重启后接管云端持仓（价未知）
  · `SYNC`：云端已清零 → 本进程同步退出
  · 未平仓：苯乙烯 9 次 `多开` 后没有平仓（僵尸腿）
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

import data

LEDGER_DIR = data.LEDGER_DIR
FLOW_SUFFIX = "_成交流水.csv"
ENCODING = "utf-8-sig"

RE_STOP = re.compile(r"stop≈([\d.]+)")
RE_ATR = re.compile(r"ATR\s+([\d.]+)")
RE_SL = re.compile(r"止损线\s+([\d.]+)")
RE_EXC = re.compile(r"最大浮盈=(-?[\d.]+).*?最大浮亏=(-?[\d.]+)")
RE_RECOVER_POS = re.compile(r"云端存在持仓\s+(-?\d+)\s*手")
RE_RECOVER_STOP = re.compile(r"止损≈([\d.]+)")


# ---------------------------------------------------------------------------
# 原始读取
# ---------------------------------------------------------------------------
def flow_path(sym_cn: str) -> Path:
    p = LEDGER_DIR / f"{sym_cn}{FLOW_SUFFIX}"
    if not p.exists():
        raise FileNotFoundError(f"台账里没有 {p}")
    return p


def load_flow(sym_cn: str) -> pd.DataFrame:
    """读某品种成交流水（utf-8-sig），返回按时间排序的原始表。"""
    d = pd.read_csv(flow_path(sym_cn), encoding=ENCODING, dtype=str)
    d.columns = [str(c).lstrip("\ufeff") for c in d.columns]
    d["时间"] = pd.to_datetime(d["记录时间"], errors="coerce")
    return d.sort_values("时间").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 事件（图上买卖点直接用这个，不做任何配平推断）
# ---------------------------------------------------------------------------
def load_events(sym_cn: str) -> pd.DataFrame:
    """每条 FILL / RECOVER / SYNC 一个事件。

    列：时间 / 类型(开|平|接管|清零) / 方向(多|空) / 手数 / 价格 / 合约 / 备注
    """
    d = load_flow(sym_cn)
    rows: list[dict] = []

    f = d[d["类型"] == "FILL"]
    for _, r in f.iterrows():
        dr = str(r.get("方向") or "")
        if dr not in ("多开", "空开", "多平", "空平"):
            continue
        rows.append({
            "时间": r["时间"], "类型": dr[-1], "方向": dr[0],
            "手数": int(float(r["手数"] or 0)), "价格": float(r["成交价"]),
            "合约": str(r.get("备注") or ""), "备注": "",
        })

    for _, r in d[d["类型"] == "RECOVER"].iterrows():
        m = RE_RECOVER_POS.search(str(r.get("备注") or ""))
        if not m:
            continue
        n = int(m.group(1))
        rows.append({
            "时间": r["时间"], "类型": "接管", "方向": "多" if n > 0 else "空",
            "手数": abs(n), "价格": float("nan"), "合约": "",
            "备注": str(r.get("备注") or ""),
        })

    for _, r in d[d["类型"] == "SYNC"].iterrows():
        note = str(r.get("备注") or "")
        if "清零" in note and "持仓" in note:
            rows.append({
                "时间": r["时间"], "类型": "清零", "方向": "", "手数": 0,
                "价格": float("nan"), "合约": "", "备注": note,
            })

    if not rows:
        return pd.DataFrame(columns=["时间", "类型", "方向", "手数", "价格", "合约", "备注"])
    return pd.DataFrame(rows).sort_values("时间").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 交易配平（尽力而为，带来源标注）
# ---------------------------------------------------------------------------
def _signals(d: pd.DataFrame) -> pd.DataFrame:
    s = d[d["类型"] == "SIGNAL"].copy()
    if s.empty:
        return pd.DataFrame(columns=["时间", "方向", "ATR", "止损", "触发价"])
    s["方向"] = s["备注"].astype(str).str.extract(r"^([多空])")[0]
    s["ATR"] = s["备注"].astype(str).str.extract(RE_ATR)[0].astype(float)
    s["止损"] = s["备注"].astype(str).str.extract(RE_STOP)[0].astype(float)
    s["触发价"] = pd.to_numeric(s["参考价"], errors="coerce")
    return s[["时间", "方向", "ATR", "止损", "触发价"]].sort_values("时间").reset_index(drop=True)


def _stoplosses(d: pd.DataFrame) -> pd.DataFrame:
    s = d[d["类型"] == "STOPLOSS"].copy()
    if s.empty:
        return pd.DataFrame(columns=["时间", "方向", "止损线", "触发价", "原因"])
    s["方向"] = s["方向"].fillna("").astype(str)
    s["止损线"] = s["备注"].astype(str).str.extract(RE_SL)[0].astype(float)
    s["触发价"] = pd.to_numeric(s["参考价"], errors="coerce")
    # ★ 不能简单 extract("时间止损|止损触发")：台账里 `止损触发=False 时间止损=True` 会先命中前者。
    flag = s["备注"].astype(str).str.extract(r"止损触发=(True|False).*?时间止损=(True|False)")
    s["原因"] = [
        ("价格止损" if a == "True" else ("时间止损" if b == "True" else "止损"))
        for a, b in zip(flag[0].fillna(""), flag[1].fillna(""))
    ]
    return s[["时间", "方向", "止损线", "触发价", "原因"]].sort_values("时间").reset_index(drop=True)


def _excursions(d: pd.DataFrame) -> pd.DataFrame:
    s = d[d["类型"] == "EXCURSION"].copy()
    if s.empty:
        return pd.DataFrame(columns=["时间", "MFE", "MAE"])
    ext = s["备注"].astype(str).str.extract(RE_EXC)
    s["MFE"] = pd.to_numeric(ext[0], errors="coerce")
    s["MAE"] = pd.to_numeric(ext[1], errors="coerce")
    return s[["时间", "MFE", "MAE"]].dropna(subset=["MFE", "MAE"]).sort_values("时间").reset_index(drop=True)


def _nearest(df: pd.DataFrame, t, tol_min: float = 3.0):
    """在 df 里找时间最接近 t 的一行（≤ tol_min 分钟）；找不到返回 None。"""
    if df is None or df.empty or pd.isna(t):
        return None
    dt = (df["时间"] - t).abs()
    i = dt.idxmin()
    if dt.loc[i] <= pd.Timedelta(minutes=tol_min):
        return df.loc[i]
    return None


def load_trades(sym_cn: str) -> pd.DataFrame:
    """把成交流水还原成每笔交易。

    列：品种 / 方向 / 手数 / 开仓时间 / 开仓价 / 平仓时间 / 平仓价 / 止损 / ATR
        MFE / MAE / 盈亏 / 原因 / 配对 / 持仓分钟
    `配对` ∈ {FILL平, 反转, 接管, SYNC清零, 未平仓}
    `盈亏` = (平−开)×方向×手数×乘数（**毛利，未扣手续费**）
    """
    key = data.CN2KEY.get(sym_cn, sym_cn.upper())
    mult = data.MULT.get(key, 1)

    d = load_flow(sym_cn)
    ev = load_events(sym_cn)
    sig = _signals(d)
    sls = _stoplosses(d)
    exc = _excursions(d)

    # 状态：lots = [{dir, qty, price, time, stop, atr, src}]
    lots: list[dict] = []
    trades: list[dict] = []

    def _push_trade(lot, qty, exit_time, exit_price, reason, src, sl_row=None):
        dir_sign = lot["dir"]
        pnl = float("nan")
        if pd.notna(lot["price"]) and pd.notna(exit_price):
            pnl = (exit_price - lot["price"]) * dir_sign * qty * mult
        ex = _nearest(exc, exit_time)
        reason2 = reason
        # ★ 两套止损：
        #   `计划止损` = 开仓 SIGNAL 里写的 stop≈（**开仓时已知**，回放只能用这个）
        #   `止损`     = 出场 STOPLOSS 行里的止损线（**出场时才有**，只能用于事后详情）
        planned_stop = lot.get("stop", float("nan"))
        actual_stop = planned_stop
        if sl_row is not None:
            if pd.notna(sl_row.get("止损线")):
                actual_stop = float(sl_row["止损线"])
            if pd.notna(sl_row.get("原因")):
                reason2 = str(sl_row["原因"])
        trades.append({
            "品种": sym_cn,
            "方向": "多" if dir_sign > 0 else "空",
            "手数": int(qty),
            "开仓时间": lot["time"],
            "开仓价": lot["price"],
            "平仓时间": exit_time,
            "平仓价": exit_price,
            "止损": actual_stop,
            "计划止损": planned_stop,
            "ATR": lot.get("atr", float("nan")),
            "MFE": float(ex["MFE"]) if ex is not None else float("nan"),
            "MAE": float(ex["MAE"]) if ex is not None else float("nan"),
            "盈亏": pnl,
            "原因": reason2,
            "配对": src,
            "持仓分钟": (exit_time - lot["time"]).total_seconds() / 60.0
                        if pd.notna(exit_time) and pd.notna(lot["time"]) else float("nan"),
        })

    def _close_matching(exit_time, exit_price, side_dir, qty, reason, src, sl_row=None):
        """平掉与 side_dir 同向的 lots（FIFO），最多 qty 手。"""
        remain = int(qty)
        while remain > 0 and lots and lots[0]["dir"] == side_dir:
            lot = lots[0]
            take = min(remain, lot["qty"])
            # 止损行只挂到**最后平掉的那一笔**（避免一笔止损挂多条）
            use_sl = sl_row if take == lot["qty"] else None
            _push_trade(lot, take, exit_time, exit_price, reason, src, use_sl)
            lot["qty"] -= take
            remain -= take
            if lot["qty"] <= 0:
                lots.pop(0)
        return remain

    for _, e in ev.iterrows():
        t, typ = e["时间"], e["类型"]

        if typ == "接管":
            # 重启接管：先清空本地（进程重启后原内存态已丢），再按云端持仓建 lot
            lots = [{"dir": 1 if e["方向"] == "多" else -1, "qty": int(e["手数"]),
                     "price": float("nan"), "time": t,
                     "stop": _parse_recover_stop(e.get("备注")), "atr": float("nan"),
                     "src": "接管"}]
            continue

        if typ == "清零":
            for lot in list(lots):
                _push_trade(lot, lot["qty"], t, float("nan"), "同步清零", "SYNC清零")
            lots = []
            continue

        if typ not in ("开", "平"):
            continue

        side_dir = 1 if e["方向"] == "多" else -1
        price, qty = float(e["价格"]), int(e["手数"])

        if typ == "开":
            # 反转：持反向仓时直接开 → 隐性平掉旧仓
            if lots and lots[0]["dir"] == -side_dir:
                for lot in list(lots):
                    _push_trade(lot, lot["qty"], t, price, "反转平仓", "反转")
                lots = []
            # 若同向已有 lots，这是加仓：为保持 FIFO 简单，合并进最后一笔的均价
            if lots and lots[0]["dir"] == side_dir:
                lot = lots[-1]
                total = lot["qty"] + qty
                if pd.notna(lot["price"]) and pd.notna(price):
                    lot["price"] = (lot["price"] * lot["qty"] + price * qty) / total
                lot["qty"] = total
            else:
                srow = _nearest(sig, t, tol_min=1.5)
                lots.append({
                    "dir": side_dir, "qty": qty, "price": price, "time": t,
                    "stop": float(srow["止损"]) if srow is not None and pd.notna(srow["止损"]) else float("nan"),
                    "atr": float(srow["ATR"]) if srow is not None and pd.notna(srow["ATR"]) else float("nan"),
                    "src": "FILL",
                })
        else:  # 平
            sl_row = _nearest(sls, t, tol_min=2.0)
            _close_matching(t, price, side_dir, qty, "主动平仓", "FILL平", sl_row)

    # 收尾：仍未平的 lots
    for lot in lots:
        _push_trade(lot, lot["qty"], pd.NaT, float("nan"), "未平仓", "未平仓")

    if not trades:
        return pd.DataFrame(columns=["品种", "方向", "手数", "开仓时间", "开仓价", "平仓时间",
                                     "平仓价", "止损", "计划止损", "ATR", "MFE", "MAE", "盈亏",
                                     "原因", "配对", "持仓分钟"])
    out = pd.DataFrame(trades).sort_values("开仓时间").reset_index(drop=True)
    return out


def _parse_recover_stop(note) -> float:
    if note is None or pd.isna(note):
        return float("nan")
    m = RE_RECOVER_STOP.search(str(note))
    return float(m.group(1)) if m else float("nan")


# ---------------------------------------------------------------------------
# 策略状态（B：每笔开仓时的指标快照）
# ---------------------------------------------------------------------------
def state_at(kline_bj: pd.DataFrame, t) -> dict:
    """给定 K 线（**显示时间索引**，含 ma/slope/atr 等）与时刻 t，返回该根的状态快照。

    只取 **t 所在或之前最近的已收盘 bar** ⇒ 无未来函数。
    列名兼容两套：`ma|ma13`、`slope|slope13_5`、`atr|atr14`。
    """
    if kline_bj is None or kline_bj.empty or pd.isna(t):
        return {}
    i = int(kline_bj["datetime"].searchsorted(pd.Timestamp(t), side="right") - 1)
    if i < 0:
        return {}
    r = kline_bj.iloc[i]

    def _get(*names):
        for n in names:
            if n in r.index and pd.notna(r[n]):
                return float(r[n])
        return float("nan")

    return {
        "bar_time": r["datetime"],
        "close": float(r["close"]),
        "ma": _get("ma", "ma13"),
        "slope": _get("slope", "slope13_5"),
        "atr": _get("atr", "atr14"),
        "atr_dist": _get("atr_dist"),
    }


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def _selftest() -> int:
    ok = True
    print("=" * 72)
    for cn in data.CN2KEY:
        ev = load_events(cn)
        tr = load_trades(cn)
        n_open = int((ev["类型"] == "开").sum()) if not ev.empty else 0
        n_close = int((ev["类型"] == "平").sum()) if not ev.empty else 0
        n_open_trade = int((tr["配对"] != "未平仓").sum()) if not tr.empty else 0
        n_left = int((tr["配对"] == "未平仓").sum()) if not tr.empty else 0
        print(f"[{cn:>2}] FILL开={n_open:>2} FILL平={n_close:>2} 事件={len(ev):>2} "
              f"→ 已平交易={n_open_trade:>2} 未平={n_left}")
        if not tr.empty:
            sample = tr.head(2)[["方向", "手数", "开仓时间", "开仓价", "平仓价", "止损", "MFE", "MAE", "盈亏", "配对"]]
            print(sample.to_string(index=False))
            print("-" * 72)
        # 硬校验：平仓事件数应 ≤ 开仓事件数（不含接管/清零）
        if n_close > n_open + 50:
            ok = False
    print("=" * 72)
    print("交易解析自检：" + ("通过 ✅" if ok else "异常 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
