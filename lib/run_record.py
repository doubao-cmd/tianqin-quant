# -*- coding: utf-8 -*-
"""
运行归档（回测记录）
==========================================================
把一次回测的产物收进一个**自包含目录**，形成可追溯的历史档案：

    回测记录/<策略>__<品种>/<时间戳>_<样本>/
        说明.md         人读的一页纸：策略/品种/区间/成本口径/备注（先读这个）
        参数.json       本次全部参数（策略参数 + 区间 + 手数口径）+ 代码快照指针
        report.html     回测报告副本（统一改名）
        明细.txt        成交明细副本（逐笔）

与 回测结果/ 的分工（**两者并存，不互相替代**）
----------------------------------------------
    回测结果/   面向「当前状态」——按 策略/品种 组织，同名覆盖，
                只留最新一份 + 汇总页，是 汇总报告.py / 回滚.py 的工作区。
    回测记录/   面向「历史留档」——一次运行一个目录，**永不覆盖**，
                用来回答「当时到底跑的什么参数、什么代码、什么区间」。

不改引擎：报告仍由 engine 落到 回测结果/，本模块只做**复制归档**。
如果源文件不存在（比如引擎改了命名规则），归档照常建目录但跳过复制，
并在说明里写明缺哪一份 —— 不静默失败。
"""
import json
import os
import shutil
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.paths import 回测记录 as _DIR_回测记录
from lib.strategy_dirs import 策略目录名 as _策略目录名


def 归档(策略, 品种中文名, 样本, *, 参数=None, 区间=None, 备注="",
         成本口径="", 风险口径="", 报告=None, 明细=None, 静默=False):
    """把一次运行归档成自包含目录，返回目录路径。

    参数：
        策略/品种中文名 —— 用来命名归档目录（<策略>__<品种>）
        样本           —— IS / VAL / OOS 等，进目录名，便于一眼区分
        参数           —— dict，原样写进 参数.json
        区间           —— (起, 止) 字符串或 date，写进说明
        风险口径       —— ★ 一行话说明「算手数用哪个权益」。**必须传**：
                          2026-09-25 起本项目的风险口径可变（固定金额 / 当前权益），
                          两种口径下的收益率与回撤**不可横向比较** ——
                          不写下来，半年后就分不清哪份记录是哪套口径了。
        报告/明细      —— 源文件绝对路径；不存在则跳过复制并在说明里标注
    """
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    名 = f"{stamp}_{样本}" if 样本 else stamp
    目标 = os.path.join(_ROOT, _DIR_回测记录, f"{策略}__{品种中文名}", 名)
    os.makedirs(目标, exist_ok=True)

    缺 = []
    if 报告:
        if os.path.isfile(报告):
            shutil.copy2(报告, os.path.join(目标, "report.html"))
        else:
            缺.append(f"报告（找的是 {报告}）")
    if 明细:
        if os.path.isfile(明细):
            shutil.copy2(明细, os.path.join(目标, "明细.txt"))
        else:
            缺.append(f"明细（找的是 {明细}）")

    # 代码快照指针：引擎在回测开跑前会自动存一份代码，这里只记「是哪一份」，
    # 不重复复制（快照在 工具/_归档/代码快照/ 下，按 策略/品种/时间戳 分钟级定位）。
    快照 = None
    try:
        from lib import code_snapshot
        快照 = code_snapshot.最新(策略, 品种中文名)
    except Exception:
        pass
    快照路径 = (f"工具/_归档/代码快照/{_策略目录名(策略)}/{品种中文名}/{快照}"
               if 快照 else None)

    with open(os.path.join(目标, "参数.json"), "w", encoding="utf-8") as f:
        json.dump({
            "归档时间": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
            "策略": 策略, "品种": 品种中文名, "样本": 样本,
            "区间": list(区间) if 区间 else None,
            "参数": 参数 or {},
            "风险口径": 风险口径 or "（未记录）",
            "备注": 备注,
            "代码快照": 快照路径,
        }, f, ensure_ascii=False, indent=2)

    lines = [
        f"# {策略} × {品种中文名} — {样本}", "",
        f"- 归档时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 样本：{样本}",
        f"- 区间：{区间[0]} ~ {区间[1]}" if 区间 else "- 区间：（未记录）",
        f"- **风险口径：{风险口径 or '（未记录 —— 无法与其它记录比较）'}**",
        f"- 代码快照：`{快照路径}`" if 快照路径 else "- 代码快照：（未找到）",
        f"- 备注：{备注 or '（无）'}",
        "",
        "> ⚠️ 风险口径自 2026-09-25 起可变（固定金额 ↔ 按当前权益）。",
        "> 两套口径下的收益率 / 回撤 / 手数**不可横向比较**，对比前先看上面这一行。",
        "",
        "## 本目录内容", "",
        "| 文件 | 说明 |",
        "|---|---|",
        "| `说明.md` | 本文件 |",
        "| `参数.json` | 本次全部参数 + 代码快照指针（机器可读） |",
        "| `report.html` | 回测报告（资金曲线 + 逐笔 + 指标卡） |",
        "| `明细.txt` | 逐笔成交明细 |",
        "",
    ]
    if 缺:
        lines += ["## ⚠️ 归档不完整", ""]
        lines += [f"- 缺 {x}" for x in 缺]
        lines += ["", "（多半是引擎命名规则变了，或该次运行没落盘。请核对 回测结果/ 下的实际文件名。）", ""]
    if 成本口径:
        lines += ["## 成本口径", "", 成本口径, ""]
    with open(os.path.join(目标, "说明.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    if not 静默:
        print(f"[归档] 回测记录/{策略}__{品种中文名}/{名}/")
    return 目标
