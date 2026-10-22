# -*- coding: utf-8 -*-
"""
长假收盘强平（#22-B，用户 2026-10-21 11:47 显式授权覆盖红线）
==========================================================
【这是全工程**唯一**被授权的自动平仓例外】
红线原文：「不得自动平掉未平持仓」。用户 11:47 明示：
    "长假是五天这种长假，就不留仓位了，五天以及以上的长假可以在收盘最后五分钟强平
     （就是最后一条k线）"
D2 §8-③ 据此拍板，并加了五条安全约束（本模块逐条落实）。

【触发条件（三个都满足才动）】
  1. 当日是**长假前最后一个交易日**：`trade_calendar.gap_days(day) >= 5`
     （春节 10 天 / 国庆 7 天 / 劳动节 5 天；**清明·端午·中秋 3 天不算，元旦 4 天不算**）
  2. 已到当日**最后一条 5min K 线收盘之后**：
     · 正常情形（长假前最后交易日交易所**取消夜盘**）⇒ 日盘 15:00 收盘
     · 若实测当日 15:00 之后仍有 bar（异常/规则变化）⇒ 自适应延后到最后一根 bar 收盘
     · **绝不提前**：宁可晚平，也不在盘中误平
  3. 当前有可平持仓（无仓 ⇒ 不动、不记账）

【五条安全约束（D2 §8-③，缺一不可）】
  ① `--drill-holidayclose` 演练：只打印将平清单，不发任何单、不写"已执行"标记；
  ② **幂等**：同一长假只触发一次（`台账\\长假强平_已执行.json` 记 trade_day）；
  ③ 走**既有平仓路径**，绝不因强平而屏蔽止损/对账等其他平仓；
  ④ 台账 + 告警队列记「长假强平」+ 间隔天数 + 将平品种手数；
  ⑤ **日历缺失/过期 ⇒ 不强行平**，改为最高级告警"长假日历缺失，请人工确认"。

【本模块的职责边界】
  只做**判定**（要不要平、平谁、为什么）。真正的下单在主文件里走既有平仓函数 ——
  避免在 lib 层再造一套下单逻辑（那是 P0-1 重报/三层账要管的事）。

【自检】
    python -c "from lib import holiday_close as h; h.selftest()"
"""
import datetime as _dt
import json
import os
import time as _time

try:
    from . import trade_calendar as _tc
except ImportError:
    import trade_calendar as _tc             # noqa: F401

MIN_GAP = 5                 # D2 §8-③：≥5 个连续非交易日才算"长假"（★强平阈值，非降级阈值 4）
DONE_NAME = "长假强平_已执行.json"
NAME = "长假强平"

# 日盘收盘（长假前最后交易日取消夜盘 ⇒ 这就是最后收盘）
DAY_CLOSE_HM = (15, 0)


# ---------------------------------------------------------------- 幂等
def done_path(record_dir):
    return os.path.join(record_dir, DONE_NAME)


def already_done(record_dir, day):
    """同一交易日（同一长假）是否已强平过。"""
    try:
        p = done_path(record_dir)
        if not os.path.isfile(p):
            return False
        with open(p, "r", encoding="utf-8") as f:
            doc = json.load(f)
        return str(day) in [str(x) for x in (doc.get("days") or [])]
    except Exception:
        return False


def mark_done(record_dir, day, info=None):
    """记录已强平（幂等标记）。失败不影响主流程。"""
    try:
        os.makedirs(record_dir, exist_ok=True)
        p = done_path(record_dir)
        doc = {"days": [], "detail": {}}
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    doc = json.load(f) or doc
            except Exception:
                doc = {"days": [], "detail": {}}
        days = [str(x) for x in (doc.get("days") or [])]
        if str(day) not in days:
            days.append(str(day))
        doc["days"] = days[-60:]                 # 只留最近 60 次，防无限增长
        doc.setdefault("detail", {})[str(day)] = info or {}
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 判定
def _last_bar_end(bars_dt, day, ksec=300):
    """当日最后一根已收盘 K 线的**收盘时刻**。无法判定返回 None。

    bars_dt : 可迭代的 bar 时间（datetime 或 str），tqsdk 的 datetime 是 bar **起始**
    正常情形（长假前无夜盘）：最后一根是 14:55 开始/15:00 结束 ⇒ 返回当日 15:00。
    异常情形（15:00 后仍有 bar）：返回"最后一根开始 + ksec"。
    """
    try:
        la = None
        for x in (bars_dt or []):
            if isinstance(x, str):
                x = _dt.datetime.fromisoformat(x[:19])
            if la is None or x > la:
                la = x
        if la is None:
            return None
        close_at = la + _dt.timedelta(seconds=int(ksec))
        # 若最后一根已在日盘收盘之后（有夜盘），以它为准；否则以 15:00 为准
        nm = _dt.datetime(la.year, la.month, la.day, DAY_CLOSE_HM[0], DAY_CLOSE_HM[1])
        return max(close_at, nm)
    except Exception:
        return None


def decide(day, now, bars_dt=None, positions=None, ksec=300,
           record_dir=None, drill=False):
    """判定此刻要不要强平。返回 dict：

    {act: bool, reason: str, gap: int, holiday: str|None, next_day: date|None,
     legs: [{key, hands, dir}], close_at: datetime|None, warn: str|None}

    `act=True` 只表示"应该平"，**不代表已平**（下单在主文件）。
    `warn` 非空 ⇒ 调用方必须发最高级告警（如日历缺失）。
    """
    out = {"act": False, "reason": "", "gap": -1, "holiday": None,
           "next_day": None, "legs": [], "close_at": None, "warn": None}
    try:
        d = day
        if isinstance(d, str):
            s = d.replace("-", "")
            d = _dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))

        # ⑤ 日历覆盖检查：缺失/过期 ⇒ 不强行平，交人工
        warn = _tc.coverage_warning(d)
        if warn:
            out["warn"] = warn
            out["reason"] = "日历缺失/过期，不做自动强平"
            return out

        is_pre, gap, hol, nxt = _tc.is_pre_long_holiday(d, MIN_GAP)
        out.update({"gap": gap, "holiday": hol, "next_day": nxt})
        if not is_pre:
            out["reason"] = (f"非长假前最后交易日（gap={gap} < {MIN_GAP}）"
                             if gap >= 0 else "非交易日或无法判定")
            return out

        close_at = _last_bar_end(bars_dt, d, ksec)
        out["close_at"] = close_at
        if close_at is None:
            out["reason"] = "K 线未就绪，无法确定最后收盘时刻"
            return out
        try:
            if now < close_at:
                out["reason"] = f"未到最后一条 K 线收盘（{close_at:%H:%M}）"
                return out
        except Exception:
            out["reason"] = "时间无法比较"
            return out

        legs = []
        for p in (positions or []):
            try:
                h = int(p.get("hands") or p.get("pos") or 0)
                if h == 0:
                    continue
                legs.append({"key": str(p.get("key")), "hands": abs(h),
                             "dir": "多" if h > 0 else "空"})
            except Exception:
                continue
        out["legs"] = legs
        if not legs:
            out["reason"] = "无持仓，无需强平"
            return out

        if record_dir and already_done(record_dir, d.strftime("%Y%m%d")):
            out["reason"] = "本长假已执行过（幂等）"
            return out

        out["act"] = True
        out["reason"] = (f"{hol} 长假前最后交易日（gap={gap} 天，"
                         f"下一交易日 {nxt:%Y-%m-%d}）⇒ "
                         f"{'【演练】将' if drill else ''}强平 {len(legs)} 个品种")
        return out
    except Exception as e:                          # noqa: BLE001
        out["reason"] = f"判定异常（按不平处理）：{type(e).__name__}: {e}"
        out["warn"] = f"长假强平判定异常，已放弃本次自动强平：{e}"
        return out


def selftest():
    import tempfile
    d = tempfile.mkdtemp(prefix="hol_")
    bars_ok = [_dt.datetime(2026, 9, 30, 14, 50), _dt.datetime(2026, 9, 30, 14, 55)]
    pos = [{"key": "M", "hands": 8},
           {"key": "SA", "hands": -8}]

    # 1 非长假日（普通周三）⇒ 不动
    r = decide("20261021", _dt.datetime(2026, 10, 21, 15, 30), bars_ok, pos, record_dir=d)
    assert r["act"] is False and r["gap"] == 0, r
    print("  1 普通日不动 OK")

    # 2 3 天小长假（清明）⇒ 不动（强平阈值是 5，不是 4）
    r = decide("20260403", _dt.datetime(2026, 4, 3, 15, 30),
               [_dt.datetime(2026, 4, 3, 14, 55)], pos, record_dir=d)
    assert r["act"] is False and r["gap"] == 3, r
    print("  2 清明(3天)不强平 OK")

    # 3 ★国庆前 14:00 太早 ⇒ 不动（绝不提前平）
    r = decide("20260930", _dt.datetime(2026, 9, 30, 14, 0), bars_ok, pos, record_dir=d)
    assert r["act"] is False and "未到最后" in r["reason"], r
    print("  3 未到收盘不动 OK")

    # 4 ★国庆前 15:00 之后 ⇒ 强平 2 个品种
    r = decide("20260930", _dt.datetime(2026, 9, 30, 15, 1), bars_ok, pos, record_dir=d)
    assert r["act"] is True and len(r["legs"]) == 2, r
    assert r["gap"] == 7 and r["holiday"] == "国庆节", r
    assert r["close_at"] == _dt.datetime(2026, 9, 30, 15, 0), r["close_at"]
    print("  4 国庆前收盘后强平 OK")

    # 5 无仓 ⇒ 不动
    r = decide("20260930", _dt.datetime(2026, 9, 30, 15, 1), bars_ok, [], record_dir=d)
    assert r["act"] is False and "无持仓" in r["reason"], r
    print("  5 无仓不动 OK")

    # 6 ★幂等：标记后不再触发
    mark_done(d, "20260930", {"gap": 7})
    r = decide("20260930", _dt.datetime(2026, 9, 30, 15, 2), bars_ok, pos, record_dir=d)
    assert r["act"] is False and "幂等" in r["reason"], r
    print("  6 幂等只触发一次 OK")

    # 7 ★日历缺失（超出覆盖期）⇒ 不强行平 + 必须告警
    r = decide("20270105", _dt.datetime(2027, 1, 5, 15, 30), bars_ok, pos, record_dir=d)
    assert r["act"] is False and r["warn"] and "超出覆盖期" in r["warn"], r
    print("  7 日历缺失不平并告警 OK")

    # 8 春节（gap=10）也触发
    r = decide("20260213", _dt.datetime(2026, 2, 13, 15, 1),
               [_dt.datetime(2026, 2, 13, 14, 55)], pos, record_dir=d)
    assert r["act"] is True and r["gap"] == 10 and r["holiday"] == "春节", r
    print("  8 春节(gap=10)强平 OK")

    # 9 ★异常情形：15:00 后仍有 bar（有夜盘）⇒ 收盘时刻自适应到最后一根
    bars_night = [_dt.datetime(2026, 9, 30, 14, 55), _dt.datetime(2026, 9, 30, 22, 55)]
    r = decide("20260930", _dt.datetime(2026, 9, 30, 23, 1), bars_night, pos,
               record_dir=tempfile.mkdtemp(prefix="hol2_"))
    assert r["act"] is True and r["close_at"] == _dt.datetime(2026, 9, 30, 23, 0), r["close_at"]
    r2 = decide("20260930", _dt.datetime(2026, 9, 30, 15, 1), bars_night, pos,
                record_dir=tempfile.mkdtemp(prefix="hol3_"))
    assert r2["act"] is False, "有夜盘时不能 15:00 就平"
    print("  9 夜盘自适应收盘时刻 OK")

    # 10 同一长假在 done 文件里只留一次
    mark_done(d, "20260930", {"gap": 7})
    with open(done_path(d), encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["days"].count("20260930") == 1, doc
    print("  10 幂等标记不重复 OK")

    # 11 坏输入不炸
    r = decide("乱码", _dt.datetime(2026, 10, 21), None, None, record_dir=d)
    assert r["act"] is False and r["reason"], r
    print("  11 坏输入不炸 OK")

    print("[selftest] 全部通过")


if __name__ == "__main__":
    selftest()
