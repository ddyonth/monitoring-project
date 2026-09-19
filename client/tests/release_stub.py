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
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, HTTPServer

CLIENT_KEY = "TEST_CLIENT_KEY"

# платформы релизов и правило подстановки — как на сервере (server_app.py)
PLATFORMS = ("windows", "linux")
PLATFORM_DEFAULT = "windows"

Req = namedtuple("Req", "path client_key platform")


def normalize_platform(value):
    """Заголовок X-Client-Platform -> платформа; нет/незнакомое -> windows."""
    p = str(value or "").strip().lower()
    return p if p in PLATFORMS else PLATFORM_DEFAULT


class FakeReleaseServer:
    """Минимальный сервер релизов: /api/client-release и /api/download/client-agent.

    Релиз, опубликованный без платформы (set_release без platform=), отдаётся
    любому агенту: тестам, которым платформа безразлична, не приходится знать,
    под какой ОС идёт прогон. Релиз с явной платформой виден только агенту с
    этой платформой — как на настоящем сервере.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0, client_key: str = CLIENT_KEY):
        self.release = None          # dict(version, sha256, size_bytes) или None — для любой платформы
        self.data = b""              # байты, которые отдаёт download
        self.releases = {}           # platform -> (dict(release), bytes); приоритетнее self.release
        self.requests = []           # Req(path, client_key, platform)
        self.client_key = client_key
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # тишина в выводе pytest
                pass

            def do_GET(self):
                platform = normalize_platform(self.headers.get("X-Client-Platform"))
                outer.requests.append(Req(self.path, self.headers.get("X-Client-Key"), platform))
                if self.headers.get("X-Client-Key") != outer.client_key:
                    self.send_response(401)
                    self.end_headers()
                    return
                release, data = outer.release_for(platform)
                if self.path == "/api/client-release":
                    body = json.dumps({"client_release": release}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/api/download/client-agent":
                    if release is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
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

    def release_for(self, platform):
        """Релиз и байты для платформы: сначала платформенный, иначе общий."""
        if platform in self.releases:
            return self.releases[platform]
        return self.release, self.data

    def set_release(self, version, data: bytes, sha256=None, platform=None):
        """Опубликовать релиз; sha256 можно задать неверный (проверка защиты от подмены).

        platform=None — релиз для любой платформы, иначе только для указанной.
        """
        release = {
            "version": version,
            "sha256": sha256 if sha256 is not None else hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
        }
        if platform is None:
            self.release, self.data = release, data
        else:
            self.releases[platform] = (release, data)
