# -*- coding: utf-8 -*-
"""
test_adopt_no_exit.py —— 离线单测：接管当根不得判出场
=========================================================
事故复盘（2026-10-18 22:02 实盘实测，真实发生过）

  鸡蛋 JD 空 8 手被换池保护接管（CARRIED → RECOVER），
  止损由「前 5 根的 swing ± 0.5ATR」现算得 3335.0；
  **紧接着用当根（周五最后一根）的 high 去判它是否被击穿** —— 而当根 high 早已越过 3335。
  ⇒ 开机 30 秒就发了一笔平仓单，云端并未成交（持仓仍 -8），
    台账却写了 FILL / STOPLOSS / FILLX 三行，本地 pos 归零、与云端脱钩，
    随后又被 RECOVER 拉回，同一根重复接管。

  这是**逻辑上不可能成立的自证**（不是数据未来函数）：
  止损在用「截至上一根」的信息计算，就不能拿「已经走完的当根」反证它被击穿。

修法：接管时置 `s["_skip_exit_once"] = True`，`_process` 里在出场判定处 consume 一次。

本测试用源码静态断言 + 纯函数复算两条腿来锁死这个行为（_process 依赖 tqsdk 运行时，
不适合整体桩测）。

跑法（需 tqsdk 解释器，因为模块顶层 import tqsdk）：
    %USERPROFILE%\\.workbuddy\\binaries\\python\\envs\\tqsdk\\python.exe test_adopt_no_exit.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
for _p in (_HERE, _ROOT, os.path.join(_ROOT, "策略", "06_MA13超短线", "工作台")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import ma13_live_multi as m
except Exception as e:
    print(f"SKIP: 无法导入 ma13_live_multi（{type(e).__name__}: {e}）")
    raise SystemExit(0)

import numpy as np


# ---------------------------------------------------------------- 行为断言
def test_recover_sets_skip_flag():
    """接管**默认**跳过当根；但「现价已穿越止损」时例外（E4，2026-10-21）。

    ★ 从源码字符串断言改为行为断言：原断言 `'s["_skip_exit_once"] = True' in src`
      在 E4 把赋值改成 `= not _pen` 后直接失效，而且它本来就测不到真实行为。

    三种路径都必须锁住：
      ① 路径 B（无记录、重算 stop 在现价另一侧）⇒ 跳过（JD 事故的防线）
      ② 路径 A（有记录、现价未穿计划止损）      ⇒ 跳过
      ③ 路径 A（有记录、**现价已穿计划止损**）   ⇒ **不跳过**（13:32 FU 实测：
         跳过多等一根，成交 4804 而非 ~4815，多亏 11.64 点 ≈ 815 元）
    """
    import tempfile

    import pandas as pd

    from lib import plan_stop as _ps

    rd = tempfile.mkdtemp(prefix="adopt_")

    def _mk():
        n = 40
        k = pd.DataFrame({"low": np.full(n, 3240.0), "high": np.full(n, 3245.0)})

        class _Q:
            last_price = 3250.0
            underlying_symbol = "DCE.m2701"
            datetime = "2026-10-21 09:10:00"

        class _Rec:
            def event(self, *a, **kw):
                pass

        return {"q": _Q(), "k": k, "record_dir": rd, "key": "M", "name": "豆粕",
                "rec": _Rec(), "stop": 0.0, "entry": 0.0, "bars_in": 0}

    # ① 路径 B：无记录 ⇒ 重算 stop 必在现价下方 ⇒ 必须跳过
    s = _mk()
    m._attach_stop_on_boot(s, 8, 3250.0, 52.0)
    assert s.get("_skip_exit_once") is True, "路径B 接管当根未跳过 ⇒ JD 假平仓事故会重演"
    assert s["stop"] < 3250.0, f"路径B 重算 stop 应低于现价，实际 {s['stop']}"

    # ② 路径 A：有记录且现价未穿损 ⇒ 跳过，且止损原样恢复
    _ps.save(rd, "M", "DCE.m2701", 3200.0, 3230.0, 1, 8, "2026-10-21 09:10:00")
    s = _mk()
    m._attach_stop_on_boot(s, 8, 3250.0, 52.0)
    assert abs(s["stop"] - 3200.0) < 1e-9, f"未原样恢复：{s['stop']}"
    assert s["_skip_exit_once"] is True, "现价未穿损却未跳过 ⇒ 会拿历史 bar 反证（JD 事故）"

    # ③ 路径 A：现价已穿计划止损（3250 ≤ 3260）⇒ 必须允许当根出场
    _ps.save(rd, "M", "DCE.m2701", 3260.0, 3230.0, 1, 8, "2026-10-21 09:10:00")
    s = _mk()
    m._attach_stop_on_boot(s, 8, 3250.0, 52.0)
    assert abs(s["stop"] - 3260.0) < 1e-9, f"未原样恢复：{s['stop']}"
    assert s["_skip_exit_once"] is False, \
        "现价已穿计划止损却仍跳过当根 ⇒ 多等一根 bar（13:32 FU 因此多亏 815 元）"
    print("  PASS  test_recover_sets_skip_flag（路径B跳过 / A未穿跳过 / A穿损不跳过）")


def test_skip_consumed_before_exit_hit():
    """consume 必须发生在 exit_hit 之前，且是「跳过」而非「照判」。"""
    src = open(os.path.join(_HERE, "ma13_live_multi.py"), encoding="utf-8").read()
    i_pop = src.find('s.pop("_skip_exit_once", False)')
    i_hit = src.find("why = sig.exit_hit(ind, i, pos, s[\"stop\"], s[\"bars_in\"])")
    assert i_pop > 0, "找不到 consume 位置"
    assert i_hit > 0, "找不到 exit_hit 调用"
    assert i_pop < i_hit, "★ consume 在 exit_hit 之后 ⇒ 跳过无效，当根仍会被扫"
    print("  PASS  test_skip_consumed_before_exit_hit")


def test_confirmation_checks_position_delta():
    """FILL_CONFIRMED 必须核对持仓变化，不能只看「云端有均价」。"""
    src = open(os.path.join(_HERE, "ma13_live_multi.py"), encoding="utf-8").read()
    assert "expect_net" in src, "pending_fill 没有记录目标净仓 ⇒ 无法核对成交"
    assert "filled = " in src, "没有 filled 判定"
    # expect_net 必须被写入。★ 2026-10-19 改：三处平仓已归并到 _send_close
    #   （上期所平今拆单要用同一套记账），所以现在只剩 2 处字面量：
    #   _send_close（覆盖盘中扫损/收盘出场/换月之外的平仓）+ 开仓。
    #   平仓侧覆盖度由 test_close_offset.py 的 test_send_close_* 行为测试保证。
    n_expect = src.count('"expect_net":')
    assert n_expect >= 2, f"expect_net 只写了 {n_expect} 处（应为 2：_send_close / 开仓）"
    assert src.count("_send_close(") >= 3, "平仓未统一走 _send_close ⇒ 拆单记账会漏"
    # confirmed 必须被 filled 门控
    i_filled = src.find("if not filled:")
    i_confirmed = src.find("_L.confirmed(")
    assert 0 < i_filled < i_confirmed, "confirmed 未被 filled 门控 ⇒ 休市报单也会记成成交"
    print(f"  PASS  test_confirmation_checks_position_delta（expect_net ×{n_expect}）")


# ---------------------------------------------------------------- 行为复算
def test_exit_hit_would_fire_on_same_bar():
    """证明「若不做跳过，当根确实会触发」—— 说明这个修不是多余的。

    用事故真实数字：空 8 手，接管算出的 stop=3335.0，
    当根 high 若 ≥ 3335 就会被 exit_hit 判成 stop。
    """
    from signals import Ma13Signal
    sig = Ma13Signal()
    n = 40
    close = np.full(n, 3332.0)
    high = np.full(n, 3332.5)
    high[-1] = 3340.0                 # 当根 high 越过 stop
    low = np.full(n, 3330.0)
    ind = sig.indicators({"open": close, "high": high, "low": low, "close": close})
    why = sig.exit_hit(ind, n - 1, -8, 3335.0, 0)
    assert why == "stop", f"预期当根会被判 stop（这正是事故原因），实际 {why}"
    print("  PASS  test_exit_hit_would_fire_on_same_bar（确认跳过是必要的）")


def main() -> int:
    print("=" * 72)
    print("接管当根不判出场 / 成交确认核对持仓 —— 离线单测")
    print("=" * 72)
    test_recover_sets_skip_flag()
    test_skip_consumed_before_exit_hit()
    test_confirmation_checks_position_delta()
    test_exit_hit_would_fire_on_same_bar()
    print("=" * 72)
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
