# -*- coding: utf-8 -*-
"""
stub_forwarder_a2.py —— 「解释器转发器」假核心（T6-4 专用测试夹具，不是交易程序）
================================================================
存在的唯一理由：验证看门狗的 **PID 语义自检**（T6-4 / D1 体检 §三 P2-A）能不能
在「Popen.pid 不是真实核心」时告警。

真实世界里的原型：venv 的 `pythonw.exe` 是个**转发器**（只负责再拉起一个真解释器，
然后自己退出）。实测 `Popen.pid=38612` 而真正的守护是 `28460`。
危害：看门狗 `proc.kill()` 只杀掉壳，**真核心作为孤儿继续跑、继续下单** —— 比不复位更危险。

本夹具精确复刻这个语义：
  · 启动 → 拉起一个**子 python** 当作「真身」（长睡）
  · 自己 0.2s 后以 **rc=0** 退出（壳退出，真身留作孤儿）

⇒ 看门狗应在日志里写「⚠️ PID 自检：… 疑为解释器转发器」，而不是以为自己拿到了真核心。
   自检只留痕、不阻断（真正的修复是换用非转发器的 python.exe），所以本夹具
   不需要写心跳、也不参与任何交易逻辑。

用法（由 test_watchdog_a2.py 通过 --core 指向）：
  python stub_forwarder_a2.py --symbols FU --trade
"""
import os
import sys
import time
import subprocess

_DUR = float(os.environ.get("STUB_FWD_CHILD_SEC", "60"))

# 「真身」：一个长睡的 python 子进程（模拟被转发器包了一层的真实解释器）
child = subprocess.Popen([sys.executable, "-c",
                          f"import time; time.sleep({_DUR})"])
print(f"[fwd] 转发器 pid={os.getpid()} 已拉起真身 pid={child.pid}，即将自行退出 rc=0",
      flush=True)
time.sleep(0.2)
sys.exit(0)
