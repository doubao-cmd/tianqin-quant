# -*- coding: utf-8 -*-
"""
新建策略骨架生成器
==========================================================
2026-09-30 新建（工作台二期）。一句话：把「加一个新策略」从手工复制粘贴
变成一条命令，且骨架里预埋了四条硬性规则和预注册纪律，防止过拟合老毛病。

【生成什么】
    策略/<编号>_<短名>/_档案.md                 ← 预注册模板（假设/参数/裁决标准先写死）
    策略/<编号>_<短名>/回测/<品种>/<短名>_回测.py ← 参数壳 + signal_fn 示例逻辑
    策略/<编号>_<短名>/回测/<品种>/回测.bat      ← 双击即跑（解释器自动探测）
    lib/strategy_dirs.py 的 映射 里自动登记一行   ← 报告路径才落对地方

【走哪条引擎】
    生成的回测壳走 lib.engine.run_backtest 通用入口（均线框架）：
    撮合、资金、报告、防未来函数全由引擎兜底，你只写 signal_fn。
    ⚠️ 非均线结构的逻辑（摆动点/布林带这类）不适用本框架 —— 那种需要在
       lib/engine.py 里写引擎函数，先讨论清楚再动手，本生成器不碰引擎。

【防呆】
    · 编号自动取「现存最大编号」的下一个（01~07 已占用/冻结，不复用）
    · 目录已存在 / 短名已登记 → 拒绝，不写半个文件
    · 先全部构造好内容、确认后才落盘

【用法】
    python 工具/新建策略.py                          # 交互式
    python 工具/新建策略.py 动量突破 --品种 螺纹钢,甲醇 --yes   # 直通（脚本/测试用）
"""
import os
import re
import sys
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config_symbols

线 = "=" * 66

# 池内品种默认清单（与 05_摆动线 的 12 品种=全池口径一致）
池 = [(名, key) for key, (名, _mc, _mult, _tick) in config_symbols.SYMBOLS.items()]


# ---------------------------------------------------------------- 编号

def 下一编号():
    """现存最大策略编号 + 1；01~07 历史占用（部分已冻结归档），不复用。"""
    d = os.path.join(_ROOT, "策略")
    nums = [7]
    if os.path.isdir(d):
        for f in os.listdir(d):
            m = re.match(r"^(\d{2})_", f)
            if m:
                nums.append(int(m.group(1)))
    return max(nums) + 1


def 已登记短名():
    """读 lib/strategy_dirs.py 里已登记的短名（防重复登记）。"""
    p = os.path.join(_ROOT, "lib", "strategy_dirs.py")
    with open(p, "r", encoding="utf-8") as f:
        return set(re.findall(r'^\s*"([^"]+)"\s*:', f.read(), re.M))


# ---------------------------------------------------------------- 模板

档案模板 = """# __名__ —— 策略档案

> __TODAY__ 由 `工具/新建策略.py` 生成骨架。状态：**新建，未跑过任何回测**。

---

## 一、假设（预注册：先写清楚为什么它该挣钱，再写代码）

> 规则 2：先有交易逻辑再写代码。这里的假设写不完，就不许动 signal_fn。
> 写法参考：「<市场现象> + <为什么它长期存在/不会被套利光> + <我的规则怎么吃到它>」

（在这里写假设）

## 二、参数（一次定死，禁止扫描择优）

| 参数 | 值 | 定它的依据 |
|---|---|---|
| N1 快线 | 5 | 常规值（示例默认，可改一次后冻结） |
| N2 慢线 | 20 | 常规值（同上） |
| 周期 | 日线 | 新策略先日线，跑通再说 |
| 滑点 | 1 跳 | 全项目统一口径 |

## 三、裁决标准（先于任何回测写死）

1. **样本内门槛**：本项目尺子 K=172 ⇒ 门槛 t=3.2086 ⇒ **样本内夏普 < 2.03 = 没走得出去**，
   直接判死，不进样本外、不找借口（夏普 0.5~1.0 级别在现样本上验证不出 alpha）。
2. **样本外**：OOS(2025-01 ~ 2026-09) **只跑一次**，跑完即作废；看过再用它改策略 = 污染。
3. **成本压测**：devslip3（3 跳）下还活着才算数。
4. 每次只改一处逻辑，改完立刻回测，在「五、版本沿革」逐条记录。

## 四、结果（跑完填）

| 窗口 | 收益率 | 夏普 | 最大回撤 | 回合数 | 结论 |
|---|---|---|---|---|---|
| IS 2016-2019 | | | | | |
| slip3 压测 | | | | | |
| VAL 2020-2024 | | | | | |
| OOS 2025-2026.9（只此一次） | | | | | |

## 五、版本沿革（每次只改一处，逐条记录）

| 日期 | 改了哪一处 | 为什么 | IS 结果 | 结论 |
|---|---|---|---|---|
| | | | | |
"""

回测壳模板 = '''# -*- coding: utf-8 -*-
# ⚠️ 本文件由 工具/新建策略.py 生成（__TODAY__）——骨架是通用的，逻辑要你自己写。
"""
__名__ —— 回测入口（__品种名__ · 日线 · 均线框架）
==========================================================
【四条硬性规则（用户 2026-09-16 下达，逐字有效）】
  1. 禁未来函数：信号只用已收盘 K 线，下单在下一根开盘（引擎已保证，别破坏它）
  2. 先有交易逻辑再写代码：不为拉胜率叠过滤、不遍历参数择优
  3. 样本内/外分离：OOS 只看一次，看过即作废
  4. 条件简洁：每次只改一处逻辑，改完立刻回测并记进 _档案.md

怎么用：
  1. 先把 策略/__编号____名__/_档案.md 的「假设」写完
  2. 改下面 signal_fn 里的信号逻辑（现在只是示例：双均线金叉死叉）
  3. 双击同目录 回测.bat

信号框架说明：
  引擎通用入口 lib.engine.run_backtest 负责撮合/资金/报告/防未来函数；
  你只提供 signal_fn(now_f, now_s, pre_f, pre_s)：
      now_f / pre_f = 快线(N1) 当前根 / 上一根【已收盘】值
      now_s / pre_s = 慢线(N2) 同上
      返回 +1 = 做多（持空则反手）；-1 = 做空（持多则反手）；0 = 不动
  ⚠️ 非均线结构的逻辑（摆动点/布林带这类）不适用本框架 —— 那种要在
     lib/engine.py 里写引擎函数，先讨论清楚再动手，别硬塞进 signal_fn。
"""
import sys
import os
from datetime import date, timedelta

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib import engine

# ==================== 可调参数 ====================
SYMBOL_KEY = "__品种KEY__"            # __品种名__
N1 = 5                                # 快线周期（一次定死；禁止扫描择优）
N2 = 20                               # 慢线周期（同上）
VOLUME = 1                            # 固定手数（新手先 1 手）
# ---- 样本切分（与 05/07 同一口径；定稿后不得再改）----
IS_START  = date(2016, 1, 1)     # 练习(样本内)：可反复跑
IS_END    = date(2019, 12, 31)
VAL_START = date(2020, 1, 1)     # 验证集：结构选择，可跑有限几次
VAL_END   = date(2024, 12, 31)
OOS_START = date(2025, 1, 1)     # 期末考试(样本外)：只跑一次！
OOS_END   = date(2026, 9, 30)
SAMPLE = "in"                    # "in"=样本内 | "val"=验证集 | "out"=期末考试(只看一次)
assert SAMPLE in ("in", "val", "out"), f"SAMPLE 只能填 'in'/'val'/'out'，现在是 {SAMPLE!r}"
EVAL_START = {"in": IS_START, "val": VAL_START, "out": OOS_START}[SAMPLE]
EVAL_END   = {"in": IS_END,   "val": VAL_END,   "out": OOS_END}[SAMPLE]
START = EVAL_START - timedelta(days=60)   # 预热 60 天（够 N2=20 根日线算均线）
END = EVAL_END
INIT_BALANCE = 200000            # 初始资金（元）
KLINE_SEC = 24 * 60 * 60         # 日线（新策略先日线跑通，别一上来 30 分钟）
WEB_GUI = True                   # 跑完用浏览器打开报告
SLIP_TICKS = 1                   # 费用层滑点（跳）；压测改成 3
# =================================================


def signal_fn(now_f, now_s, pre_f, pre_s):
    """★ 你的信号逻辑写在这里（下面是示例：快线上/下穿慢线，只改这一处）。

    只用传入的四个【已收盘】值；返回 +1 / -1 / 0。
    原则：先在 _档案.md 写清假设，再来动这里；每次只改一处，改完立即回测。
    """
    if pre_f <= pre_s and now_f > now_s:
        return 1
    if pre_f >= pre_s and now_f < now_s:
        return -1
    return 0


if __name__ == "__main__":
    _label = {"in": "练习(样本内，可反复跑)",
              "val": "验证集(结构选择，有限几次)",
              "out": "期末考试(样本外，只此一次)"}[SAMPLE]
    print(f"[样本模式] {_label} | 回测区间 {START} ~ {END}")
    if SAMPLE == "out":
        print("!! 期末考试提示：样本外只应跑一次，跑完即作废，此后不得再据此修改策略 !!")
    engine.run_backtest(
        SYMBOL_KEY, signal_fn, N1, N2, volume=VOLUME,
        start=START, end=END, eval_start=EVAL_START,
        init_balance=INIT_BALANCE, kline_sec=KLINE_SEC,
        strategy_name="__名__", open_html=WEB_GUI, sample_tag=SAMPLE,
        slip_ticks=SLIP_TICKS,
    )
'''

回测bat模板 = """@echo off
chcp 65001 >nul
title __品种名__ __名__ 回测
cd /d "%~dp0"
set "PYTHONUTF8=1"

echo ============================================================
echo  __名__ __品种名__ 回测
echo ============================================================
echo.
echo  回测运行中... 跑完自动弹出 HTML 报告(存到 回测结果/__编号____名__/__品种名__/)

rem ---- interpreter: pick the first candidate that really has tqsdk ----
set "PY="
set "_VENV_TQ=%USERPROFILE%\\.workbuddy\\binaries\\python\\envs\\tqsdk\\Scripts\\python.exe"
for %%P in ("%_VENV_TQ%" python py) do (
  if not defined PY (
    "%%~P" -c "import importlib.util as u, sys; sys.exit(0 if u.find_spec('tqsdk') else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=%%~P"
  )
)
if not defined PY (
  echo.
  echo  [错误] 没找到装了 tqsdk 的 Python，程序起不来。
  echo         已依次试过：%_VENV_TQ%  /  python  /  py
  echo.
  pause
  exit /b 1
)

"%PY%" -u __名___回测.py

echo.
echo ------------------------------------------------------------
echo  程序已结束
echo ------------------------------------------------------------
pause
"""


# ---------------------------------------------------------------- 落盘

def 填模板(模板, **kw):
    s = 模板
    for k, v in kw.items():
        s = s.replace("__" + k + "__", str(v))
    return s


def 登记_策略目录(短名, 目录名, 今天):
    """往 lib/strategy_dirs.py 的 映射 里插一行（在 `映射 = {` 之后）。"""
    p = os.path.join(_ROOT, "lib", "strategy_dirs.py")
    with open(p, "r", encoding="utf-8") as f:
        src = f.read()
    锚 = "映射 = {\n"
    if 锚 not in src:
        raise RuntimeError("lib/strategy_dirs.py 结构变了（找不到 `映射 = {`），请手工登记")
    行 = f'    "{短名}": "{目录名}",   # {今天} 新建策略生成器自动登记\n'
    src = src.replace(锚, 锚 + 行, 1)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(src)


def 生成(短名, 品种keys, 确认=True):
    短名 = 短名.strip()
    if not 短名 or re.search(r'[\\/:*?"<>|\' ]', 短名):
        print("  ⛔ 短名不合法（不能含空格和 \\/:*?\"<>| 等字符）")
        return 1
    已登记 = 已登记短名()
    if 短名 in 已登记:
        print(f"  ⛔ 短名「{短名}」已在 lib/strategy_dirs.py 登记过，换一个名字")
        return 1

    编号 = 下一编号()
    目录名 = f"{编号:02d}_{短名}"
    策略目录 = os.path.join(_ROOT, "策略", 目录名)
    if os.path.exists(策略目录):
        print(f"  ⛔ 目录已存在：{策略目录}")
        return 1

    # 品种解析
    名到key = dict(池)
    选品种 = []
    for k in 品种keys:
        k = k.strip()
        if not k:
            continue
        if k in 名到key:
            选品种.append((k, 名到key[k]))          # (品种中文名, 品种代码)
        elif k in dict(池).values():
            选品种.append((dict((v, k2) for k2, v in 池)[k], k))
        else:
            print(f"  ⛔ 认不得品种「{k}」（用中文名如 螺纹钢，或代码如 RB；"
                  f"可选：{'、'.join(名 for 名, _ in 池)}）")
            return 1
    if not 选品种:
        选品种 = 池  # 默认全池

    今天 = date.today().isoformat()

    # ---- 先全部构造，确认后统一落盘 ----
    计划 = [(os.path.join(策略目录, "_档案.md"),
             填模板(档案模板, 名=短名, TODAY=今天))]
    for 品种名, key in 选品种:
        品种目录 = os.path.join(策略目录, "回测", 品种名)
        计划.append((os.path.join(品种目录, f"{短名}_回测.py"),
                     填模板(回测壳模板, 名=短名, 编号=编号, TODAY=今天,
                            品种名=品种名, 品种KEY=key)))
        计划.append((os.path.join(品种目录, "回测.bat"),
                     填模板(回测bat模板, 名=短名, 编号=编号, 品种名=品种名)
                     .replace("\n", "\r\n")))

    print("\n" + 线)
    print(f"  将生成策略 {目录名}（品种 {len(选品种)} 个）：")
    for p, _ in 计划:
        print("    " + os.path.relpath(p, _ROOT))
    print(f"  并在 lib/strategy_dirs.py 登记：\"{短名}\": \"{目录名}\"")
    print(线)

    if 确认:
        q = input("\n  确认生成？(回车=生成 / n=取消)：").strip().lower()
        if q == "n":
            print("  已取消。")
            return 0

    for p, 内容 in 计划:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        newline = "" if p.endswith(".bat") else "\n"   # bat 内容已含 \r\n
        with open(p, "w", encoding="utf-8", newline=newline) as f:
            f.write(内容)
    登记_策略目录(短名, 目录名, 今天)

    print(f"""
  ✅ 生成完毕：策略/{目录名}/

  下一步（按纪律来）：
    1. 打开 _档案.md，把「一、假设」写完 —— 写不完就不许动 signal_fn
    2. 改 回测/<品种>/<短名>_回测.py 里的 signal_fn（现在是金叉死叉示例）
    3. 双击该目录 回测.bat 跑样本内（日线，很快）
    4. 对照 _档案.md 第三节的裁决标准：夏普 ≥ 2.03 才继续，否则记档判死
    5. 工作台/菜单里会自动出现这个策略，不用再登记
""")
    return 0


# ---------------------------------------------------------------- 入口

def _用法():
    print(__doc__)


def main(argv):
    if argv and argv[0] in ("-h", "--help"):
        _用法()
        return 0
    确认 = "--yes" not in argv
    argv = [a for a in argv if a != "--yes"]
    品种keys = []
    if "--品种" in argv:
        i = argv.index("--品种")
        if i + 1 < len(argv):
            品种keys = argv[i + 1].split(",")
            argv = argv[:i] + argv[i + 2:]
    if argv:
        return 生成(argv[0], 品种keys, 确认=确认)

    print(线)
    print(" 天勤量化 · 新建策略骨架")
    print(线)
    print(f"  可选品种：{'、'.join(名 for 名, _ in 池)}")
    print(f"  （直接回车 = 生成全池 {len(池)} 个品种；或输中文名/代码，逗号分隔）")
    短名 = input("\n  策略短名（如：动量突破）：").strip()
    if not 短名:
        print("  已取消。")
        return 0
    品种输入 = input("  品种（回车=全池）：").strip()
    return 生成(短名, 品种输入.split(",") if 品种输入 else [], 确认=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
