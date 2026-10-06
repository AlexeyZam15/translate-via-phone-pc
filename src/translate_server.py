# src/translate_server.py
"""HTTP-сервер перевода на CTranslate2 + Opus-MT.

ИЗМЕНЕНО:
  - HTTPServer → ThreadingHTTPServer. Параллельные запросы
    не ждут друг друга.
  - _ct2_lock вокруг CTranslate2 translate_batch — движок
    не thread-safe, вызовы сериализуются.
  - log_translate_input(texts) — логирует вход переводчика.
  - Параметры декодирования из профиля качества.
  - Прогрев CTranslate2 при старте.
  - Кэш переводов (dict + Lock, максимум 5000 записей).
"""

import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src.config import (
    PC_TRANSLATE_PORT,
    APP_DIR,
    TRANSLATE_PROFILE_CONFIGS,
)
from src.settings_manager import settings_manager
from src.debug_logger import logger as dbg
from src.text_logger import log_translate_text, log_translate_input


def _get_models_dir() -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = APP_DIR
    return os.path.join(base, "models")


def _get_optimal_threads() -> tuple:
    try:
        import psutil
        physical = psutil.cpu_count(logical=False) or 4
    except ImportError:
        physical = os.cpu_count() or 4

    intra = max(2, int(physical * 0.7))
    inter = 1
    return intra, inter


_ct2_available = False
_ct2_translator = None
_sp_source = None
_sp_target = None
_ct2_lock = threading.Lock()

_translation_cache = {}
_cache_lock = threading.Lock()
CACHE_MAX_SIZE = 5000

DEFAULT_SOURCE = "en"
DEFAULT_TARGET = "ru"

MODEL_DIR_NAME_ORIG = f"opus-mt-{DEFAULT_SOURCE}-{DEFAULT_TARGET}-ct2-int8-orig"
MODEL_DIR_NAME_FALLBACK = f"opus-mt-{DEFAULT_SOURCE}-{DEFAULT_TARGET}-ct2-int8"

INTRA_THREADS, INTER_THREADS = _get_optimal_threads()


def _get_profile_config():
    profile = settings_manager.get("translate_profile", "speed")
    cfg = TRANSLATE_PROFILE_CONFIGS.get(profile)
    if cfg is None:
        cfg = TRANSLATE_PROFILE_CONFIGS["speed"]
        profile = "speed"
    return profile, cfg


def _check_model_dir(path: str) -> bool:
    required = ["model.bin", "config.json", "source.spm", "target.spm"]
    if not os.path.isdir(path):
        return False
    return all(os.path.exists(os.path.join(path, f)) for f in required)


def _warmup_ctranslate2():
    if not _ct2_available or _ct2_translator is None:
        return
    if _sp_source is None or _sp_target is None:
        return
    try:
        t0 = time.perf_counter()
        tokens = _sp_source.encode("hello world", out_type=str) + ["</s>"]
        with _ct2_lock:
            _ct2_translator.translate_batch(
                [tokens],
                beam_size=1, max_batch_size=1,
                repetition_penalty=1.0, no_repeat_ngram_size=0,
                max_decoding_length=16,
            )
        ms = (time.perf_counter() - t0) * 1000.0
        print(f"[Translate-Server] ✅ CTranslate2 прогрет ({ms:.0f} мс)")
        dbg.log_event("pc.translate_server_warmup", {"ms": round(ms, 1)})
    except Exception as e:
        print(f"[Translate-Server] ⚠️ Прогрев не удался: {e}")


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
            profile, cfg = _get_profile_config()
            print(f"[Translate-Server] ✅ CTranslate2 модель загружена: {os.path.basename(model_path)}")
            print(f"[Translate-Server]    intra_threads={INTRA_THREADS}, "
                  f"inter_threads={INTER_THREADS}, cache_max={CACHE_MAX_SIZE}")
            print(f"[Translate-Server]    профиль по умолчанию: {profile} "
                  f"(beam={cfg['beam_size']}, rep_pen={cfg['repetition_penalty']}, "
                  f"no_repeat={cfg['no_repeat_ngram_size']}, "
                  f"max_len={cfg['max_decoding_length']})")
            dbg.log_event("pc.translate_server_ct2_ok",
                          {"model": os.path.basename(model_path),
                           "intra_threads": INTRA_THREADS,
                           "inter_threads": INTER_THREADS,
                           "default_profile": profile,
                           "cache_max": CACHE_MAX_SIZE})

            # Прогрев без удержания _ct2_lock (иначе _init и warmup — deadlock).
        except Exception as e:
            print(f"[Translate-Server] ❌ Ошибка загрузки модели: {e}")
            traceback.print_exc()
            dbg.log_event("pc.translate_server_ct2_error", {"error": str(e)})
            return

    # Прогрев вне _ct2_lock, чтобы не блокировать инициализацию.
    _warmup_ctranslate2()


def _cache_get(key):
    with _cache_lock:
        return _translation_cache.get(key)


def _cache_set(key, value):
    with _cache_lock:
        if len(_translation_cache) >= CACHE_MAX_SIZE:
            try:
                _translation_cache.pop(next(iter(_translation_cache)))
            except Exception:
                pass
        _translation_cache[key] = value


def _translate_ct2(texts, source, target):
    if not _ct2_available or _ct2_translator is None:
        return None
    if _sp_source is None or _sp_target is None:
        return None

    try:
        profile, cfg = _get_profile_config()
        beam_size = cfg["beam_size"]
        rep_penalty = cfg["repetition_penalty"]
        no_repeat = cfg["no_repeat_ngram_size"]
        max_len = cfg["max_decoding_length"]

        results = [None] * len(texts)
        to_translate = []
        for i, t in enumerate(texts):
            if not t or not t.strip():
                results[i] = ""
                continue
            cache_key = (t, source, target, profile)
            cached = _cache_get(cache_key)
            if cached is not None:
                results[i] = cached
            else:
                to_translate.append((i, t))

        if not to_translate:
            return results

        tokenized = []
        for _, t in to_translate:
            tokens = _sp_source.encode(t, out_type=str) + ["</s>"]
            tokenized.append(tokens)

        # CTranslate2 не thread-safe — сериализуем вызовы.
        with _ct2_lock:
            ct_results = _ct2_translator.translate_batch(
                tokenized,
                beam_size=beam_size,
                max_batch_size=16,
                repetition_penalty=rep_penalty,
                no_repeat_ngram_size=no_repeat,
                max_decoding_length=max_len,
            )

        for (idx, orig_text), r in zip(to_translate, ct_results):
            hyp = r.hypotheses[0] if r.hypotheses else []
            decoded = _sp_target.decode(hyp)
            results[idx] = decoded
            _cache_set((orig_text, source, target, profile), decoded)

        return results
    except Exception as e:
        print(f"[Translate-Server] ❌ ct2 translate error: {e}")
        traceback.print_exc()
        dbg.log_event("pc.translate_server_ct2_translate_error",
                      {"error": str(e), "traceback": traceback.format_exc()})
        return None


class TranslateServer:
    """HTTP-сервер перевода. ThreadingHTTPServer."""

    def __init__(self):
        self.server = None
        self.server_thread = None
        self.running = False
        self._request_count = 0
        self._cache_hits = 0

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

                    try:
                        data = json.loads(body.decode('utf-8'))
                    except json.JSONDecodeError as e:
                        print(f"[Translate-Server] ❌ JSON decode error: {e}")
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

                    try:
                        log_translate_input(texts)
                    except Exception:
                        pass

                    profile, _ = _get_profile_config()

                    translations = _translate_ct2(texts, source, target)
                    engine = "ctranslate2"
                    if translations is None:
                        translations = [""] * len(texts)
                        engine = "none"

                    ms = (time.perf_counter() - t_start) * 1000.0
                    parent._request_count += 1
                    if parent._request_count % 20 == 1:
                        with _cache_lock:
                            cache_size = len(_translation_cache)
                        print(f"[Translate-Server] 📥 #{parent._request_count}: "
                              f"{len(texts)} texts, engine={engine}, "
                              f"profile={profile}, {ms:.0f}ms, "
                              f"cache={cache_size}")

                    dbg.log_timing("pc.translate_server", ms,
                                   {"count": len(texts), "engine": engine,
                                    "profile": profile, "target": target})

                    if translations and engine == "ctranslate2":
                        log_translate_text(texts, translations)

                    self._send_json({"translations": translations,
                                     "engine": engine, "profile": profile})
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
            # ИЗМЕНЕНО: ThreadingHTTPServer вместо HTTPServer.
            self.server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
            self.server.daemon_threads = True
            self.server_thread = threading.Thread(
                target=self.server.serve_forever, daemon=True
            )
            self.server_thread.start()
            self.running = True
            print(f"[Translate-Server] ✅ HTTP-сервер (threading) запущен на порту {port}")
            dbg.log_event("pc.translate_server_started", {"port": port, "threading": True})
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
        with _cache_lock:
            cache_size = len(_translation_cache)
        print(f"[Translate-Server] 👋 HTTP-сервер остановлен (кэш: {cache_size})")
        dbg.log_event("pc.translate_server_stopped", {"cache_size": cache_size})