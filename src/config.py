# src/config.py
"""Все константы проекта."""

import os
import sys


def _get_app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


APP_DIR = _get_app_dir()

# ======================== КОНФИГУРАЦИЯ ========================
PC_TO_PHONE_PORT = 5005  # MJPEG-поток на телефон
PC_OCR_PORT = 5006  # Приём OCR-результатов от телефона
PC_DEBUG_PORT = 5007  # Приём debug-метрик от телефона
PC_TRANSLATE_PORT = 5008  # HTTP-сервер перевода (CTranslate2)
PC_SETTINGS_PORT = 5009  # HTTP-сервер настроек (синхронизация с Android)

# --- Параметры стрима ---
# FPS не важен для OCR — важно качество картинки.
FPS = 6
JPEG_QUALITY = 95
MAX_STREAM_WIDTH = 1920

FIRST_FRAME_TIMEOUT = 30

# --- Оверлей ---
OVERLAY_POLL_MS = 500

# ======================== НАСТРОЙКИ ========================
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")

# Профили качества перевода CTranslate2.
TRANSLATE_PROFILES = ["speed", "balanced", "quality"]

TRANSLATE_PROFILE_CONFIGS = {
    "speed": {
        "beam_size": 1,
        "repetition_penalty": 1.0,
        "no_repeat_ngram_size": 0,
        "max_decoding_length": 128,
    },
    "balanced": {
        "beam_size": 3,
        "repetition_penalty": 1.15,
        "no_repeat_ngram_size": 3,
        "max_decoding_length": 192,
    },
    "quality": {
        "beam_size": 5,
        "repetition_penalty": 1.2,
        "no_repeat_ngram_size": 3,
        "max_decoding_length": 256,
    },
}

TRANSLATE_PROFILE_LABELS = {
    "speed": "Скорость",
    "balanced": "Баланс",
    "quality": "Качество",
}

# Значения по умолчанию. Источник истины — этот словарь.
# Android при старте забирает эти значения с ПК через GET /settings.
DEFAULT_SETTINGS = {
    "debug": False,  # показать OCR-рамки
    "translate_profile": "balanced",  # speed / balanced / quality
    "confidence": 0,  # 0..100 (порог уверенности)
    "ocr_padding": 0,  # 0..50 (отступ OCR, %)
    "overlay_alpha": 0,  # 0..100 (прозрачность фона перевода) — 0 = чёрный фон
    "source_lang": "auto",  # auto / en / ru / ...
    "target_lang": "ru",  # ru / en / ...
    "auto_launch_android": True,  # автоматически запускать Android-приложение
    # SHA-256 последнего APK, успешно установленного на телефон.
    # Используется для версионного контроля: если хэш APK из папки
    # не совпадает с сохранённым — предлагается переустановка.
    "last_installed_apk_hash": "",
}

LANGUAGES = ["auto", "en", "ru", "de", "fr", "es", "it", "zh", "ja"]

# ======================== ТЕКСТОВЫЙ ЛОГ ========================
OCR_TEXT_LOG_FILE = os.path.join(APP_DIR, "ocr_text.log")
OCR_TEXT_LOG_ENABLED = True

# ======================== DEBUG TIMING ========================
DEBUG_LOG_ENABLED = True
DEBUG_LOG_FILE = os.path.join(APP_DIR, "debug_timing.jsonl")
DEBUG_LOG_MAX_BYTES = 10 * 1024 * 1024
DEBUG_VERBOSE = False

DEBUG_AGGREGATE_WINDOW_SEC = 5.0

DEBUG_LOG_SKIP_STAGES = {
    "pc.capture_frame",
    "pc.encode_raw_copy",
    "pc.encode_jpeg",
    "pc.encode_jpeg_size_kb",
    "pc.capture_fps",
    "pc.encode_fps",
    "android.mjpeg_read_body",
    "android.mjpeg_frame_size_kb",
}