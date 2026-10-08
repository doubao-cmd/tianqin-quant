# -*- coding: utf-8 -*-
"""
engine 实盘防护层（2026-10-08 新增）
========================================================================
【为什么有这个文件】
审查 `lib/engine.py`（6540 行）发现：引擎里**完全没有**账户级回撤熔断与
进程守护重连（`fuse`/`halted`/`熔断`/`重连` 全文零命中）。而 06_MA13 的
`ma13_live_multi.py` 已经把这套做出来了（并踩过坑）。本模块把ma13 验证过的
实现搬进 engine，供所有 `run_live_*` 复用。

★ 搬移原则：**逐字照抄语义，不改策略逻辑**。本模块只管"要不要继续开仓 /
断线怎么办"，不碰任何信号或出场判断，符合规则 4「每次只改动一处逻辑」。

【engine 与ma13 的关键差异（搬移时必须注意）】
ma13 的对账方向是「**云端 → 覆盖内存**」（云端是唯一真相源），
engine 是「**内存 want → 问云端**」（want 是主变量）。
⇒ 所以 engine **不需要** ma13 的 `FIX_WAIT_SEC` 撮合窗口：刚下单时 want 已等于
   目标手数，`want == pos_now` 天然不会判定为不一致，不存在"把刚开的仓误当
   成外部遗留持仓"的问题。硬搬过来反而是画蛇添足。
⇒ 但"断线后 want 与真实持仓脱钩"的风险 engine 也有 ⇒ 熔断/守护同样必要。

【能力】
1. `Fuse`      —— 账户级回撤熔断，闩锁跨会话持久化，需人工复位
2. `ReconnectLoop` —— 断线自动重连外层循环
3. `install()`  —— 一行接入到任意 run_live_* 入口
"""
import os
import io
import json
import time
import threading

# 账户级回撤熔断线（自峰值回撤）。纯灾难保护阀，**不是策略参数**。
# ⚠️ 数值待用真数据定稿（SOP 欠账#1）。20% 与 ma13 实测值一致。
DD_FUSE = 0.20

RECONNECT_WAIT = 10       # 断线后等待秒数（避免撞上限流）
WAIT_DEADLINE = 30        # wait_update 的 deadline（秒）——项目铁律：必须传，否则休市永久阻塞


# ---------------------------------------------------------------- 弹窗
def popup(title, msg):
    """Windows 原生弹窗（独立线程，绝不阻塞交易主循环）。
    无窗口环境/非 Windows 下静默降级为只在控制台响铃。"""
    print("\a" + f"[弹窗] {title}: {msg}", flush=True)

    def _go():
        try:
            import ctypes
            # 48=MB_ICONWARNING, 0x40000=MB_SYSTEMMODAL（置顶）
            ctypes.windll.user32.MessageBoxW(0, msg, title, 48 | 0x40000)
        except Exception:
            pass                      # 弹不出来不能影响交易
    threading.Thread(target=_go, daemon=True, name="popup").start()


# ---------------------------------------------------------------- 熔断
class Fuse(object):
    """账户级回撤熔断。语义：自峰值回撤 ≥ DD_FUSE → 闩死，禁止新开仓。

    ★ 与 ma13 的差异：`threshold` 可按入口覆盖（不同策略风险偏好不同），
      默认统一 0.20。**不引入新参数择优**，只是把数值提到可配置。

    ★ peak初值语义（照抄 ma13 的教训）：peak 必须跟着**真实账户余额**走。
      若停在 INIT（20 万）而云端账户实际 100 万，则账户跌到 96 万就会被判
      「回撤 380%」而误触发 ⇒ 所以首次 eval 会把 peak 抬到真实水平，这是正确的。
    """

    def __init__(self, state_dir, init_balance=200000.0,
                 name="engine", threshold=DD_FUSE, rec=None):
        self.name = name
        self.threshold = float(threshold)
        self.rec = rec                  # 可选：_LiveRecorder，用于落CIRCUIT 台账
        self.path = os.path.join(state_dir, "熔断状态.json")
        self.peak = float(init_balance)
        self.halted = False
        self._load()

    # ---- 持久化（峰值与闩锁都跨会话，闩死重启也不自动复位）----
    def _load(self):
        try:
            with io.open(self.path, "r", encoding="utf-8") as f:
                st = json.load(f)
            self.peak = max(self.peak, float(st.get("peak") or self.peak))
            self.halted = bool(st.get("halted"))
        except Exception:
            pass                      # 无文件/损坏 = 全新状态

    def save(self):
        try:
            with io.open(self.path, "w", encoding="utf-8") as f:
                json.dump({"peak": self.peak, "halted": self.halted,
                           "threshold": self.threshold,
                           "update": time.strftime("%Y-%m-%d %H:%M:%S")},
                          f, ensure_ascii=False)
        except Exception:
            pass                      # 状态存不上不能影响交易

    # ---- 判定（抽成方法是为了让演练走**与真实触发完全同一条代码路径**）----
    def eval(self, bal, drill=False):
        """返回 True=本次刚好触发（边沿）。NaN/非正不计。

        ★ drill=True 是**演练**：走完全相同的算式与弹窗，但**不闩死、不落盘**，
          所以演练完状态文件仍是干净的（否则一次演练就把程序锁死在只出不进，
          还得人工删文件才能恢复）。
        """
        if not (bal == bal) or bal <= 0:
            return False
        if bal > self.peak:
            self.peak = bal
            if not drill:
                self.save()             # 新峰值即落盘
        if self.halted or bal > self.peak * (1 - self.threshold):
            return False
        if not drill:
            self.halted = True
            self.save()                 # 闩锁立即落盘
        tag = "【演练】" if drill else ""
        msg = (f"{tag}回撤熔断触发：权益{bal:.0f} 自峰值{self.peak:.0f} "
               f"回撤≥{self.threshold:.0%} → 停止新开仓(持仓照常出场) 恢复需人工复位")
        if self.rec is not None:
            self.rec.event("CIRCUIT", msg)
        print(f"[{self.name}][CIRCUIT] ⚠️ {msg}", flush=True)
        popup(f"{self.name}：⚠️ 回撤熔断触发" + ("（演练）" if drill else ""),
              f"权益 {bal:.0f} 自峰值 {self.peak:.0f} 回撤达到 {self.threshold:.0%}。\n\n"
              "已自动执行：停止一切新开仓；已有持仓照常按止损/时间止损出场。\n"
              "程序不会自动恢复，请尽快人工介入。\n\n"
              f"复位方法（确认后）：\n"
              f"  1. 关闭本程序窗口\n"
              f"  2. 删除文件 {self.path}\n"
              f"  3. 重新启动")
        return True

    def blocked(self):
        """开仓闸门：已闩死则返回 True，调用方**必须**用它挡住新开仓。"""
        return bool(self.halted)

    def announce_if_latched(self):
        """启动时若发现未复位的闩锁，弹窗提示（照抄 ma13 行为）。"""
        if not self.halted:
            return
        popup(f"{self.name}：回撤熔断未复位",
               "上一次运行触发了回撤熔断，本次进程将以【只出不进】模式运行"
               "（已有持仓照常出场，禁止一切新开仓）。\n\n"
               f"历史峰值: {self.peak:.0f}\n\n"
               f"确认复位方法：\n"
               f"  1. 关闭本程序窗口\n"
               f"  2. 删除文件 {self.path}\n"
               f"  3. 重新启动")
        if self.rec is not None:
            self.rec.event("CIRCUIT", "启动时检测到未复位的熔断闩锁 → 只出不进模式")


# ---------------------------------------------------------------- 重连
class ReconnectLoop(object):
    """断线自动重连外层循环（照抄 ma13_live_multi 的守护段）。

    ★ 项目铁律：`wait_update()` **必须传 deadline**，否则休市/无行情推送时
      会永久阻塞 ⇒ 主循环停摆、对账与心跳全部不再执行（看起来像"启动了却
      什么都没做"）。ma13 在 2026-10-08 13:25 午休时实测踩过这个坑。
    """

    def __init__(self, connect_fn, on_event=None, name="engine"):
        self.connect_fn = connect_fn# 无参可调用，返回 api
        self.on_event = on_event      # 可选：事件回调（写台账）
        self.name = name

    def run(self, body, reconnect_wait=RECONNECT_WAIT,
            wait_deadline=WAIT_DEADLINE, drill_fuse=False):
        """body(api) 是单次会话的主循环体；本方法负责无限重连直到 KeyboardInterrupt。

        约定：body 内部自己处理 KeyboardInterrupt（人工 Ctrl+C 应正常退出不重连）。
        """
        api = self.connect_fn()
        while True:
            try:
                while True:
                    api.wait_update(deadline=time.time() + wait_deadline)
                    body(api)
            except KeyboardInterrupt:
                raise                     # 人工 Ctrl+C：正常退出，不重连
            except Exception as e:        # 断线/被踢/网络异常：记账后重连
                msg = f"{type(e).__name__}: {e}"
                print(f"[{self.name}][守护] 连接异常，{reconnect_wait}s 后自动重连：{msg}",
                      flush=True)
                if self.on_event is not None:
                    try:
                        self.on_event("RECONNECT", f"连接异常自动重连 | {msg[:120]}")
                    except Exception:
                        pass
            finally:
                try:
                    api.close()
                except Exception:
                    pass
            time.sleep(reconnect_wait)
            api = self.connect_fn()


# ---------------------------------------------------------------- 整会话重启
class RestartLoop(object):
    """engine 专用的守护：异常后**整体重跑** live 入口，而不是原地重连。

    ★ 为什么 engine 用「重启整个入口」而不是 `ReconnectLoop` 的原地重连：
      engine 的每个 `run_live_*` 在函数开头**自建全部 serials**
      （klines/quote/account/trade_log/orders/TargetPosTask），这些对象在断线后
      全部失效。原地重连必须把7 个对象的重新获取 + 止损补挂 + 状态恢复逐条抄一遍，
      漏一条就是静默失效（项目已吃过「补挂漏了 ⇒ 持仓裸奔」的亏）。
      而「重跑整个入口」天然走一遍完整的启动流程，**漏不掉**。
      ma13 用原地重连是因为它的 connect_fn 封装了全部 serials 的重建。

    ★ 跨重启不丢的东西（重启前已确认）：
      - 止损线/时间止损计数：engine 已落`_boll_state.json`，重跑时精确恢复
      - 熔断峰值/闩锁：`熔断状态.json`，重跑时恢复
      - 目标仓位：入口开头 `want = api.get_position(...).pos` 以真实持仓为准
    """

    def __init__(self, session_fn, name="engine", on_event=None,
                 reconnect_wait=RECONNECT_WAIT, max_restart=0, cleanup=None):
        self.session_fn = session_fn      # 无参可调用：跑完一次完整会话
        self.name = name
        self.on_event = on_event
        self.reconnect_wait = reconnect_wait
        self.max_restart = max_restart    # 0 = 无限重试；>0 = 连续失败上限后放弃
        self.cleanup = cleanup            # 可选：每次会话结束（含异常）后调用，
                                          # 用来api.close()—— 不关的话重启会累积连接

    def run(self):
        n = 0
        while True:
            try:
                self.session_fn()         # 正常跑完（只有人为收尾才会走到这）
                return
            except KeyboardInterrupt:
                raise                     # 人工 Ctrl+C：正常退出，不重启
            except Exception as e:
                n += 1
                msg = f"{type(e).__name__}: {e}"
                print(f"[{self.name}][守护] 会话异常退出，{self.reconnect_wait}s 后"
                      f"重启整个实盘（第 {n} 次）：{msg}", flush=True)
                if self.on_event is not None:
                    try:
                        self.on_event("RECONNECT",
                                      f"会话异常自动重启 | {msg[:120]}")
                    except Exception:
                        pass
                if self.max_restart and n >= self.max_restart:
                    print(f"[{self.name}][守护] 已连续失败 {n} 次，达到上限，"
                          f"放弃自动重启。请人工检查后手动启动。", flush=True)
                    raise
            finally:
                # ★ 无论正常/异常都收尾：关掉本会话的 api。
                #   不关的话每次重启都在进程里留一个 TqApi（连接 + 线程 + 订阅），
                #   连续重启几次后会出现「多个连接同时推行情」的怪现象。
                if self.cleanup is not None:
                    try:
                        self.cleanup()
                    except Exception:
                        pass
            time.sleep(self.reconnect_wait)
