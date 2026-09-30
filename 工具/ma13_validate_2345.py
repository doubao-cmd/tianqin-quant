# -*- coding: utf-8 -*-
"""MA13 ② ③ ④ ⑤ 全品种验证驱动。
- ② OOS 纯净验证：24-25（从未调参）
- ③ 多体制鲁棒性：21-23 开发 / 2020 趋势 / 16-19 死亡
- ④ 成本 realism：开发窗 SLIP=3 压测
- ⑤ 仓位地基：脚本内 HAND_MAX=8 扁平封顶（已在位，仅确认）
每品种一个子进程，隔离 tqsdk 会话（禁并发、每进程 <20 会话）。
汇总落 JSON + 末尾打印可读矩阵。
"""
import os, sys, json, glob, subprocess

ROOT = r"C:/Users/cc/Desktop/天勤量化"
PY = r"C:/Users/cc/.workbuddy/binaries/python/versions/3.13.12/python.exe"
RUNNER = os.path.join(ROOT, "工具/ma13_validate_runner.py")
OUT_JSON = os.path.join(ROOT, "工具/ma13_validate_2345.json")

scripts = sorted(glob.glob(os.path.join(ROOT, "策略/06_MA13超短线/回测/*/MA13超短线_回测.py")))
print(f"发现 {len(scripts)} 个 MA13 品种脚本\n", flush=True)

allres = {}
for sc in scripts:
    var = os.path.basename(os.path.dirname(sc))
    print(f"[launch] {var}", flush=True)
    try:
        r = subprocess.run([PY, RUNNER, sc], capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        allres[var] = {"error": "timeout 1800s"}
        print(f"  !! {var} 超时", flush=True)
        continue
    if r.returncode != 0:
        allres[var] = {"error": (r.stderr or r.stdout)[-600:]}
        print(f"  !! {var} 失败: {(r.stderr or '')[-200:]}", flush=True)
        continue
    lines = [l for l in r.stdout.strip().splitlines() if l.strip().startswith("{")]
    if not lines:
        allres[var] = {"error": "no json", "raw": r.stdout[-400:]}
        continue
    try:
        allres[var] = json.loads(lines[-1])
    except Exception as ex:
        allres[var] = {"error": f"json解析失败: {ex}", "raw": r.stdout[-400:]}

with open(OUT_JSON, "w", encoding="utf-8") as f:
    json.dump(allres, f, ensure_ascii=False, indent=2)
print(f"\n[done] 已落盘 {OUT_JSON}", flush=True)

# ---- 可读矩阵 ----
tags = [("dev", "开发21-23"), ("oos", "OOS24-25"), ("trend20", "趋势2020"),
        ("death", "死亡16-19"), ("dev_slip3", "开发SLIP3")]
hdr = "品种".ljust(8) + "".join(t[1].rjust(14) for t in tags)
print("\n" + hdr, flush=True)
for var, rec in allres.items():
    if "error" in rec:
        print(var.ljust(8) + "  ERROR: " + rec["error"][:80], flush=True)
        continue
    res = rec.get("results", {})
    row = var.ljust(8)
    for tag, _ in tags:
        d = res.get(tag, {})
        if "error" in d:
            row += "  ERR".rjust(14)
        elif d.get("trades", 0) == 0:
            row += "  无数据".rjust(14)
        else:
            tr = d.get("total_return", 0) * 100
            sh = d.get("sharpe", 0)
            row += f"{tr:+.0f}%/{sh:.2f}".rjust(14)
    print(row, flush=True)
