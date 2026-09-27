# src/text_logger.py
"""Отдельный лог распознанного и переведённого текста.

Пишет в OCR_TEXT_LOG_FILE (ocr_text.log) рядом с main.py.
Записи блокируются по хэшу: если текст/перевод не изменился —
в файл ничего не пишется. Это даёт компактность без спама.

Формат:
  [HH:MM:SS] OCR (N blocks, mode=translate)
    [1] <полный текст блока 1>
    [2] <полный текст блока 2>
    ...
  [HH:MM:SS] TR
    <вход>  →  <выход>
    ...

Потокобезопасен (Lock). Не ротируется — если файл станет большим,
удалите вручную.
"""

import hashlib
import threading
import time
from typing import List, Optional

from src.config import OCR_TEXT_LOG_FILE, OCR_TEXT_LOG_ENABLED

_lock = threading.Lock()

# Хэш последнего записанного OCR-состояния
_last_ocr_hash: Optional[str] = None
# Хэш последнего записанного перевода
_last_tr_hash: Optional[str] = None


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
    """Пишет все блоки OCR без обрезки.

    Блокирует по хэшу: если набор блоков/текстов/координат не изменился —
    ничего не пишет.
    """
    global _last_ocr_hash

    if not OCR_TEXT_LOG_ENABLED:
        return
    if not blocks:
        return

    try:
        # Отфильтровываем пустые тексты
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
            # Полный текст блока, без обрезки. Многострочный текст —
            # оставляем как есть, чтобы было видно структуру.
            lines.append(f"  [{i}] {t}\n")
        lines.append("\n")
        _append("".join(lines))
    except Exception:
        pass


def log_translate_text(texts: List[str], translations: List[str]) -> None:
    """Пишет все пары вход→выход без обрезки.

    Блокирует по хэшу: если перевод не изменился — ничего не пишет.
    """
    global _last_tr_hash

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
            if src and dst:
                pairs.append((src, dst))
        if not pairs:
            return

        fp = _hash_tr(texts, translations)
        with _lock:
            if fp == _last_tr_hash:
                return
            _last_tr_hash = fp

        ts = time.strftime("%H:%M:%S")
        lines = [f"[{ts}] TR\n"]
        for src, dst in pairs:
            lines.append(f"  {src}  →  {dst}\n")
        lines.append("\n")
        _append("".join(lines))
    except Exception:
        pass


def reset_ocr_cache() -> None:
    """Сбрасывает кэш хэшей OCR (например, при смене режима/настроек)."""
    global _last_ocr_hash
    with _lock:
        _last_ocr_hash = None


def reset_translate_cache() -> None:
    """Сбрасывает кэш хэшей перевода."""
    global _last_tr_hash
    with _lock:
        _last_tr_hash = None