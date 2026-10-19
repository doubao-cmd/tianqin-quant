# -*- coding: utf-8 -*-
"""拼接 bundle 顶层重名扫描：防 `Identifier 'X' has already been declared`。

复用 build_html.py 的 strip 规则（import/export 去掉后首尾相接 = 同一作用域），
扫 src 全树顶层 const/let/var/function/class 声明，报告跨文件重名。
用法：python scan_dup.py [--quiet]   exit 0 = 无重名
"""
from __future__ import annotations
import re, sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

def strip(src: str) -> str:
    src = re.sub(r"^\s*import\s+.*?;\s*$", "", src, flags=re.M | re.S)
    src = re.sub(r"^\s*export\s*\{.*?\}\s*;\s*$", "", src, flags=re.M | re.S)
    src = re.sub(r"^(\s*)export\s+(const|let|var|function|async function)\b", r"\1\2", src, flags=re.M)
    return src

DECL = re.compile(r"^(const|let|var|function|class|async function)\s+")
NAME = re.compile(r"[A-Za-z_$][\w$]*")

def names_in(text: str):
    out = []
    for line in text.split("\n"):
        m = DECL.match(line)
        if not m:
            continue
        kind = m.group(1)
        rest = line[m.end():]
        if kind in ("function", "class", "async function"):
            nm = NAME.match(rest)
            if nm:
                out.append((nm.group(0), line.strip()[:70]))
        else:
            # const/let/var：可能多声明符；在赋值/分号之前按 , 切
            head = re.split(r"[=;]", rest, 1)[0]
            for part in head.split(","):
                nm = NAME.match(part.strip())
                if nm:
                    out.append((nm.group(0), line.strip()[:70]))
    return out

def main():
    files = sorted(SRC.rglob("*.js"))
    seen = {}   # name -> [(file, line)]
    for f in files:
        rel = f.relative_to(SRC).as_posix()
        for nm, ctx in names_in(strip(f.read_text(encoding="utf-8"))):
            seen.setdefault(nm, []).append((rel, ctx))
    dups = {k: v for k, v in seen.items() if len(v) > 1}
    if dups:
        print(f"发现 {len(dups)} 组顶层重名：")
        for k, v in sorted(dups.items()):
            print(f"  ✗ {k}")
            for rel, ctx in v:
                print(f"      {rel}: {ctx}")
        return 1
    if "--quiet" not in sys.argv:
        print(f"OK：{len(files)} 个文件，0 组顶层重名")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
