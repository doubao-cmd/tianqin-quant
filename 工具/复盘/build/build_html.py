# -*- coding: utf-8 -*-
"""组装前端（P0-A）：从 src/ 模块化源码生成两套产物。

 1) dist/            → 本地 HTTP 版（ES module，命名空间由 import/export 管理）
 2) 复盘终端.html     → 自包含单文件（双击即开，内联 Plotly + 数据 + 拼接后的模块）

拼接规则：按依赖顺序去掉 import/export 后首尾相接，boot.js 必须最后（唯一顶层执行）。
"""
from __future__ import annotations
import os, re, shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "src"
DIST = ROOT / "dist"
OUT = ROOT / "复盘终端.html"

# 依赖顺序：core → data → view* → replay → boot（boot 唯一顶层执行）
ORDER = [
    "core/fmt.js",
    "core/state.js",
    "core/time.js",
    "core/store.js",     # lsStore 工厂：被 marks/ledger 依赖，必须在前
    "data/index.js",
    "replay/book.js",
    "replay/marks.js",
    "replay/score.js",   # ★ P2-4：被 ledger 依赖（scoreSession），必须排在 ledger 之前
    "replay/ledger.js",
    "replay/compare.js", # ★ P2-5：被 view/compare 依赖（cmpAt/cmpRows），必须排在它之前
    "replay/segstats.js",# ★ P3：被 view/stats、view/detail 依赖（segStats/truncation/afterExit）
    "replay/trend.js",   # ★ 五期 T2.1：被 view/stats 依赖（trendSeries/trendHover）
    "view/main.js",
    "view/intra.js",
    "view/table.js",
    "view/detail.js",
    "view/stats.js",
    "view/simpane.js",
    "view/topbar.js",
    "view/compare.js",   # ★ P2-5：被 view/index 依赖（renderCompare），必须排在 index 之前
    "view/mklist.js",    # ★ 五期 T2.3：标记 tab（被 view/index 依赖 renderMkTab）
    "view/index.js",
    "view/report.js",    # ★ P2-4：被 engine 依赖（showReport），必须排在 engine 之前
    "replay/engine.js",
    "boot.js",
]


def read(p):
    return (SRC / p).read_text(encoding="utf-8")


def strip_module(src: str) -> str:
    # 整体处理，兼容多行 import（如 import {\n ... } from '...';）
    src = re.sub(r"^\s*import\s+.*?;\s*$", "", src, flags=re.M | re.S)   # 去掉 import
    src = re.sub(r"^\s*export\s*\{.*?\}\s*;\s*$", "", src, flags=re.M | re.S)  # 去掉 export 列表
    src = re.sub(r"^(\s*)export\s+(const|let|var|function|async function)\b",
                 r"\1\2", src, flags=re.M)                               # export 前缀去掉
    return src


def build_bundle() -> str:
    parts = []
    for f in ORDER:
        parts.append("// ===== " + f + " =====")
        parts.append(strip_module(read(f)))
    return "\n".join(parts)


def main():
    # ★ 2026-10-18：plotly 未装时复用 dist/plotly.min.js（该文件已由前次构建产出）。
    #   本机三个 Python 环境都没装 plotly，硬依赖会让构建直接失败；而复用缓存与
    #   "pip install plotly" 结果等价（同一份 plotly.js 产物），不影响产物正确性。
    try:
        from plotly.offline import get_plotlyjs
        plotly_js = get_plotlyjs().replace("</script>", "<\\/script>")
    except Exception:
        cache = DIST / "plotly.min.js"
        if not cache.exists():
            raise SystemExit("plotly 未安装，且 dist/plotly.min.js 不存在"
                             " ⇒ 先 pip install plotly 再重跑")
        plotly_js = cache.read_text(encoding="utf-8")
        print(f"[warn] plotly 未安装，复用 {cache.name}"
              f"（{len(plotly_js)/1024/1024:.1f} MB）")
    theme = read("theme.css")
    index_tpl = read("index.html")
    data_path = DIST / "data" / "bundle.json"
    if not data_path.exists():
        raise SystemExit("先运行 build/build_data.py 生成 dist/data/bundle.json")
    data_json = data_path.read_text(encoding="utf-8")
    bundle = build_bundle()

    # ---------- 1) HTTP 版：dist/ ----------
    (DIST / "plotly.min.js").write_text(plotly_js, encoding="utf-8")
    if DIST.exists():
        shutil.rmtree(DIST / "src") if (DIST / "src").exists() else None
    shutil.copytree(SRC, DIST, dirs_exist_ok=True)     # 拷贝 src 树（含 boot.js / core / view / ...）
    http_html = (index_tpl
                 .replace("__PLOTLY_SRC__", "plotly.min.js")
                 .replace("__THEME_SRC__", "theme.css")
                 .replace("__BOOT_SRC__", "boot.js"))
    (DIST / "index.html").write_text(http_html, encoding="utf-8")
    print(f"[http] dist/index.html（{ (DIST/'index.html').stat().st_size/1024/1024:.1f} MB）")

    # ---------- 2) 单文件版：复盘终端.html ----------
    safe_data = data_json.replace("</script", "<\\/script")
    safe_bundle = bundle.replace("</script", "<\\/script")
    single = (index_tpl
              .replace('<script src="__PLOTLY_SRC__"></script>',
                       '<script>' + plotly_js + "</script>")
              .replace('<link rel="stylesheet" href="__THEME_SRC__">',
                       "<style>" + theme + "</style>")
              .replace('<script type="module" src="__BOOT_SRC__"></script>',
                       "<script>window.__REVIEW_DATA__=" + safe_data + ";" + safe_bundle + "</script>"))
    OUT.write_text(single, encoding="utf-8")
    print(f"[single] {OUT} （{OUT.stat().st_size/1024/1024:.1f} MB）")

    # 桌面快捷方式指向的全英文路径
    ascii_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ReviewTerminal"
    ascii_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(OUT, ascii_dir / "index.html")
    print(f"[shortcut] {ascii_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
