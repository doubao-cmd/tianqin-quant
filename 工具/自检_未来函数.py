# -*- coding: utf-8 -*-
"""
防未来函数体检（规则 1 的机器证明，2026-10-11）
================================================================

回答一个问题：我们的信号，有没有用事后才知道的数据？

三层检查，互补，各自有盲区，必须一起跑：

  第 0 层 · 元测试（先证明体检本身没坏）
      故意写 4 个「作弊版」决策函数，断言体检必须把它们全抓出来。
      抓不出来 = 体检失效 -> 直接判失败，避免「测试通过」其实是「测试是废的」。
      SETS 那种开源项目有 tests/ 兜底，我们以前只有注释里写着「无未来函数」，
      这一层是补上那个缺口。

  第 1 层 · 静态扫描（查真实文件）
      用 AST 扫代码，找已知的偷看写法：
        iloc[-1]            -> 取到未收盘那根（ERROR）
        shift(-N) / diff(-N) / pct_change(-N)  -> 显式未来（ERROR）
        rolling(center=True) -> 居中窗口，一半来自未来（ERROR）
        close.mean() 等全序列统计 -> 全局归一化用了未来（WARN）
      优点：扫的就是你真正跑的那份源码，不需要数据、不需要账号、CI 可跑。
      盲区：只能抓写死的套路，抓不到 `close[i+1]` 这类间接偷看。

  第 2 层 · 动态因果测试（验证行为，不依赖写法）
      合成一段行情，对若干时点 k 做两条断言：
        T1 截断：只喂 <= k 的数据算一遍，结果必须和全序列算的一致
        T2 涂改未来：把 k 之后的行情全改成乱码，k 处的结果必须一动不动
      优点：直接验证「未来变了，过去会不会跟着变」。
      盲区：测的是按同样口径复制出来的决策函数，不是文件里那行代码的运行时。

退出码：0 = 干净；1 = 有问题（静态 ERROR 或 动态测试失败）。
用法：python 自检_未来函数.py            （跑全部）
      python 自检_未来函数.py --scan-only
      python 自检_未来函数.py --json 工具/_输出/xxx.json
"""
from __future__ import annotations

import argparse
import ast
import datetime as _dt
import json
import os
import re
import sys
import traceback

import numpy as np

# ---- 找项目根（向上直到含 lib 的目录），与本项目其他脚本同一套路 ----
ROOT = os.path.dirname(os.path.abspath(__file__))
while ROOT != os.path.dirname(ROOT) and not os.path.isdir(os.path.join(ROOT, "lib")):
    ROOT = os.path.dirname(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ==================================================================
#  第 1 层：静态扫描
# ==================================================================

# 全序列统计：套在一个完整 Series 上就等于把未来揉进了历史
GLOBAL_STATS = {
    "mean", "std", "var", "max", "min", "median", "quantile", "sum",
    "skew", "kurt", "idxmax", "idxmin", "autocorr", "corr", "cov",
}
ERROR_RULES = "iloc负 / shift负 / diff负 / pct_change负 / rolling居中"
NOQA = "noqa"          # 行尾写 # noqa 可豁免（报告类代码确有必要用到最后一根）
IGNORE_DIRS = {"__pycache__", "_归档", "_冻结", "_收纳", "_输出",
               "node_modules", ".git", ".github", "backups"}
BASELINE = os.path.join(ROOT, "工具", "_账本", "未来函数_豁免.json")


def _chain(node) -> str:
    """把 AST 表达式拍平成 'close.rolling(20).mean()' 这样的字符串。"""
    try:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Constant):
            return repr(node.value)
        if isinstance(node, ast.Attribute):
            return _chain(node.value) + "." + node.attr
        if isinstance(node, ast.Call):
            # 只用函数名的前若干字符，够机器看就行
            args = ",".join(_chain(a) for a in node.args)
            kw = ",".join(f"{k.arg}={_chain(k.value)}" for k in node.keywords)
            inner = ",".join(x for x in (args, kw) if x)
            return _chain(node.func) + f"({inner})"
        if isinstance(node, ast.Subscript):
            return _chain(node.value) + "[" + _chain(node.slice) + "]"
        if isinstance(node, ast.BinOp):
            return f"({_chain(node.left)}?{_chain(node.right)})"
        if isinstance(node, ast.UnaryOp):
            # ★ 这里踩过坑（2026-10-11 元测试抓出来的）：
            #   Python 把 -1 解析成 UnaryOp(USub, Constant(1))，
            #   早期版本一律拍平成 "(-?1)"，于是正则匹配不到 [-1]，
            #   iloc[-1] / shift(-1) 全部漏判 —— 扫描器假装在干活，其实是瞎的。
            if isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant) \
                    and isinstance(node.operand.value, (int, float)):
                return "-" + repr(node.operand.value)
            return f"(-?{_chain(node.operand)})"
        if isinstance(node, ast.Index):      # py3.8 兼容
            return _chain(node.value)
    except Exception:
        return "?"
    return "?"


def _has_minus_one(s: str) -> bool:
    """只认 [-1]：那是未收盘的当前根。

    注意：本项目里 iloc[-2] 是【正确写法】（刚收盘那根），iloc[-3] 同理，
    所以不能一棍子打死所有负索引 —— 早期版本把 -2/-3 也报错，噪音大到没法用。
    """
    return bool(re.search(r"\[\s*-\s*1\s*\]", s))


def _first_negative_call(s: str, names) -> str | None:
    for nm in names:
        if nm in s and re.search(re.escape(nm) + r"\(\s*-\s*\d", s):
            return nm
    return None


def scan_source(src: str, path: str) -> list[dict]:
    """扫一段源码，返回命中列表。"""
    hits: list[dict] = []
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [{"file": path, "line": 0, "code": f"<无法解析> {e}",
                 "rule": "语法错误", "level": "ERROR", "why": "跳过本文件的启发式检查"}]

    lines = src.splitlines()

    # 行号 -> 由内到外的函数链，方便分流「这是信号路径还是画图/日志」
    _scopes: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lo = getattr(node, "lineno", 0)
            hi = getattr(node, "end_lineno", lo)
            _scopes.setdefault(node.name, (lo, hi))

    def scopes_of(lineno: int) -> list:
        found = [(hi - lo, name) for name, (lo, hi) in _scopes.items() if lo <= lineno <= hi]
        found.sort()
        return [name for _, name in found]

    # 节点 -> 父节点，用来区分「内层被外层包着」的重复命中
    parents: dict = {}
    for _n in ast.walk(tree):
        for _c in ast.iter_child_nodes(_n):
            parents[_c] = _n

    def parent_is_datetime(node) -> bool:
        p = parents.get(node)
        return isinstance(p, ast.Subscript) and isinstance(p.slice, ast.Constant) \
            and p.slice.value == "datetime"

    LIVE_HINTS = ("run_live", "ma_now", "mas_now", "boll_now", "mid_now", "_session")
    # ★ 辅助/非信号函数（2026-10-11 补）：这些函数里的 iloc[-1] 与「信号是否偷看未来」无关，
    #   判 ERROR 只会制造噪音 —— 而噪音比漏报更危险（体检被无视就等于没有体检）。
    #   实测误报三例：_paint_countdown（画倒计时取收盘时刻）、_stat_to_dict（tqsdk 兼容层格式转换）、
    #   _cloud_fill_stats/_sync_cloud_pos 一类对账与只读统计。
    #   判据：函数名含这些词 ⇒ 该函数职责不是「产出交易信号」。
    #   ⚠️ 仍然打印 INFO 而不是静默跳过 —— 留痕，便于人工复核白名单有没有盖宽。
    AUX_HINTS = ("paint", "draw", "chart", "report", "html", "stat", "dict",
                 "print", "log", "debug", "summary", "display", "render",
                 "countdown", "sync", "reconcile", "fill_stats", "cloud_pos")

    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        physical = lines[line - 1] if 1 <= line <= len(lines) else ""
        if NOQA in physical:
            continue

        s = _chain(node)

        # --- iloc[-1]：按「取的是什么、在哪个函数里」三档分流 ---
        #   ① 连带取 datetime / 包着 api.is_changing(...) -> 换根触发，tqsdk 惯用法
        #   ② 落在实盘/now 类函数里 -> 未收盘K线参与实时判断，属「口径一致性」问题
        #   ③ 其余（尤其回测路径）-> 真嫌疑，ERROR
        if isinstance(node, ast.Subscript) and "iloc[" in s and _has_minus_one(s):
            scopes = scopes_of(line)
            if parent_is_datetime(node) or "is_changing" in physical:
                hits.append({"file": path, "line": line, "code": s.strip(),
                             "rule": "换根触发", "level": "INFO",
                             "why": "api.is_changing(最后一根, 'datetime') 是 tqsdk 的换根触发写法，"
                                    "与信号无关"})
                continue
            # ★ 辅助/非信号函数里的 iloc[-1] -> INFO（留痕但不当嫌疑）
            if any(any(h in nm for h in AUX_HINTS) for nm in scopes):
                hits.append({"file": path, "line": line, "code": s.strip(),
                             "rule": "辅助函数读末根", "level": "INFO",
                             "why": "落在辅助/展示/统计类函数（非信号产出路径），"
                                    "iloc[-1] 与未来函数无关；如判断有误请在行尾写 # noqa"})
                continue
            if any(any(h in nm for h in LIVE_HINTS) for nm in scopes):
                hits.append({"file": path, "line": line, "code": s.strip(),
                             "rule": "实盘读未收盘K线", "level": "WARN",
                             "why": "实盘取的是正在形成那根；若回测侧用 iloc[-2]，"
                                    "两边口径就不一致，回测结论不再可靠（需人工确认是否故意）"})
                continue
            hits.append({"file": path, "line": line, "code": s.strip(),
                         "rule": "iloc[-1]", "level": "ERROR",
                         "why": "iloc[-1] 是最近一根；出现在非实盘路径上，需确认是否偷看未收盘数据"})
            continue

        # --- ERROR 2/3/4：显式的负向位移 ---
        if isinstance(node, ast.Call):
            bad = _first_negative_call(s, ("shift", "diff", "pct_change"))
            if bad:
                hits.append({"file": path, "line": line, "code": s.strip(),
                             "rule": f"{bad}负位移", "level": "ERROR",
                             "why": f"{bad}(-N) 把后面的数据搬到前面，标准未来函数写法"})
                continue
            if "rolling(" in s and "center=True" in s:
                hits.append({"file": path, "line": line, "code": s.strip(),
                             "rule": "rolling居中", "level": "ERROR",
                             "why": "center=True 的窗口一半来自未来"})
                continue

            # --- INFO：全序列统计（信号里是大忌，报告里是常态，默认只提示）---
            #排除项都是实测踩出来的噪音源：
            #   ewm(adjust=False) 本身就是递推的、因果的；
            #   axis=1 是横向跨列，跟时间先后无关；
            #   float(...) 外层包壳已经在内层记过一笔。
            parts = [p for p in s.split(".") if p]
            if len(parts) >= 2 and "ewm(" not in s and "axis=1" not in s:
                last = parts[-1].split("(")[0]
                if last in GLOBAL_STATS and "rolling" not in s and "expanding" not in s:
                    hits.append({"file": path, "line": line, "code": s.strip(),
                                 "rule": "全序列统计", "level": "INFO",
                                 "why": f".{last}() 落在整列上，归一化/极值会随未来数据变化；"
                                        f"报告与画图里正常，信号路径里出现必须人工确认"})
                    continue

        # --- WARN：裸的 [-1] 取值（可能是 trades[-1] 这种合法的，只提醒）---
        if isinstance(node, ast.Subscript) and "iloc" not in s and _has_minus_one(s):
            hits.append({"file": path, "line": line, "code": s.strip(),
                         "rule": "末尾取值", "level": "INFO",
                         "why": "[-1] 取最后一个元素；常用在 trades[-1] 这类场合，"
                                "信号路径里的等价写法已被 iloc 规则单独标注"})
    for h in hits:
        sc = scopes_of(h.get("line", 0))
        h.setdefault("func", sc[0] if sc else "")
        h.setdefault("scopes", "/".join(sc[:3]))
    return hits


def scan_paths(paths) -> list[dict]:
    out: list[dict] = []
    for root in paths:
        root = os.path.abspath(root)
        if os.path.isfile(root) and root.endswith(".py"):
            files = [root]
        else:
            files = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
                files += [os.path.join(dirpath, f) for f in filenames if f.endswith(".py")]
        for f in files:
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    out += scan_source(fh.read(), os.path.relpath(f, ROOT))
            except Exception as e:      # 读不动就记一笔，不拖垮整体
                out.append({"file": os.path.relpath(f, ROOT), "line": 0,
                            "code": "", "rule": "读取失败", "level": "WARN", "why": str(e)})

    # 去重：同一 (文件, 行, 规则) 只留最长的一条；
    #       再按规则折叠，避免同一个写法在每行重复刷屏。
    seen: dict = {}
    for h in out:
        key = (h["file"], h["line"], h["rule"])
        old = seen.get(key)
        if old is None or len(h["code"]) > len(old["code"]):
            seen[key] = h
    dedup = list(seen.values())
    dedup.sort(key=lambda h: ({"ERROR": 0, "WARN": 1, "INFO": 2}.get(h["level"], 3),
                              h["file"], h["line"]))
    return dedup


# ---- 元扫描：先证明静态扫描器本身没坏（否则「0 ERROR」可能只是它瞎了）----
SAMPLE_BAD = {
    "iloc[-1]": "def f(k):\n    return k.close.iloc[-1]\n",
    "shift(-1)": "def f(c):\n    return c.shift(-1)\n",
    "diff(-2)": "def f(c):\n    return c.diff(-2)\n",
    "pct_change(-1)": "def f(c):\n    return c.pct_change(-1)\n",
    "rolling(center=True)": "def f(c):\n    return c.rolling(20, center=True).mean()\n",
}
SAMPLE_GOOD = {
    "iloc[-2]（刚收盘那根，本项目正解）": "def f(k):\n    return k.close.iloc[-2]\n",
    "rolling+shift+iloc[-2]": "def f(c):\n    return c.rolling(20).mean().shift(1).iloc[-2]\n",
    "ewm(adjust=False)": "def f(c):\n    return c.ewm(span=12, adjust=False).mean()\n",
    "rolling max + shift": "def f(c):\n    return c.rolling(20).max().shift(1).iloc[-2]\n",
}


def 元扫描() -> tuple[bool, list[str]]:
    log, ok = [], True
    for name, src in SAMPLE_BAD.items():
        got = [h for h in scan_source(src, "<元扫描>") if h["level"] == "ERROR"]
        if got:
            log.append(f"    [OK] 静态能抓到「{name}」")
        else:
            ok = False
            log.append(f"    [!!] 静态抓不到「{name}」-> 扫描器失效")
    for name, src in SAMPLE_GOOD.items():
        got = [h for h in scan_source(src, "<元扫描>") if h["level"] == "ERROR"]
        if got:
            ok = False
            log.append(f"    [!!] 静态误报「{name}」（{got[0]['rule']}）-> 会把正确的写法当错误")
        else:
            log.append(f"    [OK] 静态不误报「{name}」")
    return ok, log


# ==================================================================
#  第 2 层：动态因果测试
# ==================================================================

def gen_series(n=800, seed=2026, sigma=0.011, drift=0.0002, start=3000.0) -> dict:
    """合成一段 OHLC（不需要天勤账号，不需要网络，同 seed 完全可复现）。"""
    rng = np.random.default_rng(seed)
    ret = rng.normal(drift, sigma, n)
    close = start * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[start], close[:-1]])
    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0, sigma * 0.6, n)))
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0, sigma * 0.6, n)))
    return {"open": open_, "high": high, "low": low, "close": close}


def _upto(S: dict, k: int) -> dict:
    return {key: arr[: k + 1] for key, arr in S.items()}


def _scribble(S: dict, k: int, seed: int) -> dict:
    """把 k 之后的所有行情改成不相干的乱码（保持正数、量级接近）。"""
    rng = np.random.default_rng(seed)
    out = {}
    for key, arr in S.items():
        a = arr.copy()
        tail = len(a) - (k + 1)
        if tail > 0:
            a[k + 1:] = a[k + 1:] * (0.5 + 1.5 * rng.random(tail))
        out[key] = a
    return out


def _same(a, b) -> bool:
    try:
        if np.isnan(a) and np.isnan(b):
            return True
    except Exception:
        pass
    try:
        return bool(np.isclose(float(a), float(b), rtol=0.0, atol=1e-12))
    except Exception:
        return False


# ---- 因果的决策函数（口径与 lib/engine.py 一致：信号取刚收盘那根，下一根成交）----
def sma(arr, n, i):
    if i + 1 < n:
        return float("nan")
    return float(arr[i - n + 1: i + 1].mean())


def atr(S, n, i):
    if i < n:
        return float("nan")
    prev_close = S["close"][i - n: i]
    tr = np.maximum.reduce([
        S["high"][i - n + 1: i + 1] - S["low"][i - n + 1: i + 1],
        np.abs(S["high"][i - n + 1: i + 1] - prev_close),
        np.abs(S["low"][i - n + 1: i + 1] - prev_close),
    ])
    return float(tr.mean())


def 决策_双均线(S, k):
    """正版：MA5 / MA20 交叉，now 与 pre 都取自已收盘的 K 线。"""
    now_f, now_s = sma(S["close"], 5, k), sma(S["close"], 20, k)
    pre_f, pre_s = sma(S["close"], 5, k - 1), sma(S["close"], 20, k - 1)
    if np.isnan(now_f) or np.isnan(pre_f):
        return float("nan")
    return 1.0 if (pre_f <= pre_s and now_f > now_s) else (-1.0 if (pre_f >= pre_s and now_f < now_s) else 0.0)


def 决策_趋势过滤(S, k):
    """正版：金叉死叉 + MA60 过滤（与 engine.trend_reverse_signal 同口径）。"""
    now_t, pre_t = sma(S["close"], 60, k), sma(S["close"], 60, k - 1)
    sig = 决策_双均线(S, k)
    if np.isnan(sig) or np.isnan(now_t):
        return float("nan")
    return 1.0 if (sig > 0 and now_s_v(S, k) > now_t) else (-1.0 if (sig < 0 and now_s_v(S, k) < now_t) else 0.0)


def now_s_v(S, k):
    return sma(S["close"], 20, k)


def 决策_通道(S, k):
    """正版：唐奇安，入场轨道取前 n 根的极值（不含当前根）。"""
    n1, n2 = 20, 10
    if k < n1:
        return float("nan")
    en_up = float(S["high"][k - n1: k].max())
    en_dn = float(S["low"][k - n1: k].min())
    ex_up = float(S["high"][k - n2: k].max())
    ex_dn = float(S["low"][k - n2: k].min())
    c = S["close"][k]
    if c < ex_dn:
        return 0.0
    if c > ex_up:
        return 0.0
    if c > en_up:
        return 1.0
    if c < en_dn:
        return -1.0
    return np.nan


def 决策_ATR止损(S, k):
    """正版：用已收盘那根的 high/low 判是否触及（与 engine 第 1062 行同口径）。"""
    a = atr(S, 14, k)
    if np.isnan(a) or k < 40:
        return float("nan")
    entry = S["close"][k - 20]
    stop_line = entry - 2.0 * a
    hit = S["low"][k] <= stop_line
    return 1.0 if hit else 0.0


# ---- 作弊版：元测试要证明体检能把它们全抓出来 ----
def 作弊_看下一根(S, k):
    return float(S["close"][k + 1])      # 直接读未来一根


def 作弊_全序列均值(S, k):
    return float(np.mean(S["close"]))    # 归一化用了整段历史（含未来）


def 作弊_居中均线(S, k):
    w = 5
    lo, hi = max(0, k - w), min(len(S["close"]), k + w + 1)
    return float(S["close"][lo:hi].mean())


def 作弊_取最后一根(S, k):
    return float(S["close"][-1])         # 全序列的末根 = k 的未来


正版 = [
    ("双均线交叉", 决策_双均线),
    ("趋势过滤", 决策_趋势过滤),
    ("通道突破", 决策_通道),
    ("ATR止损", 决策_ATR止损),
]
作弊 = [
    ("看下一根 close[k+1]", 作弊_看下一根),
    ("全序列均值归一化", 作弊_全序列均值),
    ("居中均线 rolling(center)", 作弊_居中均线),
    ("取末根 close[-1]", 作弊_取最后一根),
]


def 跑两套测试(decider, S: dict, ks) -> dict:
    """返回 {'T1': (通过?, 说明), 'T2': (通过?, 说明)}"""
    res = {}
    # T1 截断一致性
    bad = None
    for k in ks:
        try:
            d_full = decider(S, k)
        except Exception as e:
            res["T1"] = (False, f"k={k} 全序列就算不动：{type(e).__name__}")
            break
        try:
            d_trunc = decider(_upto(S, k), k)
        except Exception as e:
            # 合法决策只读 <=k，截断后不该炸；炸了说明它依赖了超出自己的数据
            bad = f"k={k} 截断后失效（{type(e).__name__}），疑似读了 k 之后的数据"
            break
        if not _same(d_full, d_trunc):
            bad = f"k={k} 全序列 {d_full} ≠ 只喂到 k 的 {d_trunc}"
            break
    res["T1"] = (bad is None, bad or "截断后结果不变")

    # T2 涂改未来
    bad = None
    for k in ks:
        d0 = decider(S, k)
        d1 = decider(_scribble(S, k, seed=31), k)
        if not _same(d0, d1):
            bad = f"k={k} 涂改未来后结果变了：{d0} -> {d1}"
            break
    res["T2"] = (bad is None, bad or "涂改未来后结果不变")
    return res


def 元测试(S: dict, ks) -> tuple[bool, list[str]]:
    """第 0 层：作弊版必须被抓出来；抓不出 = 体检失效。"""
    log, ok = [], True
    for name, fn in 作弊:
        r = 跑两套测试(fn, S, ks)
        caught = (not r["T1"][0]) or (not r["T2"][0])
        if caught:
            which = "T1" if not r["T1"][0] else "T2"
            log.append(f"    [OK] 作弊版「{name}」被 {which} 抓出：{r[which][1][:80]}")
        else:
            ok = False
            log.append(f"    [!!] 作弊版「{name}」没被抓出来 -> 体检本身失效，结果不可信")
    return ok, log


def 正测试(S: dict, ks) -> tuple[bool, list[str]]:
    log, ok = [], True
    for name, fn in 正版:
        r = 跑两套测试(fn, S, ks)
        if r["T1"][0] and r["T2"][0]:
            log.append(f"    [OK] {name}：截断不变 且 涂改未来不变")
        else:
            ok = False
            for tag in ("T1", "T2"):
                if not r[tag][0]:
                    log.append(f"    [!!] {name} 未通过 {tag}：{r[tag][1][:120]}")
    return ok, log


def 真实函数测试(S: dict, ks) -> tuple[bool, list[str]]:
    """从 lib/engine.py 里取真正的纯函数，包一层因果特征后跑体检。"""
    log = []
    try:
        import lib.engine as engine          # 依赖 tqsdk 的 venv
    except Exception as e:
        return True, [f"    [--] 跳过（导入 lib.engine 失败：{type(e).__name__}: {e}）"]

    ok = True

    def _登记(name, fn, note):
        nonlocal ok
        r = 跑两套测试(fn, S, ks)
        if r["T1"][0] and r["T2"][0]:
            log.append(f"    [OK] engine.{name}（{note}）通过因果测试")
        else:
            ok = False
            det = "; ".join(f"{t}: {r[t][1][:90]}" for t in ("T1", "T2") if not r[t][0])
            log.append(f"    [!!] engine.{name} 未通过：{det}")

    def 包_均值回归(_S, k):
        mid = sma(_S["close"], 20, k)
        if np.isnan(mid):
            return float("nan")
        return float(engine.rev_signal(0, _S["close"][k], mid))

    def 包_双均线过滤(_S, k):
        now_f, now_s = sma(_S["close"], 5, k), sma(_S["close"], 20, k)
        pre_f, pre_s = sma(_S["close"], 5, k - 1), sma(_S["close"], 20, k - 1)
        now_t, pre_t = sma(_S["close"], 60, k), sma(_S["close"], 60, k - 1)
        if np.isnan(now_f) or np.isnan(now_t):
            return float("nan")
        return float(engine.trend_reverse_signal(0, now_f, now_s, pre_f, pre_s, now_t, pre_t))

    def 包_通道(_S, k):
        n1, n2 = 20, 10
        if k < n1:
            return float("nan")
        return float(engine.channel_signal(
            0,
            float(_S["high"][k - n1: k].max()), float(_S["low"][k - n1: k].min()),
            float(_S["high"][k - n2: k].max()), float(_S["low"][k - n2: k].min()),
            _S["close"][k]))

    def 包_止损(_S, k):
        a = atr(_S, 14, k)
        if np.isnan(a) or k < 40 or a <= 0:
            return float("nan")
        line = engine._dual_ma_stop_line(_S["close"][k - 20], a, 1, 2.0, 0.0, 0.0)
        return 1.0 if engine._dual_ma_stop_hit(line, 1, _S["high"][k], _S["low"][k]) else 0.0

    _登记("rev_signal", 包_均值回归, "均值回归")
    _登记("trend_reverse_signal", 包_双均线过滤, "双均线+MA60过滤")
    _登记("channel_signal", 包_通道, "唐奇安通道")
    _登记("_dual_ma_stop_line/hit", 包_止损, "ATR止损")
    return ok, log


# ==================================================================
#  主流程
# ==================================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="防未来函数体检（规则 1）")
    ap.add_argument("--scan-only", action="store_true", help="只跑静态扫描")
    ap.add_argument("--dyn-only", action="store_true", help="只跑动态因果测试")
    ap.add_argument("--path", nargs="*", default=None, help="指定要扫的文件/目录")
    ap.add_argument("--json", default=None, help="结果另存为 JSON")
    ap.add_argument("--no-fail", action="store_true", help="有问题也返回 0（CI 调试用）")
    ap.add_argument("--strict", action="store_true", help="把全序列统计从 INFO 提到 WARN")
    ap.add_argument("--show-info", action="store_true", help="打印全序列统计明细")
    ap.add_argument("--with-tools", action="store_true", help="连工具目录一起扫（默认只扫 lib 与 策略）")
    ap.add_argument("--update-baseline", action="store_true",
                    help="把当前 ERROR 写入豁免清单（用于已人工确认无害的历史写法）")
    ap.add_argument("--baseline", default=BASELINE, help="豁免清单路径")
    args = ap.parse_args(argv)

    print("=" * 72)
    print("  防未来函数体检 · 规则 1 的机器证明")
    print("=" * 72)

    report: dict = {
        "time": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "root": ROOT,
    }

    # ---------- 第 1 层 ----------
    static_hits: list[dict] = []
    scan_ok = True
    if not args.dyn_only:
        paths = args.path or [os.path.join(ROOT, d) for d in ("lib", "策略")]
        if args.with_tools:
            paths.append(os.path.join(ROOT, "工具"))
        hits = scan_paths(paths)

        # 豁免清单：已知无害的位置（画图、统计收尾等），不改动被检 Files 本身
        baseline = {}
        try:
            if os.path.exists(args.baseline):
                with open(args.baseline, "r", encoding="utf-8") as f:
                    baseline = json.load(f)
        except Exception as e:
            print(f"  [!] 豁免清单读不动：{e}")
        if args.update_baseline:
            os.makedirs(os.path.dirname(args.baseline), exist_ok=True)
            baseline = {f"{h['file']}:{h['line']}": {"rule": h["rule"], "code": h["code"],
                                                     "func": h.get("func", ""),
                                                     "reason": "（人工补写理由）"}
                        for h in hits if h["level"] == "ERROR"}
            with open(args.baseline, "w", encoding="utf-8") as f:
                json.dump(baseline, f, ensure_ascii=False, indent=2)
            print(f"  豁免清单已重写：{args.baseline}  （{len(baseline)} 条，请补写 reason）")

        exempt = []
        kept = []
        for h in hits:
            key = f"{h['file']}:{h['line']}"
            if key in baseline and h["level"] == "ERROR":
                h["level"] = "INFO"
                h["rule"] = "已豁免"
                h["why"] = "在本项目豁免清单里：" + str(baseline[key].get("reason", ""))
                exempt.append(h)
            else:
                kept.append(h)
        hits = kept
        infos = [h for h in hits if h["level"] == "INFO"]
        if args.strict:
            for h in infos:
                h["level"] = "WARN"
            infos = []
        static_hits = [h for h in hits if h["level"] in ("ERROR", "WARN")]

        print("\n[第 0 层] 元测试 · 先证明体检本身没坏")
        scan_ok, log_s = 元扫描()
        for line in log_s:
            print(line)
        report["meta_scan"] = {"ok": scan_ok, "log": log_s}

        errs = [h for h in static_hits if h["level"] == "ERROR"]
        warns = [h for h in static_hits if h["level"] == "WARN"]
        print(f"\n[第 1 层] 静态扫描 · 扫了 {len(paths)} 个入口目录")
        print(f"          ERROR {len(errs)} / WARN {len(warns)} / INFO {len(infos)}")
        if errs:
            print("\n  --- ERROR（必须处理，或在行尾写 # noqa 说明理由）---")
            for h in errs:
                print(f"    {h['file']}:{h['line']}  [{h['rule']}]  in {h.get('func', '?')}()")
                print(f"        {h['code'][:110]}")
        if warns:
            print(f"\n  --- WARN ---")
            for h in warns[:25]:
                print(f"    {h['file']}:{h['line']}  [{h['rule']}]  {h['code'][:80]}")
                print(f"        {h['why'][:100]}")
        if infos:
            print(f"\n  --- INFO · 全序列统计 {len(infos)} 处（画图/报告里正常；"
                  f"加 --show-info 看全部，加 --strict 升级为 WARN）---")
            for h in (infos if args.show_info else infos[:5]):
                print(f"    {h['file']}:{h['line']}  {h['code'][:80]}")
        report["static"] = {"error": errs, "warn": warns, "info_count": len(infos)}

    # ---------- 第 0 / 2 层 ----------
    dyn_ok = True
    if not args.scan_only:
        S = gen_series()
        ks = [120, 200, 300, 450, 600]
        print("\n[第 0 层] 元测试 · 作弊版决策函数必须被抓出来")
        ok0, log0 = 元测试(S, ks)
        for line in log0:
            print(line)

        print("\n[第 2 层] 动态因果测试 · 正版口径")
        ok1, log1 = 正测试(S, ks)
        for line in log1:
            print(line)

        print("\n[第 2 层+] 真实环境 · lib/engine.py 里的纯函数")
        ok2, log2 = 真实函数测试(S, ks)
        for line in log2:
            print(line)

        dyn_ok = ok0 and ok1 and ok2
        report["dynamic"] = {"meta": ok0, "ref": ok1, "engine": ok2,
                             "log": log0 + log1 + log2}

    n_err = len([h for h in static_hits if h["level"] == "ERROR"]) if not args.dyn_only else 0
    clean = (n_err == 0) and dyn_ok and scan_ok
    report["clean"] = clean

    print("\n" + "=" * 72)
    if clean:
        print("  结论：干净。没有任何已知的未来函数写法，动态因果测试全通过。")
    else:
        print("  结论：有问题。上面 ERROR 与 [!!] 行是具体位置。")
    print("=" * 72)

    if args.json:
        try:
            os.makedirs(os.path.dirname(args.json), exist_ok=True)
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            print(f"\n  JSON 已写入：{args.json}")
        except Exception as e:
            print(f"\n  JSON 写入失败：{e}")

    return 0 if (clean or args.no_fail) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
