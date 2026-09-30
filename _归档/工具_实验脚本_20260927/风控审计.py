# -*- coding: utf-8 -*-
"""
==========================================================
 风控审计：验证「每笔只亏 0.3%」这句话是不是真的
==========================================================

用法：
    python 工具/风控审计.py                    # 默认跑 01 双均线 V/M/RU 三份日线
    python 工具/风控审计.py --key V            # 只跑一个品种
    python 工具/风控审计.py --明细 <txt> --日志 <log> --key V

────────────────────────────────────────────────────────────
为什么不能只「看公式对不对」
────────────────────────────────────────────────────────────
`_risk_budget_hand` 的公式是恒等式，读一遍代码就知道它「应该」对。
真正的审计问题是：**引擎在运行时到底做了什么，以及做出来真的只亏 0.3% 吗？**

两者差别很大，因为设计风险 ≠ 实际亏损：
  · 手数只能取整 ⇒ 设计风险必然偏离目标风险（手数越少偏得越狠）
  · 跳空穿止损 ⇒ 实际亏损 > 设计风险（止损不是保证成交的价格）
  · 换月、反手、成本 ⇒ 都在设计之外

所以本工具不看公式，只看三份**引擎自己产生的证据**：

  ① 成交明细 txt（tqsdk 记的每笔成交）
  ② 回测日志里的「账户权益: X」行（tqsdk 打的每日权益）
  ③ 主连 K 线（用来复算信号根 ATR —— 唯一需要我算的量）

────────────────────────────────────────────────────────────
三条设计原则
────────────────────────────────────────────────────────────
1. **参数从明细表头解析，不从外部假设。**
   表头里写着「止损：初始 2×ATR(14)」「单笔目标风险 0.3% × 当前权益（复利）」
   「= 6.5 元/手/单边」「手数 = 该金额 ÷ (2×信号根ATR×5)」——
   审计用的止损倍数、风险%、成本、乘数**全部来自这份产物自己**，
   所以审计的对象是这份回测，而不是「我以为的参数」。

2. **先对账，再审计。**
   用「明细累积净盈亏 + 起始资金」去对「日志里的结束资金」。
   对不上 ⇒ 我的明细解析或成本模型有错 ⇒ 后面所有数字都不可信，直接停止。
   （这一步是自证清白：没人验证过的审计器，结论不值得信。）

3. **R 是从「实际盈亏 ÷ 目标风险」算的，不是从公式算的。**
   止损挂在 1R 处，所以亏损笔的 R 理应 ≈ −1。出现 −3R、−5R 就是真问题
   （跳空、换月、成本叠加……），这才是审计要抓的东西。
"""

import argparse
import io
import json
import math
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 默认批次：01 双均线日线（2026-09-26 仪器校准跑的三份，8.77 年）
默认批次 = [
    ("V", "回测结果/01_双均线/PVC/PVC_双均线_1d.txt", "工具/_输出/_校准_V.log"),
    ("M", "回测结果/01_双均线/豆粕/豆粕_双均线_1d.txt", "工具/_输出/_校准_M.log"),
    ("RU", "回测结果/01_双均线/天然橡胶/天然橡胶_双均线_1d.txt", "工具/_输出/_校准_RU.log"),
]


# ============================================================
# 一、解析：三份证据
# ============================================================
def 读文本(p):
    with io.open(p, encoding="utf-8", errors="ignore") as f:
        return f.read()


def 解析表头(文本):
    """从明细表头解析审计所需参数 —— 让产物自证，不引入外部假设。"""
    头 = { }

    m = re.search(r"止损：初始\s*([\d.]+)×ATR\((\d+)\)", 文本)
    if m:
        头["止损倍数"] = float(m.group(1))
        头["ATR周期"] = int(m.group(2))

    m = re.search(r"单笔目标风险\s*([\d.]+)%", 文本)
    if m:
        头["风险pct"] = float(m.group(1))

    m = re.search(r"=\s*([\d.]+)\s*元/手/单边", 文本)
    if m:
        头["单边成本"] = float(m.group(1))

    m = re.search(r"信号根ATR×([\d.]+)", 文本)
    if m:
        头["乘数"] = float(m.group(1))

    m = re.search(r"周期：(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", 文本)
    if m:
        头["起"] = m.group(1)
        头["止"] = m.group(2)

    # 权益口径
    if "当前权益（复利）" in 文本:
        头["权益口径"] = "当前权益（复利）"
    else:
        m = re.search(r"([\d,]+)\s*元（固定）", 文本)
        头["权益口径"] = ("固定 " + m.group(1)) if m else "未知"

    return 头


def 解析主连(文本):
    m = re.search(r"（(KQ\.[^）]+)）", 文本)
    return m.group(1) if m else ""


行模式 = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(BUY|SELL)\s+(OPEN|CLOSE)\s+(\d+)手\s+([\d.]+)")


def 解析明细(文本):
    """→ [(时间, 方向, offset, 手数, 价格), ...]"""
    行 = []
    for ln in 文本.splitlines():
        m = 行模式.match(ln.strip())
        if m:
            行.append((m.group(1), m.group(2), m.group(3),
                      int(m.group(4)), float(m.group(5))))
    return 行


def 配对(行):
    """开→平配成完整交易。返回 (交易列表, 未平仓的残渣)。"""
    交易, 持 = [], None
    for 时, 向, off, 手, 价 in 行:
        if off == "OPEN":
            if 持 is not None:
                raise SystemExit("明细异常：第 %s 笔开仓时仍有未平仓持仓" % 时)
            持 = (时, 向, 手, 价)
        else:
            if 持 is None:
                raise SystemExit("明细异常：第 %s 笔平仓时没有持仓" % 时)
            开时, 开向, 开手, 开价 = 持
            交易.append({
                "开时": 开时, "开向": 开向, "开手": 开手, "开价": 开价,
                "平时": 时, "平向": 向, "平手": 手, "平价": 价,
                # 开仓方向即持仓方向：BUY OPEN = 多头(+1)
                "方向": 1 if 开向 == "BUY" else -1,
                "手数一致": (开手 == 手),
            })
            持 = None
    return 交易, 持


权益模式 = re.compile(r"日期:\s*(\d{4}-\d{2}-\d{2}),\s*账户权益:\s*([\d.]+)")


def 解析权益(文本):
    """→ {日期: 账户权益}，tqsdk 自己打的每日权益。"""
    d = {}
    for m in 权益模式.finditer(文本):
        d[m.group(1)] = float(m.group(2))
    return d


def 解析终值(文本):
    """从 stat 段拿起始/结束资金。"""
    o = {}
    m = re.search(r"起始资金\s+([\d,\.]+)", 文本)
    if m:
        o["起始"] = float(m.group(1).replace(",", ""))
    m = re.search(r"结束资金\s+([\d,\.]+)", 文本)
    if m:
        o["结束"] = float(m.group(1).replace(",", ""))
    return o


# ============================================================
# 二、复算信号根 ATR（唯一由我算的量，与引擎 _atr 逐字一致）
# ============================================================
def 拉主连(api, 主连, sec=86400, n=8000):
    import pandas as pd
    ks = api.get_kline_serial(主连, sec, data_length=n)
    end = time.time() + 120
    上次 = -1
    while time.time() < end:
        api.wait_update(deadline=time.time() + 0.5)
        try:
            v = int((ks["datetime"] > 0).sum())
        except Exception:
            continue
        # ⚠️ 前端补零：等第一根非零永远不成立 ⇒ 判据 = 非零根数连续两次不增长
        if v > 0 and v == 上次:
            break
        上次 = v
    df = ks[ks["datetime"] > 0].copy()
    df["日期"] = [(pd.Timestamp(int(x)) + pd.Timedelta(hours=8)).strftime("%Y-%m-%d")
                  for x in df["datetime"]]
    return df


def 算ATR(df, period=14):
    """与引擎 lib/engine._atr 逐字一致：TR 的简单均值。"""
    import pandas as pd
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev).abs(),
                    (low - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ============================================================
# 三、审计一个品种
# ============================================================
def 审计一份(key, 明细路径, 日志路径, api, 行输出):
    def P(s=""):
        行输出.append(s)

    明细文本 = 读文本(明细路径)
    日志文本 = 读文本(日志路径)
    头 = 解析表头(明细文本)
    主连 = 解析主连(明细文本)
    终值 = 解析终值(日志文本)
    权益表 = 解析权益(日志文本)
    行 = 解析明细(明细文本)
    交易, 残渣 = 配对(行)

    P("=" * 108)
    P(" 【%s】%s" % (key, 主连))
    P("=" * 108)
    P("  表头解析（审计参数全部取自这份产物自己）：")
    P("    止损 = %.1f×ATR(%d) ｜ 风险 = %.3f%% ｜ 单边成本 = %.2f 元/手"
      % (头.get("止损倍数", float("nan")), 头.get("ATR周期", 14),
         头.get("风险pct", float("nan")), 头.get("单边成本", float("nan"))))
    P("    乘数 = %.0f ｜ 权益口径 = %s ｜ 区间 %s ~ %s"
      % (头.get("乘数", float("nan")), 头.get("权益口径", "?"),
         头.get("起", "?"), 头.get("止", "?")))
    P("  明细：%d 笔成交 ⇒ 配对 %d 笔完整交易%s"
      % (len(行), len(交易), ("（有 %d 笔未平仓）" % 1) if 残渣 else ""))
    不一致 = [t for t in 交易 if not t["手数一致"]]
    if 不一致:
        P("    ⚠️ %d 笔开平手数不一致（审计按开仓手数算）" % len(不一致))

    # ---------- ① 拉 K 线（对账要用它处理「回测结束时仍有持仓」那笔）----------
    df = 拉主连(api, 主连, 86400) if api is not None else None
    if df is None:
        return None

    # ---------- ② 对账 ----------
    乘数 = 头["乘数"]
    单边 = 头["单边成本"]
    累积毛 = 累积成本 = 0.0
    for t in 交易:
        累积毛 += (t["平价"] - t["开价"]) * t["方向"] * t["开手"] * 乘数
        累积成本 += 单边 * t["开手"] * 2
    累积净 = 累积毛 - 累积成本

    # ⚠️ 回测结束时若仍有持仓，它的**浮动盈亏 + 已扣的开仓手续费**同样在账户权益里。
    #   不补上会得到一笔假差额（RU 实测：577 元 ⇒ 对账差 0.4%，看起来像解析错了）。
    #   ⚠️⚠️ 浮盈必须用**回测结束那天**的收盘价算 —— K 线拉的是「最新的 8000 根」，
    #   覆盖到 2026 年 ⇒ 用 iloc[-1] 会拿 2026 年的价格去平 2025 年的仓（差 5.9%）。
    残渣净 = 0.0
    结束日 = max(权益表) if 权益表 else df["日期"].iloc[-1]
    if 残渣:
        时, 向, 手, 价 = 残渣
        行末 = df[df["日期"] == 结束日]
        末收 = float(行末["close"].iloc[0]) if len(行末) else float("nan")
        if 末收 != 末收:
            raise SystemExit("找不到结束日 %s 的 K 线 ⇒ 无法算未平仓浮盈" % 结束日)
        残渣净 = ((末收 - 价) * (1 if 向 == "BUY" else -1) * 手 * 乘数
                  - 单边 * 手)          # 平仓还没发生 ⇒ 只扣开仓那一次手续费

    起始 = 终值.get("起始", 0.0)
    结束 = 终值.get("结束", 0.0)
    推算 = 起始 + 累积净 + 残渣净
    差 = 推算 - 结束

    P()
    P("  【一】对账 —— 先证明我读的数是对的")
    P("  " + "-" * 104)
    P("    起始资金 %14s ｜ 已平仓累积毛盈亏 %+14s ｜ 成本 %12s ｜ 净 %+14s"
      % ("{:,.0f}".format(起始), "{:,.0f}".format(累积毛),
         "{:,.0f}".format(累积成本), "{:,.0f}".format(累积净)))
    if 残渣:
        P("    未平仓 1 笔（回测结束时仍持仓，按 %s 收盘价计浮盈）：%s"
          % (结束日, "{:+,.0f}".format(残渣净)))
    P("    推算末权益 %14s ｜ 日志末权益 %14s ｜ 差 %+12s"
      % ("{:,.0f}".format(推算), "{:,.0f}".format(结束), "{:,.0f}".format(差)))
    相对 = abs(差) / 结束 * 100 if 结束 else float("nan")
    if 相对 < 0.01:
        P("    ⇒ ✅ 对账通过（差 %.4f%%）—— 明细解析 + 成本模型与引擎一致" % 相对)
        对账通过 = True
    else:
        P("    ⇒ ❌ 对账不通过（差 %.2f%%）—— 后面所有数字都不可信，先修解析" % 相对)
        对账通过 = False

    # ---------- ③ 复算 ATR + 逐笔核对 ----------

    日索引 = {d: i for i, d in enumerate(df["日期"].tolist())}
    atr = 算ATR(df, 头.get("ATR周期", 14))
    止损倍数 = 头["止损倍数"]
    风险pct = 头["风险pct"]

    明细 = []
    缺 = 0
    for t in 交易:
        成交日 = t["开时"][:10]
        i = 日索引.get(成交日)
        if i is None:
            缺 += 1
            continue
        j = i - 1                      # 信号根 = 成交根的前一根（成交在下一根开盘）
        if j < 0:
            缺 += 1
            continue
        a = float(atr.iloc[j])
        if not (a == a) or a <= 0:
            缺 += 1
            continue
        # 当时权益：用信号根收盘后的权益（手数正是在那一刻算的）
        权 = 权益表.get(df["日期"].iloc[j])
        if 权 is None:
            权 = 权益表.get(成交日)      # 退一步：用成交日
        if 权 is None:
            缺 += 1
            continue
        目标 = 权 * 风险pct / 100.0
        设计 = t["开手"] * 止损倍数 * a * 乘数
        盈亏 = (t["平价"] - t["开价"]) * t["方向"] * t["开手"] * 乘数 - 单边 * t["开手"] * 2
        # 入场相对信号根收盘价的偏移：>0 表示「顺着自己方向入场」
        #   ⇒ 止损线（挂在信号根收盘价 ∓ 2ATR）离入场价更远 ⇒ 打止损亏得更多。
        #   这是止损之外的真实风险来源之一，单独量出来，免得算进「止损被穿」。
        信号收 = float(df["close"].iloc[j])
        入场偏 = (t["开价"] - 信号收) * t["方向"] / 信号收 if 信号收 else float("nan")
        明细.append({
            "成交日": 成交日, "方向": t["方向"], "手数": t["开手"],
            "开价": t["开价"], "平价": t["平价"], "ATR": a, "权益": 权,
            "信号收": 信号收, "入场偏": 入场偏,
            "目标风险": 目标, "设计风险": 设计,
            "偏离": (设计 / 目标 - 1) if 目标 else float("nan"),
            "盈亏": 盈亏, "R": (盈亏 / 目标) if 目标 else float("nan"),
            "R设计": (盈亏 / 设计) if 设计 else float("nan"),
        })

    if 缺:
        P("    （%d 笔无法归位：成交日不在 K 线 / ATR 未预热 / 权益缺失）" % 缺)

    P()
    P("  【二】逐笔核对 —— 设计风险 vs 目标风险（设计 = 手数×%.1f×ATR×%.0f）"
      % (止损倍数, 乘数))
    P("  " + "-" * 128)
    P("    抽样：前 4 笔 + 手数最大的 3 笔 + 偏离最大的 3 笔（去重）")
    P("    %-12s %-4s %4s %9s %9s %11s %11s %9s"
      % ("成交日", "方向", "手数", "信号根ATR", "当时权益", "目标风险", "设计风险", "偏离"))
    show, 已见 = [], set()
    for x in (明细[:4] + sorted(明细, key=lambda x: -x["手数"])[:3]
              + sorted(明细, key=lambda x: -abs(x["偏离"]))[:3]):
        if x["成交日"] in 已见:
            continue
        已见.add(x["成交日"])
        show.append(x)
    for x in show:
        P("    %-12s %-4s %4d %9.1f %9.0f %11.0f %11.0f %8.1f%%"
          % (x["成交日"], "多" if x["方向"] > 0 else "空", x["手数"], x["ATR"],
             x["权益"], x["目标风险"], x["设计风险"], x["偏离"] * 100))

    偏离 = [x["偏离"] for x in 明细 if x["偏离"] == x["偏离"]]
    if 偏离:
        均 = sum(偏离) / len(偏离)
        绝对 = sorted(abs(v) for v in 偏离)
        中位 = 绝对[len(绝对) // 2]
        P()
        P("    偏离分布（%d 笔）：均值 %+.1f%% ｜ 中位绝对偏离 %.1f%% ｜ 最大 %.1f%%"
          % (len(偏离), 均 * 100, 中位 * 100, max(绝对) * 100))
        # 按手数分档，看「手数越少、偏离越大」是否成立
        P("    按手数分档：")
        档 = {}
        for x in 明细:
            档.setdefault(x["手数"], []).append(abs(x["偏离"]))
        for k in sorted(档):
            v = 档[k]
            P("      手数 %2d：%3d 笔 ｜ 平均绝对偏离 %5.1f%%"
              % (k, len(v), sum(v) / len(v) * 100))

    # ★ 直接检验「每笔只亏 0.3% 这句话」：单笔设计风险 ÷ 当时权益
    真险 = [x["设计风险"] / x["权益"] * 100 for x in 明细 if x["权益"]]
    if 真险:
        排序 = sorted(真险)
        超预算 = [v for v in 真险 if v > 风险pct * 1.15]
        P()
        P("    ★ 直接检验「每笔只亏 %.2f%%」—— 单笔设计风险 ÷ 当时权益：" % 风险pct)
        P("      中位 %.2f%% ｜ 均值 %.2f%% ｜ 最大 %.2f%%"
          % (排序[len(排序) // 2], sum(真险) / len(真险), max(真险)))
        P("      超出预算 15%% 以上的笔数：%d / %d（%.0f%%）"
          % (len(超预算), len(真险), len(超预算) / len(真险) * 100))
        中ATR = sorted(x["ATR"] for x in 明细)[len(明细) // 2]
        够格 = 止损倍数 * 中ATR * 乘数 / (风险pct / 100.0)
        P("      够格权益（中位 ATR %.1f ⇒ 一手风险 %.0f 元）：**%.0f 元**"
          % (中ATR, 止损倍数 * 中ATR * 乘数, 够格))
        P("      ⇒ 本账户 40 万 ÷ 够格权益 %.0f 万 = %.0f%% ⇒ %s"
          % (够格 / 10000, 400000 / 够格 * 100,
             "预算基本能生效" if 400000 >= 够格 else
             "**本金不足，风险预算无法生效**（真实风险由 1 手决定）"))

    # ---------- ③ R 分布 ----------
    R = [x["R"] for x in 明细 if x["R"] == x["R"]]
    RD = [x["R设计"] for x in 明细 if x["R设计"] == x["R设计"]]
    P()
    P("  【三】R 分布 —— R = 实际盈亏 ÷ 目标风险（止损挂在 1R 处）")
    P("  " + "-" * 104)
    if R:
        负 = [v for v in R if v < 0]
        正 = [v for v in R if v > 0]
        P("    %d 笔 ｜ 正 %d 笔 ｜ 负 %d 笔" % (len(R), len(正), len(负)))
        P("    R：均值 %+.3f ｜ 中位 %+.3f ｜ 合计 %.2f ｜ 最差 %+.2f ｜ 最好 %+.2f"
          % (sum(R) / len(R), sorted(R)[len(R) // 2], sum(R), min(R), max(R)))
        if 负:
            排序负 = sorted(负)
            P("    亏损笔 R：均值 %+.3f ｜ 中位 %+.3f ｜ 最差 %+.2f"
              % (sum(负) / len(负), 排序负[len(排序负) // 2], min(负)))
            超 = [v for v in 负 if v < -1.0]
            P("    亏损超过 1R 的笔数：%d / %d（%.1f%%）"
              % (len(超), len(负), len(超) / len(负) * 100))

    if RD:
        负D = sorted(v for v in RD if v < 0)
        if 负D:
            P()
            P("    ★ 干净的判据：R设计 = 实际盈亏 ÷ 设计风险 —— 只看「止损有没有守住」")
            P("      它把「手数被抬高」这一层剥掉了（分母用的是这笔**实际**的设计风险）。")
            P("      亏损笔 R设计：均值 %+.3f ｜ 中位 %+.3f ｜ **最差 %+.2f**"
              % (sum(负D) / len(负D), 负D[len(负D) // 2], 负D[0]))
            穿 = [v for v in 负D if v < -1.15]
            P("      ⇒ %s"
              % ("✅ 都贴着 −1 ⇒ **止损守住了**：超损来自手数被抬高，不是止损失效"
                 if not 穿 else
                 "⚠️ %d 笔跌破 −1.15 ⇒ 止损被跳空穿过/滑价，需逐笔查" % len(穿)))

    入 = [x["入场偏"] for x in 明细 if x["入场偏"] == x["入场偏"]]
    if 入:
        P()
        P("    入场相对信号根收盘价的偏移（>0 = 顺着自己方向入场 ⇒ 止损线更远 ⇒ 亏更多）：")
        P("      均值 %+.3f%% ｜ 中位 %+.3f%% ｜ 最大 %+.3f%%"
          % (sum(入) / len(入) * 100, sorted(入)[len(入) // 2] * 100, max(入) * 100))
        P("      这是「隔夜跳空」在单笔风险上的量化 —— 信号在收盘算、单在次日开盘下，")
        P("      ATR 却按信号根收盘价量止损距离，中间那段跳空是我们必须承担的真实敞口。")

    if R:
        P()
        P("    ⇒ 读法：亏损笔 R 的中位若明显大于 −1（如 −0.6），说明多数亏损笔")
        P("       还没打到止损就被信号平掉了（止损是防线、不是常态出场）。")

    负D = sorted(v for v in RD if v < 0)
    负R = sorted(v for v in R if v < 0)
    真险 = [x["设计风险"] / x["权益"] * 100 for x in 明细 if x["权益"]]
    排序险 = sorted(真险)
    中ATR = sorted(x["ATR"] for x in 明细)[len(明细) // 2] if 明细 else float("nan")
    return {
        "key": key, "主连": 主连, "头": 头,
        "成交笔数": len(行), "配对交易": len(交易),
        "起": 起始, "毛": 累积毛, "成本": 累积成本, "净": 累积净,
        "未平仓": 残渣净 if 残渣 else None,
        "推算末": 推算, "日志末": 结束, "对账差": 差, "对账差pct": 相对,
        "对账通过": 对账通过,
        "可审笔数": len(明细),
        "R均值": (sum(R) / len(R)) if R else None,
        "R合计": sum(R) if R else None,
        "R最差": min(R) if R else None,
        "R设计中位": (负D[len(负D) // 2] if 负D else None),
        "R设计最差": (负D[0] if 负D else None),
        "R亏损中位": (负R[len(负R) // 2] if 负R else None),
        "偏离中位": (sorted(abs(v) for v in 偏离)[len(偏离) // 2] if 偏离 else None),
        "超1R占比": (len([v for v in 负R if v < -1.0]) / len(负R) if 负R else None),
        "真实风险中位pct": (排序险[len(排序险) // 2] if 排序险 else None),
        "真实风险最大pct": (max(排序险) if 排序险 else None),
        "超预算占比": (len([v for v in 真险 if v > 风险pct * 1.15]) / len(真险)
                    if 真险 else None),
        "够格权益": (止损倍数 * 中ATR * 乘数 / (风险pct / 100.0)
                  if 中ATR == 中ATR else None),
        "手数分布": _手数直方(明细),
        "明细": 明细,
    }


def _手数直方(明细):
    """→ '1手×54 / 2手×12'（按手数升序）—— 一眼看出「手数是不是被 1 手卡住」。"""
    档 = {}
    for x in 明细:
        档[x["手数"]] = 档.get(x["手数"], 0) + 1
    return " / ".join("%d手×%d" % (k, 档[k]) for k in sorted(档))


def _净(x):
    """JSON 里不留 NaN（非标准写法），一律转 None。"""
    if isinstance(x, dict):
        return {k: _净(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_净(v) for v in x]
    if isinstance(x, float) and x != x:
        return None
    return x


# ============================================================
def main():
    import config_account as CA
    from tqsdk import TqApi, TqAuth

    ap = argparse.ArgumentParser()
    ap.add_argument("--key", type=str, default="", help="只跑一个品种")
    ap.add_argument("--明细", type=str, default="")
    ap.add_argument("--日志", type=str, default="")
    ap.add_argument("--不落盘", action="store_true", help="只打印")
    args = ap.parse_args()

    批次 = 默认批次
    if args.key and args.明细 and args.日志:
        批次 = [(args.key.upper(), args.明细, args.日志)]
    elif args.key:
        批次 = [x for x in 默认批次 if x[0] == args.key.upper()]
        if not 批次:
            raise SystemExit("默认批次里没有 %s（要自定义请同时给 --明细 与 --日志）" % args.key)

    for _k, _m, _l in 批次:
        if not os.path.isfile(os.path.join(ROOT, _m)):
            raise SystemExit("找不到明细：%s" % _m)
        if not os.path.isfile(os.path.join(ROOT, _l)):
            raise SystemExit("找不到日志：%s" % _l)

    行输出 = []
    u, p = CA.check()
    api = TqApi(auth=TqAuth(u, p), web_gui=False)
    结果 = []
    try:
        for k, m, l in 批次:
            结果.append(审计一份(k, os.path.join(ROOT, m), os.path.join(ROOT, l), api, 行输出))
    finally:
        api.close()

    # ---- 汇总 ----
    def P(s=""):
        行输出.append(s)

    P()
    P("=" * 108)
    P(" 【四】汇总")
    P("=" * 108)
    P("  %-5s %5s %5s %9s %11s %10s %9s %10s %12s"
      % ("品种", "成交", "配对", "对账差", "真实风险中位", "超预算占比",
         "亏损R中位", "R设计最差", "够格权益"))
    P("  " + "-" * 100)
    for r in 结果:
        if not r:
            continue
        P("  %-5s %5d %5d %8.4f%% %10s %10s %9s %10s %12s"
          % (r["key"], r["成交笔数"], r["配对交易"], r["对账差pct"],
             ("%.2f%%" % r["真实风险中位pct"]) if r["真实风险中位pct"] is not None else "—",
             ("%.0f%%" % (r["超预算占比"] * 100)) if r["超预算占比"] is not None else "—",
             ("%+.2f" % r["R亏损中位"]) if r["R亏损中位"] is not None else "—",
             ("%+.2f" % r["R设计最差"]) if r["R设计最差"] is not None else "—",
             ("%.0f 万" % (r["够格权益"] / 10000)) if r["够格权益"] else "—"))
    P()
    P("  读表：真实风险中位 = 单笔设计风险 ÷ 当时权益（目标 %.2f%%）｜"
      " 够格权益 = 让「1 手」就落在预算内所需的本金"
      % (结果[0]["头"]["风险pct"] if 结果 and 结果[0] else 0.3))

    全通过 = all(r and r["对账通过"] for r in 结果)
    P()
    P("  对账：%s" % ("✅ 全部通过 ⇒ 明细解析与成本模型与引擎逐位一致，后面的数可以信"
                      if 全通过 else "❌ 有不通过项 ⇒ 先修解析，别信后面的数"))
    过 = [r for r in 结果 if r]
    n = len(过)

    if 过:
        P()
        P("  %s结论（第 1 层 · 风险预算有没有生效）" % ("★ " if 全通过 else ""))
        P("  " + "-" * 104)
        偏 = [r["偏离中位"] for r in 过 if r["偏离中位"] is not None]
        真 = [r["真实风险中位pct"] for r in 过 if r["真实风险中位pct"] is not None]
        目标 = 过[0]["头"]["风险pct"]
        P("    手数分布：%s"
          % " ｜ ".join("%s: %s" % (r["key"], r["手数分布"]) for r in 过))
        if 真:
            P("    单笔真实风险（占权益）：%s ｜ 目标 %.2f%%"
              % (" / ".join("%.2f%%" % v for v in 真), 目标))
            超 = [r["超预算占比"] for r in 过 if r["超预算占比"] is not None]
            if 超:
                P("    超出预算 15%% 以上的笔数占比：%s"
                  % " / ".join("%.0f%%" % (v * 100) for v in 超))
        P("    够格权益（1 手即落在预算内所需本金）：%s"
          % " ｜ ".join("%s %.0f 万" % (r["key"], r["够格权益"] / 10000)
                        for r in 过 if r["够格权益"]))
        P()
        if any((r["够格权益"] or 0) > 400000 for r in 过):
            差品 = [r for r in 过 if (r["够格权益"] or 0) > 400000]
            P("    ⇒ ⚠️ **「每笔只亏 %.2f%%」在有品种上根本不成立**：" % 目标)
            for r in 差品:
                P("       %s：一手就要 %.0f 元风险（够格权益 %.0f 万，本账户 40 万只有 %.0f%%）"
                  % (r["key"], r["够格权益"] * 目标 / 100.0, r["够格权益"] / 10000,
                     400000 / r["够格权益"] * 100))
            P("       机制：手数取整后 `max(1, ...)` 强制开 1 手，而 1 手的止损亏损本身")
            P("             就超过预算 ⇒ **真实风险由「1 手」决定，与 risk_pct 参数脱钩**。")
            P("       这不是 bug，是「账户规模 vs 品种波动」的物理不匹配。")
            P("       对策（四选一，都不是改代码能解决的）：")
            P("         ① 给该品种放大本金 ② 提高 risk_pct 到与 1 手匹配")
            P("         ③ 换一手风险更小的品种 ④ 明确接受它")
            P("            —— 那就不能再叫「每笔 0.30%」，而要写成「每笔 ≤ X%」（X 由 1 手定）")
        elif 真 and max(真) < 目标 * 1.5:
            P("    ⇒ ✅ 单笔真实风险基本贴着目标（最大偏离 %.1f%%），风险预算生效。"
              % ((max(真) / 目标 - 1) * 100))

        P()
        P("  %s结论（第 2 层 · 止损有没有守住）" % ("★ " if 全通过 else ""))
        P("  " + "-" * 104)
        RD = [r["R设计最差"] for r in 过 if r["R设计最差"] is not None]
        if RD:
            最差 = min(RD)
            P("    亏损笔 R设计（实际盈亏 ÷ **本笔实际设计风险**）：最差 %s"
              % " / ".join("%+.2f" % v for v in RD))
            if 最差 > -1.15:
                P("    ⇒ ✅ 止损守住了。超损来自「手数被抬高」，不是止损失效。")
            else:
                P("    ⇒ ⚠️ 有笔亏损超过本笔设计风险的 1.15 倍 ⇒ **止损被跳空穿过/滑价**。")
                入 = [max((x["入场偏"] for x in r["明细"]), default=0) for r in 过]
                P("      最大入场跳空：%s ⇒ 止损线挂在「信号根收盘价 ∓ 2×ATR」，"
                  % " / ".join("%+.2f%%" % (v * 100) for v in 入))
                P("      跳空直接跨过止损线时，成交价只能是跳空后的价格，滑价无法避免。")
                P("      ★ 这是**执行层的真实敞口**，与手数量化是两个独立问题：")
                P("        前者是「止损防不住跳空」（物理限制），后者是「1 手太粗」（账户规模）。")
            P("    ⇒ 两层是**独立**的问题，得分开处理：")
            P("       · 量化层（1 手太粗）→ 靠本金 / risk_pct / 换品种，见上面第 1 层")
            P("       · 执行层（止损防不住跳空）→ **这是物理限制，没有代码解**：")
            P("         期货有隔夜跳空与涨跌停，止损单只能在「穿价后的第一个可成交价」成交。")
            P("         唯一能做的是**承认它并计入预算** —— 把单笔最坏情况按 1R 估改为按更大倍数估；")
            P("         或让止损距离远离价格（更大周期 / 更长 ATR）⇒ 跳空占止损距离的比例变小。")
            P("         ⚠️ 关键：止损距离越远，跳空相对越小 —— 这正是「转大周期」的机制依据之一。")
            P("       · 另注：「跳空大就不开仓」属规则 2 禁止的行情过滤，此处只记录，不采纳。")

    print("\n".join(行输出))

    if not args.不落盘:
        出 = os.path.join(_HERE, "_输出")
        os.makedirs(出, exist_ok=True)
        后缀 = "" if not args.key else "_" + args.key.upper()
        with io.open(os.path.join(出, "_风控审计_控制台%s.txt" % 后缀), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write("\n".join(行输出) + "\n")
        with io.open(os.path.join(出, "_风控审计_结果%s.json" % 后缀), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(_净([r for r in 结果 if r]), ensure_ascii=False, indent=1))
        print("\n 已落盘：工具/_输出/_风控审计_控制台%s.txt" % 后缀)


if __name__ == "__main__":
    main()
