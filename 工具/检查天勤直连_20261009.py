# -*- coding: utf-8 -*-
"""
验证：Clash 代理开启后，天勤链路是否真的走直连（没被代理接管）
================================================================
★为什么这个必须实测，不能看配置就下结论：
  规则写对了 ≠ 生效了。可能的原因：
    - 订阅导入时 Merge 没被应用
    - 规则顺序错了（自己的规则排在 MATCH 之后，永远轮不到）
    - 开了 TUN 模式 ⇒ 系统代理开关不代表真实接管情况
    - 天勤实际连的域名不在规则里

★ 判据（按记忆铁律：**用真实场景测，不用连通性测**）：
  1. 读 Clash 的运行时配置（不是 Merge 模板）⇒ 确认规则真的进去了
  2. 查 mihomo 的连接日志/连接列表 ⇒ 看天勤域名的实际出口是 DIRECT 还是代理
  3. 真实连一次 api.shinnytech.com，看能否建连

用法：
    python 工具/检查天勤直连_20261009.py
    python 工具/检查天勤直连_20261009.py --实时    （保持 30s 观察新连接）
"""
import json
import os
import socket
import ssl
import sys
import time
import urllib.request

VERGE = 'C:/Users/cc/AppData/Roaming/io.github.clash-verge-rev.clash-verge-rev'
# ★ 2026-10-09 修正：Clash Verge Rev 内核生成的运行时配置叫 clash-verge.yaml，
#   不是 mihomo 原生的 runtime.yaml（原值导致本项永远误报「规则没进运行时」）。
RUNNING = os.path.join(VERGE, 'clash-verge.yaml')
CLASH_DIRS = [VERGE, os.path.join(VERGE, 'profiles')]
控制端口 = None

# tqsdk 真实会连的域名（实测 grep 出来的，全集合不抽样）
天勤域名 = [
    'api.shinnytech.com',                # 行情 + 交易主链路
    'auth.shinnytech.com',               # 鉴权
    'md-settlement-system-fc-api.shinnytech.com',   # 四级子域
    'symbol-ranking-system-fc-api.shinnytech.com',  # 四级子域
    'account.shinnytech.com',
    'edb.shinnytech.com',
    'openmd.shinnytech.com',
]


def 找控制端口():
    """从配置里读 controller 端口（mihomo 的运行时 API）。"""
    for f in ['config.yaml', 'clash-verge.yaml', 'runtime.yaml']:
        p = os.path.join(VERGE, f)
        if not os.path.isfile(p):
            continue
        try:
            txt = open(p, encoding='utf-8', errors='replace').read()
            for line in txt.splitlines():
                line = line.split('#')[0].strip()
                if line.startswith(('external-controller:', 'external-controller :')):
                    v = line.split(':', 1)[1].strip()
                    if v.startswith('0.0.0.0:'):
                        v = v.split(':', 1)[1]
                    return v
        except Exception:
            pass
    return None


def 读运行时配置():
    """★ 读 clash-verge.yaml（真正生效的配置），而不是 Merge 模板。"""
    if not os.path.isfile(RUNNING):
        return None
    try:
        return open(RUNNING, encoding='utf-8', errors='replace').read()
    except Exception:
        return None


def 检查规则在运行时里():
    cfg = 读运行时配置()
    if cfg is None:
        return None, "clash-verge.yaml 不存在（还没启动过内核）"
    命中 = []
    for r in ['DOMAIN-SUFFIX,shinnytech.com,DIRECT', 'GEOIP,CN,DIRECT']:
        命中.append((r, r in cfg))
    # 顺序检查：必须排在 MATCH 之前
    顺序OK = None
    try:
        # 粗略取第一处 shinnytech 规则 与 MATCH 的下标
        i = cfg.find('shinnytech.com,DIRECT')
        j = cfg.find('MATCH')
        if i >= 0 and j > 0:
            顺序OK = i < j
    except Exception:
        pass
    return (命中, 顺序OK), None


def 直接连(host, 超时=8):
    """★ 不走代理直连天勤，看是否可达（这是模拟盘的基线路径）。"""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        t0 = time.time()
        ip = socket.gethostbyname(host)
        s = socket.create_connection((ip, 443), timeout=超时)
        w = ctx.wrap_socket(s, server_hostname=host)
        ms = (time.time() - t0) * 1000
        w.close()
        return True, f"{ip}  {ms:.0f}ms"
    except Exception as e:
        return False, f"{type(e).__name__}"


def 查连接表():
    """通过 mihomo 的外部 controller 查当前连接，判断出口。"""
    if not 控制端口:
        return None
    try:
        url = f'http://127.0.0.1:{控制端口}/connections'
        with urllib.request.urlopen(url, timeout=4) as r:
            data = json.loads(r.read().decode())
        return data.get('connections', [])
    except Exception as e:
        return f'ERR {type(e).__name__}'
    return None


def main():
    实时 = '--实时' in sys.argv

    print('=' * 72)
    print(' 天勤直连检查（Clash 开启后跑这个）')
    print('=' * 72)

    # ---- 1. 系统代理状态 ----
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r'Software\Microsoft\Windows\CurrentVersion\Internet Settings')
        pe = winreg.QueryValueEx(k, 'ProxyEnable')[0]
        ps = winreg.QueryValueEx(k, 'ProxyServer')
        ps = ps[0] if isinstance(ps, tuple) else ps
        print(f'\n[1] 系统代理: ProxyEnable={pe}' + (f'  Server={ps}' if pe else '（未开）'))
    except Exception as e:
        print(f'\n[1] 系统代理: 读取失败 {e}')

    # ---- 2. TUN 模式 ----
    verge = os.path.join(VERGE, 'verge.yaml')
    if os.path.isfile(verge):
        t = open(verge, encoding='utf-8', errors='replace').read()
        tun = [l for l in t.splitlines() if 'enable_tun_mode' in l]
        sp = [l for l in t.splitlines() if 'enable_system_proxy' in l]
        print(f'[2] {tun[0].strip() if tun else "enable_tun_mode: ?"}   '
              f'←TUN 开着会绕过系统代理开关直接接管全部流量')
        print(f'    {sp[0].strip() if sp else "enable_system_proxy: ?"}')

    # ---- 3. 订阅是否已导入 ----
    prof = os.path.join(VERGE, 'profiles.yaml')
    it = open(prof, encoding='utf-8', errors='replace').read() if os.path.isfile(prof) else ''
    已导入 = 'items:' in it and it.count('uid:') > 0
    print(f'\n[3] 订阅已导入: {"是" if 已导入 else "★ 否（还没导入）"}')

    # ---- 4. 规则是否进了运行时 ----
    res, err = 检查规则在运行时里()
    print('\n[4] 天勤规则是否已进入运行时配置:')
    if err:
        print(f'    ★ {err}')
    elif res:
        (命中, 顺序OK), = [res]
        for r, ok in 命中:
            print(f'    {"✅" if ok else "★ 缺失"}  {r}')
        if 顺序OK is False:
            print('    ★★ 顺序不对：自己的规则排在 MATCH 之后 ⇒ 永远不会被匹配')
        elif 顺序OK:
            print('    ✅ 顺序正确（在 MATCH 之前）')

    # ---- 5. 天勤域名直连可达性 ----
    print('\n[5] 天勤域名直连可达性（模拟盘基线路径）:')
    for h in 天勤域名:
        ok, info = 直接连(h)
        print(f'    {"✅" if ok else "✗"}  {h:<44} {info}')

    # ---- 6. mihomo 连接表（关键证据）----
    conns = 查连接表()
    print('\n[6] mihomo 当前连接（看天勤走哪个出口）:')
    if conns is None:
        print('    （读不到 controller，运行时验证跳过）')
        print(f'    代理判断请看 [5]：若 [5] 全✅，说明本机直连可用。')
    elif isinstance(conns, str):
        print(f'    ★ {conns}')
    else:
        天勤conn = [c for c in conns
                   if any(k in str(c.get('metadata', {}).get('host', ''))
                          for k in ['shinnytech', 'tqsdk'])]
        if not 天勤conn:
            print('    当前无天勤连接（模拟盘没在跑 ⇒ 正常）')
            print('    ★ 判断方法：跑模拟盘时再看这里，')
            print('      host=api.shinnytech.com 且 chains 里有 DIRECT = 正确')
            print('      chains 里出现代理节点名 = ★ 天勤被代理接管了')
        for c in 天勤conn:
            m = c.get('metadata', {})
            print(f"    host={m.get('host')}  chains={c.get('chains')}")

    if 实时:
        print('\n[7] 实时观察 30 秒…（现在去启动模拟盘）')
        for i in range(6):
            time.sleep(5)
            c = 查连接表()
            if isinstance(c, list):
                t = [x for x in c if 'shinnytech' in str(x.get('metadata', {}).get('host', ''))]
                for x in t:
                    print(f"    [{i*5+5:2d}s] {x['metadata']['host']}  chains={x.get('chains')}")
                if t:
                    break
        else:
            print('    30 秒内没捕捉到天勤连接（模拟盘可能没启动）')

    print('\n' + '=' * 72)
    return 0


if __name__ == '__main__':
    sys.exit(main())