# -*- coding: utf-8 -*-
"""
stub_core_a2.py —— 看门狗 A2 验收专用「假核心」（测试夹具，不是交易程序）
================================================================
存在的唯一理由：A2 要求验证「关窗自愈 / 假死检测 / 退避熔断」，
而这些场景若拿真核心 + 实盘持仓去试，等于用真仓做实验（A2 红线 4 明令禁止）。
⇒ 用本夹具冒充核心：照真核心的规矩写 `<品种>_运行心跳.csv`（同名、同目录、同格式），
  并支持「正常跑 / 启动即崩 / 中途假死 / 按剧本演」四种行为。

调用方式与真核心一致：  python stub_core_a2.py --symbols FU,BU --trade
行为由环境变量 STUB_CFG 指向的 JSON 决定：
  { "dir": 沙箱目录, "hb_dir": 心跳目录, "symbols": ["FU","BU"],
    "mode": "normal|crash|crash1|freeze|script",
    "hb_sec": 1,             # 心跳间隔秒
    "freeze_after": 2,       # freeze 模式：写这么久后僵住（进程不退，心跳停）
    "script": ["crash","run:3","freeze","run:9999"] }
  crash  = 启动即崩 rc=2（配置/令牌类）
  crash1 = 启动期网络失败 rc=1（DNS 解析类，2026-10-20 23:55 真核心实测形态）
"""
import os
import sys
import time
import json
import datetime

CFG = {}
try:
    with open(os.environ["STUB_CFG"], "r", encoding="utf-8") as f:
        CFG = json.load(f)
except Exception as e:
    print(f"[stub] 读配置失败：{e!r}", flush=True)
    sys.exit(97)

DIR = CFG["dir"]
HB_DIR = CFG["hb_dir"]
SYMS = [str(k).upper() for k in CFG.get("symbols") or ["FU"]]
HB_SEC = float(CFG.get("hb_sec", 1))
MODE = str(CFG.get("mode", "normal"))
SCRIPT = CFG.get("script") or []
FREEZE_AFTER = float(CFG.get("freeze_after", 2))

os.makedirs(DIR, exist_ok=True)
os.makedirs(HB_DIR, exist_ok=True)

# ---- 第几次被拉起（看门狗重启次数 = 本文件的启动次数）----
STARTS = os.path.join(DIR, "stub_starts.txt")
n = 1
try:
    with open(STARTS, "r", encoding="utf-8") as f:
        n = int((f.read().strip() or "0")) + 1
except Exception:
    n = 1
with open(STARTS, "w", encoding="utf-8") as f:
    f.write(str(n))
with open(os.path.join(DIR, f"stub_pid_{n}.txt"), "w", encoding="utf-8") as f:
    f.write(f"{os.getpid()}\n{datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")

print(f"[stub] 第 {n} 次启动 pid={os.getpid()} mode={MODE} symbols={','.join(SYMS)}", flush=True)

# ---- 决定本次行为 ----
if MODE == "script":
    behave = SCRIPT[n - 1] if n <= len(SCRIPT) else "run:999999"
elif MODE == "crash":
    behave = "crash"
elif MODE == "crash1":
    behave = "crash1"
elif MODE == "freeze":
    behave = "freeze"
else:
    behave = "run:999999"


def _cn_name(key):
    """key → 中文名（心跳文件名用的是中文名，必须与看门狗 _hb_files 的口径一致）。

    故意复用 config_symbols 而不是在夹具里另写一张表 —— 两边一旦漂移，
    测试就会「自证自洽」，反而掩盖真实环境里的文件名不匹配。
    """
    r = os.path.dirname(os.path.abspath(__file__))
    while r != os.path.dirname(r) and not os.path.isfile(os.path.join(r, "config_symbols.py")):
        r = os.path.dirname(r)
    if r not in sys.path:
        sys.path.insert(0, r)
    try:
        import config_symbols as _cs
        info = _cs.SYMBOLS.get(key)
        if info:
            return str(info[0])
    except Exception:
        pass
    return key


def _write_hb():
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for k in SYMS:
        path = os.path.join(HB_DIR, f"{_cn_name(k)}_运行心跳.csv")
        new = not os.path.exists(path)
        try:
            with open(path, "a", encoding="utf-8") as f:
                if new:
                    f.write("记录时间,K线时间,现价,持仓手数,目标手数\r\n")
                f.write(f"{ts},{ts}.000000,0,0,0\r\n")
        except Exception as e:
            print(f"[stub] 写心跳失败：{e!r}", flush=True)


if behave == "crash":
    print("[stub] 按剧本：启动即崩 rc=2", flush=True)
    sys.exit(2)

if behave == "crash1":
    # ★ 2026-10-21 T6-3 新增：模拟「启动期网络失败 rc=1」
    #   实证来源：2026-10-20 23:55 T6-6 真核心验收时，核心因 auth.shinnytech.com
    #   DNS 解析失败而以 rc=1 退出 —— 与「配置/令牌/席位/依赖」完全不同的失败类型。
    print("[stub] 按剧本：启动期网络失败 rc=1（DNS 解析失败）", flush=True)
    sys.exit(1)

if behave == "freeze":
    # 先正常写一会儿心跳，然后「进程还在、但不动了」——模拟卡死/死循环/等网络
    t0 = time.time()
    while time.time() - t0 < FREEZE_AFTER:
        _write_hb()
        time.sleep(min(HB_SEC, 0.5))
    print("[stub] 按剧本：进入假死（进程存活、心跳停摆）", flush=True)
    while True:
        time.sleep(3600)          # 不退、不写 —— 旧版看门狗永远发现不了这种

run_sec = 999999.0
if behave.startswith("run:"):
    run_sec = float(behave.split(":", 1)[1])

t0 = time.time()
while time.time() - t0 < run_sec:
    _write_hb()
    time.sleep(HB_SEC)
print(f"[stub] 按剧本：正常跑完 {run_sec}s，主动退出 rc=3", flush=True)
sys.exit(3)
