# -*- coding: utf-8 -*-
"""
CTP tick 录制器 —— 把 CTP 行情快照落成 Parquet 分区文件（可增量、可断线重连）
================================================================================
为什么是 Parquet 而不是数据库：
  · CTP ≈ **2 笔快照/秒** ⇒ 单合约 ≈ 4.1 万条/日 ≈ **10 MB/日**（CSV）≈ 2.6 GB/年；
    100 合约 ≈ 259 GB/年（CSV）⇒ 行存/云数据库（Supabase/Postgres）完全不适合。
  · Parquet 列式 + zstd 通常压到 1/5~1/10；且 **DuckDB 可以直接 SQL 查 parquet**，
    不用导入、不用起服务 ⇒ 最省事。

落盘结构（Hive 风格分区，DuckDB 原生识别）：
    tick/date=2026-10-12/sym=JM/part-20261012-234500.parquet
  ⇒ `SELECT * FROM read_parquet('tick/**/*.parquet', hive_partitioning=1)`

★ **落盘批大小很重要（实测教训）**：parquet 每个文件都有固定的元数据/footer 开销，
  行数少时开销压倒数据。实测 `--flush 40`（每品种每文件约 55 行）⇒ **348 字节/条，
  比未压缩 CSV 还大**。把 `--flush` 提到 300~600 秒（每文件数百~上千行）才划算。
  ⇒ 默认 `--flush 300`。**别为了"实时性"把 flush 调小**，tick 落盘不需要秒级可见。

⚠️ 安全：**只连 SimNow**（沿用 simnow_最小接入.py 的前置地址白名单），且**只订阅行情、无任何下单代码**。

⚠️ 两个必须记住的 CTP 细节：
  1. **空价 = DBL_MAX**（1.797e308）而不是 0/NaN。不处理就会把「无买一」写成天文数字，
     污染所有分位数统计。本脚本统一转成 NaN。
  2. 日期要用 **ActionDay（自然日）** 拼时间戳，不是 TradingDay（交易日）——
     夜盘 23:00 的行情，TradingDay 已经是"明天"，拼出来会错位一天。

用法
----
  python tick录制.py --minutes 5                      # 录 5 分钟（SimNow 724 环境，自动生成合约）
  python tick录制.py --env 1 --minutes 10 --inst rb2701,m2701
  python tick录制.py --minutes 0                      # 一直录（Ctrl+C 结束）
  python tick查询.py                                  # 用 DuckDB 查录到的数据
"""
import os
import sys
import time
import signal
import tempfile
import datetime
import threading
import argparse

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from openctp_ctp import thostmduserapi as mdapi          # noqa: E402

# 复用「最小接入」里的白名单与候选合约生成逻辑，避免两处口径漂移
import importlib.util                                    # noqa: E402
_spec = importlib.util.spec_from_file_location("_base", os.path.join(_HERE, "simnow_最小接入.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

SIMNOW_FRONTS = _base.SIMNOW_FRONTS
ALLOWED_HOSTS = _base.ALLOWED_HOSTS
_default_insts = _base._default_insts

FLOW_DIR = os.environ.get("CTP_FLOW_DIR") or os.path.join(tempfile.gettempdir(), "ctp_flow_tqsdk")
OUT_DIR = os.path.join(_HERE, "tick")
STATE_FILE = os.path.join(_HERE, "tick录制_状态.txt")

# ---- 落盘字段（只留有用的；CTP 原始结构有 50+ 字段）----
FIELDS = ["ts", "trading_day", "sym", "exchange", "last", "pre_settle", "open", "high", "low",
          "upper_limit", "lower_limit", "volume", "turnover", "open_interest", "avg_price"]
for i in range(1, 6):
    FIELDS += [f"bid{i}", f"bidv{i}", f"ask{i}", f"askv{i}"]
SCHEMA = pa.schema(
    [("ts", pa.timestamp("ms")), ("trading_day", pa.string()),
     ("sym", pa.string()), ("exchange", pa.string())]
    + [(f, pa.float64()) for f in FIELDS if f not in ("ts", "trading_day", "sym", "exchange")]
)


def _f(v):
    """CTP 的"空值"是 DBL_MAX（≈1.797e308），必须转 NaN，否则污染统计。"""
    try:
        x = float(v)
    except Exception:
        return float("nan")
    if x != x or abs(x) > 1e100:
        return float("nan")
    return x


def _i(v):
    try:
        return float(int(v))
    except Exception:
        return float("nan")


class Recorder(mdapi.CThostFtdcMdSpi):
    def __init__(self, api, cfg, instruments, flush_sec, log_every=30):
        mdapi.CThostFtdcMdSpi.__init__(self)
        self.api = api
        self.cfg = cfg
        self.instruments = instruments
        self.sess_dir = {}
        self.flush_sec = flush_sec
        self.log_every = log_every

        self.buf = []                  # [(ts, trading_day, sym, exchange, ...)]
        self.n_total = 0
        self.n_file = 0
        self.n_bytes = 0
        self.sub_ok = 0
        self.last_tick_t = time.time()
        self.last_flush = time.time()
        self.last_log = time.time()
        self.lock = threading.Lock()
        self.stop = False

    # ---------------- CTP 回调 ----------------
    def OnFrontConnected(self):
        self._log("前置已连接 ⇒ 登录")
        f = mdapi.CThostFtdcReqUserLoginField()
        f.BrokerID = self.cfg["broker"]; f.UserID = self.cfg["user"]; f.Password = self.cfg["pwd"]
        self.api.ReqUserLogin(f, 1)

    def OnFrontDisconnected(self, nReason):
        self._log(f"⚠️ 前置断开（原因码 {nReason}）⇒ 5 秒后重连")
        threading.Thread(target=self._reconnect, daemon=True).start()

    def _reconnect(self):
        time.sleep(5)
        try:
            self.api.Init()            # 重新 Init 会再次触发 OnFrontConnected ⇒ 重新登录+订阅
            self._log("已触发重连 Init()")
        except Exception as e:
            self._log(f"重连失败：{type(e).__name__}: {e}")

    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
        eid = int(getattr(pRspInfo, "ErrorID", -1) or 0) if pRspInfo else -1
        if eid != 0:
            self._log(f"登录失败 ErrorID={eid} {getattr(pRspInfo,'ErrorMsg','')}")
            return
        self._log("登录成功 ⇒ 订阅")
        insts = [s.encode("ascii", "ignore") for s in self.instruments]
        self.api.SubscribeMarketData(insts, len(insts))

    def OnRspSubMarketData(self, pSpecificInstrument, pRspInfo, nRequestID, bIsLast):
        if pRspInfo and int(getattr(pRspInfo, "ErrorID", -1) or 0) == 0:
            self.sub_ok += 1

    def OnRtnDepthMarketData(self, d):
        if d is None:
            return
        try:
            sym = str(d.InstrumentID)
            tday = str(getattr(d, "TradingDay", "") or "")
            aday = str(getattr(d, "ActionDay", "") or "")
            ut = str(getattr(d, "UpdateTime", "") or "")
            ums = int(getattr(d, "UpdateMillisec", 0) or 0)
            # ⚠️ 用 ActionDay（自然日），不用 TradingDay —— 夜盘会错位一天
            day = aday if len(aday) == 8 else tday
            ts = pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} {ut}").value // 10**6 + ums

            row = [ts, tday, sym, str(getattr(d, "ExchangeID", "") or ""),
                   _f(d.LastPrice), _f(getattr(d, "PreSettlementPrice", 0)), _f(d.OpenPrice),
                   _f(d.HighestPrice), _f(d.LowestPrice),
                   _f(getattr(d, "UpperLimitPrice", 0)), _f(getattr(d, "LowerLimitPrice", 0)),
                   _i(d.Volume), _f(d.Turnover), _i(d.OpenInterest), _f(d.AveragePrice)]
            for i in range(1, 6):
                row += [_f(getattr(d, f"BidPrice{i}", 0)), _i(getattr(d, f"BidVolume{i}", 0)),
                        _f(getattr(d, f"AskPrice{i}", 0)), _i(getattr(d, f"AskVolume{i}", 0))]
            with self.lock:
                self.buf.append(row)
                self.n_total += 1
            self.last_tick_t = time.time()
        except Exception as e:
            self._log(f"解析行情异常：{type(e).__name__}: {e}")

    # ---------------- 落盘 ----------------
    def flush(self, force=False):
        with self.lock:
            if not self.buf:
                return
            if not force and time.time() - self.last_flush < self.flush_sec:
                return
            rows = self.buf
            self.buf = []
            self.last_flush = time.time()
        df = pd.DataFrame(rows, columns=FIELDS)
        t0 = pd.Timestamp(df["ts"].min(), unit="ms")
        day = t0.strftime("%Y-%m-%d")
        # 按品种切文件（一次 flush 可能含多个品种）
        for sym, g in df.groupby("sym", sort=True):
            d = os.path.join(OUT_DIR, f"date={day}", f"sym={sym}")
            os.makedirs(d, exist_ok=True)
            fp = os.path.join(d, f"part-{t0.strftime('%Y%m%d-%H%M%S')}-{self.n_file:05d}.parquet")
            tab = pa.Table.from_pandas(g[FIELDS], schema=SCHEMA, preserve_index=False)
            pq.write_table(tab, fp, compression="zstd")
            self.n_file += 1
            try:
                self.n_bytes += os.path.getsize(fp)
            except OSError:
                pass

    def heartbeat(self):
        now = time.time()
        idle = now - self.last_tick_t
        msg = (f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  "
               f"条数={self.n_total}  文件={self.n_file}  "
               f"体积={self.n_bytes/1e6:.2f}MB  订阅成功={self.sub_ok}/{len(self.instruments)}  "
               f"最后行情距今={idle:.0f}s")
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as fh:
                fh.write(msg + "\n")
        except OSError:
            pass
        if now - self.last_log >= self.log_every:
            self.last_log = now
            self._log(msg)

    def _log(self, s):
        print(f"[tick] {s}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="724", choices=list(SIMNOW_FRONTS))
    ap.add_argument("--inst", default=None, help="逗号分隔合约；不给则按当前日期生成活跃候选")
    ap.add_argument("--max-syms", type=int, default=12, help="自动生成时最多订阅几个合约")
    ap.add_argument("--flush", type=int, default=300, help="每多少秒落一次盘（默认 300）")
    ap.add_argument("--minutes", type=float, default=5, help="录多少分钟后自动收尾（0=一直录）")
    a = ap.parse_args()

    try:
        import simnow_account as acc
    except ImportError:
        raise SystemExit("找不到 simnow_account.py")
    if not acc.PASSWORD:
        raise SystemExit("simnow_account.py 里的 PASSWORD 为空")

    cfg = {"broker": acc.BROKER_ID, "user": (acc.USER_ID), "pwd": acc.PASSWORD,
           **SIMNOW_FRONTS[a.env]}
    for k in ("trade", "md"):
        host = cfg[k].split("//")[-1].split(":")[0]
        if host not in ALLOWED_HOSTS:
            raise SystemExit(f"⛔ 拒绝连接：{cfg[k]} 不在 SimNow 白名单内")

    insts = ([s.strip() for s in a.inst.split(",") if s.strip()] if a.inst
             else _default_insts()[:a.max_syms])
    print("=" * 84)
    print(f"CTP tick 录制  ｜ env={a.env}  ｜ 账号={cfg['user']}  ｜ 落盘每 {a.flush}s  ｜ "
          f"时长 {'∞' if a.minutes <= 0 else str(a.minutes) + ' 分钟'}")
    print(f"  行情前置 {cfg['md']}   白名单闸门已生效：只可能连 {ALLOWED_HOSTS}")
    print(f"  订阅 {len(insts)} 个合约：{','.join(insts)}")
    print(f"  输出 {OUT_DIR}")
    print("=" * 84, flush=True)

    md_flow = os.path.join(FLOW_DIR, "md")
    os.makedirs(md_flow, exist_ok=True)
    md = mdapi.CThostFtdcMdApi.CreateFtdcMdApi(md_flow + os.sep, False, False)
    rec = Recorder(md, cfg, insts, a.flush)
    md.RegisterFront(cfg["md"])
    md.RegisterSpi(rec)
    md.Init()

    def _bye(*_):
        rec.stop = True
    signal.signal(signal.SIGINT, _bye)

    t0 = time.time()
    try:
        while not rec.stop:
            time.sleep(1)
            rec.flush()
            rec.heartbeat()
            if a.minutes > 0 and (time.time() - t0) / 60 >= a.minutes:
                break
            if time.time() - rec.last_tick_t > 180:
                print("[tick] ⚠️ 180 秒无行情（可能休市或前置异常）", flush=True)
    except KeyboardInterrupt:
        pass

    rec.flush(force=True)
    el = time.time() - t0
    print("\n" + "=" * 84)
    print(f"录制结束：时长 {el/60:.1f} 分钟  总条数 {rec.n_total}  文件 {rec.n_file}  "
          f"体积 {rec.n_bytes/1e6:.2f} MB")
    if rec.n_total > 0 and el > 0:
        n_sym = max(1, rec.sub_ok or len(insts))
        per_sym_h = rec.n_total / n_sym / (el / 3600)
        bpr = rec.n_bytes / rec.n_total
        rows_per_file = rec.n_total / max(1, rec.n_file)
        print(f"  ⇒ 单合约速率 {per_sym_h:,.0f} 条/小时（{per_sym_h/3600:.2f} 条/秒）"
              f" —— CTP 快照理论上限约 2 条/秒")
        print(f"  ⇒ 体积效率 {bpr:.0f} 字节/条（zstd 压缩后）；平均 {rows_per_file:.0f} 行/文件")
        if bpr > 150:
            print(f"  ⚠️ **每行字节数偏高（{bpr:.0f}B）**：parquet 的元数据/footer 开销被小文件放大了。"
                  f"把 --flush 调大（300~600 秒）可显著降低。")
        print(f"  ⇒ 按此推算：单合约一交易日(约 5.75h) ≈ "
              f"{per_sym_h*5.75*bpr/1e6:.1f} MB；"
              f"{n_sym} 合约一年(250 日) ≈ "
              f"{per_sym_h*5.75*250*n_sym*bpr/1e9:.2f} GB")
        try:
            from collections import Counter
            cnt = Counter()
            for root, _, files in os.walk(OUT_DIR):
                if "sym=" in root:
                    sym = root.split("sym=")[-1]
                    for f in files:
                        if f.endswith(".parquet"):
                            cnt[sym] += pq.read_table(os.path.join(root, f)).num_rows
            print("  ⇒ 逐品种条数：", "  ".join(f"{k}={v}" for k, v in sorted(cnt.items())))
        except Exception as e:
            print(f"  （逐品种统计失败：{e}）")
    print("=" * 84, flush=True)
    os._exit(0)


if __name__ == "__main__":
    main()
