# -*- coding: utf-8 -*-
"""
成交质量监控 · 滑点实测（#20，D2 2026-10-21 拍板「先纯记录、不告警」）
==========================================================
【为什么需要】
现状只有 `台账统计.py` 的**事后估算**：
    `净盈亏估计 = 毛 − 手数×2×(滑点×一跳×乘数 + 佣金)`
它用的是 `config_symbols` 里的**假设成本表**，不是真实成交。
⇒ 若实际成交比假设差 3 跳，策略在实盘就是负期望，而现在**发现不了**。
模拟盘的核心价值（测真实成本）被浪费。

【D2 拍死的口径（不得自行改动）】
  20.1 基准 = **信号那根 K 线的收盘价**（与回测口径一致，且已收盘 bar 不引入未来函数）。
       ⚠️ 注意：既有 `FILLX` 行里的 `滑点≈X元` 用的是 `ref_price`，
         而 `ref_price = _lp`（**下单那一刻的 last_price**），**不是**信号根收盘价。
          本模块因此必须显式接收 `sig_close`，不能拿 FILLX 的数字当本项数据。
  20.2 **只记录、不告警**。先跑 2 周拿分布，再由 D 定阈值。
       禁止在拿到分布前预设任何滑点阈值（同 #19「先测后定」）。

【与 P0-1 重报的边界（D2 §6-20.4）】
  本模块**只由「真实成交确认」路径调用（FILLX 且持仓已变）**，
  未成交/部分成交/在途委托的轨迹归 P0-1（`live_resend` + 订单生命周期）管，
  这里**不重复记账**。同一 `order_key` 只落一行（幂等）。

【字段】
  时间 / 交易日 / 品种 / 合约 / 方向 / 开平 / 手数 /
  信号根收盘 / 成交均价 / 滑点跳数 / 滑点点数 / 滑点金额 / 标记 / 订单key
  · 滑点正值 = **不利**（买得更贵 / 卖得更便宜）；负值 = 有利。
  · 标记位（flags）：涨停 / 跌停 / 换月 / 重报 / 首日 / 均价缺失 …（供统计时剔除）

【自检】
    python -c "from lib import slippage as s; s.selftest()"
"""
import csv
import os
import time as _time

try:
    from . import live_resend as _rs
except ImportError:
    import live_resend as _rs             # noqa: F401

HEADER = ["时间", "交易日", "品种", "合约", "方向", "开平", "手数",
          "信号根收盘", "成交均价", "滑点跳数", "滑点点数", "滑点金额",
          "标记", "订单key"]

_seen = {}          # name -> set(order_key)，进程内幂等


def path_of(record_dir, name):
    return os.path.join(record_dir, f"{name}_滑点.csv")


def _trade_day():
    try:
        return str(_rs.trading_day())
    except Exception:
        return _time.strftime("%Y%m%d")


def _existing_keys(p):
    """读已有订单 key（重启后仍幂等）。文件不存在返回空集。"""
    out = set()
    try:
        if not os.path.isfile(p):
            return out
        with open(p, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                k = (row.get("订单key") or "").strip()
                if k:
                    out.add(k)
    except Exception:
        pass
    return out


def record(record_dir, name, order_key, symbol, direction, side, hands,
           sig_close, fill_avg, tick, mult, flags="", ts=None, day=None):
    """落一行滑点。**永不抛异常**，返回 (ok: bool, note: str)。

    sig_close : 信号那根已收盘 K 线的收盘价（D2 20.1）
    fill_avg  : 云端真实成交均价
    direction : "多" / "空"（决定不利方向的符号）
    side      : "开" / "平"
    """
    try:
        if not record_dir:
            return False, "无 record_dir"
        if not order_key:
            return False, "无订单key（无法幂等）"
        okey = str(order_key)
        p = path_of(record_dir, name)

        s_set = _seen.setdefault(name, None)
        if s_set is None:
            s_set = _existing_keys(p)
            _seen[name] = s_set
        if okey in s_set:
            return False, "重复（同订单已记）"

        try:
            sc = float(sig_close)
            fa = float(fill_avg)
        except Exception:
            return False, "价格缺失"
        if not (sc > 0 and fa > 0):
            return False, "价格非正"

        try:
            tk = float(tick or 0)
            mt = float(mult or 0)
        except Exception:
            tk, mt = 0.0, 0.0

        # ★ 符号由「这笔单是买还是卖」决定，不是由持仓方向决定。
        #   买（多开 / 空平）：成交价高于基准 = 不利 ⇒ +1
        #   卖（空开 / 多平）：成交价低于基准 = 不利 ⇒ -1
        #   写成 (fa-sc)*方向符号 会把「多平」的不利记成有利（实测踩过）。
        is_buy = (str(direction) == "多") == (str(side) == "开")
        sign = 1.0 if is_buy else -1.0
        pts = (fa - sc) * sign                     # 正 = 不利
        ticks = (pts / tk) if tk > 0 else 0.0
        yuan = pts * (mt or 1.0) * float(hands or 0)

        os.makedirs(record_dir, exist_ok=True)
        new = not os.path.isfile(p)
        with open(p, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(HEADER)
            w.writerow([
                ts or _time.strftime("%Y-%m-%d %H:%M:%S"),
                day or _trade_day(),
                name, symbol, direction, side, int(hands or 0),
                f"{sc:.4f}", f"{fa:.4f}",
                f"{ticks:.2f}", f"{pts:.4f}", f"{yuan:.2f}",
                flags or "", okey,
            ])
        s_set.add(okey)
        return True, "ok"
    except Exception as e:                          # noqa: BLE001
        try:
            print(f"[滑点] 写入失败（已忽略，不影响交易）：{type(e).__name__}: {e}",
                  flush=True)
        except Exception:
            pass
        return False, f"{type(e).__name__}: {e}"


def read(record_dir, name):
    """读回该品种全部滑点行。失败返回 []。"""
    out = []
    try:
        p = path_of(record_dir, name)
        if not os.path.isfile(p):
            return out
        with open(p, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                out.append(row)
    except Exception:
        return []
    return out


def summary(record_dir, name, exclude_flags=("涨停", "跌停", "换月")):
    """按品种汇总（供 #19/#20 后续统计用）：笔数/均值跳数/总额。不告警。"""
    rows = read(record_dir, name)
    keep = []
    for r in rows:
        fl = (r.get("标记") or "")
        if any(x in fl for x in exclude_flags):
            continue
        keep.append(r)
    n = len(keep)
    if not n:
        return {"n": 0, "mean_ticks": 0.0, "total_yuan": 0.0}
    try:
        ticks = sum(float(r.get("滑点跳数") or 0) for r in keep)
        yuan = sum(float(r.get("滑点金额") or 0) for r in keep)
    except Exception:
        return {"n": n, "mean_ticks": 0.0, "total_yuan": 0.0}
    return {"n": n, "mean_ticks": ticks / n, "total_yuan": yuan}


def selftest():
    import tempfile
    d = tempfile.mkdtemp(prefix="slip_")
    _seen.clear()

    # 1 买入（多开）成交比信号收盘贵 3 点 ⇒ 不利，滑点为正
    ok, note = record(d, "豆粕", "K1", "DCE.m2701", "多", "开", 8,
                      sig_close=3200.0, fill_avg=3203.0, tick=1.0, mult=10,
                      day="20261021")
    assert ok, note
    r = read(d, "豆粕")[0]
    assert abs(float(r["滑点点数"]) - 3.0) < 1e-9, r
    assert abs(float(r["滑点跳数"]) - 3.0) < 1e-9, r
    assert abs(float(r["滑点金额"]) - 240.0) < 1e-6, r        # 3*10*8
    print("  1 多开不利方向为正 OK")

    # 2 卖出（多平）成交比信号收盘低 2 点 ⇒ 不利，仍为正
    ok, _ = record(d, "豆粕", "K2", "DCE.m2701", "多", "平", 8,
                   sig_close=3250.0, fill_avg=3248.0, tick=1.0, mult=10,
                   day="20261021")
    assert ok
    r = read(d, "豆粕")[-1]
    assert abs(float(r["滑点点数"]) - 2.0) < 1e-9, r
    print("  2 多平不利方向为正 OK")

    # 3 空头方向：卖出成交价比信号高 = 有利（负）
    ok, _ = record(d, "纯碱", "K3", "CZCE.SA701", "空", "开", 8,
                   sig_close=950.0, fill_avg=953.0, tick=1.0, mult=20,
                   day="20261021")
    assert ok
    r = read(d, "纯碱")[0]
    assert float(r["滑点点数"]) < 0, r          # 卖得更高 = 有利
    assert abs(float(r["滑点金额"]) + 480.0) < 1e-6, r
    print("  3 空头有利方向为负 OK")

    # 4 幂等：同 order_key 不重复记
    ok, note = record(d, "豆粕", "K1", "DCE.m2701", "多", "开", 8,
                      sig_close=3200.0, fill_avg=3203.0, tick=1.0, mult=10,
                      day="20261021")
    assert ok is False and "重复" in note, (ok, note)
    assert len(read(d, "豆粕")) == 2
    print("  4 同订单幂等 OK")

    # 5 重启后仍幂等（清进程内缓存，靠文件去重）
    _seen.clear()
    ok, note = record(d, "豆粕", "K1", "DCE.m2701", "多", "开", 8,
                      sig_close=3200.0, fill_avg=3203.0, tick=1.0, mult=10,
                      day="20261021")
    assert ok is False, "重启后必须仍能识别已记订单"
    print("  5 重启后幂等 OK")

    # 6 标记位可写入并可被 summary 剔除
    record(d, "豆粕", "K5", "DCE.m2701", "多", "开", 8,
           sig_close=3200.0, fill_avg=3300.0, tick=1.0, mult=10, flags="涨停",
           day="20261021")
    sm = summary(d, "豆粕")
    assert sm["n"] == 2, sm          # K1/K2 计入，K5(涨停) 剔除
    print("  6 标记位剔除 OK")

    # 7 异常不抛
    assert record(d, "x", "K9", "y", "多", "开", 1, 0, 100, 1, 10)[0] is False
    assert record(d, "x", "K9", "y", "多", "开", 1, 100, 100, 0, 0)[0] is True
    assert record(None, "x", "K9", "y", "多", "开", 1, 100, 100, 1, 10)[0] is False
    print("  7 异常输入不抛 OK")

    # 8 tick 为 0 时不产生 inf/nan
    r = read(d, "x")[0]
    assert float(r["滑点跳数"]) == 0.0, r
    print("  8 tick=0 不产生 inf OK")

    print("[selftest] 全部通过")


if __name__ == "__main__":
    selftest()
