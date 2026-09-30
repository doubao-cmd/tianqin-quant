# -*- coding: utf-8 -*-
"""
账户流水单（模拟盘 / 实盘的真实成交记录）
==========================================================
★ 与 `_LiveRecorder`（成交流水 + 运行心跳）的区别
--------------------------------------------------
`_LiveRecorder` 是**策略视角**：记的是「这个策略发了什么信号、下了什么单」，
文件放在 `实测记录/<策略>/<品种>/` 下，用来做台账（判断程序有没有在跑）。

本模块是**账户视角**：像期货交易软件那样，只认账户 ——
不管哪个策略下的单，只看「账户里真实成交了什么、扣了多少手续费、浮盈多少」。
文件放在 `实测记录/账户/` 下，一份流水覆盖账户里的**所有品种、所有策略**。

★ 数据来源（三个对象拼出来，这是关键）
--------------------------------------------------
tqsdk 的 `Trade` 对象**不含手续费和盈亏**（那是账户/持仓级别的），所以：
    成交价 / 手数 / 方向 / 合约 / 成交编号  <- Trade
    手续费（单笔）                          <- Account.commission  差分
    平仓盈亏（单笔）                        <- Account.close_profit 差分
    滑点（成交价 vs 委托价）                <- Trade.price vs Order.trade_price/limit_price
    浮盈（快照）                            <- Position.float_profit

★ 为什么手续费要"差分"
--------------------------------------------------
`Account.commission` 是**开盘至今的累计值**，不是单笔值。
所以每次抓到新成交时，用「本次累计 − 上次累计」得到这一笔的手续费。
若一次抓到多笔成交（同一 tick 内），按手数比例分摊。

★ 滑点怎么算
--------------------------------------------------
真滑点 = 成交价 − 委托价（买为正=吃亏，卖为负=吃亏）。
委托价来源优先级：
    1) Order.trade_price（该委托单的实际成交均价）—— 最准
    2) Order.limit_price（限价）
    3) 目标仓位下达时的现价（`参考价`）—— 兜底，精度差
没有参照时留空，**绝不编造**。

★ 拒单 / 漏单怎么记
--------------------------------------------------
`Order.is_error = True` 或 `last_msg` 含"拒绝" → 记一条 REJECT 行。
「目标仓位已下达、但一直没成交」由调用方（engine）在定期对账时记 DESYNC 行。

记录失败绝不允许拖垮交易：所有写盘都包在 try/except 里。
"""

import csv
import io
import os
from datetime import datetime

# 中文映射（与 engine 里的 DIR_CN/OFF_CN 保持一致，这里独立一份避免循环依赖）
_方向中文 = {"BUY": "买", "SELL": "卖"}
_开平中文 = {"OPEN": "开仓", "CLOSE": "平仓", "CLOSETODAY": "平今",
             "CLOSE_YESTERDAY": "平昨"}


class AccountLedger:
    """账户级成交流水单。一个账户一份，覆盖所有品种/所有策略。

    用法（在 run_live_* 里）：
        ledger = AccountLedger(账户目录, 标签="03布林带")   # 标签只做备注，不参与分文件
        ledger.snapshot(api, account, positions)            # 每轮可选：记账户快照
        ledger.on_trade(trade, order, api, account)         # 每笔新成交
    """

    # 成交明细列（对齐期货软件成交单）
    TRADE_COLS = [
        "成交时间",              # 记录时刻
        "交易所成交号",           # exchange_trade_id —— 真实流水单的凭证
        "合约",                  # instrument_id
        "方向",                  # 买/卖
        "开平",                  # 开仓/平仓/平今
        "手数",                  # volume
        "成交价",                # price
        "委托价",                # 参照价（用于算滑点）
        "滑点",                  # 成交价 − 委托价（买正卖负=吃亏）
        "滑点(跳)",              # 换算成最小变动价位
        "手续费",                # 本笔（账户累计差分，按手数分摊）
        "平仓盈亏",              # 本笔（账户累计差分，按手数分摊）
        "成交额",                # price × volume × 乘数
        "策略标签",              # 哪个策略的程序记的（仅备注，不用于分文件）
        "备注",
    ]

    # 账户快照列（对齐期货软件的「资金/持仓」栏）
    SNAP_COLS = [
        "时间", "权益", "可用资金", "占用保证金",
        "浮动盈亏", "平仓盈亏(累计)", "手续费(累计)",
        "持仓品种数", "持仓明细", "持仓浮盈合计",
    ]

    def __init__(self, ledger_dir, tag="", enabled=True):
        self.on = bool(enabled and ledger_dir)
        self.tag = tag or ""
        self._tv = None
        self._sv = None
        self._last_comm = None      # 上次看到的账户累计手续费
        self._last_close = None     # 上次看到的账户累计平仓盈亏
        if not self.on:
            return
        try:
            os.makedirs(ledger_dir, exist_ok=True)
            self._tv = self._open(
                os.path.join(ledger_dir, "成交明细.csv"), self.TRADE_COLS)
            self._sv = self._open(
                os.path.join(ledger_dir, "账户快照.csv"), self.SNAP_COLS)
            self._事件(os.path.join(ledger_dir, "流水单事件.log"),
                       f"===== 流水单启动 {datetime.now():%Y-%m-%d %H:%M:%S} "
                       f"| 标签 {self.tag or '(无)'} =====")
        except Exception as e:                     # 记录失败不影响交易
            print(f"  [!] 流水单初始化失败（不影响交易）：{e}", flush=True)
            self.on = False

    # ---------------- 内部工具 ----------------
    @staticmethod
    def _open(path, cols):
        new = (not os.path.exists(path)) or os.path.getsize(path) == 0
        fh = open(path, "a", encoding="utf-8-sig", newline="")
        w = csv.writer(fh)
        if new:
            w.writerow(cols)
            fh.flush()
        return (fh, w)

    def _w(self, pair, row):
        if not self.on or pair is None:
            return
        fh, w = pair
        try:
            w.writerow(row)
            fh.flush()          # 立刻落盘：程序随时可能被强杀
        except Exception:
            pass

    @staticmethod
    def _事件(path, text):
        try:
            with io.open(path, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass

    @staticmethod
    def _now():
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _f(v, nd=2):
        """安全转 float；nan/None → 空字符串（不编造数据）。"""
        try:
            x = float(v)
            if x != x:                # nan
                return ""
            return round(x, nd)
        except Exception:
            return ""

    # ---------------- 基准 ----------------
    def prime(self, account):
        """记下「启动时刻」的账户累计手续费/平仓盈亏，作为第一笔差分的基准。

        ★ 必须在主循环开始前调用一次。否则程序启动后的**第一笔**成交，
          手续费会被记成"账户开盘至今的累计值"（因为那时没有基准可减），
          数字严重偏大。这是差分法的必要前提。
        """
        if not self.on or account is None:
            return
        c = self._f(getattr(account, "commission", None), 3)
        cp = self._f(getattr(account, "close_profit", None), 2)
        if c != "":
            self._last_comm = c
        if cp != "":
            self._last_close = cp

    # ---------------- 成交 ----------------
    def on_trade(self, trade, order=None, account=None, mult=1, tick=1.0,
                 ref_price=None):
        """记一笔成交。

        trade      : tqsdk Trade 对象
        order      : 同一 order_id 的 Order 对象（可 None）
        account    : tqsdk Account（用于手续费/平仓盈亏差分）
        mult       : 合约乘数（算成交额）
        tick       : 最小变动价位（算滑点跳数）
        ref_price  : 兜底参考价（拿不到委托价时用，通常是下达目标仓位时的现价）
        """
        if not self.on:
            return

        d = _方向中文.get(getattr(trade, "direction", ""), getattr(trade, "direction", ""))
        o = _开平中文.get(getattr(trade, "offset", ""), getattr(trade, "offset", ""))
        vol = int(getattr(trade, "volume", 0) or 0)
        px = self._f(getattr(trade, "price", None))

        # ---- 委托价：Order.trade_price 优先，其次 limit_price，最后 ref_price ----
        委托价 = None
        委托来源 = ""
        if order is not None:
            tp = self._f(getattr(order, "trade_price", None))
            lp = self._f(getattr(order, "limit_price", None))
            if tp != "" and tp:
                委托价, 委托来源 = tp, "委托单成交均价"
            elif lp != "" and lp:
                委托价, 委托来源 = lp, "委托限价"
        if 委托价 is None and ref_price is not None:
            委托价 = self._f(ref_price)
            委托来源 = "下达时现价(兜底)"

        # ---- 滑点：正值=吃亏（买贵了/卖便宜了）----
        滑点, 滑点跳 = "", ""
        if 委托价 is not None and px != "" and 委托价:
            差 = px - 委托价
            if d == "卖":
                差 = -差                    # 卖出：成交价低于委托价 = 吃亏
            if abs(差) < 1e-9:
                差 = 0.0                    # 避免出现 "-0.0"
            滑点 = round(差, 3)
            滑点跳 = round(差 / tick, 2) if tick else ""

        # ---- 手续费 / 平仓盈亏：账户累计差分 ----
        #   ★ 初值处理：`_last_*` 为 None 说明这是本程序启动后的第一笔。
        #     此时**以 0 为基准**（即本笔手续费 = 当前累计值）。
        #     这对「程序启动后第一笔」是准确的 —— 引擎在 __init__ 后已由
        #     调用方 prime() 记过启动基准，正常情况下不会走到 None 分支。
        #     （若调用方没 prime，则第一笔记成"启动至今累计"，会偏大，
        #      所以 prime 必须在主循环前调用。）
        手续费, 平仓盈亏 = "", ""
        if account is not None:
            c = self._f(getattr(account, "commission", None), 3)
            cp = self._f(getattr(account, "close_profit", None), 2)
            if c != "":
                base = self._last_comm if self._last_comm is not None else 0.0
                dc = c - base
                if dc >= 0:
                    手续费 = round(dc, 3)
                self._last_comm = c
            if cp != "":
                base = self._last_close if self._last_close is not None else 0.0
                dcp = cp - base
                平仓盈亏 = round(dcp, 2)
                self._last_close = cp

        # ---- 成交额 ----
        成交额 = ""
        if px != "":
            成交额 = round(float(px) * vol * mult, 2)

        备注 = 委托来源
        if order is not None:
            msg = (getattr(order, "last_msg", "") or "").strip()
            if msg:
                备注 = (备注 + " | " + msg) if 备注 else msg

        self._w(self._tv, [
            self._now(),
            getattr(trade, "exchange_trade_id", "") or "",
            getattr(trade, "instrument_id", "") or "",
            d, o, vol, px,
            委托价 if 委托价 is not None else "",
            滑点, 滑点跳,
            手续费, 平仓盈亏, 成交额,
            self.tag,
            备注,
        ])

    # ---------------- 拒单 ----------------
    def on_reject(self, order, note=""):
        """委托被拒 / 报错 —— 记为一条成交明细（手数 0、带 REJECT 备注）。"""
        if not self.on:
            return
        msg = (getattr(order, "last_msg", "") or "").strip()
        self._w(self._tv, [
            self._now(),
            getattr(order, "exchange_order_id", "") or "",
            getattr(order, "instrument_id", "") or "",
            _方向中文.get(getattr(order, "direction", ""),
                        getattr(order, "direction", "")),
            _开平中文.get(getattr(order, "offset", ""),
                        getattr(order, "offset", "")),
            "-", "-", "-", "-", "-", "-", "-", "-",
            self.tag,
            f"★拒单/报错 | {msg} {note}".strip(),
        ])

    # ---------------- 账户快照 ----------------
    def snapshot(self, account, positions, extra_note=""):
        """记一次账户全景（资金 + 持仓），对齐期货软件的「资金/持仓」栏。

        positions : dict 或 list，元素需有 instrument_id / pos / float_profit 等
        """
        if not self.on:
            return
        持仓行 = []
        浮盈合计 = 0.0
        有持仓 = 0
        try:
            它 = positions.values() if isinstance(positions, dict) else positions
            for p in 它:
                n = int(getattr(p, "pos", 0) or 0)
                if n == 0:
                    continue
                有持仓 += 1
                fp = self._f(getattr(p, "float_profit", None))
                if fp != "":
                    浮盈合计 += float(fp)
                sym = getattr(p, "instrument_id", "?")
                持仓行.append(f"{sym} {n:+d}手(浮盈{fp if fp != '' else '?'})")
        except Exception:
            pass

        self._w(self._sv, [
            self._now(),
            self._f(getattr(account, "balance", None)),
            self._f(getattr(account, "available", None)),
            self._f(getattr(account, "margin", None)),
            self._f(getattr(account, "float_profit", None)),
            self._f(getattr(account, "close_profit", None)),
            self._f(getattr(account, "commission", None), 3),
            有持仓,
            " ; ".join(持仓行) if 持仓行 else "空仓",
            round(浮盈合计, 2),
        ])

    # ---------------- 其他 ----------------
    def event(self, note):
        """落一条文本事件（不写 CSV，只进 log）。"""
        if not self.on:
            return
        d = os.path.dirname(os.path.abspath(self._tv[0].name)) if self._tv else ""
        if d:
            self._事件(os.path.join(d, "流水单事件.log"),
                       f"[{self._now()}] {note}")
