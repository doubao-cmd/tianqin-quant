# -*- coding: utf-8 -*-
"""
==========================================================
 模拟盘自检.py —— 逐个入口验证「能不能正常运行、有没有心跳」
==========================================================
为什么要它：
    改完参数（比如 2026-09-27 把 12 品种 × 5 策略统一切到观察模式）之后，
    「双击能不能起来」这件事没人验过。回测脚本静态扫一眼就行，模拟盘不行 ——
    它要真连快期模拟账户、真订行情、真进主循环。本工具就是替你把 60 个入口
    逐个跑一遍。

两种检查：
  【静态】不连网，秒级。逐个 exec 脚本（不触发 __main__），核对：
            · 语法能不能编译
            · SYMBOL_KEY 与目录里的品种名对不对得上
            · ALLOW_TRADE 是不是 False（观察模式，防误下单）
            · 调的是哪个 engine.run_live_*（策略与函数要对应）
            · 同目录 模拟盘.bat 是否 CRLF、是否调用本目录那个 .py、有没有 chcp
            · ★ 与同策略同品种的【回测脚本】逐项对照口径（K 线周期、止损、
              手数上限、开盘保护、failsafe……）—— 两侧走偏了就等于白测

  【动态】逐个真连（约 10 秒/个，串行）。为不动真实台账：
            · 强制 web_gui=False（否则会弹出 60 个浏览器页）
            · 强制 allow_trade=False（自检绝不下单）
            · 把 record_dir / ledger_dir 改指到临时目录
            · 监视 TargetPosTask 是否被创建 —— 创建成功 = 已拿到 quote +
              主力合约 + 账户，说明行情链路真的通了
            · 再看临时目录里有没有落下「_运行心跳.csv」且写进了行

  问题分两档，不要混：
     ✗ 硬错误  —— 入口跑不起来，或者与回测口径分叉（必须改）
     ! 提醒    —— 跑得起来，但与其他品种/策略不一致（比如不落运行心跳）

用法：
    python 工具/模拟盘自检.py                # 静态 + 动态（全量）
    python 工具/模拟盘自检.py --静态          # 只做静态（秒级，不连网）
    python 工具/模拟盘自检.py --策略 01       # 只查 01 双均线
    python 工具/模拟盘自检.py --品种 PVC      # 只查 PVC
    python 工具/模拟盘自检.py --超时 40       # 动态每个入口最多等多少秒

也可以双击  工具/模拟盘自检.bat
"""
import os
import re
import ast
import sys
import time
import argparse
import inspect
import subprocess
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config_symbols

# ---------------------------------------------------------------- 常量表
策略目录名 = ["01_双均线", "02_均线排列", "03_均值回归", "04_通道突破", "05_摆动线"]
# ★ 2026-09-28 用户拍板：02_均线排列 停用，不纳入自检。
#   原因：它走 run_backtest_align / run_live_align —— 这两个函数的签名里
#   连 risk_pct / slip_ticks / margin_per_hand / risk_equity 都没有（纯固定 1 手），
#   且它自己内部周期都不统一（PVC 30min、其余 11 品种 15min）。
#   停用只影响「是否纳入自检」：策略文件一个字不删，
#   删掉 `策略/<策略>/_已停用.md` 即自动恢复（菜单那边同款逻辑）。
_停用策略 = {s for s in 策略目录名
             if os.path.isfile(os.path.join(_ROOT, "策略", s, "_已停用.md"))}
策略目录名 = [s for s in 策略目录名 if s not in _停用策略]
策略中文 = {"01_双均线": "双均线", "02_均线排列": "均线排列", "03_均值回归": "均值回归",
            "04_通道突破": "通道突破", "05_摆动线": "摆动线"}
# 每个策略的模拟盘入口该调哪个 engine 函数（对不上就是接错线）
模拟盘函数 = {"01_双均线": "run_live", "02_均线排列": "run_live_align",
              "03_均值回归": "run_live", "04_通道突破": "run_live",
              "05_摆动线": "run_live_swing"}
回测函数 = {"01_双均线": "run_backtest", "02_均线排列": "run_backtest_align",
            "03_均值回归": "run_backtest", "04_通道突破": "run_backtest",
            "05_摆动线": "run_backtest_swing"}
品种名到键 = {v[0]: k for k, v in config_symbols.SYMBOLS.items()}

# 全项目约定（2026-09-27 定稿口径）——只核对，不改脚本
约定 = {
    "RISK_EQUITY": 200000.0,     # 20 万，与回测初始资金对齐
    "TARGET_RISK_PCT": 1.0,
    "RISK_PCT": 1.0,             # 05 用的名字
    "HAND_MAX": 8,
    "RISK_HAND_MAX": 8,
    "ALLOW_TRADE": False,        # 全项目统一观察模式
}

# 回测 / 模拟盘都要对得上的口径项（名字取两边共用的 kwargs）
口径项 = ["kline_sec", "pass_dir", "signal_kind", "atr_stop_mult", "breakeven_atr",
          "atr_period", "risk_pct", "risk_hand_min", "risk_hand_max", "risk_equity",
          "no_open_window_min", "turtle_failsafe", "stop_atr", "hist_exit",
          "stop_pct", "ma_filter", "adx_filter", "entry_ext_atr", "stop_atr"]

PY_FALLBACK = os.path.join(os.environ.get("USERPROFILE", ""),
                           ".workbuddy", "binaries", "python", "envs",
                           "tqsdk", "Scripts", "python.exe")

_耗时字段 = "耗时"


# ---------------------------------------------------------------- 采集入口
def 找入口(只查策略=None, 只查品种=None):
    """返回 [(策略, 品种, 脚本, bat, 台账bat, 回测脚本)]"""
    out = []
    for s in 策略目录名:
        if 只查策略 and not s.startswith(只查策略):
            continue
        base = os.path.join(_ROOT, "策略", s, "模拟盘")
        if not os.path.isdir(base):
            continue
        for p in sorted(os.listdir(base)):
            d = os.path.join(base, p)
            if not os.path.isdir(d):
                continue
            if 只查品种 and p != 只查品种:
                continue
            名字 = [f for f in os.listdir(d) if f.endswith("_模拟盘.py")]
            脚 = os.path.join(d, 名字[0]) if len(名字) == 1 else None
            bat = os.path.join(d, "模拟盘.bat")
            台账 = os.path.join(d, "看台账.bat")
            回测 = os.path.join(_ROOT, "策略", s, "回测", p,
                                策略中文[s] + "_回测.py")
            out.append((s, p, 脚,
                        bat if os.path.exists(bat) else None,
                        台账 if os.path.exists(台账) else None,
                        回测 if os.path.exists(回测) else None))
    return out


# ---------------------------------------------------------------- 取值小工具
def 参数顺序(函数名):
    """engine 函数的形参名顺序 —— 用来把位置实参绑到参数名上。"""
    try:
        import lib.engine as _E
        fn = getattr(_E, 函数名, None)
        if fn is None:
            return []
        return [p.name for p in inspect.signature(fn).parameters.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    except Exception:
        return []


def 取调用参数(源码, 函数名, ns):
    """AST 里找 engine.<函数名>(...) 的关键字实参，尽量求出真实值。

    ★ 必须同时处理【位置实参】：01/03/04 的回测脚本是
      run_backtest(SYMBOL_KEY, signal_fn, N1, N2, VOLUME, START, END,
                   INIT_BALANCE, KLINE_SEC, ...) —— kline_sec 在第 9 个位置，
      只看 keywords 会把它读成引擎默认值(86400)，凭空报一个假差异。
    同一个函数被调多次时取源码里最靠后的那次（= __main__ 里那次）。
    """
    树 = ast.parse(源码)
    形参 = 参数顺序(函数名)
    命中 = None
    for node in ast.walk(树):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == 函数名:
            if 命中 is None or node.lineno > 命中.lineno:
                命中 = node
    if 命中 is None:
        return None
    值 = {}
    for i, a in enumerate(命中.args):
        if i < len(形参):
            值[形参[i]] = 求值(a, ns)
    for k in 命中.keywords:
        if k.arg:
            值[k.arg] = 求值(k.value, ns)
    return 值


def 求值(node, ns):
    try:
        代码 = compile(ast.Expression(node), "<自检>", "eval")
        return eval(代码, {"__builtins__": {}}, ns)
    except Exception:
        try:
            return ast.unparse(node)
        except Exception:
            return "?"


# ---------------------------------------------------------------- 静态检查
def 静态检查(策略, 品种, 脚本, bat, 台账bat, 回测脚本):
    """不连网。返回 (硬错误列表, 提醒列表, 参数 dict)"""
    硬 = []
    醒 = []
    参数 = {}

    if 脚本 is None:
        return ["目录里找不到唯一一个 *_模拟盘.py"], 醒, 参数

    src = open(脚本, encoding="utf-8").read()
    ns = {"__name__": "自检_不执行main", "__file__": 脚本}
    旧out, 旧err = sys.stdout, sys.stderr
    try:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = 旧out
        exec(compile(src, 脚本, "exec"), ns)
    except Exception as e:
        硬.append(f"脚本加载失败：{type(e).__name__}: {e}")
        return 硬, 醒, 参数
    finally:
        sys.stdout = 旧out
        sys.stderr = 旧err

    # ---- 1. 品种对不对得上 ----
    键 = ns.get("SYMBOL_KEY")
    参数["SYMBOL_KEY"] = 键
    if 品种 not in 品种名到键:
        硬.append(f"目录名「{品种}」不在 config_symbols.SYMBOLS 里")
    else:
        期望键 = 品种名到键[品种]
        if 键 != 期望键:
            硬.append(f"SYMBOL_KEY={键!r} 与目录「{品种}」不符，应为 {期望键!r}")

    # ---- 2. 全局约定 ----
    for k, want in sorted(约定.items()):
        if k in ns:
            参数[k] = ns[k]
            if ns[k] != want:
                硬.append(f"{k}={ns[k]!r}，全项目约定 {want!r}")

    # ---- 3. 调的 engine 函数对不对 ----
    用 = None
    for m in re.findall(r"engine\.(run_live\w*)\s*\(", src):
        用 = m
        break
    参数["引擎函数"] = 用
    if 用 != 模拟盘函数[策略]:
        硬.append(f"调用 engine.{用}，本策略应为 engine.{模拟盘函数[策略]}")

    # ---- 4. 运行记录 / 账户流水单 ----
    try:
        import lib.engine as _E
        签名 = inspect.signature(getattr(_E, 用)) if 用 else None
    except Exception:
        签名 = None
    有rec = bool(签名 and "record_dir" in 签名.parameters)
    有led = bool(签名 and "ledger_dir" in 签名.parameters)
    接rec = ("record_dir=" in src)
    接led = ("ledger_dir=" in src)
    参数.update({"支持运行记录": 有rec, "接了运行记录": 接rec,
                 "支持账户流水单": 有led, "接了账户流水单": 接led,
                 "RECORD_DIR": ns.get("RECORD_DIR", "未定义")})
    if 有rec and not 接rec:
        醒.append("没接 record_dir —— 不会落「运行心跳.csv」")
    if 有led and not 接led:
        醒.append("没接 ledger_dir —— 不会落「账户流水单」")
    if 接rec and not 有rec:
        硬.append("脚本传了 record_dir，但该 engine 函数不支持这个参数")

    # ---- 5. 与回测脚本对照口径 ----
    参_模 = 取调用参数(src, 用, ns) if 用 else None
    参_测 = None
    if 回测脚本:
        回源 = open(回测脚本, encoding="utf-8").read()
        回ns = {"__name__": "自检_不执行main", "__file__": 回测脚本}
        try:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
            exec(compile(回源, 回测脚本, "exec"), 回ns)
        except Exception:
            回ns = ns
        finally:
            sys.stdout = 旧out
        参_测 = 取调用参数(回源, 回测函数[策略], 回ns)
        参数["回测脚本"] = os.path.relpath(回测脚本, _ROOT)
    else:
        硬.append(f"找不到对应的回测脚本 策略/{策略}/回测/{品种}/"
                  f"{策略中文[策略]}_回测.py")

    if 参_模 and 参_测:
        默认_模 = 取默认值(用)
        默认_测 = 取默认值(回测函数[策略])
        比对项 = list(dict.fromkeys(口径项))
        # risk_equity：回测那边走的是 init_balance（位置参数）。只有策略真的用了
        # 波动率预算（risk_pct>0）才比这一项 —— 02 是固定手数，比了纯属误报。
        try:
            _用risk = max(float(参_模.get("risk_pct", 0) or 0),
                          float(参_测.get("risk_pct", 0) or 0)) > 0
        except Exception:
            _用risk = False
        if _用risk:
            参_测.setdefault("risk_equity", 参_测.get("init_balance"))
        elif "risk_equity" in 比对项:
            比对项.remove("risk_equity")
        差异 = []
        for k in 比对项:
            v1 = 参_模.get(k, 默认_模.get(k, "（两边都没有）"))
            v2 = 参_测.get(k, 默认_测.get(k, "（两边都没有）"))
            if 判等(v1, v2):
                continue
            差异.append(f"{k}：模拟盘 {简(v1)} ｜ 回测 {简(v2)}")
        if 差异:
            醒.append("与回测脚本口径不同 → " + "；".join(差异))
    elif 参_模 is None:
        硬.append(f"解析不出 engine.{用}(...) 的调用参数")

    # ---- 6. bat ----
    if bat is None:
        硬.append("同目录没有 模拟盘.bat（用户双击的入口）")
    else:
        raw = open(bat, "rb").read()
        裸LF = raw.count(b"\n") - raw.count(b"\r\n")
        if 裸LF:
            硬.append(f"模拟盘.bat 有 {裸LF} 处裸 LF（bat 必须 CRLF）")
        txt = raw.decode("utf-8-sig", "replace")
        调 = re.findall(r'"%PY%"\s+-u\s+(\S+\.py)', txt)
        if not 调:
            硬.append("模拟盘.bat 里没找到「%PY% -u xxx.py」调用行")
        elif 调[0] != os.path.basename(脚本):
            硬.append(f"模拟盘.bat 调的是 {调[0]}，本目录是 {os.path.basename(脚本)}")
        if "chcp 65001" not in txt:
            硬.append("模拟盘.bat 缺 chcp 65001（中文会乱码）")

    return 硬, 醒, 参数


def 判等(a, b):
    if isinstance(a, str) or isinstance(b, str):
        return str(a).strip() == str(b).strip()
    try:
        return abs(float(a) - float(b)) < 1e-9
    except Exception:
        return a == b


def 取默认值(函数名):
    """engine 函数的参数默认值 —— 「脚本没传」要按默认值比，不能当成一致。"""
    try:
        import lib.engine as _E
        fn = getattr(_E, 函数名, None)
        if fn is None:
            return {}
        return {k: v.default for k, v in inspect.signature(fn).parameters.items()
                if v.default is not inspect.Parameter.empty}
    except Exception:
        return {}


def 简(v):
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, (list, tuple)):
        return "(" + ",".join(简(x) for x in v) + ")"
    return str(v)


# ---------------------------------------------------------------- 动态检查
# 子进程里跑的东西。全 ASCII 源码，用 -c 传进去；中文只出现在被 exec 的业务脚本里。
#   ★ 为什么要「cap」这个参数：
#     收盘后（含周末）行情没有推送，run_live* 开头那句
#     `api.wait_update(deadline=time.time() + 30)` 一定会**死等到 30 秒**才返回 ——
#     实测每个入口因此固定多花 30 秒（60 个 = 半小时）。
#     但数据其实早就到齐了（get_kline_serial 是同步取数），所以自检时可以把
#     deadline 削到 cap 秒；削不动才退回「原样等满 30 秒」重试一次。
#   ★ 心跳：把 heartbeat 强压到 5 秒，只为在几秒内看到那行「盘面 ...」，
#     证明心跳链路真的在跑（CSV 那层（01/03/04）是启动即写，不受影响）。
_运行器源码 = r'''
import os, sys, time, runpy, inspect, traceback, threading
script, rec, ledger, limit, grace, cap = sys.argv[1:7]
limit = float(limit); grace = float(grace); cap = float(cap)
root = os.path.dirname(os.path.abspath(script))
while root != os.path.dirname(root) and not os.path.isdir(os.path.join(root, "lib")):
    root = os.path.dirname(root)
sys.path.insert(0, root)
import lib.engine as E

HB = chr(30424) + chr(38754)      # 盘面
st = {"fn": None, "sym": None, "tp": False, "exc": None, "hb": False}

class _Tee:
    def __init__(self, real):
        self.real = real
        self.buf = []
        self.lock = threading.Lock()
    def write(self, s):
        if s:
            with self.lock:
                self.buf.append(s)
        return self.real.write(s)
    def flush(self):
        try:
            self.real.flush()
        except Exception:
            pass
    def isatty(self):
        return False

_tee = _Tee(sys.stdout)
sys.stdout = _tee

def _hb():
    with _tee.lock:
        return HB in "".join(_tee.buf)

_orig_TqApi = E.TqApi
_orig_tp = E.TargetPosTask

if cap and cap > 0:
    class _FastApi(_orig_TqApi):
        def wait_update(self, deadline=None, **kw):
            if deadline:
                deadline = min(deadline, time.time() + cap)
            return _orig_TqApi.wait_update(self, deadline=deadline, **kw)
    E.TqApi = _FastApi

def _tp(api, symbol, **kw):
    st["sym"] = str(symbol)
    st["tp"] = True
    return _orig_tp(api, symbol, **kw)

E.TargetPosTask = _tp

def _wrap(nm, fn):
    sig = inspect.signature(fn)
    def g(*a, **kw):
        st["fn"] = nm
        kw["web_gui"] = False
        kw["allow_trade"] = False
        if "heartbeat" in sig.parameters:
            kw["heartbeat"] = 5
        if "record_dir" in sig.parameters and rec:
            kw["record_dir"] = rec
            if "record_heartbeat_sec" in sig.parameters:
                kw["record_heartbeat_sec"] = 30
        if "ledger_dir" in sig.parameters and ledger:
            kw["ledger_dir"] = ledger
        def _run():
            try:
                fn(*a, **kw)
            except BaseException:
                st["exc"] = traceback.format_exc()
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        _end = time.time() + limit
        while time.time() < _end:
            if st["exc"]:
                break
            if st["tp"] and (_hb() or st["hb"]):
                break
            t.join(0.2)
        if st["tp"]:
            time.sleep(grace)        # 留一点时间让心跳/记录落盘
        return None
    return g

for _nm in ("run_live", "run_live_boll", "run_live_align", "run_live_swing", "run_live_pro"):
    if hasattr(E, _nm):
        setattr(E, _nm, _wrap(_nm, getattr(E, _nm)))

try:
    runpy.run_path(script, run_name="__main__")
except SystemExit:
    pass
except BaseException:
    st["exc"] = traceback.format_exc()

st["hb"] = _hb()
print("@@FUNC=" + str(st["fn"]), flush=True)
print("@@SYM=" + str(st["sym"]), flush=True)
print("@@TP=" + ("1" if st["tp"] else "0"), flush=True)
print("@@HB=" + ("1" if st["hb"] else "0"), flush=True)
print("@@EXC=" + ("1" if st["exc"] else "0"), flush=True)
if st["exc"]:
    print(st["exc"], flush=True)
sys.stdout.flush()
os._exit(0)
'''


def 跑一次(脚本, 工作目录, 超时, cap, 优雅=3.0):
    os.makedirs(工作目录, exist_ok=True)
    rec = os.path.join(工作目录, "记录")
    led = os.path.join(工作目录, "账户")
    os.makedirs(rec, exist_ok=True)
    os.makedirs(led, exist_ok=True)

    py = sys.executable
    if not os.path.exists(py) and os.path.exists(PY_FALLBACK):
        py = PY_FALLBACK

    t0 = time.time()
    try:
        p = subprocess.run(
            [py, "-u", "-c", _运行器源码, 脚本, rec, led, str(超时), str(优雅),
             str(cap)],
            capture_output=True, cwd=_ROOT, timeout=超时 + 90)
        out = p.stdout.decode("utf-8", "replace")
        err = p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        return None, "", "", time.time() - t0, {"ok": False,
                "说明": f"子进程 {超时 + 90:.0f}s 未退出（疑似卡死）",
                "心跳行数": 0, "主力合约": "", "日志": str(e)[:400]}

    耗时 = time.time() - t0
    # ★ 必须 strip：Windows 上子进程 stdout 是文本模式，"\n" 会翻成 "\r\n"，
    #   不 strip 的话 @@TP 会读成 "1\r"，判等失败 -> 白白误报「没连上」。
    标志 = {k: v.strip() for k, v in
            re.findall(r"^@@(\w+)=(.*)$", out, re.M)}

    if 标志.get("EXC") == "1":
        尾 = out.split("@@EXC=1", 1)[-1].strip().splitlines()
        日志 = "\n".join(尾[-14:]) or err.strip()[-1200:]
        return 标志, out, err, 耗时, {"ok": False, "说明": "运行中抛异常",
                "心跳行数": 0, "主力合约": 标志.get("SYM", ""), "日志": 日志}

    if 标志.get("TP") != "1":
        合并 = []
        for tag, txt in (("stdout", out), ("stderr", err)):
            尾巴 = [L for L in txt.strip().splitlines()
                    if "免责条款" not in L and "shinnytech.com/blog" not in L]
            if 尾巴:
                合并.append(f"--- {tag} ---")
                合并.extend(尾巴[-40:])
        return 标志, out, err, 耗时, {
                "ok": False, "说明": "没连上 / 没订到行情（TargetPosTask 未创建）",
                "心跳行数": 0, "主力合约": "", "日志": "\n".join(合并)}

    心跳行 = 0
    for f in os.listdir(rec):
        if f.endswith("_运行心跳.csv"):
            with open(os.path.join(rec, f), encoding="utf-8-sig") as fh:
                心跳行 = max(0, len(fh.read().splitlines()) - 1)
    d = {"ok": True, "说明": "已进主循环", "心跳行数": 心跳行,
         "控制台心跳": 标志.get("HB") == "1",
         "主力合约": 标志.get("SYM", ""), "日志": ""}
    return 标志, out, err, 耗时, d


def 动态检查(脚本, 工作目录, 超时, cap=5.0):
    """先快速跑一次（削 wait_update 的死等）；不成再原样重试一次。"""
    标志, out, err, 耗时, d = 跑一次(脚本, 工作目录, 超时, cap)
    if d["ok"]:
        d[_耗时字段] = 耗时
        return d
    标志2, out2, err2, 耗时2, d2 = 跑一次(脚本, 工作目录, 超时, 0)
    if d2["ok"]:
        d2[_耗时字段] = 耗时 + 耗时2
        d2["说明"] += "（快跑没成，原样重试后通过）"
        return d2
    d[_耗时字段] = 耗时 + 耗时2
    d["日志"] = (d["日志"] + "\n\n===== 原样重试 =====\n" + d2["日志"])[-2500:]
    return d


def 心跳文本(r):
    """心跳 CSV 那一列。★ 脚本本来就没接 record_dir 的策略（02/05）显示「无」，
    不要显示 0 —— 0 会和「接了但没写进去」混淆，那是两种完全不同的情况。"""
    d = r["动态"]
    if not d:
        return "—"
    if not r["参数"].get("接了运行记录", True):
        return "无"
    return str(d.get("心跳行数", "—"))


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="模拟盘自检：能不能跑、有没有心跳")
    ap.add_argument("--静态", action="store_true", help="只做静态检查，不连网")
    ap.add_argument("--策略", default=None, help="只查某个策略，如 01")
    ap.add_argument("--品种", default=None, help="只查某个品种，如 PVC")
    ap.add_argument("--超时", type=float, default=70.0,
                    help="动态每个入口最多等多少秒（默认 70）")
    ap.add_argument("--cap", type=float, default=5.0,
                    help="快跑：把 run_live* 开头那次 wait_update 的死等削到多少秒"
                         "（0 = 不削，原样等满 30 秒；默认 5）。削不动会自动原样重试。")
    ap.add_argument("--无报告", action="store_true", help="不写 markdown 报告")
    args = ap.parse_args()

    入口 = 找入口(args.策略, args.品种)
    if not 入口:
        print("没找到匹配的模拟盘入口")
        return 1

    戳 = datetime.now().strftime("%Y%m%d_%H%M%S")
    工作根 = os.path.join(_ROOT, "工具", "_输出", f"_自检临时_{戳}")
    行 = []
    方式 = "仅静态" if args.静态 else f"静态 + 真连（每入口≤{args.超时:g}s）"
    print("=" * 78)
    print(f" 模拟盘自检   入口 {len(入口)} 个   {方式}")
    print("=" * 78)

    n通过 = 0
    for i, (策略, 品种, 脚本, bat, 台账bat, 回测) in enumerate(入口, 1):
        硬, 醒, 参数 = 静态检查(策略, 品种, 脚本, bat, 台账bat, 回测)
        结果 = {"策略": 策略, "品种": 品种, "硬": 硬, "醒": 醒,
                "参数": 参数, "动态": None, "标记": ""}
        if args.静态:
            标记 = "静态OK" if not 硬 else f"静态✗{len(硬)}"
        else:
            print(f"[{i:>2}/{len(入口)}] {策略} / {品种} ... ", end="", flush=True)
            d = 动态检查(脚本, os.path.join(工作根, 策略, 品种), args.超时,
                        args.cap)
            结果["动态"] = d
            if d["ok"]:
                心跳 = f"心跳CSV{d['心跳行数']}行" + ("+控制台✓" if d.get("控制台心跳")
                                                       else "+控制台✗")
                标记 = f"OK {d[_耗时字段]:.1f}s {心跳}"
            else:
                标记 = f"✗ {d['说明']}"
            if 硬:
                标记 += f" ｜口径✗{len(硬)}"
            print(标记, flush=True)
        结果["标记"] = 标记
        if not 硬 and (args.静态 or 结果["动态"]["ok"]):
            n通过 += 1
        行.append(结果)

    print()
    print("=" * 78)
    print(" 明细")
    print("=" * 78)
    print(f"{'策略':<12}{'品种':<10}{'结果':<26}{'主力合约':<14}"
          f"{'耗时':>7}{'心跳CSV':>9}{'控制台':>7}  提醒")
    print("-" * 78)
    for r in 行:
        d = r["动态"] or {}
        耗时 = f"{d[_耗时字段]:.1f}s" if d.get(_耗时字段) else "—"
        心跳 = 心跳文本(r)
        控 = ("✓" if d.get("控制台心跳") else "✗") if d else "—"
        提醒 = "；".join(r["醒"]) if r["醒"] else ""
        print(f"{r['策略']:<12}{r['品种']:<10}{r['标记'][:24]:<26}"
              f"{(d.get('主力合约') or '—')[:12]:<14}{耗时:>7}{心跳:>9}{控:>7}"
              + (f"  ! {提醒[:36]}" if 提醒 else ""))

    硬项 = [r for r in 行 if r["硬"]]
    动态坏 = [r for r in 行 if r["动态"] and not r["动态"]["ok"]]
    print()
    if not 硬项 and not 动态坏:
        print(f" 全部通过：{n通过}/{len(行)}")
    else:
        print(f" 通过 {n通过}/{len(行)}   硬错误 {len(硬项)} 个 / 动态失败 {len(动态坏)} 个")
        for r in 硬项:
            for q in r["硬"]:
                print(f"   ✗ {r['策略']}/{r['品种']}  {q}")
        for r in 动态坏:
            print(f"   ✗ {r['策略']}/{r['品种']}  [动态] {r['动态']['说明']}")
            for L in (r["动态"].get("日志") or "").strip().splitlines()[-6:]:
                print(f"        {L}")

    # 提醒统一收口（同一个问题在很多品种上重复，按内容归类，不刷屏）
    归类 = {}
    for r in 行:
        for q in r["醒"]:
            归类.setdefault(q, []).append(f"{r['策略'][:2]}/{r['品种']}")
    if 归类:
        print()
        print("-" * 78)
        print(f" 提醒 {len(归类)} 类（跑得起来，但口径不齐）")
        print("-" * 78)
        for q, who in 归类.items():
            print(f"  ! {q}")
            print(f"      共 {len(who)} 个：{'、'.join(who)}")

    if not args.无报告:
        报告 = os.path.join(_ROOT, "工具", "_输出", "_模拟盘自检_结果.md")
        写报告(报告, 行, args, n通过, 归类, 工作根)
        print()
        print(f" 报告已落盘 -> {报告}")
    print(f" 自检临时文件 -> {工作根}")

    try:
        from lib import recycle
        if os.path.isdir(工作根):
            recycle.移入回收站([工作根])
            print(" 自检临时文件已送入回收站")
    except Exception as e:
        print(f" （自检临时文件未能自动清理：{e}）")
    return 0 if (not 硬项 and not 动态坏) else 1


def 写报告(path, 行, args, n通过, 归类, 工作根):
    L = []
    L.append("# 模拟盘自检结果")
    L.append("")
    L.append(f"- 时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    L.append(f"- 方式：{'仅静态（未连网）' if args.静态 else '静态 + 真连天勤'}")
    L.append(f"- 结果：**通过 {n通过} / {len(行)}**")
    L.append("")
    L.append("## 一、逐个入口")
    L.append("")
    L.append("| 策略 | 品种 | 结果 | 主力合约 | 耗时 | 心跳CSV行 | 控制台心跳 | 提醒 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in 行:
        d = r["动态"] or {}
        耗时 = f"{d[_耗时字段]:.1f}s" if d.get(_耗时字段) else "—"
        心跳 = 心跳文本(r)
        控 = ("✓" if d.get("控制台心跳") else "✗") if d else "—"
        提醒 = "；".join(r["醒"]) or "—"
        L.append(f"| {r['策略']} | {r['品种']} | {r['标记']} | "
                 f"{d.get('主力合约') or '—'} | {耗时} | {心跳} | {控} | {提醒} |")
    L.append("")
    L.append("## 二、硬错误（入口跑不起来 / 与回测口径分叉）")
    L.append("")
    硬项 = [r for r in 行 if r["硬"]]
    if not 硬项:
        L.append("无。")
    else:
        for r in 硬项:
            L.append(f"### {r['策略']} / {r['品种']}")
            for q in r["硬"]:
                L.append(f"- {q}")
            L.append("")
    L.append("## 三、动态失败日志")
    L.append("")
    失败 = [r for r in 行 if r["动态"] and not r["动态"]["ok"]]
    if not 失败:
        L.append("无。")
    else:
        for r in 失败:
            L.append(f"### {r['策略']} / {r['品种']} —— {r['动态']['说明']}")
            L.append("```")
            L.append((r["动态"].get("日志") or "").strip()[-2000:])
            L.append("```")
            L.append("")
    L.append("## 四、提醒（跑得起来，但口径不齐）")
    L.append("")
    if not 归类:
        L.append("无。")
    else:
        for q, who in 归类.items():
            L.append(f"- **{q}**")
            L.append(f"  - 共 {len(who)} 个：{'、'.join(who)}")
    L.append("")
    L.append("> 「心跳CSV行」那一列：**无** = 该策略的模拟盘脚本本来就没接 `record_dir`，")
    L.append("> 不是「接了却没写」；**0** 才表示接了但一行都没落盘（那才是问题）。")
    L.append("")
    L.append(f"（自检临时文件：`{工作根}`，已自动送入回收站）")
    open(path, "w", encoding="utf-8", newline="\n").write("\n".join(L) + "\n")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断")
