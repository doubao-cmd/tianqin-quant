# -*- coding: utf-8 -*-
"""
==========================================================
 腿数体检：N 条腿同时在场，真实分布到底长什么样？
==========================================================

用法：
    python 工具/腿数体检.py                     # 全部组合，全部可用区间
    python 工具/腿数体检.py --段 IS             # 只看样本内
    python 工具/腿数体检.py --keys V,M,RU       # 只看子集
    python 工具/腿数体检.py --K 6               # 组合层腿数上限（默认 6）
    python 工具/腿数体检.py --no-net            # 用缓存，不联网
    python 工具/腿数体检.py --自测              # 离线自测扫描线算法（不联网）

它回答的问题：
    `工具/组合风险.py` 给出的是【静态】推算 —— 假设 N 腿**全部同时在场**。
    它自己在结尾写着：
        「⚠️ 以上全部是【同时持仓】口径。真实运行时腿数在 1 腿和 N 腿之间来回跳。」

    这个「来回跳」的真实分布，**至今从未被算过**。本工具把它算出来。

做法（不碰引擎、不下单、不消耗样本）：
    1. 逐品种拉 30min 主连 K 线（免费接口，最近约 8900 根）
    2. 调 `策略池/<策略>.算信号` 逐根生成目标仓位（只用已收盘 K 线，无未来函数）
    3. 按「信号在第 i 根收盘产生、第 i+1 根开盘成交」转成持仓区间
    4. 扫描线算**时间加权**的腿数分布
    5. 再按开仓时刻顺序模拟 K 闸门，对比「有闸门 / 无闸门」

五块输出：
    【一】单腿口径：每条腿多久持仓一次、在场时间占比
    【二】腿数分布（无闸门）：自然状态下有几条腿在场
    【三】K 闸门模拟：按「先到先得」拒单后，分布变成什么样
    【四】敏感性：只开 k 条腿时，K 还有没有用（穷举全部子集）
    【五】结论：K 是摆设（从不触发）还是真闸门（频繁拒单）

⚠️ 两份口径别混：
    · `--口径 引擎`（默认）：含 ATR 止损 + 时间止损，与 engine.run_backtest_boll 逐行对齐
    · `--口径 信号`：只跑 boll_decision ⇒ 持仓偏长 ⇒ 腿数是**上界**
      （实测信号层单腿在场占比 70%、9 条腿平均 6.50 腿；加止损后降到 3.47 腿）

⚠️ 样本口径：免费接口 `get_kline_serial` 只能拿**最近** 8900 根，
   约 = 2023-08 起。`get_kline_data_series`（任意区间）**需要专业版**，用不了。
   所以 IS 段只能覆盖到 2023-08-24 之后（缺 2023-01~07）。报告中会显式标注。
"""

import argparse
import os
import pickle
import sys
import time
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
while ROOT != os.path.dirname(ROOT) and not os.path.isdir(os.path.join(ROOT, "lib")):
    ROOT = os.path.dirname(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 样本划分（研究路线图.md 7.3 定稿口径）
段表 = {
    "IS":  ("2023-01-01", "2025-06-30"),
    "VAL": ("2025-07-01", "2026-02-28"),
    "OOS": ("2026-03-01", "2026-09-13"),
}
默认根数 = 8900
默认K = 6
输出目录 = os.path.join(ROOT, "工具", "_输出")


# ============================================================
# 双写（控制台 + 报告文件）
# ============================================================
class 双写:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.f = open(path, "w", encoding="utf-8")

    def __call__(self, *a):
        s = " ".join(str(x) for x in a)
        print(s, flush=True)
        self.f.write(s + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


# ============================================================
# 组合 / 策略 / 品种 的读取
# ============================================================
def 扫组合():
    """扫 组合/ 目录 → [(策略名, 品种key, 文件名), ...]。文件名格式 <策略>__<key>.py"""
    目录 = os.path.join(ROOT, "组合")
    出 = []
    if not os.path.isdir(目录):
        return 出
    for fn in sorted(os.listdir(目录)):
        if not fn.endswith(".py") or fn.startswith("_") or "__" not in fn:
            continue
        策略名, _, key = fn[:-3].partition("__")
        if 策略名 and key:
            出.append((策略名, key.upper(), fn))
    return 出


def _载模块(path, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def 读策略(策略名):
    # 规范（2026-09-27）：一个策略一个文件夹 —— 策略池/<策略名>/<策略名>.py
    p = os.path.join(ROOT, "策略池", 策略名, 策略名 + ".py")
    if not os.path.isfile(p):                      # 兼容旧的「一个策略一个 .py」
        p = os.path.join(ROOT, "策略池", 策略名 + ".py")
    return _载模块(p, f"_策略_{策略名}")


def 读品种(key):
    p = os.path.join(ROOT, "品种池", key + ".py")
    if not os.path.isfile(p):
        return None
    return getattr(_载模块(p, f"_品种_{key}"), "品种", None)


def 读组合参数(文件名, 策略名, key):
    """读 组合/<策略>__<key>.py 的参数块（信号参数以组合层为准）。

    ⚠️ 组合层的参数块**不含 EXIT_OFF**（那是策略池的默认值），
       所以调用方必须做「策略池默认 ← 组合层覆盖」的合并，不能直接用。
    """
    try:
        m = _载模块(os.path.join(ROOT, "组合", 文件名), f"_组合_{策略名}_{key}")
        return dict(getattr(m, "参数", {}) or {}), int(getattr(m, "周期", 1800) or 1800)
    except Exception as e:
        print(f"  [警告] 读 {文件名} 失败（用策略池默认参数）：{e}", flush=True)
        return {}, 1800


# ============================================================
# 数据
# ============================================================
def 拉数据(keys, 根数, 缓存路径):
    """一门 API 拉全部品种 30min 主连。返回 {key: {"t": DatetimeIndex, "close": ndarray}}。"""
    import pandas as pd
    from tqsdk import TqApi, TqAuth
    import config_account as CA

    u, p = CA.check()
    api = TqApi(auth=TqAuth(u, p))
    出, 表 = {}, {}
    try:
        for k in keys:
            d = 读品种(k)
            if not d:
                print(f"  [跳过] 品种池里没有 {k}.py", flush=True)
                continue
            try:
                表[k] = api.get_kline_serial(d["主连"], 1800, data_length=根数)
            except Exception as e:
                print(f"  [跳过] {k} 订阅失败：{e}", flush=True)

        # ⚠️ 必须等【最早一根也就绪】，否则前段会被当成缺失（tqsdk 老坑）
        截止 = time.time() + 300
        while time.time() < 截止:
            api.wait_update(deadline=time.time() + 0.5)
            if all(float(x["datetime"].iloc[0]) > 0 and float(x["close"].iloc[0]) > 0
                   for x in 表.values()):
                break

        for k, kl in 表.items():
            t = pd.to_datetime(kl["datetime"].to_numpy(), unit="ns")
            出[k] = {
                "t": t,
                "open": kl["open"].to_numpy(dtype=float),
                "high": kl["high"].to_numpy(dtype=float),
                "low": kl["low"].to_numpy(dtype=float),
                "close": kl["close"].to_numpy(dtype=float),
            }
        return 出
    finally:
        try:
            api.close()
        except Exception:
            pass


# ============================================================
# 信号 → 持仓区间
# ============================================================
def 算区间(时刻, 收盘, 算信号, 完整参数):
    """逐根目标仓位 → 持仓区间 [(起, 止), ...]。

    执行口径（与引擎一致）：第 i 根**收盘后**产生目标仓位，第 i+1 根**开盘**成交。
        开仓时刻 = 时刻[i+1]；平仓时刻 = 时刻[j+1]。
    最后一根仍持仓 ⇒ 区间截到数据末尾。
    """
    import pandas as pd

    # ⚠️ 必须传 DataFrame：算信号 内部用 `len(k线表)` 取根数，
    #    传 {"close": series} 这种 dict 会让 len() = 1 ⇒ 只算一根 ⇒ 全 0 信号（踩过）。
    表 = pd.DataFrame({"close": pd.Series(收盘)})
    目标序列 = 算信号(表, 完整参数)
    n = len(目标序列)
    区间, 持向, 起点 = [], 0, None
    for i in range(n - 1):
        目标 = 目标序列[i]
        if 目标 == 持向:
            continue
        执行时刻 = 时刻[i + 1]
        if 持向 == 0:
            起点 = 执行时刻
        else:
            区间.append((起点, 执行时刻))
            if 目标 != 0:            # 反手（布林带不会发生，防御性处理）
                起点 = 执行时刻
            else:
                起点 = None
        持向 = 目标
    if 持向 != 0 and 起点 is not None:
        区间.append((起点, 时刻[n - 1]))
    return 区间


def 算区间_引擎口径(时刻, high, low, close, 完整参数, 起索引=0):
    """**与 engine.run_backtest_boll 逐行对齐**的持仓区间（默认口径）。

    为什么不能用 `算信号` 代替：
        `策略池/布林带.算信号` 只搬了 `boll_decision` 那一段状态机，
        **不含 ATR 止损、不含时间止损** —— 而那两条正是把持仓砍短的主力。
        实测（2023-08~2026-09，9 品种）信号层单腿在场占比高达 **70%**，
        真实值远低于此 ⇒ 信号层只是**上界**，用它算腿数会严重高估。

    本函数不复写引擎逻辑，而是**直接 import 引擎的纯函数**逐个复用：
        `_boll`（三线）、`_atr`（ATR）、`_dual_ma_stop_line` / `_dual_ma_stop_hit`（止损）、
        `boll_decision`（决策）⇒ 零口径漂移。

    引擎里的优先级（engine.py 2124~2196）：
        ① ATR 止损：锚 = **信号根收盘价**，宽度 = ATR_STOP_MULT × ATR(信号根)，
           固定线不移动；已收盘根的 high/low 触线 ⇒ 该根收盘发单、下根开盘成交
           ⇒ 触发后本根 `continue`，不再做决策
        ② 时间止损：开仓当根记 0，之后每根 +1，满 TIME_STOP_BARS 即离场
        ③ boll_decision：中轨/超调出场
    起索引 = 引擎的 eval_start 语义（此前只预热、不交易）。
    """
    import pandas as pd
    from lib.engine import (_atr, _dual_ma_stop_line, _dual_ma_stop_hit,
                            boll_decision, _boll)

    收, 高, 低 = pd.Series(close), pd.Series(high), pd.Series(low)
    中, 上, 下 = _boll(收, 完整参数["BOLL_N"], 完整参数["BOLL_K"])
    ATR = _atr(高, 低, 收, 完整参数["ATR_PERIOD"])
    止损倍 = float(完整参数["ATR_STOP_MULT"])
    时间止损 = int(完整参数.get("TIME_STOP_BARS") or 0)

    n = len(收)
    区间 = []
    want, 止损活, 基准, 锁ATR, 持根, 起点 = 0, False, 0.0, 0.0, 0, None

    def _成交时刻(i):
        return 时刻[i + 1] if i + 1 < n else 时刻[n - 1]

    for i in range(起索引, n):
        m, u, l = 中.iloc[i], 上.iloc[i], 下.iloc[i]
        if m != m:                       # 预热不足，布林带尚未算出
            continue
        c = float(收.iloc[i])
        _a = float(ATR.iloc[i])
        atr_i = _a if _a == _a else 0.0

        # ① ATR 止损（优先级最高）
        if 止损倍 > 0 and 止损活 and want != 0:
            sd = 1 if want > 0 else -1
            线 = _dual_ma_stop_line(基准, 锁ATR, sd, 止损倍)
            if _dual_ma_stop_hit(线, sd, float(高.iloc[i]), float(低.iloc[i])):
                want, 止损活 = 0, False
                区间.append((起点, _成交时刻(i)))
                起点 = None
                continue

        # ② 时间止损
        if 时间止损 and want != 0:
            持根 += 1
            if 持根 >= 时间止损:
                want, 止损活 = 0, False
                区间.append((起点, _成交时刻(i)))
                起点 = None
                continue

        # ③ 决策（boll_decision 只看方向，手数传 1）
        目标, _ = boll_decision(want, c, m, u, l, 1)
        if 目标 != want:
            if 目标 == 0:
                止损活 = False
                区间.append((起点, _成交时刻(i)))
                起点 = None
            else:
                if want != 0:                # 反手（布林带不会发生，防御性处理）
                    区间.append((起点, _成交时刻(i)))
                起点 = _成交时刻(i)
                止损活, 基准, 锁ATR, 持根 = True, c, atr_i, 0
            want = 目标

    if want != 0 and 起点 is not None:
        区间.append((起点, 时刻[n - 1]))
    return [r for r in 区间 if r[0] is not None]


def 裁剪(区间表, 段起, 段止):
    """把区间裁到 [段起, 段止] 内。"""
    出 = []
    for a, b in 区间表:
        x, y = max(a, 段起), min(b, 段止)
        if y > x:
            出.append((x, y))
    return 出


# ============================================================
# 扫描线：时间加权的腿数分布
# ============================================================
def 扫描线(区间表, 段起, 段止, 定位=None):
    """[(起,止), ...] → ({腿数: 时长}, 最大腿数)。

    做法：把每个区间的起止当事件，按时间排序后扫描，
          两个相邻事件之间腿数不变，故该段时长直接累加到对应腿数上。
    同一时刻的多事件**合并成净变化**处理（一开一平 ⇒ 腿数不变，符合现实）。

    `定位`：时刻 → 整数（统一时间网格的位置）。
        ⚠️ 不传时退回**日历秒** —— 那会把休市期也算进去（一年约 1/3 是休市日），
           而闸门只在交易时段才可能被触发。所以正式调用**必须传 `定位`**，
           用「交易时段格数」计时；自测不传（合成 Timestamp 无需网格）。
    """
    def 跨(a, b):
        if 定位 is not None:
            return float(定位(b) - 定位(a))
        d = b - a
        return d.total_seconds() if hasattr(d, "total_seconds") else float(d)

    事件 = []
    for a, b in 区间表:
        if b > a:
            事件.append((a, +1))
            事件.append((b, -1))
    if not 事件:
        return {0: 跨(段起, 段止)}, 0

    事件.sort(key=lambda x: (x[0], -x[1]))
    分布 = defaultdict(float)
    腿数, 最大, 上一, i = 0, 0, 段起, 0
    while i < len(事件):
        t = 事件[i][0]
        if t >= 段止:
            break
        if t > 上一:
            分布[腿数] += 跨(上一, t)
            上一 = t
        净 = 0
        while i < len(事件) and 事件[i][0] == t:
            净 += 事件[i][1]
            i += 1
        腿数 += 净
        最大 = max(最大, 腿数)
    if 段止 > 上一:
        分布[腿数] += 跨(上一, 段止)
    return dict(分布), 最大


def 模拟闸门(带腿名区间, K, 段起, 段止):
    """按开仓时刻**先到先得**模拟 K 闸门。

    返回 (接受的 [(腿,起,止)], 拒绝的 [(腿,起,止)])。
    语义与 组合/_说明.md 的 K 规格一致：n >= K 则拒绝**新开仓**，已有仓位不动。
    """
    全部 = []
    for 腿, a, b in 带腿名区间:
        x, y = max(a, 段起), min(b, 段止)
        if y > x:
            全部.append((腿, x, y))
    全部.sort(key=lambda r: r[1])

    在场, 接受, 拒绝 = [], [], []
    for 腿, a, b in 全部:
        在场 = [z for z in 在场 if z > a]
        if len(在场) < K:
            在场.append(b)
            接受.append((腿, a, b))
        else:
            拒绝.append((腿, a, b))
    return 接受, 拒绝


# ============================================================
# 离线自测（不联网）
# ============================================================
def 自测(说):
    """用合成区间验证扫描线 + 闸门模拟：答案手算已知。"""
    import pandas as pd

    说("=" * 84)
    说(" 离线自测：扫描线腿数分布 + 闸门模拟（合成数据，答案手算已知）")
    说("=" * 84)
    T0 = pd.Timestamp("2024-01-01")
    天 = lambda n: T0 + pd.Timedelta(days=n)

    失败 = []

    def 校验(名, 得到, 期望):
        ok = 得到 == 期望
        说(f"  {'[OK]' if ok else '[!!]'} {名}：{得到}" + ("" if ok else f"  期望 {期望}"))
        if not ok:
            失败.append(名)

    # --- 用例 1：单区间，腿数 0 → 1 → 0 ---
    # 逐段推演：[0,1) 0腿 ｜ [1,3) 1腿 ｜ [3,4) 0腿
    # ⇒ 0腿 = 1+1 = **2** 天（不是 3 天 —— 这是本工具第一版自测写错的地方）
    d, m = 扫描线([(天(1), 天(3))], 天(0), 天(4))
    校验("用例1 分布(天)", {k: round(v / 86400, 3) for k, v in d.items()},
         {0: 2.0, 1: 2.0})
    校验("用例1 最大腿数", m, 1)

    # --- 用例 2：完全重叠的三条腿 ---
    # [0,1) 0腿 ｜ [1,3) 3腿 ｜ [3,4) 0腿 ⇒ 0腿 = 2 天
    d, m = 扫描线([(天(1), 天(3))] * 3, 天(0), 天(4))
    校验("用例2 分布(天)", {k: round(v / 86400, 3) for k, v in d.items()},
         {0: 2.0, 3: 2.0})
    校验("用例2 最大腿数", m, 3)

    # --- 用例 3：完全不重叠（错开） ---
    d, m = 扫描线([(天(0), 天(1)), (天(1), 天(2)), (天(2), 天(3))], 天(0), 天(3))
    校验("用例3 分布(天)", {k: round(v / 86400, 3) for k, v in d.items()}, {1: 3.0})
    校验("用例3 最大腿数", m, 1)

    # --- 用例 4：同一时刻一平一开 ⇒ 腿数应保持不变 ---
    d, m = 扫描线([(天(0), 天(2)), (天(2), 天(4))], 天(0), 天(4))
    校验("用例4 分布(天)", {k: round(v / 86400, 3) for k, v in d.items()}, {1: 4.0})
    校验("用例4 最大腿数", m, 1)

    # --- 用例 5：部分重叠 ---
    d, m = 扫描线([(天(0), 天(3)), (天(1), 天(4))], 天(0), 天(4))
    校验("用例5 分布(天)", {k: round(v / 86400, 3) for k, v in d.items()},
         {1: 2.0, 2: 2.0})
    校验("用例5 最大腿数", m, 2)

    # --- 用例 6：空输入 ---
    d, m = 扫描线([], 天(0), 天(4))
    校验("用例6 空输入", (round(d.get(0, 0) / 86400, 3), m), (4.0, 0))

    # --- 用例 7：闸门 K=2，第 3 条腿开仓时已有 2 条在场 ⇒ 应被拒 ---
    区间 = [("A", 天(0), 天(3)), ("B", 天(0), 天(3)), ("C", 天(1), 天(2))]
    接受, 拒绝 = 模拟闸门(区间, 2, 天(0), 天(4))
    校验("用例7 接受腿数", sorted(r[0] for r in 接受), ["A", "B"])
    校验("用例7 拒绝腿数", [r[0] for r in 拒绝], ["C"])

    # --- 用例 8：闸门 K=2，但 C 在 A 平仓后才开 ⇒ 不应被拒 ---
    区间 = [("A", 天(0), 天(2)), ("B", 天(0), 天(2)), ("C", 天(2), 天(3))]
    接受, 拒绝 = 模拟闸门(区间, 2, 天(0), 天(4))
    校验("用例8 拒绝腿数", [r[0] for r in 拒绝], [])

    # --- 用例 9：闸门后分布（K=1，两条重叠腿只留一条） ---
    区间 = [("A", 天(0), 天(3)), ("B", 天(1), 天(4))]
    接受, 拒绝 = 模拟闸门(区间, 1, 天(0), 天(4))
    d, m = 扫描线([(a, b) for _, a, b in 接受], 天(0), 天(4))
    校验("用例9 闸门后最大腿数", m, 1)
    校验("用例9 拒绝腿数", len(拒绝), 1)

    说()
    if 失败:
        说(f" ⇒ 自测未通过：{len(失败)} 项 {失败}")
        return 1
    说(" ⇒ 自测全部通过（9 个用例）")
    return 0


# ============================================================
# 主流程
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", default="", help="逗号分隔品种 key，默认 = 组合/ 扫到的全部")
    ap.add_argument("--段", default="", help="IS / VAL / OOS，默认全部可用区间")
    ap.add_argument("--K", type=int, default=默认K, help=f"组合层腿数上限（默认 {默认K}）")
    ap.add_argument("--根数", type=int, default=默认根数, help=f"拉多少根（默认 {默认根数}）")
    ap.add_argument("--no-net", action="store_true", help="用本地缓存，不联网")
    ap.add_argument("--刷新", action="store_true", help="忽略缓存，强制重拉")
    ap.add_argument("--口径", choices=["引擎", "信号"], default="引擎",
                    help="引擎=含 ATR 止损 + 时间止损（默认，准）；"
                         "信号=只跑 boll_decision（快，但是**上界**）")
    ap.add_argument("--自测", action="store_true", help="离线自测扫描线算法后退出")
    args = ap.parse_args()

    os.makedirs(输出目录, exist_ok=True)
    报告路径 = os.path.join(输出目录, f"_腿数体检_{args.段 or '全部'}.txt")
    说 = 双写(报告路径)

    if args.自测:
        码 = 自测(说)
        说.close()
        return 码

    import numpy as np
    import pandas as pd

    # ---- 组合清单 ----
    组合表 = 扫组合()
    if args.keys:
        想要 = {k.strip().upper() for k in args.keys.split(",") if k.strip()}
        组合表 = [r for r in 组合表 if r[1] in 想要]
    if not 组合表:
        说("没有可分析的组合（组合/ 目录为空或 --keys 没匹配到）")
        说.close()
        return 2

    说("=" * 96)
    说(" 腿数体检：N 条腿同时在场，真实分布长什么样")
    说("=" * 96)
    说(f" 组合 {len(组合表)} 个：" + "  ".join(f"{s}×{k}" for s, k, _ in 组合表))
    说(f" 腿数上限 K = {args.K}（组合层参数，见 组合/_说明.md）")
    说(f" 区间口径 = {args.口径}"
          + ("（含 ATR 止损 + 时间止损，与 engine.run_backtest_boll 逐行对齐）"
             if args.口径 == "引擎" else
             "（只跑 boll_decision，**不含止损** ⇒ 持仓偏长，腿数是上界）"))
    说()
    说(" 怎么读这份报告：")
    说("   · 【一】【二】【三】回答「9 条腿全开时，K 触发得频繁吗」")
    说("   · 【四】回答「将来只筛出几条腿时，K 还灵吗」← **更常用**")
    说("   · 【五】结论 + 三个边界（脚本口径 / 样本长度 / 保证金与拒单）")

    # ---- 数据 ----
    缓存 = os.path.join(输出目录, f"_腿数数据_v2_{args.根数}.pkl")
    品种键 = sorted({k for _, k, _ in 组合表})
    数据 = None
    if os.path.isfile(缓存) and not args.刷新:
        说(f"[数据] 用缓存 {os.path.relpath(缓存, ROOT)}")
        try:
            with open(缓存, "rb") as f:
                原始 = pickle.load(f)
            数据 = {k: {"t": pd.to_datetime(v["t"]), "open": v["open"],
                        "high": v["high"], "low": v["low"], "close": v["close"]}
                    for k, v in 原始.items()}
        except Exception as e:
            说(f"  [警告] 缓存读失败（{e}），改为联网")
            数据 = None
    if 数据 is None:
        if args.no_net:
            说(" [错误] 指定了 --no-net 但没有缓存，退出")
            说.close()
            return 2
        说(f"[数据] 联网拉取 {len(品种键)} 个品种 × {args.根数} 根 30min 主连……")
        t0 = time.time()
        数据 = 拉数据(品种键, args.根数, 缓存)
        说(f"  ✓ 用时 {time.time() - t0:.1f}s，拿到 {len(数据)} 个品种")
        try:
            with open(缓存, "wb") as f:
                pickle.dump({k: {"t": v["t"].to_numpy(), "open": v["open"],
                                 "high": v["high"], "low": v["low"],
                                 "close": v["close"]}
                             for k, v in 数据.items()}, f)
        except Exception as e:
            说(f"  [警告] 缓存写失败：{e}")

    缺 = [k for k in 品种键 if k not in 数据]
    if 缺:
        说(f" [警告] 缺数据：{缺}（这几个品种不参与统计）")
    可用 = [k for k in 品种键 if k in 数据]
    if not 可用:
        说(" [错误] 没有任何可用数据")
        说.close()
        return 2

    # ---- 时间范围 ----
    # ⚠️ 各品种能拿到的历史长度不同（K 线密度不一样）⇒ 必须取**交集**。
    #    若取并集，早期只有部分品种有数据，那些品种被当成「没信号」，
    #    腿数会被系统性低估。
    说(" 各腿数据范围：")
    for k in 可用:
        说(f"   {k:<5}{数据[k]['t'][0]:%Y-%m-%d} ~ {数据[k]['t'][-1]:%Y-%m-%d}"
             f"（{len(数据[k]['t'])} 根）")
    最早 = max(数据[k]["t"][0] for k in 可用)
    最晚 = min(数据[k]["t"][-1] for k in 可用)
    说(f" ⇒ 取交集：{最早:%Y-%m-%d} ~ {最晚:%Y-%m-%d}")
    if args.段:
        if args.段 not in 段表:
            说(f" [错误] --段 只能是 {list(段表)} 之一")
            说.close()
            return 2
        a, b = 段表[args.段]
        段起 = max(pd.Timestamp(a), 最早)
        段止 = min(pd.Timestamp(b) + pd.Timedelta(days=1), 最晚)
        说明 = f"{args.段} 段（{a} ~ {b}）"
        if 段起 > pd.Timestamp(a):
            说明 += f"  ⚠️ 数据只到 {最早:%Y-%m-%d}，实际从 {段起:%Y-%m-%d} 起"
    else:
        段起, 段止 = 最早, 最晚
        说明 = "全部可用区间（未分段）"
    总秒 = (段止 - 段起).total_seconds()
    总天 = 总秒 / 86400
    说(f" 样本：{段起:%Y-%m-%d} ~ {段止:%Y-%m-%d}（{总天:,.0f} 天）")
    说(f" 口径：{说明}")
    说(f" ⚠️ 免费接口只能拿最近 {args.根数} 根，且各品种能回溯的长度不同 ⇒")
    说(f"    样本起止取**所有腿的交集**（由最短的那条腿决定），否则早期腿数会被低估。")

    # ---- 统一时间网格 ----
    # ⚠️ 为什么不用日历时间：一年约 1/3 的日子休市，长假 K 线停更 8~10 天（春节/国庆），
    #    而腿还持着 ⇒ 日历计时会把「腿数不变的一大段」算进分布，稀释占比。
    #    闸门只在交易时段才可能被触发 ⇒ 改用「交易时段格数」计时。
    网格 = np.unique(np.concatenate([数据[k]["t"].to_numpy() for k in 可用]))
    # 相邻时间戳的真实间隔（分钟），> 40 分视为休市 ⇒ 计 0。
    # ⇒ 定位() 返回「自网格起点累计的**交易时段分钟数**」，
    #    两个时刻相减 = 它们之间真正落在交易时段里的时间。
    #    （只数「几个时间戳」是不够的：不同品种时刻错开，格数与时长不成比例。）
    _间 = np.diff(网格.astype("datetime64[m]").astype(np.int64)).astype(float)
    _间 = np.where(_间 <= 40.0, _间, 0.0)
    累积 = np.concatenate([[0.0], np.cumsum(_间)])

    def 定位(ts):
        return float(累积[np.searchsorted(网格, np.datetime64(ts))])

    总格 = max(定位(段止) - 定位(段起), 1.0)
    说(f" 时间网格：{len(网格):,} 个时间戳（所有腿 K 线时间戳的并集）")
    说(f" 计入时长：{总格 / 60:,.0f} 交易时段小时"
       f"（相邻时间戳间隔 > 40 分的按休市计 0 —— 长假不会虚增时长）")
    说()

    # ---- 逐腿算区间 ----
    每腿 = []           # [(标签, 策略名, key, 区间表, 完整参数)]
    说("=" * 96)
    说(" 【一】单腿口径：每条腿多久持仓一次、在场时间占比")
    说("=" * 96)
    说(f" {'腿':<22}{'区间数':>7}{'持仓(时)':>10}{'在场占比':>10}{'平均一笔':>11}{'最长一笔':>11}")
    说("-" * 96)
    for 策略名, key, fn in 组合表:
        if key not in 数据:
            continue
        try:
            策略模块 = 读策略(策略名)
        except Exception as e:
            说(f" [跳过] 策略池/{策略名}.py 载入失败：{e}")
            continue
        组合参, 周期 = 读组合参数(fn, 策略名, key)
        完整 = dict(getattr(策略模块, "参数", {}) or {})
        完整.update(组合参)                      # 组合层覆盖策略池默认
        if 周期 != 1800:
            说(f" [警告] {fn} 周期 = {周期}s（本工具只拉 30min，跳过）")
            continue
        t = 数据[key]["t"]
        if args.口径 == "引擎":
            起索引 = int((t < 段起).sum())      # 引擎 eval_start 语义：之前只预热不交易
            全区间 = 算区间_引擎口径(t, 数据[key]["high"], 数据[key]["low"],
                                      数据[key]["close"], 完整, 起索引)
        else:
            全区间 = 算区间(t, 数据[key]["close"], 策略模块.算信号, 完整)
        区间 = 裁剪(全区间, 段起, 段止)
        长 = [定位(b) - 定位(a) for a, b in 区间]        # 交易时段分钟
        格 = sum(长)
        标签 = f"{策略名}×{读品种(key)['中文名'] if 读品种(key) else key}"
        每腿.append((标签, 策略名, key, 全区间, 完整))
        说(f" {标签:<22}{len(区间):>7}{格 / 60:>10,.0f}"
              f"{格 / 总格 * 100:>9.1f}%"
              f"{(格 / len(长) / 60 if 长 else 0):>10.2f}"
              f"{(max(长) / 60 if 长 else 0):>10.2f}")
    说("-" * 96)
    _合 = sum(sum(定位(b) - 定位(a) for a, b in 裁剪(r[3], 段起, 段止)) for r in 每腿)
    说(f" {'合计':<22}{sum(len(r[3]) for r in 每腿):>7}{_合 / 60:>10,.0f}"
          f"{_合 / 总格 * 100:>9.1f}%")
    说(" （单位 = 交易时段小时，休市不计；「平均/最长一笔」= 单次持仓的时段小时）")
    说()

    # ---- 无闸门分布 ----
    全部区间 = [(a, b) for _, _, _, 区间, _ in 每腿 for a, b in 裁剪(区间, 段起, 段止)]
    分布, 最大 = 扫描线(全部区间, 段起, 段止, 定位)
    总 = sum(分布.values()) or 1.0
    平均 = sum(k * v for k, v in 分布.items()) / 总

    说("=" * 96)
    说(" 【二】腿数分布（无闸门）—— 自然状态下有几条腿在场")
    说("=" * 96)
    说(f" {'在场腿数':>10}{'时段(时)':>12}{'占比':>10}   累计")
    说("-" * 96)
    累 = 0.0
    for n in range(0, max(分布) + 1):
        v = 分布.get(n, 0.0)
        if v <= 0:
            continue
        累 += v / 总
        说(f" {n:>10}{v / 60:>12,.0f}{v / 总 * 100:>9.1f}%   {累 * 100:>6.1f}%")
    说("-" * 96)
    说(f" ⇒ 平均同时在场 {平均:.2f} 腿 ｜ 历史最大 {最大} 腿")
    说(f" ⇒ ≥{args.K} 腿的时间占比："
          f"{sum(v for n, v in 分布.items() if n >= args.K) / 总 * 100:.2f}%")
    说()

    # ---- K 闸门模拟 ----
    带腿名 = [(标签, a, b) for 标签, _, _, 区间, _ in 每腿 for a, b in 区间]
    接受, 拒绝 = 模拟闸门(带腿名, args.K, 段起, 段止)
    分布2, 最大2 = 扫描线([(a, b) for _, a, b in 接受], 段起, 段止, 定位)
    总2 = sum(分布2.values()) or 1.0
    平均2 = sum(k * v for k, v in 分布2.items()) / 总2

    说("=" * 96)
    说(f" 【三】K = {args.K} 闸门模拟（按开仓时刻先到先得，先到的不动）")
    说("=" * 96)
    说(f" 开仓信号共 {len(带腿名)} 次 ｜ 接受 {len(接受)} ｜ 拒绝 {len(拒绝)}"
          f"（拒绝率 {len(拒绝) / max(len(带腿名), 1) * 100:.1f}%）")
    说()
    说(f" {'在场腿数':>10}{'无闸门占比':>13}{'K闸门后占比':>14}   变化")
    说("-" * 96)
    for n in range(0, max(max(分布), max(分布2)) + 1):
        a_, b_ = 分布.get(n, 0.0) / 总 * 100, 分布2.get(n, 0.0) / 总2 * 100
        if a_ <= 0 and b_ <= 0:
            continue
        说(f" {n:>10}{a_:>12.1f}%{b_:>13.1f}%   {b_ - a_:+.1f}pp")
    说("-" * 96)
    说(f" ⇒ 平均 {平均:.2f} 腿 → {平均2:.2f} 腿 ｜ 最大 {最大} 腿 → {最大2} 腿")
    说()

    # 被拒的腿分布
    if 拒绝:
        计数 = defaultdict(int)
        for 腿, _, _ in 拒绝:
            计数[腿] += 1
        说(" 被拒次数最多的 5 条腿：")
        for 腿, c in sorted(计数.items(), key=lambda x: -x[1])[:5]:
            说(f"   {腿:<22}{c:>4} 次")
        说()

    # ---- 【五】敏感性：开几条腿时 K 才起作用 ----
    # 「几条腿值得开」由模块 3 的判决决定。这里穷举全部子集，
    # 回答「将来筛出 k 条正期望腿时，K 还有没有用」。
    from itertools import combinations
    说("=" * 96)
    说(f" 【四】敏感性：只开 k 条腿时，K = {args.K} 还有用吗")
    说("=" * 96)
    说(f" 穷举全部 2^{len(每腿)}−1 个子集，每个子集都重算一遍腿数分布，再对子集取平均。")
    说(f" ⚠️ 「几条腿值得开」由模块 3 的判决决定（**当前 = 0 条**）。")
    说()
    说(f" {'开的腿数':>9}{'子集数':>8}{'平均在场':>10}{'最大腿数':>10}{'≥K 占比':>10}{'K 会触发吗':>13}")
    说("-" * 96)
    for k in range(1, len(每腿) + 1):
        子集们 = list(combinations(range(len(每腿)), k))
        均腿, 均最大, 均触发 = [], [], []
        for 子 in 子集们:
            子区间 = [(a, b) for i in 子 for a, b in 裁剪(每腿[i][3], 段起, 段止)]
            d, mx = 扫描线(子区间, 段起, 段止, 定位)
            t = sum(d.values()) or 1.0
            均腿.append(sum(nn * v for nn, v in d.items()) / t)
            均最大.append(mx)
            均触发.append(sum(v for nn, v in d.items() if nn >= args.K) / t)
        a1 = sum(均腿) / len(均腿)
        a2 = sum(均最大) / len(均最大)
        a3 = sum(均触发) / len(均触发) * 100
        会 = "✗ 摆设" if a2 < args.K else ("△ 偶尔" if a3 < 5 else "✓ 经常")
        说(f" {k:>9}{len(子集们):>8}{a1:>10.2f}{a2:>10.2f}{a3:>9.2f}%{会:>13}")
    说("-" * 96)
    说(f" ⇒ 读法：只开 k 条腿时，「最大腿数」到不了 {args.K} ⇒ K 永远不触发，就是摆设。")
    说(f"    平均在场腿数 ≈ k × 单腿在场占比（{len(每腿)} 条全开时实测 "
          f"{平均:.2f}/{len(每腿)} = {平均 / len(每腿):.1%}）—— 腿数不够，闸门自然闲置。")
    说()

    # ---- 结论 ----
    说("=" * 96)
    说(" 【五】结论")
    说("=" * 96)
    触发 = sum(v for n, v in 分布.items() if n >= args.K) / 总 * 100
    if 最大 < args.K:
        说(f" ⚠️ 无闸门时历史最大只有 {最大} 腿 < K = {args.K}")
        说(f"    ⇒ **K 闸门是摆设**：它永远不会被触发，写了也不起作用。")
        说(f"    ⇒ 「用腿数封顶」的前提在这个腿数下不成立 —— 真正需要封顶的场景")
        说(f"      （全市场共振）在本段样本里没有出现。")
    elif 最大2 <= args.K:
        说(f" ⇒ 无闸门最大 {最大} 腿 → K={args.K} 后最大 **{最大2}** 腿：**上限被真正压住**。")
        说(f"   ≥{args.K} 腿的时间占比 {触发:.2f}%（无闸门）｜"
              f"开仓被拒 {len(拒绝)}/{len(带腿名)} = {len(拒绝) / max(len(带腿名), 1) * 100:.1f}%")
        说(f"   平均腿数 {平均:.2f} → {平均2:.2f}。")
        说(f"   ⇒ 闸门**有效**：削掉了尾部，但只在 {触发:.1f}% 的时间里起作用 ——")
        说(f"     对不到 {args.K} 腿的日常状态零影响（这正是「用腿数封顶」的设计意图）。")
        说(f"   ⇒ ⚠️ 但别忘了【四】：把腿数从 {len(每腿)} 减到 4 条，K 就基本碰不到了。")
    else:
        说(f" ⇒ 无闸门最大 {最大} 腿 → K={args.K} 后 {最大2} 腿（**未压到** {args.K}）")
        说(f"   ⚠️ 反常：K 生效后最大值仍超过 K ⇒ 有腿「被拒后立刻又占了位置」，需人工核查。")
    说()
    说(f" ⚠️ 三个必须记住的边界：")
    说(f"   ① 这是【成交层】腿数（含 ATR/时间止损），但仍假设「有信号就成交」。")
    说(f"      真实还要过：保证金够不够（组合风险.py【四】）、交易所最小开仓手数、")
    说(f"      静默拒单 —— 这三者只会**再降**腿数（拒单 = 这条腿根本没进过场）。")
    说(f"   ② 样本只有 {总天:,.0f} 日历天（{总格 / 60 / 5.5:,.0f} 个交易日当量），"
          f"本段**没经历过全市场共振**。")
    说(f"      「没观测到 9 腿同时在场」≠「不会发生」（与组合风险.py 的尾部警告同源）。")
    说(f"   ③ 腿数分布只取决于**信号频率与持仓时长**，与策略是否盈利无关 ——")
    说(f"      所以这份分布**不因策略被淘汰而失效**，也不需要消耗样本。")
    说()
    说(f" 报告：{报告路径}")
    说.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
