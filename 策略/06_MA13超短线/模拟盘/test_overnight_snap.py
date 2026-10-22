# -*- coding: utf-8 -*-
"""#22-C 隔夜快照落盘节流 / 时点窗口 回归测试（2026-10-22）

背景（两个真实缺陷，均在 2026-10-22 实盘日志与台账里被抓到）：

  【A】刷屏：`_periodic_risk` 只在 `ok=True` 时置位 `_RSK_STATE["snap_day"]`。
      幂等命中（文件里已有今天这份）返回 `(False,"本交易日已快照")` ⇒ 不置位
      ⇒ 每 60s 重试一次、每次打一行。实测 2026-10-22 日志 350 行里 198 行
      （57%）是这句，把真事故日志淹了。

  【B】时点错位：触发条件是 `hour >= 15`，而 `trading_day()` 在 `hour >= 20`
      会 +1 天。实测 2026-10-21 20:00:37（进程重启后第一时间跑）写了一份
      `trade_day=20261022` 的快照，把 10-22 的额度提前消费掉 ⇒ 10-22 15:00 后
      trading_day 仍是 20261022 ⇒ 幂等命中 ⇒ **10-22 的真收盘快照整天没写**。
      台账里留着的是「10-21 夜盘持仓」标成 10-22（FU/SA 方向与实际相反、缺 M）。

断言（对应规则 F：疏漏固化为断言）：
  t1 窗口内、文件不存在              ⇒ 真写盘 + 打印「已落盘」+ 置位
  t2 窗口内、文件已存在（模拟重启）  ⇒ 打印「已快照（重启前已落盘」+ **置位**
  t3 t2 之后再次调用                  ⇒ **完全无输出**（刷屏已停）
  t4 窗口外（20:30，夜盘后）          ⇒ 不写盘、不打印
  t5 真失败（record_dir 不可写）      ⇒ 仍打印「未落盘」，不被误判为「已办完」

跑法：python test_overnight_snap.py
"""
import contextlib
import datetime as _real_dt
import io
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import ma13_live_multi as m                      # noqa: E402
from lib import overnight as _ovn                # noqa: E402

_FAIL = []


def _ok(cond, msg):
    if cond:
        print(f"  PASS  {msg}")
    else:
        print(f"  FAIL  {msg}")
        _FAIL.append(msg)


def _call(record_dir):
    """调一次 _periodic_risk，返回它打到 stdout 的全部文本。"""
    m._RSK_STATE["last"] = 0.0                   # 绕开 60s 节流
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m._periodic_risk(None, [], {}, None, record_dir, False)
    return buf.getvalue()


def _fresh_state():
    m._RSK_STATE["last"] = 0.0
    m._RSK_STATE["snap_day"] = None              # 模拟「进程刚重启，内存清零」


def t1_窗口内_文件不存在_真写盘():
    _fresh_state()
    d = tempfile.mkdtemp(prefix="snapfix_")
    out = _call(d)
    pj = os.path.join(d, _ovn.SNAP_JSON)
    _ok(os.path.isfile(pj), f"t1 窗口内应真写盘：{pj}")
    _ok("已落盘" in out, f"t1 应打印「已落盘」，实得：{out.strip()[:80]!r}")
    _ok(m._RSK_STATE["snap_day"] is not None, "t1 成功后应置位 snap_day")
    print(f"  PASS  t1_窗口内_文件不存在_真写盘")


def t2_幂等命中_必须置位():
    _fresh_state()                               # 重启：内存清零，但文件还在
    d = tempfile.mkdtemp(prefix="snapfix_")
    _ovn.snapshot(d, str(m._rs.trading_day()), [], 100000.0)   # 模拟「重启前已落盘」
    out = _call(d)
    _ok("已快照（重启前已落盘" in out,
        f"t2 幂等命中应打印「已快照（重启前已落盘」，实得：{out.strip()[:90]!r}")
    _ok(m._RSK_STATE["snap_day"] is not None,
        "t2 ★幂等命中必须置位 snap_day（不置位 = 每 60s 刷一行）")
    print(f"  PASS  t2_幂等命中_必须置位")


def t3_置位之后_再次调用_零输出():
    _fresh_state()
    d = tempfile.mkdtemp(prefix="snapfix_")
    _ovn.snapshot(d, str(m._rs.trading_day()), [], 100000.0)
    _call(d)                                     # 第一次：提示一行并置位
    out2 = _call(d)                              # 第二次：应完全安静
    _ok(out2.strip() == "", f"t3 ★置位后不应再有输出，实得：{out2.strip()[:90]!r}")
    print(f"  PASS  t3_置位之后_再次调用_零输出")


def t4_窗口外_2030_不写盘():
    _fresh_state()
    d = tempfile.mkdtemp(prefix="snapfix_")
    real_now = _real_dt.datetime.now()
    # 造一个「20:30」的 datetime 模块替身：只覆盖 .now()，其余属性照抄真模块
    fake = types.SimpleNamespace(
        **{k: getattr(_real_dt, k) for k in dir(_real_dt) if not k.startswith("_")}
    )

    class _DT(_real_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return real_now.replace(hour=20, minute=30, second=0, microsecond=0)

    fake.datetime = _DT
    _orig = m._dt
    m._dt = fake
    try:
        out = _call(d)
    finally:
        m._dt = _orig
    pj = os.path.join(d, _ovn.SNAP_JSON)
    _ok(not os.path.isfile(pj),
        "t4 ★20:30（夜盘已起，trading_day 会+1天）不应写快照")
    _ok("隔夜快照" not in out, f"t4 窗口外不应打印快照相关日志，实得：{out.strip()[:90]!r}")
    print(f"  PASS  t4_窗口外_2030_不写盘")


def t5_真失败_仍报未落盘():
    _fresh_state()
    # record_dir 指向一个「文件名已被普通文件占用」的路径 ⇒ makedirs 必失败
    blocker = os.path.join(tempfile.mkdtemp(prefix="snapfix_"), "blocked")
    with open(blocker, "w", encoding="utf-8") as f:
        f.write("x")
    out = _call(blocker)
    _ok("未落盘" in out or "异常" in out,
        f"t5 真失败必须仍可见（不许被当成「已办完」），实得：{out.strip()[:90]!r}")
    _ok(m._RSK_STATE["snap_day"] is None,
        "t5 真失败时**不得**置位（否则当天再也不会重试）")
    print(f"  PASS  t5_真失败_仍报未落盘")


def t6_提前消费的脏快照_必须重写():
    """复刻 2026-10-21 20:00:37 事故：ts 是**前一自然日 20:00**，却标成**今天**。

    `trading_day()` 在 hour>=20 会 +1 天 ⇒ 夜盘一启动就写了「次日」的份。
    这种快照必须被判为不合法并覆盖重写，否则今天的真收盘快照永远写不进去。
    """
    _fresh_state()
    d = tempfile.mkdtemp(prefix="snapfix_")
    today = str(m._rs.trading_day())
    pj = os.path.join(d, _ovn.SNAP_JSON)
    y = (m._dt.datetime.now() - m._dt.timedelta(days=1)).strftime("%Y-%m-%d")
    dirty = {"ts": f"{y} 20:00:37", "trade_day": today, "equity": 1.0,
             "legs": [], "total_notional": 0.0, "total_ratio": None}
    with open(pj, "w", encoding="utf-8") as f:
        json.dump(dirty, f, ensure_ascii=False)
    out = _call(d)
    _ok("已落盘" in out,
        f"t6 ★前一晚 20:00 写、却标成今天的脏快照必须重写，实得：{out.strip()[:90]!r}")
    with open(pj, "r", encoding="utf-8") as f:
        after = json.load(f)
    _ok(str(after.get("ts", "")).startswith(f"{today[:4]}-{today[4:6]}-{today[6:]}"),
        f"t6 重写后 ts 应落在今天，实得 {after.get('ts')!r}")
    print(f"  PASS  t6_提前消费的脏快照_必须重写")


if __name__ == "__main__":
    for fn in (t1_窗口内_文件不存在_真写盘, t2_幂等命中_必须置位,
               t3_置位之后_再次调用_零输出, t4_窗口外_2030_不写盘,
               t5_真失败_仍报未落盘, t6_提前消费的脏快照_必须重写):
        try:
            fn()
        except Exception as e:                   # noqa: BLE001
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
            _FAIL.append(fn.__name__)
    print()
    if _FAIL:
        print(f"FAILED {len(_FAIL)}: " + "; ".join(_FAIL))
        sys.exit(1)
    print("ALL PASS (6/6)")
