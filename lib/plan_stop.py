# -*- coding: utf-8 -*-
"""
计划止损持久化（#15：RECOVER 止损重锚修复）—— **纯读写，不 import tqsdk**
================================================================================
问题（`02_审计台账.md` #15，2026-10-21 执行员实测复现）
--------------------------------------------------------------
`ma13_live_multi._attach_stop_on_boot()` 在重启/重连接管云端持仓时，用
    stop = 当前收盘 ∓ 1.15 × ATR
**重新算一条**止损，并把 `entry` 重置为当前价、`bars_in` 归零。

行情反向时这条线比开仓时**更宽** ⇒ 每次重启都在悄悄放大风险：
  · 实测豆粕 开仓 3276 → 重启后止损 3271.1（放宽 4.9 点）
  · 实测纯碱            → 止损 951.9
  · `bars_in` 归零 ⇒ 时间止损（MAX_HOLD）重新计时，"持仓过久"永远不触发

修法（只补「止损线的持久化」，不动出场逻辑）
--------------------------------------------------------------
开仓成交那一刻，把**这条止损线本身**落盘；接管时优先读回来：
  · 有记录（合约 + 方向都对）⇒ 原样恢复 `stop` / `entry` / `bars_in`，**不重算**
  · 无记录（老持仓 / 换月 / 文件被删）⇒ 才走原来的现价重算兜底，并打告警
  · 平仓 ⇒ 清掉记录（防止下一次重启读到上一笔的旧线）
  · 兜底重算出来的线也会立即固化（`src=SYNC`），下次重启起不再重算

为什么不是「计划和重算取更紧的那个」
--------------------------------------------------------------
重算在**盈利**时会收紧止损、在**亏损**时会放宽。前者等价于偷偷加了一条
"移动止损"——策略里没有这个逻辑，属于规则 2 禁止的「叠加条件」；后者直接
放大风险。两个方向都不对，所以唯一语义正确的解是「原样恢复」。

落盘位置
--------------------------------------------------------------
`<record_dir>/计划止损_<key>.json`，每个品种一份（6 条腿互不覆盖）：
    {"key":"M","symbol":"DCE.m2701","dir":1,"stop":3010.0,"entry":3000.0,
     "hand":2,"bar_time":"2026-10-21 09:05:00.000000","src":"OPEN",
     "open_ts":"2026-10-21 09:05:03"}

跑法（纯标准库自检）：
    python -c "from lib import plan_stop; plan_stop.selftest()"
"""
import json
import os
import time

STATE_PREFIX = "计划止损_"
STATE_SUFFIX = ".json"


# ---------------------------------------------------------------- 路径
def path_of(record_dir, key):
    """记录文件路径（record_dir 为空时返回 None）。"""
    if not record_dir or not key:
        return None
    return os.path.join(record_dir, f"{STATE_PREFIX}{key}{STATE_SUFFIX}")


# ---------------------------------------------------------------- 读
def load(record_dir, key, symbol=None, direction=None):
    """读回计划止损。**任何一个环节对不上就返回 None**（宁可不接管，也不拿错线）。

    校验项：
      · 文件存在且可解析、`stop` 是有限正数；
      · `symbol` 给定时必须与记录一致 —— 换月后旧合约的线对新合约无效；
      · `direction` 给定时必须与记录一致（+1 多 / -1 空）——
        方向反了说明这条记录属于上一笔，绝不能套到当前持仓上。
    """
    p = path_of(record_dir, key)
    if not p:
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    try:
        stop = float(d.get("stop"))
        if not (stop == stop) or stop <= 0:
            return None
    except Exception:
        return None
    if symbol is not None and str(d.get("symbol") or "") != str(symbol):
        return None
    if direction is not None:
        try:
            if int(d.get("dir") or 0) != int(direction):
                return None
        except Exception:
            return None
    return d


# ---------------------------------------------------------------- 写
def save(record_dir, key, symbol, stop, entry, direction, hand=0,
         bar_time=None, src="OPEN", open_price=None):
    """落盘一条计划止损。返回是否写入成功（失败只返回 False，**绝不抛**）。

    原子写（tmp + os.replace）：进程在写盘瞬间被强杀也不会留半个 JSON
    —— 半个 JSON 会让下次重启的 `load()` 静默失败、退化成重算，等于白存。

    ★★ E5（2026-10-21）：`entry` 是**策略记账用的入场价**，不等于真实开仓价。
      兜底重算时 `entry` 会被写成"当时现价"（实测 BU 记 4998、真实成交 5003，
      风险被低估 5.4 点/手）。新增 `open_price` 专存**真实成交均价**：
        · 开仓时未知 ⇒ None；FILLX 确认后由 `set_open_price()` 回填；
        · 当日志/报表要说"开仓价"时，必须用 `open_price`，没有就只能说"记录 entry"。
    """
    p = path_of(record_dir, key)
    if not p:
        return False
    try:
        stop = float(stop)
        if not (stop == stop) or stop <= 0:
            return False
    except Exception:
        return False

    def _f2(v):
        try:
            if v is None:
                return None
            x = float(v)
            return x if (x == x and x > 0) else None
        except Exception:
            return None

    d = {
        "key": str(key),
        "symbol": str(symbol or ""),
        "dir": int(direction or 0),
        "stop": float(stop),
        "entry": float(entry or 0.0),
        "open_price": _f2(open_price),
        "hand": int(hand or 0),
        "bar_time": (str(bar_time)[:26] if bar_time is not None else None),
        "src": str(src),
        "open_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp = p + ".tmp"
    try:
        os.makedirs(record_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


# ---------------------------------------------------------------- 回填开仓价
def set_open_price(record_dir, key, price, symbol=None):
    """FILLX 确认后用**真实成交均价**回填 `open_price`（E5）。

    只在记录存在、且 `open_price` 还没值（或是同一笔）时写。
    **绝不改 stop / entry / hand** —— 只补这一个字段，避免动到出场依据。
    返回是否真的写入了（无记录 / 值无效 / 写失败 ⇒ False，不抛）。
    """
    try:
        rec = load(record_dir, key, symbol)
        if not rec:
            return False
        try:
            px = float(price)
        except Exception:
            return False
        if not (px == px and px > 0):
            return False
        if rec.get("open_price") and abs(float(rec["open_price"]) - px) < 1e-9:
            return True                              # 已经是这个值，无需重写
        return save(record_dir, rec.get("key") or key, rec.get("symbol"),
                    rec.get("stop"), rec.get("entry"), rec.get("dir"),
                    rec.get("hand") or 0, rec.get("bar_time"),
                    src=rec.get("src") or "FILLX", open_price=px)
    except Exception:
        return False


# ---------------------------------------------------------------- 清
def clear(record_dir, key, symbol=None):
    """删掉记录。`symbol` 给定时**只删该合约的**记录。

    为什么要校验 symbol：平仓成交与下一笔开仓可能在同一节拍内先后发生，
    无条件删会把这笔新仓刚写的线删掉。返回是否真的删了。
    """
    p = path_of(record_dir, key)
    if not p:
        return False
    try:
        if symbol is not None:
            cur = load(record_dir, key)
            if cur is not None and str(cur.get("symbol") or "") != str(symbol):
                return False          # 记录属于别的合约 ⇒ 不是这笔仓的，别动
        os.remove(p)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


# ---------------------------------------------------------------- 自检
def selftest():
    """离线自检：写入/读回、合约与方向校验、清空、原子性、坏文件降级。"""
    import tempfile
    print("[selftest] plan_stop.py（纯读写，无网络）")
    d = tempfile.mkdtemp(prefix="pstop_")

    # 1) 写入 + 读回逐字一致
    assert save(d, "M", "DCE.m2701", 3200.5, 3260.0, 1, 2, "2026-10-21 09:05:00.000000") is True
    r = load(d, "M", "DCE.m2701", 1)
    assert r is not None
    assert r["stop"] == 3200.5 and r["entry"] == 3260.0 and r["dir"] == 1
    assert r["hand"] == 2 and r["bar_time"] == "2026-10-21 09:05:00.000000"
    print("  1 写入/读回一致 OK")

    # 2) 换月（合约不符）⇒ 视为无记录，绝不套旧线
    assert load(d, "M", "DCE.m2705", 1) is None, "换月后仍套用旧合约止损 = 张冠李戴"
    # 3) 方向不符 ⇒ 无记录
    assert load(d, "M", "DCE.m2701", -1) is None, "方向反了仍套用 = 把上一笔的空单线套到多单"
    # 4) 不给校验参数 ⇒ 仍然可读（调用方自担）
    assert load(d, "M") is not None
    print("  2 合约/方向校验 OK")

    # 5) 其它品种互不干扰
    assert load(d, "SA") is None
    print("  3 品种隔离 OK")

    # 6) clear 只删匹配合约的记录
    assert clear(d, "M", "DCE.m2705") is False, "合约不符时不该删"
    assert load(d, "M") is not None
    assert clear(d, "M", "DCE.m2701") is True
    assert load(d, "M") is None
    assert clear(d, "M") is False, "重复删除应返回 False 而非抛"
    print("  4 定点清空 OK")

    # 7) 坏文件 / 空文件 ⇒ 静默降级为 None
    p = path_of(d, "SA")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"stop": ')                 # 半个 JSON（模拟被强杀）
    assert load(d, "SA") is None
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"stop": -1}')
    assert load(d, "SA") is None, "负止损必须判无效"
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"stop": NaN}')
    assert load(d, "SA") is None, "NaN 止损必须判无效"
    print("  5 坏文件/非法值降级 OK")

    # 8) 非法入参不抛
    assert save(None, "M", "x", 1, 1, 1) is False
    assert save(d, None, "x", 1, 1, 1) is False
    assert save(d, "M", "x", float("nan"), 1, 1) is False
    assert save(d, "M", "x", 0, 1, 1) is False
    assert clear(None, "M") is False
    print("  6 非法入参不抛 OK")

    # 9) 原子写：写完后目录里不应残留 .tmp
    save(d, "EG", "DCE.eg2701", 4000.0, 4050.0, -1, 3)
    leftovers = [x for x in os.listdir(d) if x.endswith(".tmp")]
    assert not leftovers, f"残留临时文件：{leftovers}"
    print("  7 无 .tmp 残留 OK")

    # 8 ★E5：open_price 回填（真实成交均价），且**不得改动 stop/entry/hand**
    d8 = tempfile.mkdtemp(prefix="ps8_")
    assert save(d8, "M", "DCE.m2701", 3200.0, 3260.0, 1, 8, None) is True
    r0 = load(d8, "M", "DCE.m2701", 1)
    assert r0.get("open_price") is None, f"未成交前 open_price 应为 None：{r0}"
    assert set_open_price(d8, "M", 3263.5, symbol="DCE.m2701") is True
    r1 = load(d8, "M", "DCE.m2701", 1)
    assert abs(r1["open_price"] - 3263.5) < 1e-9, r1
    # 回填不能碰出场依据
    assert abs(r1["stop"] - 3200.0) < 1e-9 and abs(r1["entry"] - 3260.0) < 1e-9 \
        and r1["hand"] == 8, f"回填污染了其他字段：{r1}"
    # 幂等 + 非法值不写
    assert set_open_price(d8, "M", 3263.5, symbol="DCE.m2701") is True
    assert set_open_price(d8, "M", 0, symbol="DCE.m2701") is False
    assert set_open_price(d8, "NOPE", 100.0) is False
    assert abs(load(d8, "M", "DCE.m2701", 1)["open_price"] - 3263.5) < 1e-9
    print("  8 open_price 回填（不污染 stop/entry/hand）OK")

    print("[selftest] 全部通过")
    return True


if __name__ == "__main__":
    selftest()
