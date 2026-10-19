# -*- coding: utf-8 -*-
"""
live.py —— 阶段 6 · 实时盯盘（独立进程，5 秒一轮）
================================================================
只做两件事，其余一律不做：
  ① 价格穿越 MA13 → 报警
  ② 心跳（K 线）超过 2 个周期未更新 → 报警
⇒ **只报警，不做实时图**（§3 D4：人盯不住 4 小时；Streamlit 图表刷新是整图重画会闪）。
   这个进程的真正价值是**为明天积累数据**。

红线：
  ★ D7：只用 `TqSim`，**绝不 `TqKq` 会话**（云端快期账户被 5 腿模拟盘占着，抢会踢下线）。
  ★ §7/D5：本文件 **不 import tqsdk**，实时会话一律走 `data.open_live_sim()`（唯一连网层）。
  ★ §6·禁未来：只用**已收盘** bar —— kline_serial 最后一根是"进行中"bar，一律取 `-2`。
  ★ §4 阶段 6 验收：断网恢复后**不重复报警**（去重靠时间戳窗口）。

运行（★ 必须用策略 venv；复盘 venv `.venv_viz` 按规定不装 tqsdk）：
  & "$env:USERPROFILE\\.workbuddy\\binaries\\python\\envs\\tqsdk\\Scripts\\python.exe" `
      工具\\复盘\\live.py --symbols MA,M,JM,JD,SR

自检（**无需网络 / 无需 tqsdk**，用合成行情驱动状态机；可在 `.venv_viz` 里跑）：
  .venv_viz\\Scripts\\python.exe 工具\\复盘\\live.py --selftest
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import data          # ★ 唯一连网层（tqsdk 只在 data.py 里 import）
import features      # MA13 口径与策略同源（§5·3）
import sessions      # 时段口径单一真相源（夜盘归属日 / 休市闸门）

INTERVAL_SEC = 5                      # 每轮 5 秒（§4 阶段 6）
KLINE_SEC = 300                       # 订阅 5min，与 06 策略 KSEC 对齐
HEARTBEAT_LIMIT_SEC = 2 * KLINE_SEC   # ★ 心跳超 2 个周期未更新 ⇒ 报警
DEDUP_WINDOW_SEC = 1800               # ★ 去重窗口：同 (品种, 类型) 内不重复响
KLINE_LEN = 200                       # 实时上限 ≈8964（§6·17），200 根足够 MA13
RECONNECT_WAIT_SEC = 10               # 重连前歇一口气，避免撞限流

DB_PATH = data.LIVE_DB
LOG_PATH = data.CACHE_DIR / "live_alarms.log"


# ---------------------------------------------------------------------------
# 品种元数据：只读根目录 config_symbols.py（不复制一份配置，也不修改它，§8 红线 1）
# ---------------------------------------------------------------------------
def _load_symbol_meta() -> dict[str, dict]:
    """{短码: {"name": 中文名, "cont": 主连代码}}，来源 `config_symbols.SYMBOLS`。"""
    import importlib.util
    cfg = data.ROOT / "config_symbols.py"
    spec = importlib.util.spec_from_file_location("_cfg_symbols", cfg)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return {k: {"name": v[0], "cont": v[1]} for k, v in m.SYMBOLS.items()}


# ---------------------------------------------------------------------------
# ★ 纯状态机：只判断「此刻该不该报警」，不碰 tqsdk、不碰 OS 告警
#   ⇒ 这样才能被合成行情驱动，`--selftest` 不需要网络
# ---------------------------------------------------------------------------
class Monitor:
    UP, DOWN, FLAT = 1, -1, 0

    def __init__(self, keys, heartbeat_limit: float = HEARTBEAT_LIMIT_SEC,
                 dedup: float = DEDUP_WINDOW_SEC):
        self.keys = list(keys)
        self.hb_limit = float(heartbeat_limit)
        self.dedup = float(dedup)
        self.last_bar_ts: dict[str, float] = {}     # 最后一根已收盘 bar 的时刻（秒）
        self.last_sign: dict[str, int] = {}          # 上次 (现价 − MA13) 的符号
        self.last_alarm: dict[tuple, float] = {}     # (类型, 品种) → 上次报警时刻
        self.stale: set[str] = set()                 # 已因心跳报警、尚未恢复

    def _armed(self, kind: str, key: str, now: float) -> bool:
        """去重闸门：同 (类型, 品种) 在窗口内只放行一次（§4 阶段 6 验收）。"""
        prev = self.last_alarm.get((kind, key))
        if prev is not None and (now - prev) < self.dedup:
            return False
        self.last_alarm[(kind, key)] = now
        return True

    def on_bar(self, key: str, bar_ts: float, ma13, price, now: float) -> list[tuple]:
        """喂一根**已收盘** bar 的 MA13 + 当前最新价；返回本轮要报的警。"""
        self.last_bar_ts[key] = float(bar_ts)        # 心跳看的是"这根 bar 有没有在动"
        out: list[tuple] = []
        if ma13 is None or price is None:
            return out
        if not (np.isfinite(ma13) and np.isfinite(price)):
            return out
        sign = self.UP if price > ma13 else (self.DOWN if price < ma13 else self.FLAT)
        prev = self.last_sign.get(key)
        self.last_sign[key] = sign
        if sign != self.FLAT and prev not in (None, self.FLAT) and sign != prev:
            if self._armed("穿越MA13", key, now):
                d = "向上" if sign == self.UP else "向下"
                out.append(("穿越MA13", key, f"{d}穿越 MA13={ma13:.2f}（现价 {price:.2f}）"))
        return out

    def check_heartbeat(self, now: float) -> list[tuple]:
        """心跳：最后一根已收盘 bar 距今超过阈值 ⇒ 报一次；恢复后**重新武装**。

        注意：调用方须先过「时段闸门」（休市不查，见 run()），否则整夜会误报。
        """
        out: list[tuple] = []
        for k in self.keys:
            ts = self.last_bar_ts.get(k)
            if ts is None:
                continue
            age = now - ts
            if age > self.hb_limit:
                if k not in self.stale and self._armed("心跳停滞", k, now):
                    self.stale.add(k)
                    out.append(("心跳停滞", k, f"K线 {age:.0f}s 未更新（阈值 {self.hb_limit:.0f}s）"))
            else:
                self.stale.discard(k)                # 恢复 ⇒ 重新武装
        return out

    def rearm_all(self, now: float):
        """进入新交易时段时重置心跳基线并清掉方向基准。

        为什么必须重置：休市可能长达十几小时，若沿用休市前那根 bar 的时刻，
        开盘第一轮就会立刻撞上"心跳超时"而误报；跨时段方向也已不延续。
        """
        for k in self.keys:
            self.last_bar_ts[k] = float(now)
            self.stale.discard(k)
        self.last_sign.clear()


# ---------------------------------------------------------------------------
# 报警：响铃 + 日志 + 弹窗（独立线程，绝不阻塞主循环）+ 落库
# ---------------------------------------------------------------------------
def _beep():
    try:
        import winsound
        winsound.Beep(880, 220)
    except Exception:
        print("\a", end="", flush=True)              # 无声卡/非 Windows 时至少响铃


def _popup(title: str, msg: str):
    """Windows 原生弹窗。范式取自 `ma13_live_multi.py:157` 的 `_popup`。"""
    def _go():
        try:
            import ctypes
            # 48=MB_ICONWARNING, 0x40000=MB_SYSTEMMODAL 置顶
            ctypes.windll.user32.MessageBoxW(0, msg, title, 48 | 0x40000)
        except Exception:
            pass                                     # 弹不出来不能影响盯盘
    threading.Thread(target=_go, daemon=True, name="popup").start()


def _alert(kind: str, key: str, detail: str, now: float, db=None,
           popup: bool = True, beep: bool = True, log: Path | None = None):
    ts = pd.Timestamp(now, unit="s").strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {key} | {kind} | {detail}"
    log = LOG_PATH if log is None else log
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    if db is not None:
        db.execute("INSERT INTO alarm(ts,key,kind,detail) VALUES(?,?,?,?)", (ts, key, kind, detail))
        db.commit()
    if beep:
        _beep()
    if popup:
        _popup(f"复盘盯盘 · {kind}", f"[{key}] {detail}")
    print(f"  ★ 报警 {line}", flush=True)


# ---------------------------------------------------------------------------
# SQLite（§4 阶段 6：`_缓存/live.db`，PRAGMA journal_mode=WAL）
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tick(
  ts TEXT NOT NULL, key TEXT NOT NULL, last_price REAL, ma13 REAL, seg TEXT, stale_sec REAL);
CREATE TABLE IF NOT EXISTS alarm(
  ts TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_tick_ts  ON tick(ts);
CREATE INDEX IF NOT EXISTS ix_alarm_ts ON alarm(ts);
"""


def _db_open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_SCHEMA)
    con.commit()
    return con


# ---------------------------------------------------------------------------
# 主循环（唯一需要 tqsdk 的部分，通过 data.open_live_sim 间接使用）
# ---------------------------------------------------------------------------
def _round(sess, mon: Monitor, keys, now: float, seg: str, db):
    """跑一轮：算 MA13 → 判穿越 → 判心跳 → 落 tick。返回本轮报警列表。"""
    alarms: list[tuple] = []
    tick_rows = []
    for s in sess:
        k = s["key"]
        kdf = pd.DataFrame(s["kline"])
        if len(kdf) < 20:
            continue
        ma_s = features.ma(kdf)                      # MA13，口径同 signals.py（§5·3）
        ma13 = float(ma_s.iloc[-2])                  # ★ -2 = 最后一根**已收盘** bar
        bar_ts = float(kdf["datetime"].iloc[-2]) / data.NS   # ★ §6·10 纳秒 → 秒
        price = float(s["quote"].last_price)
        if np.isnan(price):
            price = None
        alarms += mon.on_bar(k, bar_ts, ma13, price, now)
        prev_ts = mon.last_bar_ts.get(k)
        tick_rows.append((pd.Timestamp(now, unit="s").strftime("%Y-%m-%d %H:%M:%S"),
                          k, price, ma13, seg, (now - prev_ts) if prev_ts else None))
    if seg != sessions.CLOSED:
        alarms += mon.check_heartbeat(now)
    if db is not None and tick_rows:
        db.executemany(
            "INSERT INTO tick(ts,key,last_price,ma13,seg,stale_sec) VALUES(?,?,?,?,?,?)", tick_rows)
        db.commit()
    return alarms


def run(keys, meta, period_sec: int = KLINE_SEC, interval: float = INTERVAL_SEC,
        popup: bool = True, beep: bool = True, max_rounds: int | None = None) -> int:
    specs = [{"key": k, "code": meta[k]["cont"]} for k in keys]
    mon = Monitor(keys)
    db = _db_open(DB_PATH)
    prev_seg: str | None = None
    rounds = 0
    attempts = 0                    # ★ 按"尝试次数"封顶：连接一直失败时也不会死循环
    print(f"复盘盯盘（live.py）｜间隔 {interval:g}s｜5min K线｜库 {DB_PATH}", flush=True)
    names = "、".join(f"{k}({meta[k]['name']})" for k in keys)
    print(f"  订阅：{names}", flush=True)
    while max_rounds is None or attempts < max_rounds:
        attempts += 1
        api = None
        try:
            api, sess = data.open_live_sim(specs, period_sec, data_length=KLINE_LEN)
            print(f"  已连接（TqSim 会话），{len(sess)} 个合约订阅就绪", flush=True)
        except Exception as e:
            print(f"  连接失败：{type(e).__name__}: {e}；{RECONNECT_WAIT_SEC}s 后重试", flush=True)
            time.sleep(RECONNECT_WAIT_SEC)
            continue
        try:
            while max_rounds is None or rounds < max_rounds:
                api.wait_update(deadline=time.time() + interval)   # 到期即返回 ⇒ 5s 节拍
                now = time.time()
                seg = sessions.seg_of_bj(pd.Timestamp.now())       # 本机在 Asia/Shanghai
                if prev_seg == sessions.CLOSED and seg != sessions.CLOSED:
                    mon.rearm_all(now)                             # 开盘重置心跳基线
                prev_seg = seg
                for kind, k, detail in _round(sess, mon, keys, now, seg, db):
                    _alert(kind, k, detail, now, db=db, popup=popup, beep=beep)
                rounds += 1
        except Exception as e:
            print(f"  运行异常：{type(e).__name__}: {e}；断开重连", flush=True)
        finally:
            if api is not None:
                try:
                    api.close()
                except Exception:
                    pass
        if max_rounds is not None and rounds >= max_rounds:
            break
        time.sleep(RECONNECT_WAIT_SEC)                             # 断线后歇一会再重连
    db.close()
    return 0


# ---------------------------------------------------------------------------
# 自检（不依赖网络 / tqsdk）
# ---------------------------------------------------------------------------
def _selftest() -> int:
    ok = True
    t0 = 1_700_000_000.0

    # ① 穿越：建基准不报 → 反向报 1 次 → 同向继续不报 → 窗口内反向被去重 → 窗口外可再报
    m = Monitor(["MA"], heartbeat_limit=600, dedup=1800)
    m.on_bar("MA", t0, 100.0, 101.0, t0)
    a = m.on_bar("MA", t0 + 300, 100.0, 99.0, t0 + 300)
    b = m.on_bar("MA", t0 + 600, 100.0, 99.5, t0 + 600)
    c = m.on_bar("MA", t0 + 900, 100.0, 101.0, t0 + 900)
    d = m.on_bar("MA", t0 + 10800, 100.0, 99.0, t0 + 10800)
    t1 = (len(a) == 1 and not b and not c and len(d) == 1)
    print(f"[① 穿越MA13] 首次={len(a)} 同向={len(b)} 窗口内反向={len(c)} 窗口外={len(d)} ⇒ {t1}")
    ok &= t1

    # ② 心跳：超阈值报 1 次 → 仍停不重复 → 恢复归零 → 去重窗内再停**仍不报** → 窗外出窗才再报
    m2 = Monitor(["M"], heartbeat_limit=600, dedup=1800)
    m2.on_bar("M", t0, 1.0, 1.0, t0)
    h1 = m2.check_heartbeat(t0 + 601)        # 首报
    h2 = m2.check_heartbeat(t0 + 1200)       # 仍停 ⇒ 不重复
    m2.on_bar("M", t0 + 1201, 1.0, 1.0, t0 + 1201)   # 恢复
    h3 = m2.check_heartbeat(t0 + 1210)       # 恢复 ⇒ 0
    h5 = m2.check_heartbeat(t0 + 1900)       # 再次停，但仍在去重窗内 ⇒ 0（§4：不重复报警）
    h4 = m2.check_heartbeat(t0 + 3000)       # 距上次报警 2399s > 1800 ⇒ 出窗再报 1 次
    t2 = (len(h1) == 1 and not h2 and not h3 and not h5 and len(h4) == 1)
    print(f"[② 心跳阈值] 首报={len(h1)} 续停={len(h2)} 恢复={len(h3)} "
          f"窗内再停={len(h5)} 出窗再停={len(h4)} ⇒ {t2}")
    ok &= t2

    # ③ 验收：断流 30 秒 → 恢复，全程**一条都不报**（30s < 600s 阈值）
    m3 = Monitor(["JD"], heartbeat_limit=600, dedup=1800)
    m3.on_bar("JD", t0, 1.0, 1.0, t0)
    got: list[tuple] = []
    for s in range(0, 31, 5):
        got += m3.check_heartbeat(t0 + s)
    m3.on_bar("JD", t0 + 35, 1.0, 1.0, t0 + 35)
    got += m3.check_heartbeat(t0 + 35)
    t3 = (got == [])
    print(f"[③ 断网 30s 恢复] 报警条数={len(got)}（应为 0，且未重复）⇒ {t3}")
    ok &= t3

    # ④ 跨时段：休市 12h 后进入新时段 → 不应拿休市前旧 bar 误报心跳
    m4 = Monitor(["SR"], heartbeat_limit=600, dedup=1800)
    m4.on_bar("SR", t0, 1.0, 1.0, t0)
    m4.rearm_all(t0 + 12 * 3600)
    t4 = (m4.check_heartbeat(t0 + 12 * 3600 + 60) == [])
    print(f"[④ 跨时段重置] 休市12h后首轮报警={0 if t4 else 1}（应 0）⇒ {t4}")
    ok &= t4

    # ⑤ 落库：WAL 生效 + 表可用 + 回读
    tmp = Path(tempfile.mkdtemp()) / "live.db"
    con = _db_open(tmp)
    mode = con.execute("PRAGMA journal_mode").fetchone()[0].lower()
    _alert("穿越MA13", "MA", "向下穿越 MA13=100.00（现价 99.00）", t0,
           db=con, popup=False, beep=False, log=tmp.parent / "a.log")
    n = con.execute("SELECT COUNT(*) FROM alarm").fetchone()[0]
    row = con.execute("SELECT key,kind FROM alarm").fetchone()
    con.close()
    t5 = (mode == "wal" and n == 1 and row == ("MA", "穿越MA13"))
    print(f"[⑤ live.db] journal_mode={mode} alarm行数={n} 首行={row} ⇒ {t5}")
    ok &= t5

    # ⑥ 红线：源码里**不得存在** `TqKq` 会话的调用（注释里提到不算）；
    #    live.py 自身不得 import tqsdk；data.py 必须用 TqSim。
    _tqkq = re.compile(r"TqKq\s*\(")
    _imp = re.compile(r"^\s*(?:import|from)\s+tqsdk", re.M)
    live_src = Path(__file__).read_text(encoding="utf-8")
    data_src = Path(data.__file__).read_text(encoding="utf-8")
    t6a = _tqkq.search(live_src) is None and _tqkq.search(data_src) is None
    t6b = _imp.search(live_src) is None
    t6c = "TqSim(" in data_src
    t6 = t6a and t6b and t6c
    print(f"[⑥ 红线] 无 TqKq 会话调用={t6a}；live.py 不 import tqsdk={t6b}；data.py 用 TqSim={t6c} ⇒ {t6}")
    ok &= t6

    print(f"\n阶段 6 自检：{'全部通过 ✅' if ok else '存在失败 ❌'}")
    return 0 if ok else 1


def _main() -> int:
    ap = argparse.ArgumentParser(description="阶段 6 · 实时盯盘（只报警，不做实时图）")
    ap.add_argument("--symbols", default=None, help="逗号分隔品种短码；缺省取 config_symbols 前 5 个")
    ap.add_argument("--period", type=int, default=KLINE_SEC, help="K 线秒数（默认 300 = 5min）")
    ap.add_argument("--interval", type=float, default=INTERVAL_SEC, help="每轮间隔秒（默认 5）")
    ap.add_argument("--max-rounds", type=int, default=None, help="跑够 N 轮就退出（调试用）")
    ap.add_argument("--no-popup", action="store_true", help="不弹窗（只响铃 + 写日志/库）")
    ap.add_argument("--no-beep", action="store_true", help="不响铃")
    ap.add_argument("--selftest", action="store_true", help="离线自检（不需要网络/tqsdk）")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()

    meta = _load_symbol_meta()
    if a.symbols:
        keys = [k.strip().upper() for k in a.symbols.split(",") if k.strip()]
    else:
        keys = list(meta)[:5]                        # ★ 不静默用隐藏默认值，启动时打印出来
    bad = [k for k in keys if k not in meta]
    if bad:
        raise SystemExit(f"品种短码不在 config_symbols 里：{bad}\n可用：{', '.join(meta)}")
    return run(keys, meta, period_sec=a.period, interval=a.interval,
               popup=not a.no_popup, beep=not a.no_beep, max_rounds=a.max_rounds)


if __name__ == "__main__":
    raise SystemExit(_main())
