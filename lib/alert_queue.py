# -*- coding: utf-8 -*-
"""
告警队列（#17-A，D2 2026-10-21 拍板「采纳 A」）
==========================================================
【为什么需要】
现有风控告警的唯一出口是 `ma13_live_multi._popup()` —— Windows `MessageBoxW`
弹窗 + 控制台响铃 + 台账一行。**无人值守时（上课/睡眠）等于没有告警**：
熔断、对账停机、单日亏损阀、守护重启，全部只在屏幕上闪一下。

本模块把告警落到一份**结构化、可枚举、可去重**的 append-only 文件：

    <record_dir>/告警队列.jsonl

一期只做「有据可查」，不做外送（#17-C 外送已被 D2 二期否决：
凭证/网络/限流/明文密钥都是新风险面，且「推送失败绝不能影响交易」是硬不变量）。
未来若做外送，必须**独立旁路进程只读本文件**，绝不从交易主循环调用。

【D2 拍板的四条硬约束（不得放宽）】
  1. 等级**可枚举**（LEVELS），不接受自由文本 —— 否则下游无法分级过滤；
  2. **交易日级去重**：同一 dedup_key 同一交易日只落一次，次日自动复位；
     跨交易日同事件重新计数（避免卡死条件刷屏淹没有效告警）；
  3. 写入失败**只记日志、绝不抛、绝不影响主循环**（`push()` 永远返回 bool 不抛）；
  4. **不改既有台账**：告警独立成文件，`成交流水.csv` 等一个字节都不动。

【为什么 dedup 用内存缓存 + 启动回填】
告警量小（每天几十条），但若每次 push 都全量扫文件，长期会变慢；
若每次 push 都重写去重状态文件，则多进程（核心 + 守护）并发写会互相覆盖。
折中：首次用时只回填**当日**已有 key 到内存 set，之后纯内存判重，文件只追加。
进程重启不会重复告警（回填生效）；同一天多进程各自回填也不会重复落同一 key
（因为落盘前还会再查一次文件尾部，见 `_already_on_disk`）。

【自检】
    python -c "from lib import alert_queue as a; a.selftest()"
"""
import json
import os
import time as _time

try:                                       # 包内导入（正常路径）
    from . import live_resend as _rs
except ImportError:                        # 直接 `python lib/alert_queue.py` 时
    import live_resend as _rs             # noqa: F401

# ===================== 常量（D2 拍板，改动即改变告警行为）=====================
QUEUE_NAME = "告警队列.jsonl"

# ★ 等级必须可枚举。新增等级要同步改这里 + LEVEL_RANK + 文档，不许临时造字符串。
LEVEL_CRITICAL = "CRITICAL"     # 熔断 / 对账停机 / 日亏阀 / 守护重启 / 长假强平 / 日历缺失
LEVEL_WARN = "WARN"             # 保证金预警 / 滑点超限 / 隔夜超限 / 连亏触发
LEVEL_INFO = "INFO"             # 参数自证 / 快照完成等留痕类
LEVELS = (LEVEL_CRITICAL, LEVEL_WARN, LEVEL_INFO)

# D2 §2-17.2：必须进最高级的事件集（供接线方对照，本模块不强制校验）
CRITICAL_EVENTS = ("回撤熔断", "对账停机", "单日亏损阀", "守护重启", "长假强平", "日历缺失")

# 单行上限：超过就截断，防止某条异常文本把文件写成天文数字
_MAX_TEXT = 2000

# ---- 进程内状态（不落盘，重启后由当日文件回填）----
_cache_day = None          # 缓存归属的交易日
_cache_keys = set()        # 当日已落盘的 dedup_key
_cache_filled = False      # 是否已从文件回填过


# ---------------------------------------------------------------- 路径 / 工具
def path_of(record_dir):
    return os.path.join(record_dir, QUEUE_NAME)


def _trade_day():
    """当前交易日（复用 live_resend，禁止第二份交易日历）。"""
    try:
        return str(_rs.trading_day())
    except Exception:
        return _time.strftime("%Y%m%d")


def _norm_level(level):
    s = str(level or "").strip().upper()
    return s if s in LEVELS else LEVEL_INFO          # 未知等级降级为 INFO，不抛


def _dkey(level, source, text, dedup_key):
    """去重键：显式给了就用（加等级前缀），否则由 source+text 派生。

    ★ 派生而不是要求调用方必传：接线点散落在各处，漏传就退化成"不去重"，
      那正是 D2 要避免的刷屏。派生能保证**默认行为就是去重的**。
    """
    if dedup_key:
        k = str(dedup_key)
    else:
        k = f"{source}|{text}"
    return f"{level}:{k}"[:300]


def _fill_cache(record_dir, day):
    """从文件回填**当日**已落盘的 key（只读尾部 512KB，避免大文件全量扫）。"""
    global _cache_day, _cache_keys, _cache_filled
    _cache_day, _cache_keys, _cache_filled = day, set(), True
    p = path_of(record_dir)
    if not os.path.isfile(p):
        return
    try:
        size = os.path.getsize(p)
        start = max(0, size - 512 * 1024)
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            if start:
                f.seek(start)
                f.readline()                   # 丢掉被截断的半行
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                if str(o.get("trade_day")) == day and o.get("dedup_key"):
                    _cache_keys.add(str(o["dedup_key"]))
    except Exception:
        pass                                   # 回填失败 = 退化为"不去重"，不阻断交易


def _already_on_disk(record_dir, day, dkey):
    """跨进程兜底：核心与守护各自有内存缓存，落盘前再查一次文件尾部。

    只在**内存判重未命中**时调用（绝大多数 push 走内存路径，不会频繁读文件）。
    """
    p = path_of(record_dir)
    if not os.path.isfile(p):
        return False
    try:
        size = os.path.getsize(p)
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            f.seek(max(0, size - 128 * 1024))
            if size > 128 * 1024:
                f.readline()
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                if (str(o.get("trade_day")) == day
                        and str(o.get("dedup_key")) == dkey):
                    return True
    except Exception:
        return False
    return False


# ---------------------------------------------------------------- 主接口
def push(record_dir, level, source, text, dedup_key=None, day=None,
         extra=None):
    """落一条告警。**永不抛异常**，返回 (ok: bool, note: str)。

    参数
    ----
    record_dir : 台账目录（None/不存在 ⇒ 直接返回 False，不抛）
    level      : LEVELS 之一，未知值降级 INFO
    source     : 来源（品种名 / 模块名，如 "豆粕"、"守护"、"daily_loss"）
    text       : 告警正文
    dedup_key  : 去重键；缺省由 source+text 派生（默认就去重）
    day        : 交易日（缺省按 live_resend 推）；跨日测试可直接给
    extra      : dict，附加上下文（写进 json 的 `extra` 字段，便于复盘）

    返回 (False, "重复") 表示本交易日该 key 已落过 ⇒ 调用方不必外送/弹窗。
    """
    global _cache_day, _cache_keys, _cache_filled
    try:
        if not record_dir:
            return False, "无 record_dir"
        lv = _norm_level(level)
        td = str(day) if day else _trade_day()
        src = str(source or "?")[:80]
        tx = str(text or "").strip()
        if not tx:
            return False, "空文本"
        if len(tx) > _MAX_TEXT:
            tx = tx[:_MAX_TEXT] + "…(截断)"
        dk = _dkey(lv, src, tx, dedup_key)

        # ---- 交易日切换 ⇒ 缓存复位（D2 §2-17.3：次日自动复位）----
        if (not _cache_filled) or (_cache_day != td):
            _fill_cache(record_dir, td)

        if dk in _cache_keys:
            return False, "重复（本交易日已落）"
        if _already_on_disk(record_dir, td, dk):     # 另一个进程已落
            _cache_keys.add(dk)
            return False, "重复（另一进程已落）"

        os.makedirs(record_dir, exist_ok=True)
        rec = {
            "ts": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "trade_day": td,
            "level": lv,
            "source": src,
            "text": tx,
            "dedup_key": dk,
        }
        if extra:
            try:
                rec["extra"] = dict(extra)
            except Exception:
                pass
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        # ★ append-only 单行写入：CRITICAL 也不能因为写盘失败而影响交易，
        #   所以这里不用原子写（tmp+replace 会把整文件重写，并发下丢记录）。
        with open(path_of(record_dir), "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
        _cache_keys.add(dk)
        return True, "ok"
    except Exception as e:                          # noqa: BLE001
        # 绝不抛：写不进队列只是少一条记录，绝不能连累交易主循环
        try:
            print(f"[告警队列] 写入失败（已忽略，不影响交易）：{type(e).__name__}: {e}",
                  flush=True)
        except Exception:
            pass
        return False, f"{type(e).__name__}: {e}"


def recent(record_dir, day=None, level=None, limit=50):
    """读最近 limit 条（供人工/复盘/未来旁路外送进程用）。失败返回 []。"""
    out = []
    try:
        p = path_of(record_dir)
        if not os.path.isfile(p):
            return out
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                if day and str(o.get("trade_day")) != str(day):
                    continue
                if level and str(o.get("level")) != str(level).upper():
                    continue
                out.append(o)
    except Exception:
        return []
    return out[-limit:]


def reset_cache():
    """仅测试用：清进程内去重缓存。"""
    global _cache_day, _cache_keys, _cache_filled
    _cache_day, _cache_keys, _cache_filled = None, set(), False


# ---------------------------------------------------------------- 自检
def selftest():
    import tempfile
    d = tempfile.mkdtemp(prefix="alert_")

    # 1 基本写入
    ok, note = push(d, LEVEL_CRITICAL, "回撤熔断", "自峰值回撤 20%", day="20261021")
    assert ok, note
    rows = recent(d, day="20261021")
    assert len(rows) == 1 and rows[0]["level"] == LEVEL_CRITICAL, rows
    assert rows[0]["source"] == "回撤熔断", rows
    print("  1 基本写入 OK")

    # 2 等级可枚举：未知等级降级 INFO，不抛
    ok, _ = push(d, "BANANA", "x", "未知等级", day="20261021")
    assert ok, "未知等级不应阻断写入"
    assert recent(d, day="20261021")[-1]["level"] == LEVEL_INFO
    print("  2 未知等级降级 INFO OK")

    # 3 同日去重：同 dedup_key 只落一次
    ok1, _ = push(d, LEVEL_WARN, "豆粕", "保证金预警", dedup_key="m-margin", day="20261021")
    ok2, note2 = push(d, LEVEL_WARN, "豆粕", "保证金预警", dedup_key="m-margin", day="20261021")
    assert ok1 is True and ok2 is False, (ok1, ok2, note2)
    assert "重复" in note2, note2
    print("  3 同交易日去重 OK")

    # 4 默认去重（不传 dedup_key 也要去重，避免接线点漏传退化成刷屏）
    a1, _ = push(d, LEVEL_WARN, "MACD", "同一句话", day="20261021")
    a2, _ = push(d, LEVEL_WARN, "MACD", "同一句话", day="20261021")
    assert a1 is True and a2 is False, (a1, a2)
    print("  4 缺省去重 OK")

    # 5 跨交易日复位（D2 §2-17.3：次日自动复位）
    b1, _ = push(d, LEVEL_WARN, "豆粕", "保证金预警", dedup_key="m-margin", day="20261022")
    assert b1 is True, "次日同 dedup_key 必须重新计数"
    print("  5 跨交易日复位 OK")

    # 6 等级隔离：同 dedup_key 不同等级互不去重（分级是独立信号）
    c1, _ = push(d, LEVEL_CRITICAL, "豆粕", "保证金预警", dedup_key="m-margin", day="20261021")
    assert c1 is True, "不同等级的同名 key 不应被 WARN 挡掉"
    print("  6 等级隔离 OK")

    # 7 异常不抛：record_dir 是文件而不是目录 ⇒ 写失败但返回 False 不抛
    bad = os.path.join(d, "不是目录")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("x")
    ok, note = push(bad, LEVEL_CRITICAL, "x", "y", day="20261021")
    assert ok is False and note, (ok, note)
    print("  7 写入失败不抛（注入异常）OK")

    # 8 空参数不炸
    assert push(None, LEVEL_INFO, "x", "y")[0] is False
    assert push(d, LEVEL_INFO, "x", "")[0] is False
    print("  8 空参数不抛 OK")

    # 9 半行损坏不影响读取（模拟被强杀写了一半）
    with open(path_of(d), "a", encoding="utf-8") as f:
        f.write('{"ts": "2026-10-21 11:00:00", "level": "CRIT')
    rows = recent(d, day="20261021", limit=100)
    assert all("level" in r for r in rows), "损坏行必须被跳过而不是让读取整体失败"
    print("  9 损坏行跳过 OK")

    # 10 重启后不重复告警（缓存回填生效）
    reset_cache()
    ok, note = push(d, LEVEL_WARN, "豆粕", "保证金预警", dedup_key="m-margin", day="20261021")
    assert ok is False and "重复" in note, (ok, note)
    print("  10 重启后回填去重 OK")

    # 11 CRITICAL_EVENTS 覆盖 D2 要求的事件集
    for k in ("回撤熔断", "对账停机", "单日亏损阀", "守护重启"):
        assert k in CRITICAL_EVENTS, k
    print("  11 最高级事件集齐备 OK")

    print("[selftest] 全部通过")


if __name__ == "__main__":
    selftest()
