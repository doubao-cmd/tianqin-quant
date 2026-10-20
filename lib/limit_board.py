# -*- coding: utf-8 -*-
"""
涨跌停封板判定（P1-3）—— **纯逻辑，不 import tqsdk**
================================================================================
为什么需要（任务书 §二 P1-3）
--------------------------------------------------------------
封板合约报单必废（涨停时没人卖、跌停时没人买），废单既占额度又制造
「以为平了其实没平」的假象。原实现下单前不看封板。

红线（任务书 §四-2）
--------------------------------------------------------------
**只读交易所 `upper_limit` / `lower_limit` 字段**，不得用行情推断「该封板了」
（"一字板"那种 K 线推断属于复盘研究口径，不是这里）。

方向语义（这是最容易搞反的地方）
--------------------------------------------------------------
  · 涨停（last ≥ upper）⇒ **BUY 不可报**（开多、平空都要买）
                        SELL 可报（开空、平多能成交）
  · 跌停（last ≤ lower）⇒ **SELL 不可报**（开空、平多都要卖）
                        BUY 可报
  注意：不是"封板就一概不报" —— 那样会在涨停时白白错过平多（该平的反而是好价）。

`upper_limit` 可读性
--------------------------------------------------------------
tqsdk 3.10.2 `objs.py:81` 有该字段；`api.py:3203` 明写「回测时 upper_limit/
lower_limit 为 nan」⇒ 实盘行情（TqKq 模拟盘走实盘行情）可读，回测不可读。
本模块对 nan/≤0 一律**视为不可读 ⇒ 不拦**（宁可不拦，也不因字段缺失把交易卡死），
主文件会按腿打印一次可读性自证日志。

跑法（离线单测，纯标准库）：
    python -c "from lib import limit_board; limit_board.selftest()"
"""
import math


def _ok_num(v):
    try:
        f = float(v)
    except Exception:
        return None
    return f if math.isfinite(f) and f > 0 else None


def board_side(last_price, upper_limit, lower_limit, tick=0.0):
    """返回被封板挡住的方向：'BUY' / 'SELL' / None。

    None = 未封板，或上下限不可读（字段缺失/回测环境）。
    """
    lp = _ok_num(last_price)
    up = _ok_num(upper_limit)
    lo = _ok_num(lower_limit)
    if lp is None or (up is None and lo is None):
        return None
    t = _ok_num(tick) or 0.0
    eps = t * 0.5 if t > 0 else 1e-9
    if up is not None and lp >= up - eps:
        return "BUY"
    if lo is not None and lp <= lo + eps:
        return "SELL"
    return None


def check(direction, last_price, upper_limit, lower_limit, tick=0.0):
    """下单前检查。返回 (blocked: bool, note: str)。

    direction: 'BUY' / 'SELL'（大小写不敏感）
    """
    side = str(direction or "").upper()
    b = board_side(last_price, upper_limit, lower_limit, tick=tick)
    if b is None:
        return False, ""
    if side == b:
        kind = "涨停" if b == "BUY" else "跌停"
        return True, (f"{kind}封板（last={float(last_price):.4g} "
                      f"upper={float(upper_limit):.4g} lower={float(lower_limit):.4g}）"
                      f" ⇒ {side} 方向不报单")
    return False, ""


def readable(upper_limit, lower_limit):
    """该合约的涨跌停价是否可读（供主文件打印一次性自证日志）。"""
    return _ok_num(upper_limit) is not None or _ok_num(lower_limit) is not None


# ---------------------------------------------------------------- 自检
def selftest():
    print("[selftest] limit_board.py（纯逻辑，无网络）")

    # 1) 涨停 ⇒ 只拦 BUY，不拦 SELL
    assert board_side(3000.0, 3000.0, 2700.0) == "BUY"
    assert check("BUY", 3000.0, 3000.0, 2700.0)[0] is True
    assert check("SELL", 3000.0, 3000.0, 2700.0)[0] is False
    print("  1 涨停拦 BUY / 放 SELL OK")

    # 2) 跌停 ⇒ 只拦 SELL，不拦 BUY
    assert board_side(2700.0, 3000.0, 2700.0) == "SELL"
    assert check("SELL", 2700.0, 3000.0, 2700.0)[0] is True
    assert check("BUY", 2700.0, 3000.0, 2700.0)[0] is False
    print("  2 跌停拦 SELL / 放 BUY OK")

    # 3) 未封板 ⇒ 都不拦
    assert board_side(2850.0, 3000.0, 2700.0) is None
    assert check("BUY", 2850.0, 3000.0, 2700.0)[0] is False
    assert check("SELL", 2850.0, 3000.0, 2700.0)[0] is False
    print("  3 未封板不拦 OK")

    # 4) 字段不可读（回测 nan / 0）⇒ 不拦，且 readable()=False
    assert board_side(2850.0, float("nan"), float("nan")) is None
    assert board_side(2850.0, 0.0, 0.0) is None
    assert readable(float("nan"), float("nan")) is False
    assert readable(3000.0, 2700.0) is True
    print("  4 limit 不可读 ⇒ 不拦（不因缺字段卡死交易）OK")

    # 5) 一跳容差：差半跳视为封板（避免浮点/撮合最后半跳漏判）
    assert board_side(2999.0, 3000.0, 2700.0, tick=4.0) == "BUY"
    assert board_side(2994.0, 3000.0, 2700.0, tick=4.0) is None
    print("  5 一跳容差 OK")

    # 6) 价格非法（nan/0）⇒ 不拦（由上游"缺参考价"那条路处理）
    assert board_side(float("nan"), 3000.0, 2700.0) is None
    assert board_side(0.0, 3000.0, 2700.0) is None
    print("  6 非法 last_price ⇒ 不拦 OK")

    print("[selftest] 全部通过")
    return True


if __name__ == "__main__":
    selftest()
