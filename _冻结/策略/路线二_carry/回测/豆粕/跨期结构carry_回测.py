# -*- coding: utf-8 -*-
"""
路线二(carry) · 跨期结构 carry 滚动版  (豆粕 M)
===========================================================
逻辑（赚期限结构的钱，与方向无关）：
  - 近月 = 主连 quote.underlying_symbol（回测中实时变化 = 当时真实主力）
  - 远月 = 活跃月序列 [1,5,9] 中主力的下一个月份
  - spread = 近月收 - 远月收；backwardation(>0) 多价差 / contango(<0) 空价差
  - 结构翻转才换向；主力换月时：旧价差结清；新对重新锚定（台阶不计盈亏——展期收益已内嵌于日常价差收敛）
  - 本地模拟 P&L（tqsdk 只取 K 线，账户本地算）
  - 禁未来函数：信号只用已收盘日线；窗口 2021-01-01 ~ 2025-12-31（五年全样本，不挑窗）

tqsdk 两个实测坑（已处理，勿改回）：
  1) 回测结束时 wait_update() 抛 BacktestFinished 异常（不是返回 False），
     不捕获则进程静默 exit(0)，后续指标计算/写盘全部不执行。
  2) kline serial 是 DataFrame：kn[-1] 会当列名 KeyError，必须 kn.iloc[-1]。
  3) 具体合约代码必须带交易所前缀（SHFE.rb2405），否则报 non-existent。
"""
import os
import sys
import json
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import config_account
import config_symbols
from tqsdk import TqApi, TqBacktest, TqSim, TqAuth
try:
    from tqsdk import BacktestFinished
except ImportError:
    from tqsdk.backtest import BacktestFinished
from datetime import date

# ==================== 品种参数 ====================
SYMBOL_KEY = "M"                          # 棕榈油
NAME_CN = "豆粕"
CONT = config_symbols.get(SYMBOL_KEY)[1]  # KQ.m@DCE.p
_, _, MULT, TICK = config_symbols.get(SYMBOL_KEY)
ACT_MONTHS = [1, 5, 9]                    # 活跃月序列（按品种交割习惯）
LOTS = 1                                  # 演示 1 手价差
INIT = 200000.0
START_DT = date(2021, 1, 1)
END_DT = date(2025, 12, 31)
OUT_JSON = os.path.join(ROOT, "工具", f"carry_roll_{SYMBOL_KEY}.json")
# =================================================


def next_active(sym_near):
    """由近月合约代码推同品种下一个活跃月合约（含年份进位）。"""
    exch = sym_near.split(".")[0]          # DCE / SHFE / CZCE
    code = sym_near.split(".")[1]          # p2405
    prod = code[:-4]                       # p
    yy, mm = int(code[-4:-2]), int(code[-2:])
    cand = [(m, yy + (1 if m <= mm else 0)) for m in ACT_MONTHS if m > mm]
    if not cand:
        cand = [(ACT_MONTHS[0], yy + 1)]
    m2, y2 = min(cand, key=lambda t: (t[1], t[0]))   # 最近的下一个活跃月
    if exch == "CZCE":
        raise SystemExit("CZCE 3位码 10年一循环，禁构造老代码（项目铁律）")
    return f"{exch}.{prod}{y2 % 100:02d}{m2:02d}"


def run_backtest():
    user, pwd = config_account.check()
    api = TqApi(TqSim(INIT), backtest=TqBacktest(start_dt=START_DT, end_dt=END_DT),
                auth=TqAuth(user, pwd))
    q_cont = api.get_quote(CONT)                  # 主连 quote：underlying_symbol 实时主力
    km = api.get_kline_serial(CONT, 86400)        # 主连日线 = 时钟
    api.wait_update()

    near = q_cont.underlying_symbol
    far = next_active(near)
    kn = api.get_kline_serial(near, 86400)
    kf = api.get_kline_serial(far, 86400)

    equity = INIT
    pos = 0
    prev_spread = None
    last_day = None
    eq_curve = [INIT]
    days = flips = rolls = backw = 0
    pending_roll = None      # (new_near, new_far) 待换月

    try:
        while True:
            if not api.wait_update():
                break
            if len(km) == 0:
                continue
            kmb = km.iloc[-1]
            day = pd.Timestamp(kmb["datetime"]).date() if kmb["datetime"] else None
            if day is None or day == last_day:
                continue
            # 仅当日线收出新一根时处理（日线时钟）
            if kmb["id"] < 0:
                continue
            last_day = day

            # ---- 换月检测：主连 underlying 变了 ----
            u = q_cont.underlying_symbol
            if u != near:
                # 旧价差最后一天结清
                if prev_spread is not None and pos != 0:
                    nc_o = kn.iloc[-1]["close"]; fc_o = kf.iloc[-1]["close"]
                    if nc_o and fc_o:
                        spread_o = nc_o - fc_o
                        equity += pos * (spread_o - prev_spread) * MULT * LOTS
                        old_spread_last = spread_o
                    else:
                        old_spread_last = prev_spread
                else:
                    old_spread_last = prev_spread
                near, far = u, next_active(u)
                kn = api.get_kline_serial(near, 86400)
                kf = api.get_kline_serial(far, 86400)
                # 等新 K 线出数
                for _ in range(50):
                    api.wait_update()
                    if len(kn) > 0 and len(kf) > 0:
                        break
                rolls += 1
                if len(kn) > 0 and len(kf) > 0:
                    nc_n, fc_n = kn.iloc[-1]["close"], kf.iloc[-1]["close"]
                    if nc_n and fc_n:
                        spread_n = nc_n - fc_n
                        prev_spread = spread_n
                        new_pos = 1 if spread_n > 0 else -1
                        if pos == 0:
                            pos = new_pos
                        elif new_pos != pos:
                            pos = new_pos
                            flips += 1
                continue

            if len(kn) == 0 or len(kf) == 0:
                continue
            nc, fc = kn.iloc[-1]["close"], kf.iloc[-1]["close"]
            if not nc or not fc:
                continue

            spread = nc - fc
            if prev_spread is None:
                pos = 1 if spread > 0 else -1
                prev_spread = spread
                continue

            equity += pos * (spread - prev_spread) * MULT * LOTS
            days += 1
            if spread > 0:
                backw += 1
            new_pos = 1 if spread > 0 else -1
            if new_pos != pos:
                pos = new_pos
                flips += 1
            prev_spread = spread
            eq_curve.append(equity)
    except BacktestFinished:
        pass
    api.close()

    peak, max_dd = INIT, 0.0
    for e in eq_curve:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak)
    import statistics
    sharpe = 0.0
    rets = [(eq_curve[i] - eq_curve[i-1]) / eq_curve[i-1]
            for i in range(1, len(eq_curve)) if eq_curve[i-1] > 0]
    if len(rets) > 1 and statistics.pstdev(rets) > 0:
        sharpe = statistics.mean(rets) / statistics.pstdev(rets) * (252 ** 0.5)

    metrics = {
        "品种": f"{NAME_CN}({SYMBOL_KEY}) {near}/{far}",
        "窗口": f"{START_DT}~{END_DT}",
        "初始权益": INIT, "期末权益": round(equity, 2),
        "收益率%": round((equity - INIT) / INIT * 100, 2),
        "最大回撤%": round(max_dd * 100, 2),
        "年化夏普(近似)": round(sharpe, 2),
        "交易日数": days, "backwardation天数": backw,
        "换月次数": rolls, "结构翻转次数": flips,
        "最终持仓": "多价差" if pos > 0 else "空价差",
    }
    return metrics


if __name__ == "__main__":
    m = run_backtest()
    print("==== 路线二 carry 滚动版 结果 ====")
    for k, v in m.items():
        print(f"  {k}: {v}")
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    print("指标已落盘:", OUT_JSON)
