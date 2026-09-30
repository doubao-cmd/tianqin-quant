# -*- coding: utf-8 -*-
"""tee_log.py —— 三通工具：把管道输入同时写到 stdout（黑窗口实时显示）和日志文件。

被 策略/*/回测.bat、模拟盘.bat 以如下方式调用：
    "%PY%" -u 某策略_回测.py 2>&1 | "%PY%" "%ROOT%工具\\tee_log.py" "%ABS_LOG%"

设计要点：
- 二进制读写（stdin.buffer / stdout.buffer），不碰文本编解码，
  tqsdk 的混合编码输出原样透传，绝不因解码失败中断回测；
- 日志父目录不存在则自动创建（运行日志/03_布林带/回测/<品种>/ 等）；
- 追加模式写日志，每次运行在末尾补一行开始分隔符（带本地时间）；
- 管道关闭 / Ctrl+C 时安静退出。
"""
import os
import sys
import time


def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("用法: python tee_log.py <日志文件路径>\n")
        sys.exit(2)

    log_path = sys.argv[1]
    log_dir = os.path.dirname(os.path.abspath(log_path))
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    out = sys.stdout.buffer
    with open(log_path, "ab") as f:
        sep = ("\n==== 运行开始 " + time.strftime("%Y-%m-%d %H:%M:%S") + " ====\n").encode("utf-8")
        f.write(sep)
        f.flush()
        while True:
            try:
                chunk = sys.stdin.buffer.readline()
            except (KeyboardInterrupt, OSError):
                break
            if not chunk:
                break
            out.write(chunk)
            out.flush()
            f.write(chunk)
            f.flush()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
