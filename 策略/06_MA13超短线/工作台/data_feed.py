# -*- coding: utf-8 -*-
"""
data_feed.py —— 选择数据层（卡 C 的数据来源）

职责：
  1. 把 50 池主连的「日线」拉到本地缓存（CSV），供两阶段选择算
     「20日平均成交量」与「近3日 |涨跌幅|」。
  2. load_cache / build_universe_daily：无网络、无 tqsdk 也能读缓存，
     保证 run_workbench --dry-run / 测试可离线演示。
  3. fetch_daily：lazy import tqsdk（仅实盘/回测环境需要），把数据落盘。

主连 symbol：tqsdk 格式 `KQ.m@<EXCH>.<KEY>`，EXCH 取自 universe 元数据。
  - 日线 K 线用 86400 秒；最后一根可能正在形成 ⇒ 收集时排除它（与回测一致性）。
  - 主连拼接会有换月跳空，volume 在换月附近可能异常；工程上先接受，
    「主连换月清洗」列为中优先级待办（见预注册文档 §8）。

不依赖 pandas：CSV 读写用纯标准库 csv，避免 dry-run 环境缺包。
"""
from __future__ import annotations
import csv
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, "data")
_SIM_DIR = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))  # 项目根（config_account 所在）

# 日线不需要很长；选阶段1=20日 + 阶段2=3日，多留缓冲
DEFAULT_DAYS = 40

# ---------------------------------------------------------------------------
# ★★ 未收盘 bar 剔除（2026-10-18 修，P0）
#
# 口径：tqsdk 日线 `datetime` 是 **UTC 纳秒**，且日线对齐的是**北京 00:00**。
#   ⇒ 缓存里 `2026-10-18 16:00` 实际 = 北京 `2026-10-19 00:00` = **周一那个交易日**。
#   （实测校验：label 序列 10-07/10-08/10-11/10-12/10-13/10-14/10-15/10-18
#     +1 天后正好是 10-08(四)/10-09(五)/10-12(一)/10-13/10-14/10-15/10-16(五)/10-19(一)
#     —— 完整跳过了周末，是「交易日 − 1」的 UTC 标签。）
#
# 旧写法 `_last.date() == now.date() and now.hour < 15` 有两个坑：
#   ① **夜里永远判 false**（hour=21 时 21<15 不成立）⇒ 周日晚拉数据会把「周一那根
#      只含周五夜盘 21:00–23:00 的未成形 bar」当成已收盘日线保留；
#   ② 用 UTC 日期跟本地日期比，跨日边界错 8 小时。
# 实证后果（2026-10-18 21:xx 实测 57 个缓存）：46/57 个文件末根是那根未收盘 bar，
#   把它剔除后两阶段选择**直接换了 2/6 条腿**（EB/EG → BU/SA）——违反铁律 1。
#
# 正解：先 +8h 换成北京时间，再判「交易日 > 今天」或「交易日 == 今天且未过 15:00」。
# ---------------------------------------------------------------------------
TZ_OFFSET_HOURS = 8
DAY_CLOSE_HM = (15, 0)          # 日线在北京时间 15:00 收盘
DAY_CLOSE_BUFFER_MIN = 5        # 收盘后留 5 分钟缓冲再认定「已收盘」


def _to_bj(ts):
    """UTC 时间戳 → 北京时间（日线标签语义）。"""
    import pandas as _pd
    return _pd.Timestamp(ts) + _pd.Timedelta(hours=TZ_OFFSET_HOURS)


def is_incomplete_bar(label, now=None) -> bool:
    """该日线 bar 是否**未收盘/未发生**（label 为缓存里的 UTC 时间字符串或 Timestamp）。

    未收盘 ⇒ 必须剔除，否则就是拿未完成 K 线做决策（铁律 1）。
    """
    import pandas as _pd
    now = _pd.Timestamp(now) if now is not None else _pd.Timestamp.now()
    bj = _to_bj(label)
    trade_day = bj.normalize()                      # 交易日（北京日期）
    today = now.normalize()
    if trade_day > today:
        return True                                # 尚未发生（如周日晚看到周一那根）
    if trade_day == today:
        close_at = trade_day + _pd.Timedelta(
            hours=DAY_CLOSE_HM[0], minutes=DAY_CLOSE_HM[1] + DAY_CLOSE_BUFFER_MIN)
        return now < close_at                      # 今天但还没收盘
    return False


def trim_incomplete(dts, now=None) -> int:
    """给定按时间升序的 datetime 序列，返回「可用的前 n 根」长度（尾部未收盘的剔掉）。

    容忍尾部有多根未收盘 bar（正常只有一根，但不假设）。
    """
    n = len(dts)
    while n >= 1:
        try:
            if not is_incomplete_bar(dts[n - 1], now=now):
                break
        except Exception:
            pass                                   # 判断不了就保守丢这一根
        n -= 1
    return n


def _main_code(key: str) -> str:
    """把短码映射成 tqsdk 主连 symbol。

    ★ 大小写按交易所区分（2026-10-17 修复，对照 config_symbols.py 实测口径）：
      SHFE/INE/DCE/GFEX 品种码小写（SHFE.rb / DCE.m / INE.sc / GFEX.si），
      CZCE 大写（CZCE.MA / CZCE.SR）。全大写直拼会在订阅时报
      「non-existent instrument」。
    """
    import universe as _u
    d = _u.get(key)
    if d is None:
        raise KeyError(f"品种 {key} 不在 universe 池内")
    exch = str(d["exch"]).upper()
    code_key = key.upper() if exch == "CZCE" else key.lower()
    return f"KQ.m@{exch}.{code_key}"


def _cache_path(key: str) -> str:
    return os.path.join(DATA_DIR, f"daily_{key.upper()}.csv")


def load_cache(key: str) -> dict | None:
    """读本地日线缓存。返回 {"datetime":[..], "close":[float], "volume":[float]}
    或 None（无缓存）。纯 csv，无 pandas 依赖。"""
    p = _cache_path(key)
    if not os.path.isfile(p):
        return None
    dt, cl, vol = [], [], []
    try:
        with open(p, "r", encoding="utf-8-sig", newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                try:
                    cl.append(float(row["close"]))
                    vol.append(float(row["volume"]))
                    dt.append(row.get("datetime", ""))
                except (ValueError, TypeError, KeyError):
                    continue
    except Exception:
        return None
    if not cl:
        return None
    return {"datetime": dt, "close": cl, "volume": vol}


def _resolve_auth(auth):
    if auth:
        return auth
    # 尝试从模拟盘目录的 config_account 取（实盘环境才有）
    if _SIM_DIR not in sys.path:
        sys.path.insert(0, _SIM_DIR)
    try:
        import config_account  # type: ignore
        return config_account.check()
    except Exception as e:
        raise RuntimeError(
            "未提供 auth=(user,pwd)，且无法从 config_account 取得账号。"
            "请在 tqsdk 实盘环境运行，或显式传入 auth。"
        ) from e


def fetch_daily(symbol_keys: list[str], days: int = DEFAULT_DAYS,
                auth=None) -> dict[str, int]:
    """拉主连日线并落盘 CSV。需 tqsdk 环境（lazy import）。返回 {key: 根数}。

    ★ 单一 TqApi 连接批量订阅全部品种（2026-10-17 改：原版每品种各开一次
    连接，57 品种 = 57 次鉴权，慢且易撞快期限流）。
    """
    import time as _time
    import pandas as pd
    from tqsdk import TqApi, TqAuth

    user, pwd = _resolve_auth(auth)
    codes: dict[str, str] = {}
    for key in symbol_keys:
        try:
            codes[key] = _main_code(key)
        except KeyError as e:
            print(f"  [skip] {key}: {e}")
    if not codes:
        return {}

    out: dict[str, int] = {}
    api = TqApi(auth=TqAuth(user, pwd))  # TqAuth 走 auth=（与 lib/engine.py 同款），不能当 account 传
    try:
        ks = {}
        for key, code in codes.items():
            print(f"[订阅] {key} {code}", flush=True)
            try:
                ks[key] = api.get_kline_serial(code, 86400, data_length=days + 10)
            except Exception as e:
                print(f"  [warn] {key} 订阅失败: {e}", flush=True)
        # 等到每条 kline 都有数据（datetime 末值非 0），总超时 60s
        pending = set(ks)
        deadline = _time.time() + 60
        while pending and _time.time() < deadline:
            api.wait_update(deadline=min(_time.time() + 5, deadline))
            for key in list(pending):
                try:
                    if int(ks[key]["datetime"].iloc[-1]) > 0:
                        pending.discard(key)
                except Exception:
                    pass
        for key in list(pending):
            print(f"  [warn] {key} 等待数据超时（60s），跳过")
        os.makedirs(DATA_DIR, exist_ok=True)
        for key, k in ks.items():
            try:
                dts = k["datetime"].to_numpy()
                cl = k["close"].to_numpy(float)
                vo = k["volume"].to_numpy(float)
                # ★★ 2026-10-18 修（P0）：剔除「未收盘/未发生」的尾部 bar。
                #   判断逻辑集中在 trim_incomplete()（+8h 转北京时间后判交易日），
                #   不再用 `now.hour < 15`——那在夜盘/周末会把未成形 bar 当已收盘。
                n = trim_incomplete(dts)
                rows = [(int(dts[j]), float(cl[j]), float(vo[j])) for j in range(n)]
            except Exception as e:
                print(f"  [warn] {key} 读取失败: {e}")
                continue
            if not rows:
                print(f"  [warn] {key} 无数据")
                continue
            with open(_cache_path(key), "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["datetime", "close", "volume"])
                for ns, c, v in rows:
                    w.writerow([pd.to_datetime(ns).strftime("%Y-%m-%d %H:%M:%S"), c, v])
            out[key] = len(rows)
            print(f"  [ok] {key} {len(rows)} 根 -> {_cache_path(key)}", flush=True)
    finally:
        try:
            api.close()
        except Exception:
            pass
    return out


def repair_caches(now=None, dry_run: bool = False) -> list[tuple]:
    """★ 离线修复：把各 daily_*.csv 末尾「未收盘/未发生」的 bar 剪掉。

    纯本地文件操作，**不发任何网络请求**（不占 tqsdk 连接，不影响正在跑的模拟盘）。
    返回 [(key, 原行数, 新行数)]，只列有改动的。

    存在理由：fetch_daily 的判定 bug 已经产出了 46 个带未收盘 bar 的缓存，
    改代码不会改历史数据 ⇒ 需要一次性就地修剪，否则选择层继续吃脏数据。
    """
    import pandas as _pd
    fixed = []
    if not os.path.isdir(DATA_DIR):
        return fixed
    for fn in sorted(os.listdir(DATA_DIR)):
        if not (fn.startswith("daily_") and fn.endswith(".csv")):
            continue
        key = fn[len("daily_"):-len(".csv")]
        p = os.path.join(DATA_DIR, fn)
        try:
            with open(p, "r", encoding="utf-8-sig", newline="") as f:
                rd = csv.DictReader(f)
                rows = list(rd)
        except Exception:
            continue
        if not rows:
            continue
        n0 = len(rows)
        n = trim_incomplete([r.get("datetime", "") for r in rows], now=now)
        if n == n0:
            continue
        fixed.append((key, n0, n))
        if dry_run:
            continue
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["datetime", "close", "volume"])
            for r in rows[:n]:
                w.writerow([r.get("datetime", ""), r.get("close", ""), r.get("volume", "")])
    return fixed


def build_universe_daily(symbol_keys: list[str], days: int = DEFAULT_DAYS,
                         use_cache: bool = True, fetch: bool = False,
                         auth=None) -> dict[str, dict]:
    """构建 {key: {"close":[..], "volume":[..]}} 供 select_two_stage 用。

    - use_cache=True 且缓存存在 → 直接读（离线可用）。
    - fetch=True → 真拉（需 tqsdk + 账号 + 连网），并写缓存供下次读。
    缺数据的品种被跳过（不在返回里）。
    """
    result: dict[str, dict] = {}
    to_fetch = []
    for key in symbol_keys:
        d = load_cache(key) if use_cache else None
        if d is not None and len(d["close"]) >= 5:
            # ★★ 双保险（2026-10-18）：load_cache 不做任何清洗，缓存里若混进
            #   「未收盘/未发生」的尾部 bar，选择层就会拿未完成 K 线做决策（铁律 1）。
            #   fetch_daily / --repair 已在写盘时剪过，这里再按同一口径剪一次，
            #   保证"任何来源的缓存"都干净（手工改过、旧脚本写过的也不例外）。
            n_ok = trim_incomplete(d.get("datetime") or [])
            if n_ok < len(d["close"]):
                d = {"datetime": d["datetime"][:n_ok],
                     "close": d["close"][:n_ok],
                     "volume": d["volume"][:n_ok]}
            if len(d["close"]) >= 5:
                result[key] = d
        elif fetch:
            to_fetch.append(key)
        else:
            # 无缓存也没开 fetch：跳过（不报错，dry-run 只演示有缓存的部分）
            pass
    if to_fetch:
        fetch_daily(to_fetch, days=days, auth=auth)
        for key in to_fetch:
            d = load_cache(key)
            if d is not None:
                result[key] = d
    return result


if __name__ == "__main__":
    # 命令行：python data_feed.py RB MA EG [--fetch] [--days 40]
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("keys", nargs="*", help="品种短码，缺省=全池")
    ap.add_argument("--fetch", action="store_true", help="真拉（需 tqsdk+账号）")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--repair", action="store_true",
                    help="★ 离线剪掉缓存末尾「未收盘/未发生」的日线 bar（不联网）")
    ap.add_argument("--repair-dry", action="store_true",
                    help="只报告 --repair 会改哪些文件，不写盘")
    a = ap.parse_args()
    if a.repair or a.repair_dry:
        fixed = repair_caches(dry_run=a.repair_dry)
        if not fixed:
            print("所有缓存末尾都是已收盘 bar，无需修复。")
        else:
            print(f"{'[预演] 将修复' if a.repair_dry else '已修复'} {len(fixed)} 个缓存：")
            for key, n0, n in fixed:
                print(f"  {key:<4} {n0} → {n} 根（剪掉 {n0 - n} 根未收盘 bar）")
    else:
        import universe as _u
        keys = [k.upper() for k in a.keys] or _u.all_keys()
        if a.fetch:
            fetch_daily(keys, days=a.days)
        else:
            print("（dry-run）使用本地缓存；加 --fetch 真拉。")
        d = build_universe_daily(keys, fetch=a.fetch, days=a.days)
        print(f"可用日线：{len(d)} / {len(keys)} 个品种")
