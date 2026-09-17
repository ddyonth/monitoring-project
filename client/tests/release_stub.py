"""
Стаб сервера релизов для тестов самообновления: настоящий локальный
http.server с теми же двумя эндпоинтами, что у сервера мониторинга
(GET /api/client-release и GET /api/download/client-agent), без моков.

Используется юнит-тестами (порт 0, любой свободный) и сквозным тестом на
настоящих exe (порт 8000: адрес сервера вшит в exe, см. test_selfupdate_e2e.py).
"""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

CLIENT_KEY = "TEST_CLIENT_KEY"


class FakeReleaseServer:
    """Минимальный сервер релизов: /api/client-release и /api/download/client-agent."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, client_key: str = CLIENT_KEY):
        self.release = None          # dict(version, sha256, size_bytes) или None
        self.data = b""              # байты, которые отдаёт download
        self.requests = []           # (path, client_key)
        self.client_key = client_key
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # тишина в выводе pytest
                pass

            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("X-Client-Key")))
                if self.headers.get("X-Client-Key") != outer.client_key:
                    self.send_response(401)
                    self.end_headers()
                    return
                if self.path == "/api/client-release":
                    body = json.dumps({"client_release": outer.release}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/api/download/client-agent":
                    if outer.release is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(outer.data)))
                    self.end_headers()
                    self.wfile.write(outer.data)
                else:
                    self.send_response(404)
                    self.end_headers()

        self.httpd = HTTPServer((host, port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def base_url(self):
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def set_release(self, version, data: bytes, sha256=None):
        """Опубликовать релиз; sha256 можно задать неверный (проверка защиты от подмены)."""
        self.data = data
        self.release = {
            "version": version,
            "sha256": sha256 if sha256 is not None else hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
        }
