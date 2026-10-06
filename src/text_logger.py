# src/text_logger.py
"""Отдельный лог распознанного и переведённого текста.

Пишет в OCR_TEXT_LOG_FILE (ocr_text.log) рядом с main.py.

ИЗМЕНЕНО:
  - Добавлена log_translate_input(texts) — логирует ВХОД
    переводчика (то, что Android прислал на /translate).
    Это позволяет увидеть, что именно уходит в CTranslate2,
    и отличить проблему OCR от проблемы перевода.
  - Файл ПЕРЕСОЗДАЁТСЯ при старте приложения (init_text_log).
  - Записи блокируются по хэшу.
  - log_translate_text НЕ пишет пары, где перевод совпадает
    с оригиналом.
"""

import hashlib
import threading
import time
from typing import List, Optional

from src.config import OCR_TEXT_LOG_FILE, OCR_TEXT_LOG_ENABLED

_lock = threading.Lock()

_last_ocr_hash: Optional[str] = None
_last_tr_input_hash: Optional[str] = None
_last_tr_output_hash: Optional[str] = None


def init_text_log() -> None:
    """Пересоздаёт ocr_text.log при старте приложения."""
    global _last_ocr_hash, _last_tr_input_hash, _last_tr_output_hash
    if not OCR_TEXT_LOG_ENABLED:
        return
    with _lock:
        try:
            with open(OCR_TEXT_LOG_FILE, "w", encoding="utf-8"):
                pass
        except Exception:
            pass
        _last_ocr_hash = None
        _last_tr_input_hash = None
        _last_tr_output_hash = None


def _hash_ocr(blocks: List[dict], mode: str) -> str:
    h = hashlib.md5()
    h.update(mode.encode("utf-8", errors="ignore"))
    h.update(b"|")
    for b in blocks:
        t = (b.get("text") or "").strip()
        h.update(t.encode("utf-8", errors="ignore"))
        h.update(b"|")
        h.update(str(b.get("x", 0)).encode())
        h.update(b",")
        h.update(str(b.get("y", 0)).encode())
        h.update(b",")
        h.update(str(b.get("w", 0)).encode())
        h.update(b",")
        h.update(str(b.get("h", 0)).encode())
        h.update(b";")
    return h.hexdigest()


def _hash_texts(texts: List[str]) -> str:
    h = hashlib.md5()
    for t in texts:
        h.update((t or "").encode("utf-8", errors="ignore"))
        h.update(b"|")
    return h.hexdigest()


def _hash_tr(texts: List[str], translations: List[str]) -> str:
    h = hashlib.md5()
    for i in range(min(len(texts), len(translations))):
        h.update((texts[i] or "").encode("utf-8", errors="ignore"))
        h.update(b"=>")
        h.update((translations[i] or "").encode("utf-8", errors="ignore"))
        h.update(b"|")
    return h.hexdigest()


def _append(text: str) -> None:
    try:
        with open(OCR_TEXT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(text)
            f.flush()
    except Exception:
        pass


def log_ocr_text(blocks: List[dict], mode: str = "translate") -> None:
    """Пишет все блоки OCR без обрезки. Блокирует по хэшу."""
    global _last_ocr_hash

    if not OCR_TEXT_LOG_ENABLED:
        return
    if not blocks:
        return

    try:
        items = []
        for b in blocks:
            t = (b.get("text") or "").strip()
            if t:
                items.append(t)
        if not items:
            return

        fp = _hash_ocr(blocks, mode)
        with _lock:
            if fp == _last_ocr_hash:
                return
            _last_ocr_hash = fp

        ts = time.strftime("%H:%M:%S")
        lines = [f"[{ts}] OCR ({len(items)} blocks, mode={mode})\n"]
        for i, t in enumerate(items, 1):
            lines.append(f"  [{i}] {t}\n")
        lines.append("\n")
        _append("".join(lines))
    except Exception:
        pass


def log_translate_input(texts: List[str]) -> None:
    """
    ИЗМЕНЕНО: логирует ВХОД переводчика — то, что Android
    прислал на /translate. Блокирует по хэшу.
    """
    global _last_tr_input_hash

    if not OCR_TEXT_LOG_ENABLED:
        return
    if not texts:
        return

    try:
        items = []
        for t in texts:
            s = (t or "").strip()
            if s:
                items.append(s)
        if not items:
            return

        fp = _hash_texts(items)
        with _lock:
            if fp == _last_tr_input_hash:
                return
            _last_tr_input_hash = fp

        ts = time.strftime("%H:%M:%S")
        lines = [f"[{ts}] TR-IN ({len(items)} texts)\n"]
        for i, t in enumerate(items, 1):
            lines.append(f"  [{i}] {t}\n")
        lines.append("\n")
        _append("".join(lines))
    except Exception:
        pass


def log_translate_text(texts: List[str], translations: List[str]) -> None:
    """Пишет все пары вход→выход без обрезки. Блокирует по хэшу."""
    global _last_tr_output_hash

    if not OCR_TEXT_LOG_ENABLED:
        return
    if not texts or not translations:
        return

    try:
        pairs = []
        n = min(len(texts), len(translations))
        for i in range(n):
            src = (texts[i] or "").strip()
            dst = (translations[i] or "").strip()
            if not src or not dst:
                continue
            if src.lower() == dst.lower():
                continue
            pairs.append((src, dst))
        if not pairs:
            return

        fp = _hash_tr(texts, translations)
        with _lock:
            if fp == _last_tr_output_hash:
                return
            _last_tr_output_hash = fp

        ts = time.strftime("%H:%M:%S")
        lines = [f"[{ts}] TR-OUT\n"]
        for src, dst in pairs:
            lines.append(f"  {src}  →  {dst}\n")
        lines.append("\n")
        _append("".join(lines))
    except Exception:
        pass


def reset_ocr_cache() -> None:
    global _last_ocr_hash
    with _lock:
        _last_ocr_hash = None


def reset_translate_cache() -> None:
    global _last_tr_input_hash, _last_tr_output_hash
    with _lock:
        _last_tr_input_hash = None
        _last_tr_output_hash = None