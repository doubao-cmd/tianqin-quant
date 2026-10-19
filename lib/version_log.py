# -*- coding: utf-8 -*-
"""
版本切换留档（A11 版本分段的数据源）
==========================================================
要解决的问题
------------
复盘工具按「INTRABAR_STOP 口径」分段统计成交（旧版「收盘才查」vs 新版「盘中触价即平」）。
分段需要一个**事实**：口径从哪一刻开始真正生效。

靠人补录有三个坑，2026-10-18 全踩了一遍：
  ① 记的是「代码改完的时刻」（22:06:20），不是「进程真正加载该代码的时刻」——
     当时进程 22:01 就起来了，跑的是旧码，中间那段成交其实属于旧版；
  ② 忘了记 → 台账没有任何标记，复盘只能全部混算（方案 §1.4 后果 2）；
  ③ 手改容易写错列、写错编码，把下游解析器搞崩。

做法
----
策略**每次启动**（run_live_multi 抢到核心锁、写完 START 行之后）调用
`log_version_switch()`，把「本次真正加载的口径」写进
`策略/06_MA13超短线/模拟盘/台账/版本切换记录.csv`。

写盘规则（幂等，绝不刷屏）：
  · 口径值（INTRABAR_STOP）与最后一行**相同** → 不写（重启 ≠ 切换）；
  · 与最后一行**不同** → 追加一行（真切换）；
  · 最后一行是**手工补录**且口径相同 → 把它的时间**改成本次启动时刻**
    （手工行只能代表"代码落地"，本次启动才是"真正生效"；原时间写进备注留痕）。
"""
from __future__ import annotations

import csv
import hashlib
import os
import time
from datetime import datetime

# 列顺序即文件列顺序。前两列是复盘解析器认的（记录时间 / INTRABAR_STOP），
# 其余是给人看的证据链，加列不会破坏解析（DictReader 按名取）。
COLS = ["记录时间", "INTRABAR_STOP", "代码指纹", "PID", "池子", "来源", "备注"]

SRC_AUTO = "启动自记录"
SRC_MANUAL = "手工补录"
FILE_NAME = "版本切换记录.csv"


def code_fingerprint(paths) -> str:
    """把若干源文件的内容按固定顺序串起来取 sha1 前 12 位。

    只用于"这份留档对应哪一版代码"的追溯，**不参与**分段判定
    （否则改一句注释就会凭空多一个版本段，把统计切碎）。
    """
    h = hashlib.sha1()
    for p in paths or ():
        try:
            with open(p, "rb") as fh:
                h.update(fh.read())
        except Exception:
            h.update(b"<missing>")
    return h.hexdigest()[:12] if paths else ""


def _read(path):
    rows = []
    if not os.path.isfile(path):
        return rows
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            ts = str(r.get("记录时间") or "").strip()
            if len(ts) < 16:                 # 半行/表头残留 → 跳过
                continue
            rows.append({c: str(r.get(c) or "").strip() for c in COLS})
    return rows


def _write(path, rows):
    """整表重写（先写 .tmp 再 replace）：绝不留下写了一半的台账。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLS})
    # 复盘构建/编辑器可能正开着该文件（Windows 上是独占锁）→ 短暂重试，仍失败则抛出
    for i in range(4):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            if i == 3:
                raise
            time.sleep(0.15)


def _on_of(row) -> bool:
    return str(row.get("INTRABAR_STOP") or "").strip().lower() == "true"


def log_version_switch(ledger_dir, intrabar_stop, code_paths=(), pool=None,
                       now=None, note="") -> dict:
    """按需追加/修正一行版本切换记录。

    参数
      ledger_dir   台账目录（绝对路径）
      intrabar_stop 当前加载的 INTRABAR_STOP 取值
      code_paths   参与指纹的源文件（顺序固定）
      pool         本次启动订阅的品种 key 列表
      now          注入时间（测试用），默认 datetime.now()
    返回 {"action": append|correct|unchanged|error, "line":..., "path":..., "why":...}
    """
    out = {"action": "error", "line": "", "path": "", "why": ""}
    try:
        path = os.path.join(ledger_dir, FILE_NAME)
        out["path"] = path
        on = bool(intrabar_stop)
        fp = code_fingerprint(code_paths)
        ts = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        pool_s = "/".join([str(x) for x in (pool or [])])
        memo = f"{SRC_AUTO}：PID={os.getpid()} 池子={pool_s or '—'} 代码指纹={fp or '—'}"
        if note:
            memo += f" {note}"

        rows = _read(path)
        if not rows:
            rows.append({"记录时间": ts, "INTRABAR_STOP": str(on), "代码指纹": fp,
                         "PID": str(os.getpid()), "池子": pool_s,
                         "来源": SRC_AUTO, "备注": memo + "（首条记录）"})
            _write(path, rows)
            out.update(action="append", line=ts, why="首次记录")
            return out

        last = rows[-1]
        same = (_on_of(last) == on)
        src = last.get("来源") or ""

        if same and src in ("", SRC_MANUAL):
            # 手工行 = 代码落地时刻的推测；本次启动才是真正生效。改时间，留痕。
            old = last["记录时间"]
            last["记录时间"] = ts
            last["INTRABAR_STOP"] = str(on)
            last["代码指纹"] = fp or last.get("代码指纹", "")
            last["PID"] = str(os.getpid())
            if pool_s:
                last["池子"] = pool_s
            last["来源"] = SRC_AUTO
            tail = (f"｜生效时刻修正：原手工记录 {old}（代码落地），"
                    f"本次启动 {ts} 才真正加载本口径；复盘按 {ts} 切分")
            last["备注"] = ((last.get("备注") or "") + tail).strip("｜ ")
            _write(path, rows)
            out.update(action="correct", line=ts,
                       why=f"手工行 {old} → 改为本次启动时刻（口径未变，只修生效时刻）")
            return out

        if same:
            out.update(action="unchanged", line=last["记录时间"],
                       why=f"口径未变（{str(on)}），不写")
            return out

        rows.append({"记录时间": ts, "INTRABAR_STOP": str(on), "代码指纹": fp,
                     "PID": str(os.getpid()), "池子": pool_s,
                     "来源": SRC_AUTO, "备注": memo})
        _write(path, rows)
        out.update(action="append", line=ts, why=f"口径切换 → {str(on)}")
        return out
    except Exception as e:                      # 台账写失败绝不能拖垮交易
        out.update(action="error", why=f"{type(e).__name__}: {e}")
        return out
