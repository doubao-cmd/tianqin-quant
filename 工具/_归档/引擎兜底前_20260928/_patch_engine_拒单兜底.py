# -*- coding: utf-8 -*-
"""给 lib/engine.py 的 5 个回测函数加「下单被拒」兜底。

背景（2026-09-28 深夜实测）：
  03 均值回归 / 纯碱，新练习区间 2020-01-01~2022-12-31，跑到 2020-10-20 时
  账户权益只剩 9,100 元（20 万本金亏掉 95.5%：成本 90,640 + 已实现亏损 100,260），
  此后每次想开仓都被柜台拒：

      Exception: 遇到错单: CZCE.SA101 SELL OPEN 6手 1629.000000 开仓资金不足

  引擎 5 个回测函数的异常口只有 `except BacktestFinished:`，接不住这个普通
  Exception ⇒ 直接穿透 ⇒ 整个回测白跑、**一份报告都不出**。

本补丁把 5 处的
      except BacktestFinished:
  改成
      except Exception as <自定名>: + 分流
  语义：
      · BacktestFinished            -> 正常跑完，行为逐字不变
      · 资金不足 / 遇到错单          -> 提前结束，照样走原有收尾（出报告 + 存快照），
                                        并把「报告只覆盖到中断那一刻」显著打出来
      · 其它任何异常                -> 原样 raise，绝不掩盖

⚠️ 变量名一律带 `_兜底` 前缀：engine.py 里已有 9 处 `except Exception as _e:`，
   其中一处就在 run_backtest_boll 的收尾块**内部**。Python 离开 except 块时会
   自动 `del _e`，用同名会让外层异常变量被内层悄悄删掉 -> NameError。

⚠️ 只改「异常出口」，不动任何信号 / 手数 / 止损 / 出场逻辑。

用法：
  python _patch_engine_拒单兜底.py           # 干跑，只报告不改
  python _patch_engine_拒单兜底.py --go      # 真改（自动备份）
"""
import hashlib
import os
import shutil
import sys

这里 = os.path.dirname(os.path.abspath(__file__))
_ROOT = 这里
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)

引擎 = os.path.join(_ROOT, "lib", "engine.py")

签名 = "except Exception as _兜底异常:"

旧 = (
    "    except BacktestFinished:\n"
    "        # ★ 先确认这段区间到底有没有行情。没有的话（该品种当时还没上市），\n"
)

新 = (
    "    " + 签名 + "\n"
    "        # ★ 2026-09-28：原为 `except BacktestFinished:`，只接「正常跑完」。\n"
    "        #   tqsdk 的模拟账户在下单被拒时抛的是**普通 Exception**\n"
    "        #   （实测原话：Exception: 遇到错单: CZCE.SA101 SELL OPEN 6手 1629.000000 开仓资金不足），\n"
    "        #   它不是 BacktestFinished，会直接穿透出去 —— 整个回测白跑、一份报告都不出。\n"
    "        #   实测场景：03 均值回归 / 纯碱，2020-01~2020-10 把 20 万本金亏到 9,100 元，\n"
    "        #   此后每根 K 线都在「想开仓 -> 被拒 -> 抛异常」上崩掉。\n"
    "        #   这里接住所有异常再分流（正常路径的行为逐字不变）：\n"
    "        #     · BacktestFinished      -> 正常跑完，往下走原有收尾\n"
    "        #     · 资金不足 / 遇到错单    -> 提前结束，同样走收尾，并显著标注\n"
    "        #     · 其它任何异常          -> 原样 raise，绝不掩盖\n"
    "        _兜底局部 = locals()\n"
    "        _兜底消息 = str(_兜底异常)\n"
    "        _兜底拒单 = ((\"遇到错单\" in _兜底消息) or (\"开仓资金不足\" in _兜底消息)\n"
    "                     or (\"平仓资金不足\" in _兜底消息))\n"
    "        if not isinstance(_兜底异常, BacktestFinished) and not _兜底拒单:\n"
    "            raise\n"
    "        if _兜底拒单:\n"
    "            _兜底权益 = \"未知\"\n"
    "            try:\n"
    "                _兜底权益 = f\"{float(_兜底局部['account'].balance):,.2f} 元\"\n"
    "            except Exception:\n"
    "                pass\n"
    "            _兜底时点 = \"?\"\n"
    "            try:\n"
    "                _兜底时点 = equity_curve[-1][0]\n"
    "            except Exception:\n"
    "                pass\n"
    "            _兜底初始 = _兜底局部.get(\"init_balance\")\n"
    "            _兜底初始文本 = (f\"{int(_兜底初始):,} 元\"\n"
    "                             if isinstance(_兜底初始, (int, float)) else \"?\")\n"
    "            print(f\"\\n>>> {_兜底局部.get('name', symbol_key)}\"\n"
    "                  f\" {_兜底局部.get('main_code', '')}\"\n"
    "                  f\" 回测 {_兜底局部.get('start')} ~ {_兜底局部.get('end')}\")\n"
    "            print(f\"    [!] 下单被柜台拒绝，回测在 {_兜底时点} 提前结束：{_兜底消息}\")\n"
    "            print(f\"        中断时账户权益 {_兜底权益}（初始 {_兜底初始文本}）。\")\n"
    "            print(f\"        这是该策略在这段区间的**真实表现**，不是程序坏了；\")\n"
    "            print(f\"        但下面这份报告只覆盖到中断那一刻，\")\n"
    "            print(f\"        **不是完整区间，不能当完整结果用**。要看完整区间得改仓位口径。\")\n"
    "        # ★ 先确认这段区间到底有没有行情。没有的话（该品种当时还没上市），\n"
)


def 主():
    干跑 = "--go" not in sys.argv
    b = open(引擎, "rb").read()
    文本 = b.decode("utf-8")

    命中 = 文本.count(旧)
    已改 = 文本.count(签名)
    print("锚点命中次数   : %d  （应为 5 = 5 个回测函数）" % 命中)
    print("新签名已存在数 : %d  （应为 0）" % 已改)
    print("引擎 md5       : %s" % hashlib.md5(b).hexdigest())
    print("行尾           : CRLF %d / 裸LF %d"
          % (b.count(b"\r\n"), b.count(b"\n") - b.count(b"\r\n")))

    if 命中 != 5:
        print("\n✗ 锚点不是 5 个，中止（避免误改）")
        return 1
    if 已改 != 0:
        print("\n✗ 看起来已经打过补丁了，中止")
        return 1

    新文本 = 文本.replace(旧, 新)
    print("\n替换后 anchor 剩余 : %d （应为 0）" % 新文本.count(旧))
    print("替换后 新签名      : %d （应为 5）" % 新文本.count(签名))

    try:
        compile(新文本, 引擎, "exec")
        print("语法检查            : 通过")
    except SyntaxError as e:
        print("\n✗ 语法错误：%s" % e)
        return 1

    if 干跑:
        print("\n这是干跑。要真改请加 --go")
        return 0

    备份 = os.path.join(这里, "engine.py")
    shutil.copy2(引擎, 备份)
    with open(引擎, "wb") as f:
        f.write(新文本.encode("utf-8"))

    b2 = open(引擎, "rb").read()
    print("\n已写入。新 md5 : %s" % hashlib.md5(b2).hexdigest())
    print("新行尾        : CRLF %d / 裸LF %d"
          % (b2.count(b"\r\n"), b2.count(b"\n") - b2.count(b"\r\n")))
    print("原件备份      : %s" % os.path.relpath(备份, _ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(主())
