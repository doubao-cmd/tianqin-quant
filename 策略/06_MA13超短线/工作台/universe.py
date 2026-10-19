# -*- coding: utf-8 -*-
"""
universe.py —— 品种池 + 交易时段工具（纯逻辑，不依赖 tqsdk / 网络）

职责：
  1. MAIN_CONTINUOUS：约 50 个商品期货主连的品种池（短码 = config_symbols 的 key）。
  2. session 工具：给定时间，判定属于「早盘 / 午盘 / 夜盘 / 休市」，
     并生成 session_id、计算"下一个该被封的 session"。

设计约束（对应用户 2026-10-17 口径）：
  - 夜盘 = 21:00 ~ 23:00（用户定义窗口，不取真实收盘延展）。
  - 早盘 = 09:00 ~ 11:30；午盘 = 13:30 ~ 15:00。
  - 无夜盘的品种（has_night=False）走 "早→午→次日早" 链，跳过夜盘。
  - 所有函数只用「已发生时刻」，无未来函数。
"""
from __future__ import annotations
import datetime as _dt

# ----------------------------------------------------------------------------
# 1. 品种池（约 50 个商品主连；金融期货 CFFEX 不在池内）
#    字段：key=短码, exch=交易所, name=中文, night=是否有夜盘
# ----------------------------------------------------------------------------
MAIN_CONTINUOUS = [
    # 大商所 DCE
    dict(key="M",  exch="DCE", name="豆粕",   night=True),
    dict(key="Y",  exch="DCE", name="豆油",   night=True),
    dict(key="P",  exch="DCE", name="棕榈油", night=True),
    dict(key="C",  exch="DCE", name="玉米",   night=False),
    dict(key="CS", exch="DCE", name="玉米淀粉", night=False),
    dict(key="A",  exch="DCE", name="豆一",   night=False),
    dict(key="B",  exch="DCE", name="豆二",   night=False),
    dict(key="JD", exch="DCE", name="鸡蛋",   night=False),
    dict(key="JM", exch="DCE", name="焦煤",   night=True),
    dict(key="J",  exch="DCE", name="焦炭",   night=True),
    dict(key="I",  exch="DCE", name="铁矿石", night=True),
    dict(key="LH", exch="DCE", name="生猪",   night=False),
    dict(key="EG", exch="DCE", name="乙二醇", night=True),
    dict(key="EB", exch="DCE", name="苯乙烯", night=True),
    dict(key="PG", exch="DCE", name="液化石油气", night=True),
    dict(key="PP", exch="DCE", name="聚丙烯", night=True),
    dict(key="V",  exch="DCE", name="聚氯乙烯", night=True),
    dict(key="L",  exch="DCE", name="聚乙烯", night=True),
    dict(key="RR", exch="DCE", name="粳米",   night=False),
    # 上期所 SHFE
    dict(key="RB", exch="SHFE", name="螺纹钢", night=True),
    dict(key="HC", exch="SHFE", name="热轧卷板", night=True),
    dict(key="CU", exch="SHFE", name="铜",     night=True),
    dict(key="AL", exch="SHFE", name="铝",     night=True),
    dict(key="ZN", exch="SHFE", name="锌",     night=True),
    dict(key="PB", exch="SHFE", name="铅",     night=True),
    dict(key="NI", exch="SHFE", name="镍",     night=True),
    dict(key="SN", exch="SHFE", name="锡",     night=True),
    dict(key="AU", exch="SHFE", name="黄金",   night=True),
    dict(key="AG", exch="SHFE", name="白银",   night=True),
    dict(key="RU", exch="SHFE", name="橡胶",   night=True),
    dict(key="FU", exch="SHFE", name="燃油",   night=True),
    dict(key="BU", exch="SHFE", name="沥青",   night=True),
    dict(key="SP", exch="SHFE", name="纸浆",   night=True),
    dict(key="SS", exch="SHFE", name="不锈钢", night=True),
    dict(key="AO", exch="SHFE", name="氧化铝", night=True),
    # 郑商所 CZCE
    dict(key="CF", exch="CZCE", name="棉花",   night=True),
    dict(key="SR", exch="CZCE", name="白糖",   night=True),
    dict(key="RM", exch="CZCE", name="菜粕",   night=True),
    dict(key="OI", exch="CZCE", name="菜油",   night=True),
    dict(key="TA", exch="CZCE", name="PTA",    night=True),
    dict(key="MA", exch="CZCE", name="甲醇",   night=True),
    dict(key="FG", exch="CZCE", name="玻璃",   night=True),
    dict(key="SA", exch="CZCE", name="纯碱",   night=True),
    dict(key="SM", exch="CZCE", name="锰硅",   night=True),
    dict(key="SF", exch="CZCE", name="硅铁",   night=True),
    dict(key="UR", exch="CZCE", name="尿素",   night=True),
    dict(key="PF", exch="CZCE", name="短纤",   night=True),
    dict(key="PK", exch="CZCE", name="花生",   night=True),
    dict(key="AP", exch="CZCE", name="苹果",   night=False),
    dict(key="CJ", exch="CZCE", name="红枣",   night=False),
    # 上海国际能源 INE
    dict(key="SC", exch="INE", name="原油",    night=True),
    dict(key="NR", exch="INE", name="20号胶",  night=True),
    dict(key="LU", exch="INE", name="低硫燃油", night=True),
    dict(key="BC", exch="INE", name="国际铜",  night=True),
    # 广期所 GFEX
    dict(key="SI", exch="GFEX", name="工业硅", night=True),
    dict(key="LC", exch="GFEX", name="碳酸锂", night=True),
    dict(key="PS", exch="GFEX", name="多晶硅", night=True),
]

# ---- 索引 ----
_BY_KEY = {d["key"]: d for d in MAIN_CONTINUOUS}


def get(key: str) -> dict:
    """按短码取品种元数据；返回 None 表示不在池内。"""
    return _BY_KEY.get(key.upper())


def all_keys() -> list[str]:
    return [d["key"] for d in MAIN_CONTINUOUS]


def has_night(key: str) -> bool:
    d = _BY_KEY.get(key.upper())
    return bool(d and d["night"])


# ----------------------------------------------------------------------------
# 2. 时段工具
# ----------------------------------------------------------------------------
# 用户定义：夜盘 21:00–23:00；日盘 早 09:00–11:30 / 午 13:30–15:00
_MORNING = (_dt.time(9, 0),  _dt.time(11, 30))
_AFTERNOON = (_dt.time(13, 30), _dt.time(15, 0))
_NIGHT = (_dt.time(21, 0), _dt.time(23, 0))

SEG_ORDER = ("morning", "afternoon", "night")  # 一日内的顺序


def seg_of(ts) -> str:
    """给定 datetime，返回 'morning' / 'afternoon' / 'night' / 'closed'。"""
    if isinstance(ts, str):
        ts = _parse(ts)
    if ts is None:
        return "closed"
    t = ts.time()
    if _MORNING[0] <= t < _MORNING[1]:
        return "morning"
    if _AFTERNOON[0] <= t < _AFTERNOON[1]:
        return "afternoon"
    if _NIGHT[0] <= t < _NIGHT[1]:
        return "night"
    return "closed"


def session_id(ts) -> str:
    """生成稳定 session_id：'YYYY-MM-DD_seg'。休市返回 'closed'。"""
    if isinstance(ts, str):
        ts = _parse(ts)
    if ts is None:
        return "closed"
    seg = seg_of(ts)
    if seg == "closed":
        return "closed"
    return f"{ts.strftime('%Y-%m-%d')}_{seg}"


def next_session_id(sid: str, key: str = "") -> str:
    """
    给定某 session_id，返回「紧接着的下一个 session」的 id（用于封禁判定）。
    - 早 -> 午
    - 午 -> 夜（若该品种有夜盘）否则 次日早
    - 夜 -> 次日早
    key 用于判断 has_night；省略时按有夜盘处理。
    """
    if sid == "closed" or sid.count("_") < 1:
        return "closed"
    date_str, seg = sid.rsplit("_", 1)
    try:
        d = _dt.datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return "closed"
    if seg == "morning":
        nxt_seg = "afternoon"
        nxt_date = d
    elif seg == "afternoon":
        if key and has_night(key):
            nxt_seg, nxt_date = "night", d
        else:
            nxt_seg, nxt_date = "morning", d + _dt.timedelta(days=1)
    elif seg == "night":
        nxt_seg, nxt_date = "morning", d + _dt.timedelta(days=1)
    else:
        return "closed"
    return f"{nxt_date.strftime('%Y-%m-%d')}_{nxt_seg}"


def _parse(ts) -> _dt.datetime | None:
    if isinstance(ts, _dt.datetime):
        return ts
    try:
        s = str(ts)
        # tqsdk 时间戳形如 '2026-01-05 09:35:00.000001'
        s = s.split(".")[0]
        return _dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


if __name__ == "__main__":
    # 自检
    assert len(MAIN_CONTINUOUS) >= 50, f"品种池应≥50，实际{len(MAIN_CONTINUOUS)}"
    assert len(_BY_KEY) == len(MAIN_CONTINUOUS), "短码重复！"
    print(f"品种池：{len(MAIN_CONTINUOUS)} 个；night={sum(1 for d in MAIN_CONTINUOUS if d['night'])}")
    # 时段
    t = _dt.datetime(2026, 1, 5, 9, 35)
    print("9:35 ->", seg_of(t), session_id(t))
    print("next of morning(RB) ->", next_session_id(session_id(t), "RB"))
    print("next of afternoon(no-night C) ->", next_session_id("2026-01-05_afternoon", "C"))
    print("next of night(EB) ->", next_session_id("2026-01-05_night", "EB"))
