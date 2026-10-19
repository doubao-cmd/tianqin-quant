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


# ---------------------------------------------------------------- 静态断言
def test_recover_sets_skip_flag():
    """接管函数必须置 `_skip_exit_once`，否则当根就会被扫。"""
    src = open(os.path.join(_HERE, "ma13_live_multi.py"), encoding="utf-8").read()
    assert 's["_skip_exit_once"] = True' in src, \
        "接管处没有置 _skip_exit_once ⇒ 接管当根会立刻判出场（事故会重演）"
    print("  PASS  test_recover_sets_skip_flag")


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
    # expect_net 必须在三处下单点都被写入
    n_expect = src.count('"expect_net":')
    assert n_expect >= 3, f"expect_net 只写了 {n_expect} 处（应为 3：盘中扫损/出场/开仓）"
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
