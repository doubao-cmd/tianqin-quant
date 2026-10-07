# -*- coding: utf-8 -*-
"""名义夹隔离对照：复利口径 + 暂时关闭名义夹（NOTIONAL_CAP 置巨值）
目的：为棕榈油/天然橡胶/白糖/生猪 补一份「只有复利、没有名义夹」的干净基准，
      这样它们与名义夹版的差值才能单变量归因（此前它们只有固定20万口径的旧档，差值混了两个变化）。
安全：try/finally 保证脚本一定还原成 2.0；产物归档到 _名义夹隔离_20261007/。
"""
import glob
import io
import json
import os
import shutil
import subprocess
import sys

PY = sys.executable
VAR = ["棕榈油", "天然橡胶", "白糖", "生猪"]
BASE = "回测结果/06_MA13超短线"
OUT = f"{BASE}/_名义夹隔离_20261007"
os.makedirs(OUT, exist_ok=True)

CON = "C:/Users/cc/.workbuddy/binaries/python/envs/tqsdk/Scripts/python.exe"

for v in VAR:
    script = f"策略/06_MA13超短线/回测/{v}/MA13超短线_回测.py"
    src = io.open(script, encoding="utf-8").read()
    old = "NOTIONAL_CAP = 2.0"
    assert src.count(old) == 1, f"{v} 结构不符"
    # ★ 先把「有名义夹」版存一份到 OUT，最后用它还原正式目录——
    #   教训(v1 缺陷)：只还原 .py 不还原结果目录 ⇒ 正式报告变成无名义夹版本
    for suf in ("_参数.json", ".txt"):
        g = glob.glob(f"{BASE}/{v}/*开发窗*{suf}")
        if g:
            shutil.copy(g[0], f"{OUT}/{v}_开发窗_复利有名义夹{suf}")
    try:
        io.open(script, "w", encoding="utf-8", newline="\n").write(
            src.replace(old, "NOTIONAL_CAP = 999999.0  # 临时关闭（隔离对照用，跑完还原）"))
        print(f"[{v}] 名义夹已临时关闭，开始回测", flush=True)
        p = subprocess.run([CON, "-u", script], capture_output=True, text=True,
                           env={**os.environ, "MA13_BATCH": "1"}, timeout=2400)
        if p.returncode != 0:
            print(f"[{v}] 回测失败 rc={p.returncode}：{(p.stderr or '')[-200:]}", flush=True)
            continue
        for suf in ("_参数.json", ".txt"):
            g = glob.glob(f"{BASE}/{v}/*开发窗*{suf}")
            if g:
                shutil.copy(g[0], f"{OUT}/{v}_开发窗_复利无名义夹{suf}")
                # 还原正式目录为「有名义夹」版
                shutil.copy(f"{OUT}/{v}_开发窗_复利有名义夹{suf}", g[0])
        print(f"[{v}] 完成归档并已还原正式结果目录", flush=True)
    finally:
        io.open(script, "w", encoding="utf-8", newline="\n").write(src)
        assert "NOTIONAL_CAP = 2.0" in io.open(script, encoding="utf-8").read()
        print(f"[{v}] 脚本已还原 NOTIONAL_CAP=2.0", flush=True)
print("ALL_DONE")