# -*- coding: utf-8 -*-
"""
行尾归一.py —— 把全项目文件行尾统一到约定，避免"补丁脚本静默匹配失败"

约定（见项目记忆「编码铁律」）：
    lib/**/*.py、策略池/**/*.py、工具/**/*.py、根目录 *.py   -> LF
    组合/**/*.py、**/*.bat                                  -> CRLF

为什么要它：
    编辑工具（Edit）写回文件时会把整个文件改成 CRLF。engine.py 本来是 LF，
    改几行之后整文件变 CRLF，之后再写按 LF 匹配的字节级补丁脚本就会"静默匹配不到"，
    改动看起来执行成功、实际没落盘 —— 本项目踩过这个坑。

用法：
    python 工具/行尾归一.py            # 干跑：只报告哪些文件不符合约定
    python 工具/行尾归一.py --go       # 实际改写
    python 工具/行尾归一.py --go 组合   # 只处理名字含"组合"的路径
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 目录名命中则整棵子树跳过（生成物/归档/缓存不进版本约定）
# _归档 整体跳过 —— 里面是冷存的历史代码，不再维护，也就不必归一行尾。
SKIP_DIRS = {"回测结果", "回测记录", "运行日志", "实测记录", "__pycache__", "_归档",
             ".git", ".idea", ".vscode", "node_modules"}

# 相对路径前缀 -> 期望行尾（按顺序匹配，先命中生效）
RULES = [
    ("组合/", "CRLF"),        # 组合层 = 运行入口，与旧策略脚本同类
    ("策略池/", "LF"),        # 纯逻辑库
    ("lib/", "LF"),
    ("工具/", "LF"),
    ("config_", "LF"),
]


def want_eol(rel):
    """返回该相对路径期望的行尾（LF/CRLF），不匹配任何规则返回 None。"""
    if not rel.lower().endswith((".py", ".bat")):
        return None
    posix = rel.replace("\\", "/")
    if posix.lower().endswith(".bat"):
        return "CRLF"          # bat 一律 CRLF
    for prefix, eol in RULES:
        if posix.startswith(prefix) or os.path.basename(posix).startswith(prefix):
            return eol
    return None


def walk(root, contains=None):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root)
            if contains and contains not in rel:
                continue
            yield p, rel.replace("\\", "/")


def detect(raw):
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    if crlf and lf:
        return "混合"
    if crlf:
        return "CRLF"
    return "LF"


def normalize(raw, eol):
    """先统一成 LF，再按目标替换，保证不会出现 \\r\\r\\n。"""
    data = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if eol == "CRLF":
        data = data.replace(b"\n", b"\r\n")
    return data


def main():
    go = "--go" in sys.argv
    contains = None
    for a in sys.argv[1:]:
        if not a.startswith("--"):
            contains = a

    checked = 0
    bad = []
    for p, rel in walk(ROOT, contains):
        eol = want_eol(rel)
        if eol is None:
            continue
        checked += 1
        raw = open(p, "rb").read()
        cur = detect(raw)
        if cur != eol:
            bad.append((p, rel, eol, cur, raw))

    print(f"扫描 {checked} 个 .py/.bat 文件，不符合行尾约定的：{len(bad)} 个")
    print("=" * 70)
    for p, rel, eol, cur, _raw in bad:
        print(f"  期望 {eol:<4s} 实际 {cur:<4s}  {rel}")

    if not bad:
        print("\n全部符合约定，无需处理。")
        return 0

    if not go:
        print("\n[干跑] 未改写。加 --go 实际执行。")
        return 0

    print("\n[执行] 归一 ...")
    for p, rel, eol, cur, raw in bad:
        open(p, "wb").write(normalize(raw, eol))
        now = detect(open(p, "rb").read())
        flag = "OK" if now == eol else "!! 失败"
        print(f"  {flag}  {cur} -> {now}  {rel}")

    # 复核
    left = [(rel, detect(open(p, "rb").read()), eol) for p, rel, eol, _c, _r in bad]
    bad_left = [x for x in left if x[1] != x[2]]
    print(f"\n[复核] 仍不符合：{len(bad_left)} 个")
    return 1 if bad_left else 0


if __name__ == "__main__":
    sys.exit(main())
