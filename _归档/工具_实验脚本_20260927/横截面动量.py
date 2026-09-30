# -*- coding: utf-8 -*-
"""机制 B｜横截面动量 —— 研究级回测器（跨品种多空组合）
==========================================================
机制一句话
----------
**同一时点，相对最强的品种继续强、相对最弱的继续弱** —— 赚资金在品种间轮动的钱。
（做多过去 N 日涨得最好的 k 只，做空跌得最差的 k 只，等名义额、净敞口 0。）

为什么它值得做（研究路线图 §7.12 B）
------------------------------------
**天然对冲「环境」**：全市场同涨同跌的部分被多空抵消 ⇒ 直接回应本项目最大发现
「**环境 > 策略**」（三策略按年份同向涨跌，均值 ±4~13%）。五个已死策略全是单品种
时序暴露，都不具备这个性质。

为什么是「研究级」而不是接引擎
------------------------------
`lib/engine.py` 的每个回测/实盘函数**只接一个品种**（`run_backtest_*(symbol_key, ...)`），
「同时持多空多品种」属于**模块 5 的组合运行器**范畴（路线图 §7.12 B 的风险③）。
在花大工程改引擎之前，先用**最便宜的代价**回答模块 3 的问题：
**这个机制到底有没有边缘？** ⇒ 本文件就是这个「最便宜的证否器」。
若结论为否，引擎改造直接省掉；若为是，再去做组合运行器。

★★ 设计一：**自算夏普，绝不调用 tqsdk 的 get_sharp**
----------------------------------------------------------------
2026-09-25 实测（`工具/_输出/_日线校准_结果.md` §三）：`tqsdk/tafunc.py:1478` 的
    `get_sharp(series, trading_days_of_year=250, r=0.025)`
**内含无风险利率 2.5%** ⇒ `夏普 = √250·(μ_d − rf)/σ_d`。拿它做 t 检验会得到
`t = √(250T)·μ_d/σ_d − √(250T)·rf/σ_d`，**第二项只依赖样本长度与波动、与 μ 无关**
⇒ **样本越长，这个「负 t 假象」越大**（PVC 8.77 年被凭空扣掉 −4.80）。
本文件自己算权益曲线、自己算夏普（Rf = 0），**从根上不存在这个坑**。
⇒ 这也顺带兑现了 A 报告里留下的待办：「导出 equity_curve 自算 t」。

★ 设计二：价格用**指数合约 KQ.i@**，不用主连 KQ.m@
------------------------------------------------------
2026-09-26 实测（侦察输出见报告 §一）：主连 KQ.m@ **在换月处有硬跳空**：
    RU 每年 8 月 +9~19%（2018-08-08 单日 +19.2%）／JD 每年 4 月 +15~31%
    ／M、JM 每年 12 月 +13~18%
这些不是行情，是**换月价差**。而 20 日动量的量级只有 ±5% ⇒ 跳空会直接淹没信号，
让「每年 8 月的橡胶」「每年 4 月的鸡蛋」**机械地变成最强品种**（纯假信号）。
换用指数 KQ.i@（按持仓量加权的连续序列）后同口径极值降到：
    RU +19.2% → **+6.4%**｜JD +31.7% → **+5.7%**｜M、JM 的换月尖刺消失，
剩余尖刺全部落在**真实事件日**（2020-02-03 疫情、2021-11 能源危机、2022-07 棕榈油崩）。

★★ 设计三：收益必须拆成「隔夜段 + 日内段」——否则动量收益被系统性虚高
------------------------------------------------------------------------
信号在 `sp` 日**收盘**算出，调仓在 `sp+1` 日**开盘**执行（规则 1：下单在下一根开盘）。
若图省事按「收盘→收盘」算 `sp+1` 的收益，就把 `close[sp] → open[sp+1]` 这段
**隔夜跳空**也算进去了 —— 可那段我们**手里没有仓位**。而动量策略的跳空通常顺势，
这会让收益**系统性虚高**（等于凭空捡了一个顺势跳空）。
⇒ 正确拆法（本文件采用）：
    隔夜收益 `ov[i] = open[i]/close[i-1] − 1`  ← 由**上一期的旧权重**赚
    日内收益 `id[i] = close[i]/open[i] − 1`   ← 由**当期的权重**赚
    `组合日收益[i] = W[i-1]·ov[i] + W[i]·id[i]`
非调仓日 `W[i] == W[i-1]`，自动退化为普通的「收盘→收盘」，口径无损。

⚠️ 诚实边界（结论方向因此偏乐观，所以「失败」是硬失败）
  · 指数**不可交易**：真实交易要落到具体合约，还有**换月成本**没计 ⇒ 本结果是**上界**。
  · ⇒ 若上界都过不了门槛，机制可以**确定性地否掉**，不需要再补换月成本模型。

判据（**跑前写死**，见 `研究路线图.md` §7.12.5 B）
--------------------------------------------------
主判据：`t = 自算年化夏普 × √年数`，与门槛 `t = √(2·ln K)` 比较。
    · `t > 门槛`      ⇒ **显著为正**，机制 B 成立，值得做组合运行器
    · `|t| ≤ 门槛`    ⇒ **与零无法区分** ⇒ 机制 B 也否掉
    · `t < −门槛`     ⇒ **显著为负**（注意：**不许反着做**，换符号依然不显著）
纪律：结果不管好坏都接受，**不许「再跑一次指望翻案」**；跑完**必须登记 K +1**。

参数（**预注册：一次性按学术常规设定，不扫描、不择优** —— 遵守规则 2 与规则 4）
-----------------------------------------------------------------------------
    N = 20   排序窗口（过去 20 个交易日收益）—— Jegadeesh-Titman 一个月形成期
    H = 20   调仓周期（每 20 个交易日调一次）
    k = 2    多空各取 2 只（7 品种 ⇒ top2 / bottom2，中间 3 只不碰）
    多空各占 **50% 名义**（总名义 100%、净敞口 0，dollar-neutral 标准口径）
样本：`2016-01-05`（7 品种日线交集硬上限）→ 预热 150 天 ⇒ 评估 `~2016-06` 起，
      止于 **`2025-03-12`**（干净样本上限），**与 A 口径对齐**，便于两条机制直接比 t。
成本：每腿每次换手 `|Δ权重| × 单边费率`；单边费率 = `(手续费 + 2跳) / (价格 × 乘数)`
      —— 手续费取 `品种池/*.py` 的「成本」字段；滑点按项目口径 `slip_ticks=1 ⇒ 实盘 2 跳`。

用法：
    python 工具/横截面动量.py                # 主口径跑一次
    python 工具/横截面动量.py --干跑          # 只打印将要跑什么，不联网
"""
import os
import sys
import math
import json
import glob
import time
from datetime import date, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ============================================================
# 预注册参数（跑前写死，不扫描、不择优）
# ============================================================
品种默认 = ["RU", "JM", "P", "M", "V", "JD", "SR"]
排序窗口N = 20            # 过去 N 个交易日收益
调仓周期H = 20            # 每 H 个交易日调一次
每侧只数k = 2             # 多空各 k 只
数据起点 = date(2016, 1, 5)      # 7 品种日线交集硬上限（实测）
评估止 = date(2025, 3, 12)       # 干净样本上限（路线图 §7.12.1）
预热天数 = 150                   # 与 A（工具/日线校准.py）对齐
滑点跳数 = 2                     # slip_ticks=1 ⇒ 实盘 2 跳（项目口径铁律）


def 读品种(key):
    import importlib.util as ilu
    p = os.path.join(ROOT, "品种池", f"{key}.py")
    spec = ilu.spec_from_file_location(f"_sp_{key}", p)
    m = ilu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.品种


def 指数代码(主连):
    """KQ.m@DCE.v → KQ.i@DCE.v（主连 → 指数）。"""
    return 主连.replace("KQ.m@", "KQ.i@")


def 读K():
    """Σ 各账本 累计试验次数（与 工具/回测体检.py 同口径）。"""
    tot = 0
    for f in sorted(glob.glob(os.path.join(_HERE, "_账本", "*.json"))):
        try:
            d = json.load(open(f, encoding="utf-8"))
            tot += int(d.get("累计试验次数") or 0)
        except Exception:
            pass
    return tot


def 门槛t(K):
    return math.sqrt(2 * math.log(K)) if K > 1 else 0.0


def 判决(t, 门):
    if t > 门:
        return "显著为正"
    if t < -门:
        return "显著为负"
    return "与零无法区分"


# ============================================================
# 取数
# ============================================================
def 拉数据(keys):
    """返回 (开, 收) 两个 DataFrame，index = 共同交易日，columns = keys。"""
    import config_account
    import pandas as pd
    from tqsdk import TqApi, TqAuth

    USER, PWD = config_account.check()
    api = TqApi(auth=TqAuth(USER, PWD), web_gui=False)
    各 = {}
    try:
        for k in keys:
            p = 读品种(k)
            sym = 指数代码(p["主连"])
            ks = api.get_kline_serial(sym, 86400, data_length=4000)
            上次 = -1
            t0 = time.time()
            while time.time() - t0 < 45:
                try:
                    api.wait_update(deadline=time.time() + 6)
                except Exception:
                    break
                现在 = int((ks["datetime"] > 0).sum())
                if 现在 > 0 and 现在 == 上次:
                    break
                上次 = 现在
            d = ks[ks["datetime"] > 0].copy()
            # 基准是 1970-01-01 08:00 北京 ⇒ 按 UTC 解析即得到北京时间
            d["日期"] = pd.to_datetime(d["datetime"] / 1e9, unit="s", utc=True) \
                          .dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
            d = d.set_index("日期")
            d = d[~d.index.duplicated(keep="last")]
            各[k] = d[["open", "close"]].astype(float)
            print("  [%s] %-9s %-16s %s ~ %s  %d 根"
                  % (k, p["中文名"], sym,
                     d.index[0].strftime("%Y-%m-%d"), d.index[-1].strftime("%Y-%m-%d"),
                     len(d)))
    finally:
        api.close()

    import pandas as pd
    收 = pd.DataFrame({k: 各[k]["close"] for k in keys}).dropna()
    开 = pd.DataFrame({k: 各[k]["open"] for k in keys}).dropna()
    共同 = 收.index.intersection(开.index)
    return 开.loc[共同].sort_index(), 收.loc[共同].sort_index()


# ============================================================
# 单边费率（元/手 → 名义金额占比）
# ============================================================
def 单边费率(价格, 品种):
    元每手 = float(品种["成本"]) + 滑点跳数 * float(品种["一跳"]) * float(品种["乘数"])
    名义 = 价格 * float(品种["乘数"])
    return 元每手 / 名义 if 名义 > 0 else 0.0


def 组合收益(权重, ov, id_):
    """把「隔夜段 + 日内段」合成组合日收益（**乘法精确式**）。

        W[i-1] 赚隔夜段 ov[i]，W[i] 赚日内段 id_（调仓在 i 的开盘完成）
        ⇒ 日收益 = (1 + ΣW[i-1]·ov) × (1 + ΣW[i]·id) − 1

    ⚠️ 不能写成 `ΣW[i-1]·ov + ΣW[i]·id`：那是**加法近似**，会系统性漏掉
       二阶项 `ov×id`（单日 ~3e-6，但 2200 天累计 ~0.07%，且方向固定偏低）。
        乘法式对恒权重持仓**逐位精确**：`(1+ov)(1+id) = close[i]/close[i-1]`
        ⇒ 连乘自动望远镜化。自检项 ① 就是验这个。
    """
    import numpy as np
    W前 = np.vstack([np.zeros((1, 权重.shape[1])), 权重[:-1]])
    return (1.0 + (W前 * ov).sum(axis=1)) * (1.0 + (权重 * id_).sum(axis=1)) - 1.0


# ============================================================
# 仪器自检（不消耗 K —— 这是量具校准，不是机制实验）
# ============================================================
def 自检(开, 收, keys):
    """三个独立校验，任一失败就不许跑实验（否则会污染 K 与判决）。

    ① 恒权重 1.0 单腿 ⇒ 权益必须**逐位等于**该品种价格比（验收益合成没漏项）
    ② 全零权重 ⇒ 日收益恒为 0（验没凭空造收益）
    ③ (1+隔夜)(1+日内) 必须**逐位等于** 收盘比（验拆分自洽）
    """
    import numpy as np
    n, T = len(keys), len(收)
    CL, OP = 收.values, 开.values
    ov = np.zeros((T, n))
    id_ = np.zeros((T, n))
    ov[1:] = OP[1:] / CL[:-1] - 1.0
    id_[1:] = CL[1:] / OP[1:] - 1.0
    ok = True

    W = np.zeros((T, n))
    W[:, 0] = 1.0
    实际 = float(np.prod(1.0 + 组合收益(W, ov, id_)))
    理论 = CL[-1, 0] / CL[0, 0]
    差 = abs(实际 - 理论) / 理论
    print("  ① 恒权重单腿 vs 价格比：实际 %.10f ｜ 理论 %.10f ｜ 相对差 %.2e  %s"
          % (实际, 理论, 差, "OK" if 差 < 1e-9 else "**失败**"))
    ok = ok and 差 < 1e-9

    m = float(abs(组合收益(np.zeros((T, n)), ov, id_)).max())
    print("  ② 全零权重日收益最大值：%.2e  %s" % (m, "OK" if m < 1e-15 else "**失败**"))
    ok = ok and m < 1e-15

    d = float(abs((1 + ov[1:]) * (1 + id_[1:]) - CL[1:] / CL[:-1]).max())
    print("  ③ (1+隔夜)×(1+日内) vs 收盘比：最大差 %.2e  %s"
          % (d, "OK" if d < 1e-12 else "**失败**"))
    ok = ok and d < 1e-12

    # ④ 口径体检：指数合约的 open 必须是**真实**开盘价，不能是空值/等于前收
    _ovs, _ids = float(ov[1:].std()), float(id_[1:].std())
    _零隔夜 = float((abs(ov[1:]) < 1e-12).mean())
    print("  ④ 口径体检：隔夜段 std %.3f%% ｜ 日内段 std %.3f%% ｜ 隔夜恒零占比 %.1f%%"
          % (_ovs * 100, _ids * 100, _零隔夜 * 100))
    _ok4 = 0.0005 < _ovs < _ids and _零隔夜 < 0.20
    print("     判据：0.05%% < 隔夜std < 日内std 且 隔夜恒零 < 20%%  ⇒ %s"
          % ("OK（open 为真实开盘价）" if _ok4 else "**异常，需查 open 字段**"))
    ok = ok and _ok4

    print("  ⇒ 仪器自检：%s" % ("全部通过" if ok else "**未通过 —— 禁止跑实验**"))
    return ok


# ============================================================
# 回测主循环
# ============================================================
def 跑(开, 收, keys, 起, 止):
    import numpy as np

    全掩 = np.array([d.date() <= 止 for d in 收.index])
    OP = 开.values[全掩]
    CL = 收.values[全掩]
    日期 = 收.index[全掩]
    T, n = CL.shape

    位置起 = int(np.searchsorted(日期.values, np.datetime64(起)))
    if 位置起 < 排序窗口N:
        raise SystemExit("预热不足：%d 根 < 排序窗口 %d" % (位置起, 排序窗口N))
    if T - 位置起 < 调仓周期H * 2:
        raise SystemExit("评估区太短：%d 根" % (T - 位置起))

    # ---- 隔夜段 / 日内段 ----
    ov = np.zeros((T, n))
    id_ = np.zeros((T, n))
    ov[1:] = OP[1:] / CL[:-1] - 1.0
    id_[1:] = CL[1:] / OP[1:] - 1.0

    # ---- 单边费率矩阵（按当日价格折算）----
    费率 = np.zeros((T, n))
    for j, k in enumerate(keys):
        p = 读品种(k)
        for i in range(T):
            费率[i, j] = 单边费率(CL[i, j], p)

    # ---- 权重（信号只用已收盘数据；调仓在下一根开盘执行）----
    权重 = np.zeros((T, n))
    信号日 = list(range(位置起, T - 1, 调仓周期H))
    记录 = []
    上期 = np.zeros(n)
    for si, sp in enumerate(信号日):
        动量 = CL[sp] / CL[sp - 排序窗口N] - 1.0
        名次 = np.argsort(-动量)
        目标 = np.zeros(n)
        for j in 名次[:每侧只数k]:
            目标[j] = +0.5 / 每侧只数k
        for j in 名次[-每侧只数k:]:
            目标[j] = -0.5 / 每侧只数k
        下一生效 = 信号日[si + 1] + 1 if si + 1 < len(信号日) else T
        权重[sp + 1:下一生效] = 目标
        记录.append({
            "信号日": 日期[sp].strftime("%Y-%m-%d"),
            "生效日": 日期[sp + 1].strftime("%Y-%m-%d"),
            "多头": [keys[j] for j in 名次[:每侧只数k]],
            "空头": [keys[j] for j in 名次[-每侧只数k:]],
            "换手": float(np.abs(目标 - 上期).sum()),
            "成本": float((np.abs(目标 - 上期) * 费率[sp + 1]).sum()),
            "动量": {keys[j]: round(float(动量[j]), 4) for j in 名次},
        })
        上期 = 目标

    # ---- 成本（在调仓生效日的开盘一次性扣）----
    成本 = np.zeros(T)
    for sp in 信号日:
        i = min(sp + 1, T - 1)
        前 = 权重[i - 1] if i > 0 else np.zeros(n)
        成本[i] = float((np.abs(权重[i] - 前) * 费率[i]).sum())
    # 校验：**非调仓日一分钱都不能扣**；并显式记录「零换手调仓」（标的完全没变）
    _应有 = sorted({min(sp + 1, T - 1) for sp in 信号日})
    _非调仓 = sorted(set(range(T)) - set(_应有))
    if _非调仓 and float(np.abs(成本[_非调仓]).max()) > 0:
        raise SystemExit("扣费时点错误：非调仓日也扣了费")
    _零换手 = [x["信号日"] for x in 记录 if x["换手"] == 0.0]
    if _零换手:
        print("  ⚠️ %d/%d 次调仓的标的与上期**完全相同**（换手 0、不扣费）：%s"
              % (len(_零换手), len(记录), ", ".join(_零换手)))

    # ---- 组合日收益 = W[i-1]·隔夜 + W[i]·日内 − 成本（乘法精确式）----
    毛 = 组合收益(权重, ov, id_)
    净 = np.maximum(毛 - 成本, -0.99)

    # ---- 只取评估区间 ----
    m = np.array([d.date() >= 起 for d in 日期])
    毛, 净, 成本, 权重e = 毛[m], 净[m], 成本[m], 权重[m]
    ov_e, id_e = ov[m], id_[m]
    W前 = np.vstack([np.zeros((1, n)), 权重[:-1]])

    权益 = np.cumprod(1.0 + 净)
    毛权益 = np.cumprod(1.0 + 毛)
    年数 = (日期[m][-1].date() - 日期[m][0].date()).days / 365.25
    交易日 = len(净)
    年化 = 权益[-1] ** (1 / 年数) - 1 if 年数 > 0 else 0.0
    毛年化 = 毛权益[-1] ** (1 / 年数) - 1 if 年数 > 0 else 0.0
    # ★ 自算夏普：Rf = 0（本文件从根上不碰 tqsdk 的 get_sharp，故无 Rf 陷阱）
    sd = 净.std(ddof=1)
    夏普 = (净.mean() / sd) * math.sqrt(250) if sd > 0 else 0.0
    t = 夏普 * math.sqrt(年数)
    峰 = np.maximum.accumulate(权益)
    回撤 = float((权益 / 峰 - 1).min())

    分年 = {}
    年标 = np.array([d.year for d in 日期[m]])
    for y in sorted(set(年标)):
        mm = 年标 == y
        分年[int(y)] = float(np.prod(1 + 净[mm]) - 1)

    多头腿 = float((np.where(权重e > 0, 权重e, 0) * (ov_e + id_e)).sum())
    空头腿 = float((np.where(权重e < 0, 权重e, 0) * (ov_e + id_e)).sum())

    return {
        "年数": 年数, "交易日": 交易日, "调仓次数": len(信号日),
        "年化": 年化, "毛年化": 毛年化, "夏普": 夏普, "t": t,
        "总收益": float(权益[-1] - 1), "回撤": 回撤,
        "成本累计": float(成本.sum()), "毛累计": float(毛.sum()),
        "净累计": float(净.sum()),
        "平均换手": float(np.mean([x["换手"] for x in 记录])) if 记录 else 0.0,
        "隔夜占比": float((W前 * ov)[m].sum() / 毛.sum() * 100) if abs(毛.sum()) > 1e-9 else 0.0,
        "分年": 分年, "多头腿贡献": 多头腿, "空头腿贡献": 空头腿,
        "记录": 记录, "起": str(日期[m][0].date()), "止": str(日期[m][-1].date()),
    }


def main():
    argv = sys.argv[1:]
    干跑 = "--干跑" in argv
    自检模式 = "--自检" in argv
    keys = 品种默认
    if "--keys" in argv:
        keys = [k.strip() for k in argv[argv.index("--keys") + 1].split(",") if k.strip()]

    K = 读K()
    门 = 门槛t(K)
    评估起 = 数据起点 + timedelta(days=预热天数)
    年数0 = (评估止 - 评估起).days / 365.25

    print("=" * 100)
    print(" 机制 B｜横截面动量 ｜ 研究级回测器（跨品种多空组合）")
    print("=" * 100)
    print(" 机制：做多过去 %d 日最强的 %d 只、做空最弱的 %d 只，多空各 50%% 名义（净敞口 0）"
          % (排序窗口N, 每侧只数k, 每侧只数k))
    print(" 品种池：%s（%d 只）" % (" ".join(keys), len(keys)))
    print(" 价格口径：**指数合约 KQ.i@**（主连 KQ.m@ 换月跳空达 +19~31%，会淹没 20 日动量）")
    print(" 收益口径：拆「隔夜段（旧权重）+ 日内段（新权重）」，杜绝开盘建仓偷吃隔夜跳空")
    print(" 样本：数据 %s 起（交集硬上限）｜ 预热 %d 天 ⇒ 评估 %s ~ %s（%.2f 年）"
          % (数据起点, 预热天数, 评估起, 评估止, 年数0))
    print(" 成本：每腿换手 |Δw| × 单边费率；单边 =（手续费 + %d 跳）/（价格 × 乘数）"
          % 滑点跳数)
    print(" 门槛 t = √(2·lnK)，K=%d ⇒ **门槛 t = %.3f**；过线所需年化夏普 = %.2f"
          % (K, 门, 门 / math.sqrt(年数0)))
    print(" ★ 夏普为**本脚本自算（Rf=0）**，不调用 tqsdk get_sharp ⇒ 无 Rf 陷阱")
    print("=" * 100)

    if 干跑:
        print("\n [干跑] 将拉取下列指数合约的日线（open + close）：")
        for k in keys:
            p = 读品种(k)
            print("   %-5s %-9s %s  手续费 %.3f 元/手 ｜ 一跳 %.2f × %d 手 = %.1f 元"
                  % (k, p["中文名"], 指数代码(p["主连"]),
                     float(p["成本"]), float(p["一跳"]), int(p["乘数"]),
                     float(p["一跳"]) * float(p["乘数"])))
        print("\n 去掉 --干跑 即真正开跑。")
        return 0

    print("\n 取数中……")
    开, 收 = 拉数据(keys)
    print("  ⇒ 共同交易日 %d 天 ｜ %s ~ %s"
          % (len(收), 收.index[0].strftime("%Y-%m-%d"), 收.index[-1].strftime("%Y-%m-%d")))

    if 自检模式:
        print("\n" + "=" * 100)
        print(" 【零】仪器自检（**不消耗 K** —— 这是量具校准，不是机制实验）")
        print("=" * 100)
        ok = 自检(开, 收, keys)
        if not ok:
            print("\n ⛔ 仪器未通过 ⇒ **不许跑实验**（会污染 K 与判决）。先修代码。")
            return 1
        print("\n ✅ 仪器通过 ⇒ 去掉 --自检 即真正跑实验。")
        return 0

    print("\n 回测中……")
    r = 跑(开, 收, keys, 评估起, 评估止)

    print("\n" + "=" * 100)
    print(" 【一】主结果")
    print("=" * 100)
    print("  评估区间      %s ~ %s（%.2f 年 / %d 个交易日）"
          % (r["起"], r["止"], r["年数"], r["交易日"]))
    print("  调仓次数      %d 次（每 %d 交易日一次，平均换手 %.2f 名义）"
          % (r["调仓次数"], 调仓周期H, r["平均换手"]))
    print("  年化收益      **%+.2f%%**（毛利 %+.2f%%）"
          % (r["年化"] * 100, r["毛年化"] * 100))
    print("  累计收益      %+.2f%%" % (r["总收益"] * 100))
    print("  年化夏普      **%.2f**（自算，Rf=0）" % r["夏普"])
    print("  ★ t 值        **%+.2f**   门槛 %.3f   ⇒ **%s**"
          % (r["t"], 门, 判决(r["t"], 门)))
    print("  最大回撤      %.2f%%" % (r["回撤"] * 100))
    print("  成本         累计 %.2f%%（占毛利 %s）｜ 毛利累计 %.2f%%"
          % (r["成本累计"] * 100,
             ("%.0f%%" % (r["成本累计"] / r["毛累计"] * 100)) if abs(r["毛累计"]) > 1e-6 else "n/a",
             r["毛累计"] * 100))
    print("  时段分解     隔夜段占毛利 %+.0f%%（其余为日内段）" % r["隔夜占比"])
    print("  多空分解     多头腿 %+.2f%% ｜ 空头腿 %+.2f%%（各腿贡献之和，未扣成本）"
          % (r["多头腿贡献"] * 100, r["空头腿贡献"] * 100))

    print("\n" + "=" * 100)
    print(" 【二】分年收益（看是不是某几年贡献了全部）")
    print("=" * 100)
    for y, v in r["分年"].items():
        bar = "#" * min(int(abs(v) * 200), 60)
        print("   %d  %+7.2f%%  %s%s" % (y, v * 100, "-" if v < 0 else "", bar))
    正年 = sum(1 for v in r["分年"].values() if v > 0)
    print("   ⇒ 正收益年份 %d/%d" % (正年, len(r["分年"])))

    print("\n" + "=" * 100)
    print(" 【三】最近 6 次调仓明细（看信号长什么样）")
    print("=" * 100)
    for x in r["记录"][-6:]:
        print("   信号 %s → 生效 %s ｜ 多 %s ｜ 空 %s ｜ 换手 %.2f ｜ 成本 %.3f%%"
              % (x["信号日"], x["生效日"], ",".join(x["多头"]), ",".join(x["空头"]),
                 x["换手"], x["成本"] * 100))

    print("\n" + "=" * 100)
    print(" 【四】判决")
    print("=" * 100)
    print("  主判据：t = %+.2f vs 门槛 %.3f ⇒ **%s**" % (r["t"], 门, 判决(r["t"], 门)))
    print("  ⚠️ 边界：本结果**未计换月成本**（指数不可交易）⇒ 是**乐观上界**。")
    print("     上界都过不了 ⇒ 机制可以确定性地否掉；上界过了 ⇒ 才值得做组合运行器。")
    print("  ⚠️ 纪律：不许「再跑一次指望翻案」（判决清单第 4 问）。")
    print("  ⚠️ 本次必须登记 K +1：当前 K=%d ⇒ 跑完 K=%d，门槛 %.3f -> %.3f"
          % (K, K + 1, 门, 门槛t(K + 1)))
    print("=" * 100)

    出 = os.path.join(_HERE, "_输出")
    os.makedirs(出, exist_ok=True)
    路径 = os.path.join(出, "_横截面动量_结果.json")
    with open(路径, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=2)
    print(" 明细已落盘：%s" % 路径)
    return 0


if __name__ == "__main__":
    sys.exit(main())
