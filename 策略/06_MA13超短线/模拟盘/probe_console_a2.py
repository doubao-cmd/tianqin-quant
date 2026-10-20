# -*- coding: utf-8 -*-
"""
probe_console_a2.py —— 测试夹具：摸清「某进程所在的控制台」，并向它发一个控制台级事件
================================================================
为什么需要它（A2 R2 验收的技术关键，这里踩了三个坑，全部实测过）：

  坑 1：`(Get-Process).MainWindowHandle` + `.CloseMainWindow()`
        Windows 10 起控制台窗口归 **conhost.exe**，控制台程序的 MainWindowHandle 恒为 0
        （实测返回 NOHWND），根本发不出 WM_CLOSE —— 那是「假装关窗」。
  坑 2：`AttachConsole` 后 `PostMessage(hwnd, WM_CLOSE / WM_SYSCOMMAND+SC_CLOSE)`
        消息投递成功（API 返回 True），但 conhost **不理会外部进程发来的关闭消息**，
        核心进程纹丝不动（实测 3 种投递方式全部无效）。
  坑 3：CTRL_CLOSE_EVENT 只能由系统在「用户真的点 X」时产生，
        `GenerateConsoleCtrlEvent` 只支持 CTRL_C / CTRL_BREAK，造不出来。

⇒ 采用「等价事件 + 直接证据」两步（见 test_watchdog_a2.py T7）：
  ① **直接证据**：`GetConsoleProcessList()` 列出同一个控制台上的所有进程，
     直接回答「守护和核心是不是共用一个控制台」—— 这正是今晚事故的根因本身，
     比任何黑盒观察都硬。
  ② **等价事件**：向该控制台广播 `CTRL_BREAK`。它与「关窗」同属**控制台级事件**：
     凡是挂在这个控制台上的进程都会被终止，退出码同为 0xC000013A
     （实测核心被 CTRL_BREAK 杀掉的 rc=3221225786 = 0xC000013A，与关窗完全一致）。
     因此它对「守护是否在同一控制台」的检验力，与人工点 X 等同。

用法：
  python probe_console_a2.py <目标PID> <结果json> [--break]
"""
import os
import sys
import json
import ctypes
import ctypes.wintypes as wt

CTRL_BREAK_EVENT = 1


def main():
    if len(sys.argv) < 3:
        print("usage: probe_console_a2.py <pid> <out.json> [--break]")
        return 2
    pid = int(sys.argv[1])
    out = sys.argv[2]
    do_break = "--break" in sys.argv

    k = ctypes.windll.kernel32
    res = {"target_pid": pid, "attached": False, "console_pids": [],
           "hwnd": 0, "break_sent": False, "error": ""}

    k.FreeConsole()                      # 先脱离自己所在的控制台
    if not k.AttachConsole(pid):
        res["error"] = f"AttachConsole 失败 err={k.GetLastError()}（目标可能没有控制台）"
        _dump(out, res)
        return 3
    res["attached"] = True
    try:
        arr = (wt.DWORD * 256)()
        n = k.GetConsoleProcessList(arr, 256)
        res["console_pids"] = [int(arr[i]) for i in range(max(0, n))]
        res["hwnd"] = int(k.GetConsoleWindow() or 0)
        # ★ 先落盘再发事件：事件很可能把本进程一起带走
        _dump(out, res)
        if do_break:
            k.SetConsoleCtrlHandler(None, True)      # 尽量让自己活下来（能活就补写结果）
            ok = bool(k.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, 0))
            res["break_sent"] = ok
            _dump(out, res)
            import time
            time.sleep(2)
    finally:
        try:
            k.FreeConsole()
        except Exception:
            pass
    _dump(out, res)
    return 0


def _dump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())


if __name__ == "__main__":
    sys.exit(main())
