# -*- coding: utf-8 -*-
"""日线仪器校准：把 01 双均线原样搬到大周期，检验「换周期是否真的改变结论」。
==========================================================
为什么要有这个工具（2026-09-25 用户拍板「先找正期望腿」→ 转大周期）
------------------------------------------------------------------
`研究路线图.md` §7.12 实测：过门槛所需的**年化夏普**从「30min 半年样本的 4.52」
降到「9 年日线样本的 1.06」（降 77%）。但那是**算术**，不是证据 ——
必须拿一支**答案已知的标准砝码**去校验这套新口径。

01 双均线就是那支砝码 —— 但「答案究竟是什么」，2026-09-25 晚重查账本才弄清。
**账本（30min·v7·半年样本）实测的 5 品种 t 值**（t = 夏普 × √年数）：
    PVC −1.21（OOS 2025-03-13~09-13）｜ 豆粕 −0.91 ｜ 甲醇 −0.22
    纯碱 +0.04 ｜ 螺纹钢 −2.25（后四个 = IS 2026-03-13~09-13）
⇒ t 的**均值 −0.91、标准差 0.90** —— 与标准正态 N(0,1) 几乎重合。
⇒ **01 双均线在 30min 上是「零期望」（μ≈0），它从来就不「显著」。**

⚠️ 本文件上一版写「PVC v1 半年 t = −2.94（显著为负）⇒ 答案已知」是**错的**：
   −2.94 = v1 夏普 −4.16 × √0.5，而 v1 用的是 **1.3 个月（34 个交易日）** 的
   5min 时代短窗，与「8.77 年日线样本」根本不可比。该结论已作废。

★★ 最重要的一件事：**tqsdk 的「年化夏普」含无风险利率 2.5%，不能直接做 t 检验**
------------------------------------------------------------------------
`tqsdk/tafunc.py:1478` 的 `get_sharp(series, trading_days_of_year=250, r=0.025)`：
    夏普 = √250 × (日收益均值 − 日无风险利率) / 日收益标准差，  日 rf = 2.5%/250
⇒ 于是 `t = 夏普 × √年数 = √(250T)·μ_d/σ_d − √(250T)·rf/σ_d`
   **第二项只依赖样本长度 T 与波动 σ_d，与 μ 无关**！
⇒ **样本越长，这个「负 t 假象」越大** —— 它会被误读成「机制显著为负」。
   实测（PVC，8.77 年）：假象 −4.80 + 真实 +0.92 = −3.88，与工具输出逐位吻合。
⇒ **正确做法：去 Rf 的夏普（信息比率）= 夏普 × R_年化 / (R_年化 − 0.025)**
   然后 `真实 t = 去Rf夏普 × √年数`。
⇒ 本工具会在汇总里同时打印两者，以「去 Rf」为准。

判据（跑前写死，见 `研究路线图.md` §7.12.5 A）
--------------------------------------------------
(a) **仪器层（必须过）**：本工具算出的 t 与手算 `年化夏普 × √年数` 必须一致。
(b) **机制层**：记录 7 品种的年化夏普**中位数**。
    · 中位数 > 0            -> 机制在大周期上可能有戏（仍需 t > 门槛才算「显著」）
    · |t| 全在门槛内         -> 「与零无法区分」=> 说明半年样本的「显著为负」
                              至少部分是**样本太短**造成的（仪器确实在撒谎）
    · 中位数为负且 |t| > 门槛 -> **双均线在日线上仍是负期望** => 机制 A 否掉，转 B
(c) 无论结果如何，**本次要登记 K +1**（判决清单第 0 步）。

⚠️ 本工具**不写 策略池/、不写 组合/** —— 理由：
   `策略池/_说明.md` 已明确「旧策略不迁入（避免『还在服役』的错觉）」，
   而 `组合/` 的语义是「策略池 × 品种池 的配对」。仪器校准是**一次性诊断**，
   直调 `engine.run_backtest`（01 的逻辑源就在 engine 里，回测与模拟盘共用），
   所以落在 `工具/`。

两个日线专属的坑（2026-09-25 实测，改之前先读）
--------------------------------------------------
1. **开盘保护在日线上空转**：`no_open_window_min=30` 查的是「09:00 / 21:00 开盘后
   30 分钟」，而日线 K 线的时间戳实测 = **北京时间 00:00**（V/SR/RU/JD 四个交易所
   都一样）⇒ 该窗口**永远不会命中**。为了守规则 4（每次只改一处逻辑），
   这里**照传 30**（与 v7 逐字一致），并在此声明它在日线上是 no-op。
2. **预热不能按「天」算**：`组合/*.py` 里「预热前移 7 天」是给 30min 设计的
   （7 天 ≈ 56 根）。日线 7 天只有 5 根，而 MA60 需要 60 根 ⇒ 前 55 根会全是 NaN。
   本工具改成**按根数**：`预热天数` 默认 150（≈ 100 个交易日）。
   另注：主连日线的**数据硬上限是 2016-01-05**，无法再往前预热 ⇒ 评估起点
   必须从 2016-01-05 往后推，这段损失是数据边界造成的，不是参数选择。

用法：
    python 工具/日线校准.py                      # 7 品种 · 9 年全长，一次跑完
    python 工具/日线校准.py --keys V,M           # 只看子集（先试跑用）
    python 工具/日线校准.py --折数 9             # walk-forward：把评估区间等分 9 折
    python 工具/日线校准.py --止 2024-12-31      # 换止点
    python 工具/日线校准.py --起 2025-06-01 --止 2026-01-01   # 冒烟测试（跑在被烧区，不污染干净样本）
    python 工具/日线校准.py --干跑               # 只打印将要跑什么，不联网
"""
import os
import sys
import math
import json
import glob
import time
from datetime import date, timedelta, datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from lib.paths import 回测结果 as _DIR_回测结果          # noqa: E402
from lib.strategy_dirs import 策略目录名                 # noqa: E402

# ============================================================
# 定稿口径（用户 2026-09-25 拍板）
# ============================================================
策略名 = "双均线"
周期 = 86400                      # 日线
品种默认 = ["RU", "JM", "P", "M", "V", "JD", "SR"]   # 7 品种（去掉 EB、LH）
数据起点 = date(2016, 1, 5)        # 主连日线的硬上限（实测，非选择）
评估止 = date(2025, 3, 12)         # 干净样本上限（之后再被烧，见 §7.12.1）
预热天数 = 150                     # ≈100 个交易日 ⇒ 足够 MA60 + ATR14

# ★ 30min 基准（判据 (b) 的对照物）—— 2026-09-25 晚从账本提取，口径 = 30min·v7·半年样本
#   PVC -1.71 / 豆粕 -1.29 / 甲醇 -0.31 / 纯碱 +0.06 / 螺纹钢 -3.18（夏普）
#   ⇒ t = 夏普 × √0.5 ⇒ 均值 −0.91、标准差 0.90 —— 与 N(0,1) 几乎重合
#   ⇒ 结论：01 双均线在 30min 上 μ≈0，**从来就不显著**（原先写的 t=−2.94 已作废）
基准均值 = -0.91
基准标准差 = 0.90

# --- 01 双均线 v7 参数（原样，一字不改） ---
P = {
    "N1": 5, "N2": 20, "N3": 60,          # MA5 / MA20 / MA60 趋势闸门
    "VOLUME": 1,
    "ATR_STOP_MULT": 2.0,                 # 初始 2×ATR(14) 止损
    "BREAKEVEN_ATR": 0.0,                 # 保本关闭（v4 回滚到阶段A）
    "ATR_PERIOD": 14,
    "SLIP_TICKS": 1,
    "NO_OPEN_WINDOW_MIN": 30,             # ⚠️ 日线上空转（见文件头第 1 条）
    # --- 手数口径：用项目 2026-09-25 现行标准（不是 v7 当年的 1% / 夹[1,8]） ---
    "RISK_PCT": 0.30,
    "HAND_MIN": 1,
    "HAND_MAX": 0,                        # 0 = 不夹
    "MARGIN_CAP_PCT": 30.0,
    "RISK_EQUITY": 0,                     # 0 = 按当前账户权益（复利）
    "INIT_BALANCE": 400000,
}


def 读品种(key):
    import importlib.util as ilu
    p = os.path.join(ROOT, "品种池", f"{key}.py")
    spec = ilu.spec_from_file_location(f"_sp_{key}", p)
    m = ilu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.品种


def 读K():
    """Σ 各账本 累计试验次数（与 工具/回测体检.py 同口径）。"""
    tot, 明细 = 0, []
    for f in sorted(glob.glob(os.path.join(_HERE, "_账本", "*.json"))):
        try:
            d = json.load(open(f, encoding="utf-8"))
            n = int(d.get("累计试验次数") or 0)
            tot += n
            明细.append((os.path.basename(f)[:-5], n))
        except Exception:
            pass
    return tot, 明细


def 门槛t(K):
    return math.sqrt(2 * math.log(K)) if K > 1 else 0.0


# ★ tqsdk 的夏普含无风险利率（tafunc.get_sharp 默认 r=0.025）⇒ 必须去掉才能做 t 检验
无风险年化 = 0.025


def 去Rf(夏普, 年化收益):
    """把 tqsdk 的夏普（含 Rf）换成信息比率（Rf=0）。

    推导：夏普 = √250·(μ_d − rf)/σ_d，μ_d = 年化收益/250，rf = Rf/250
          ⇒ 去Rf夏普 = √250·μ_d/σ_d = 夏普 × μ_d/(μ_d − rf) = 夏普 × 年化/(年化 − Rf)
    ⚠️ 当「年化收益」接近 Rf 时分母趋于 0，反算极不稳（误差被放大）⇒ 见 可靠() 。
    """
    if 年化收益 - 无风险年化 == 0:
        return float('nan')
    return 夏普 * 年化收益 / (年化收益 - 无风险年化)


def 可靠(年化收益):
    """反算法是否可信：|年化 − Rf| 太小 ⇒ 分母放大误差 ⇒ 不可信。"""
    return abs(年化收益 - 无风险年化) > 0.01


def 判决(t, 门):
    if t > 门:
        return "显著为正"
    if t < -门:
        return "显著为负"
    return "与零无法区分"


def 算笔数(txt):
    """从成交明细 txt 数开仓笔数（格式：dt 方向 开平 手数 价格 合约）。"""
    try:
        n = 0
        for ln in open(txt, encoding="utf-8"):
            if "OPEN" in ln.upper():
                n += 1
        return n
    except Exception:
        return -1


def 产物路径(key):
    """推算引擎本次落盘路径（与 engine._save_backtest_report 命名规则一致）。"""
    p = 读品种(key)
    per = f"_{周期 // 86400}d"
    d = os.path.join(ROOT, _DIR_回测结果, 策略目录名(策略名), p["中文名"])
    base = f"{p['中文名']}_{策略名}{per}"
    return os.path.join(d, base + ".html"), os.path.join(d, base + ".txt")


def 跑一段(key, 起, 止, 评估起, 开报告=False):
    """跑一个品种的一段日线回测，返回 (stat, 笔数)。"""
    from lib import engine
    p = 读品种(key)
    _html, _txt = 产物路径(key)
    _t0 = time.time()
    stat = engine.run_backtest(
        key, engine.trend_reverse_signal, P["N1"], P["N2"], P["VOLUME"],
        起, 止, init_balance=int(P["INIT_BALANCE"]), kline_sec=周期,
        strategy_name=策略名, open_html=开报告, eval_start=评估起,
        sample_tag="",                              # 不留 _in/_out 后缀（这不是 IS/OOS）
        pass_dir=True,                              # 目标仓位式：允许只平不反手
        ma_filter=P["N3"],
        slip_ticks=P["SLIP_TICKS"],
        atr_stop_mult=P["ATR_STOP_MULT"],
        breakeven_atr=P["BREAKEVEN_ATR"],
        atr_period=P["ATR_PERIOD"],
        no_open_window_min=P["NO_OPEN_WINDOW_MIN"],
        risk_pct=P["RISK_PCT"], risk_hand_min=P["HAND_MIN"], risk_hand_max=P["HAND_MAX"],
        risk_equity=P["RISK_EQUITY"],
        margin_per_hand=p.get("保证金", 0), margin_cap_pct=P["MARGIN_CAP_PCT"],
    )
    return stat, 算笔数(_txt), time.time() - _t0


def 表头():
    print("  %-5s %-9s %7s %9s %10s %9s %8s %7s  %s"
          % ("品种", "中文名", "笔数", "收益率", "年化夏普", "t值", "年数", "用时", "判决"))


def 一行(key, 中文名, 笔数, stat, 年数, 门, 用时=None):
    sh = float(stat.get("sharpe_ratio", 0) or 0)
    ror = float(stat.get("ror", 0) or 0)
    t = sh * math.sqrt(年数)
    el = ("%5.0fs" % 用时) if 用时 is not None else "   -  "
    print("  %-5s %-9s %7s %9s %10.2f %9.2f %8.2f %7s  %s"
          % (key, 中文名, ("?" if 笔数 < 0 else 笔数), f"{ror:.2%}", sh, t, 年数,
             el, 判决(t, 门)))
    return sh, t


def main():
    argv = sys.argv[1:]
    keys = 品种默认
    if "--keys" in argv:
        keys = [k.strip() for k in argv[argv.index("--keys") + 1].split(",") if k.strip()]
    止 = 评估止
    if "--止" in argv:
        y, m, d = (int(x) for x in argv[argv.index("--止") + 1].split("-"))
        止 = date(y, m, d)
    折数 = 0
    if "--折数" in argv:
        折数 = int(argv[argv.index("--折数") + 1])
    干跑 = "--干跑" in argv
    # --起：显式指定评估起点。默认 = 数据起点 + 预热天数（按根数）。
    #   存在的理由 = **冒烟测试**：可以在「已被烧掉的区段」里试跑，
    #   验证工具能跑通，而不碰干净样本（规则 3）。
    评估起_手动 = None
    回测起_手动 = None
    if "--起" in argv:
        y, m, d = (int(x) for x in argv[argv.index("--起") + 1].split("-"))
        评估起_手动 = date(y, m, d)
        回测起_手动 = 评估起_手动 - timedelta(days=预热天数)

    K, K明细 = 读K()
    门 = 门槛t(K)
    评估起0 = 评估起_手动 or (数据起点 + timedelta(days=预热天数))
    回测起0 = 回测起_手动 or 数据起点
    年数0 = (止 - 评估起0).days / 365.25

    print("=" * 96)
    print(" 日线仪器校准 ｜ %s ｜ 周期 1d ｜ %d 个品种" % (策略名, len(keys)))
    print("=" * 96)
    print(f" 数据起点 {数据起点}（主连日线硬上限）｜ 预热 {预热天数} 天 "
          f"⇒ 评估起 {评估起0} ｜ 评估止 {止}")
    if 回测起0 != 数据起点:
        print(f" ⚠️ 本次回测起被 --起 覆盖为 {回测起0}（冒烟测试用；**这一段可能落在被烧区**）")
    print(f" 有效年数 {年数0:.2f} 年")
    print(f" 门槛 t = √(2·lnK)，K={K}（各账本 累计试验次数 之和）⇒ 门槛 t = {门:.3f}")
    print(f" ⇒ 过线所需年化夏普 = {门:.2f} ÷ √{年数0:.2f} = **{门 / math.sqrt(年数0):.2f}**")
    print(" 参数（01 双均线 v7 原样，一字不改）：")
    print(f"   MA{P['N1']}/{P['N2']} + MA{P['N3']} 趋势闸门 ｜ "
          f"{P['ATR_STOP_MULT']}×ATR({P['ATR_PERIOD']}) 止损 ｜ 保本关闭")
    print(f"   开盘保护 {P['NO_OPEN_WINDOW_MIN']} 分钟 —— ⚠️ 日线上空转（时间戳=00:00，实测）")
    print(f"   手数：风险 {P['RISK_PCT']}% × 按当前权益 ｜ 不夹 ｜ "
          f"单品种保证金 ≤ {P['MARGIN_CAP_PCT']:g}% ｜ 起始资金 {P['INIT_BALANCE']:,}")
    print(f"   滑点 slip_ticks={P['SLIP_TICKS']}（费用层；价格层由 TqSim 对价成交）")
    print("=" * 96)

    if 干跑:
        print("\n [干跑] 将依次执行：")
        for k in keys:
            p = 读品种(k)
            print(f"   {k:5s} {p['中文名']:9s} {p['主连']:18s} "
                  f"{评估起0} ~ {止}（回测自 {回测起0}）")
        print("\n 去掉 --干跑 即真正开跑。")
        return 0

    if 折数 and 折数 > 1:
        全长 = (止 - 评估起0).days
        段长 = 全长 // 折数
        print(f"\n walk-forward：评估区间 {评估起0} ~ {止} 等分 {折数} 折"
              f"（每折约 {段长} 天）；逐折单独回测、各自按根数预热。\n")
        汇总 = {}
        for i in range(折数):
            f起 = 评估起0 + timedelta(days=i * 段长)
            f止 = 止 if i == 折数 - 1 else 评估起0 + timedelta(days=(i + 1) * 段长)
            y = (f止 - f起).days / 365.25
            print("-" * 96)
            print(f" 折 {i + 1}/{折数}：{f起} ~ {f止}（{y:.2f} 年）")
            表头()
            for k in keys:
                p = 读品种(k)
                try:
                    stat, bn, _el = 跑一段(k, f起 - timedelta(days=预热天数), f止, f起)
                    sh, t = 一行(k, p["中文名"], bn, stat, y, 门, _el)
                    汇总.setdefault(k, []).append((i + 1, sh, t))
                except BaseException as e:
                    print(f"  {k:5s} {p['中文名']:9s} [失败] {type(e).__name__}: {e}")
        print("\n" + "=" * 96)
        print(" 逐折汇总（每格 = 该折的年化夏普）")
        print("=" * 96)
        print("  %-5s %s   %s" % ("品种", "  ".join(f"折{i + 1:>2d}" for i in range(折数)),
                                  "正折/总折"))
        for k in keys:
            row = sorted(汇总.get(k, []))
            cells = "  ".join(f"{sh:5.2f}" for _, sh, _ in row)
            正 = sum(1 for _, sh, _ in row if sh > 0)
            print("  %-5s %s     %d/%d" % (k, cells, 正, len(row)))
        print(f"\n 判据：≥{int(折数 * 0.6) + 1}/{折数} 折为正 = 通过（对齐跨年筛选的 60% 线）")
    else:
        print()
        表头()
        shs = []
        keys_跑过 = []
        跑过stat = {}
        for k in keys:
            p = 读品种(k)
            try:
                stat, bn, _el = 跑一段(k, 回测起0, 止, 评估起0)
                sh, t = 一行(k, p["中文名"], bn, stat, 年数0, 门, _el)
                shs.append(sh)
                keys_跑过.append(k)
                跑过stat[k] = stat
            except BaseException as e:
                print(f"  {k:5s} {p['中文名']:9s} [失败] {type(e).__name__}: {e}")
        if shs:
            ts_all = [x * math.sqrt(年数0) for x in shs]
            shs.sort()
            med = shs[len(shs) // 2] if len(shs) % 2 else (shs[len(shs) // 2 - 1] + shs[len(shs) // 2]) / 2
            print("-" * 96)
            print(f"  年化夏普：最小 {min(shs):.2f} ｜ 中位数 {med:.2f} ｜ 最大 {max(shs):.2f}")
            print(f"  日线 t 值：{', '.join(f'{t:+.2f}' for t in sorted(ts_all))}")
            print(f"  为正的品种：{sum(1 for x in shs if x > 0)}/{len(shs)}")
            过 = [x for x in ts_all if abs(x) > 门]
            print(f"  |t| > 门槛 {门:.2f} 的品种：{len(过)}/{len(ts_all)}")
            # ---- ★ 去 Rf（tqsdk 夏普含 2.5% 无风险利率，不去掉会凭空造出负 t）----
            print("-" * 96)
            print("  ★★ 去无风险利率后的真实 t（tqsdk 夏普含 Rf=2.5%，直接用会造假象）")
            _raw = sorted(zip(ts_all, keys_跑过))
            _txt, _tt, _bad = [], [], []
            for _t, _k in _raw:
                _st = _跑过stat.get(_k) or {}
                _ror = float(_st.get("ror", 0) or 0)
                _ra = (1 + _ror) ** (1 / 年数0) - 1 if 年数0 > 0 else 0.0
                _sh0 = float(_st.get("sharpe_ratio", 0) or 0)
                _I = 去Rf(_sh0, _ra)
                _t2 = _I * math.sqrt(年数0)
                _txt.append(f"{_k} {_t2:+.2f}" if 可靠(_ra) else f"{_k} {_t2:+.2f}?")
                if 可靠(_ra):
                    _tt.append(_t2)
                else:
                    _bad.append(_k)
            print(f"     去 Rf t：{' ｜ '.join(_txt)}")
            if _bad:
                print(f"     ⚠️ 带 ? 的品种（{','.join(_bad)}）年化收益贴近 Rf，反算不可信，已排除")
            if len(_tt) >= 2:
                import statistics as _st2
                print(f"     ⇒ 均值 {_st2.mean(_tt):+.2f}、标准差 {_st2.stdev(_tt):.2f}"
                      f"（对照 N(0,1)：0 / 1）")
            print("-" * 96)
            print("  ★ 判据 (b)：日线 t 的**分布** vs 标准正态 N(0,1)")
            print(f"     基准（30min·v7·半年·5 品种实测） = 均值 {基准均值:+.2f}、标准差 {基准标准差:.2f}")
            if len(ts_all) >= 2:
                import statistics as _st
                _m, _sd = _st.mean(ts_all), _st.stdev(ts_all)
                print(f"     本表（{len(ts_all)} 品种·日线）            = 均值 {_m:+.2f}、标准差 {_sd:.2f}")
                if _sd > 0:
                    _z = _m / (_sd / math.sqrt(len(ts_all)))
                    print(f"     单样本检验：均值是否偏离 0 ｜ z = {_z:+.2f}"
                          f"（|z| < 1.96 ⇒ 与 0 无法区分）")
                print("     判读：μ=0 时 t~N(0,1) **与样本长度无关**")
                print("           ⇒ 日线仍 ≈ N(0,1) = 口径一致、仪器正常（且再次确认机制无 edge）")
            else:
                print("     ⚠️ 只跑了 1 个品种，无法做分布检验 —— 建议至少 3 个")

    print("\n" + "=" * 96)
    print(" ⚠️ 本次必须登记 K +1（判决清单第 0 步）。当前 K=%d ⇒ 跑完 K=%d，"
          "门槛 t %.3f -> %.3f" % (K, K + 1, 门, 门槛t(K + 1)))
    print("    在 工具/_账本/*_双均线.json 的顶层 `累计试验次数` 上加 1，并在 版本[] 里补一条。")
    print("    体检：python 工具/回测体检.py --策略 双均线 --明细")
    print(" 判据（跑前写死，见 研究路线图.md §7.12.5 A）：")
    print("    (a) 仪器层：本表的 t 必须 = 年化夏普 × √年数（本工具就是按这个算的）")
    print("    (b) 机制层：日线 t 的**分布**是否仍 ≈ N(0,1)"
          "（30min 基准 = 均值 %.2f / 标准差 %.2f）" % (基准均值, 基准标准差))
    print("        · 仍 ≈ N(0,1)  -> 口径一致、仪器正常，且再次确认机制无 edge")
    print("        · |t| 明显放大  -> 机制在新周期上真有 edge")
    print("        · 偏离 N(0,1)  -> 先查口径（也可能机制换周期后真变了）")
    print("        ⚠️「30min 不显著 + 日线不显著」= 一致，**不是**仪器撒谎（上一轮我读错过）")
    print("        ⚠️ 本校准测的是**特异性**（会不会凭空造 edge），测不了**灵敏度**")
    print("           （要测灵敏度需另找砝码：未来函数版，答案已知 t≈60）")
    print("    (c) 纪律层：结果不管好坏都要接受，不许「再跑一次指望翻案」")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    sys.exit(main())
