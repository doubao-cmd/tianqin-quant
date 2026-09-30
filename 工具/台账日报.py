# -*- coding: utf-8 -*-
"""
台账日报工具 —— 读模拟盘台账 CSV，自动出结论（人不翻流水）
==========================================================
输入：策略/06_MA13超短线/模拟盘/台账/ 下的
  <品种>_成交流水.csv   类型,记录时间,K线时间,方向,手数,成交价,参考价,备注
  <品种>_运行心跳.csv   记录时间,K线时间,现价,持仓手数,目标手数

输出（控制台 + 台账/日报_*.txt）：
  1. 事件统计：SIGNAL / FILL / STOPLOSS / RECONNECT 计数，日均信号数
  2. 隐含滑点（观察模式粗估）：每个 SIGNAL 之后第一条「现价>0」的心跳价
     当作"假如成交"的价，与信号根收盘价之差 × 方向 = 隐含滑点（跳）
     —— 方法是粗估（心跳间隔 5 分钟内首个报价），结论看分布不看单笔
  3. 心跳完整率与断层：实际行数 / 应有行数；断层（>2×心跳间隔）次数、
     最长断层、断层前后 K 线时间是否推进（推进=期间可能漏信号）
  4. 对照回测画像：日均信号数 vs 开发窗回测预期

用法：
  python 工具/台账日报.py                    # 默认台账目录
  python 工具/台账日报.py --hb 300           # 指定心跳间隔(秒)
"""
import sys, os, csv, glob, argparse
from datetime import datetime

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config_symbols

LEDGER_DIR = os.path.join(_ROOT, "策略", "06_MA13超短线", "模拟盘", "台账")
# 回测画像（dev 窗 2021-2023，来自 2345 验证与深挖报告，粗口径）
BENCH_SIGNAL_PER_DAY = {"棕榈油": 4.0, "苯乙烯": 4.0}
# 观察模式隐含滑点红线（跳）——任务A 判定：P 实测若长期 >2.5 跳则淘汰
SLIP_REDLINE = 2.5


def _name_key():
    """品种中文名 -> (key, tick, mult)，用于滑点换算成跳。"""
    d = {}
    for key in config_symbols.SYMBOLS:
        name, cont, mult, tick = config_symbols.get(key)
        d[name] = (key, float(tick), float(mult))
    return d


def _read_csv(path):
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def _ts(s):
    """记录时间/K线时间 -> 时间戳（解析失败返回 None）。"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s.strip(), fmt).timestamp()
        except Exception:
            continue
    return None


def report_symbol(name, nk, hb_sec):
    """单个品种的日报段落。返回 (文本, 是否有数据)。"""
    ev_path = os.path.join(LEDGER_DIR, f"{name}_成交流水.csv")
    hv_path = os.path.join(LEDGER_DIR, f"{name}_运行心跳.csv")
    if not os.path.exists(ev_path) or not os.path.exists(hv_path):
        return None, False
    ev = _read_csv(ev_path)
    hv = _read_csv(hv_path)
    if not ev:
        return None, False

    L = []
    L.append(f"════════ {name} ════════")

    # ---- 1. 事件统计 ----
    cnt = {}
    for r in ev:
        cnt[r["类型"]] = cnt.get(r["类型"], 0) + 1
    days = None
    starts = [_ts(r["记录时间"]) for r in ev if r["类型"] == "START"]
    stops = [_ts(r["记录时间"]) for r in ev if r["类型"] == "STOP"]
    t_all = [_ts(r["记录时间"]) for r in ev if _ts(r["记录时间"])]
    if t_all:
        span_d = (max(t_all) - min(t_all)) / 86400.0
        # 运行天数按"有台账覆盖的日历天数"粗算（>=1 天才给日均）
        days = max(1, int(span_d) + 1) if span_d >= 1 else None
    sig = cnt.get("SIGNAL", 0)
    L.append(f"事件: SIGNAL={sig}  STOPLOSS={cnt.get('STOPLOSS', 0)}  "
             f"FILL={cnt.get('FILL', 0)}  重连={cnt.get('RECONNECT', 0)}  "
             f"熔断={cnt.get('CIRCUIT', 0)}  "
             f"启动={cnt.get('START', 0)}  正常退出={cnt.get('STOP', 0)}")
    if cnt.get("CIRCUIT"):
        L.append("🚨 出现回撤熔断记录——进程已被闩死在『只出不进』状态，需人工核查后重启")
    if days and sig:
        daily = sig / days
        bench = BENCH_SIGNAL_PER_DAY.get(name)
        tag = ""
        if bench:
            ratio = daily / bench
            tag = f"（回测预期≈{bench:.0f}笔/天，实际为 {ratio:.0%}）"
        L.append(f"日均信号: {daily:.1f} 笔/天 {tag}")
    # 启动次数 vs 正常退出：差值=有被强杀/断电等非正常结束
    if cnt.get("START", 0) - cnt.get("STOP", 0) > 1:
        L.append(f"⚠️ 有 {cnt['START'] - cnt.get('STOP', 0) - 1} 次启动没有对应正常退出（强杀/断电/崩溃）")

    # ---- 2. 隐含滑点（观察模式粗估） ----
    _, tick, _mult = nk.get(name, ("", 1.0, 1.0))
    hb_px = [( _ts(r["记录时间"]), float(r["现价"])) for r in hv
             if _ts(r["记录时间"]) and r["现价"] not in ("", None)
             and float(r["现价"] or 0) > 0]
    hb_px.sort()
    slips = []
    for r in ev:
        if r["类型"] != "SIGNAL":
            continue
        t0 = _ts(r["记录时间"])
        try:
            ref = float(r["参考价"])
        except Exception:
            continue
        d = 1 if r["方向"] == "多" else (-1 if r["方向"] == "空" else 0)
        # 信号之后第一条有价心跳 = "假如下一根开盘市价单"的粗估成交价
        nxt = next((p for t, p in hb_px if t > t0), None)
        if nxt and d != 0 and tick > 0:
            slips.append((nxt - ref) * d / tick)
    if slips:
        n = len(slips)
        avg = sum(slips) / n
        srt = sorted(slips)
        med = srt[n // 2]
        p90 = srt[min(n - 1, int(n * 0.9))]
        over = sum(1 for x in slips if abs(x) > SLIP_REDLINE)
        L.append(f"隐含滑点(粗估,{n}笔): 均值{avg:+.1f}跳 中位{med:+.1f}跳 "
                 f"P90{p90:+.1f}跳 | 绝对值>{SLIP_REDLINE}跳占比 {over / n:.0%}")
        if name == "棕榈油" and abs(avg) > SLIP_REDLINE:
            L.append(f"⚠️ 棕榈油隐含滑点均值超过 {SLIP_REDLINE} 跳红线（任务A判定线）→ 到裁决点")
        if abs(avg) < 0.5 and n >= 20:
            L.append("✔ 隐含滑点量级健康（|均值|<0.5 跳）")
    else:
        L.append("隐含滑点: 暂无 SIGNAL 或无有效心跳价，无法估算")

    # ---- 3. 心跳完整率与断层 ----
    ht = [( _ts(r["记录时间"]), r["K线时间"]) for r in hv if _ts(r["记录时间"])]
    ht.sort()
    if ht:
        exp = (ht[-1][0] - ht[0][0]) / hb_sec + 1
        rate = min(1.0, len(ht) / max(1.0, exp))
        gaps = [(ht[i][0] - ht[i - 1][0], ht[i - 1][1], ht[i][1])
                for i in range(1, len(ht))
                if ht[i][0] - ht[i - 1][0] > 2 * hb_sec]
        L.append(f"心跳: {len(ht)}行 / 应有≈{exp:.0f}行 完整率 {rate:.0%} "
                 f"覆盖 {datetime.fromtimestamp(ht[0][0]):%m-%d %H:%M} ~ "
                 f"{datetime.fromtimestamp(ht[-1][0]):%m-%d %H:%M}")
        if gaps:
            mx = max(gaps)
            advanced = sum(1 for _, a, b in gaps if a and b and a != b)
            L.append(f"⚠️ 断层 {len(gaps)} 次，最长 {mx[0] / 60:.0f} 分钟；"
                     f"其中 {advanced} 次断层前后 K 线时间推进（可能漏信号）")
            if rate < 0.95:
                L.append("⚠️ 心跳完整率 <95%：程序有较长时间没在跑，检查守护/开机")
        else:
            L.append("✔ 无断层（程序全程在线）")
    return "\n".join(L), True


def main():
    global LEDGER_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--hb", type=int, default=300, help="心跳间隔秒数（默认300）")
    ap.add_argument("--dir", default=LEDGER_DIR)
    args = ap.parse_args()
    LEDGER_DIR = args.dir

    nk = _name_key()
    names = sorted({os.path.basename(p).split("_成交流水")[0]
                    for p in glob.glob(os.path.join(LEDGER_DIR, "*_成交流水.csv"))})
    if not names:
        print(f"台账目录没有成交流水：{LEDGER_DIR}")
        return

    out = [f"台账日报  {datetime.now():%Y-%m-%d %H:%M}  （心跳间隔按 {args.hb}s 计）"]
    for name in names:
        seg, ok = report_symbol(name, nk, args.hb)
        out.append(seg if ok else f"════════ {name} ════════\n（台账不完整，跳过）")
    text = "\n\n".join(x for x in out if x)
    print(text)

    fp = os.path.join(LEDGER_DIR, f"日报_{datetime.now():%Y-%m-%d_%H%M}.txt")
    with open(fp, "w", encoding="utf-8", newline="") as f:
        f.write(text + "\n")
    print(f"\n已存：{fp}")


if __name__ == "__main__":
    main()
