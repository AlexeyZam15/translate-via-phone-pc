# src/debug_logger.py
"""Централизованный debug-логгер с таймерами и агрегацией.

Пишет JSONL в DEBUG_LOG_FILE. Потокобезопасен.
- log_event(stage, data) — мгновенная запись
- log_timing(stage, duration_ms, extra) — запись тайминга
- timer(stage, extra) — контекстный менеджер
- Агрегация таймингов по окну (avg/min/max/count/p95) при DEBUG_VERBOSE=False
"""

import json
import os
import threading
import time
from collections import defaultdict
from typing import Any, Dict, Optional

from src.config import (
    DEBUG_LOG_ENABLED,
    DEBUG_LOG_FILE,
    DEBUG_LOG_MAX_BYTES,
    DEBUG_VERBOSE,
    DEBUG_AGGREGATE_WINDOW_SEC,
)


class _Aggregator:
    """Агрегирует тайминги по stage в окне времени."""

    def __init__(self):
        self._lock = threading.Lock()
        self._buckets: Dict[str, list] = defaultdict(list)
        self._last_flush = time.time()

    def add(self, stage: str, duration_ms: float, extra: Optional[Dict[str, Any]] = None):
        with self._lock:
            self._buckets[stage].append((duration_ms, extra or {}))

    def maybe_flush(self, logger: "DebugLogger"):
        now = time.time()
        if now - self._last_flush < DEBUG_AGGREGATE_WINDOW_SEC:
            return
        with self._lock:
            if not self._buckets:
                self._last_flush = now
                return
            snapshot = self._buckets
            self._buckets = defaultdict(list)
            self._last_flush = now

        for stage, items in snapshot.items():
            durations = [d for d, _ in items]
            durations_sorted = sorted(durations)
            n = len(durations_sorted)
            p95_idx = min(n - 1, int(n * 0.95))
            logger._write_raw({
                "ts": now,
                "source": "pc",
                "stage": stage,
                "type": "aggregate",
                "count": n,
                "avg_ms": round(sum(durations) / n, 3),
                "min_ms": round(durations_sorted[0], 3),
                "max_ms": round(durations_sorted[-1], 3),
                "p95_ms": round(durations_sorted[p95_idx], 3),
                "window_sec": DEBUG_AGGREGATE_WINDOW_SEC,
            })


class DebugLogger:
    """Singleton debug-логгер."""

    _instance: Optional["DebugLogger"] = None
    _instance_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._lock = threading.Lock()
        self._aggregator = _Aggregator()
        self._file_path = DEBUG_LOG_FILE
        self._ensure_dir()
        try:
            with open(self._file_path, "w", encoding="utf-8"):
                pass
        except Exception:
            pass

    def _ensure_dir(self):
        d = os.path.dirname(self._file_path)
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)

    def _rotate_if_needed(self):
        try:
            if os.path.exists(self._file_path) and os.path.getsize(self._file_path) > DEBUG_LOG_MAX_BYTES:
                bak = self._file_path + ".1"
                if os.path.exists(bak):
                    os.remove(bak)
                os.rename(self._file_path, bak)
        except Exception:
            pass

    def _write_raw(self, record: Dict[str, Any]):
        if not DEBUG_LOG_ENABLED:
            return
        try:
            line = json.dumps(record, ensure_ascii=False)
            with self._lock:
                self._rotate_if_needed()
                with open(self._file_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass

    def log_event(self, stage: str, data: Optional[Dict[str, Any]] = None, source: str = "pc"):
        self._write_raw({
            "ts": time.time(),
            "source": source,
            "stage": stage,
            "type": "event",
            "data": data or {},
        })

    def log_timing(self, stage: str, duration_ms: float, extra: Optional[Dict[str, Any]] = None, source: str = "pc"):
        if DEBUG_VERBOSE:
            self._write_raw({
                "ts": time.time(),
                "source": source,
                "stage": stage,
                "type": "timing",
                "duration_ms": round(duration_ms, 3),
                "extra": extra or {},
            })
        else:
            self._aggregator.add(stage, duration_ms, extra)
            self._aggregator.maybe_flush(self)

    def timer(self, stage: str, extra: Optional[Dict[str, Any]] = None, source: str = "pc") -> "Timer":
        return Timer(self, stage, extra, source)

    def flush(self):
        self._aggregator.maybe_flush(self)


class Timer:
    """Контекстный менеджер для замера времени."""

    def __init__(self, logger: DebugLogger, stage: str, extra: Optional[Dict[str, Any]] = None, source: str = "pc"):
        self._logger = logger
        self._stage = stage
        self._extra = extra or {}
        self._source = source
        self._start = 0.0

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = (time.perf_counter() - self._start) * 1000.0
        self._logger.log_timing(self._stage, duration_ms, self._extra, self._source)
        return False


logger = DebugLogger()