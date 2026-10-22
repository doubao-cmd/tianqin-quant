# -*- coding: utf-8 -*-
"""
期货交易日历（#24，D2 2026-10-21 拍板「采纳，必须新建」）
==========================================================
【为什么必须新建】
`lib/live_resend.py:49-60` 的 `trading_day()` 只做两件事：
  「≥20:00 归到次日」+「跳过周六周日」，**第 53 行自证不含法定节假日**。
⇒ 它回答不了「下一交易日在几天后」，于是 #22-B「长假收盘强平」在当前代码下
  **物理上不可实现**。本模块是 #22-B 的强制前置。

【与 trading_day() 的分工（不许混用）】
  · `live_resend.trading_day()` = **会话边界映射器**（把 20:00 后的时刻归到次日），
    保留不动，`daily_loss.py` 等仍在复用。
  · 本模块 = **日历**（哪一天开市、下一交易日隔几天、是不是长假前最后交易日）。
  · 两者的「交易日编号」必须一致（selftest 第 7 条交叉验证）。

【数据源与准确边界（★ 不许凭印象改日期）】
  主选 = 提交式本地静态表（D2 §8-④），零外部依赖、可离线测、不挂网络。
  2026 年数据来源：国务院办公厅《关于2026年部分节假日安排的通知》
                  （2025-11-04 发布），已核对北京市政府网转载原文。
  2027 年：**国务院通常在前一年 11 月才发布**，截至本文件编写（2026-10-21）
          尚未发布 ⇒ 本表**只覆盖到 2027-12-31 之后的空缺，不编造**。
          超出覆盖期时 `is_covered()` 返回 False，调用方必须告警交人工
          （D2 §8-④：绝不静默漏判）。

【一个容易搞错的点（已核实，别"修正"回去）】
  调休上班的**周六/周日，期货市场不开市**。证券期货市场不因补班开市。
  例：2026-02-14(周六)、2026-02-28(周六)、2026-10-10(周六) 国务院要求上班，
      但期货**没有交易**。所以本模块只认「周一~周五 且 非法定假日」，
      **不把调休上班日当作交易日**。

【长假定义（D2 §8-③ 拍板）】
  `gap_days(最后交易日) = 该日之后、下一交易日之前的连续非交易日天数`。
  gap ≥ 5 ⇒ 长假前最后交易日（春节 10 天、国庆 7 天、劳动节 5 天）。
  gap = 3（清明/端午/中秋）与 gap = 4（元旦）⇒ **不算长假**。

【自检】
    python -c "from lib import trade_calendar as tc; tc.selftest()"
"""
import datetime as _dt

# ===================== 常量（改动即改变长假判定）=====================
LONG_HOLIDAY_MIN_GAP = 5      # D2 §8-③：≥5 个连续非交易日才算长假（3天清明等不算）

# ---- 法定节假日休市区间（含其中的周末）。数据源见文件头。——
# 只列**交易所休市**的日期区间；调休上班日不列（期货不开市，见文件头说明）。
HOLIDAYS = {
    # 2026：国务院办公厅 2025-11-04 发布
    "元旦":   [("2026-01-01", "2026-01-03")],
    "春节":   [("2026-02-15", "2026-02-23")],
    "清明节": [("2026-04-04", "2026-04-06")],
    "劳动节": [("2026-05-01", "2026-05-05")],
    "端午节": [("2026-06-19", "2026-06-21")],
    "中秋节": [("2026-09-25", "2026-09-27")],
    "国庆节": [("2026-10-01", "2026-10-07")],
}

# 覆盖期：本表能可靠回答「下一交易日」的最后一天。
# ★ 2027 年安排未发布 ⇒ 不能假装覆盖。到 2027 年前必须补表（否则告警交人工）。
COVERAGE_UNTIL = _dt.date(2026, 12, 31)
SOURCE = ("国务院办公厅《关于2026年部分节假日安排的通知》2025-11-04；"
          "2027 年安排未发布，表止于 2026-12-31")

_cache = {}          # "YYYY-MM-DD" -> bool（是否休市）


def _d(x):
    """接受 date / datetime / 'YYYY-MM-DD' / 'YYYYMMDD'。失败抛 ValueError。"""
    if isinstance(x, _dt.datetime):
        return x.date()
    if isinstance(x, _dt.date):
        return x
    s = str(x).strip().replace("/", "-")
    if len(s) == 8 and s.isdigit():
        return _dt.date(int(s[:4]), int(s[4:6]), int(s[6:]))
    return _dt.date.fromisoformat(s[:10])


def _holiday_ranges():
    out = []
    for name, spans in HOLIDAYS.items():
        for a, b in spans:
            out.append((_d(a), _d(b), name))
    return out


def holiday_name(d):
    """d 属于哪个节假日休市区间；不在假期返回 None。"""
    try:
        dd = _d(d)
    except Exception:
        return None
    for a, b, nm in _holiday_ranges():
        if a <= dd <= b:
            return nm
    return None


def is_covered(d):
    """本表能否可靠回答 d 的日历问题（超出覆盖期 ⇒ False，调用方须告警）。"""
    try:
        return _d(d) <= COVERAGE_UNTIL
    except Exception:
        return False


def is_trading_day(d):
    """交易日 = 周一~周五 且 非法定假日。

    ⚠️ 调休上班的周末**不算交易日**（见文件头），这是本模块最容易被"修正"错的地方。
    """
    key = None
    try:
        dd = _d(d)
        key = dd.isoformat()
        if key in _cache:
            return _cache[key]
        ok = (dd.weekday() < 5) and (holiday_name(dd) is None)
        _cache[key] = ok
        return ok
    except Exception:
        return False


def next_trading_day(d, limit=40):
    """d **之后**的第一个交易日（不含 d 本身）。找不到返回 None。"""
    try:
        dd = _d(d)
    except Exception:
        return None
    for i in range(1, int(limit) + 1):
        x = dd + _dt.timedelta(days=i)
        if is_trading_day(x):
            return x
    return None


def prev_trading_day(d, limit=40):
    """d **之前**的最后一个交易日（不含 d 本身）。找不到返回 None。"""
    try:
        dd = _d(d)
    except Exception:
        return None
    for i in range(1, int(limit) + 1):
        x = dd - _dt.timedelta(days=i)
        if is_trading_day(x):
            return x
    return None


def gap_days(d):
    """d 之后、下一交易日之前的**连续非交易日天数**（D2 拍板的长假判据）。

    例：2026-02-13(五) 之后下一交易日是 02-24(二) ⇒ 2/14~2/23 共 10 天。
        2026-04-03(五) 之后下一交易日是 04-07(二) ⇒ 4/4~4/6 共 3 天。
    返回 (gap:int, next_day:date|None)；无法判定返回 (-1, None)。
    """
    try:
        dd = _d(d)
    except Exception:
        return -1, None
    nxt = next_trading_day(dd)
    if nxt is None:
        return -1, None
    return (nxt - dd).days - 1, nxt


def is_pre_long_holiday(d, min_gap=None):
    """d 是否为**长假前的最后一个交易日**。

    返回 (bool, gap, 假期名, 下一交易日)。非交易日传入 ⇒ (False, ...)。
    """
    mg = LONG_HOLIDAY_MIN_GAP if min_gap is None else int(min_gap)
    try:
        dd = _d(d)
    except Exception:
        return False, -1, None, None
    if not is_trading_day(dd):
        return False, -1, None, None
    gap, nxt = gap_days(dd)
    if gap < 0:
        return False, gap, None, None
    if gap < mg:
        return False, gap, None, nxt
    # 假期名取区间内第一个命中的节假日
    nm = None
    for i in range(1, gap + 1):
        nm = holiday_name(dd + _dt.timedelta(days=i))
        if nm:
            break
    return True, gap, nm, nxt


def coverage_warning(d=None):
    """超出覆盖期时的告警文本；在覆盖期内返回 ""。

    D2 §8-④：日历缺失必须**最高级告警交人工**，绝不静默漏判。
    """
    try:
        dd = _d(d) if d is not None else _dt.date.today()
    except Exception:
        return "交易日历：日期解析失败，无法判定长假 ⇒ 请人工确认"
    if dd <= COVERAGE_UNTIL:
        return ""
    return (f"交易日历：{dd} 超出覆盖期（表止于 {COVERAGE_UNTIL}）⇒ "
            f"**无法判定长假/下一交易日，请人工确认**。数据源：{SOURCE}")


# ---------------------------------------------------------------- 自检
def selftest():
    # 1 普通工作日
    assert is_trading_day("2026-10-21") is True          # 周三
    print("  1 普通工作日 OK")

    # 2 ★调休上班的周末不算交易日（最易搞错，必须锁死）
    for s in ("2026-02-14", "2026-02-28", "2026-10-10", "2026-01-04", "2026-05-09"):
        assert _d(s).weekday() >= 5, f"{s} 应是周末"
        assert is_trading_day(s) is False, f"{s} 国务院要求上班，但期货不开市"
    print("  2 调休周末不开市 OK")

    # 3 法定假日休市（含落在工作日的）
    assert is_trading_day("2026-10-01") is False
    assert is_trading_day("2026-02-16") is False         # 周一，春节
    assert is_trading_day("2026-05-04") is False         # 周一，劳动节
    print("  3 法定假日休市 OK")

    # 4 ★长假判定（D2 §8-③：≥5 天才算）
    cases = [
        ("2026-02-13", True, 10, "春节"),      # 2/14~2/23 共 10 天
        ("2026-09-30", True, 7, "国庆节"),      # 10/1~10/7 共 7 天
        ("2026-04-30", True, 5, "劳动节"),      # 5/1~5/5 共 5 天
        ("2026-04-03", False, 3, "清明节"),     # 3 天不算长假
        ("2026-06-18", False, 3, "端午节"),
        ("2026-09-24", False, 3, "中秋节"),
        ("2025-12-31", False, 4, "元旦"),       # 4 天不算
        ("2026-10-21", False, 0, None),        # 普通周三
    ]
    for s, want, gap_want, _nm in cases:
        ok, gap, nm, nxt = is_pre_long_holiday(s)
        assert ok is want, f"{s} 长假判定={ok} 期望={want}"
        assert gap == gap_want, f"{s} gap={gap} 期望={gap_want}"
        print(f"    {s}: 长假={ok} gap={gap} 假期={nm} 下一交易日={nxt}")
    print("  4 长假判定 OK")

    # 5 非交易日传入不误判为"长假前最后交易日"
    ok, gap, _, _ = is_pre_long_holiday("2026-02-16")
    assert ok is False and gap == -1, (ok, gap)
    print("  5 非交易日不误判 OK")

    # 6 覆盖期与告警
    assert is_covered("2026-12-31") is True
    assert is_covered("2027-01-04") is False
    assert coverage_warning("2026-10-21") == ""
    assert "超出覆盖期" in coverage_warning("2027-01-04")
    print("  6 覆盖期告警 OK")

    # 7 ★与 live_resend.trading_day() 的关系（禁止第二份交易日历的漂移）
    try:
        from . import live_resend as _rs
    except ImportError:
        import live_resend as _rs
    # 7a 普通日：两者对「今天是不是交易日」结论一致
    for ts in ("2026-10-21 10:30:00", "2026-10-21 15:30:00"):
        dtm = _dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        td = str(_rs.trading_day(dtm))
        assert td == "20261021", td
        assert is_trading_day(td) is True, td
    # 7b ★已知限制（不是 bug，是分工）：trading_day 无节假日表，
    #    节前夜盘会把交易日算到假期里。本模块**不负责**纠正它（那是会话边界映射），
    #    但必须把差异**显式记录**下来，防止有人以为两套日历等价。
    dtm = _dt.datetime(2026, 9, 30, 21, 30)        # 国庆前最后一日的夜盘时刻
    td = str(_rs.trading_day(dtm))
    assert is_trading_day(td) is False, (td, "trading_day 在节前夜盘给出休市日，属已知限制")
    assert is_trading_day("2026-09-30") is True
    print(f"    已知限制：trading_day(2026-09-30 21:30)={td} 是休市日，"
          f"本模块判 is_trading_day=False ⇒ 两套语义分工不同，不可互换")
    print("  7 与 trading_day 分工明确 OK")

    # 8 跨年/极端输入不炸
    assert next_trading_day("2026-12-31") is not None
    assert is_trading_day("乱码") is False
    assert prev_trading_day("2026-01-05").isoformat() == "2026-01-02"
    print("  8 边界输入 OK")

    print("[selftest] 全部通过")


if __name__ == "__main__":
    selftest()
