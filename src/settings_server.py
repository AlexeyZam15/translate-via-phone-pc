# src/settings_server.py
"""HTTP-сервер настроек на порту 5009.

Android через ADB reverse обращается к http://localhost:5009/settings.

  GET  /settings  → {"debug": false, "translate_engine": "auto", ...}
  POST /settings  → принять JSON, сохранить, применить.

Потокобезопасен (ThreadingMixIn).
"""

import http.server
import json
import socketserver
import threading

from src.config import PC_SETTINGS_PORT
from src.debug_logger import logger as dbg
from src.settings_manager import settings_manager


class SettingsServer:
    """HTTP-сервер настроек."""

    def __init__(self, port: int = PC_SETTINGS_PORT):
        self.port = port
        self.server = None
        self.thread = None

    def start(self) -> bool:
        mgr = settings_manager

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send_json(self, code: int, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:
                    pass

            def do_GET(self):
                if self.path == "/settings" or self.path.startswith("/settings?"):
                    self._send_json(200, mgr.all())
                    return
                self.send_error(404)

            def do_POST(self):
                if self.path != "/settings":
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length) if length > 0 else b""
                    text = raw.decode("utf-8", errors="replace")
                    mgr.from_json(text)
                    dbg.log_event("pc.settings_received", {"json": text[:500]})
                    self._send_json(200, {"ok": True, "settings": mgr.all()})
                except Exception as e:
                    dbg.log_event("pc.settings_receive_error", {"error": str(e)})
                    self._send_json(400, {"ok": False, "error": str(e)})

            def log_message(self, fmt, *args):
                pass

        class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
            allow_reuse_address = True
            daemon_threads = True

        try:
            self.server = ThreadedTCPServer(("0.0.0.0", self.port), Handler)
            self.thread = threading.Thread(
                target=self.server.serve_forever, daemon=True
            )
            self.thread.start()
            dbg.log_event("pc.settings_server_started", {"port": self.port})
            print(f"[Settings-Server] ✅ HTTP-сервер настроек на порту {self.port}")
            return True
        except Exception as e:
            dbg.log_event("pc.settings_server_error", {"error": str(e)})
            print(f"[Settings-Server] ❌ Ошибка: {e}")
            return False

    def stop(self) -> None:
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
            self.server = None
        self.thread = None
        print("[Settings-Server] 👋 HTTP-сервер настроек остановлен")
        dbg.log_event("pc.settings_server_stopped")