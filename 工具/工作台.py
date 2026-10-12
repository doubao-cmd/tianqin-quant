# -*- coding: utf-8 -*-
"""
天勤量化 · 工作台（总入口）
==========================================================
2026-09-30 新建 ｜ 2026-10-11 改造（修过时入口 + 新增自检/作品集能力）

【设计原则】
    本文件不实现任何业务逻辑，只把散落的入口收拢成五个区块。
    回测逻辑原样复用 工具/菜单.py（import 菜单），以后菜单改了这里自动跟着变。

【命令行直通】（不进菜单时）
    python 工具/工作台.py 自检        # 本地自检，几秒出结果
    python 工具/工作台.py 日报        # 模拟盘台账日报
    python 工具/工作台.py 未来函数    # 防未来函数 4 层机器证明
    python 工具/工作台.py 体检        # 05/07 策略体检（数小时）

【区块】
    [回测策略]   选策略选品种跑回测 / 新建策略骨架 / 打开目录
    [模拟盘]     MA13 五腿启动 / 守护 / 台账日报 / 运营SOP

    [体检自检]   防未来函数 / 本地自检 / 环境自检 / 行尾 / 密钥 / 05-07体检
    [复盘作品]   README / 三段对照 / 项目路线 / 基础补强 / 学习目录 / 汇总报告
    [系统]       git 状态 / 回滚 / 目录地图 / 打开根目录

★ 2026-10-11 改造要点（对照旧版的差异）：
    ① 修 BUG：模拟盘区原来指向已删除的「启动_P_EB_观察模式.bat」（P/EB 双品种旧方案），
       点进去必报「找不到文件」。现改为当前真实的 **五腿** 入口 ma13_start_now.cmd。
    ② 新增「防未来函数自检」——规则 1 的 4 层机器证明（静态 AST + 截断 + 涂改未来 + 元测试）。
    ③ 新增「复盘与作品集」区：README / 三段对照 / 三条学习路线 HTML 一键打开。
    ④ 主菜单顶部显示 **熔断状态**（原来要点进模拟盘区才看得到）。
    ⑤ 代码卫生工具（行尾 / 密钥扫描）纳入「体检自检」区。
"""

import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
根 = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if 根 not in sys.path:
    sys.path.insert(0, 根)

PY = sys.executable
线 = "=" * 66

# ---------------------------------------------------------------- 路径集中定义
模拟盘目录 = os.path.join(根, "策略", "06_MA13超短线", "模拟盘")
CMD_五腿立即 = os.path.join(模拟盘目录, "ma13_start_now.cmd")      # 立即启动（不等 08:55，真下单）
BAT_五腿定时 = os.path.join(模拟盘目录, "每日0855自动启动.bat")     # 定时启动（等 08:55）
PY_守护 = os.path.join(模拟盘目录, "模拟盘守护.py")
SOP_模拟盘 = os.path.join(模拟盘目录, "运营SOP.md")
台账目录 = os.path.join(模拟盘目录, "台账")
熔断文件 = os.path.join(台账目录, "熔断状态.json")

PY_日报 = os.path.join(_HERE, "台账日报.py")
PY_自检本地 = os.path.join(_HERE, "自检_本地.py")
PY_环境自检 = os.path.join(_HERE, "环境自检.py")
PY_未来函数 = os.path.join(_HERE, "自检_未来函数.py")
PY_行尾 = os.path.join(_HERE, "自检_行尾_20261009.py")
PY_密钥 = os.path.join(_HERE, "自检_密钥扫描_20261009.py")
PY_体检0507 = os.path.join(_HERE, "体检_05_07.py")
JSON_体检0507 = os.path.join(_HERE, "体检_05_07.json")
PY_新建策略 = os.path.join(_HERE, "新建策略.py")
PY_回滚 = os.path.join(_HERE, "回滚.py")
PY_汇总报告 = os.path.join(_HERE, "汇总报告.py")

MD_三段对照 = os.path.join(根, "回测结果", "06_MA13超短线", "三段对照_20261010.md")
HTML_项目路线 = os.path.join(根, "项目仓库_可打勾路线.html")
HTML_基础补强 = os.path.join(根, "基础补强_可打勾路线.html")
HTML_学习目录 = os.path.join(根, "量化职场学习路线图.html")
README = os.path.join(根, "README.md")

import 菜单  # noqa: E402  （复用回测菜单的全部选项函数）


# ---------------------------------------------------------------- 小工具

def _暂停():
    input("\n  回车返回工作台 ...")


def _开目录(相对):
    p = os.path.join(根, 相对)
    if os.path.isdir(p):
        os.startfile(p)
        print(f"\n  已打开：{p}")
    else:
        print(f"\n  ⛔ 目录不存在：{p}")


def _开文件(p, 名=None):
    """打开文件；不存在则给出友好提示。"""
    名 = 名 or os.path.basename(p)
    if os.path.isfile(p):
        os.startfile(p)
        print(f"\n  已打开：{名}")
    else:
        print(f"\n  ⛔ 找不到 {名}（{p}）")


def _跑脚本(py路径, 额外参数=None, cwd=None):
    """在当前控制台里跑一个工具脚本，跑完暂停。"""
    if not os.path.isfile(py路径):
        print(f"\n  ⛔ 脚本不存在：{py路径}")
        _暂停()
        return
    参数 = [PY, py路径] + (额外参数 or [])
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    print("\n" + 线)
    print(f"  执行：python {os.path.relpath(py路径, 根)} {' '.join(额外参数 or [])}")
    print(线 + "\n")
    try:
        subprocess.call(参数, cwd=cwd or 根, env=env)
    except KeyboardInterrupt:
        print("\n  —— 已中断 ——")
    _暂停()


def _开新窗口(脚本路径, cwd=None):
    """在**新控制台窗口**里启动 .cmd/.bat（不阻塞工作台）。"""
    if not os.path.isfile(脚本路径):
        print(f"\n  ⛔ 找不到：{脚本路径}")
        _暂停()
        return False
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    subprocess.Popen(["cmd", "/c", 脚本路径], cwd=cwd or os.path.dirname(脚本路径), env=env,
                     creationflags=subprocess.CREATE_NEW_CONSOLE)
    return True


def _熔断信息():
    """读熔断状态文件。

    ★ 2026-10-11 修的 bug：该状态文件**始终存在**（里面存 peak 峰值与 halted 标记），
    判据必须是 JSON 里的 `halted` 字段，**不能只看文件在不在**。
    旧版 `os.path.isfile(熔断状态.json)` ⇒ 永远为真 ⇒ 永远误报「熔断挂着」⇒
    模拟盘 [1] 启动会被错误拦住（这是个一直没被发现的潜伏 bug）。
    """
    if not os.path.isfile(熔断文件):
        return False, ""
    try:
        import json
        with open(熔断文件, "r", encoding="utf-8") as f:
            d = json.load(f)
        return bool(d.get("halted", False)), str(d.get("update", ""))
    except Exception as e:      # 文件损坏/读不动 ⇒ 不敢误报挂起，按正常处理并在状态条提示
        return False, f"读取失败({e})"


def _熔断挂着():
    return _熔断信息()[0]


# ---------------------------------------------------------------- 回测策略区

def 区_回测策略():
    while True:
        print(f"""

  ── 回测与策略 ─────────────────────────
    [1] 选策略 + 选品种 → 跑回测
    [2] 打开 策略/ 目录
    [3] 打开 回测结果/ 目录
    [4] 新建策略骨架（档案预注册 + 回测壳 + bat + 登记）
    [5] 打开 三段窗口对照表（2026-10-10）
    [回车] 返回工作台""")
        c = input("  请输入：").strip()
        if c == "1":
            菜单.选项_跑回测()
        elif c == "2":
            _开目录("策略")
        elif c == "3":
            _开目录("回测结果")
        elif c == "4":
            _跑脚本(PY_新建策略)
        elif c == "5":
            _开文件(MD_三段对照, "三段对照_20261010.md")
            _暂停()
        else:
            return


# ---------------------------------------------------------------- 模拟盘区

def 区_模拟盘():
    while True:
        挂 = _熔断挂着()
        提示 = "\n  ⚠️⚠️ 熔断挂着！先复位再启动（系统区[1]自检会给三步）" if 挂 else ""
        print(f"""

  ── 模拟盘 · MA13 五腿（焦煤/豆粕/鸡蛋/棕榈油/白糖）云端模拟 ──{提示}
    [1] 立即启动（ma13_start_now.cmd，不等 08:55 · ★真下单到云端模拟账户）
    [2] 启动守护（崩溃自动拉起，新窗口）
    [3] 台账日报（--hb 心跳完整率）
    [4] 打开台账目录
    [5] 打开 运营SOP.md
    [回车] 返回工作台""")
        c = input("  请输入：").strip()
        if c == "1":
            if 挂:
                print("\n  ⛔ 熔断未复位：关旧窗口 → 删 台账/熔断状态.json → 再启动。")
                _暂停()
                continue
            print("\n  ⚠️ 这一项会**真的向云端模拟账户下单**，并占用一个天勤登录席位。")
            print("     启动前请确认：主账号没在跑回测/取数（同机两个 TqApi 会互相拖慢到超时）。")
            q = input("  确认立即启动？(回车=启动 / n=取消)：").strip().lower()
            if q == "n":
                continue
            if _开新窗口(CMD_五腿立即, cwd=模拟盘目录):
                print("  已在新窗口启动五腿模拟盘。")
            _暂停()
        elif c == "2":
            if not os.path.isfile(PY_守护):
                print(f"\n  ⛔ 找不到 {PY_守护}")
                _暂停()
                continue
            print("\n  提醒：守护会拉起模拟盘 —— 同样占用天勤席位。")
            q = input("  确认启动守护？(回车=启动 / n=取消)：").strip().lower()
            if q == "n":
                continue
            env = dict(os.environ)
            env["PYTHONUTF8"] = "1"
            subprocess.Popen(
                [PY, PY_守护], cwd=模拟盘目录, env=env,
                creationflags=subprocess.CREATE_NEW_CONSOLE)
            print("  守护已在新窗口启动（日志：模拟盘/台账/守护日志.txt）。")
            _暂停()
        elif c == "3":
            _跑脚本(PY_日报, 额外参数=["--hb"])
        elif c == "4":
            _开目录(os.path.join("策略", "06_MA13超短线", "模拟盘", "台账"))
            _暂停()
        elif c == "5":
            _开文件(SOP_模拟盘, "运营SOP.md")
            _暂停()
        else:
            return


# ---------------------------------------------------------------- 体检自检区

def 区_体检自检():
    while True:
        已有结果 = "有（直接看）" if os.path.isfile(JSON_体检0507) else "尚无"
        print(f"""

  ── 体检与自检 ─────────────────────────
    [1] ★ 防未来函数自检（规则1 的 4 层机器证明：静态/截断/涂改/元测试）
    [2] 本地自检（配置/路径/行尾/git/熔断，不联网，几秒）
    [3] 环境自检（联网登录天勤 + 验证品种，约 1 分钟）
    [4] 行尾自检（bat/cmd/ps1 必须 CRLF）
    [5] 密钥扫描（真实账号/邮箱/身份证）
    [6] 05/07 策略体检（120 次回测，数小时；结果：{已有结果}）
    [7] 打开 工具/_数据/（行情缓存）
    [回车] 返回工作台""")
        c = input("  请输入：").strip()
        if c == "1":
            _跑脚本(PY_未来函数)
        elif c == "2":
            _跑脚本(PY_自检本地)
        elif c == "3":
            _跑脚本(PY_环境自检)
        elif c == "4":
            _跑脚本(PY_行尾)
        elif c == "5":
            _跑脚本(PY_密钥)
        elif c == "6":
            if os.path.isfile(JSON_体检0507):
                print("\n  ⚠️ 已有体检结果 —— 重跑会再花数小时。")
                q = input("  确认重跑？(n=取消 / 其他=重跑)：").strip().lower()
                if q == "n":
                    continue
            _跑脚本(PY_体检0507)
        elif c == "7":
            _开目录(os.path.join("工具", "_数据"))
            _暂停()
        else:
            return


# ---------------------------------------------------------------- 复盘作品集区

def 区_复盘作品():
    while True:
        print(f"""

  ── 复盘与作品集 ───────────────────────
    [1] 打开 README（项目主页，含 23 条自审记录）
    [2] 打开 三段窗口对照表（口径统一后的诊断数字）
    [3] 打开 项目仓库路线（作品集与执行体系）
    [4] 打开 基础补强路线（知识地基补强）
    [5] 打开 学习总目录（两条线索引）
    [6] 生成汇总报告（工具/汇总报告.py）
    [回车] 返回工作台""")
        c = input("  请输入：").strip()
        if c == "1":
            _开文件(README, "README.md")
            _暂停()
        elif c == "2":
            _开文件(MD_三段对照, "三段对照_20261010.md")
            _暂停()
        elif c == "3":
            _开文件(HTML_项目路线)
            _暂停()
        elif c == "4":
            _开文件(HTML_基础补强)
            _暂停()
        elif c == "5":
            _开文件(HTML_学习目录)
            _暂停()
        elif c == "6":
            _跑脚本(PY_汇总报告)
        else:
            return


# ---------------------------------------------------------------- 系统区

def 区_系统():
    while True:
        print(f"""

  ── 系统 ─────────────────────────────
    [1] 本地自检（配置/路径/行尾/git/熔断，不联网，几秒）
    [2] git 状态
    [3] 回滚（打开回滚工具）
    [4] 打开 目录地图.md
    [5] 打开项目根目录
    [回车] 返回工作台""")
        c = input("  请输入：").strip()
        if c == "1":
            _跑脚本(PY_自检本地)
        elif c == "2":
            try:
                r = subprocess.run(
                    ["git", "-C", 根, "status"],
                    capture_output=True, text=True, timeout=10,
                    encoding="utf-8", errors="replace")
                print("\n" + (r.stdout or r.stderr))
            except FileNotFoundError:
                print("\n  ⛔ git 命令不存在（装 Git for Windows 后重试）")
            _暂停()
        elif c == "3":
            _跑脚本(PY_回滚)
        elif c == "4":
            # 目录地图.md 已移进 _收纳/（根目录只留 6 项）；两处都试，兼容两种状态。
            for _rel in (os.path.join("_收纳", "目录地图.md"), "目录地图.md"):
                _p = os.path.join(根, _rel)
                if os.path.isfile(_p):
                    _开文件(_p, "目录地图.md")
                    break
            else:
                print("\n  ⛔ 两处都没找到目录地图.md")
            _暂停()
        elif c == "5":
            _开目录(".")
            _暂停()
        else:
            return


# ---------------------------------------------------------------- 主循环

def _状态条():
    """主菜单顶部状态：熔断是否**真的**挂着（读 halted 字段，非文件存在性）。"""
    halted, ts = _熔断信息()
    if halted:
        return f"  ⚠️⚠️ 熔断状态：挂着（{ts}）—— 模拟盘已闩死，启动前须复位"
    if ts:
        return f"  ✅ 熔断状态：正常（状态更新 {ts}）"
    return "  ✅ 熔断状态：正常（无状态文件）"


def 主菜单():
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        print(线)
        print("        天勤量化 · 工作台")
        print(线)
        print(_状态条())
        print(线)
        print("  【1】回测与策略    跑回测 / 新建骨架 / 策略与结果目录")
        print("  【2】模拟盘        MA13 五腿启动 / 守护 / 台账日报 / SOP")
        print("  【3】体检与自检    防未来函数 / 本地自检 / 环境自检 / 体检")
        print("  【4】复盘与作品集   README / 三段对照 / 三条路线 / 汇总报告")
        print("  【5】系统          git / 回滚 / 目录地图 / 打开根目录")
        print()
        print("  【0】退出")
        print(线)
        print(f"  项目根：{根}")
        print(线)
        c = input("  请输入序号：").strip()
        if c == "1":
            区_回测策略()
        elif c == "2":
            区_模拟盘()
        elif c == "3":
            区_体检自检()
        elif c == "4":
            区_复盘作品()
        elif c == "5":
            区_系统()
        elif c == "0":
            print("\n  再见。\n")
            return


def main():
    # 命令行直通：python 工具/工作台.py 自检 / 日报 / 未来函数 / 体检
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "自检":
            sys.exit(subprocess.call([PY, PY_自检本地]))
        if arg == "日报":
            sys.exit(subprocess.call([PY, PY_日报, "--hb"]))
        if arg == "未来函数":
            sys.exit(subprocess.call([PY, PY_未来函数]))
        if arg == "体检":
            sys.exit(subprocess.call([PY, PY_体检0507]))
        print(f"  未知参数：{arg}（支持：自检 / 日报 / 未来函数 / 体检）")
        sys.exit(2)
    主菜单()


if __name__ == "__main__":
    main()
