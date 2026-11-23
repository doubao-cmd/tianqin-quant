# -*- coding: utf-8 -*-
"""日盘尾盘禁开仓守卫（2026-10-28 用户指令）

背景（台账实测，263 个完整回合 2026-10-09 ~ 10-28）：
    信号根收盘出信号、下一根开盘成交。日盘最后一根（5min 下 14:55，收盘 15:00）的
    下一根是 21:00 夜盘开盘，中间隔 6 小时；15:00–21:00 正是欧洲开盘/美盘盘前，
    跳空全堆在 21:00 那一下。
      · 21:00 首根开仓 8 笔，0 胜，合计 −14,090（占全部亏损 25%），横跨 6 个品种；
      · 该 8 笔开仓滑点均值 +146 元 vs 其它时点绝对均值 107.7 元（1.55 倍）。
    09:00 首根 7 笔 +6,320、10:30/13:30 中性 ⇒ **只拦日盘尾盘这一条路径**。

★ 判据用 bar 的**结束时刻**（= 信号真正产生的时刻），不是起点：
  tqsdk `Kline.datetime` 是起点（`objs.py:231`），5min 下最后一根起点是 14:55，
  按起点判 `>=14:56` 永远不命中 —— 会把守卫写成空转。
"""
import os
import sys
import datetime as _dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ma13_live_multi as M   # noqa: E402

FAIL = []
N = [0]


def _ok(cond, msg):
    N[0] += 1
    if cond:
        print(f"  PASS  {msg}")
    else:
        print(f"  FAIL  {msg}")
        FAIL.append(msg)


def _bar(hh, mm, ksec=300, bar_id=100, with_dt=True):
    """构造一根 bar：datetime = 起点、unix epoch 纳秒。"""
    b = {"id": bar_id}
    if with_dt:
        t = _dt.datetime(2026, 10, 28, hh, mm, 0)
        b["datetime"] = int(t.timestamp() * 1e9)
    return b


def _s(ksec=None):
    s = {"name": "测试品种", "pos": 0}
    if ksec is not None:
        s["ksec"] = ksec
    return s


print("== 1. 5min 周期（KSEC=300，当前实盘口径）==")
_ok(M._late_day_block(_s(), _bar(14, 50)) is False, "14:50 起点（收 14:55）⇒ 不拦")
_ok(M._late_day_block(_s(), _bar(14, 55)) is True,  "14:55 起点（收 15:00）⇒ 拦 ★核心用例")
_ok(M._late_day_block(_s(), _bar(14, 45)) is False, "14:45 起点（收 14:50）⇒ 不拦")
_ok(M._late_day_block(_s(), _bar(14, 40)) is False, "14:40 起点（收 14:45）⇒ 不拦")

print("== 2. 其它时段边界必须放行（09:00 实测是赚的，不能误伤）==")
_ok(M._late_day_block(_s(), _bar(9, 0)) is False,  "09:00 日盘首根 ⇒ 不拦")
_ok(M._late_day_block(_s(), _bar(21, 0)) is False, "21:00 夜盘首根 ⇒ 不拦（拦的是它的信号源，不是它自己）")
_ok(M._late_day_block(_s(), _bar(13, 30)) is False, "13:30 午休后 ⇒ 不拦")
_ok(M._late_day_block(_s(), _bar(10, 30)) is False, "10:30 小节后 ⇒ 不拦")
_ok(M._late_day_block(_s(), _bar(23, 0)) is False, "23:00（不存在，防呆）⇒ 不拦")

print("== 3. 若将来改 1min 周期，口径不变（按结束时刻判）==")
_ok(M._late_day_block(_s(60), _bar(14, 56, 60)) is True,  "1min 14:56（收 14:57）⇒ 拦")
_ok(M._late_day_block(_s(60), _bar(14, 59, 60)) is True,  "1min 14:59（收 15:00）⇒ 拦")
_ok(M._late_day_block(_s(60), _bar(14, 54, 60)) is False, "1min 14:54（收 14:55）⇒ 不拦")

print("== 4. 失败方向：拿不到时间就放行（守卫只该少挡，不该多挡）==")
_ok(M._late_day_block(_s(), _bar(14, 55, with_dt=False)) is False, "bar 无 datetime ⇒ 放行")
_ok(M._late_day_block(_s(), {"id": 1, "datetime": "坏值"}) is False, "datetime 非法 ⇒ 放行")
_ok(M._late_day_block(_s(), None) is False, "bar=None ⇒ 放行")

print("== 5. 静态约束：只挡开仓，不碰出场 ==")
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "ma13_live_multi.py"), encoding="utf-8").read()
_i_open = _src.find("# ---- 开仓（对齐回测")
_i_call = _src.find("_late_block = _late_day_block(s, _sig_bar)")
_i_exit = _src.find("why = sig.exit_hit(ind, i, pos, s[\"stop\"], s[\"bars_in\"])")
_ok(_i_call > 0, "`_late_block` 已接入 _process")
_ok(_i_open > 0 and _i_call > _i_open, "挂载点在「开仓」段之后")
_ok(_i_exit > 0 and _i_call > _i_exit, "挂载点在出场判定之后（出场在前、开仓在后）")
_ok(_i_exit < _i_open, "出场分支确实在开仓分支之前（结构顺序未被破坏）")
_ok("_open_ok = False" in _src[_i_call:_i_call + 400], "命中时置 _open_ok=False")
_ok("_sig_bar = k.iloc[-2]" in _src, "★ 守卫判据取 k.iloc[-2]（已收盘的信号来源 bar），不是 iloc[-1]")
_ok("LATEDAY" in _src, "有 LATEDAY 台账事件（可复盘「被拦了几次」）")
_ok("LATEGUARD" in _src, "有 LATEGUARD 启动自证日志（无信号时也能证明已加载）")
_ok(_src.find("_late_probe") < _src.find("_late_block = _late_day_block"), "自证日志在开仓判定之前（首 tick 即打印）")

print()
if FAIL:
    print(f"FAILED {len(FAIL)} 项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
print(f"ALL PASS（{N[0]} 项）")
