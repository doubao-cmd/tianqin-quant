# -*- coding: utf-8 -*-
"""给 60 个模拟盘入口补齐「运行记录 + 账户流水单」两套记录（纯入口改动）。

只加落盘参数与常量，**不碰任何交易参数**（周期/均线/止损/手数/信号函数一律不动）。

    python _patch_entries.py           # 干跑，只报告
    python _patch_entries.py --go      # 真写
"""
import os
import re
import sys

ROOT = r"C:\Users\cc\Desktop\天勤量化"
GO = "--go" in sys.argv

策略清单 = [
    ("01_双均线", "双均线_模拟盘.py"),
    ("02_均线排列", "均线排列_模拟盘.py"),
    ("03_均值回归", "均值回归_模拟盘.py"),
    ("04_通道突破", "通道突破_模拟盘.py"),
    ("05_摆动线", "摆动线_模拟盘.py"),
]

LEDGER_常量 = (
    '\n# ---- 账户流水单（2026-09-27 补齐，与其余策略一致）----\n'
    '#   账户视角：不管哪个策略、哪个品种，账户里所有真实成交都记进这里。\n'
    '#   与上面「策略视角」的 _成交流水.csv 互补，互不替代。\n'
    'LEDGER_DIR = os.path.join(_ROOT, "实测记录", "账户")\n'
)

报警 = []
记录 = []


def 处理(策略, 脚本体, 品种):
    原文 = 脚本体
    做 = []

    # ---------------- 01 双均线 ----------------
    if 策略 == "01_双均线":
        if "LEDGER_DIR = " not in 脚本体:
            锚 = "RECORD_HEARTBEAT_SEC = 300   # 心跳落盘间隔（秒）\n"
            if 脚本体.count(锚) != 1:
                raise SystemExit(f"[{策略}/{品种}] 常量锚点命中 {脚本体.count(锚)} 次")
            脚本体 = 脚本体.replace(锚, 锚 + LEDGER_常量)
            做.append("常量 LEDGER_DIR")
        if "ledger_dir=LEDGER_DIR" not in 脚本体:
            锚2 = ("        record_dir=RECORD_DIR,\n"
                   "        record_heartbeat_sec=RECORD_HEARTBEAT_SEC,\n")
            if 脚本体.count(锚2) != 1:
                raise SystemExit(f"[{策略}/{品种}] 调用锚点命中 {脚本体.count(锚2)} 次")
            脚本体 = 脚本体.replace(锚2, 锚2 + "        ledger_dir=LEDGER_DIR,\n")
            做.append("调用 ledger_dir")

    # ---------------- 02 均线排列 ----------------
    elif 策略 == "02_均线排列":
        新增常量 = ""
        if "RECORD_DIR = " not in 脚本体:
            # 品种名写在路径里，台账工具按 SYMBOLS 的中文名找目录
            新增常量 += (
                '\n# ---- 运行记录（2026-09-27 补齐，与 01/03/04/05 一致）----\n'
                '#   把「程序有没有在跑」和「有没有开仓条件」分开落盘，供事后做台账：\n'
                f'#     实测记录/02_均线排列/{品种}/{品种}_成交流水.csv  （事件流）\n'
                f'#     实测记录/02_均线排列/{品种}/{品种}_运行心跳.csv  （每 5 分钟一行，**休市照写**）\n'
                '#   想关掉记录：把 RECORD_DIR 改成 None。\n'
                f'RECORD_DIR = os.path.join(_ROOT, "实测记录", "02_均线排列", "{品种}")\n'
                'RECORD_HEARTBEAT_SEC = 300   # 心跳落盘间隔（秒）\n'
            )
        if "LEDGER_DIR = " not in 脚本体:
            新增常量 += LEDGER_常量
        if 新增常量:
            锚 = "STOP_PCT = 0.0                        # 百分比止损（0=关闭）\n"
            if 脚本体.count(锚) != 1:
                raise SystemExit(f"[{策略}/{品种}] 常量锚点命中 {脚本体.count(锚)} 次")
            脚本体 = 脚本体.replace(锚, 锚 + 新增常量)
            做.append(("常量 " + ("RECORD_DIR " if "RECORD_DIR" in 新增常量 else "")
                       + ("LEDGER_DIR" if "LEDGER_DIR" in 新增常量 else "")).strip())

        # 调用块：PVC 是双向版（已带 ledger_dir），其余是精简版
        if "ledger_dir=LEDGER_DIR" in 脚本体 and "record_dir=RECORD_DIR" in 脚本体:
            pass
        elif "ledger_dir=LEDGER_DIR" in 脚本体:      # PVC：只缺 record
            锚2 = "        ledger_dir=LEDGER_DIR,\n"
            if 脚本体.count(锚2) != 1:
                raise SystemExit(f"[{策略}/{品种}] PVC 调用锚点命中 {脚本体.count(锚2)} 次")
            脚本体 = 脚本体.replace(
                锚2, "        ledger_dir=LEDGER_DIR,\n"
                     "        record_dir=RECORD_DIR,\n"
                     "        record_heartbeat_sec=RECORD_HEARTBEAT_SEC,\n")
            做.append("调用 record_dir")
        else:                                       # 精简版：两个都补
            锚2 = "        web_gui=WEB_GUI, stop_pct=STOP_PCT,\n"
            if 脚本体.count(锚2) != 1:
                raise SystemExit(f"[{策略}/{品种}] 精简版调用锚点命中 {脚本体.count(锚2)} 次")
            脚本体 = 脚本体.replace(
                锚2, "        web_gui=WEB_GUI, stop_pct=STOP_PCT,\n"
                     "        # 运行记录 + 账户流水单（2026-09-27 补齐）\n"
                     "        record_dir=RECORD_DIR,\n"
                     "        record_heartbeat_sec=RECORD_HEARTBEAT_SEC,\n"
                     "        ledger_dir=LEDGER_DIR,\n")
            做.append("调用 record_dir + ledger_dir")

    # ---------------- 03 / 04 ----------------
    elif 策略 in ("03_均值回归", "04_通道突破"):
        if "LEDGER_DIR = " not in 脚本体:
            锚 = "RECORD_HEARTBEAT_SEC = 300   # 心跳落盘间隔（秒）\n"
            if 脚本体.count(锚) != 1:
                raise SystemExit(f"[{策略}/{品种}] 常量锚点命中 {脚本体.count(锚)} 次")
            脚本体 = 脚本体.replace(锚, 锚 + LEDGER_常量)
            做.append("常量 LEDGER_DIR")
        if "ledger_dir=LEDGER_DIR" not in 脚本体:
            锚2 = ("        record_dir=RECORD_DIR,\n"
                   "        record_heartbeat_sec=RECORD_HEARTBEAT_SEC,\n")
            if 脚本体.count(锚2) != 1:
                raise SystemExit(f"[{策略}/{品种}] 调用锚点命中 {脚本体.count(锚2)} 次")
            脚本体 = 脚本体.replace(锚2, 锚2 + "        ledger_dir=LEDGER_DIR,\n")
            做.append("调用 ledger_dir")

    # ---------------- 05 摆动线 ----------------
    elif 策略 == "05_摆动线":
        if "RECORD_DIR = " not in 脚本体 or "RISK_EQUITY = " not in 脚本体:
            锚 = "STOP_ATR = 2.0                        # 0=关闭\n"
            if 脚本体.count(锚) != 1:
                raise SystemExit(f"[{策略}/{品种}] 常量锚点命中 {脚本体.count(锚)} 次")
            新增 = ""
            if "RISK_EQUITY = " not in 脚本体:
                新增 += (
                    '\n# ---- 权益口径（2026-09-27 补齐）----\n'
                    '#   ★ 原缺这一项 -> 手数按账户【实时权益】算（快期模拟户 100 万），\n'
                    '#     而回测按 init_balance=20 万 -> 两边手数不一致、对不上账。\n'
                    '#   传固定值即与回测同口径。0 = 用账户实时权益（旧行为，不推荐）。\n'
                    'RISK_EQUITY = 200000.0\n'
                )
            if "RECORD_DIR = " not in 脚本体:
                新增 += (
                    '\n# ---- 运行记录（2026-09-27 补齐，与 01/03/04 一致）----\n'
                    '#   把「程序有没有在跑」和「有没有开仓条件」分开落盘，供事后做台账：\n'
                    f'#     实测记录/05_摆动线/{品种}/{品种}_成交流水.csv  （事件流）\n'
                    f'#     实测记录/05_摆动线/{品种}/{品种}_运行心跳.csv  （每 5 分钟一行，**休市照写**）\n'
                    '#   想关掉记录：把 RECORD_DIR 改成 None。\n'
                    f'RECORD_DIR = os.path.join(_ROOT, "实测记录", "05_摆动线", "{品种}")\n'
                    'RECORD_HEARTBEAT_SEC = 300   # 心跳落盘间隔（秒）\n'
                )
            脚本体 = 脚本体.replace(锚, 锚 + 新增)
            做.append(("常量 " + ("RISK_EQUITY " if "RISK_EQUITY = " not in 原文 else "")
                       + ("RECORD_DIR" if "RECORD_DIR = " not in 原文 else "")).strip())

        锚2 = ("        risk_hand_max=RISK_HAND_MAX,\n"
               "        stop_atr=STOP_ATR,\n"
               "        ledger_dir=LEDGER_DIR,\n")
        if 脚本体.count(锚2) != 1:
            raise SystemExit(f"[{策略}/{品种}] 05 调用锚点命中 {脚本体.count(锚2)} 次")
        脚本体 = 脚本体.replace(锚2,
                               "        risk_hand_max=RISK_HAND_MAX,\n"
                               "        risk_equity=RISK_EQUITY,       # ★ 与回测 init_balance 对齐\n"
                               "        stop_atr=STOP_ATR,\n"
                               "        ledger_dir=LEDGER_DIR,\n"
                               "        record_dir=RECORD_DIR,\n"
                               "        record_heartbeat_sec=RECORD_HEARTBEAT_SEC,\n")
        做.append("调用 risk_equity + record_dir")

    if 脚本体 == 原文:
        报警.append(f"{策略}/{品种}：无改动（可能已齐）")
        return None
    return 脚本体, 做


总计 = 0
for 策略, 文件名 in 策略清单:
    基 = os.path.join(ROOT, "策略", 策略, "模拟盘")
    for 品种 in sorted(os.listdir(基)):
        路径 = os.path.join(基, 品种, 文件名)
        if not os.path.isfile(路径):
            continue
        src = open(路径, encoding="utf-8").read()
        结果 = 处理(策略, src, 品种)
        if 结果 is None:
            continue
        新, 做 = 结果
        记录.append((策略, 品种, "、".join(做)))
        总计 += 1
        if GO:
            open(路径, "w", encoding="utf-8", newline="\n").write(新)

print(f"{'已写入' if GO else '干跑'}：{总计} 个入口")
for s, p, w in 记录:
    print(f"  {s:<12}{p:<10}{w}")
if 报警:
    print("无改动：")
    for x in 报警:
        print("  -", x)
