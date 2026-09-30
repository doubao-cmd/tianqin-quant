# -*- coding: utf-8 -*-
"""
组合：通道突破 × 甲醇
==========================================================
本文件把「策略池/通道突破/通道突破.py」的纯逻辑和「品种池/MA.py」的品种配置配起来，
调用 `lib/engine.run_backtest(signal_kind="channel")` 执行。

【一句话逻辑】
  收盘突破前 5 个交易日（60 根 30min）的最高价 -> 做多；
  跌破前 5 个交易日的最低价                    -> 做空；
  持有中反向破位                              -> 平仓离场（不反手）；
  亏到 1 个「日线 ATR」                        -> 止损。

【为什么走趋势跟踪】
  用户路线（2026-09-27）：不靠消息面、只用技术面。
  技术面不提供优势（均线/突破/布林带人人可复制），能活的只有两样：
    · 纪律 —— 规则人人会写，十年不走样的极少；
    · 盈亏比结构 —— 唯一不需要预测能力就能构造正期望的东西。
  「亏小（止损封顶）+ 让利润跑（反向破位才走）」= 低胜率 + 大盈亏比，
  这正是趋势跟踪的形态，也是本策略的全部设计依据。**没有加任何过滤条件。**

【为什么是 30 分钟】
  用户 2026-09-27 选定：不做日线级别。
  30min 一年约 3000 根（日线的 12 倍）⇒ 同样跨度笔数多十几倍 ⇒ 更快看出期望正负。
  ⚠️ 代价：成本按笔固定，笔数多则成本占比高（本组合单次约 48 元/手）。

【止损宽度为什么带 √每日根数】
  见 `策略池/通道突破/通道突破.py::止损宽度` 的注释。一句话：
  ATR 是「**一根** bar 的波幅」，30min 的 ATR 不是「一天的波幅」，
  要乘 √N（N = 每日根数）才折算成日线尺子。
  ⚠️ 2026-09-27 实测事故：漏掉折算 ⇒ 止损窄 3.5 倍 ⇒ 胜率 29.2% → 8.2%。

怎么用：
    python 组合/通道突破__MA.py                    # 只看参数
    python 组合/通道突破__MA.py 回测 IS            # 样本内（可反复跑）
    python 组合/通道突破__MA.py 回测 VAL           # 验证段
    python 组合/通道突破__MA.py 回测 OOS           # 样本外（**只跑一次**）
    python 组合/通道突破__MA.py 模拟盘             # 观察（不下单）
    python 组合/通道突破__MA.py 模拟盘 --下单      # 模拟盘真实下单

参考：目录地图.md、策略池/_说明.md、组合/_说明.md
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.paths import 根 as _根, 实测记录 as _DIR_实测记录, 回测结果 as _DIR_回测结果

import importlib.util as _ilu


def _读模块(relpath, name):
    spec = _ilu.spec_from_file_location(name, _根(*relpath.split("/")))
    m = _ilu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


策略模块 = _读模块("策略池/通道突破/通道突破.py", "_sp_通道突破")
算信号 = 策略模块.算信号
算手数 = 策略模块.算手数
止损宽度 = 策略模块.止损宽度


def _读品种(key):
    p = os.path.join(_ROOT, "品种池", f"{key}.py")
    spec = _ilu.spec_from_file_location(f"_sp_{key}", p)
    m = _ilu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.品种


# --- 策略 × 品种 ---
品种 = {"key": "MA"}
品种.update(_读品种("MA"))

策略 = "通道突破"
周期 = 1800                      # 30min
# 样本区间（30min 历史可回溯范围有限，故与日线口径的 2015 起不同）
区间 = {
    "IS":  ("2023-08-01", "2025-06-30"),
    "VAL": ("2025-07-01", "2026-02-28"),
    "OOS": ("2026-03-01", "2026-09-24"),
}

# 策略参数（一次设定，固定；不扫描择优）
参数 = {
    "入场窗口": 60,          # ≈ 5 个交易日
    "出场窗口": 60,          # 反向破位离场（同宽）
    "ATR_PERIOD": 20,
    "止损_日ATR倍数": 1.0,    # 止损宽度 = 1 个「日线 ATR」
    "手数": 1,               # 固定 1 手 —— 仓位管理是后面的事，这一轮不掺进来
}
INIT_BALANCE = 200000        # 回测模拟账户起点资金（与手数口径无关）


def _每日根数(kline_sec):
    """一个交易日大约多少根该周期 K 线 —— 只用于把「日线 ATR」折算成止损宽度。

    6 小时 = 日盘 4h（09:00-10:15 / 10:30-11:30 / 13:30-15:00）
            + 夜盘 2h（21:00-23:00），按 6h/日 折算。
    30min -> 12 根；日线 -> 1 根（max 兜底）。
    """
    return max(1, round(21600.0 / kline_sec))


备注 = ("趋势跟踪 / 唐奇安通道突破（海龟原型）。"
        "入场 = 突破前 60 根（≈5 交易日）最高价做多 / 跌破最低价做空；"
        "出场 = 反向破位平仓、不反手；"
        "止损 = 1 个「日线 ATR」= √每日根数 × ATR(20)（30min 下 ×√12≈3.46）。"
        "手数固定 1 手（仓位管理不在本轮）。无任何过滤条件。")


# ============================================================
# 运行入口
# ============================================================
def _区间(样本, 日线=False):
    """区间键 -> (预热起, 评估起, 评估止)。

    ⚠️ 预热必须覆盖「入场窗口 + ATR 周期」：
       30min 下前移 20 天 ≈ 160 根（够 60 根轨道 + ATR20）；
       日线下前移 200 天 ≈ 135 个交易日。
       预热不足 ⇒ 评估期开头一段**静默无信号**（不是策略没信号，是轨道算不出来）。
    """
    from datetime import date, timedelta
    a, b = 区间[样本]
    ya, ma, da = (int(x) for x in a.split("-"))
    yb, mb, db = (int(x) for x in b.split("-"))
    起, 止 = date(ya, ma, da), date(yb, mb, db)
    前移 = 200 if 日线 else 20
    return 起 - timedelta(days=前移), 起, 止


def _产物路径(样本, 日线=False):
    """推算引擎本次落盘的报告/明细路径（命名规则见 engine._save_backtest_report）。

    ⚠️ 周期标签必须与 `lib/engine.py:5047` 逐字一致：
         `kline_sec % 86400 == 0` ⇒ `_{kline_sec//86400}d`（日线 = **`1d`**，不是 1440min）
         否则                     ⇒ `_{kline_sec//60}min`
       写错 ⇒ 归档记录里的报告路径指向不存在的文件（归档不报错，静默错）。
    """
    from lib.strategy_dirs import 策略目录名
    _sec = 86400 if 日线 else 周期
    per = f"_{_sec // 86400}d" if _sec % 86400 == 0 else f"_{_sec // 60}min"
    tag = f"_{样本.lower()}" if 样本 else ""
    d = os.path.join(_ROOT, _DIR_回测结果, 策略目录名(策略), 品种["中文名"])
    base = f"{品种['中文名']}_{策略}{per}{tag}"
    return os.path.join(d, base + ".html"), os.path.join(d, base + ".txt")


def 跑回测(样本="IS", 开报告=False, 归档=True, 日线=False):
    """按本组合的参数跑一次回测。

    落盘走 回测结果/<策略目录>/<品种>/，跑完复制一份到
    回测记录/<策略>__<品种>/<时间戳>_<样本>/ 形成自包含档案。
    """
    from lib import engine
    _周期 = 86400 if 日线 else 周期
    _每日 = _每日根数(_周期)
    _止损倍 = 参数["止损_日ATR倍数"] * (_每日 ** 0.5)
    预热, 起, 止 = _区间(样本, 日线=日线)

    print(f"[{策略} × {品种['中文名']}] 回测 {样本}：{起} ~ {止}（预热自 {预热}）")
    print(f"[周期] {'日线（86400 秒）' if 日线 else f'{周期 // 60} 分钟（{周期} 秒）'}"
          f"（每日约 {_每日} 根）")
    print(f"[信号] 突破前 {参数['入场窗口']} 根极值入场 / 前 {参数['出场窗口']} 根反向破位出场"
          f"（不反手）")
    print(f"[止损] {参数['止损_日ATR倍数']:g} 个日线 ATR = √{_每日} × ATR({参数['ATR_PERIOD']})"
          f" = {_止损倍:.2f} × ATR")
    print(f"[手数] 固定 {参数['手数']} 手 ｜ 账户起点 {INIT_BALANCE:,} 元")
    if 样本 == "OOS":
        print("!! 样本外只应跑一次，跑完即作废，此后不得再据此修改策略 !!")

    engine.run_backtest(
        品种["key"], signal_fn=engine.channel_signal,
        n1=参数["入场窗口"], n2=参数["出场窗口"], volume=参数["手数"],
        start=预热, end=止, eval_start=起,
        init_balance=int(INIT_BALANCE), kline_sec=_周期,
        strategy_name=策略, open_html=开报告, sample_tag=样本.lower(),
        slip_ticks=1, exec_price="ACTIVE",
        signal_kind="channel", pass_dir=True,
        atr_stop_mult=_止损倍, atr_period=参数["ATR_PERIOD"],
        risk_pct=0,
    )
    if 归档:
        try:
            from lib import run_record
            _html, _txt = _产物路径(样本, 日线=日线)
            run_record.归档(
                策略, 品种["中文名"], 样本,
                参数={**参数, "止损乘数": round(_止损倍, 4), "每日根数": _每日},
                区间=(str(起), str(止)), 备注=备注,
                风险口径="固定 1 手（本轮不做仓位管理）",
                成本口径="TqSim 对价成交（价格层 1 跳）＋ slip_ticks=1（费用层 1 跳）"
                         "→ 等效实盘滑点假设 2 跳/次",
                报告=_html, 明细=_txt,
            )
        except Exception as e:
            print(f"[归档] 跳过（不影响回测结果）：{e}")


def 跑模拟盘(允许下单=False, 日线=False):
    """连快期模拟账户跑实时信号（程序要一直开着）。"""
    print(f"[{策略} × {品种['中文名']}] 模拟盘"
          f" ｜ {'真实下单' if 允许下单 else '观察模式（不下单）'}")
    print("!! 本轮尚未接通实时链路（组合层模拟盘改造属独立一轮），"
          "先用 回测 验证期望。")
    raise SystemExit("模拟盘入口暂未接通 —— 见 模拟盘执行清单.md")


def _用法():
    print(f"组合：{策略} × {品种['中文名']}（{品种['主连']}）")
    print(f"周期：{周期} 秒（30min）｜ 加 --日线 则用 86400 秒（日线）")
    print(f"信号：突破前 {参数['入场窗口']} 根极值入场"
          f" / 前 {参数['出场窗口']} 根反向破位出场（不反手）")
    print(f"止损：{参数['止损_日ATR倍数']:g} 个日线 ATR"
          f"（= √每日根数 × ATR({参数['ATR_PERIOD']})）")
    print(f"手数：固定 {参数['手数']} 手 ｜ 账户起点 {INIT_BALANCE:,} 元")
    print(f"备注：{备注}")
    print()
    print("用法：")
    print("  python 组合/通道突破__MA.py 回测 IS|VAL|OOS [--日线]   # IS=样本内 VAL=验证 OOS=样本外")
    print()
    print("⚠️ 参数一次定死，跑完不管结果好坏都不回头调（用户规则 2 / 4）。")


if __name__ == "__main__":
    _args = sys.argv[1:]
    if not _args:
        _用法()
        sys.exit(0)
    _动作 = _args[0]
    _日线 = "--日线" in _args or "--daily" in _args
    if _动作 == "回测":
        _样本 = next((x for x in _args[1:] if not x.startswith("--")), "IS")
        if _样本 not in 区间:
            print(f"样本只能是 {list(区间)} 之一，现在是 {_样本!r}")
            sys.exit(2)
        跑回测(_样本, 开报告=False, 日线=_日线)
    elif _动作 == "模拟盘":
        跑模拟盘(允许下单="--下单" in _args, 日线=_日线)
    else:
        print(f"不认识的动作 {_动作!r}；可用：回测 / 模拟盘")
        _用法()
        sys.exit(2)
