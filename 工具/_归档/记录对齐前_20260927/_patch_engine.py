# -*- coding: utf-8 -*-
"""给 run_live_align（02）与 run_live_swing（05）接入 _LiveRecorder。

只做「记录层」对齐，不碰任何交易判断。每处替换都带断言（期望命中次数），
任一处不符立刻抛错、不落盘 —— 宁可失败，不可误改。
"""
import ast
import shutil
import sys

P = r"C:\Users\cc\Desktop\天勤量化\lib\engine.py"
s = open(P, encoding="utf-8").read()
原始 = s


def rep(seg, old, new, cnt=1, tag=""):
    n = seg.count(old)
    if n != cnt:
        raise SystemExit(f"[{tag}] 期望命中 {cnt} 处，实际 {n} 处 —— 已中止")
    return seg.replace(old, new)


# ==================================================================
# 一、run_live_align（02 均线排列）
# ==================================================================
i = s.index("def run_live_align(")
j = s.index("def swing_decision(")
align = s[i:j]

# 1) 签名
align = rep(align,
            "                   pullback_tol_atr=0.5, stop_atr=0.0, ledger_dir=None,\n"
            "                   hist_exit=0):\n",
            "                   pullback_tol_atr=0.5, stop_atr=0.0, ledger_dir=None,\n"
            "                   hist_exit=0, record_dir=None, record_heartbeat_sec=300):\n",
            1, "align/signature")

# 2) docstring
align = rep(align,
            '    信号质量过滤（adx_filter / entry_ext_atr）同样与回测同口径，默认全关。"""\n',
            '    信号质量过滤（adx_filter / entry_ext_atr）同样与回测同口径，默认全关。\n'
            '    运行记录（2026-09-27 补齐，与 run_live 逐字同口径）：\n'
            '        record_dir=None（默认）-> 不记录，行为不变。\n'
            '        record_dir=目录      -> 落两个 CSV（成交流水 + 运行心跳），\n'
            '                               只落盘、不参与任何交易判断。"""\n',
            1, "align/docstring")

# 3) _LiveRecorder 初始化（want 已定义、主循环开始之前）
锚 = '    print("  盘中盯盘看这里：每笔真实成交会标 *，并定时刷新盘面快照\\n")\n'
align = rep(align, 锚, 锚 + '''
    # ---- 运行记录（2026-09-27 补齐）：只落盘，不参与任何交易判断 ----
    #   01/03/04 走 run_live 一直有这一层；02 走 run_live_align，此前没有 ——
    #   结果是 02 的 12 个模拟盘跑起来「看不到心跳」，巡检时无法区分
    #   「程序没跑」和「在跑但没信号」。这里把口径补齐，与 run_live 一致。
    #   record_dir=None 时 _rec.on 为 False，下面所有 _rec.xxx() 全是空操作。
    _rec = _LiveRecorder(record_dir, name, record_heartbeat_sec)
    if _rec.on:
        # 手续费：优先用 tqsdk 报的值（TqSim 实际就按它扣），拿不到再退回配置估值
        try:
            _comm = float(quote.commission)
        except Exception:
            _comm = -1.0
        if not (_comm == _comm) or _comm < 0:      # NaN / 非法
            _comm = float(config_symbols.COST.get(symbol_key, 0.0))
        _rec.start(f"合约 {cur_sym} | 下单开关 {'开' if allow_trade else '关'} | "
                   f"MA{'/MA'.join(str(p) for p in ma_periods)}"
                   f" | 固定 {volume} 手 | 手续费 {_comm:g} 元/手/单边")
        _rec.heartbeat(_dt_str(klines.iloc[-2]["datetime"]), float(quote.last_price),
                       int(api.get_position(cur_sym).pos), int(want), force=True)

        def _rec_bye(_r=_rec):
            _r.stop("程序退出（正常结束或 Ctrl+C）")
            _r.close()
        atexit.register(_rec_bye)
        print(f" 运行记录 -> {record_dir}")
        print("   （成交流水 + 运行心跳；台账用 工具/模拟盘台账.py 生成）")
''', 1, "align/recorder")

# 4) 成交落盘
成交锚 = ("                    except Exception as _le:\n"
          '                        print(f"  [!] 流水单写入失败（不影响交易）：{_le}", flush=True)\n')
align = rep(align, 成交锚, 成交锚 + '''                if _rec.on:
                    try:
                        _rec.fill(d, o, int(t.volume), float(t.price), 合约)
                    except Exception:
                        pass
''', 1, "align/fill")

# 5) 信号落盘（开/平仓判定之后、心跳之前）
信号锚 = ('                    print(f"            （观察模式，未下单）")\n'
          "\n"
          "        # ---- 4) 盘面心跳：定时刷新快照，确认程序没卡死 ----\n")
align = rep(align, 信号锚, '''                    print(f"            （观察模式，未下单）")
                # 运行记录：信号本身落盘。参考价取「信号根已收盘那根的收盘价」——
                # 与回测决策基准是同一个价，事后才能用它算真实滑点。
                # 平仓信号的手数取当时实际持仓（want 此时为 0，不能拿来记）。
                _rec.signal(
                    {1: "多", -1: "空"}.get(open_side if do_open else 0, "平"),
                    int(abs(want)) if do_open
                    else int(abs(api.get_position(cur_sym).pos)),
                    float(last_close), _dt_str(klines.iloc[-2]["datetime"]),
                    float(quote.last_price), atr_v,
                    tag + ("｜已下单" if allow_trade else "｜观察模式未下单"))

        # ---- 4) 盘面心跳：定时刷新快照，确认程序没卡死 ----
''', 1, "align/signal")

# 6a) 止损落盘：百分比止损
锚_pct = ('                    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 触及 "\n'
          '                          f"{stop_pct*100:.0f}% 止损，平仓")\n'
          "                    continue\n")
align = rep(align, 锚_pct, '''                    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 触及 "
                          f"{stop_pct*100:.0f}% 止损，平仓")
                    _rec.stoploss("多" if net_pos > 0 else "空", last_close * (1 - stop_pct)
                                  if net_pos > 0 else last_close * (1 + stop_pct),
                                  float(quote.last_price), f"百分比止损 {stop_pct*100:.0f}%")
                    continue
''', 1, "align/stoploss-pct")

# 6b) 止损落盘：固定 ATR
锚_atr = ('                        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {stop_atr:g}ATR固定止损 -> "\n'
          "                              f\"{'平多' if _hit_side > 0 else '平空'}\"\n"
          "                              f\" | 触发价 {stop_line:.2f} | 开仓 {stop_st['entry']:.2f}\"\n"
          "                              f\" 开仓ATR {stop_st['entry_atr']:.2f}\")\n"
          "                        continue\n")
align = rep(align, 锚_atr, 锚_atr.replace("                        continue\n", '''                        _rec.stoploss("多" if _hit_side > 0 else "空", stop_line,
                                      float(quote.last_price),
                                      f"固定 {stop_atr:g}ATR 止损"
                                      f"（开仓 {stop_st['entry']:.2f}"
                                      f" ATR {stop_st['entry_atr']:.2f}）")
                        continue
'''), 1, "align/stoploss-atr")

# 6c) 止损落盘：动态 ATR（仅多头）
锚_dyn = ('                        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ATR动态止损 -> 平多"\n'
          "                              f\" | 触发价 {stop_line:.2f} | 开仓 {stop_st['entry']:.2f}\"\n"
          "                              f\" 最高 {stop_st['ext_high']:.2f} ATR {atr_v:.2f}\")\n"
          "                        continue\n")
align = rep(align, 锚_dyn, 锚_dyn.replace("                        continue\n", '''                        _rec.stoploss("多", stop_line, float(quote.last_price),
                                      f"动态ATR止损（开仓 {stop_st['entry']:.2f}"
                                      f" 最高 {stop_st['ext_high']:.2f} ATR {atr_v:.2f}）")
                        continue
'''), 1, "align/stoploss-dyn")

# 7) 心跳落盘
心跳锚 = ('            print(f"[{datetime.now():%H:%M:%S}] 盘面 现价 {_px_h:.2f} "\n'
          "                  f\"| MA{ma_periods[0]}={mv[0]:.2f} \"\n"
          "                      f\"MA{ma_periods[-1]}={mv[-1]:.2f} \"\n"
          "                      f\"| 持仓 {int(pos)}手 目标 {want} \"\n"
          '                      f"| 可用 {account.available:,.0f}")\n')
align = rep(align, 心跳锚, 心跳锚 + '''            _rec.heartbeat(_dt_str(klines.iloc[-2]["datetime"]),
                           _px_h, pos, want)
''', 1, "align/heartbeat")

s = s[:i] + align + s[j:]

# ==================================================================
# 二、run_live_swing（05 摆动线）
# ==================================================================
i2 = s.index("def run_live_swing(")
j2 = s.index("# 均线排列升级版", i2)
sw = s[i2:j2]

# 1) 签名
sw = rep(sw,
         "                   atr_period=14, stop_atr=0.0, ledger_dir=None):\n",
         "                   atr_period=14, stop_atr=0.0, ledger_dir=None,\n"
         "                   record_dir=None, record_heartbeat_sec=300):\n",
         1, "swing/signature")

# 2) docstring
sw = rep(sw,
         "    触发后空仓等下一次结构翻转（方案A）；重启后基准丢失 -> 止损不生效（与旧live一致）。\n"
         '    """\n',
         "    触发后空仓等下一次结构翻转（方案A）；重启后基准丢失 -> 止损不生效（与旧live一致）。\n"
         "    运行记录（2026-09-27 补齐，与 run_live 逐字同口径）：\n"
         "        record_dir=None（默认）-> 不记录，行为不变。\n"
         "        record_dir=目录      -> 落两个 CSV（成交流水 + 运行心跳），\n"
         "                               只落盘、不参与任何交易判断。\n"
         '    """\n',
         1, "swing/docstring")

# 3) _LiveRecorder 初始化
锚3 = '    print("  盘中盯盘看这里：每笔真实成交会标 *，并定时刷新盘面快照\\n")\n'
sw = rep(sw, 锚3, 锚3 + '''
    # ---- 运行记录（2026-09-27 补齐）：只落盘，不参与任何交易判断 ----
    #   与 run_live（01/03/04）逐字同口径；05 走 run_live_swing，此前没有这一层 ——
    #   结果是 05 的 12 个模拟盘跑起来「看不到心跳」，巡检时无法区分
    #   「程序没跑」和「在跑但没信号」。
    #   record_dir=None 时 _rec.on 为 False，下面所有 _rec.xxx() 全是空操作。
    _rec = _LiveRecorder(record_dir, name, record_heartbeat_sec)
    if _rec.on:
        # 手续费：优先用 tqsdk 报的值（TqSim 实际就按它扣），拿不到再退回配置估值
        try:
            _comm = float(quote.commission)
        except Exception:
            _comm = -1.0
        if not (_comm == _comm) or _comm < 0:      # NaN / 非法
            _comm = float(config_symbols.COST.get(symbol_key, 0.0))
        if risk_pct > 0:
            _ptxt = (f"波动率预算 {risk_pct:g}% -> {risk_hand_min}~{risk_hand_max} 手"
                     f"（算手数权益 {_risk_equity_text(risk_equity, None)}）")
        else:
            _ptxt = f"固定 {volume} 手"
        _rec.start(f"合约 {cur_sym} | 下单开关 {'开' if allow_trade else '关'} | "
                   f"摆动线 | {_ptxt} | 手续费 {_comm:g} 元/手/单边")
        _rec.heartbeat(_dt_str(klines.iloc[-2]["datetime"]), float(quote.last_price),
                       int(api.get_position(cur_sym).pos), int(want), force=True)

        def _rec_bye(_r=_rec):
            _r.stop("程序退出（正常结束或 Ctrl+C）")
            _r.close()
        atexit.register(_rec_bye)
        print(f" 运行记录 -> {record_dir}")
        print("   （成交流水 + 运行心跳；台账用 工具/模拟盘台账.py 生成）")
''', 1, "swing/recorder")

# 4) 成交落盘
sw = rep(sw, 成交锚, 成交锚 + '''                if _rec.on:
                    try:
                        _rec.fill(d, o, int(t.volume), float(t.price), 合约)
                    except Exception:
                        pass
''', 1, "swing/fill")

# 5a) 止损平仓信号
sw = rep(sw, '''                    if allow_trade:
                        tp.set_target_volume(0)
                        print(f"            > 已下达目标仓位 0 手，等待撮合...")
                    else:
                        print(f"            （观察模式，未下单）")

            new_state, action, line = swing_decision(state, close, hh2, ll2)
''', '''                    if allow_trade:
                        tp.set_target_volume(0)
                        print(f"            > 已下达目标仓位 0 手，等待撮合...")
                    else:
                        print(f"            （观察模式，未下单）")
                    _rec.signal("平", int(abs(api.get_position(cur_sym).pos)),
                                close, _dt_str(klines.iloc[-2]["datetime"]),
                                float(quote.last_price), entry_atr_s,
                                f"{stop_atr:g}×ATR固定止损 平{_dir_cn}"
                                + ("｜已下单" if allow_trade else "｜观察模式未下单"))
                    _rec.stoploss(_dir_cn, stop_line, float(quote.last_price),
                                  f"固定 {stop_atr:g}×ATR 止损"
                                  f"（开仓基准 {entry_px:.2f} ATR {entry_atr_s:.2f}）")

            new_state, action, line = swing_decision(state, close, hh2, ll2)
''', 1, "swing/signal-stop")

# 5b) 开多信号
sw = rep(sw, '''                if allow_trade:
                    tp.set_target_volume(want)
                    print(f"            > 已下达目标仓位 {want} 手，等待撮合...")
                else:
                    print(f"            （观察模式，未下单）")
            elif action == 'open_short':
''', '''                if allow_trade:
                    tp.set_target_volume(want)
                    print(f"            > 已下达目标仓位 {want} 手，等待撮合...")
                else:
                    print(f"            （观察模式，未下单）")
                _rec.signal("多", int(abs(want)), close,
                            _dt_str(klines.iloc[-2]["datetime"]),
                            float(quote.last_price), atr_v,
                            tag + ("｜已下单" if allow_trade else "｜观察模式未下单"))
            elif action == 'open_short':
''', 1, "swing/signal-long")

# 5c) 开空信号
sw = rep(sw, '''                if allow_trade:
                    tp.set_target_volume(want)
                    print(f"            > 已下达目标仓位 {want} 手，等待撮合...")
                else:
                    print(f"            （观察模式，未下单）")

        # ---- 盘面心跳：无条件打印，肉眼判中断 ----
''', '''                if allow_trade:
                    tp.set_target_volume(want)
                    print(f"            > 已下达目标仓位 {want} 手，等待撮合...")
                else:
                    print(f"            （观察模式，未下单）")
                _rec.signal("空", int(abs(want)), close,
                            _dt_str(klines.iloc[-2]["datetime"]),
                            float(quote.last_price), atr_v,
                            tag + ("｜已下单" if allow_trade else "｜观察模式未下单"))

        # ---- 盘面心跳：无条件打印，肉眼判中断 ----
''', 1, "swing/signal-short")

# 7) 心跳落盘
sw = rep(sw, '''            print(f"[{datetime.now():%H:%M:%S}] 盘面 现价 {px:.2f} "
                  f"| 状态 {'多头' if state == 1 else '空头' if state == -1 else '未定'} "
                  f"| 止盈线 {_h if state == -1 else _l:.1f} "
                   f"| 持仓 {int(pos)}手 目标 {want} "
                   f"| 可用 {account.available:,.0f}")
''', '''            print(f"[{datetime.now():%H:%M:%S}] 盘面 现价 {px:.2f} "
                  f"| 状态 {'多头' if state == 1 else '空头' if state == -1 else '未定'} "
                  f"| 止盈线 {_h if state == -1 else _l:.1f} "
                   f"| 持仓 {int(pos)}手 目标 {want} "
                   f"| 可用 {account.available:,.0f}")
            _rec.heartbeat(_dt_str(klines.iloc[-2]["datetime"]), px, pos, want)
''', 1, "swing/heartbeat")

s = s[:i2] + sw + s[j2:]

# ==================================================================
ast.parse(s)                      # 语法不过就别写盘
if s == 原始:
    raise SystemExit("没有任何改动，异常")
open(P, "w", encoding="utf-8", newline="\n").write(s)
print("engine.py 已改写：run_live_align + run_live_swing 接入 _LiveRecorder")
print("行数 %d -> %d" % (原始.count("\n") + 1, s.count("\n") + 1))
