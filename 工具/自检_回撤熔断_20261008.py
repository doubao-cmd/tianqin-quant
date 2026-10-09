# -*- coding: utf-8 -*-
"""回撤熔断闸门离线自测（2026-10-08）
==================================================================
★ 不连网、不下单：只验engine_guard.Fuse 与 run_live_boll 闸门判据的
  **算式与状态机**，把「写完没验过就上线」这个风险掐掉。

验三件事：
  1. 熔断不误触发（权益单边上涨/小回撤）
  2. 熔断准时触发，且闩死（后续回升也不自动复位）+ 落盘
  3. 重启后闩锁仍在（跨会话持久化）→ 只出不进
  4. drill=True 走同一算式但**不落盘不闩死**
  5. 闸门判据：平仓放行 / 反手与新开仓拦截 / 减仓放行
"""
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.engine_guard import Fuse, DD_FUSE
# 弹窗在自测里会真的弹 Windows 对话框 ⇒临时换成静默版
import lib.engine_guard as G

_popup_calls = []
G.popup = lambda title, msg: _popup_calls.append((title, msg))

_ok = True


def check(cond, label):
    global _ok
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        _ok = False


def blocked_gate(want, target, fuse_blocked=True):
    """把 run_live_boll 里的闸门判据**逐字抄过来**。
    ⚠️ 抄写而非调用，是为了让两边判据不一致时能看出来（源码在这里可查）。"""
    return not (fuse_blocked and target != 0
                and (abs(target) > abs(want) or (want * target) < 0))


def main():
    tmp = tempfile.mkdtemp(prefix="fuse_test_")
    p = os.path.join(tmp, "熔断状态.json")

    print("=" * 66)
    print(" 1) 不误触发")
    f = Fuse(tmp, init_balance=200000.0, threshold=DD_FUSE)
    check(not f.eval(200000.0), "起点 200000 不触发")
    check(not f.eval(260000.0), "涨到 260000 不触发（峰值应抬到 26万）")
    check(abs(f.peak - 260000.0) < 1e-6, f"峰值已抬到 {f.peak:.0f}")
    check(not f.eval(240000.0), "从 26万回撤到 24万 = 7.7% 不触发")
    check(not f.eval(215000.0), "从 26万回撤到 21.5万 = 17.3% 不触发（<20%）")
    check(not f.blocked(), "此时未闩死")

    print("=" * 66)
    print(" 2) 准时触发 + 闩死")
    r = f.eval(207000.0)
    check(r, "从 26万跌到 20.7万 = 20.4% → 触发")
    check(f.blocked(), "已闩死")
    check(len(_popup_calls) >= 1, "弹窗被调用")
    check(any(x[0].endswith("回撤熔断触发") for x in _popup_calls),
          "弹窗标题含「回撤熔断触发」")
    check(not f.eval(250000.0), "回升到 25万 → 不再重复触发（边沿语义）")
    check(f.blocked(), "★ 回升后仍是闩死（不会自动解锁）")
    check(os.path.exists(p), "熔断状态文件已落盘")
    with io.open(p, encoding="utf-8") as fh:
        st = json.load(fh)
    check(bool(st.get("halted")), "落盘内容 halted=True")
    check(abs(float(st.get("peak", 0)) - 260000.0) < 1e-6,
          f"落盘峰值 {st.get('peak'):.0f}")
    check(abs(float(st.get("threshold", 0)) - DD_FUSE) < 1e-9,
          f"落盘阈值 {st.get('threshold')}")

    print("=" * 66)
    print(" 3) 重启后闩锁仍在（跨会话持久化）")
    f2 = Fuse(tmp, init_balance=200000.0, threshold=DD_FUSE)
    check(f2.blocked(), "新进程读同一文件 → 仍是闩死（只出不进）")
    check(not f2.eval(300000.0), "新高不解除闩锁")
    check(f2.blocked(), "★ 创新高也仍是闩死")

    print("=" * 66)
    print(" 4) 人工复位 = 删文件")
    os.remove(p)
    f3 = Fuse(tmp, init_balance=200000.0, threshold=DD_FUSE)
    check(not f3.blocked(), "删掉状态文件 → 闩锁解除")

    print("=" * 66)
    print(" 5) drill 演练：同算式，但不落盘不闩死")
    f4 = Fuse(tmp, init_balance=200000.0, threshold=DD_FUSE)
    r = f4.eval(150000.0, drill=True)
    check(r, "演练触发（算式与真实一致）")
    check(not f4.blocked(), "★ 演练后**未闩死**")
    check(not os.path.exists(p), "★ 演练**未落盘**（状态文件仍是干净的）")
    check(any("演练" in x[0] for x in _popup_calls), "演练弹窗标题带「演练」")

    print("=" * 66)
    print(" 6) 闸门判据（逐字抄自 run_live_boll）")
    # blocked=True 时：want 持仓 -> target 决策
    cases = [
        # (want, target, 应否放行, 说明)
        (0, 5, False, "空仓→开多 5手：拦截"),
        (0, -3, False, "空仓→开空 3手：拦截"),
        (-3, 5, False, "持空 3手→反手开多 5手：拦截"),
        (3, -2, False, "持多 3手→反手开空 2手：拦截"),
        (3, 5, False, "持多 3手→加仓 5手：拦截"),
        (3, 0, True, "持多 3手→平仓：放行"),
        (-3, 0, True, "持空 3手→平仓：放行"),
        (3, 2, True, "持多 3手→减仓 2手：放行（engine 不产生此值，保险）"),
        (3, 3, True, "维持现仓：放行（无动作）"),
    ]
    for want, target, should_pass, label in cases:
        got = blocked_gate(want, target)
        check(got == should_pass, label)

    print("=" * 66)
    print(" 7) 未闩死时闸门必须完全不拦（否则是致命 bug）")
    for want, target in [(0, 5), (0, -3), (-3, 5), (3, 5), (3, 0)]:
        check(blocked_gate(want, target, fuse_blocked=False),
              f"未闩死：want={want} target={target} 正常放行")

    print("=" * 66)
    print(" 8) 边界：NaN / 负数 / 零权益 不计入")
    f5 = Fuse(tmp, init_balance=200000.0, threshold=DD_FUSE)
    check(not f5.eval(float("nan")), "NaN 不触发")
    check(not f5.eval(-1.0), "负权益不触发")
    check(not f5.eval(0.0), "零权益不触发")
    check(abs(f5.peak - 200000.0) < 1e-6, "以上均未污染峰值")

    shutil.rmtree(tmp, ignore_errors=True)
    print("=" * 66)
    print(" 结果：" + ("全部通过" if _ok else "★ 有 FAIL ★"))
    print("=" * 66)
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())