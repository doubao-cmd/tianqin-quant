# -*- coding: utf-8 -*-
"""
组合：布林带 × 焦煤
==========================================================
本文件把「策略池/布林带/布林带.py」的纯逻辑和「品种池/JM.py」的品种配置配起来。

状态（2026-09-25，新池配对）：
  · 逻辑与参数**完全复制**自同目录下的首个组合（布林带 × PVC，模板），
    本文件只改「绑哪个品种」这一处 —— 见 `组合/_说明.md`「加组合 = 改三处」。
  · 策略来源：`策略池/布林带/布林带.py`（与 lib/engine.boll_decision 逐位对照一致）。
  · ⚠️ 03 布林带已在**旧池**（MA/M/RB/V/SA）上判定淘汰（跨年 1/20、年均 −19.9%）；
    本文件用于在**新池**上复核，不是「策略复活」。
  · ★ 2026-09-25 风险口径二次变更：算手数的权益从「固定 40 万」改为
    **按当前账户权益**（`RISK_EQUITY=0`）。⚠️ 从此之后，本组合产出的
    收益率 / 回撤与 09-25 之前的历史记录**不可横向比较**。
    要用旧口径复现历史数字：把 `RISK_EQUITY` 填回 `400000`。

怎么用：
    python 组合/布林带__JM.py                  # 只看参数
    python 组合/布林带__JM.py 回测 IS          # 跑样本内（可反复跑）
    python 组合/布林带__JM.py 回测 VAL         # 跑验证段（选版本 ≤3 次）
    python 组合/布林带__JM.py 回测 OOS         # 跑样本外（**只跑一次**）
    python 组合/布林带__JM.py 模拟盘           # 模拟盘观察（不下单）
    python 组合/布林带__JM.py 模拟盘 --下单    # 模拟盘真实下单
    python 组合/布林带__JM.py 模拟盘 --下单 --日线   # ★ T1 口径：日线 + 20 万 + 多手

★ 2026-09-27（T1 交付执行系统）新增 `--日线` 支持：
    · 只把 K 线周期从 1800 秒换成 86400 秒，**根数参数一字未改**（BOLL 20 根 = 20 根）
      ⇒ 是改**时间尺度**，不是调参。产物命名带 `_1d`，与 30min 的 `_30min` 并存。
    · 同时修了一处**真实缺陷**：`_区间()` 的预热前移天数原先固定 7 天，
      日线下只有约 5 根 K 线 ⇒ BOLL(20) 算不出 ⇒ 评估期前 15 个交易日**静默无信号**。
      现改为日线前移 45 天。

参考：研究路线图.md 第七章（样本划分）、组合/_说明.md
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# --- 策略（策略池：纯逻辑，无品种/无路径） ---
from lib.paths import 根 as _根, 实测记录 as _DIR_实测记录, 回测结果 as _DIR_回测结果

import importlib.util as _ilu


def _读模块(relpath, name):
    spec = _ilu.spec_from_file_location(name, _根(*relpath.split("/")))
    m = _ilu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


策略模块 = _读模块("策略池/布林带/布林带.py", "_sp_布林带")
算信号 = 策略模块.算信号
算手数 = 策略模块.算手数


def _读品种(key):
    p = os.path.join(_ROOT, "品种池", f"{key}.py")
    spec = _ilu.spec_from_file_location(f"_sp_{key}", p)
    m = _ilu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.品种


# --- 策略（策略池/布林带/布林带.py） ---
品种 = {"key": "JM"}
品种.update(_读品种("JM"))

策略 = "布林带"
周期 = 1800                      # 30min
# 样本区间（研究路线图.md 7.3 定稿口径）
区间 = {
    "IS":  ("2023-01-01", "2025-06-30"),
    "VAL": ("2025-07-01", "2026-02-28"),
    "OOS": ("2026-03-01", "2026-09-13"),
}

# 策略参数（v4 定格口径；迁移时从旧脚本原样搬，不改数）
参数 = {
    "BOLL_N": 20,
    "BOLL_K": 2.0,
    "ATR_PERIOD": 14,            # ATR 周期（止损线与手数共用同一把尺子）
    "ATR_STOP_MULT": 1.0,        # 1×ATR 止损，锚=信号根收盘价
    "TIME_STOP_BARS": 20,        # 时间止损 20 根
    "HAND_MIN": 1,               # 手数下限（1 = 只保证「不能开 0 手」）
    "HAND_MAX": 0,               # ★ 0 = 不夹（让风险预算说话）★ 2026-09-25 口径变更
    "MARGIN_CAP_PCT": 30.0,      # 单品种保证金占权益上限 %（替代拍出来的手数上限）
    "RISK_PCT": 1.00,            # 单笔风险 1.00%（★ 定稿口径 2026-09-27）
    # ★★ 2026-09-25 口径变更（用户拍板）：算手数用的权益从「固定金额」改为「按当前权益」
    #    RISK_EQUITY = 0  -> 每笔风险 = **当前账户权益** × RISK_PCT（复利、凯利方向）
    #    RISK_EQUITY > 0  -> 固定用这个数（= 旧口径，想复现历史数字就填 400000）
    #    ⚠️ 它与下面的 INIT_BALANCE 是两件事，别混：
    #       INIT_BALANCE 只是回测模拟账户的**起点资金**，不参与手数计算。
    "RISK_EQUITY": 0,
    "INIT_BALANCE": 200000,      # 回测模拟账户初始资金（与手数口径无关）
}

备注 = ("v4 信号定格（超调出场=中轨+0.5σ + 时间止损20根 + 1×ATR止损）；"
        "手数口径 2026-09-25 两处变更：① **不夹手数**，上限改为单品种保证金 ≤ 权益 30%"
        "（HAND_MAX=0）② 算手数权益从「固定金额」改为 **按当前账户权益**"
        "（RISK_EQUITY=0）⇒ 权益缩水时风险占比不再被动上升，与凯利方向一致；"
        "★ 2026-09-27 定稿口径：RISK_PCT 0.30→1.00、INIT_BALANCE 40万→20万")


# ============================================================
# 运行入口
# ============================================================
def _区间(样本, 日线=False):
    """区间键 -> (预热起, 评估起, 评估止)。

    ⚠️ 预热天数**必须随周期变**：30min 下 7 天 ≈ 336 根（够 MA20/BOLL20）；
       但**日线下 7 天只有约 5 根**，BOLL(20) 根本算不出来 ⇒ 评估期前 15 个
       交易日会静默无信号（不是策略没信号，是预热不足）。
       故日线一律前移 **45 天**（约 30 根交易日，覆盖 BOLL20 + ATR14）。
    """
    from datetime import date, timedelta
    a, b = 区间[样本]
    ya, ma, da = (int(x) for x in a.split("-"))
    yb, mb, db = (int(x) for x in b.split("-"))
    起, 止 = date(ya, ma, da), date(yb, mb, db)
    前移 = 45 if 日线 else 7
    return 起 - timedelta(days=前移), 起, 止


def _产物路径(样本, 日线=False):
    """推算引擎本次落盘的报告/明细路径（命名规则见 engine._save_backtest_report）。

    ⚠️ 周期标签必须与 `lib/engine.py:5046` 逐字一致：
         `kline_sec % 86400 == 0`  ⇒ `_{kline_sec//86400}d`（日线 = **`1d`**，不是 1440min）
         否则                           ⇒ `_{kline_sec//60}min`
       写错 ⇒ 归档记录里的报告路径**指向不存在的文件**，而 `run_record.归档`
       对不存在的路径是**静默跳过**（只写进说明的「缺」列表）⇒ 报告悄悄丢。
       2026-09-27 实测：豆粕日线产出的是 `豆粕_布林带_1d_is.html`。
    """
    from lib.strategy_dirs import 策略目录名
    _sec = 86400 if 日线 else 周期
    per = f"_{_sec // 86400}d" if _sec % 86400 == 0 else f"_{_sec // 60}min"
    tag = f"_{样本.lower()}" if 样本 else ""
    d = os.path.join(_ROOT, _DIR_回测结果, 策略目录名(策略), 品种["中文名"])
    base = f"{品种['中文名']}_{策略}{per}{tag}"
    return os.path.join(d, base + ".html"), os.path.join(d, base + ".txt")


def 跑回测(样本="IS", 开报告=True, 归档=True, 日线=False):
    """按本组合的参数跑一次回测。

    落盘仍走 回测结果/<策略目录>/<品种>/（汇总报告 / 回滚 的工作区），
    跑完再复制一份到 回测记录/<策略>__<品种>/<时间戳>_<样本>/ 形成自包含档案。

    ⚠️ `日线=True` 时只把**时间尺度**从 30min 换成日线，根数参数一字未改
       （BOLL 20 根就是 20 根）——**不是调参**。产物命名会带上 `_1d`。
    """
    from lib import engine
    P = 参数
    _周期 = 86400 if 日线 else 周期
    预热, 起, 止 = _区间(样本, 日线=日线)
    print(f"[{策略} × {品种['中文名']}] 回测 {样本}：{起} ~ {止}（预热自 {预热}）")
    print(f"[周期] {'日线（86400 秒）' if 日线 else f'{周期 // 60} 分钟（{周期} 秒）'}")
    _口径 = ("按当前账户权益（复利）" if not P['RISK_EQUITY']
             else f"固定 {P['RISK_EQUITY']:,} 元")
    print(f"[手数] 波动率预算 {P['RISK_PCT']}% × {_口径}"
          f" ｜ 不夹手数（下限 {P['HAND_MIN']} 手）"
          f" ｜ 单品种保证金上限 {P['MARGIN_CAP_PCT']:g}% 权益"
          f" ｜ {P['ATR_STOP_MULT']}×ATR({P['ATR_PERIOD']}) 止损")
    print(f"[账户] 回测起始资金 {P['INIT_BALANCE']:,} 元（仅账户起点，不参与手数计算）")
    if 样本 == "OOS":
        print("!! 样本外只应跑一次，跑完即作废，此后不得再据此修改策略 !!")
    engine.run_backtest_boll(
        品种["key"], period=P["BOLL_N"], k=P["BOLL_K"], volume=1,
        start=预热, end=止, eval_start=起,
        init_balance=int(P["INIT_BALANCE"]), kline_sec=_周期,
        strategy_name=策略, open_html=开报告, sample_tag=样本.lower(),
        slip_ticks=1,
        atr_period=P["ATR_PERIOD"], atr_stop_mult=P["ATR_STOP_MULT"],
        risk_pct=P["RISK_PCT"], risk_hand_min=P["HAND_MIN"], risk_hand_max=P["HAND_MAX"],
        risk_equity=P["RISK_EQUITY"],
        margin_per_hand=品种.get("保证金", 0), margin_cap_pct=P["MARGIN_CAP_PCT"],
        time_stop_bars=P["TIME_STOP_BARS"],
    )
    if 归档:
        try:
            from lib import run_record
            _html, _txt = _产物路径(样本, 日线=日线)
            run_record.归档(
                策略, 品种["中文名"], 样本,
                参数=P, 区间=(str(起), str(止)), 备注=备注,
                风险口径=("按当前账户权益（复利）：每笔风险 = 当前权益 × "
                          f"{P['RISK_PCT']}%（RISK_EQUITY=0）"
                          if not P["RISK_EQUITY"] else
                          f"固定金额：每笔风险 = {P['RISK_EQUITY']:,} 元 × "
                          f"{P['RISK_PCT']}%（RISK_EQUITY={P['RISK_EQUITY']}）"),
                成本口径="TqSim 对价成交（价格层 1 跳）＋ slip_ticks=1（费用层 1 跳）"
                         "→ 等效实盘滑点假设 2 跳/次",
                报告=_html, 明细=_txt,
            )
        except Exception as e:
            print(f"[归档] 跳过（不影响回测结果）：{e}")


def 跑模拟盘(允许下单=False, 日线=False):
    """连快期模拟账户跑实时信号（程序要一直开着）。

    ⚠️ `日线=True`：只改时间尺度（K 线周期 86400 秒），根数参数一字未改。
       日线下一天只收 1 根 K 线 ⇒ **信号一天最多一次**，盘面大部分时间静止，
       适合「先跑通链路」的定位（动作少 = 容易盯住、不易出事故）。
    """
    from lib import engine
    P = 参数
    _周期 = 86400 if 日线 else 周期
    print(f"[{策略} × {品种['中文名']}] 模拟盘"
          f" ｜ {'真实下单' if 允许下单 else '观察模式（不下单）'}")
    print(f"[周期] {'日线（86400 秒）' if 日线 else f'{周期 // 60} 分钟（{周期} 秒）'}")
    _口径 = ("按当前账户权益（复利）" if not P['RISK_EQUITY']
             else f"固定 {P['RISK_EQUITY']:,} 元")
    print(f"[手数] 波动率预算 {P['RISK_PCT']}% × {_口径}"
          f" ｜ 不夹手数（下限 {P['HAND_MIN']} 手）"
          f" ｜ 单品种保证金上限 {P['MARGIN_CAP_PCT']:g}% 权益")
    engine.run_live_boll(
        品种["key"], period=P["BOLL_N"], k=P["BOLL_K"], volume=1,
        kline_sec=_周期, allow_trade=允许下单, web_gui=True, heartbeat=30,
        atr_period=P["ATR_PERIOD"], atr_stop_mult=P["ATR_STOP_MULT"],
        risk_pct=P["RISK_PCT"], risk_hand_min=P["HAND_MIN"], risk_hand_max=P["HAND_MAX"],
        margin_per_hand=品种.get("保证金", 0), margin_cap_pct=P["MARGIN_CAP_PCT"],
        risk_equity=P["RISK_EQUITY"], time_stop_bars=P["TIME_STOP_BARS"],
        ledger_dir=os.path.join(_ROOT, _DIR_实测记录, "账户"),
    )


def _用法():
    print(f"组合：{策略} × {品种['中文名']}（{品种['主连']}）")
    print(f"周期：{周期} 秒（30min）｜ 加 --日线 则用 86400 秒（日线）")
    print(f"手数：波动率预算 {参数['RISK_PCT']}%，不夹"
          f"（下限 {参数['HAND_MIN']} 手），单品种保证金 ≤ {参数['MARGIN_CAP_PCT']:g}% 权益")
    print(f"算手数权益："
          + ("按当前账户权益（复利）" if not 参数['RISK_EQUITY']
             else f"固定 {参数['RISK_EQUITY']:,} 元"))
    print(f"回测起始资金：{参数['INIT_BALANCE']:,} 元（与手数口径无关）")
    print(f"备注：{备注}")
    print()
    print("用法：")
    print("  python 组合/布林带__JM.py 回测 IS|VAL|OOS [--日线]   # IS=样本内 VAL=验证 OOS=样本外")
    print("  python 组合/布林带__JM.py 模拟盘 [--下单] [--日线]   # 不带 --下单 = 只观察不下单")
    print()
    print("⚠️ --日线 只改**时间尺度**（根数参数一字未改，BOLL 20 根就是 20 根）—— 不是调参。")


if __name__ == "__main__":
    _args = sys.argv[1:]
    if not _args:
        _用法()
        sys.exit(0)
    _动作 = _args[0]
    _日线 = "--日线" in _args or "--daily" in _args
    if _动作 == "回测":
        # 位置参数 = 样本名（过滤掉 --开关），缺省 IS
        _样本 = next((x for x in _args[1:] if not x.startswith("--")), "IS")
        if _样本 not in 区间:
            print(f"样本只能是 {list(区间)} 之一，现在是 {_样本!r}")
            sys.exit(2)
        跑回测(_样本, 开报告=True, 日线=_日线)
    elif _动作 == "模拟盘":
        跑模拟盘(允许下单="--下单" in _args, 日线=_日线)
    else:
        print(f"不认识的动作 {_动作!r}；可用：回测 / 模拟盘")
        _用法()
        sys.exit(2)
