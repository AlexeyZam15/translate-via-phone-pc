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

# --- Параметры стрима ---
FPS = 10
JPEG_QUALITY = 60
MAX_STREAM_WIDTH = 960

# Увеличено с 10 до 30 секунд: даём пользователю время переключиться
# на подходящее окно после старта трансляции.
FIRST_FRAME_TIMEOUT = 30

# --- Оверлей ---
OVERLAY_POLL_MS = 500

# ======================== DEBUG TIMING ========================
DEBUG_LOG_ENABLED = True
DEBUG_LOG_FILE = os.path.join(APP_DIR, "debug_timing.jsonl")
DEBUG_LOG_MAX_BYTES = 10 * 1024 * 1024
DEBUG_VERBOSE = False
DEBUG_AGGREGATE_WINDOW_SEC = 1.0