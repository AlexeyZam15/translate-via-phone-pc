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

# --- Параметры стрима ---
FPS = 10
JPEG_QUALITY = 60
MAX_STREAM_WIDTH = 960

FIRST_FRAME_TIMEOUT = 30

# --- Оверлей ---
OVERLAY_POLL_MS = 500

# ======================== DEBUG TIMING ========================
DEBUG_LOG_ENABLED = True
DEBUG_LOG_FILE = os.path.join(APP_DIR, "debug_timing.jsonl")
DEBUG_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
DEBUG_VERBOSE = False

# ИЗМЕНЕНО: окно агрегации увеличено с 1.0 до 5.0 сек —
# меньше записей в логе.
DEBUG_AGGREGATE_WINDOW_SEC = 5.0

# ИЗМЕНЕНО: список stage, которые НЕ пишутся в лог.
# Это частые технические события (каждую секунду), от которых
# лог раздувается до десятков MB.
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