# -*- coding: utf-8 -*-
"""根目录清爽 —— 把「日常用不到」的顶层项设为隐藏属性。

为什么需要它
------------
顶层项里，真正每天要碰到的是**三个文件夹**（策略 / 回测结果 / 实测记录），其余是
共享库、辅助脚本、冷存归档（_归档）、账号密钥、说明文档、总菜单入口 ——
它们在**程序运行时会一直被访问**，但不需要人眼在桌面上扫到。

⚠️ 它只改「隐藏属性」，**不移动、不删除、不改名**：
    · 文件仍在原路径 ⇒ Python / bat / 所有工具脚本访问它们**完全不受影响**
      （隐藏属性只影响资源管理器的显示，不参与任何文件读写）
    · Windows 资源管理器默认不显示隐藏项 ⇒ 视觉上消失
    · 随时 `--show` 一键恢复

⚠️★ **隐藏会让「原子替换式写入」失败**（2026-09-27 实测，两次踩到）：
    带隐藏属性的文件，编辑器 / AI 用 `os.replace`（写临时文件再改名）那一套保存时会报
    `EPERM: operation not permitted`。本项目里 `README.md` `目录地图.md` `config_symbols.py`
    都中过招。
    ⇒ **要改这些文件，先单独取消那一项的隐藏，改完再藏回来**（或临时 `--show`）。
    （普通的 `open(p, 'w')` 截断写入不受影响 —— 只有原子替换会失败。）

⚠️ 如果设完了资源管理器里还是看得到，那是勾了「查看 → 显示 → 隐藏的项目」，
   取消勾选即可。

用法
----
    python 工具/根目录清爽.py              # 隐藏清单内的项（默认动作）
    python 工具/根目录清爽.py --show       # 恢复显示（只还原清单内的）
    python 工具/根目录清爽.py --show-all   # 恢复显示（顶层全部，含清单外的）
    python 工具/根目录清爽.py --list       # 只看当前状态，什么都不改
"""

import argparse
import ctypes
import os

FILE_ATTRIBUTE_HIDDEN = 0x2
INVALID = 0xFFFFFFFF

_k = ctypes.windll.kernel32
_k.GetFileAttributesW.restype = ctypes.c_uint32
_k.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
_k.SetFileAttributesW.restype = ctypes.c_int
_k.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]


# ---------------------------------------------------------------------------
# 清单：想调整，改这两个字典就行（增删一项不必动下面的代码）
# ---------------------------------------------------------------------------

隐藏清单 = {
    ".workbuddy":        "工具自带的后台数据（记忆 / 技能 / 缓存）",
    "_归档":             "历史存档，只在考古时进",
    "_冻结":             "已冻结策略存档（11策略证伪+VWAP线），只当证据留着",
    "运行日志":          "程序自动写的日志，人不用翻",
    "lib":               "共享库代码（engine / paths / ...），只在改引擎时进",
    "工具":              "辅助脚本，日常由 AI 代跑",
    "学习":              "学习资料，不是日常文件",
    "config_account.py": "账号密钥，不该长期摆在桌面上",
    "config_account_template.py": "账号模板，只在配新机时抄一次",
    "config_symbols.py": "品种配置（引擎 import 它，但不需要人眼扫）",
    ".gitignore":        "git 工程文件（⚠️改它前先 --show，隐藏态下原子替换保存会 EPERM）",
    "README.md":         "项目总说明",
    "模拟盘执行清单.md": "模拟盘操作备忘",
    "跑回测.bat":        "旧总菜单入口（已被 天勤量化.bat 取代，留作兼容）",
    "nul":               "删不掉的系统残留（Windows保留名），藏住眼不见为净",
}

# 只作说明用 —— 程序不会动它们的属性
保持可见 = {
    "策略":     "唯一工作区：写逻辑 + 双击启动都在这里",
    "回测结果": "当前工作状态的报告",
    "实测记录": "模拟盘 / 实盘台账",
    "天勤量化.bat": "总入口：双击进工作台",
    "量化职场学习路线图.html": "职业路线图，随时翻",
    "目录地图.md": "找东西用的地图",
}


def 找根():
    """从本文件位置向上找，直到看见 品种池/ 或 README.md。

    不写死层数 —— 整个项目拖到哪都能跑（与全项目的路径健壮性口径一致）。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(12):
        if os.path.isdir(os.path.join(here, "品种池")) or \
                os.path.isfile(os.path.join(here, "README.md")):
            return here
        上级 = os.path.dirname(here)
        if 上级 == here:        # 到盘根了还没找到
            break
        here = 上级
    raise SystemExit("找不到项目根目录（没看到 品种池/ 或 README.md）")


def 是隐藏(path):
    a = _k.GetFileAttributesW(path)
    if a == INVALID:
        return None                     # 路径不存在或读不到
    return bool(a & FILE_ATTRIBUTE_HIDDEN)


def 设隐藏(path, 藏):
    a = _k.GetFileAttributesW(path)
    if a == INVALID:
        return False
    新 = (a | FILE_ATTRIBUTE_HIDDEN) if 藏 else (a & ~FILE_ATTRIBUTE_HIDDEN)
    return bool(_k.SetFileAttributesW(path, 新))


def 扫描(根, 清单):
    """返回 [(名称, 存在吗, 当前隐藏吗, 理由)]"""
    return [(n, os.path.exists(os.path.join(根, n)), 是隐藏(os.path.join(根, n)), why)
            for n, why in sorted(清单.items(), key=lambda kv: kv[0].lower())]


def 打印状态(根, 清单):
    print(f"项目根：{根}")
    print()
    for n, 存在, 隐藏, why in 扫描(根, 清单):
        if not 存在:
            标 = "—"
        else:
            标 = "隐藏" if 隐藏 else "显示"
        print(f"  [{标}] {n:<20} {why}")
    缺 = [n for n, 存在, _, _ in 扫描(根, 清单) if not 存在]
    if 缺:
        print(f"\n  （清单里有 {len(缺)} 项在本目录下不存在：{'、'.join(缺)}）")


def 切换(根, 清单, 藏):
    改 = 跳过 = 缺 = 0
    for n, 存在, 当前, why in 扫描(根, 清单):
        if not 存在:
            缺 += 1
            continue
        if 当前 == 藏:
            跳过 += 1
            continue
        if 设隐藏(os.path.join(根, n), 藏):
            改 += 1
            print(f"  {'隐藏' if 藏 else '还原'}  {n}")
        else:
            print(f"  ⚠️ 改不动  {n}（属性写入失败）")
    print(f"\n  完成：改动 {改} 项，本来就是目标状态 {跳过} 项，清单内不存在 {缺} 项。")
    print("  ⚠️ 资源管理器若仍显示，取消勾选「查看 → 显示 → 隐藏的项目」。")


def main():
    ap = argparse.ArgumentParser(
        description="把日常用不到的顶层项设为隐藏属性（只改属性，不动文件）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", action="store_true",
                    help="恢复显示（只还原隐藏清单内的项）")
    ap.add_argument("--show-all", dest="show_all", action="store_true",
                    help="恢复显示（顶层全部，含清单外的）")
    ap.add_argument("--list", action="store_true",
                    help="只看当前状态，什么都不改")
    args = ap.parse_args()

    根 = 找根()

    if args.list:
        打印状态(根, 隐藏清单)
        print("\n保持可见（程序不动）：")
        for n, why in sorted(保持可见.items(), key=lambda kv: kv[0].lower()):
            存在 = os.path.exists(os.path.join(根, n))
            print(f"  [{'有' if 存在 else '—'}] {n:<20} {why}")
        return

    if args.show_all:
        print("恢复显示：顶层全部")
        全部 = {n: "" for n in os.listdir(根)
                if not n.startswith("__pycache__")}
        切换(根, 全部, False)
        return

    切换(根, 隐藏清单, not args.show)


if __name__ == "__main__":
    main()
