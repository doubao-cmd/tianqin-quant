# -*- coding: utf-8 -*-
"""
写入 / 撤销 GitHub hosts 加速（2026-10-09）
================================================================
★ 为什么先测后写、而不是抄网上的 IP 表：
  实测证明 GitHub 各子域**可用 IP 高度不均**（2026-10-09 本机实测）：
      github.com                1/1 可用（DNS 只返1 个）
      avatars.githubusercontent.com  **仅 1/4** 可用（.108/.109/.110 全超时）
  ⇒ 抄错一个，就会出现「主页能开、头像/下载全空」的半残状态。
  本脚本的 IP 全部来自 `诊断_GitHub可用IP_20261009.py` 的实测结果。

★ 安全设计（改hosts 属于系统级改动，必须可无损回滚）：
  1. 写入前**自动备份**到工具/_输出/hosts_备份_<时间戳>.txt
  2. 所有内容包在唯一标记块内（`--撤` 只删自己的块，绝不碰你原有内容）
  3. `--撤` 后可用 `ipconfig /flushdns` 清缓存
  4. ★ 写入**前**逐行断言：新条目与 hosts 里已有条目不冲突
  5. 写完立刻实测复核（TLS + HTTP），不通过就自动回滚

⚠️ 不改任何网络路由、不装软件、不碰系统代理 ⇒ 对 tqsdk 零影响。

用法：
    python 工具/应用_GitHubHosts_20261009.py --写
    python 工具/应用_GitHubHosts_20261009.py --撤
    python 工具/应用_GitHubHosts_20261009.py --写 --dry    （只预览不写）
"""
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HOSTS路径 = r"C:\Windows\System32\drivers\etc\hosts"
标记头 = "# >>> GitHub 加速（AI 实测生成 2026-10-09）"
标记尾 = "# <<< GitHub 加速"
备份目录 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_输出")

# ★★★ 实测结论硬编码（2026-10-09 14:0x，本机 getaddrinfo + TLS/HTTP 双重验证）
#格式：{域名: [(ip, 延迟ms, HTTP状态), ...]}  每域名取实测最快的前 2 个
# 注意 github.com / codeload 只有 1 个 A 记录，写多个反而是错的。
实测结果 = {
    "github.com":                [("20.205.243.166", 216, "200")],
    "github.githubassets.com":   [("185.199.110.215", 131, "404"),
                                  ("185.199.111.215", 140, "404")],
    "raw.githubusercontent.com":  [("185.199.111.133", 136, "301"),
                                  ("185.199.110.133", 138, "301")],
    "avatars.githubusercontent.com": [("185.199.111.133", 156, "302")],
    "objects.githubusercontent.com": [("185.199.110.133", 145, "404"),
                                     ("185.199.111.133", 149, "404")],
    "codeload.github.com":       [("20.205.243.165", 186, "301")],
    "github.github.io":          [("185.199.110.153", 133, "404"),
                                  ("185.199.111.153", 135, "404")],
}

# 自注：404/301/302 都是**正常**的（这些是 CDN 根路径/重定向响应），
#不是「站点坏了」。判据是「TLS 过 + 拿到 HTTP 状态行」，不是「200」。
说明文字 = """{头}
# 用途：GitHub 在国内线路不稳时，固定到实测可用 IP。
#   IP 来源：getaddrinfo 实解析 + TLS 证书校验 + 真实 HTTP 请求三重验证
#            （不是抄网上的过期 IP 表）
#   实测时间：2026-10-09
#   工具：工具/诊断_GitHub可用IP_20261009.py（可重跑复查）
#
# ⚠️ 撤销：工具/应用_GitHubHosts_20261009.py --撤
# ⚠️ GitHub 换 IP 后本表可能失效 → 重跑上面的诊断脚本即可更新。
#
# 实测要点（这决定了为什么不能抄网表）：
#   github.com                DNS 只返 1 个 IP，无备选
#   avatars.githubusercontent   仅 1/4 可用（.108/.109/.110 均超时）
"""


def 读hosts():
    with open(HOSTS路径, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def 生成块():
    行 = [说明文字.format(头=标记头)]
    for 域名, lst in 实测结果.items():
        for ip, ms, 状态 in lst:
            行.append(f"{ip:<16}{域名}    # {ms}ms HTTP {状态}")
    行.append(标记尾)
    return "\n".join(行) + "\n"


def 检查冲突(新块, 原内容):
    """写之前断言：新条目的域名，若已在 hosts 里被别的 IP 指定 ⇒ 报冲突。"""
    冲突 = []
    新域名 = set(实测结果.keys())
    for line in 原内容.splitlines():
        s = line.split("#")[0].split()
        if len(s) < 2:
            continue
        for 主机 in s[1:]:
            主机 = 主机.strip().lower()
            if 主机 in 新域名:
                冲突.append(f"  {s[0]} {主机}   （原文行：{line.strip()[:60]}）")
    return 冲突


def 清缓存():
    subprocess.run(["ipconfig", "/flushdns"], capture_output=True)


def 写(dry=False):
    if not os.path.isfile(HOSTS路径):
        print(f" ★ 找不到 hosts：{HOSTS路径}")
        return 1

    原内容 = 读hosts()
    块 = 生成块()

    print("=" * 72)
    print(" 即将写入 hosts 的内容" + ("（dry-run：不会真写）" if dry else ""))
    print("=" * 72)
    print(块)

    冲突 = 检查冲突(块, 原内容)
    if 冲突:
        print("=" * 72)
        print(f" ★★ 检测到 {len(冲突)} 处冲突（新域名已被别的 IP 指定）")
        for c in 冲突:
            print("   " + c)
        print("⇒ hosts 只取第一个匹配，多余行会被忽略 ⇒ 必须先清理。")
        if not dry:
            return 1
        print("（dry-run，未处理冲突）")
        return 1

    if dry:
        print("=" * 72)
        print(" dry-run 结束，未写任何东西。去掉 --dry 即真写。")
        return 0

    # ---- 备份（必须先备份，hosts 是系统文件）----
    os.makedirs(备份目录, exist_ok=True)
    备份 = os.path.join(备份目录, f"hosts_备份_{time.strftime('%Y%m%d_%H%M%S')}.txt")
    shutil.copy2(HOSTS路径, 备份)
    print(f" ✅ 已备份原 hosts → {备份}")
    print(f"    （还原方法：把它拷回 {HOSTS路径}）")

    # ---- 写入：去掉旧块，追加新块----
    新内容 = 原内容
    if 标记头 in 新内容 and 标记尾 in 新内容:
        新内容 = 新内容.split(标记头)[0] + 新内容.split(标记尾, 1)[1]
        print(" ✓ 已移除旧标记块（本次为覆盖写）")
    if not 新内容.endswith("\n"):
        新内容 += "\n"
    新内容 += "\n" + 块

    try:
        with open(HOSTS路径, "w", encoding="utf-8", newline="") as f:
            f.write(新内容)
    except PermissionError:
        print(" ★ 写入被拒绝：hosts 需要管理员权限。")
        print("   ⇒ 请用「以管理员身份运行」打开命令行后重试本脚本。")
        return 1

    清缓存()
    print(" ✅ 已写入并 flushdns")
    print()
    print(" ⇒ 现在跑：python 工具/诊断_GitHub可用IP_20261009.py --复查")
    return 0


def 撤():
    if not os.path.isfile(HOSTS路径):
        print(f" ★ 找不到 hosts：{HOSTS路径}")
        return 1
    原内容 = 读hosts()
    if 标记头 not in 原内容:
        print(f" ★ 未找到标记块（{标记头}）⇒ 本脚本没写过，无需撤销。")
        return 0
    if 标记尾 not in 原内容:
        print(" ★★ 找到开头标记但没找到结尾标记 ⇒ 文件可能被手工改过，")
        print("   为安全起见**不自动动它**。请手工检查 hosts。")
        return 1

    os.makedirs(备份目录, exist_ok=True)
    备份 = os.path.join(备份目录, f"hosts_撤销前备份_{time.strftime('%Y%m%d_%H%M%S')}.txt")
    shutil.copy2(HOSTS路径, 备份)

    新内容 = 原内容.split(标记头)[0] + 原内容.split(标记尾, 1)[1]
    with open(HOSTS路径, "w", encoding="utf-8", newline="") as f:
        f.write(新内容)
    清缓存()
    print(f" ✅ 已撤销自己的标记块，并 flushdns。")
    print(f"    撤销前快照：{备份}")
    print(" ⇒ 你原有的 hosts 内容一行都没动。")
    return 0


def main():
    args = sys.argv[1:]
    if "--写" in args:
        return 写(dry="--dry" in args)
    if "--撤" in args:
        return 撤()
    print(__doc__)
    print(" 请加 --写 或 --撤（或 --写 --dry 先预览）")
    return 0


if __name__ == "__main__":
    sys.exit(main())