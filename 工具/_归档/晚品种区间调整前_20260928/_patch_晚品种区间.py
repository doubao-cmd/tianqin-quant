# -*- coding: utf-8 -*-
"""把纯碱 / 苯乙烯 / 生猪 三个「晚上市品种」的练习区间单独后移。

背景（2026-09-28 用户拍板）
--------------------------------------------------------------
这仨品种在 2016-2019 尚无行情 ——
    纯碱   CZCE.SA  2019-12-05 上市
    苯乙烯 DCE.eb   2019-09-25 上市
    生猪   DCE.lh   2021-01-07 上市
所以在原练习区间 2016-01-01 ~ 2019-12-31 上「一根 K 线都没有」，
回测起不来（区间空）。用户决定：起点拉到 2020，且不与其余 9 个品种同步。

改法（15 个文件 = 5 策略 × 3 品种；只动区间常量与紧跟的注释，其余一个字节不碰）
--------------------------------------------------------------
    IS_START   2016-01-01  ->  2020-01-01
    IS_END     2019-12-31  ->  2022-12-31      （练习 3 年）
    VAL_START  2020-01-01  ->  2023-01-01      （验证集顺延为 2023-2024）
    VAL_END    2024-12-31  ->  不动
    OOS_*                     不动（样本外，不得触碰）

用法
--------------------------------------------------------------
    python _patch_晚品种区间.py            # 干跑：只报告将要改什么
    python _patch_晚品种区间.py --go       # 真改（先把 15 份原件拷进本目录）
"""
import os
import re
import shutil
import sys

本目录 = os.path.dirname(os.path.abspath(__file__))
_ROOT = 本目录
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)

策略表 = ["01_双均线", "02_均线排列", "03_均值回归", "04_通道突破", "05_摆动线"]
脚本名 = {"01_双均线": "双均线_回测.py", "02_均线排列": "均线排列_回测.py",
          "03_均值回归": "均值回归_回测.py", "04_通道突破": "通道突破_回测.py",
          "05_摆动线": "摆动线_回测.py"}
品种表 = {
    "纯碱":   "CZCE.SA 2019-12-05 上市",
    "苯乙烯": "DCE.eb 2019-09-25 上市",
    "生猪":   "DCE.lh 2021-01-07 上市",
}

新_IS_START = "date(2020, 1, 1)"
新_IS_END = "date(2022, 12, 31)"
新_VAL_START = "date(2023, 1, 1)"

说明行 = [
    "# ★★ 本品种上市晚（%s），2016-2019 一根行情都没有；",
    "#    2026-09-28 用户拍板：本品种练习区间单独后移，不与其余 9 个品种同步"
    "（其余仍是 2016-2019）。",
]


def 路径(策略, 品种):
    return os.path.join(_ROOT, "策略", 策略, "回测", 品种, 脚本名[策略])


def 改一个(src, 品种):
    """逐行处理：区间常量只换 date(...) 部分，行尾注释原样保留。"""
    行 = src.split("\n")
    出 = []
    计数 = {"IS_START": 0, "IS_END": 0, "VAL_START": 0}
    for L in 行:
        # 在「切分」说明行之后插入本品种的例外说明（只插一次）
        if "切分" in L and "样本外仅用于最终验证" in L:
            出.append(L)
            for t in 说明行:
                出.append(t % 品种 if "%s" in t else t)
            continue
        # 区间常量
        if L.startswith("IS_START"):
            L, n = re.subn(r"date\([^)]*\)", 新_IS_START, L, count=1)
            计数["IS_START"] += n
        elif L.startswith("IS_END"):
            L, n = re.subn(r"date\([^)]*\)", 新_IS_END, L, count=1)
            计数["IS_END"] += n
        elif L.startswith("VAL_START"):
            L, n = re.subn(r"date\([^)]*\)", 新_VAL_START, L, count=1)
            计数["VAL_START"] += n
        # 练习/验证 三行注释同步改口径
        elif re.match(r"^#\s*练习[（(]样本内[)）]\s*=", L):
            L = ("# 练习(样本内)     = 2020-01-01 ~ 2022-12-31   3 年；"
                 "★ 本品种上市晚，区间单独后移（2026-09-28 用户拍板）")
        elif re.match(r"^#\s*验证集\s*=", L):
            L = "# 验证集           = 2023-01-01 ~ 2024-12-31   2 年；由原「验证集 2020-2024」顺延而来"
        elif re.match(r"^#\s*期末考试[（(]样本外[)）]\s*=", L):
            L = "# 期末考试(样本外) = 2025-01-01 ~ 2026-09-30   21 个月；本次未动、不得提前查看"
        出.append(L)
    return "\n".join(出), 计数


def 检查(新文, 品种):
    """改完自检：新值在、旧值不在、OOS 没被动。"""
    问题 = []
    if 新文.count("IS_START  = " + 新_IS_START) != 1:
        问题.append("IS_START 未落到新值")
    if 新文.count("IS_END    = " + 新_IS_END) != 1:
        问题.append("IS_END 未落到新值")
    if 新文.count("VAL_START = " + 新_VAL_START) != 1:
        问题.append("VAL_START 未落到新值")
    if "VAL_END   = date(2024, 12, 31)" not in 新文:
        问题.append("VAL_END 被动了（应为 2024-12-31）")
    if "OOS_START = date(2025, 1, 1)" not in 新文 or "OOS_END   = date(2026, 9, 30)" not in 新文:
        问题.append("OOS 区间被动了（不得触碰）")
    if "date(2016, 1, 1)" in 新文:
        问题.append("仍残留 date(2016, 1, 1)")
    if "date(2019, 12, 31)" in 新文:
        问题.append("仍残留 date(2019, 12, 31)")
    if "日期" not in 新文 and 品种 not in 新文:
        问题.append("例外说明行没写进去")
    return 问题


def main():
    真改 = "--go" in sys.argv
    print("=" * 74)
    print("  晚上市品种（纯碱/苯乙烯/生猪）区间单独后移  " + ("【真改】" if 真改 else "【干跑】"))
    print("=" * 74)
    print("  IS 2016-01-01~2019-12-31  ->  2020-01-01~2022-12-31")
    print("  VAL 2020-01-01~2024-12-31 ->  2023-01-01~2024-12-31")
    print("  OOS 2025-01-01~2026-09-30 ->  不动")
    print("-" * 74)

    n改 = 0
    问题总 = []
    for 策略 in 策略表:
        for 品种 in 品种表:
            p = 路径(策略, 品种)
            if not os.path.exists(p):
                print("  [跳过] 不存在 %s" % os.path.relpath(p, _ROOT))
                continue
            原件 = open(p, encoding="utf-8").read()
            新文, 计数 = 改一个(原件, 品种)
            if 计数 != {"IS_START": 1, "IS_END": 1, "VAL_START": 1}:
                问题总.append("%s 命中数异常 %s" % (os.path.relpath(p, _ROOT), 计数))
                continue
            问题 = 检查(新文, 品种)
            if 问题:
                问题总.append("%s -> %s" % (os.path.relpath(p, _ROOT), "；".join(问题)))
                continue
            # 只应有 5 处增删：3 个常量 + 3 行注释改成 3 行 + 插入 2 行 = 净 +2 行
            if len(新文.split("\n")) != len(原件.split("\n")) + 2:
                问题总.append("%s 行数不匹配（应净 +2）" % os.path.relpath(p, _ROOT))
                continue
            if 真改:
                # 备份原件（镜像两级目录）
                备份 = os.path.join(本目录, "策略", 策略, "回测", 品种)
                os.makedirs(备份, exist_ok=True)
                shutil.copy2(p, os.path.join(备份, 脚本名[策略]))
                # 写回：LF、UTF-8、不留 BOM
                with open(p, "w", encoding="utf-8", newline="\n") as f:
                    f.write(新文)
            n改 += 1
            print("  [%s] %s / %s" % ("改" if 真改 else "将改", 策略, 品种))

    print("-" * 74)
    if 问题总:
        print("  ✗ 有问题 %d 条：" % len(问题总))
        for q in 问题总:
            print("     " + q)
        return 1
    print("  %s %d / %d 个文件，全部自检通过。" %
          ("已修改" if 真改 else "待修改", n改, len(策略表) * len(品种表)))
    if not 真改:
        print("  这是干跑。要真改请加 --go")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
