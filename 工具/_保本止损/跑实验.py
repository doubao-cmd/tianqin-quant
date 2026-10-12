# -*- coding: utf-8 -*-
"""跑「保本止损」全量实验：5 品种 × 2 组（BREAKEVEN_ATR=0 / 1.0），串行、后台友好。
用法：python 跑实验.py
输出：结果/{品种}_BE{0|1}.json  +  结果/_汇总.txt
"""
import os
import sys
import json
import time
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "保本止损_实验.py")
OUT = os.path.join(HERE, "结果")
os.makedirs(OUT, exist_ok=True)

SYMS = [("JM", "焦煤"), ("M", "豆粕"), ("JD", "鸡蛋"), ("P", "棕榈油"), ("SR", "白糖")]
MODES = [0.0, 1.0]

PY = sys.executable
env0 = dict(os.environ)
env0["PYTHONIOENCODING"] = "utf-8"

t_all = time.time()
for be in MODES:
    for sk, cn in SYMS:
        tag = f"{sk}_BE{be:g}"
        jp = os.path.join(OUT, tag + ".json")
        if os.path.exists(jp) and "--resume" in sys.argv:
            print(f"[skip] {tag} 已存在", flush=True)
            continue
        env = dict(env0)
        env["MA13_SYMBOL"] = sk
        env["MA13_BREAKEVEN"] = str(be)
        env["MA13_START"] = "2021-01-01"
        env["MA13_END"] = "2023-12-31"
        print(f"\n{'='*70}\n[run] {cn}({sk})  BREAKEVEN_ATR={be:g}  {time.strftime('%H:%M:%S')}\n{'='*70}", flush=True)
        t0 = time.time()
        r = subprocess.run([PY, SCRIPT], env=env, cwd=HERE,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        dt = time.time() - t0
        tail = [l for l in (r.stdout or "").splitlines() if "[check]" in l or "[FATAL]" in l or "[done]" in l]
        for l in tail:
            print("   " + l, flush=True)
        if r.returncode != 0:
            print(f"   [ERR] rc={r.returncode}\n   {((r.stderr or '')[-800:])}", flush=True)
        else:
            print(f"   [ok] {dt:.0f}s", flush=True)

print(f"\n[ALL DONE] 用时 {time.time()-t_all:.0f}s", flush=True)

# ---- 汇总 ----
rows = []
for be in MODES:
    for sk, cn in SYMS:
        jp = os.path.join(OUT, f"{sk}_BE{be:g}.json")
        if not os.path.exists(jp):
            continue
        d = json.load(open(jp, encoding="utf-8"))
        rows.append(d)

lines = []
lines.append("保本止损实验汇总（06 MA13 顺势回踩 · 5 品种 · 2021-2023 开发窗）")
lines.append("唯一变量：BREAKEVEN_ATR（0=现状 / 1.0=浮盈≥1×ATR 后止损移到开仓成交价）")
lines.append("")
hdr = f"{'品种':<6}{'BE':>5}{'笔数':>7}{'胜率':>8}{'夏普':>9}{'收益率':>9}{'最大回撤':>9}{'爆仓':>6}{'止损':>7}{'保本':>7}{'时间':>7}"
lines.append(hdr)
for be in MODES:
    for sk, cn in SYMS:
        d = next((x for x in rows if x["symbol_key"] == sk and abs(x["BREAKEVEN_ATR"] - be) < 1e-9), None)
        if not d:
            continue
        c = d["exit_reason_cnt"]
        lines.append(f"{cn:<6}{d['BREAKEVEN_ATR']:>5g}{d['trades']:>7}{d['win_rate']*100:>7.1f}%"
                     f"{d['sharpe']:>9.3f}{d['total_return']*100:>8.1f}%{d['max_drawdown']*100:>8.1f}%"
                     f"{'是' if d['blown'] else '否':>6}{c.get('止损',0):>7}{c.get('保本止损',0):>7}{c.get('时间止损',0):>7}")
        st = d["exit_reason_stats"]
        parts = [f"{k}: n={v['n']} 均{v['pnl_avg']:+.0f} 胜{v['win_n']}" for k, v in st.items()]
        lines.append("      " + " | ".join(parts))
txt = "\n".join(lines)
print("\n" + txt, flush=True)
open(os.path.join(OUT, "_汇总.txt"), "w", encoding="utf-8").write(txt + "\n")
