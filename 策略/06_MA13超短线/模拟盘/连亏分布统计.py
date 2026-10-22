# -*- coding: utf-8 -*-
"""
06 MA13 · 连亏笔数分布统计（#19，D2 2026-10-21 拍板「先出分布、后定阈」）
==========================================================
【为什么先测、不先上闸门】
1.5% 单日亏损阀管"一天亏多少"，管不了"连错几笔"。06 是短持仓策略，一天可能
连错 5 笔、每笔都小，日亏阀不响，但策略可能已经失效。
但"连亏几笔算异常"**不能拍脑袋定**（D2 19.1 明确：先出分布，看完再定值）。
本脚本只做统计，**不改任何代码、不连网、不写策略状态**。

【口径（D2 19.2 / 19.3）】
  · **账户级**连亏 = 所有品种按**平仓时间**合并后的连续亏损笔数 ← 触发口径
  · **per-symbol** 连亏 = 按品种各自算 ← 只作辅助观测（D2 质询：纯账户级会
    稀释单品种策略失效的早期信号）
  · **净亏**口径（含成本）：毛盈亏 − 手数×2×(1跳×乘数 + 佣金)，与既有
    `台账统计.py` 的 `净盈亏估计` 逐字一致（SLIP=1 跳）。不用毛亏 —— 06 换手高，
    成本侵蚀是负期望主因，毛亏看不见成本。

【数据源：FILLX 而不是 FILL】
  `FILL` 是**下单意图**（可能没成交：休市排队、被拒、废单），拿它配对会造出
  根本不存在的交易。`FILLX` 是**核对云端持仓后确认真实成交**的行，
  所以本脚本优先解析 FILLX；老台账没有 FILLX 时才回落到 FILL（并在报告里标注）。

【用法】
    python 策略/06_MA13超短线/模拟盘/连亏分布统计.py
    python 策略/06_MA13超短线/模拟盘/连亏分布统计.py --out 台账/连亏分布.md
"""
import argparse
import datetime
import glob
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd

import config_symbols as cs

LEDGER = os.path.join(_HERE, "台账")
SLIP = 1          # 与 06 回测/实盘/台账统计 同口径（1 跳）

# FILLX 备注样例：
#   "多开 8手 决策价=3277.00 云端成交均价=3277.50 滑点≈+32.00元"
#   "多开 8手 决策价=3277.00 **持仓未变化（云端净仓 0，应为 8）⇒ 判为未成交**"
_RE_FILLX = re.compile(
    r"^(多开|空开|多平|空平)\s+(\d+)手\s+决策价=([\d.]+)\s+云端成交均价=([\d.]+)")


def _params(cn):
    """中文品种名 → (key, MULT, TICK, COMM)。查不到**抛异常**，绝不用默认值。

    （沿用 `台账统计.py` 的教训：静默默认值会让盈亏算错数倍且没人发现。）
    """
    for k, v in cs.SYMBOLS.items():
        if v[0] == cn:
            return k, float(v[2]), float(v[3]), float(cs.COST.get(k, 0.0))
    raise KeyError(f"品种「{cn}」不在 config_symbols.SYMBOLS 里（拒绝用默认乘数）")


def _load_one(path):
    """读一个品种的台账，返回 (trades, src)。

    trades: [{"cn","key","ts","side","hands","price","gross","net","mult"}]
    src   : "FILLX" / "FILL"
    """
    cn = os.path.basename(path).replace("_成交流水.csv", "")
    key, mult, tick, comm = _params(cn)
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
    if "类型" not in df.columns:
        return [], "FILLX"

    # ---- 优先 FILLX（真实成交确认）----
    fx = df[df["类型"] == "FILLX"]
    fills = []
    for _, r in fx.iterrows():
        m = _RE_FILLX.match(str(r.get("备注") or ""))
        if not m:
            continue                     # "持仓未变化/均价未就绪" 一律不算成交
        fills.append((str(r.get("记录时间") or ""), m.group(1),
                      int(m.group(2)), float(m.group(4))))
    src = "FILLX"

    if not fills:
        # ---- 回落 FILL（老台账无 FILLX）----
        src = "FILL"
        fl = df[df["类型"] == "FILL"]
        for _, r in fl.iterrows():
            d = str(r.get("方向") or "")
            if d not in ("多开", "空开", "多平", "空平"):
                continue
            try:
                h = float(r.get("手数"))
                px = float(r.get("成交价"))
            except Exception:
                continue
            fills.append((str(r.get("记录时间") or ""), d, int(h), px))

    trades = []
    pos = None
    for ts, d, h, px in fills:
        if d in ("多开", "空开"):
            pos = (d, h, px, ts)
        elif d in ("多平", "空平") and pos is not None:
            sign = 1 if pos[0] == "多开" else -1
            gross = (px - pos[2]) * sign * h * mult
            cost = h * 2 * (SLIP * tick * mult + comm)
            trades.append({"cn": cn, "key": key, "ts": ts, "side": d,
                           "hands": h, "price": px, "mult": mult,
                           "gross": gross, "net": gross - cost})
            pos = None
    return trades, src


def _streaks(nets):
    """给一串按时间排序的净盈亏，返回连亏长度列表（每个连亏段的长度）。"""
    out, cur = [], 0
    for x in nets:
        if x < 0:
            cur += 1
        else:
            if cur:
                out.append(cur)
            cur = 0
    if cur:
        out.append(cur)
    return out


def _dist(streak_list):
    """连亏段长度 → 出现次数（用于"最长连亏 N 笔发生过几次"）。"""
    d = {}
    for x in streak_list:
        d[x] = d.get(x, 0) + 1
    return dict(sorted(d.items()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default=LEDGER, help="台账目录")
    ap.add_argument("--out", default=None, help="报告输出路径（缺省打印到屏幕）")
    ap.add_argument("--min-trades", type=int, default=5,
                    help="少于该笔数的品种不单独出连亏结论（样本太少）")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.ledger, "*_成交流水.csv")))
    if not files:
        print(f"找不到台账：{args.ledger}")
        return 1

    all_trades, per_sym, srcs = [], {}, {}
    for f in files:
        try:
            tr, src = _load_one(f)
        except Exception as e:                      # noqa: BLE001
            print(f"[跳过] {os.path.basename(f)}：{type(e).__name__}: {e}")
            continue
        if not tr:
            continue
        cn = tr[0]["cn"]
        srcs[cn] = src
        per_sym.setdefault(cn, []).extend(tr)
        all_trades.extend(tr)

    if not all_trades:
        print("台账里没有任何已平仓成交（FILLX/FILL 都为空的配对）")
        return 1

    all_trades.sort(key=lambda x: str(x["ts"]))
    nets_all = [t["net"] for t in all_trades]
    st_all = _streaks(nets_all)

    L = []
    L.append("# 06 MA13 · 连亏笔数分布")
    L.append("")
    L.append(f"> 生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    L.append(f"> 数据目录：`{args.ledger}`")
    L.append(f"> 口径：**净亏**（毛 − 手数×2×(1跳×乘数+佣金)，SLIP={SLIP} 跳）；"
             f"账户级按**平仓时间**合并；只统计已配对平仓的交易")
    L.append(f"> 数据源：{', '.join(f'{k}={v}' for k, v in sorted(srcs.items()))}")
    L.append("")
    L.append("## 一、账户级（触发口径）")
    L.append("")
    L.append(f"- 已平仓笔数：**{len(all_trades)}**")
    win = sum(1 for x in nets_all if x > 0)
    L.append(f"- 胜率：**{win / len(nets_all):.1%}**（{win} 胜 / {len(nets_all) - win} 负）")
    L.append(f"- 净盈亏合计：**{sum(nets_all):+.0f} 元**")
    if st_all:
        L.append(f"- **最长连亏：{max(st_all)} 笔**")
        L.append(f"- 连亏段共 {len(st_all)} 段，总亏损笔数 {sum(st_all)}")
    else:
        L.append("- 无连亏记录")
    L.append("")
    L.append("### 连亏长度分布")
    L.append("")
    L.append("| 连亏笔数 | 出现次数 |")
    L.append("|---:|---:|")
    for k, v in _dist(st_all).items():
        L.append(f"| {k} | {v} |")
    L.append("")

    L.append("## 二、per-symbol（辅助观测，不触发）")
    L.append("")
    L.append("| 品种 | 笔数 | 胜率 | 净盈亏 | 最长连亏 | 连亏分布 |")
    L.append("|---|---:|---:|---:|---:|---|")
    for cn in sorted(per_sym, key=lambda x: -len(per_sym[x])):
        tr = sorted(per_sym[cn], key=lambda x: str(x["ts"]))
        nets = [t["net"] for t in tr]
        st = _streaks(nets)
        w = sum(1 for x in nets if x > 0)
        longest = max(st) if st else 0
        dist = ", ".join(f"{k}笔×{v}" for k, v in _dist(st).items()) or "—"
        flag = "" if len(tr) >= args.min_trades else "（样本少）"
        L.append(f"| {cn}{flag} | {len(tr)} | {w / len(tr):.0%} | "
                 f"{sum(nets):+.0f} | {longest} | {dist} |")
    L.append("")

    L.append("## 三、逐笔明细（账户级，按平仓时间）")
    L.append("")
    L.append("| # | 平仓时间 | 品种 | 方向 | 手数 | 价格 | 净盈亏 | 累计连亏 |")
    L.append("|---:|---|---|---|---:|---:|---:|---:|")
    cur = 0
    for i, t in enumerate(all_trades, 1):
        cur = cur + 1 if t["net"] < 0 else 0
        L.append(f"| {i} | {t['ts']} | {t['cn']} | {t['side']} | {t['hands']} | "
                 f"{t['price']:.1f} | {t['net']:+.0f} | {cur} |")
    L.append("")
    L.append("---")
    L.append("")
    L.append("**给 D 的话（执行员陈述，不含建议阈值）**：本报告只给分布。"
             "「连亏几笔算异常」需要 D 结合上表的长尾（如最长连亏、高频出现的长度）自己定；"
             "执行员不预设 N 笔。样本量 "
             f"{len(all_trades)} 笔，样本越少长尾越不可信 —— 建议标注置信度。")

    txt = "\n".join(L)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(txt)
        print(f"已写入 {args.out}")
        print(f"账户级：{len(all_trades)} 笔，胜率 {win / len(all_trades):.1%}，"
              f"最长连亏 {max(st_all) if st_all else 0} 笔")
    else:
        print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
