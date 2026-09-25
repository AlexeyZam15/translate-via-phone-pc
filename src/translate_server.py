# src/translate_server.py
"""HTTP-сервер перевода на CTranslate2 + Opus-MT.

ИЗМЕНЕНО (для устранения зависаний):
  - ThreadingHTTPServer → HTTPServer (single-threaded) — нет конкуренции.
  - no_repeat_ngram_size убран — подозрение на зацикливание.
  - intra_threads=2, inter_threads=1 — меньше конкуренции.
  - Логирование до/после translate_batch.
  - traceback в except — видно ошибку.
"""

import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer  # single-threaded

from src.config import PC_TRANSLATE_PORT, APP_DIR
from src.debug_logger import logger as dbg


def _get_models_dir() -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = APP_DIR
    return os.path.join(base, "models")


_ct2_available = False
_ct2_translator = None
_sp_source = None
_sp_target = None
_ct2_lock = threading.Lock()

DEFAULT_SOURCE = "en"
DEFAULT_TARGET = "ru"

MODEL_DIR_NAME_ORIG = f"opus-mt-{DEFAULT_SOURCE}-{DEFAULT_TARGET}-ct2-int8-orig"
MODEL_DIR_NAME_FALLBACK = f"opus-mt-{DEFAULT_SOURCE}-{DEFAULT_TARGET}-ct2-int8"

# Параметры декодирования (компромисс качество/скорость)
BEAM_SIZE = 3
MAX_DECODING_LENGTH = 192
REPETITION_PENALTY = 1.15
# no_repeat_ngram_size УБРАН — подозрение на зацикливание

INTRA_THREADS = 2
INTER_THREADS = 1


def _check_model_dir(path: str) -> bool:
    required = ["model.bin", "config.json", "source.spm", "target.spm"]
    if not os.path.isdir(path):
        return False
    return all(os.path.exists(os.path.join(path, f)) for f in required)


def _init_ctranslate2():
    global _ct2_available, _ct2_translator, _sp_source, _sp_target
    with _ct2_lock:
        if _ct2_available:
            return
        try:
            import ctranslate2
            import sentencepiece as spm
        except ImportError as e:
            print(f"[Translate-Server] ⚠️ CTranslate2/SentencePiece не установлены: {e}")
            dbg.log_event("pc.translate_server_ct2_missing", {"error": str(e)})
            return

        models_dir = _get_models_dir()
        model_path_orig = os.path.join(models_dir, MODEL_DIR_NAME_ORIG)
        model_path_fallback = os.path.join(models_dir, MODEL_DIR_NAME_FALLBACK)

        if _check_model_dir(model_path_orig):
            model_path = model_path_orig
            print(f"[Translate-Server] ℹ️ Используется оригинальная модель: {MODEL_DIR_NAME_ORIG}")
        elif _check_model_dir(model_path_fallback):
            model_path = model_path_fallback
            print(f"[Translate-Server] ⚠️ Fallback-модель: {MODEL_DIR_NAME_FALLBACK}")
        else:
            print(f"[Translate-Server] ⚠️ Модель не найдена")
            dbg.log_event("pc.translate_server_model_missing",
                          {"orig": model_path_orig, "fallback": model_path_fallback})
            return

        try:
            source_spm = os.path.join(model_path, "source.spm")
            target_spm = os.path.join(model_path, "target.spm")
            _sp_source = spm.SentencePieceProcessor(model_file=source_spm)
            _sp_target = spm.SentencePieceProcessor(model_file=target_spm)

            _ct2_translator = ctranslate2.Translator(
                model_path,
                device="cpu",
                compute_type="int8",
                inter_threads=INTER_THREADS,
                intra_threads=INTRA_THREADS,
            )
            _ct2_available = True
            print(f"[Translate-Server] ✅ CTranslate2 модель загружена: {os.path.basename(model_path)}")
            print(f"[Translate-Server]    beam_size={BEAM_SIZE}, "
                  f"intra_threads={INTRA_THREADS}, inter_threads={INTER_THREADS}, "
                  f"max_dec_len={MAX_DECODING_LENGTH}, "
                  f"rep_penalty={REPETITION_PENALTY}")
            dbg.log_event("pc.translate_server_ct2_ok",
                          {"model": os.path.basename(model_path),
                           "beam_size": BEAM_SIZE,
                           "intra_threads": INTRA_THREADS,
                           "inter_threads": INTER_THREADS})
        except Exception as e:
            print(f"[Translate-Server] ❌ Ошибка загрузки модели: {e}")
            traceback.print_exc()
            dbg.log_event("pc.translate_server_ct2_error", {"error": str(e)})


def _translate_ct2(texts, source, target):
    """Перевод через CTranslate2. С логированием и без no_repeat_ngram_size."""
    if not _ct2_available or _ct2_translator is None:
        return None
    if _sp_source is None or _sp_target is None:
        return None

    try:
        tokenized = []
        for t in texts:
            if not t or not t.strip():
                tokenized.append([])
                continue
            tokens = _sp_source.encode(t, out_type=str) + ["</s>"]
            tokenized.append(tokens)

        print(f"[Translate-Server] → translate_batch (count={len(tokenized)})")

        # ИЗМЕНЕНО: без no_repeat_ngram_size
        results = _ct2_translator.translate_batch(
            tokenized,
            beam_size=BEAM_SIZE,
            max_batch_size=16,
            repetition_penalty=REPETITION_PENALTY,
            max_decoding_length=MAX_DECODING_LENGTH,
        )

        print(f"[Translate-Server] ← translate_batch done")

        out = []
        for i, r in enumerate(results):
            if not tokenized[i]:
                out.append("")
                continue
            hyp = r.hypotheses[0] if r.hypotheses else []
            decoded = _sp_target.decode(hyp)
            out.append(decoded)
        return out
    except Exception as e:
        print(f"[Translate-Server] ❌ ct2 translate error: {e}")
        traceback.print_exc()
        dbg.log_event("pc.translate_server_ct2_translate_error",
                      {"error": str(e), "traceback": traceback.format_exc()})
        return None


class TranslateServer:
    """HTTP-сервер перевода. ИЗМЕНЕНО: single-threaded."""

    def __init__(self):
        self.server = None
        self.server_thread = None
        self.running = False
        self._request_count = 0

    def start(self, port=PC_TRANSLATE_PORT):
        if self.running:
            return True

        _init_ctranslate2()

        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != '/translate':
                    self.send_error(404)
                    return
                try:
                    t_start = time.perf_counter()
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length)

                    print(f"[Translate-Server] ← POST /translate ({length} bytes)")

                    try:
                        data = json.loads(body.decode('utf-8'))
                    except json.JSONDecodeError as e:
                        print(f"[Translate-Server] ❌ JSON decode error: {e}")
                        print(f"[Translate-Server]    body: {body[:200]}")
                        self.send_error(400, "Bad JSON")
                        return

                    texts = data.get('texts', [])
                    target = data.get('target', DEFAULT_TARGET)
                    source = data.get('source', 'auto')
                    if source == 'auto' or not source:
                        source = DEFAULT_SOURCE

                    if not texts:
                        self._send_json({"translations": [], "engine": "none"})
                        return

                    translations = _translate_ct2(texts, source, target)
                    engine = "ctranslate2"
                    if translations is None:
                        translations = [""] * len(texts)
                        engine = "none"

                    ms = (time.perf_counter() - t_start) * 1000.0
                    parent._request_count += 1
                    if parent._request_count % 10 == 1:
                        print(f"[Translate-Server] 📥 #{parent._request_count}: "
                              f"{len(texts)} texts, engine={engine}, {ms:.0f}ms")

                    dbg.log_timing("pc.translate_server", ms,
                                   {"count": len(texts), "engine": engine,
                                    "target": target})

                    self._send_json({"translations": translations, "engine": engine})
                except Exception as e:
                    print(f"[Translate-Server] ⚠️ Handler error: {e}")
                    traceback.print_exc()
                    dbg.log_event("pc.translate_server_error",
                                  {"error": str(e), "traceback": traceback.format_exc()})
                    self.send_error(500)

            def _send_json(self, obj):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps(obj, ensure_ascii=False).encode('utf-8'))

            def log_message(self, fmt, *args):
                pass

        try:
            # ИЗМЕНЕНО: HTTPServer (single-threaded) вместо ThreadingHTTPServer
            self.server = HTTPServer(('0.0.0.0', port), Handler)
            self.server_thread = threading.Thread(
                target=self.server.serve_forever, daemon=True
            )
            self.server_thread.start()
            self.running = True
            print(f"[Translate-Server] ✅ HTTP-сервер (single-threaded) запущен на порту {port}")
            dbg.log_event("pc.translate_server_started", {"port": port, "threading": False})
            return True
        except Exception as e:
            print(f"[Translate-Server] ❌ Ошибка запуска: {e}")
            traceback.print_exc()
            dbg.log_event("pc.translate_server_error", {"error": str(e)})
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
        print("[Translate-Server] 👋 HTTP-сервер остановлен")
        dbg.log_event("pc.translate_server_stopped")