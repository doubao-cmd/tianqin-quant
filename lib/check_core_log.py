# -*- coding: utf-8 -*-
"""
A9 巡检断言（2026-10-20 派单 T3）：核心日志必须当日存在。
================================================================
判据（派单 §二 A9）：
  · 存在 `台账/核心输出_YYYYMMDD.log`（YYYYMMDD = 今日）
  · 文件 mtime 为当日（>= 今日 00:00 且 <= 现在）
（派单 T3 #1 验收还要求「日志里能 grep 到 [LIMITCHK]」，但该标记只在
 单日亏损阀触发时才写入，故作为可选 --marker 开关，仅在触发验收时启用。）

纯标准库，离线可跑（不需 tqsdk）。
用法：
    python lib/check_core_log.py                  # 退出码 0=通过 / 1=失败，打印 JSON
    python lib/check_core_log.py --marker LIMITCHK # 额外要求日志含该标记（触发验收用）
"""
import os
import sys
import json
import argparse
import datetime as _dt

_HERE = os.path.dirname(os.path.abspath(__file__))
# 台账目录：与 ma13_live_multi 同约定（06 模拟盘/台账）
_CANDIDATES = [
    os.path.join(_HERE, "策略", "06_MA13超短线", "模拟盘", "台账"),
    os.path.join(os.path.dirname(_HERE), "策略", "06_MA13超短线", "模拟盘", "台账"),
]


def check_core_log(marker=None):
    today = _dt.date.today().strftime("%Y%m%d")
    now = _dt.datetime.now()
    logdir = None
    for c in _CANDIDATES:
        if os.path.isdir(c):
            logdir = c
            break
    if logdir is None:
        return False, f"找不到台账目录（候选：{_CANDIDATES}）"
    path = os.path.join(logdir, f"核心输出_{today}.log")
    if not os.path.exists(path):
        return False, f"缺当日核心日志：{path}"
    mt = _dt.datetime.fromtimestamp(os.path.getmtime(path))
    if mt.date() != now.date():
        return False, f"日志 mtime 非当日：{mt.isoformat()}（今日 {today}）"
    if marker is not None:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                if marker not in f.read():
                    return False, f"日志存在但无标记 [{marker}]（触发验收未满足？）"
        except Exception as e:                       # noqa: BLE001
            return False, f"读日志失败：{type(e).__name__}: {e}"
    return True, f"核心日志存在且 mtime 当日：{path}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--marker", default=None,
                    help="额外要求日志含此标记（如 LIMITCHK），仅触发验收时用")
    a = ap.parse_args()
    ok, msg = check_core_log(marker=a.marker)
    print(json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
