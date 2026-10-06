# src/ocr_receiver.py
"""HTTP-сервер, принимает OCR-результаты от Android-приложения.

Плюс отдельный HTTP-сервер на PC_DEBUG_PORT для debug-метрик.

ИЗМЕНЕНО:
  - DebugReceiver фильтрует частые Android-стадии (_ANDROID_SKIP_STAGES).
    Расширен список: убраны все частые стадии, которые оставались в логе
    (ocr_zone_cache_stats, ocr_pass1, pc_ocr_recognize, ocr_total,
    ocr_refine_blocks_count, pc_ocr_cycle_total, zone_cache_invalidated,
    invalidate_on_padding, benchmark_ready_check, benchmark.is_ready,
    benchmark.start/done/http, translator_ready/warmup/prepare/download,
    mjpeg_start/connect_attempt/stop/reconnect, settings_loaded_from_pc).
  - OCR-блоки пишутся в ocr_text.log (только при изменении содержимого).
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
from src.text_logger import log_ocr_text

# ИЗМЕНЕНО: расширен список skip-стадий.
# Все частые android.* стадии не пишутся в debug_timing.jsonl,
# чтобы файл не раздувался.
_ANDROID_SKIP_STAGES = {
    # MJPEG
    "android.mjpeg_read_body",
    "android.mjpeg_frame_size_kb",
    "android.mjpeg_connect",
    "android.mjpeg_connect_attempt",
    "android.mjpeg_start",
    "android.mjpeg_stop",
    "android.mjpeg_reconnect",
    "android.mjpeg_disconnect",
    # OCR pass-стадии
    "android.pc_ocr_decode",
    "android.pc_ocr_decode_failed",
    "android.ocr_pass1_blocks",
    "android.ocr_pass2_blocks",
    "android.ocr_pass2_skipped",
    "android.ocr_pass2",
    "android.ocr_pass1",
    "android.ocr_total",
    "android.ocr_refine_blocks_count",
    "android.ocr_zone_cache_stats",
    "android.ocr_zone_cache_invalidated",
    # PC OCR pipeline (дедуплицируется на Android)
    "android.pc_ocr_pipeline",
    "android.pc_ocr_pipeline_texts",
    "android.pc_ocr_split_numbers",
    "android.pc_ocr_split_single_letters",
    "android.pc_ocr_skip_contained",
    "android.pc_ocr_skip_overlap",
    "android.pc_ocr_drop_fragment",
    "android.pc_ocr_new_blocks",
    "android.pc_ocr_replace_stale",
    "android.pc_ocr_replace_contained",
    "android.pc_ocr_no_translations",
    "android.pc_ocr_recognize",
    "android.pc_ocr_cycle_total",
    "android.pc_ocr_invalidate_on_padding",
    "android.pc_ocr_benchmark_ready_check",
    "android.pc_ocr_worker_error",
    # Sender
    "android.ocr_sender_build_json",
    "android.ocr_sender_http",
    "android.ocr_sender_build_error",
    "android.ocr_sender_error",
    # Benchmark
    "benchmark.is_ready",
    "benchmark.start",
    "benchmark.done",
    "benchmark.http",
    "benchmark.mlkit_ready",
    "benchmark.mlkit_not_ready",
    "benchmark.mlkit_prepare_error",
    "benchmark.http_ping_ok",
    "benchmark.http_ping_fail",
    "benchmark.http_ping_late_ok",
    "benchmark.http_ping_late_fail",
    "benchmark.http_error",
    "benchmark.mlkit_error",
    # Translator
    "android.translator_ready",
    "android.translator_warmup",
    "android.translator_prepare",
    "android.translator_download_models",
    "android.translator_detect_lang",
    "android.translator_detect_lang_batch",
    "android.translator_translate_all_total",
    # Settings
    "android.settings_loaded_from_pc",
    "android.settings_load_from_pc_failed",
}


def _should_skip_android(stage: str) -> bool:
    return stage in _ANDROID_SKIP_STAGES


def _blocks_fingerprint(blocks) -> str:
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

        self._last_fingerprint = None
        self._last_mode_for_fp = None
        self._last_alpha_for_fp = None

        self._request_count = 0
        self._accepted_count = 0
        self._skipped_count = 0

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
                    mode = data.get('mode', 'translate')
                    alpha = int(data.get('overlay_alpha', 217))
                    ts = time.time()

                    parent._request_count += 1

                    fp = _blocks_fingerprint(blocks)
                    with parent.blocks_lock:
                        same = (
                                fp == parent._last_fingerprint
                                and mode == parent._last_mode_for_fp
                                and alpha == parent._last_alpha_for_fp
                                and (fw, fh) == parent.last_frame_size
                        )

                        if same:
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
                            log_ocr_text(blocks, mode=mode)

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
        self._skipped_count = 0
        self._accepted_count = 0

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
                    skipped = 0
                    for rec in records:
                        if not isinstance(rec, dict):
                            continue
                        stage = rec.get("stage", "")
                        if _should_skip_android(stage):
                            skipped += 1
                            continue
                        rec["source"] = "android"
                        dbg._write_raw(rec)
                        n += 1
                    parent._request_count += 1
                    parent._skipped_count += skipped
                    parent._accepted_count += n
                    if parent._request_count % 10 == 1:
                        print(f"[Debug-Receiver] 📥 POST /debug: "
                              f"{n} records (skipped {skipped})")
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(f'{{"ok":true,"n":{n},"skipped":{skipped}}}'.encode())
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
        print(f"[Debug-Receiver] 👋 HTTP-сервер остановлен "
              f"(принято {self._accepted_count}, пропущено {self._skipped_count})")
        dbg.log_event("pc.debug_server_stopped",
                      {"accepted": self._accepted_count,
                       "skipped": self._skipped_count})