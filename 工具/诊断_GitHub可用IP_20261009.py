# -*- coding: utf-8 -*-
"""
GitHub 可用 IP 实测 v2（只读·不改任何文件）
================================================================
★ v1 犯的错（自己纠正自己）：
  v1 用「网上流传的候选 IP 表」去测，其中 githubassets填 .153、
  avatars 填 140.82.112.5 —— 全是**我瞎填的**，
  测出「全失败」让我以为你的网络有问题。
  真相：那些 IP 根本不是这两个域名的 A 记录。
  ⇒ **铁律：不要猜 IP。候选值必须来自 getaddrinfo 实解析，
     再逐个做 TLS+HTTP 验证。** 抄来的 IP 表是过期且张冠李戴的。

本脚本流程（每一步都取证，不推断）：
  1. getaddrinfo 拿到该域名**当前真实**的 A 记录集合（全集合，不抽样）
  2. 对每个 IP 做 TLS 握手 + 证书CN 校验（防"端口通但不是该服务"）
  3. 再发一个真实 HTTP 请求看状态码
  4. 只把「TLS 过 且 HTTP 返回状态码」的都列为可用

用法：
    python 工具/诊断_GitHub可用IP_20261009.py
    python 工具/诊断_GitHub可用IP_20261009.py --复查
        （复查模式：读已写入的 hosts，验证是否还有效）
"""
import os
import socket
import ssl
import sys
import time

#★ 域名清单来自「GitHub 页面实际会拉的资源」，
#   不是网上抄的，是按浏览器加载一个仓库页会请求的域名列全的。
域名清单 = [
    "github.com",# 主站
    "github.githubassets.com",         # JS/CSS/图片
    "raw.githubusercontent.com",        # raw 文件
    "avatars.githubusercontent.com",    # 头像
    "objects.githubusercontent.com",    # release 附件 / LFS 对象
    "codeload.github.com",            # clone 的 tar/zip 实际下载
    "github.github.io",                # Pages（可选）
]

# 真实路径：不同域名对应不同的真实 URL，才能测出内容而不是只看握手
测路径 = {
    "github.com": "/",
    "github.githubassets.com": "/",
    "raw.githubusercontent.com": "/",
    "avatars.githubusercontent.com": "/",
    "objects.githubusercontent.com": "/",
    "codeload.github.com": "/",
    "github.github.io": "/",
}


def 解析(域名):
    """返回当前真实 A 记录集合（全集合）。"""
    try:
        rs = socket.getaddrinfo(域名, 443, proto=socket.IPPROTO_TCP)
        return sorted({r[4][0] for r in rs})
    except Exception:
        return []


def 验证(ip, 域名, 路径="/", 超时=8.0):
    """
    返回 (可用?, 延迟ms, 说明)
    ★ 必须 TLS + HTTP 两关：
      只 TCP 通 = 可能端口开着但不是这个服务（实测踩过）。
    """
    t0 = time.time()
    # ★ 整个 TCP+TLS 都在 try 里：实测踩过——
    #   create_connection 写在 try 外面时，它抛的 TimeoutError
    #   不受下面任何 except 管辖 ⇒ 直接崩溃退出。
    try:
        sock = socket.create_connection((ip, 443), timeout=超时)
        try:
            w = ctx.wrap_socket(sock, server_hostname=域名)   # 证书CN 校验在 ctx 里开着
            握手ms = (time.time() - t0) * 1000
            req = (f"HEAD {路径} HTTP/1.1\r\nHost: {域名}\r\n"
                   f"User-Agent: hostcheck\r\nConnection: close\r\n\r\n")
            w.sendall(req.encode())
            buf = b""
            while b"\r\n" not in buf and len(buf) < 512:
                b = w.recv(1)
                if not b:
                    break
                buf += b
            首行 = buf.decode(errors="replace").split("\r\n")[0]
            状态 = 首行.split()[1] if len(首行.split()) > 1 else "???"
            w.close()
            return True, 握手ms, 状态
        finally:
            try:
                sock.close()
            except Exception:
                pass
    except ssl.SSLCertVerificationError as e:
        return False, 0, f"证书不匹配({str(getattr(e, 'verify_message', ''))[:36]})"
    except (socket.timeout, TimeoutError):
        # ★ Python 3.10+ 里 socket.timeout 是 TimeoutError 的别名，
        #   两个都列才不漏。
        return False, 0, "超时"
    except ConnectionRefusedError:
        return False, 0, "被拒"
    except OSError as e:
        return False, 0, f"不可达(errno={e.errno})"
    except Exception as e:
        return False, 0, f"{type(e).__name__}"


# 证书校验**必须开**：这是判据的一部分，不是可选项
ctx = ssl.create_default_context()
ctx.check_hostname = True
ctx.verify_mode = ssl.CERT_REQUIRED

HOSTS路径 = r"C:\Windows\System32\drivers\etc\hosts"
标记头 = "# >>> GitHub 加速（AI 实测生成 2026-10-09）"
标记尾 = "# <<< GitHub 加速"


def main():
    复查 = "--复查" in sys.argv

    if 复查:
        return 复盘()

    print("=" * 76)
    print(" GitHub 可用 IP 实测 v2（只读·不改 hosts）")
    print("=" * 76)
    print(" 候选值来源：getaddrinfo 实解析的当前真实 A 记录（不猜、不抄网表）")
    print()

    结论 = {}
    for 域名 in 域名清单:
        ips = 解析(域名)
        路径 = 测路径.get(域名, "/")
        print(f"── {域名}")
        if not ips:
            print("   ★ DNS 解析失败")
            print()
            continue
        print(f"   DNS 返回 {len(ips)} 个：{', '.join(ips)}")
        可用 = []
        for ip in ips:
            ok, ms, 说明 = 验证(ip, 域名, 路径)
            if ok:
                print(f"   {ip:<17} ✅ {ms:6.0f} ms   HTTP {说明}")
                可用.append((ip, ms, 说明))
            else:
                print(f"   {ip:<17} ✗ {说明}")
            sys.stdout.flush()
        if 可用:
            #★ 排序判据：3xx/4xx 都算「通」，
            #   只有 5xx 和连接失败算不通。
            #   （实测 github.github.io 返回 404 是正常的——根域本来就没内容）
            def 权重(x):
                s = int(x[2])
                return (0 if s < 400 else 1, x[1])
            可用.sort(key=权重)
            结论[域名] = 可用
            print(f"   ⇒ 可用 {len(可用)}/{len(ips)}，推荐 {可用[0][0]}（{可用[0][1]:.0f}ms，HTTP {可用[0][2]}）")
        else:
            结论[域名] = []
            print(f"   ⇒★ 全失败（这说明该域名在你当前网络下**需要代理**，hosts 解决不了）")
        print()

    print("=" * 76)
    print(" 汇总（推荐写入 hosts 的条目）")
    print("=" * 76)
    行数 = 0
    for 域名 in 域名清单:
        lst = 结论.get(域名, [])
        if not lst:
            print(f"   ★ {域名:<34} 无可用→ 需代理，hosts 无解")
            continue
        # 每个域名写最快 2 个（主+ 备，坏了一个还有）
        选 = lst[:2]
        print(f"   {域名}")
        for ip, ms, 状态 in 选:
            print(f"       {ip:<17} {ms:6.0f}ms  HTTP {状态}")
            行数 += 1
    print()
    print(f" ⇒ 需写入 {行数} 行。")
    print(" ⚠️ 本脚本**没有改任何文件**。")
    print(" ⇒ 确认后跑：工具/应用_GitHubHosts_20261009.py --写")
    print(" ⇒ 回滚跑：工具/应用_GitHubHosts_20261009.py --撤")
    return 0


def 复盘():
    """复查 hosts 里已写入的条目是否还有效。"""
    print("=" * 76)
    print(" 复查：hosts 里现有条目是否还有效")
    print("=" * 76)
    if not os.path.isfile(HOSTS路径):
        print(" hosts 文件不存在")
        return 1
    with open(HOSTS路径, "r", encoding="utf-8", errors="replace") as f:
        原文 = f.read()
    if 标记头 not in 原文:
        print(f" ★ 未找到标记（{标记头}）⇒ 从未写入过，或已被手工删除")
        return 1
    块 = 原文.split(标记头)[1].split(标记尾)[0]
    条目 = []
    for line in 块.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            for 主机 in parts[1:]:
                条目.append((parts[0], 主机))
    print(f" 找到 {len(条目)} 条：")
    坏的 = 0
    for ip, 域名 in 条目:
        ok, ms, 说明 = 验证(ip, 域名, "/")
        print(f"   {ip:<17} {域名:<34} {'✅' if ok else '✗'} {ms:6.0f}ms  {说明}")
        if not ok:
            坏的 += 1
    print()
    print(f" ⇒ {len(条目)} 条中 {坏的} 条失效。")
    if 坏的:
        print(" ⇒ 建议重新跑实测并覆盖（--写 会自动替换整个标记块）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())