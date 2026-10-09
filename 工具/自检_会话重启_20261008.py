# -*- coding: utf-8 -*-
"""会话异常自动重启 离线自测（2026-10-08）
==================================================================
★ 不连网、不下单：只验 `RestartLoop` 的状态机。
  这段代码会真的控制「实盘进程死不死」，上线前必须先离线验一遍。

验五件事：
  1. 正常跑完 → 不重启（只调一次）
  2. 抛异常 → 等N 秒后重跑会话体（次数对）
  3. cleanup 每次会话结束都调用（含异常）⇒ api 不泄漏
  4. KeyboardInterrupt → **不重启**、不cleanup（人工退出干净）
  5. max_restart 上限 → 达到后放弃并把异常抛出去
  6. reconnect_wait 真的被尊重（不忙等）
"""
import os
import sys
import time

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.engine_guard import RestartLoop

_ok = True


def check(cond, label):
    global _ok
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        _ok = False


def main():
    print("=" * 66)
    print(" 1) 正常跑完 → 不重启")
    n = {"body": 0, "cleanup": 0}
    RestartLoop(lambda: n.__setitem__("body", n["body"] + 1),
                name="t", reconnect_wait=0,
                cleanup=lambda: n.__setitem__("cleanup", n["cleanup"] + 1)).run()
    check(n["body"] == 1, f"会话体只跑 1 次（实跑 {n['body']}）")
    check(n["cleanup"] == 1, f"cleanup 调1 次（实跑 {n['cleanup']}）")

    print("=" * 66)
    print(" 2) 连续抛异常 → 逐次重启")
    n2 = {"body": 0, "cleanup": 0, "events": []}

    def _boom():
        n2["body"] += 1
        raise RuntimeError(f"第{n2['body']}次模拟断线")

    try:
        RestartLoop(_boom, name="t", reconnect_wait=0,
                    on_event=lambda k, v: n2["events"].append(k),
                    cleanup=lambda: n2.__setitem__("cleanup", n2["cleanup"] + 1),
                    max_restart=3).run()
        check(False, "达到上限时应把异常抛出去")
    except RuntimeError:
        check(True, "达到上限 → 异常抛给调用方（进程退出，不会静默卡死）")
    check(n2["body"] == 3, f"会话体跑 3 次后放弃（实跑 {n2['body']}）")
    check(n2["cleanup"] == 3, f"cleanup 也调 3 次（实跑 {n2['cleanup']}）")
    check(n2["events"] == ["RECONNECT"] * 3,
          f"RECONNECT 事件写了 3 次（每次真实断线都记，实写 {len(n2['events'])}）")

    print("=" * 66)
    print(" 3) KeyboardInterrupt → 不重启，但 api 仍要关")
    n3 = {"body": 0, "cleanup": 0}

    def _ctrlc():
        n3["body"] += 1
        raise KeyboardInterrupt

    try:
        RestartLoop(_ctrlc, name="t", reconnect_wait=0,
                    cleanup=lambda: n3.__setitem__("cleanup",
                                                    n3["cleanup"] + 1)).run()
        check(False, "KeyboardInterrupt 应向上抛")
    except KeyboardInterrupt:
        check(True, "KeyboardInterrupt 向上抛（人工退出干净）")
    check(n3["body"] == 1, f"没有重启（实跑 {n3['body']}）")
    check(n3["cleanup"] == 1,
          f"cleanup 仍被调 1 次（finally 覆盖所有路径，api 要关；实跑 {n3['cleanup']}）")

    print("=" * 66)
    print(" 4) 前两次失败、第三次成功 → 不再重启")
    n4 = {"body": 0}

    def _flaky():
        n4["body"] += 1
        if n4["body"] < 3:
            raise ConnectionError("断线")

    RestartLoop(_flaky, name="t", reconnect_wait=0).run()
    check(n4["body"] == 3, f"跑到成功为止，共 {n4['body']} 次（应为 3）")

    print("=" * 66)
    print(" 5) reconnect_wait 被尊重（不忙等）")
    t0 = time.time()
    n5 = {"body": 0}

    def _boom5():
        n5["body"] += 1
        raise OSError("x")

    try:
        RestartLoop(_boom5, name="t", reconnect_wait=1.0,
                    max_restart=2).run()
    except OSError:
        pass
    el = time.time() - t0
    # max_restart=2 ⇒失败2 次，中间等1 次 ⇒ >= 1.0s
    check(el >= 1.0, f"确实等了（实测 {el:.2f}s，应 >= 1.0s）")
    check(el < 3.0, f"没多等（实测 {el:.2f}s，应 < 3.0s）")

    print("=" * 66)
    print(" 6) cleanup 自身抛异常不得影响重启")
    n6 = {"body": 0, "cl": 0}

    def _bad_cleanup():
        n6["cl"] += 1
        raise RuntimeError("close 失败")

    def _boom6():
        n6["body"] += 1
        if n6["body"] < 2:
            raise ValueError("第一次断")
    RestartLoop(_boom6, name="t", reconnect_wait=0,
                cleanup=_bad_cleanup, max_restart=3).run()
    check(n6["cl"] == 2, f"cleanup 两次都调了（实跑 {n6['cl']}）")
    check(n6["body"] == 2, "cleanup 抛异常没有打断重启")

    print("=" * 66)
    print(" 结果：" + ("全部通过" if _ok else "★ 有 FAIL ★"))
    print("=" * 66)
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())