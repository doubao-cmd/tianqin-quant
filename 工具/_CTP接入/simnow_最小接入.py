# -*- coding: utf-8 -*-
"""
SimNow 最小接入 —— 用 openctp-ctp 直连 CTP，验证「程序化链路」能不能跑通
================================================================================
目的：这是**工程能力验证**，不是策略研究。
      跑通它你就有了「自己写的 CTP 链路」—— 求职时比"我用过文华"高一个档，
      也是将来真接实盘前的必经一步。

⚠️⚠️ 安全设计（按项目血泪条款：会下单的脚本曾真单两次）
  1. **默认只连行情、绝不下单**（`--order` 才下单，且必须再给 `--yes-i-know`）。
  2. **硬闸门：只允许连 SimNow 的两个官方地址**（写死在 SIMNOW_FRONTS 白名单里）。
     ⇒ 无论参数怎么填，**都不可能误连到你的实盘期货公司**。
  3. 下单测试用的是「**远离市价的限价单 + 立刻撤单**」，不追求成交。

用法
----
  python simnow_最小接入.py                    # 连行情，打印盘口（最安全，先跑这个）
  python simnow_最小接入.py --env 1            # 用「第一套环境」（与生产一致）
  python simnow_最小接入.py --td               # 额外连交易前置并登录（只读：查询资金/持仓）
  python simnow_最小接入.py --td --order --yes-i-know   # 再额外做一次限价单往返（模拟盘）

环境（2025-06-19 起官方换过地址，网上老教程必然连不上）
  env 724（推荐首次使用，重播上一交易日行情、随时可连）
      交易 tcp://182.254.243.31:40001     行情 tcp://182.254.243.31:40011
      注：SimNow 文档称其为「看穿式前置」；新注册用户需等第三个交易日
  env 1 （与生产一致，交易时段约 交易日 08:00-16:00 + 20:00-02:30）
      交易 tcp://182.254.243.31:30001     行情 tcp://182.254.243.31:30011
      （另有 30002/30012、30003/30013 两组，连不上可换）
"""
import os
import sys
import time
import tempfile
import datetime
import argparse

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from openctp_ctp import thostmduserapi as mdapi   # noqa: E402
from openctp_ctp import thosttraderapi as tdapi   # noqa: E402

# ==================== ★ 白名单：只允许这两个地址 ====================
SIMNOW_FRONTS = {
    "724": {"trade": "tcp://182.254.243.31:40001", "md": "tcp://182.254.243.31:40011"},
    "1":   {"trade": "tcp://182.254.243.31:30001", "md": "tcp://182.254.243.31:30011"},
}
ALLOWED_HOSTS = ("182.254.243.31",)   # SimNow 唯一官方 IP；换地址需同步改这里

# ⚠️⚠️ 血泪：CTP 的「流文件目录」**绝对不能用含中文的路径**。
#   本项目路径是 `...\桌面\天勤量化\工具\_CTP接入\`，直接拿它当 flow path 会抛
#       RuntimeError: can not open CFlow file in line 331 of ThostFtdcUserApiImplBase.cpp
#   实测（2026-10-12，四种变体）：空串 ✅ / 相对路径 ✅ / **中文绝对路径 ❌** / ASCII 绝对路径 ✅。
#   ⇒ 默认把 flow 放到系统临时目录（纯 ASCII）。要改位置，设环境变量 CTP_FLOW_DIR（同样必须全 ASCII）。
FLOW_DIR = os.environ.get("CTP_FLOW_DIR") or os.path.join(tempfile.gettempdir(), "ctp_flow_tqsdk")
TIMEOUT_S = 25


def _log(tag, msg):
    print(f"[{tag:<4}] {msg}", flush=True)


def _default_insts():
    """按**当前日期**生成一批「可能挂牌」的合约代码。

    ⚠️ 为什么不能写死：合约会到期。2026-10 时 "rb2601"/"MA601" 早已摘牌，
    写死必然订阅不到任何行情（而且**不报错**，只是永远没数据 —— 最难查的那种）。
    命名体系有两套：
      · SHFE / DCE：小写品种 + 4 位 YYMM（如 rb2701 = 2027-01）
      · CZCE：大写品种 + 3 位 YMM（如 MA701 = 2027-01，首位是年份末位）
    两个体系都生成；**订阅不到的行不会报错，只是拿不到数据**，所以多给几个无副作用。
    """
    today = datetime.date.today()
    ms = []
    for k in range(1, 7):                       # 未来 1~6 个月（跳过当月，常已临近交割）
        m = today.month + k
        yy = today.year + (m - 1) // 12
        mm = (m - 1) % 12 + 1
        ms.append((yy, mm))
    out = []
    for code in ("rb", "m", "p", "jm", "ru", "v", "i"):
        for yy, mm in ms:
            out.append(f"{code}{yy % 100:02d}{mm:02d}")
    for code in ("MA", "SR", "TA"):
        for yy, mm in ms:
            out.append(f"{code}{yy % 10}{mm:02d}")
    return out


def _rsp_err(rsp, prefix=""):
    """把 CTP 的 CThostFtdcRspInfoField 转成人话。ErrorID==0 表示成功。"""
    try:
        eid = int(getattr(rsp, "ErrorID", -1))
        emsg = getattr(rsp, "ErrorMsg", "") or ""
    except Exception:
        return f"{prefix}（无法解析响应）"
    if eid == 0:
        return f"{prefix}OK"
    return f"{prefix}❌ ErrorID={eid}  ErrorMsg={emsg}"


# ==================================================================== 行情
class MdSpi(mdapi.CThostFtdcMdSpi):
    def __init__(self, api, cfg, instruments, max_quotes, auto_exit=True):
        mdapi.CThostFtdcMdSpi.__init__(self)
        self.api = api
        self.cfg = cfg
        self.instruments = instruments
        self.max_quotes = max_quotes
        # ⚠️ 带 --td 时必须 False：行情收够就 os._exit 会把**整个进程**杀掉，
        #    交易链路（认证/登录/查询）还没跑完就被掐断 ⇒ 表现为"td 只走到发送登录"。
        self.auto_exit = auto_exit
        self.n = 0
        self.ok = False
        self.sub_ok = []
        self.sub_fail = []

    def OnFrontConnected(self):
        _log("md", f"前置已连接 {self.cfg['md']} ⇒ 发送登录")
        f = mdapi.CThostFtdcReqUserLoginField()
        f.BrokerID = self.cfg["broker"]
        f.UserID = self.cfg["user"]
        f.Password = self.cfg["pwd"]
        self.api.ReqUserLogin(f, 1)

    def OnFrontDisconnected(self, nReason):
        _log("md", f"⚠️ 前置断开，原因码 {nReason}")

    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
        _log("md", _rsp_err(pRspInfo, "登录应答 "))
        if pRspInfo and int(getattr(pRspInfo, "ErrorID", -1)) == 0:
            self.ok = True
            try:
                _log("md", f"交易日={getattr(pRspUserLogin,'TradingDay','?')}")
            except Exception:
                pass
            _log("md", f"订阅行情：{self.instruments[:8]}{' …' if len(self.instruments)>8 else ''}"
                       f"（共 {len(self.instruments)} 个）")
            # ⚠️ SWIG 的 char*[] 需要 **bytes**；传 str 列表会 TypeError: list must contain strings
            insts = [s.encode("ascii", "ignore") for s in self.instruments]
            try:
                ret = self.api.SubscribeMarketData(insts, len(insts))
                _log("md", f"SubscribeMarketData 返回 {ret}（0=成功）")
            except Exception as e:
                _log("md", f"❌ SubscribeMarketData 异常：{type(e).__name__}: {e}")

    def OnRspSubMarketData(self, pSpecificInstrument, pRspInfo, nRequestID, bIsLast):
        inst = getattr(pSpecificInstrument, "InstrumentID", "?") if pSpecificInstrument else "?"
        eid = int(getattr(pRspInfo, "ErrorID", 0) or 0) if pRspInfo else 0
        if eid == 0:
            self.sub_ok.append(inst)
        else:
            self.sub_fail.append(f"{inst}({getattr(pRspInfo,'ErrorMsg','')})")
        if bIsLast:
            _log("md", f"订阅结果：成功 {len(self.sub_ok)} 个 | 失败 {len(self.sub_fail)} 个"
                       + (f"（失败样例 {self.sub_fail[:3]}）" if self.sub_fail else ""))
            if self.sub_ok:
                _log("md", f"成功订阅：{','.join(self.sub_ok[:12])}"
                           + (" …" if len(self.sub_ok) > 12 else ""))
            else:
                _log("md", "⚠️ 一个都没订阅上 —— 合约代码可能都过期了，"
                           "用 --inst rbXXXX,MAXXX 手动指定当前挂牌合约")

    def OnRtnDepthMarketData(self, d):
        if d is None:
            return
        self.n += 1
        if self.max_quotes and self.n > self.max_quotes:
            return
        try:
            print(f"  #{self.n:<4} {str(d.InstrumentID):<10} {str(d.UpdateTime)}.{int(d.UpdateMillisec):03d} "
                  f"最新 {d.LastPrice:>9.2f}  买一 {d.BidPrice1:>9.2f}×{int(d.BidVolume1):<5} "
                  f"卖一 {d.AskPrice1:>9.2f}×{int(d.AskVolume1):<5}  "
                  f"量 {int(d.Volume):<9} 持仓 {int(d.OpenInterest):<9} 均价 {d.AveragePrice:.2f}",
                  flush=True)
        except Exception as e:
            print(f"  #{self.n} 解析行情异常: {e}", flush=True)
        if self.max_quotes and self.n >= self.max_quotes:
            if self.auto_exit:
                _log("md", f"已收到 {self.n} 条行情 ⇒ 主动退出（--quotes 可调）")
                os._exit(0)
            elif self.n == self.max_quotes:
                _log("md", f"已收到 {self.n} 条行情（行情侧不再打印；等交易链路跑完）")


# ==================================================================== 交易
class TdSpi(tdapi.CThostFtdcTraderSpi):
    def __init__(self, api, cfg, do_order, order_inst):
        tdapi.CThostFtdcTraderSpi.__init__(self)
        self.api = api
        self.cfg = cfg
        self.do_order = do_order
        self.order_inst = order_inst
        self.logged = False
        self.settled = False
        self.instruments = []
        self.ordered_ref = None
        self._order_sent = False        # 下单测试是否已开始（防止重复触发查询）
        self._order_placed = False      # ★★ 是否已经真正发过报单（防止重复报单）
        self._qry_rows = 0              # 行情查询返回的行数（用于诊断）

    def OnFrontConnected(self):
        _log("td", f"前置已连接 {self.cfg['trade']} ⇒ 发送认证（AppID={self.cfg['appid']}）")
        f = tdapi.CThostFtdcReqAuthenticateField()
        f.BrokerID = self.cfg["broker"]
        f.UserID = self.cfg["user"]
        f.AppID = self.cfg["appid"]
        f.AuthCode = self.cfg["authcode"]
        self.api.ReqAuthenticate(f, 1)

    def OnFrontDisconnected(self, nReason):
        _log("td", f"⚠️ 前置断开，原因码 {nReason}")

    def OnRspAuthenticate(self, pRspAuthenticateField, pRspInfo, nRequestID, bIsLast):
        _log("td", _rsp_err(pRspInfo, "认证应答 "))
        if pRspInfo and int(getattr(pRspInfo, "ErrorID", -1)) == 0:
            _log("td", "⇒ 发送登录")
            f = tdapi.CThostFtdcReqUserLoginField()
            f.BrokerID = self.cfg["broker"]
            f.UserID = self.cfg["user"]
            f.Password = self.cfg["pwd"]
            self.api.ReqUserLogin(f, 2)

    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
        _log("td", _rsp_err(pRspInfo, "登录应答 "))
        if pRspInfo and int(getattr(pRspInfo, "ErrorID", -1)) == 0:
            self.logged = True
            # 用会话号做 OrderRef 前缀，保证「不同会话不撞引用」（OrderRef 要求 ≤13 位字母数字）
            try:
                self._sess = str(abs(int(getattr(pRspUserLogin, "SessionID", 0))))[-6:]
            except Exception:
                self._sess = "000000"
            for k, label in [("TradingDay", "交易日"), ("SessionID", "会话"),
                             ("MaxOrderRef", "最大报单引用")]:
                try:
                    _log("td", f"  {label}={getattr(pRspUserLogin, k, '?')}")
                except Exception:
                    pass
            _log("td", "⇒ 确认结算单")
            f = tdapi.CThostFtdcSettlementInfoConfirmField()
            f.BrokerID = self.cfg["broker"]
            f.InvestorID = self.cfg["user"]
            self.api.ReqSettlementInfoConfirm(f, 3)

    def OnRspSettlementInfoConfirm(self, pSettlementInfoConfirm, pRspInfo, nRequestID, bIsLast):
        _log("td", _rsp_err(pRspInfo, "结算单确认 "))
        if pRspInfo and int(getattr(pRspInfo, "ErrorID", -1)) == 0:
            self.settled = True
            _log("td", "⇒ 查询资金账户（只读）")
            q = tdapi.CThostFtdcQryTradingAccountField()
            q.BrokerID = self.cfg["broker"]
            q.InvestorID = self.cfg["user"]
            self.api.ReqQryTradingAccount(q, 4)

    def OnRspQryTradingAccount(self, pTradingAccount, pRspInfo, nRequestID, bIsLast):
        if pTradingAccount is not None:
            _log("td", f"💰 账户权益={getattr(pTradingAccount,'Balance',float('nan')):.2f}  "
                       f"可用={getattr(pTradingAccount,'Available',float('nan')):.2f}  "
                       f"保证金={getattr(pTradingAccount,'CurrMargin',float('nan')):.2f}")
        if bIsLast:
            _log("td", "⇒ 查询持仓（只读）")
            q = tdapi.CThostFtdcQryInvestorPositionField()
            q.BrokerID = self.cfg["broker"]
            q.InvestorID = self.cfg["user"]
            self.api.ReqQryInvestorPosition(q, 5)

    def OnRspQryInvestorPosition(self, pInvestorPosition, pRspInfo, nRequestID, bIsLast):
        if pInvestorPosition is not None:
            _log("td", f"  持仓 {getattr(pInvestorPosition,'InstrumentID','?')} "
                       f"{getattr(pInvestorPosition,'PosiDirection','?')} "
                       f"{getattr(pInvestorPosition,'Position','?')} 手")
        if bIsLast:
            if self.do_order:
                self._place_test_order()
            else:
                _log("td", "✅ 交易链路验证完成（未下单）")
                os._exit(0)

    def _place_test_order(self):
        if self._order_sent:
            return
        self._order_sent = True
        inst = self.order_inst
        _log("td", f"⚠️ 下单测试：{inst} 买入 1 手，**限价挂到跌停价**（不会成交），随后撤单")
        q = tdapi.CThostFtdcQryDepthMarketDataField()
        q.InstrumentID = inst
        self.api.ReqQryDepthMarketData(q, 6)

    def OnRspQryDepthMarketData(self, d, pRspInfo, nRequestID, bIsLast):
        # ⚠️⚠️ 血泪：这个回调是**每行行情回调一次**（查询会返回一大片合约），
        #   早期版本在每行里都去下单 ⇒ **一次测试报了约 50 单**（每单 OrderRef 相同，
        #   靠柜台返回「不允许重复报单」才没真成交）。**只允许最前面那一行触发一次下单。**
        if self._order_placed or not self._order_sent:
            return
        self._qry_rows += 1
        if d is None:
            if bIsLast:
                _log("td", f"  取不到 {self.order_inst} 行情，跳过下单测试")
                os._exit(0)
            return
        # ⚠️ 查询会回一大片合约，必须**按合约代码匹配**再取值，
        #    否则拿到的是别的合约的涨跌停价（曾出现 rb2611 的"跌停价=0.5"）。
        got = str(getattr(d, "InstrumentID", "") or "")
        if got != self.order_inst:
            if bIsLast:
                _log("td", f"  ⚠️ 查询没返回 {self.order_inst} 的行情（共 {self._qry_rows} 行），跳过下单测试")
                os._exit(0)
            return
        last = float(getattr(d, "LastPrice", 0) or 0)
        px = float(getattr(d, "LowerLimitPrice", 0) or 0)
        if px <= 0 or (last > 0 and px >= last):
            _log("td", f"  ⚠️ {got} 跌停价无效（跌停={px} 最新={last}）⇒ 跳过下单测试")
            os._exit(0)
        _log("td", f"  合约={got} 最新={last} 跌停={px} ⇒ 以跌停价挂买单（绝不会立即成交）")
        o = tdapi.CThostFtdcInputOrderField()
        o.BrokerID = self.cfg["broker"]
        o.InvestorID = self.cfg["user"]
        o.InstrumentID = self.order_inst
        o.OrderPriceType = tdapi.THOST_FTDC_OPT_LimitPrice
        o.Direction = tdapi.THOST_FTDC_D_Buy
        o.CombOffsetFlag = tdapi.THOST_FTDC_OF_Open
        o.CombHedgeFlag = tdapi.THOST_FTDC_HF_Speculation
        o.LimitPrice = px
        o.VolumeTotalOriginal = 1
        o.TimeCondition = tdapi.THOST_FTDC_TC_GFD
        o.VolumeCondition = tdapi.THOST_FTDC_VC_AV
        o.MinVolume = 1
        o.ContingentCondition = tdapi.THOST_FTDC_CC_Immediately
        o.ForceCloseReason = tdapi.THOST_FTDC_FCC_NotForceClose
        o.IsAutoSuspend = 0
        o.UserForceClose = 0
        self.ordered_ref = f"{getattr(self,'_sess','000000')}_{int(time.time())%100000:05d}"
        o.OrderRef = self.ordered_ref
        self._order_placed = True          # ★ 先置位再发单，杜绝任何重复路径
        ret = self.api.ReqOrderInsert(o, 7)
        _log("td", f"  报单请求已发出，返回 {ret}（0=已接受；错误码会走 OnRspOrderInsert / OnErrRtnOrderInsert）")

    def OnRspOrderInsert(self, pInputOrder, pRspInfo, nRequestID, bIsLast):
        _log("td", _rsp_err(pRspInfo, "报单应答 "))

    def OnErrRtnOrderInsert(self, pInputOrder, pRspInfo):
        _log("td", _rsp_err(pRspInfo, "报单错误 "))

    def OnRtnOrder(self, pOrder):
        if pOrder is None:
            return
        _log("td", f"  委托回报：{getattr(pOrder,'InstrumentID','?')} "
                   f"状态={getattr(pOrder,'OrderStatus','?')} 已成交={getattr(pOrder,'VolumeTraded','?')}")
        # 撤单
        if self.ordered_ref and getattr(pOrder, "OrderRef", "") == self.ordered_ref:
            a = tdapi.CThostFtdcInputOrderActionField()
            a.BrokerID = self.cfg["broker"]
            a.InvestorID = self.cfg["user"]
            a.InstrumentID = self.order_inst
            a.OrderRef = self.ordered_ref
            a.FrontID = getattr(pOrder, "FrontID", 0)
            a.SessionID = getattr(pOrder, "SessionID", 0)
            a.ActionFlag = tdapi.THOST_FTDC_AF_Delete
            ret = self.api.ReqOrderAction(a, 8)
            _log("td", f"  撤单请求已发出，返回 {ret}")
            self.ordered_ref = None

    def OnRspOrderAction(self, pInputOrderAction, pRspInfo, nRequestID, bIsLast):
        _log("td", _rsp_err(pRspInfo, "撤单应答 "))
        _log("td", "✅ 下单→撤单 往返完成，链路可用")
        os._exit(0)


# ==================================================================== 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="724", choices=list(SIMNOW_FRONTS), help="724(默认) 或 1")
    ap.add_argument("--inst", default=None, help="逗号分隔的合约代码；不给则按当前日期生成")
    ap.add_argument("--user", default=None, help="覆盖登录账号（账户里有两个候选：用户账号 A… / 6 位投资者代码）")
    ap.add_argument("--quotes", type=int, default=8, help="收到多少条行情后退出（0=不退出）")
    ap.add_argument("--td", action="store_true", help="额外连交易前置（只读查询，不下单）")
    ap.add_argument("--order", action="store_true", help="★ 做一次限价单往返（需同时给 --yes-i-know）")
    ap.add_argument("--yes-i-know", dest="confirm", action="store_true", help="确认知道这会真的报单（SimNow 模拟盘）")
    args = ap.parse_args()

    # ---- 账号 ----
    try:
        import simnow_account as acc
    except ImportError:
        raise SystemExit("找不到 simnow_account.py —— 请把 simnow_account_template.py 复制一份并填入 PASSWORD")
    if not acc.PASSWORD:
        raise SystemExit("simnow_account.py 里的 PASSWORD 还是空的 —— 请填上 SimNow 登录密码再跑")

    cfg = {
        "broker": acc.BROKER_ID, "user": (args.user or acc.USER_ID), "pwd": acc.PASSWORD,
        "appid": acc.APP_ID, "authcode": acc.AUTH_CODE,
        **SIMNOW_FRONTS[args.env],
    }

    # ---- ★★ 硬闸门：任何前置都必须命中白名单 ----
    for k in ("trade", "md"):
        host = cfg[k].split("//")[-1].split(":")[0]
        if host not in ALLOWED_HOSTS:
            raise SystemExit(f"⛔ 拒绝连接：{k} 前置 {cfg[k]} 不在 SimNow 白名单 {ALLOWED_HOSTS} 内。"
                             f"\n本脚本设计上只能连模拟盘，防止误连实盘。")
    if args.order and not args.confirm:
        raise SystemExit("⛔ --order 会真的报单（即使是模拟盘）。确认请再加 --yes-i-know。")

    os.makedirs(FLOW_DIR, exist_ok=True)
    # ⚠️ CTP 要求流文件目录**必须已存在**，否则 API 直接抛
    #   RuntimeError: can not open CFlow file ...（在 ThostFtdcUserApiImplBase.cpp）
    # 所以 md/ 与 td/ 两个子目录都要先建出来。
    md_flow = os.path.join(FLOW_DIR, "md")
    td_flow = os.path.join(FLOW_DIR, "td")
    os.makedirs(md_flow, exist_ok=True)
    os.makedirs(td_flow, exist_ok=True)
    print("=" * 78)
    print(f"SimNow 最小接入  ｜ 环境={args.env}  ｜ 账号={cfg['user']} BrokerID={cfg['broker']}")
    print(f"  行情前置 {cfg['md']}")
    if args.td:
        print(f"  交易前置 {cfg['trade']}")
    print(f"  ⚠️ 白名单硬闸门已生效：只可能连 {ALLOWED_HOSTS}")
    print("=" * 78, flush=True)

    # ---- 行情 ----
    cand = ([s.strip() for s in args.inst.split(",") if s.strip()] if args.inst
            else _default_insts())
    _log("sys", f"本次候选合约 {len(cand)} 个（按当前日期生成）；订阅不到的不报错、只是没数据")
    md = mdapi.CThostFtdcMdApi.CreateFtdcMdApi(md_flow + os.sep, False, False)
    mspi = MdSpi(md, cfg, [s.strip() for s in cand if s.strip()], args.quotes,
                 auto_exit=(not args.td))
    md.RegisterFront(cfg["md"])
    md.RegisterSpi(mspi)
    # ⚠️ 行情 API **没有** SubscribePrivateTopic/PublicTopic（那是交易 API 才有的）——
    #    加了会 AttributeError: 'CThostFtdcMdApi' object has no attribute ...
    md.Init()
    _log("md", f"Init() 已调用，等待前置连接（最多 {TIMEOUT_S}s）…")

    if args.td:
        td = tdapi.CThostFtdcTraderApi.CreateFtdcTraderApi(td_flow + os.sep)
        tspi = TdSpi(td, cfg, args.order, (cand[0] if cand else "rb2601"))
        td.RegisterFront(cfg["trade"])
        td.RegisterSpi(tspi)
        td.SubscribePrivateTopic(tdapi.THOST_TERT_QUICK)
        td.SubscribePublicTopic(tdapi.THOST_TERT_QUICK)
        td.Init()
        _log("td", f"Init() 已调用，等待前置连接（最多 {TIMEOUT_S}s）…")

    t0 = time.time()
    try:
        while True:
            time.sleep(0.5)
            if time.time() - t0 > TIMEOUT_S and not args.td:
                _log("sys", f"⚠️ {TIMEOUT_S}s 内未收到任何行情 ⇒ 退出。"
                            f"\n  排查：① 先用 --env 1 换一套环境 ② 换一组前置端口（30002/30003）"
                            f"\n  ③ 确认 --inst 里的合约代码在当前挂牌（CZCE 是 3 位码，如 MA601）")
                os._exit(1)
            if args.td and time.time() - t0 > TIMEOUT_S * 2:
                _log("sys", "⚠️ 超时退出（交易链路未在预期时间内完成）")
                os._exit(1)
    except KeyboardInterrupt:
        _log("sys", "手动中断，退出")
        os._exit(0)


if __name__ == "__main__":
    main()
