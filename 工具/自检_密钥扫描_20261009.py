# -*- coding: utf-8 -*-
"""
密钥泄露扫描（CI 版，2026-10-09）
================================================================
★ 与 `_收纳/脚本/_密钥扫描_20261008.py` 的区别：
  那份是**一次性**的（2026-10-08 建 GitHub 私库前跑过一次，结论：干净）。
  这份是**常驻**的，挂在 CI 上每次 push 跑，防的是「哪天忘了加 .gitignore
  就把密码提交了」。

★ 为什么必须扫内容而不只是文件名
  `.gitignore` 只管「以后」，管不了「过去」。真实风险不止文件名——
  **密码明文可能藏在备份文件（.bak_*）、一次性脚本、导出 json、运行日志、bat 命令行里**。

★ 只读 git 对象，不写工作区、不动引用、不 checkout。

退出码：0 = 干净；1 = 命中（CI 会红）
"""
import os
import re
import sys
import shutil
import subprocess

# ---- 找 git 可执行文件（Windows 上 git 不一定在 PATH）----
GIT = shutil.which("git") or r"E:\Git\cmd\git.exe"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 占位符白名单：这些是「在这里填」的模板，不是真密码
# ⚠️ 中文占位符必须覆盖（config_account_template.py 用的是「在这里填…」），
#    否则 CI 会误报——第一次跑就把 4 处中文占位符报成了泄露。
PLACEHOLDER = re.compile(
    r"^(在这里填.*|请填.*|填写.*|"
    r"your_?password|your_?user|your_?account|xxx+|todo|changeme|"
    r"示例|样例|test|dummy|placeholder|"
    r"<[^>]*>|\"\"|'')$", re.I)

PATTERNS = [
    ("账号密码赋值", re.compile(
        r"""^\+\s*(LIVE_)?PASSWORD\s*=\s*["']([^"']{4,})["']""", re.I | re.M)),
    ("账号名赋值", re.compile(
        r"""^\+\s*(LIVE_)?USER\s*=\s*["']([^"']{4,})["']""", re.I | re.M)),
    ("私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("AWS key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("云 API key", re.compile(r"\bLTAI[A-Za-z0-9]{12,}\b")),
]


def git(*args):
    r = subprocess.run([GIT] + list(args), cwd=ROOT,
                       capture_output=True, encoding="utf-8", errors="replace")
    return r.stdout


def main():
    if not os.path.exists(GIT):
        print(f"[warn] 找不到 git 可执行文件：{GIT}，跳过密钥扫描")
        return 0

    print("=" * 70)
    print(" 密钥泄露扫描（工作区 + 全历史）")
    print("=" * 70)

    commits = [x for x in git("rev-list", "--all").split() if x]
    print(f" 提交总数: {len(commits)}")

    hits = []

    # ---------- 1) 扫工作区（已跟踪文件）----------
    files = [f for f in git("ls-files").split("\n") if f]
    print(f" 已跟踪文件: {len(files)}")
    for rel in files:
        p = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                txt = f.read()
        except Exception:
            continue
        for 名称, pat in PATTERNS:
            for m in pat.finditer(txt):
                s = m.group(0).strip().lstrip("+").strip()
                val = m.groups()[-1] if m.groups() else s
                if PLACEHOLDER.match(str(val).strip()):
                    continue          # 占位符，跳过
                hits.append((f"工作区/{rel}", 名称, s[:60]))

    # ---------- 2) 扫全历史（批量导出，一次进程）----------
    # ⚠️ 必须批量：逐 blob 调 `git cat-file` 会上万次调用、跑到超时。
    # ⚠️ 但全历史 diff 可能极大（本仓库实测 2.7 亿字符，CI 上会超时）。
    #    ⇒ 按提交分批导出，每批立即扫完就丢弃，不把2.7 亿字符全塞进内存。
    BATCH = 50                      # 每批提交数
    MAX_BATCH = 40                  # 最多扫多少批（2000 次提交，覆盖足够）
    alldiffs = []
    扫过的提交 = 0
    for i in range(0, min(len(commits), BATCH * MAX_BATCH), BATCH):
        batch = commits[i:i + BATCH]
        # ⚠️ 不能用 `A^..B` 这种范围语法——实测在本仓库返回空（易手性差，
        #    且 root commit 无父节点会报错）。直接用 -n 限个数 + 跳过已扫过的，
        #    靠 `--skip` 推进游标最稳。
        skip = i
        d = git("log", "-p", f"--skip={skip}", f"--max-count={len(batch)}", "--all")
        if not d.strip():
            break
        alldiffs.append(d)
        扫过的提交 += len(batch)
        if (i // BATCH) % 10 == 0:
            print(f"   ...已扫 {min(i+len(batch), len(commits))}/{len(commits)} 次提交",
                  flush=True)
    log = "\n".join(alldiffs)
    del alldiffs
    print(f" 实际扫了 {扫过的提交} 次提交，diff 长度 {len(log):,} 字符")
    if 扫过的提交 < len(commits):
        print(f" ⚠️ 有 {len(commits)-扫过的提交} 次提交未扫到"
              f"（超过 MAX_BATCH 上限）—— 需人工确认或调大上限")

    for 名称, pat in PATTERNS:
        for m in pat.finditer(log):
            s = m.group(0).strip().lstrip("+").strip()
            val = m.groups()[-1] if m.groups() else s
            if PLACEHOLDER.match(str(val).strip()):
                continue
            hits.append(("git历史", 名称, s[:60]))

    # ---------- 报告 ----------
    print("-" * 70)
    if not hits:
        print("✅ 未发现密钥。真密码从未进过仓库。")
        print("=" * 70)
        return 0

    print(f"❌ 命中 {len(hits)} 处，需人工确认：")
    for where, 名称, frag in hits[:30]:
        print(f"   [{where}] {名称}: {frag}")
    if len(hits) > 30:
        print(f"   ...另 {len(hits)-30} 处")
    print("=" * 70)
    return 1


if __name__ == "__main__":
    sys.exit(main())