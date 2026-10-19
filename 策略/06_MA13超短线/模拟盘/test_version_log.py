# -*- coding: utf-8 -*-
"""
test_version_log.py —— 离线单测：版本切换留档 `lib/version_log.log_version_switch`
====================================================================================
被测对象：`lib/version_log.py`（A11 版本分段的数据源，2026-10-19 新增）

要证明的事：
  ① 空台账 → 写首条，表头齐全、utf-8-sig 可读（下游 build_data 用 utf-8-sig 解析）；
  ② **同口径重启不写**（重启 ≠ 切换），否则 6 天一次重选、每天重启会把台账刷成噪声；
  ③ 口径变了 → 追加一行，且旧行原样保留（历史不可改）；
  ④ 手工补录行（无「来源」列的老格式）+ 同口径 → 只**改时间**为本次启动时刻，
     行数不变，原时间写进备注（"代码落地"≠"生效"这件事必须在台账里留痕）；
  ⑤ 修正只做一次（改完来源变「启动自记录」，下次同口径就是 unchanged）；
  ⑥ 下游解析口径一致：记录时间[:16] 能取到、'True'/'False' 能判成 on/off，
     合并相邻同 on 后段数正确；
  ⑦ 目录不可写时返回 error 且**不抛异常**（这条路径在主循环之前，抛了就等于起不来）。

跑法（纯标准库，任何 python 都行）：
    python test_version_log.py
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.version_log import COLS, FILE_NAME, log_version_switch, _on_of

_OK = _BAD = 0


def ok(cond, msg):
    global _OK, _BAD
    if cond:
        _OK += 1
        print(f"  ✓ {msg}")
    else:
        _BAD += 1
        print(f"  ✗ {msg}")


def rows_of(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def parse_like_build_data(path):
    """照抄 build_data._version_switch 的口径（记录时间[:16] + on/off + 合并相邻同 on）。"""
    sw = []
    for r in rows_of(path):
        ts = str(r.get("记录时间") or "").strip()[:16]
        if len(ts) < 16:
            continue
        sw.append({"t": ts, "on": str(r.get("INTRABAR_STOP") or "").strip().lower() == "true"})
    sw.sort(key=lambda x: x["t"])
    merged = []
    for s in sw:
        if merged and merged[-1]["on"] == s["on"]:
            continue
        merged.append(s)
    return merged


T1 = datetime(2026, 10, 18, 22, 6, 20)
T2 = datetime(2026, 10, 19, 8, 55, 0)
T3 = datetime(2026, 10, 19, 9, 30, 0)
T4 = datetime(2026, 10, 20, 8, 55, 0)

tmp = tempfile.mkdtemp(prefix="verlog_")
path = os.path.join(tmp, FILE_NAME)
fake_code = os.path.join(tmp, "fake_strategy.py")
with open(fake_code, "w", encoding="utf-8") as fh:
    fh.write("INTRABAR_STOP = True\n")

print("[A11] 版本切换留档")

# ① 手工补录行（模拟当前真实台账：3 列、无来源列）
with open(path, "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["记录时间", "INTRABAR_STOP", "备注"])
    w.writerow(["2026-10-18 22:06:20", "True", "INTRABAR_STOP 改为 True（代码落地时刻，进程仍在跑旧码）"])
ok(len(rows_of(path)) == 1, "起始：手工补录 1 行（老格式 3 列）")

# ④ 同口径启动 → 只修正时间
r = log_version_switch(tmp, True, [fake_code], pool=["JM", "M"], now=T2)
rws = rows_of(path)
ok(r["action"] == "correct", f"同口径 + 手工行 ⇒ 修正生效时刻（实际 {r['action']}）")
ok(len(rws) == 1, f"修正不新增行（{len(rws)} 行）")
ok(rws[0]["记录时间"] == T2.strftime("%Y-%m-%d %H:%M:%S"),
   f"记录时间改为本次启动时刻（{rws[0]['记录时间']}）")
ok("2026-10-18 22:06:20" in rws[0]["备注"], "原手工时间留在备注里（可追溯，不篡改历史）")
ok(rws[0]["来源"] == "启动自记录", f"来源标记为启动自记录（{rws[0]['来源']}）")
ok(len(rws[0]["代码指纹"]) == 12, "写了代码指纹（追溯用，不参与分段）")

# ⑤ 修正只做一次
r = log_version_switch(tmp, True, [fake_code], pool=["JM", "M"], now=T3)
ok(r["action"] == "unchanged", f"再次同口径 ⇒ 不写（实际 {r['action']}）")
ok(len(rows_of(path)) == 1, "行数仍为 1（重启不刷屏）")

# ③ 口径变化 → 追加
r = log_version_switch(tmp, False, [fake_code], pool=["JM", "M"], now=T4)
rws = rows_of(path)
ok(r["action"] == "append", f"口径 True→False ⇒ 追加一行（实际 {r['action']}）")
ok(len(rws) == 2, f"共 2 行（{len(rws)}）")
ok(rws[0]["记录时间"].startswith("2026-10-19"), "旧行原样保留（历史不可改）")

# ⑥ 下游解析口径
sw = parse_like_build_data(path)
ok([x["on"] for x in sw] == [True, False], f"解析出 2 段、方向正确（{[x['on'] for x in sw]}）")
ok(sw[0]["t"] == "2026-10-19 08:55", f"切换点取到启动时刻（{sw[0]['t']}）")

# 表头/编码
with open(path, "rb") as fh:
    raw = fh.read(3)
ok(raw == b"\xef\xbb\xbf", "文件保持 utf-8-sig（BOM），与 build_data 读取一致")
ok(list(rows_of(path)[0].keys()) == COLS, "列顺序与 COLS 一致（前两列是解析器认的）")

# ⑦ 写失败不抛
bad = os.path.join(tmp, "no_such_dir")
r = log_version_switch(bad, True, [fake_code], now=T2)
ok(r["action"] == "error" and "错误" not in r["action"], f"目录不可写 ⇒ error 不抛异常（{r['why'][:40]}）")

print(f"\n{'全部通过' if _BAD == 0 else '有失败'}：{_OK} 通过 / {_BAD} 失败")
raise SystemExit(0 if _BAD == 0 else 1)
