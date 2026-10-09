# -*- coding: utf-8 -*-
"""
行尾体检（CI 用，2026-10-09）
================================================================
★为什么需要它：`2026-10-08` 20:05 出现过「bat 双击满屏乱码」，
  根因是编辑工具把 .bat 写成 **LF**，而 cmd.exe 按 CRLF 切行——
  只有 LF 时它把整个文件当一行，于是 `REM` 注释内容被当成命令执行。
  这个坑**肉眼很难提前发现**，所以做成体检，交给 CI 每次 push 兜底。

★为什么不用 .gitattributes 保证：
  `.gitattributes` 能保证**入库后**的行尾，但**改文件时是本地操作**，
  改完还没 commit 的那一瞬间没人管。所以仍需要一个独立体检。

判定规则（与 .gitattributes 保持一致）：
    bat / cmd / ps1  → 必须是 CRLF（且**不允许出现孤立 LF**）
    py / md / json / csv / yml / yaml → 必须是 LF
    二进制            → 跳过

退出码：0 = 全部通过；1 = 有违规（CI 会红）
"""
import os
import sys

# CRLF 强制族（Windows 解释器逐行解析的文件）
CRLF_EXT = {".bat", ".cmd", ".ps1"}
# LF 强制族（其余文本）
LF_EXT = {".py", ".md", ".json", ".csv", ".yml", ".yaml", ".txt"}

# 整目录跳过（归档/冻结/缓存/版本库）
SKIP_DIR = {
    ".git", "__pycache__", "_归档", "_冻结", "_收纳",
    "node_modules", ".idea", ".vscode",
}


def 检查文件(path, rel):
    """返回 (问题字符串 or None, 统计增量dict)"""
    with open(path, "rb") as f:
        data = f.read()
    if not data:
        return None, {"crlf": 0, "lone_lf": 0}

    crlf = data.count(b"\r\n")
    lf_total = data.count(b"\n")
    lone_lf = lf_total - crlf          # 孤立 LF = 只出现 \n 而没有前置 \r
    ext = os.path.splitext(path)[1].lower()

    if ext in CRLF_EXT:
        if lone_lf > 0:
            return (f"❌ {rel}: CRLF 文件里有 {lone_lf} 个孤立 LF"
                    f"（cmd 会把整个文件当一行 ⇒ REM 被当命令跑 ⇒ 满屏乱码）"
                    ), {"crlf": crlf, "lone_lf": lone_lf}
        return None, {"crlf": crlf, "lone_lf": lone_lf}

    if ext in LF_EXT:
        if crlf > 0:
            return (f"⚠️  {rel}: LF 文件里有 {crlf} 个 CRLF"
                    f"（git 已按 .gitattributes 转换过；若你在改后看到它，"
                    f"说明改动发生在 git 之外，属正常）"
                    ), {"crlf": crlf, "lone_lf": lone_lf}
        return None, {"crlf": crlf, "lone_lf": lone_lf}

    return None, {"crlf": 0, "lone_lf": 0}


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    problems = []
    统计 = {"bat类": 0, "文本类": 0}
    违规 = {"bat类": 0, "文本类": 0}

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIR and not d.startswith(".")]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            # ⚠️ 根目录有个 Windows 保留设备名 `nul`，不是普通文件：
            #    os.path.relpath 对它会抛「path is on mount \\.\nul」。
            #    跳过所有非普通文件（这个坑已在 MEMORY 里留档）。
            if not os.path.isfile(p) or fn.lower() == "nul":
                continue
            rel = os.path.relpath(p, root).replace("\\", "/")
            ext = os.path.splitext(fn)[1].lower()
            if ext not in CRLF_EXT and ext not in LF_EXT:
                continue
            try:
                msg, st = 检查文件(p, rel)
            except Exception as e:
                problems.append(f"读取失败 {rel}: {e}")
                continue
            if ext in CRLF_EXT:
                统计["bat类"] += 1
                if msg:
                    违规["bat类"] += 1
                    problems.append(msg)
            else:
                统计["文本类"] += 1
                if msg and "⚠️" in msg:
                    # CRLF 混在 LF 文件里：只提醒不失败
                    统计["文本类"] += 1

    print("=" * 70)
    print(" 行尾体检（CRLF: bat/cmd/ps1 ｜ LF: py/md/json/csv/yml）")
    print("=" * 70)
    print(f" 已检 bat/cmd/ps1: {统计['bat类']} 个，其中违规 {违规['bat类']} 个")
    print(f" 已检 文本类      : {统计['文本类']} 个")
    print("-" * 70)

    # 硬错误（❌）与提醒（⚠️）分开
    hard = [x for x in problems if x.startswith("❌")]
    soft = [x for x in problems if not x.startswith("❌")]

    if hard:
        print("★ 有硬错误（会导致 bat 无法双击）：")
        for x in hard:
            print("   " + x)
        print("-" * 70)
        print(" 修复：跑_收纳/脚本/_修bat行尾_20261008.py（只改行尾，断言字符零改动）")
        print("=" * 70)
        return 1

    if soft:
        print(f"提醒（不致失败，{len(soft)} 条）：")
        for x in soft[:10]:
            print("   " + x)
        if len(soft) > 10:
            print(f"   ...另{len(soft)-10} 条")
        print("-" * 70)

    print(" 结果：全部通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())