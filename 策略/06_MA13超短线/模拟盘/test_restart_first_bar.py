# -*- coding: utf-8 -*-
"""A20 · #19 重启后首根 bar 不得被重放（2026-10-21 D1）

背景（实测）：`last_id` 仅存内存、重启后为 -1；`skip_first` 原来只在**同进程重连**
分支置位（`ma13_live_multi.py:953`），**进程重启**分支初始化为 False ⇒ 重启后把最后
一根**已收盘**的 bar 当成新 bar 重放。

实证 2026-10-21 18:03:41：核心于 18:02:45 重启，BU+8 / EG-7 依据 **14:59:59.8** 的 bar
在**收盘后 3 小时**成交（`SIGNAL 时刻=18:03:41 / 信号根=14:59:59.8`）。

修法：`_init_spec_state` 里 `skip_first` 初始化 False→True，两条路径对齐；
并把判定抽成 `_bar_gate` 使其可单测（逻辑一字未改）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ma13_live_multi as M   # noqa: E402

FAIL = []


def _ok(cond, msg):
    if cond:
        print(f"  PASS  {msg}")
    else:
        print(f"  FAIL  {msg}")
        FAIL.append(msg)


def _old_gate(s, bar_id):
    """修复前的判定（内联在 _process 里）：**不看 skip_first**。用于复现 bug。"""
    if bar_id == s["last_id"] or bar_id < 0:
        return False
    s["last_id"] = bar_id
    return True


print("=" * 72)
print("A20 · #19 重启后首根 bar 不得被重放")
print("=" * 72)

# ---- t1 启动首根：必须只认账不处理 ----
s = {"last_id": -1, "skip_first": True}
r = M._bar_gate(s, 100)
_ok(r is False, f"t1 启动首根不处理（返回 {r}）")
_ok(s["last_id"] == 100, f"t1 但 last_id 已记账 = {s['last_id']}")
_ok(s["skip_first"] is False, "t1 skip_first 已清标记")

# ---- t2 同一根再来：仍然不处理 ----
r = M._bar_gate(s, 100)
_ok(r is False, f"t2 同一根 bar 不重复处理（返回 {r}）")
_ok(s["last_id"] == 100, "t2 last_id 未变")

# ---- t3 下一根：正常处理 ----
r = M._bar_gate(s, 101)
_ok(r is True, f"t3 下一根正常处理（返回 {r}）")
_ok(s["last_id"] == 101, "t3 last_id 前移")

# ---- t4 未成形 bar（id<0）：跳过且不动 last_id ----
s2 = {"last_id": 101, "skip_first": False}
r = M._bar_gate(s2, -1)
_ok(r is False and s2["last_id"] == 101, f"t4 未成形 bar 跳过、last_id 不动（返回 {r}）")

# ---- t5 重连路径：置位后首根同样只认账 ----
s3 = {"last_id": 100, "skip_first": True}
r = M._bar_gate(s3, 101)
_ok(r is False and s3["last_id"] == 101 and s3["skip_first"] is False,
    f"t5 重连首根只认账不处理（返回 {r}）")

# ---- t6 初始化必须产出 skip_first=True（防有人改回 False）----
d = tempfile.mkdtemp()
s4 = {"key": "MA", "name": "甲醇", "cont": "CZCE.MA701", "mult": 10,
      "tick": 1, "comm": 2.0}
try:
    M._init_spec_state(s4, d, None, 300)
    _ok(s4.get("skip_first") is True,
        f"t6 _init_spec_state 产出 skip_first={s4.get('skip_first')}")
    _ok(s4.get("last_id") == -1, f"t6 last_id 初值 = {s4.get('last_id')}")
except Exception as e:
    _ok(False, f"t6 _init_spec_state 调用失败：{type(e).__name__}: {e}")

# ---- t7 复现旧 bug：同场景旧逻辑会放行 ----
s5 = {"last_id": -1, "skip_first": True}
r_old = _old_gate(s5, 100)
_ok(r_old is True, f"t7 旧逻辑会把重启首根当新 bar 处理（返回 {r_old}）⇒ bug 可复现")

print("=" * 72)
if FAIL:
    print(f"失败 {len(FAIL)} 项：")
    for m in FAIL:
        print("  -", m)
    sys.exit(1)
print("7/7 通过")
