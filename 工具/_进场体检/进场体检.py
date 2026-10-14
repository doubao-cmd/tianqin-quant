# -*- coding: utf-8 -*-
"""
进场体检（Entry Screening）—— 在写任何策略代码之前，先判一个「进场信号」值不值得做
================================================================================
背景（2026-10-12 的结论）：
  06 MA13 的**毛利只有成本的 4.5%** ⇒ 出场怎么改都没用；瓶颈在**进场**。
  且 62% 的单子被止损直接扫掉 ⇒ 进场后价格常立刻反向。

因此需要一个**便宜、可复用**的筛子：给定一个候选进场信号，先算三件事，
过不了就直接判死，**不进引擎、不写策略**。

三层口径
--------
  L0 纯进场（无止损）  ：进场后**固定持有期**的前瞻毛收益（点/手）
                         ⇒ 只回答「信号有没有方向信息」，不含任何出场逻辑的污染。
  L1 进场 + 固定止损   ：进场 + **固定 STOP_ATR_MULT × ATR** 止损，到期未止损则持有期满平仓
                         ⇒ 回答「这个小系统在**成本后**能不能活」。
  量级判据             ：L1 毛均 ÷ 往返成本（点）必须 ≥ **2.0**
                         ⇒ 留 1 倍余量给估计误差 / 滑点恶化（见 README §七之二 结论⑤）。

通过门槛（跑前定死，全过才算「值得进 full backtest」）
----------------------------------------------------
  ① 样本量   n ≥ 30 笔
  ② 量级     L1 毛均 ÷ 成本 ≥ 2.0
  ③ 成本后   L1 净均 > 0
  ④ 显著性   |t| ≥ 2.0（按笔，非按 bar）
  ⑤ 一致性   至少 **多数品种**（≥ ⌈品种数/2⌉）同时满足 ②③

⚠️⚠️ 两条纪律（本工具存在的意义就是它们）
----------------------------------------------------
  1. **本工具只能「判死」，不能「判生」。** 跑在 2021-2023（已污染窗口）上，
     任何"通过"都不构成 edge 证据，只说明「值得进入下一轮、且必须换干净样本」。
  2. **禁止把「批次里表现最好的那个」当成发现。** 批量跑 N 个信号 = N 次比较，
     挑最好的宣称有效就是过拟合（规则 2 禁区）。批量模式**只用于一次性砍掉一批候选**；
     任何幸存者必须**单独再过一遍预注册卡 + 独立样本**。

禁未来函数
----------
  信号只用 ≤ 第 i 根已收盘 bar 的数据；**进场在 open[i+1]**。
  内置 `--selftest` 做「截断检验」：对随机样本，用 data[:i+1] 重算 sig[i]，
  与全量算出的 sig[i] 必须逐一相等（不等即为未来函数）。

用法
----
  python 进场体检.py --list                    # 列出所有候选信号
  python 进场体检.py --signal retrace_ma13     # 只测一个（推荐）
  python 进场体检.py --all                     # 批量证伪（结果不可用于择优）
  python 进场体检.py --signal cross_ma13 --syms JM,M,P
  python 进场体检.py --selftest                # 未来函数截断检验
"""
import os
import sys
import json
import math
import argparse

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config_symbols as cs  # noqa: E402

# ==================== 冻结口径（写死，不许改；改了就等于调参）====================
MA_N = 13          # MA13（与 06 同）
SLOPE_N = 5        # 斜率周期（与 06 同）
ATR_N = 14         # ATR14（与 06 同）
HOLD = 12          # ★ L0/L1 主持有期（5min×12 ≈ 1 小时）。**外生定死，不扫**
STOP_ATR_MULT = 2.0  # ★ L1 固定止损（2×ATR）
SLIP = 1           # 滑点（跳），与全项目一致
RETRACE_TOL = 0.5  # 回踩容差（与 06 同）
BREAK_N = 20       # 唐奇安突破窗口（经典外生值，不扫）
TSMOM_N = 20       # 时序动量窗口（不扫）
DIAG_HOLDS = [1, 2, 3, 6, 12, 24, 48]  # ⚠️ 仅供「诊断曲线」，**严禁据此择优**
# ---- SMC 两个候选信号的外生参数（2026-10-13 新增，定死、不扫）----
EQUAL_W = 10       # 等高等低的窗口（5min×10 ≈ 50 分钟）
EQUAL_TOL = 0.25   # 「等高/等低」容差（ATR 倍数）
# ---- 布林带 + RSI 战法的外生参数（2026-10-13 新增，按用户提供的战法原样，不扫）----
BOLL_N = 20        # 布林带周期（与 07 布林带同口径）
BOLL_K = 2.0       # 标准差倍数（与 07 同口径）
RSI_N = 14         # RSI 周期（经典外生值）
RSI_OS = 30.0      # 超卖阈值（战法原定）
RSI_OB = 70.0      # 超买阈值（战法原定）
RSI_DIV_N = 20     # RSI 背离的比较窗口（外生定死，不扫）
# ==============================================================================

DATA_DIR = os.path.join(_HERE, "数据")


# ---------------------------------------------------------------- 指标
def _atr(high, low, close, n):
    """简单滚动均值 TR —— 与 06 回测脚本**逐字同口径**（保证可比）。

    06 的实现是 Python 循环： atr[i] = mean(tr[max(1,i-n):i])（i>=2 时），atr[1]=tr[1]。
    这里用「累计和」等价向量化（5.3 万根 × 多个信号下，循环版慢到不可用）：
        mean(tr[a:i]) = (cs[i] - cs[a]) / (i - a)，其中 a = max(1, i-n)
    ⚠️ 用 max(1, ...) 而不是 i-n，是为了**复刻 06 在预热期（i<15）的"部分窗口"行为**——
    照抄 pandas.rolling(n).mean() 会在预热期产生 NaN，与 06 不一致。
    """
    prev = np.roll(close, 1); prev[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    N = len(tr)
    out = np.full(N, np.nan)
    if N < 2:
        return out
    cs = np.cumsum(np.insert(tr, 0, 0.0))          # cs[k] = sum(tr[:k])
    i = np.arange(N)
    a = np.maximum(1, i - n)
    cnt = i - a
    m = (i >= 2) & (cnt > 0)
    out[m] = (cs[i[m]] - cs[a[m]]) / cnt[m]
    out[1] = tr[1]
    return out


def _sma(x, n):
    """ma[i] = mean(x[i-n:i])（只用 i 之前的 bar）—— 与 06 的 MA13 同口径。
    cs 长度 N+1，cs[k]=sum(x[:k])；ma[i] = (cs[i]-cs[i-n])/n，i ∈ [n, N)。
    ⇒ out[n:] = (cs[n:N] - cs[0:N-n]) / n，两段长度都是 N-n。
    """
    N = len(x)
    out = np.full(N, np.nan)
    if N <= n:
        return out
    cs = np.cumsum(np.insert(x, 0, 0.0))
    out[n:] = (cs[n:-1] - cs[:-(n + 1)]) / n
    return out


# ---------------------------------------------------------------- 信号注册表
def _s_retrace_ma13(o, h, l, c, atr, ma):
    """★ 基准锚 = 06 MA13 的**进场条件本身**：|close−MA13| ≤ 0.5×ATR 且斜率定方向。"""
    slope = ma - np.roll(ma, SLOPE_N)
    slope[:SLOPE_N] = np.nan
    d = np.sign(slope)
    touch = np.abs(c - ma) <= RETRACE_TOL * atr
    return np.where(touch, d, 0.0)


def _s_cross_ma13(o, h, l, c, atr, ma):
    """收盘价上穿/下穿 MA13 的那一根。"""
    up = (c > ma) & (np.roll(c, 1) <= np.roll(ma, 1))
    dn = (c < ma) & (np.roll(c, 1) >= np.roll(ma, 1))
    return np.where(up, 1.0, np.where(dn, -1.0, 0.0))


def _s_breakout_20(o, h, l, c, atr, ma):
    """唐奇安：收盘 > 前 20 根最高 → 多；< 前 20 根最低 → 空（轨道不含当前根）。"""
    hh = pd.Series(h).rolling(BREAK_N).max().shift(1).to_numpy()
    ll = pd.Series(l).rolling(BREAK_N).min().shift(1).to_numpy()
    return np.where(c > hh, 1.0, np.where(c < ll, -1.0, 0.0))


def _s_tsmom_20(o, h, l, c, atr, ma):
    """过去 20 根收益的符号（时序动量）。"""
    r = c - np.roll(c, TSMOM_N)
    r[:TSMOM_N] = np.nan
    return np.sign(np.nan_to_num(r, nan=0.0))


def _s_rev_2bar(o, h, l, c, atr, ma):
    """连续 2 根下跌 → 做多（逆势抓反转）；连续 2 根上涨 → 做空。"""
    d1 = np.sign(c - np.roll(c, 1))
    d2 = np.sign(np.roll(c, 1) - np.roll(c, 2))
    both_dn = (d1 < 0) & (d2 < 0)
    both_up = (d1 > 0) & (d2 > 0)
    return np.where(both_dn, 1.0, np.where(both_up, -1.0, 0.0))


def _s_vol_break(o, h, l, c, atr, ma):
    """单根振幅 > 2×ATR 时顺势跟进（波动扩张）。"""
    rng = c - np.roll(c, 1)
    big = np.abs(rng) > 2.0 * atr
    return np.where(big, np.sign(rng), 0.0)


def _s_pin_bar(o, h, l, c, atr, ma):
    """价格行为·pin bar：长影线 + 小实体。下影线主导 ⇒ 反转向上；上影线主导 ⇒ 反转向下。"""
    rng = h - l
    body = np.abs(c - o)
    up_tail = h - np.maximum(o, c)
    dn_tail = np.minimum(o, c) - l
    is_pin = (rng > 0) & (body < 0.35 * rng)
    bull = is_pin & (dn_tail > up_tail) & (dn_tail > 0.5 * rng)
    bear = is_pin & (up_tail > dn_tail) & (up_tail > 0.5 * rng)
    return np.where(bull, 1.0, np.where(bear, -1.0, 0.0))


def _s_engulf(o, h, l, c, atr, ma):
    """价格行为·吞没：当前实体完全吞没前一根实体，且方向相反。"""
    po = np.roll(o, 1); pc = np.roll(c, 1)
    bull = (pc < po) & (c > o) & (c > pc) & (o < po)
    bear = (pc > po) & (c < o) & (c < pc) & (o > po)
    return np.where(bull, 1.0, np.where(bear, -1.0, 0.0))


def _s_spring(o, h, l, c, atr, ma):
    """威科夫·spring：刺破前 BREAK_N 根区间下沿后当根收回（收盘回到支撑之上且收阳）。"""
    rl = pd.Series(l).rolling(BREAK_N).min().shift(1).to_numpy()
    return np.where((l < rl) & (c > rl) & (c > o), 1.0, 0.0)


def _s_upthrust(o, h, l, c, atr, ma):
    """威科夫·upthrust：刺破前 BREAK_N 根区间上沿后当根收回（收盘回到阻力之下且收阴）。"""
    rh = pd.Series(h).rolling(BREAK_N).max().shift(1).to_numpy()
    return np.where((h > rh) & (c < rh) & (c < o), -1.0, 0.0)


def _s_hl_pullback(o, h, l, c, atr, ma):
    """价格行为·更高低点/更低高点回踩：上升趋势(MA 上行)中近期摆动低点高于前段、收盘重回 MA 之上。"""
    LL_recent = pd.Series(l).rolling(BREAK_N).min().shift(1).to_numpy()
    LL_prior = pd.Series(l).rolling(BREAK_N).min().shift(1 + BREAK_N).to_numpy()
    HH_recent = pd.Series(h).rolling(BREAK_N).max().shift(1).to_numpy()
    HH_prior = pd.Series(h).rolling(BREAK_N).max().shift(1 + BREAK_N).to_numpy()
    upt = ma > np.roll(ma, SLOPE_N)
    dnt = ma < np.roll(ma, SLOPE_N)
    bull = (LL_recent > LL_prior) & upt & (c > ma)
    bear = (HH_recent < HH_prior) & dnt & (c < ma)
    return np.where(bull, 1.0, np.where(bear, -1.0, 0.0))


def _s_fvg_retrace(o, h, l, c, atr, ma):
    """SMC·FVG 失衡缺口回踩（Fair Value Gap）。

    FVG 定义（外生固定，无参数）：三根连续 K 线 A/B/C，A 与 C **不重叠**——
      看涨缺口：A.high < C.low  ⇒ 缺口区间 = [A.high, C.low]
      看跌缺口：A.low  > C.high ⇒ 缺口区间 = [C.high, A.low]
    进场：当根 low/high 进入缺口区间、且收盘站回缺口**另一侧**（缺口被尊重）
          ⇒ 顺缺口方向进场（看涨缺口做多 / 看跌缺口做空）。

    ⚠️ 禁未来函数：缺口由「上一根完成」的三 K 确定（A=i-3, B=i-2, C=i-1），
       回踩判定只用当根已收盘 O/H/L/C ⇒ `--selftest` 截断检验可逐值复现。
    """
    n = len(c)
    nan = np.full(n, np.nan)
    A_h = np.concatenate([nan[:3], h[:-3]])      # h[i-3]（缺口的「后沿」）
    A_l = np.concatenate([nan[:3], l[:-3]])      # l[i-3]
    C_l = np.concatenate([nan[:1], l[:-1]])      # l[i-1]（缺口的「前沿」）
    C_h = np.concatenate([nan[:1], h[:-1]])      # h[i-1]
    up_gap = A_h < C_l                           # 看涨缺口存在
    dn_gap = A_l > C_h                           # 看跌缺口存在
    touch_up = (l <= C_l) & (c >= A_h) & (c > o)   # 回踩缺口下沿并收回
    touch_dn = (h >= C_h) & (c <= A_l) & (c < o)
    return np.where(up_gap & touch_up, 1.0, np.where(dn_gap & touch_dn, -1.0, 0.0))


def _s_equal_hl_sweep(o, h, l, c, atr, ma):
    """SMC·等高等低被扫（liquidity sweep at equal highs/lows）。

    与 `spring`/`upthrust` 的区别：它们只要求「跌破前 BREAK_N 根区间」；本信号额外要求
    **两段分离窗口的低点（或高点）落在同一水平**（SMC 的 equal lows/highs「等低/等高」
    结构 = 止损确实在同一位置堆积过），再被假破后收回。

    ⚠️ 参数（EQUAL_W=10 / EQUAL_TOL=0.25ATR）**外生定死、不扫**（规则 2）。
    """
    L_near = pd.Series(l).rolling(EQUAL_W).min().shift(1).to_numpy()       # [i-W, i-1]
    L_prior = pd.Series(l).rolling(EQUAL_W).min().shift(1 + EQUAL_W).to_numpy()  # [i-2W, i-W-1]
    H_near = pd.Series(h).rolling(EQUAL_W).max().shift(1).to_numpy()
    H_prior = pd.Series(h).rolling(EQUAL_W).max().shift(1 + EQUAL_W).to_numpy()
    eq_low = np.abs(L_near - L_prior) <= EQUAL_TOL * atr      # 两段低点「等低」
    eq_high = np.abs(H_near - H_prior) <= EQUAL_TOL * atr     # 两段高点「等高」
    lvl_l = np.fmax(L_near, L_prior)                          # 支撑水平（取较高者）
    lvl_h = np.fmin(H_near, H_prior)                          # 阻力水平（取较低者）
    sweep_low = eq_low & (l < lvl_l) & (c > lvl_l) & (c > o)
    sweep_high = eq_high & (h > lvl_h) & (c < lvl_h) & (c < o)
    return np.where(sweep_low, 1.0, np.where(sweep_high, -1.0, 0.0))


def _rsi(c, n):
    """Wilder RSI（与通行定义一致；ewm 递推是因果的，不偷看未来）。"""
    d = np.diff(c, prepend=c[0])
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au = pd.Series(up).ewm(alpha=1.0 / n, adjust=False).mean().to_numpy()
    ad = pd.Series(dn).ewm(alpha=1.0 / n, adjust=False).mean().to_numpy()
    rsi = np.where(ad > 0, 100.0 - 100.0 / (1.0 + au / np.where(ad > 0, ad, 1.0)), 100.0)
    return rsi


def _s_boll_rsi_reversal(o, h, l, c, atr, ma):
    """布林带 + RSI 三重确认（★ 用户 2026-10-13 提供的战法，原样实现）。

    做多（必须同时满足三点）：
      ① 触底：当根 low 触及或跌破布林带下轨
      ② 超卖：RSI < 30
      ③ 拐头：RSI 较前一根**拐头向上**（r[i] > r[i-1]）
    做空为对称镜像：high ≥ 上轨 & RSI > 70 & RSI 较前一根拐头向下。

    ⚠️ 规则 2 提示：本战法的设计方式（在布林带上**叠加** RSI 双重过滤）与项目
       「不为拉高胜率而叠加过滤条件」的纪律冲突。此处仅按用户要求原样复现并**判死/判活**，
       不做任何阈值调整；参数（20/2σ、RSI14、30/70）全部外生定死。
    """
    mid = pd.Series(c).rolling(BOLL_N).mean().to_numpy()
    sd = pd.Series(c).rolling(BOLL_N).std(ddof=1).to_numpy()
    lower = mid - BOLL_K * sd
    upper = mid + BOLL_K * sd
    r = _rsi(c, RSI_N)
    r_prev = np.concatenate([[np.nan], r[:-1]])
    long_ = (l <= lower) & (r < RSI_OS) & (r > r_prev)
    short_ = (h >= upper) & (r > RSI_OB) & (r < r_prev)
    return np.where(long_, 1.0, np.where(short_, -1.0, 0.0))


def _s_rsi_oversold(o, h, l, c, atr, ma):
    """RSI 极值反转（把 RSI 当**主信号**，不是过滤器）：RSI<30 做多、>70 做空。

    与 `boll_rsi` 的区别：那个是「布林带进场 + RSI 过滤」；这个是**去掉布林带**，
    看 RSI 的极值本身有没有方向信息。
    """
    r = _rsi(c, RSI_N)
    return np.where(r < RSI_OS, 1.0, np.where(r > RSI_OB, -1.0, 0.0))


def _s_rsi_cross(o, h, l, c, atr, ma):
    """RSI 穿越阈值：上穿 30 做多、下穿 70 做空（只在穿越当根触发）。"""
    r = _rsi(c, RSI_N)
    rp = np.concatenate([[np.nan], r[:-1]])
    long_ = (rp <= RSI_OS) & (r > RSI_OS)
    short_ = (rp >= RSI_OB) & (r < RSI_OB)
    return np.where(long_, 1.0, np.where(short_, -1.0, 0.0))


def _s_rsi_divergence(o, h, l, c, atr, ma):
    """RSI 背离：价格创 2N 根新低、但 RSI 未创 N 根新低（看涨）；高点对称（看跌）。

    这是 RSI 三种用法里**唯一带二阶结构**的：比较的不是 RSI 的绝对水平，
    而是「价格的新极值」与「动量的新极值」是否同步。
    """
    r = _rsi(c, RSI_N)
    cmin = pd.Series(c).rolling(2 * RSI_DIV_N).min().to_numpy()
    cmax = pd.Series(c).rolling(2 * RSI_DIV_N).max().to_numpy()
    rmin = pd.Series(r).rolling(RSI_DIV_N).min().to_numpy()
    rmax = pd.Series(r).rolling(RSI_DIV_N).max().to_numpy()
    # ⚠️ 边界比较必须加容差：RSI 创 N 根新低时 r == rmin，截断重算的浮点误差
    #    会让 `r > rmin` 翻转 ⇒ selftest 报「有未来函数」（伪报）。加 1e-9 消除。
    bull = (c <= cmin) & (r > rmin + 1e-9)
    bear = (c >= cmax) & (r < rmax - 1e-9)
    return np.where(bull, 1.0, np.where(bear, -1.0, 0.0))


SIGNALS = {
    "retrace_ma13": ("回踩 MA13 + 斜率定方向（= 06 的进场条件，基准锚）", _s_retrace_ma13),
    "cross_ma13":   ("收盘上/下穿 MA13", _s_cross_ma13),
    "breakout_20":  ("唐奇安 20 根区间突破（轨道不含当前根）", _s_breakout_20),
    "tsmom_20":     ("过去 20 根收益的符号（时序动量）", _s_tsmom_20),
    "rev_2bar":     ("连续 2 根同向 → 反向开（逆势）", _s_rev_2bar),
    "vol_break":    ("单根振幅 > 2×ATR 顺势跟进", _s_vol_break),
    "pin_bar":      ("价格行为·pin bar（长影线小实体反转）", _s_pin_bar),
    "engulf":       ("价格行为·吞没形态", _s_engulf),
    "spring":       ("威科夫·spring（假跌破区间下沿后收回）", _s_spring),
    "upthrust":     ("威科夫·upthrust（假突破区间上沿后收回）", _s_upthrust),
    "hl_pullback":  ("价格行为·更高低点/更低高点回踩（趋势中）", _s_hl_pullback),
    "fvg_retrace":    ("SMC·FVG 失衡缺口回踩（三 K 不重叠后回补）", _s_fvg_retrace),
    "equal_hl_sweep": ("SMC·等高等低被扫（两段低/高点同水平后假破收回）", _s_equal_hl_sweep),
    "boll_rsi":       ("布林带+RSI 三重确认（触轨 & 超买超卖 & 拐头）", _s_boll_rsi_reversal),
    "rsi_oversold":   ("RSI 极值反转（RSI 当主信号：<30 多 / >70 空）", _s_rsi_oversold),
    "rsi_cross":      ("RSI 穿越阈值（上穿 30 多 / 下穿 70 空）", _s_rsi_cross),
    "rsi_divergence": ("RSI 背离（价格创新极值但 RSI 未同步）", _s_rsi_divergence),
}


# ---------------------------------------------------------------- 数据
PERIOD = "5min"        # 数据周期（main 里可由 --period 覆盖；见 拉数据.py 的 EP_KSEC）


def load(key, period=None):
    period = period or PERIOD
    import glob
    # 兼容旧文件名（含硬编码日期）与新区间文件：取该周期下最新的缓存
    cand = sorted(glob.glob(os.path.join(DATA_DIR, f"{key}_{period}_*.csv")))
    if not cand:
        return None
    p = cand[-1]
    df = pd.read_csv(p, encoding="utf-8-sig", parse_dates=["datetime"])
    return df


def cost_points(key):
    """往返成本（点/手）= 2 × (滑点×一跳 + 佣金/乘数)。"""
    _, _, MULT, TICK = cs.get(key)
    COMM = float(cs.COST.get(key, 0.0))
    return 2.0 * (SLIP * TICK + COMM / MULT)


# ---------------------------------------------------------------- 体检
def screen_one(key, sig_name, df, diag=False, stop_mult=None):
    sm = STOP_ATR_MULT if stop_mult is None else float(stop_mult)
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    N = len(c)
    atr = _atr(h, l, c, ATR_N)
    ma = _sma(c, MA_N)
    sig = SIGNALS[sig_name][1](o, h, l, c, atr, ma)
    # 信号预热期与无效值置 0
    bad = np.isnan(atr) | np.isnan(ma)
    sig = np.where(bad, 0.0, np.nan_to_num(sig, nan=0.0))

    valid = np.arange(0, N - HOLD - 1)
    idx = valid[sig[valid] != 0]
    if len(idx) == 0:
        return {"key": key, "signal": sig_name, "n": 0,
                "name_cn": cs.get(key)[0], "per_day": 0.0, "days": 0,
                "cost_pts": 0.0, "L0_gross": 0.0, "L0_t": 0.0,
                "L1_gross": 0.0, "L1_net": 0.0, "L1_t": 0.0,
                "L1_winrate": 0.0, "stop_share": 0.0, "ratio": 0.0}

    sgn = sig[idx]
    entry = o[idx + 1]
    cp = cost_points(key)

    # ---- L0：固定持有期、无止损 ----
    fwd = (o[idx + 1 + HOLD] - entry) * sgn
    # ---- L1：进场 + 固定 2ATR 止损 ----
    stop = np.where(sgn > 0, entry - sm * atr[idx], entry + sm * atr[idx])
    exit_px = o[idx + 1 + HOLD].copy()
    hit = np.zeros(len(idx), dtype=bool)
    for j, (e_i, st, s) in enumerate(zip(idx, stop, sgn)):
        for jj in range(e_i + 1, e_i + 1 + HOLD):
            if s > 0:
                if l[jj] <= st:
                    exit_px[j] = min(st, o[jj]); hit[j] = True; break
            else:
                if h[jj] >= st:
                    exit_px[j] = max(st, o[jj]); hit[j] = True; break
    gross1 = (exit_px - entry) * sgn
    net1 = gross1 - cp

    def _t(x):
        sd = float(np.std(x, ddof=1)) if len(x) > 1 else 0.0
        return float(np.mean(x) / (sd / math.sqrt(len(x)))) if sd > 1e-12 else 0.0

    days = df["datetime"].dt.date.nunique()
    out = {
        "key": key, "name_cn": cs.get(key)[0], "signal": sig_name,
        "n": int(len(idx)), "per_day": round(len(idx) / days, 2), "days": int(days),
        "cost_pts": round(cp, 3),
        "L0_gross": round(float(np.mean(fwd)), 3), "L0_t": round(_t(fwd), 2),
        "L1_gross": round(float(np.mean(gross1)), 3),
        "L1_net": round(float(np.mean(net1)), 3), "L1_t": round(_t(net1), 2),
        "L1_winrate": round(float(np.mean(net1 > 0)), 4),
        "stop_share": round(float(np.mean(hit)), 4),
        "ratio": round(float(np.mean(gross1) / cp), 3) if cp > 0 else None,
    }
    if diag:
        # 诊断曲线要覆盖最长持有期 ⇒ 用「能容纳最长持有期」的那批进场点（不改主判定的样本）
        mx = max(DIAG_HOLDS)
        idz = idx[idx + 1 + mx < N]
        if len(idz) > 0:
            out["L0_curve"] = {str(hh): round(float(np.mean((o[idz + 1 + hh] - o[idz + 1]) * sig[idz])), 3)
                               for hh in DIAG_HOLDS}
    return out


def verdict(rows):
    """按跑前定死的五条门槛判定（跨品种一致性）。"""
    ok = [r for r in rows if r["n"] >= 30]
    hard = [r for r in ok if (r.get("ratio") or -9) >= 2.0 and r["L1_net"] > 0]
    sig_ok = [r for r in hard if abs(r["L1_t"]) >= 2.0]
    need = math.ceil(len(ok) / 2) if ok else 1
    return {
        "n_sym": len(ok), "need": need,
        "pass_hard": len(hard), "pass_sig": len(sig_ok),
        "verdict": "PASS（仅表示值得换干净样本再验，不是 edge 证据）"
                   if len(hard) >= need else "FAIL（判死）",
    }


def _detect_window(period):
    """从缓存文件名推断实际数据窗口（如 MA_1440min_2016-01-04_2021-12-31.csv）。
    返回 (lo, hi) 或 None；用于如实标注窗口，避免硬编码『可作样本外验证』。"""
    import glob, re
    pats = sorted(glob.glob(os.path.join(DATA_DIR, f"*_{period}_*.csv")))
    ds = []
    for p in pats:
        m = re.search(r"_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.csv$", p)
        if m:
            ds.append((m.group(1), m.group(2)))
    if not ds:
        return None
    return (min(d[0] for d in ds), max(d[1] for d in ds))


# 06 战役已反复跑过的污染窗口（见 README 运行台账）
_POLLUTED_06 = ("2021-01-01", "2023-12-31")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--syms", default=None, help="逗号分隔，如 JM,M,P")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--diag", action="store_true", help="附持有期诊断曲线")
    ap.add_argument("--selftest", action="store_true", help="未来函数截断检验")
    ap.add_argument("--stop", type=float, default=STOP_ATR_MULT,
                    help="止损 ATR 倍数（默认 2.0，与项目口径一致；战法原版可传 1.5）")
    ap.add_argument("--period", default="5min",
                    help="数据周期：5min（默认）/ 15min 等（须先用 拉数据.py 拉好）")
    ap.add_argument("--hold", type=int, default=None,
                    help="L0/L1 持有期（根数，覆盖默认 HOLD=12）。日线 12 根=12 天，5min 12 根=1 小时")
    a = ap.parse_args()
    global PERIOD, HOLD
    PERIOD = a.period
    if a.hold is not None:
        HOLD = a.hold

    if a.list:
        print("可用候选信号：")
        for k, (desc, _) in SIGNALS.items():
            print(f"  {k:<15} {desc}")
        return

    keys = ([k.strip().upper() for k in a.syms.split(",")] if a.syms
            else list(cs.SYMBOLS.keys()))

    if a.selftest:
        _selftest(keys)
        return

    names = list(SIGNALS) if a.all else [a.signal]
    if names == [None]:
        raise SystemExit("请指定 --signal NAME 或 --all（--list 可看列表）")

    L = []
    L.append("=" * 112)
    _w = _detect_window(a.period)
    if _w is None:
        _win = "窗口未知（未找到缓存数据，先跑 拉数据.py）"
        _win_dates = "未知"
    elif _w == _POLLUTED_06:
        _win = f"窗口 {_w[0]}~{_w[1]}（**已污染，只能判死**）"
        _win_dates = f"{_w[0]}~{_w[1]}"
    else:
        _win = f"窗口 {_w[0]}~{_w[1]}（**非样本外，IS 层诊断**）"
        _win_dates = f"{_w[0]}~{_w[1]}"
    L.append(f"进场体检  ｜  周期={a.period}  ｜  HOLD={HOLD} 根  ｜  止损={a.stop:g}×ATR  ｜  "
             f"滑点={SLIP}跳  ｜  手数=固定 1 手  ｜  {_win}")
    L.append("=" * 112)
    L.append(f"{'信号':<14}{'品种':<7}{'笔数':>7}{'笔/日':>7}{'成本(点)':>9}"
             f"{'L0毛(点)':>10}{'L1毛(点)':>10}{'L1净(点)':>10}{'毛/成本':>9}{'胜率':>8}{'止损占比':>9}{'t':>7}{'判定':>6}")

    summary = {}
    for nm in names:
        rows = []
        for k in keys:
            df = load(k)
            if df is None or len(df) < 1000:
                print(f"  [skip] {k} 无缓存数据（先跑 拉数据.py）")
                continue
            r = screen_one(k, nm, df, diag=a.diag, stop_mult=a.stop)
            rows.append(r)
            v = "✅" if (r.get("ratio") or -9) >= 2.0 and r["L1_net"] > 0 else "❌"
            L.append(f"{nm:<14}{r['name_cn']:<7}{r['n']:>7}{r['per_day']:>7.2f}{r['cost_pts']:>9.2f}"
                     f"{r['L0_gross']:>10.3f}{r['L1_gross']:>10.3f}{r['L1_net']:>10.3f}"
                     f"{(r.get('ratio') or float('nan')):>9.2f}{r['L1_winrate']*100:>7.1f}%"
                     f"{r['stop_share']*100:>8.1f}%{r['L1_t']:>7.2f}{v:>6}")
            if a.diag and "L0_curve" in r:
                L.append("              持有期诊断曲线 L0毛(点): " +
                         "  ".join(f"{k2}根={v2}" for k2, v2 in r["L0_curve"].items()))
        vd = verdict(rows)
        summary[nm] = {"verdict": vd, "rows": rows}
        L.append(f"{'':14}└─ 样本量够的品种 {vd['n_sym']} 个；过硬门槛 {vd['pass_hard']} 个"
                 f"（需 ≥{vd['need']}）；其中显著 {vd['pass_sig']} 个  ⇒  **{vd['verdict']}**")
        L.append("-" * 112)

    L.append("")
    L.append(f"⚠️ 本工具只能「判死」，不能「判生」：窗口 {_win_dates} 非样本外（已记入运行台账），"
             f"任何 ✅ 都不构成 edge 证据。")
    L.append("⚠️ 批量模式（--all）跑 N 个信号 = N 次比较；**禁止挑最好的那个宣称有效**（规则 2 禁区）。")
    txt = "\n".join(L)
    print(txt)
    tag = "ALL" if a.all else names[0]
    if abs(a.stop - STOP_ATR_MULT) > 1e-9:
        tag += f"_stop{a.stop:g}"
    if a.period != "5min":
        tag += f"_{a.period}"
    tag += f"_hold{HOLD}"
    out = os.path.join(_HERE, f"体检结果_{tag}.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    with open(os.path.join(_HERE, f"体检结果_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n[done] {out}")


def _selftest(keys, n_probe=200, seed=7):
    """未来函数「截断检验」：用 data[:i+1] 重算 sig[i]，须与全量结果逐一相等。

    ⚠️ 性能：早期版本对每根抽查都重算整段（5.3 万根 × 上万次抽样）⇒ 根本跑不完。
    现在只取**尾部 400 根**（sig[i] 只依赖最近 ~32 根：MA13 + 斜率 5 + ATR14），
    数学上等价、速度可行。窗口长度 W 远大于依赖长度，不存在边界效应。
    """
    W = 400
    rng = np.random.default_rng(seed)
    print(f"{'信号':<15}{'品种':<7}{'抽查':>6}{'不一致':>8}  结论")
    bad_total = 0
    for nm, (_, fn) in SIGNALS.items():
        for k in keys:
            df = load(k)
            if df is None or len(df) < 500:
                continue
            o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
            l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
            N = len(c)
            sig_full = fn(o, h, l, c, _atr(h, l, c, ATR_N), _sma(c, MA_N))
            probes = rng.integers(max(MA_N + SLOPE_N + ATR_N + 5, 60), N, size=n_probe)
            bad = 0
            for i in probes:
                j0 = max(0, i + 1 - W)
                oo, hh, ll, cc = o[j0:i + 1], h[j0:i + 1], l[j0:i + 1], c[j0:i + 1]
                sf = fn(oo, hh, ll, cc, _atr(hh, ll, cc, ATR_N), _sma(cc, MA_N))
                v1, v2 = sig_full[i], sf[-1]      # 切片末元素 = 原始第 i 根
                if np.isnan(v1) and np.isnan(v2):
                    continue
                if abs(float(np.nan_to_num(v1)) - float(np.nan_to_num(v2))) > 1e-9:
                    bad += 1
            bad_total += bad
            print(f"{nm:<15}{k:<7}{n_probe:>6}{bad:>8}  {'✅ 无未来函数' if bad == 0 else '❌ 有未来函数！'}")
    print(f"\n合计不一致 {bad_total} 处 ⇒ {'全部通过 ✅' if bad_total == 0 else '存在未来函数 ❌'}")


if __name__ == "__main__":
    main()
