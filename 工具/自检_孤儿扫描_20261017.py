# -*- coding: utf-8 -*-
"""自检_孤儿扫描_20261017.py — L449 前缀 bug 的固定回归（离线，无 tqsdk 依赖）

背景（10-15 / 10-17 两次事故的同一根因）：
  `_find_orphan` 用 `cur_sym.rsplit('.', 1)[0]` 求品种前缀，实际得到的是**交易所级**
  前缀（"DCE.p2701" -> "DCE"）。多腿并存时，同交易所别的品种持仓会被判成"历史孤儿"，
  进程重启后各腿互相报单平仓。

本自检固化四条：
  1. `leg_prefix` 得到的是**品种级**前缀（DCE.p / CZCE.SR）；
  2. 5 腿真实持仓并存时，**每腿扫描都返回 None**（谁也不许命中别人）；
  3. 人为加一条真孤儿（DCE.jm2609），**只有焦煤腿**能命中；
  4. CZCE 同字母族（SR vs SF）不互相误伤。

既有 18 场景 mock 只存在于历史会话、未入库，所以这条回归此前一直没有测试拦。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.ops import leg_prefix, find_orphan  # noqa: E402


# 10-17 真实持仓（快照 _样本量快照_20261017.txt）
POS = {
    "DCE.p2701": -4,
    "DCE.jm2701": -4,
    "DCE.m2701": -8,
    "DCE.jd2612": -8,
    "CZCE.SR701": 7,
}

CASES = []


def check(name, cond, detail=""):
    CASES.append((name, bool(cond), detail))


def main():
    # 1) 前缀
    check("leg_prefix(DCE.p2701) == DCE.p", leg_prefix("DCE.p2701") == "DCE.p", leg_prefix("DCE.p2701"))
    check("leg_prefix(CZCE.SR701) == CZCE.SR", leg_prefix("CZCE.SR701") == "CZCE.SR", leg_prefix("CZCE.SR701"))
    check("leg_prefix 空/非法返回空串", leg_prefix("") == "" and leg_prefix("BAD") == "")
    # 反证：旧写法得到的是交易所级前缀（正是 bug 本身）
    check("旧 rsplit 会得到交易所级前缀（bug 的反证）",
          "DCE.p2701".rsplit(".", 1)[0] == "DCE",
          "rsplit -> DCE")

    # 2) 多腿并存：谁也不许命中别人
    for cur in POS:
        hit = find_orphan(POS, cur, exclude=cur)
        check(f"多腿并存 [{cur}] 不跨品种误判", hit is None, f"命中 {hit}")

    # 3) 真孤儿
    pos2 = dict(POS)
    pos2["DCE.jm2609"] = -2
    check("真孤儿 DCE.jm2609 -> 焦煤腿命中",
          find_orphan(pos2, "DCE.jm2701", exclude="DCE.jm2701") == "DCE.jm2609",
          find_orphan(pos2, "DCE.jm2701", exclude="DCE.jm2701"))
    check("真孤儿 DCE.jm2609 -> 棕榈油腿不命中",
          find_orphan(pos2, "DCE.p2701", exclude="DCE.p2701") is None,
          find_orphan(pos2, "DCE.p2701", exclude="DCE.p2701"))

    # 4) CZCE 同字母族不误伤
    check("CZCE.SF701 不会被白糖腿误判",
          find_orphan({"CZCE.SF701": 5}, "CZCE.SR701", exclude="CZCE.SR701") is None,
          find_orphan({"CZCE.SF701": 5}, "CZCE.SR701", exclude="CZCE.SR701"))

    print("[自检] 孤儿扫描前缀（L449 回归）")
    bad = 0
    for name, ok, detail in CASES:
        print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
        if not ok:
            bad += 1
    if bad:
        print(f"  共 {len(CASES)} 条，**{bad} 条失败**")
        return 1
    print(f"  共 {len(CASES)} 条全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
