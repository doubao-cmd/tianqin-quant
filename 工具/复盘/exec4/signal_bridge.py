# -*- coding: utf-8 -*-
"""signal_bridge —— 把工作台的 Signal / Sizer 接进复盘工具

为什么不直接 `import signals`：
  工作台目录名含中文（`策略/06_MA13超短线/工作台`），且它不是包（没有 __init__.py），
  只能按**绝对路径**注入 sys.path 后 import。这里做一次、集中一处，
  避免每个调用方各写一遍 sys.path 操作（那种写法迟早写错层级）。

⚠️ 只允许从这里拿策略对象。任何 `def ma13_xxx()` 的本地副本都是违规的。
"""
from __future__ import annotations

import sys
from pathlib import Path

# ── 定位工程根（向上找到含 config_symbols.py 的目录）──────────────────
def _find_root(start: Path) -> Path:
    p = start.resolve()
    for _ in range(8):
        if (p / "config_symbols.py").exists():
            return p
        if p.parent == p:
            break
        p = p.parent
    raise RuntimeError(f"找不到工程根（config_symbols.py），起点 {start}")


ROOT = _find_root(Path(__file__).parent)
WORKBENCH = ROOT / "策略" / "06_MA13超短线" / "工作台"

if not WORKBENCH.is_dir():
    raise RuntimeError(f"工作台目录不存在：{WORKBENCH}")

for _p in (str(ROOT), str(WORKBENCH)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ★ 这两个 import 就是「单一真相源」本身。改名/搬目录必须同步改这里，否则直接报错
#   （刻意不做 try/except 回退 —— 静默回退到「一份本地副本」正是要防的事）
import signals as _signals          # noqa: E402
import sizer as _sizer              # noqa: E402
import config_symbols as _cfg       # noqa: E402


# ── 公开 API ──────────────────────────────────────────────────────────
Signal = _signals.Signal
Ma13Signal = _signals.Ma13Signal
get_signal = _signals.get_signal
Sizer = _sizer.Sizer
RiskParSizer = _sizer.RiskParSizer
get_sizer = _sizer.get_sizer


def spec(key: str) -> dict:
    """品种属性：中文名 / 主连代码 / 乘数 / 最小变动价位。"""
    name, cont, mult, tick = _cfg.SYMBOLS[key.upper()]
    return {"key": key.upper(), "name": name, "cont": cont,
            "mult": float(mult), "tick": float(tick)}


def has_spec(key: str) -> bool:
    return key.upper() in _cfg.SYMBOLS


def default_signal() -> "Signal":
    """MA13 占位信号（当前唯一实现）。"""
    return get_signal("ma13")


def default_sizer() -> "Sizer":
    """与实盘常量逐字一致的 RiskParSizer。"""
    return get_sizer({"name": "risk_par"})
