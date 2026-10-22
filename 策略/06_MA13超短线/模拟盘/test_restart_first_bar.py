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
import datetime as _dt
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

# ---- t8/t9/t10 ★ 2026-10-22 #19 补可观测性：首根必须落一条 SKIP（可正证）----
#   原先 `skip_first` 分支只 return False、不落事件 ⇒「首根不处理」只能反证（无 FILL）。
#   现在要求台账里出现 `SKIP`，备注含「差=…s / 阈值=…s / 陈旧=是|否」。
def _skip_rows(d, name):
    """从临时台账里读出 (全部类型, SKIP 行备注) —— 走**真实落盘**，不 mock 记录器。"""
    p = os.path.join(d, name + "_成交流水.csv")
    kinds, notes = [], []
    with open(p, encoding="utf-8-sig") as fh:
        for ln in fh.read().splitlines()[1:]:
            if not ln.strip():
                continue
            c = ln.split(",")
            kinds.append(c[0])
            if c[0] == "SKIP":
                notes.append(c[-1])
    return kinds, notes


# t8 首根**陈旧**（复现 10-21 18:03：14:55 的 bar 在 18:03:41 才被拿到）⇒ 陈旧=是
_d8 = tempfile.mkdtemp()
_rec8 = M._LiveRecorder(_d8, "沥青")
_now8 = _dt.datetime(2026, 10, 21, 18, 3, 41).timestamp()
_bar8 = {"id": 103, "datetime": int(_dt.datetime(2026, 10, 21, 14, 55).timestamp() * 1e9)}
s8 = {"last_id": -1, "skip_first": True, "name": "沥青", "pos": 0,
      "rec": _rec8, "ksec": 300}
r8 = M._bar_gate(s8, 103, _bar8, _now8)
_rec8.stop("A20 t8 结束")
_k8, _n8 = _skip_rows(_d8, "沥青")
_ok(r8 is False, f"t8 陈旧首根仍不处理（返回 {r8}，控制流未变）")
_ok(len(_n8) == 1, f"t8 落了 SKIP 事件 {len(_n8)} 条")
_ok(bool(_n8) and "陈旧=是" in _n8[0], f"t8 SKIP 备注含「陈旧=是」：{_n8[0] if _n8 else '无'}")
_ok(bool(_n8) and "差=11021s" in _n8[0] and "阈值=600s" in _n8[0],
    "t8 差=11021s / 阈值=600s（与 A21 t4 同一场景、同一数字）")
_ok("STALEBAR" not in _k8,
    "t8 首根走的是 B 分支、**不落 STALEBAR** ⇒ C 的判定未被本改动影响")

# t9 首根**新鲜**（该 bar 刚收盘 120s）⇒ 陈旧=否
_d9 = tempfile.mkdtemp()
_rec9 = M._LiveRecorder(_d9, "甲醇")
_now9 = _dt.datetime(2026, 10, 22, 9, 5, 0).timestamp()
_bar9 = {"id": 201, "datetime": int((_now9 - 300 - 120) * 1e9)}   # 结束于 now-120s
s9 = {"last_id": -1, "skip_first": True, "name": "甲醇", "pos": 0,
      "rec": _rec9, "ksec": 300}
r9 = M._bar_gate(s9, 201, _bar9, _now9)
_rec9.stop("A20 t9 结束")
_k9, _n9 = _skip_rows(_d9, "甲醇")
_ok(r9 is False and s9["skip_first"] is False, f"t9 新鲜首根不处理、标记已清（返回 {r9}）")
_ok(bool(_n9) and "陈旧=否" in _n9[0], f"t9 SKIP 备注含「陈旧=否」：{_n9[0] if _n9 else '无'}")
_ok(bool(_n9) and "差=120s" in _n9[0], "t9 差=120s（未超 600s 阈值）")

# t10 bar=None（A20 其余用例的调用方式）⇒ 仍 return False，且观测不抛异常
s10 = {"last_id": -1, "skip_first": True, "name": "豆粕"}
try:
    r10 = M._bar_gate(s10, 300)
    _ok(r10 is False, f"t10 bar=None 照常返回 False（{r10}）")
except Exception as e:
    _ok(False, f"t10 bar=None 不应抛异常：{type(e).__name__}: {e}")

print("=" * 72)
if FAIL:
    print(f"失败 {len(FAIL)} 项：")
    for m in FAIL:
        print("  -", m)
    sys.exit(1)
print("10/10 通过（21 项断言；t8/t9 走真实落盘台账验证 SKIP 备注）")
