# -*- coding: utf-8 -*-
"""
复盘工具 · 数据底座（阶段 2）
================================================================
本文件是 **唯一允许 `import tqsdk` 的地方**（简报 §4 阶段 2 / §4 铁律）。

职责：
  · load_kline(sym, period, start, end, source) -> (DataFrame, meta)
  · load_ledger(sym) -> DataFrame
  · 缓存到 `工具/复盘/_缓存/*.parquet`；跨品种查询用 duckdb
  · load_rollover / rollover_points：主连换月点（§6·3 画换月竖线要用）

四条硬约束（简报 §五 + §6）：
  1. **前端永不直连 tqsdk**：所有取数落盘成 parquet，前端只读盘 ⇒ 可离线、rerun 不打爆账号。
  2. **成交量自知**：现有 CSV 只有 OHLC ⇒ `meta["has_volume"] is False`，下游必须隐藏面板，
     **不许画零线**（§6·2）。tqsdk_live 模式才有 volume。
  3. **日线 high/low 陷阱**：`工具/_进场体检/数据/*_1440min_*` 的 high/low 是开盘态坏值
     （实测 high==close 100%）⇒ **禁用**；1440 一律走 `讨论/真实HL_日线/`（§6·4）。
  4. **主连换月跳空**：CSV 是主连拼接（`is_adjusted=False`），换月处会跳空 ⇒ 换月点由
     `load_rollover` 提供，图上必须画竖线（§6·3）。

★ 运行环境（§2.1 / §3 D5）：
  · CSV 路径：在 `.venv_viz`（复盘工具 venv）里跑，**不需要 tqsdk**。
  · tqsdk 取数路径：`.venv_viz` **未装 tqsdk**（只装 streamlit/plotly/pandas/numpy/duckdb/pyarrow）。
    取数属"预取"，请用策略 venv 跑：`envs\\tqsdk\\Scripts\\python.exe 工具\\复盘\\data.py --fetch ...`
    （D7：只 new `TqSim`，**绝不 `TqKq`**；§6·16：必须 catch `BacktestFinished`，不是判 False）
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# 路径 / 常量
# ---------------------------------------------------------------------------
ROOT = Path(r"C:\Users\cc\Desktop\天勤量化")
KLINE_CSV_DIR = ROOT / "工具" / "_进场体检" / "数据"       # 5min / 15min（可用）＋ 1440min（high/low 坏，禁用）
REALHL_DIR = ROOT / "讨论" / "真实HL_日线"                  # 1440min 的合法替身（§6·4）
LEDGER_DIR = ROOT / "策略" / "06_MA13超短线" / "模拟盘" / "台账"
ROLLOVER_CSV = ROOT / "工具" / "_数据" / "展期_合约日线.csv"
CACHE_DIR = Path(__file__).resolve().parent / "_缓存"
LIVE_DB = CACHE_DIR / "live.db"                             # 阶段 6 实时盯盘库（SQLite WAL）

# ---------------------------------------------------------------------------
# 2026 近期数据（★ 复盘终端用：实盘成交发生在 2026-10，而 _进场体检 的 CSV 只到 2023）
# ---------------------------------------------------------------------------
RECENT_DIR = CACHE_DIR / "kline2026"
# key: (中文名, 天勤主连代码)   —— 与 config_symbols.py 一致，但这里**不 import** 它（简报 D5）
RECENT_SPECS = {
    "M":  ("豆粕",   "KQ.m@DCE.m"),
    "P":  ("棕榈油", "KQ.m@DCE.p"),
    "SR": ("白糖",   "KQ.m@CZCE.SR"),
    "JM": ("焦煤",   "KQ.m@DCE.jm"),
    "JD": ("鸡蛋",   "KQ.m@DCE.jd"),
    "EB": ("苯乙烯", "KQ.m@DCE.eb"),
    # ★ 2026-10-23 用户要求：把「新交易的几个品种」也做进复盘终端（06 模拟盘现池在跑的腿）。
    #   只加数据源，**不动 EXCLUDED_SYMBOLS / 池子逻辑**；是否进 bundle 仍由 build_data 的
    #   EXCLUDED_SYMBOLS 与台账池子周期决定。
    "MA": ("甲醇",   "KQ.m@CZCE.MA"),
    "SA": ("纯碱",   "KQ.m@CZCE.SA"),
    "EG": ("乙二醇", "KQ.m@DCE.eg"),
    "FU": ("燃料油", "KQ.m@SHFE.fu"),
    "BU": ("石油沥青", "KQ.m@SHFE.bu"),
}
# 合约乘数（元/点/手）—— 与 config_symbols.SYMBOLS 第 3 项逐一核对过（2026-10-23）
MULT = {"M": 10, "P": 10, "SR": 10, "JM": 60, "JD": 10, "EB": 5,
        "MA": 10, "SA": 20, "EG": 10, "FU": 10, "BU": 10}
CN2KEY = {cn: k for k, (cn, _c) in RECENT_SPECS.items()}
RECENT_PERIODS = (1, 5, 15, 1440)   # 1min 供左侧分时图；5/15/日供主图

# ---------------------------------------------------------------------------
# 交易所时段元信息（P0-5；不 import config_symbols，遵守 D5）
#   has_1015_break: 是否有 10:15–10:30 小休（DCE/CZCE 有，SHFE 无）
#   night_end:      夜盘结束小时（23 = 21:00–23:00；0 = 无夜盘；1/2.5 = 有色）
# 单独一张表，避免改动 RECENT_SPECS 的 (中文名, 主连) 元组结构、破坏现有 `[0]/[1]` 引用。
# ---------------------------------------------------------------------------
RECENT_META = {
    "M":  {"has_1015_break": True, "night_end": 23.0},
    "P":  {"has_1015_break": True, "night_end": 23.0},
    "SR": {"has_1015_break": True, "night_end": 23.0},
    "JM": {"has_1015_break": True, "night_end": 23.0},
    "JD": {"has_1015_break": True, "night_end": 0.0},   # 鸡蛋无夜盘
    "EB": {"has_1015_break": True, "night_end": 23.0},
    # ★ 2026-10-23 新增五腿。时段口径已用抓回的 K 线**实测校验**（2026-10-22 数据）：
    #   10:15–10:30 小休：五腿同日 10:05/10:10 有 bar、10:15–10:25 无、10:30 恢复 ⇒ 均有小休
    #     （含 SHFE 的 FU/BU；旧注释「SHFE 无小休」与实测不符，已在本次纠正）
    #   夜盘：21:00 起、末根 22:55 ⇒ 均为 21:00–23:00，night_end = 23.0
    "MA": {"has_1015_break": True, "night_end": 23.0},
    "SA": {"has_1015_break": True, "night_end": 23.0},
    "EG": {"has_1015_break": True, "night_end": 23.0},
    "FU": {"has_1015_break": True, "night_end": 23.0},
    "BU": {"has_1015_break": True, "night_end": 23.0},
}

ENCODING = "utf-8-sig"        # ★ §6·11：所有 CSV 带 BOM（已实测 EF BB BF），必须 utf-8-sig
NS = 1_000_000_000           # tqsdk datetime 是纳秒整数（§6·10）

# hl_source 标签：**CSV 直读路径与 parquet 缓存命中路径必须给同一串**，
# 否则命中缓存时自检 ④（`"真实HL" in hl_source`）会失败，阶段 2 验收证据不可重复。
HL_SRC_1440 = "真实HL_日线（替代 _进场体检 的 1440：后者 high==close 100%，§6·4）"
HL_SRC_CSV = "_进场体检/数据（5/15min 可用）"

# ---------------------------------------------------------------------------
# ★★ 时区（阶段 5 实测发现，简报 §5/§6 未列）★★
#   CSV 里的 `datetime` 是 **UTC**，比北京时间**慢 8 小时**。这是实测反推出来的，不是推测：
#     · MA / RU 5min 恰好 **69** 个时间点 = 09:00–10:15(15 根) + 10:30–11:30(12 根)
#       + 13:30–15:00(18 根) + 21:00–23:00(24 根)，且**正好缺** raw 02:15–02:25
#       ——这正是中/大/上期所商品期货 **10:15–10:30 休市**的断档。
#     · JD / LH（无夜盘）恰好 **45** 个时间点 = 早盘 27 + 午盘 18，raw 里**没有** 13:00–14:55 那段。
#     · 原始时刻区间 raw 01:00–14:55 ⇒ +8h = 北京 09:00–22:55（bar 按"**起始时刻**"打标：
#       5min 最后一根 raw 14:55 = 北京 22:55，覆盖 22:55–23:00）。
#   ⇒ **一切时段判定必须先 to_beijing()**；纯价差量（ATR / 收益率 / 振幅）与该平移无关。
#   ⇒ 日线（1440）raw 时刻恒为 16:00，日期口径另有来历，**不做平移**（见 app.py 说明）。
TZ_OFFSET_HOURS = 8
TZ_NOTE = ("CSV 原始 datetime 为 UTC（比北京时间慢 8h，实测反推）；"
           "时段判定与图表时间轴请用 to_beijing()")

META_KEYS = ("sym", "period_min", "source", "has_volume", "is_adjusted", "n",
             "file", "hl_source", "tz", "note")


def to_beijing(ts):
    """把 CSV 原始（UTC）时间戳换算成**北京时间**（+8h）。支持标量 / Series / DatetimeIndex。"""
    off = pd.Timedelta(hours=TZ_OFFSET_HOURS)
    if isinstance(ts, (pd.Series, pd.DatetimeIndex)):
        return ts + off
    return pd.Timestamp(ts) + off


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _period_minutes(period) -> int:
    """'5min' / '5' / 5 → 5。"""
    if isinstance(period, str):
        period = period.strip().lower().replace("min", "").strip()
    return int(float(period))


def _clean_cols(df: pd.DataFrame) -> pd.DataFrame:
    """去掉列名里的 BOM 残渣（§6·11）。"""
    df.columns = [str(c).lstrip("\ufeff") for c in df.columns]
    return df


def _pick_file(matches: list[Path], start, end) -> Path:
    """优先选文件名里含 start/end 的那个；否则取唯一/首个。"""
    if not matches:
        raise FileNotFoundError("没有匹配的 CSV")
    if start and end:
        for p in matches:
            if str(start) in p.name and str(end) in p.name:
                return p
    return matches[0]


def _kp(sym: str, period: int) -> str:
    return f"{sym.upper()}_{period}min"


def _cache_path(sym: str, period: int, start, end, source: str) -> Path:
    key = f"{source}_{_kp(sym, period)}_{start or 'full'}_{end or 'full'}.parquet"
    return CACHE_DIR / key


def _make_meta(df: pd.DataFrame, sym: str, period: int, source: str,
               file: str = "", hl_source: str = "", note: str = "") -> dict:
    # has_volume：CSV 只有 5 列 OHLC（实测无 volume）；tqsdk_live 有；tqsdk_bt 2018 后全零（§6·2）
    has_volume = source == "tqsdk_live" and "volume" in df.columns
    return {
        "sym": sym.upper(),
        "period_min": int(period),
        "source": source,
        "has_volume": bool(has_volume),
        "is_adjusted": False,     # 主连拼接，未复权 ⇒ 换月跳空仍在（§6·3）
        "n": int(len(df)),
        "file": file,
        "hl_source": hl_source,
        "tz": TZ_NOTE,
        "note": note,
    }


# ---------------------------------------------------------------------------
# K 线：CSV 源
# ---------------------------------------------------------------------------
def _read_kline_csv(sym: str, period: int, start, end) -> tuple[pd.DataFrame, dict]:
    if period == 1440:
        # ★§6·4：1440 禁用 _进场体检 那份（high/low 开盘态），改读 真实HL_日线
        matches = sorted(REALHL_DIR.glob(f"{sym.upper()}_1440min_*_realHL.csv"))
        p = _pick_file(matches, start, end)
        hl_source = HL_SRC_1440
    else:
        matches = sorted(KLINE_CSV_DIR.glob(f"{sym.upper()}_{period}min_*.csv"))
        p = _pick_file(matches, start, end)
        hl_source = HL_SRC_CSV

    df = _clean_cols(pd.read_csv(p, encoding=ENCODING))
    df["datetime"] = pd.to_datetime(df["datetime"])
    keep = ["datetime", "open", "high", "low", "close"]
    df = df[[c for c in keep if c in df.columns]].sort_values("datetime").reset_index(drop=True)

    if start is not None:
        df = df[df["datetime"].dt.date >= pd.Timestamp(start).date()]
    if end is not None:
        df = df[df["datetime"].dt.date <= pd.Timestamp(end).date()]
    df = df.reset_index(drop=True)

    note = "CSV 无 volume 列 ⇒ has_volume=False，下游必须隐藏成交量面板（§6·2）"
    meta = _make_meta(df, sym, period, "csv", file=p.name, hl_source=hl_source, note=note)
    return df, meta


# ---------------------------------------------------------------------------
# K 线：tqsdk 源（唯一 import tqsdk 处；需策略 venv + 网络）
# ---------------------------------------------------------------------------
def _load_auth():
    """懒加载 `config_account.py` 的 (USER, PASSWORD)。**绝不打印内容**（§2.2）。"""
    import importlib.util
    cfg = ROOT / "config_account.py"
    if not cfg.exists():
        raise FileNotFoundError(f"缺少 {cfg}（已 gitignore，禁止打印内容）")
    spec = importlib.util.spec_from_file_location("_cfg_account", cfg)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return (getattr(m, "USER", None), getattr(m, "PASSWORD", None))


def _fetch_tqsdk(sym: str, period: int, start, end, source: str, code: str | None, auth):
    """tqsdk 取数。仅被 source ∈ {tqsdk_bt, tqsdk_live} 调用。

    ★ D7：只用 TqSim，绝不 TqKq（会踢掉云端模拟盘，§5.5）。
    ★ §6·16：回测结束 wait_update() 抛 BacktestFinished，**不是返回 False**。
    ★ §6·17：实时模式 data_length 上限 ≈8964 根（5min ≈1 个月）；长历史必须走回测模式。
    ★ §6·10：kline 的 datetime 是纳秒整数 → pd.to_datetime(..., unit="ns")。
    """
    try:
        from tqsdk import TqApi, TqAuth, TqSim, TqBacktest, BacktestFinished
    except ImportError as e:
        raise RuntimeError(
            "当前解释器未安装 tqsdk（复盘工具 venv .venv_viz 按规定不装）。\n"
            "请用策略 venv 预取：envs\\tqsdk\\Scripts\\python.exe 工具\\复盘\\data.py --fetch ..."
        ) from e

    if not code:
        raise ValueError("tqsdk 源必须显式给 code，例如 'KQ.m@CZCE.MA'（工具不复制品种配置）")
    if auth is None:
        auth = _load_auth()

    import datetime as _dt
    s = _dt.datetime.fromisoformat(str(start)) if start else None
    e = _dt.datetime.fromisoformat(str(end)) if end else None

    kwargs = dict(auth=TqAuth(auth[0], auth[1]))
    if source == "tqsdk_bt":
        if not (s and e):
            raise ValueError("tqsdk_bt 必须给 start / end")
        kwargs["backtest"] = TqBacktest(s, e)
    api = TqApi(TqSim(200_000.0), **kwargs)      # ★ 绝不用 TqKq
    try:
        # 实时模式上限 ≈8964，留余量取 3000；回测模式可长
        data_length = 3000 if source == "tqsdk_live" else 8000
        k = api.get_kline_serial(code, period * 60, data_length=data_length)
        if source == "tqsdk_bt":
            try:
                while True:
                    api.wait_update()             # ★ 结束抛 BacktestFinished
            except BacktestFinished:
                pass
        else:
            api.wait_update()                     # 实时：拿到当前快照即可
    finally:
        df = _clean_cols(pd.DataFrame(k))
        api.close()

    df["datetime"] = pd.to_datetime(df["datetime"], unit="ns")   # ★ §6·10 纳秒
    df = df[[c for c in ["datetime", "open", "high", "low", "close", "volume"] if c in df.columns]]
    df = df.sort_values("datetime").reset_index(drop=True)
    note = "tqsdk_live 有 volume；tqsdk_bt 2018 后 volume 全零 ⇒ 仍按『无症状』处理（§6·2）"
    meta = _make_meta(df, sym, period, source, file=code, hl_source="tqsdk", note=note)
    return df, meta


# ---------------------------------------------------------------------------
# 实时会话（阶段 6）：tqsdk 也**只在本文件** import（§7/D5）
# ---------------------------------------------------------------------------
def open_live_sim(specs: list[dict], period_sec: int, data_length: int = 200,
                  init: float = 200_000.0, auth=None):
    """建立**实时** TqSim 会话并订阅多个主连，返回 (api, sessions)。

    specs: [{"key": "MA", "code": "KQ.m@CZCE.MA"}, ...]
    sessions: [{"key","code","quote","kline"}] —— 调用方持有，自己驱动 wait_update 主循环。

    ★ D7：只 `TqSim`，**绝不 `TqKq`**（TqKq 是云端快期账户，5 腿模拟盘正占着，抢账户会把它踢下线）。
    ★ §6·17：实时模式 data_length 上限 ≈8964；短周期取 200 够算 MA13 / ATR14。
    ★ live.py 因此**不需要 import tqsdk**，符合“唯一连网层”约定。
    """
    try:
        from tqsdk import TqApi, TqAuth, TqSim
    except ImportError as e:
        raise RuntimeError(
            "当前解释器未安装 tqsdk（复盘 venv .venv_viz 按规定不装）。\n"
            "live.py 请用策略 venv 运行：envs\\tqsdk\\Scripts\\python.exe 工具\\复盘\\live.py"
        ) from e

    if auth is None:
        auth = _load_auth()
    # ★ TqSim：本地内存虚拟账本，订单不出电脑、重启归零 —— 盯盘只读行情，必须用它
    api = TqApi(TqSim(float(init)), auth=TqAuth(auth[0], auth[1]))
    sessions: list[dict] = []
    try:
        for s in specs:
            sessions.append(dict(
                key=s["key"], code=s["code"],
                quote=api.get_quote(s["code"]),
                kline=api.get_kline_serial(s["code"], int(period_sec), data_length=int(data_length)),
            ))
    except Exception:
        try:
            api.close()
        except Exception:
            pass
        raise
    return api, sessions


# ---------------------------------------------------------------------------
# 对外主函数
# ---------------------------------------------------------------------------
def load_kline(sym: str, period, start=None, end=None, source: str = "csv",
               code: str | None = None, auth=None, use_cache: bool = True,
               ) -> tuple[pd.DataFrame, dict]:
    """读取某品种某周期 K 线，返回 (DataFrame, meta)。

    DataFrame 列：datetime / open / high / low / close（+ volume，仅 tqsdk_live）
    meta：sym / period_min / source / has_volume / is_adjusted / n / file / hl_source / note
    """
    period = _period_minutes(period)
    if source not in ("csv", "tqsdk_bt", "tqsdk_live"):
        raise ValueError(f"source 只能是 csv / tqsdk_bt / tqsdk_live，收到 {source!r}")

    cp = _cache_path(sym, period, start, end, source)
    if use_cache and cp.exists() and source == "csv":
        df = _clean_cols(pd.read_parquet(cp))
        df["datetime"] = pd.to_datetime(df["datetime"])
        # hl_source 必须与直读路径一致（见 HL_SRC_* 注释），否则阶段 2 自检不可重复
        meta = _make_meta(df, sym, period, source,
                          file=cp.name,
                          hl_source=HL_SRC_1440 if period == 1440 else HL_SRC_CSV,
                          note="命中 _缓存/parquet")
        return df, meta

    if source == "csv":
        df, meta = _read_kline_csv(sym, period, start, end)
    else:
        df, meta = _fetch_tqsdk(sym, period, start, end, source, code, auth)

    if use_cache:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(cp, index=False)
    return df, meta


def load_ledger(sym: str | None = None) -> pd.DataFrame:
    """读取台账成交流水（utf-8-sig）。sym 为**中文名**（与台账文件名一致，如 '豆粕'）；None=全部。"""
    pat = f"*{sym}*_成交流水.csv" if sym else "*_成交流水.csv"
    files = sorted(LEDGER_DIR.glob(pat))
    if not files:
        raise FileNotFoundError(f"台账里没有匹配 {pat} 的文件")
    frames = []
    for p in files:
        d = _clean_cols(pd.read_csv(p, encoding=ENCODING, dtype=str))
        d["品种"] = p.name.replace("_成交流水.csv", "")
        d["_来源文件"] = p.name
        frames.append(d)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _clean_recent_df(df: pd.DataFrame) -> pd.DataFrame:
    """清洗 tqsdk 近期数据的三类垃圾行（实测都会出现）：

    1. **零值占位**：日线 data_length=8800 但只有 ~2600 天有数据，其余 `datetime=0`、OHLC=NaN；
    2. **未成形的未来 bar**：日线会在下一交易日预建一根（如周日拿到 10-19 00:00 的 bar）；
    3. **坏 OHLC**：close<=0 / high<low。

    ★ 原始 datetime 是 UTC，所以「现在」要换算成 UTC-naive 再比较（否则差 8h）。
    """
    df = df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df[df["datetime"] > pd.Timestamp("1990-01-01")]
    for c in ("open", "high", "low", "close", "volume"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    need = [c for c in ("open", "high", "low", "close") if c in df.columns]
    df = df.dropna(subset=need)
    df = df[(df["close"] > 0) & (df["high"] >= df["low"])]
    now_utc = pd.Timestamp.now() - pd.Timedelta(hours=TZ_OFFSET_HOURS)
    df = df[df["datetime"] <= now_utc]
    return df.sort_values("datetime").reset_index(drop=True)


def load_recent(key: str, period=5) -> tuple[pd.DataFrame, dict]:
    """读 2026 近期 K 线（`_缓存/kline2026/{KEY}_{period}min.csv`）。

    ★ 原始 datetime 与旧 CSV 同口径：**UTC**（比北京时间慢 8h）⇒ 下游用 `to_beijing()`。
    ★ 日线（1440）例外：其原始时刻固定 16:00，日期即**交易日**，不要 +8h（会跨到次日）。
    ★ `meta["has_volume"]`：tqsdk 数据带 volume ⇒ True，可画成交量副图。
    """
    key = key.upper()
    period = _period_minutes(period)
    p = RECENT_DIR / f"{key}_{period}min.csv"
    if not p.exists():
        raise FileNotFoundError(
            f"缺少 2026 数据 {p}\n"
            f"请用策略 venv 预取：envs\\tqsdk\\Scripts\\python.exe 工具\\复盘\\data.py --fetch2026"
        )
    df = _clean_cols(pd.read_csv(p, encoding=ENCODING))
    df = _clean_recent_df(df)
    keep = [c for c in ("datetime", "open", "high", "low", "close", "volume") if c in df.columns]
    df = df[keep]
    meta = _make_meta(df, key, period, "recent", file=p.name, hl_source="tqsdk")
    vol = pd.to_numeric(df["volume"], errors="coerce") if "volume" in df.columns else None
    meta["has_volume"] = bool(vol is not None and vol.abs().sum() > 0)
    return df, meta


def _save_recent(df: pd.DataFrame, key: str, period: int) -> Path:
    RECENT_DIR.mkdir(parents=True, exist_ok=True)
    p = RECENT_DIR / f"{key}_{period}min.csv"
    df.to_csv(p, index=False, encoding="utf-8-sig")
    return p


def fetch_recent_all(periods=RECENT_PERIODS, data_length: int = 8800, auth=None) -> list[tuple]:
    """**一次连接**拉齐 6 品种 × 3 周期（复盘终端数据）。需策略 venv + 网络。

    返回 [(key, period, n, 最早, 最晚), ...]（时间已换算为北京时间，仅回显用）。

    ★ 只 new `TqSim`，绝不 `TqKq`（D7：TqKq 会踢掉云端模拟盘）。
    ★ 单连接批量订阅：18 次 get_kline_serial 后等一次 wait_update(deadline=25s)，
      避免 18 次鉴权（此前 fetch 走过这个坑：每品种各开连接，慢且易撞限流）。
    """
    try:
        import time
        from tqsdk import TqApi, TqAuth, TqSim
    except ImportError as e:
        raise RuntimeError(
            "当前解释器未安装 tqsdk（.venv_viz 按规定不装）。\n"
            "请用策略 venv 预取：envs\\tqsdk\\Scripts\\python.exe 工具\\复盘\\data.py --fetch2026"
        ) from e

    if auth is None:
        auth = _load_auth()
    api = TqApi(TqSim(200_000.0), auth=TqAuth(auth[0], auth[1]))
    try:
        serials = {}
        for key, (_cn, code) in RECENT_SPECS.items():
            for period in periods:
                serials[(key, int(period))] = api.get_kline_serial(
                    code, int(period) * 60, data_length=int(data_length))
        api.wait_update(deadline=time.time() + 25)
        out: list[tuple] = []
        for (key, period), k in serials.items():
            df = _clean_cols(pd.DataFrame(k))
            df["datetime"] = pd.to_datetime(df["datetime"], unit="ns")   # §6·10 纳秒
            df = df[[c for c in ("datetime", "open", "high", "low", "close", "volume") if c in df.columns]]
            df = _clean_recent_df(df)          # 去零值占位 / 未来未成形 bar / 坏 OHLC
            _save_recent(df, key, period)
            bj = to_beijing(df["datetime"])
            out.append((key, period, len(df), str(bj.min()), str(bj.max())))
        return out
    finally:
        api.close()


def load_rollover(key: str) -> pd.DataFrame:
    """读 展期_合约日线.csv 中某品种：列 key,sym(主力合约),日期,open,close,volume,oi。"""
    df = _clean_cols(pd.read_csv(ROLLOVER_CSV, encoding=ENCODING))
    sub = df[df["key"] == key.upper()].copy()
    sub["日期"] = pd.to_datetime(sub["日期"])
    return sub.sort_values(["日期", "sym"]).reset_index(drop=True)


def rollover_points(key: str) -> pd.DataFrame:
    """主力合约变更点（换月日）——图上画换月竖线用（§6·3）。列：日期, 旧, 新。"""
    df = load_rollover(key)
    if df.empty:
        return pd.DataFrame(columns=["日期", "旧", "新"])
    g = df.groupby("日期")["sym"].last().reset_index()
    changed = g[g["sym"] != g["sym"].shift(1)].copy()
    changed["旧"] = changed["sym"].shift(1)
    out = changed[["日期", "旧", "sym"]].rename(columns={"sym": "新"}).reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# 跨品种查询（duckdb）
# ---------------------------------------------------------------------------
def cache_files(pattern: str = "*.parquet") -> list[Path]:
    return sorted(CACHE_DIR.glob(pattern))


def query(sql: str) -> pd.DataFrame:
    """在缓存 parquet 上跑 duckdb SQL。例：SELECT 品种, count(*) FROM read_parquet('.../*.parquet') GROUP BY 1"""
    import duckdb
    return duckdb.query(sql).df()


# ---------------------------------------------------------------------------
# 自检：阶段 2 验收四条
# ---------------------------------------------------------------------------
def _selftest() -> int:
    ok = True
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 64)

    # ① 能正确读 utf-8-sig（BOM）
    raw = KLINE_CSV_DIR / "MA_5min_2021-01-01_2023-12-31.csv"
    bom = open(raw, "rb").read(3) == b"\xef\xbb\xbf"
    df_bom, meta_bom = load_kline("MA", 5, use_cache=False)
    c1 = list(df_bom.columns)[0] == "datetime"
    print(f"[① BOM] 文件首字节是 BOM={bom}；列名干净(无\\ufeff)={c1}；编码={ENCODING}")
    ok &= bom and c1

    # ② meta['has_volume'] 对现有 CSV 返回 False
    hv = meta_bom["has_volume"]
    print(f"[② has_volume] CSV → {hv}（期望 False，因 CSV 无 volume 列，§6·2）")
    ok &= (hv is False)

    # ③ 同一区间连续读两次，df1.equals(df2) 为 True（第二次命中 parquet 缓存）
    df1, m1 = load_kline("MA", 5, "2021-01-01", "2023-12-31")
    df2, m2 = load_kline("MA", 5, "2021-01-01", "2023-12-31")
    eq = df1.equals(df2)
    print(f"[③ 幂等] n1={m1['n']} n2={m2['n']}  df1.equals(df2)={eq}")
    ok &= eq

    # ④ 1440 走真实HL：broken 那份 high==close 100%，禁用（§6·4）
    df1440, m1440 = load_kline("MA", 1440)
    hl_ok = bool((df1440["high"] != df1440["close"]).mean() > 0.5)
    print(f"[④ 日线HL] 1440 来源={m1440['hl_source']}  n={m1440['n']}  high!=close 占比="
          f"{(df1440['high'] != df1440['close']).mean():.1%}（真实HL 应≫0）")
    ok &= hl_ok and "真实HL" in m1440["hl_source"]

    # 附：12 品种 × 3 周期可枚举
    files = sorted(KLINE_CSV_DIR.glob("*.csv"))
    per = {}
    for p in files:
        mm = re.match(r"([A-Z]+)_(\d+)min_", p.name)
        if mm:
            per.setdefault(int(mm.group(2)), set()).add(mm.group(1))
    print(f"[附] K线 CSV：文件 {len(files)} 个；"
          + " ｜ ".join(f"{k}min={len(v)}品种" for k, v in sorted(per.items())))

    # 附：换月点
    rp = rollover_points("MA")
    print(f"[附] MA 换月点 {len(rp)} 个；示例："
          + "，".join(f"{r['日期'].date()}:{r['旧']}→{r['新']}" for _, r in rp.head(2).iterrows()))

    # 附：duckdb 跨品种查询
    ks = {"MA": 5, "M": 5, "RB": 5}
    for s, p in ks.items():
        load_kline(s, p, use_cache=True)
    try:
        r = query(f"SELECT count(*) AS n FROM read_parquet('{str(CACHE_DIR).replace(chr(92), '/')}/*.parquet')")
        print(f"[附] duckdb read_parquet 命中缓存行数={int(r['n'].iloc[0])}")
    except Exception as ex:
        print(f"[附] duckdb 查询跳过：{ex}")

    print("=" * 64)
    print("阶段 2 自检：" + ("全部通过 ✅" if ok else "存在失败 ❌"))
    return 0 if ok else 1


def _main() -> int:
    ap = argparse.ArgumentParser(description="复盘工具数据底座（阶段 2）")
    ap.add_argument("--selftest", action="store_true", help="跑阶段 2 验收自检")
    ap.add_argument("--fetch", action="store_true", help="用 tqsdk 预取（需策略 venv + 网络）")
    ap.add_argument("--fetch2026", action="store_true",
                    help="★ 一次拉齐 6 品种 × 5/15/日 的 2026 近期日线（复盘终端用）")
    ap.add_argument("--sym", help="品种 KEY，如 MA")
    ap.add_argument("--period", default="5min", help="周期，如 5min / 15min / 1440min")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--source", default="tqsdk_bt", choices=["tqsdk_bt", "tqsdk_live"])
    ap.add_argument("--code", default=None, help="tqsdk 合约码，如 KQ.m@CZCE.MA")
    a = ap.parse_args()

    if a.fetch2026:
        rows = fetch_recent_all()
        print(f"{'key':>4} {'period':>6} {'n':>6}  {'最早(北京)':<20} {'最晚(北京)':<20}")
        for key, period, n, t0, t1 in rows:
            print(f"{key:>4} {period:>6} {n:>6}  {t0:<20} {t1:<20}")
        print(f"已落盘到 {RECENT_DIR}")
        return 0

    if a.selftest or not a.fetch:
        return _selftest()
    df, meta = load_kline(a.sym, a.period, a.start, a.end, source=a.source, code=a.code)
    print(f"fetched {meta}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
