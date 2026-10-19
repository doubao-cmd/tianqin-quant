# -*- coding: utf-8 -*-
"""本地开发服务器（P0-A HTTP 版）。

python -m http.server 在部分环境把 .js 当 text/plain，浏览器按 ES module 严格
MIME 校验会拒绝执行。这里显式把 .js/.mjs 标成 text/javascript，保证模块能加载。
"""
from __future__ import annotations
import http.server
import socketserver
from pathlib import Path

PORT = 8131
ROOT = Path(__file__).resolve().parent / "dist"


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".html": "text/html; charset=utf-8",
    }

    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(ROOT), **k)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *a):  # 静默
        pass

    def do_GET(self):
        if self.path.split("?")[0] in ("/favicon.ico",):
            self.send_response(204)
            self.end_headers()
            return
        return super().do_GET()


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"复盘终端（HTTP 版）已启动：http://127.0.0.1:{PORT}  （Ctrl+C 退出）")
        httpd.serve_forever()
