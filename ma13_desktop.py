# -*- coding: utf-8 -*-
"""桌面入口的路径适配层（唯一职责：把中文路径挡在 Python 里，让 .cmd 保持纯 ASCII）。

用法（由桌面 .cmd 调用，不给人手敲）：
    python ma13_desktop.py select     # 只选池，写 workbench.json + selection_state.json
    python ma13_desktop.py restart    # 停旧实例 -> 选池(gap=0 自动跳过) -> detach 启动

为什么不把路径写进 .cmd：项目路径含中文，cmd 按系统代码页(936)解码文件字节，
UTF-8 中文会变成乱码路径（本项目 2026-10 已踩过）。Python 侧是 UTF-8，安全。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
TARGETS = {
    "select": os.path.join(ROOT, "工具", "复盘", "exec4", "select_pool_sat.py"),
    "restart": os.path.join(ROOT, "策略", "06_MA13超短线", "模拟盘", "ma13_watchdog_main.py"),
}
ARGS = {
    "select": [],
    "restart": ["--restart", "--symbols", "auto", "--trade"],
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in TARGETS:
        print("usage: ma13_desktop.py select^|restart")
        return 64
    act = sys.argv[1]
    target = TARGETS[act]
    if not os.path.isfile(target):
        print("[ERROR] target not found: " + target)
        return 66
    print("[desktop] action  = " + act)
    print("[desktop] project = " + ROOT)
    print("[desktop] python  = " + sys.executable)
    print("[desktop] target  = " + target)
    print("-" * 70)
    rc = subprocess.call([sys.executable, "-u", target] + ARGS[act], cwd=ROOT)
    print("-" * 70)
    print("[desktop] exit code = %d" % rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
