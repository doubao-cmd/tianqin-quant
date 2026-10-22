# -*- coding: utf-8 -*-
"""R1 · T2 数据采集：分块下载天勤合约服务 latest.json

为什么不用一次性 get：整文件数 MB，本环境单次大下载会被 SIGTERM。
为什么不用 tqsdk 登录：合约信息的 margin/commission 就来自这个公开静态 JSON
（`tqsdk/api.py:250` `_ins_url` 默认值 + `:3741 _fetch_symbol_info`），
不需要登录、不占用账号、不会踢线上模拟盘进程。

产出：out/_symbols_latest.json（原始下载）+ out/_symbols_meta.json（大小/块数/时间）
"""
import json
import time
from pathlib import Path

import requests

URL = "https://openmd.shinnytech.com/t/md/symbols/latest.json"
OUT = Path(__file__).parent / "out"
CHUNK = 512 * 1024
MAX_BYTES = 64 * 1024 * 1024
PROXIES = {"http": "http://127.0.0.1:54707", "https": "http://127.0.0.1:54707"}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    dst = OUT / "_symbols_latest.json"
    buf = bytearray()
    off = 0
    t0 = time.time()
    while off < MAX_BYTES:
        r = requests.get(URL, headers={"Range": f"bytes={off}-{off + CHUNK - 1}"},
                         timeout=(15, 120), proxies=PROXIES)
        if r.status_code not in (200, 206):
            raise SystemExit(f"HTTP {r.status_code} @ {off}")
        if not r.content:
            break
        buf.extend(r.content)
        off += len(r.content)
        if len(r.content) < CHUNK:
            break
    dst.write_bytes(bytes(buf))
    txt = bytes(buf)
    try:
        data = json.loads(txt.decode("utf-8"))
        ok = f"解析成功，合约数 {len(data)}"
    except Exception as e:
        ok = f"解析失败：{e}"
    meta = {"bytes": len(txt), "chunks": (len(txt) + CHUNK - 1) // CHUNK,
            "seconds": round(time.time() - t0, 1), "parse": ok}
    (OUT / "_symbols_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    print(meta)


if __name__ == "__main__":
    main()
