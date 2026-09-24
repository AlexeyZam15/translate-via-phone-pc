# src/ocr_receiver.py
"""HTTP-сервер, принимает OCR-результаты от Android-приложения.

Плюс отдельный HTTP-сервер на PC_DEBUG_PORT для debug-метрик.
"""

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src.config import (
    PC_OCR_PORT,
    PC_DEBUG_PORT,
)
from src.debug_logger import logger as dbg


def _blocks_fingerprint(blocks) -> str:
    """
    Возвращает хэш содержимого блоков: текст + координаты + confidence.
    Используется для определения, изменился ли OCR-результат.
    """
    h = hashlib.md5()
    for b in blocks:
        h.update(str(b.get('text', '')).encode('utf-8', errors='ignore'))
        h.update(b'|')
        h.update(str(b.get('x', 0)).encode())
        h.update(b',')
        h.update(str(b.get('y', 0)).encode())
        h.update(b',')
        h.update(str(b.get('w', 0)).encode())
        h.update(b',')
        h.update(str(b.get('h', 0)).encode())
        h.update(b';')
    return h.hexdigest()


class OcrReceiver:
    """HTTP-сервер, принимает результаты OCR от Android-приложения."""

    def __init__(self):
        self.server = None
        self.server_thread = None
        self.running = False

        self.blocks_lock = threading.Lock()
        self.last_blocks = []
        self.last_frame_size = (0, 0)
        self.last_received_ts = 0.0
        self.last_mode = "translate"
        self.last_alpha = 217

        # Отпечаток предыдущих блоков — чтобы не обновлять timestamp
        # при идентичном контенте.
        self._last_fingerprint = None
        self._last_mode_for_fp = None
        self._last_alpha_for_fp = None

        # Счётчики
        self._request_count = 0
        self._accepted_count = 0  # пришли новые данные
        self._skipped_count = 0  # данные идентичны

    def start(self, port=PC_OCR_PORT):
        if self.running:
            return True

        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != '/ocr':
                    self.send_error(404)
                    return
                try:
                    t_start = time.perf_counter()

                    with dbg.timer("pc.ocr_receive_http"):
                        length = int(self.headers.get('Content-Length', 0))
                        body = self.rfile.read(length)

                    with dbg.timer("pc.ocr_parse_json"):
                        data = json.loads(body.decode('utf-8'))

                    blocks = data.get('blocks', [])
                    fw = int(data.get('frame_width', 0))
                    fh = int(data.get('frame_height', 0))
                    # ИЗМЕНЕНО: поддерживаем режим "ocr" (сырой OCR без перевода).
                    mode = data.get('mode', 'translate')
                    alpha = int(data.get('overlay_alpha', 217))
                    ts = time.time()

                    parent._request_count += 1

                    # Проверяем, изменились ли блоки
                    fp = _blocks_fingerprint(blocks)
                    with parent.blocks_lock:
                        same = (
                                fp == parent._last_fingerprint
                                and mode == parent._last_mode_for_fp
                                and alpha == parent._last_alpha_for_fp
                                and (fw, fh) == parent.last_frame_size
                        )

                        if same:
                            # Идентичный результат — не обновляем
                            # last_received_ts и не триггерим перерисовку.
                            parent._skipped_count += 1
                            if parent._request_count % 50 == 0:
                                print(
                                    f"[OCR-Receiver] ♻️ POST /ocr "
                                    f"#{parent._request_count}: без изменений "
                                    f"(пропущено {parent._skipped_count}, "
                                    f"принято {parent._accepted_count})"
                                )
                        else:
                            parent._last_fingerprint = fp
                            parent._last_mode_for_fp = mode
                            parent._last_alpha_for_fp = alpha
                            parent.last_blocks = blocks
                            parent.last_frame_size = (fw, fh)
                            parent.last_received_ts = ts
                            parent.last_mode = mode
                            parent.last_alpha = alpha
                            parent._accepted_count += 1
                            print(
                                f"[OCR-Receiver] 📥 POST /ocr "
                                f"#{parent._request_count}: "
                                f"{len(blocks)} blocks, "
                                f"frame={fw}x{fh}, mode={mode}"
                            )

                    total_ms = (time.perf_counter() - t_start) * 1000.0
                    dbg.log_timing("pc.ocr_total", total_ms,
                                   {"blocks": len(blocks), "mode": mode,
                                    "fw": fw, "fh": fh, "same": same})

                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                except Exception as e:
                    print(f"[OCR-Receiver] ⚠️ Ошибка: {e}")
                    dbg.log_event("pc.ocr_error", {"error": str(e)})
                    self.send_error(500)

            def log_message(self, fmt, *args):
                pass

        try:
            with dbg.timer("pc.ocr_server_start"):
                self.server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
                self.server_thread = threading.Thread(
                    target=self.server.serve_forever, daemon=True
                )
                self.server_thread.start()
                self.running = True
            print(f"[OCR-Receiver] ✅ HTTP-сервер запущен на порту {port}")
            dbg.log_event("pc.ocr_server_started", {"port": port})
            return True
        except Exception as e:
            print(f"[OCR-Receiver] ❌ Ошибка запуска: {e}")
            dbg.log_event("pc.ocr_server_error", {"error": str(e)})
            return False

    def get_blocks(self):
        with self.blocks_lock:
            return list(self.last_blocks), self.last_frame_size

    def get_mode(self):
        with self.blocks_lock:
            return self.last_mode

    def get_alpha(self):
        with self.blocks_lock:
            return self.last_alpha

    def stop(self):
        if not self.running:
            return
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        self.server = None
        self.server_thread = None
        self.running = False
        print(
            f"[OCR-Receiver] 👋 HTTP-сервер остановлен "
            f"(принято {self._accepted_count}, пропущено {self._skipped_count})"
        )
        dbg.log_event("pc.ocr_server_stopped")


class DebugReceiver:
    """Отдельный HTTP-сервер для приёма debug-метрик от Android."""

    def __init__(self):
        self.server = None
        self.server_thread = None
        self.running = False
        self._request_count = 0

    def start(self, port=PC_DEBUG_PORT):
        if self.running:
            return True

        parent = self

        class DebugHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length)
                    records = json.loads(body.decode('utf-8'))
                    if isinstance(records, dict):
                        records = [records]
                    n = 0
                    for rec in records:
                        if not isinstance(rec, dict):
                            continue
                        rec["source"] = "android"
                        dbg._write_raw(rec)
                        n += 1
                    parent._request_count += 1
                    if parent._request_count % 10 == 1:
                        print(f"[Debug-Receiver] 📥 POST /debug: {n} records")
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(f'{{"ok":true,"n":{n}}}'.encode())
                except Exception as e:
                    dbg.log_event("pc.debug_receive_error", {"error": str(e)})
                    self.send_error(500)

            def log_message(self, fmt, *args):
                pass

        try:
            self.server = ThreadingHTTPServer(('0.0.0.0', port), DebugHandler)
            self.server_thread = threading.Thread(
                target=self.server.serve_forever, daemon=True
            )
            self.server_thread.start()
            self.running = True
            print(f"[Debug-Receiver] ✅ HTTP-сервер запущен на порту {port}")
            dbg.log_event("pc.debug_server_started", {"port": port})
            return True
        except Exception as e:
            print(f"[Debug-Receiver] ❌ Ошибка запуска: {e}")
            dbg.log_event("pc.debug_server_error", {"error": str(e)})
            return False

    def stop(self):
        if not self.running:
            return
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        self.server = None
        self.server_thread = None
        self.running = False
        print("[Debug-Receiver] 👋 HTTP-сервер остановлен")
        dbg.log_event("pc.debug_server_stopped")