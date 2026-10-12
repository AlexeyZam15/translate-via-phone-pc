# src/overlay_utils.py
"""Текстовые и геометрические утилиты оверлея.
Все функции — чистые, не зависят от класса OcrOverlayWindow.
"""

import difflib
import hashlib
import math

from src.overlay_constants import (
    RARE_CHARS, MAX_RARE_CHARS, MAX_CONSECUTIVE_CONSONANTS, VOWELS,
    MIXED_ALPHABET_LATIN_RATIO, HUD_MAX_LEN, FRAGMENT_FUZZY_THRESHOLD,
    FRAGMENT_MULTIWORD_MIN_LEN, FRAGMENT_MULTIWORD_MIN_WORDS,
    TEXT_SIMILARITY_THRESHOLD, DEDUP_IOU_THRESHOLD, STABLE_KEY_GRID,
)


# ============================================================
# ТЕКСТ
# ============================================================

def _has_cyrillic(text: str) -> bool:
    for c in text:
        if '\u0400' <= c <= '\u04FF':
            return True
    return False


def _has_latin(text: str) -> bool:
    for c in text:
        if ('a' <= c <= 'z') or ('A' <= c <= 'Z'):
            return True
    return False


def _count_cyrillic(text: str) -> int:
    n = 0
    for c in text:
        if '\u0400' <= c <= '\u04FF':
            n += 1
    return n


def _count_latin(text: str) -> int:
    n = 0
    for c in text:
        if ('a' <= c <= 'z') or ('A' <= c <= 'Z'):
            n += 1
    return n


def _is_hud_or_short(text: str) -> bool:
    t = text.strip()
    if not t or len(t) > HUD_MAX_LEN:
        return False
    has_digit = False
    has_letter = False
    for c in t:
        if c.isdigit():
            has_digit = True
        elif c.isalpha():
            has_letter = True
    if has_digit and has_letter:
        return True
    if has_digit and not has_letter:
        return True
    return False


def _split_words(text: str):
    if not text:
        return []
    words = []
    current = []
    for c in text:
        if c.isalnum():
            current.append(c)
        else:
            if current:
                words.append(''.join(current))
                current = []
    if current:
        words.append(''.join(current))
    return words


def _is_fragment_of_known(text: str, known_texts) -> bool:
    t = text.strip().lower()
    if not t or len(t) < 2:
        return False
    words_in_t = _split_words(t)
    if (len(words_in_t) >= FRAGMENT_MULTIWORD_MIN_WORDS
            and len(t) >= FRAGMENT_MULTIWORD_MIN_LEN):
        return False
    for known in known_texts:
        if not known:
            continue
        k = known.strip().lower()
        if not k or k == t:
            continue
        if len(k) <= len(t):
            continue
        known_words = _split_words(k)
        for w in known_words:
            if not w or len(w) < 2:
                continue
            if w == t:
                return True
            if abs(len(w) - len(t)) > 1:
                continue
            try:
                ratio = difflib.SequenceMatcher(None, t, w).ratio()
                if ratio >= FRAGMENT_FUZZY_THRESHOLD:
                    return True
            except Exception:
                continue
    return False


def _is_suspicious_text(text: str) -> bool:
    if not text:
        return True
    rare_count = 0
    for c in text:
        if c in RARE_CHARS:
            rare_count += 1
            if rare_count > MAX_RARE_CHARS:
                return True
    for word in text.split():
        consecutive = 0
        for c in word:
            if c.isalpha() and c not in VOWELS:
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_CONSONANTS:
                    return True
            else:
                consecutive = 0
    has_cyr = _has_cyrillic(text)
    has_lat = _has_latin(text)
    if has_cyr and has_lat:
        cyr_n = _count_cyrillic(text)
        lat_n = _count_latin(text)
        total = cyr_n + lat_n
        if total > 0 and (lat_n / float(total)) > MIXED_ALPHABET_LATIN_RATIO:
            return True
    return False


def _normalize_text_for_dedup(text: str) -> str:
    if not text:
        return ''
    return text.strip().lower()


def _text_prefix_match(text1: str, text2: str, min_len: int = 4) -> bool:
    t1 = _normalize_text_for_dedup(text1)
    t2 = _normalize_text_for_dedup(text2)
    if not t1 or not t2:
        return False
    if t1 == t2:
        return True
    shorter, longer = (t1, t2) if len(t1) <= len(t2) else (t2, t1)
    if len(shorter) < min_len:
        return False
    if len(longer) - len(shorter) > 20:
        return False
    return longer.startswith(shorter)


def _is_text_duplicate(text1: str, text2: str) -> bool:
    return _text_prefix_match(text1, text2, min_len=4)


def _texts_close_enough(a: str, b: str) -> bool:
    if not a or not b:
        return False
    ta = a.strip().lower()
    tb = b.strip().lower()
    if ta == tb:
        return True
    has_cyr_a = _has_cyrillic(ta)
    has_lat_a = _has_latin(ta)
    has_cyr_b = _has_cyrillic(tb)
    has_lat_b = _has_latin(tb)
    if has_cyr_a and not has_lat_a and has_lat_b and not has_cyr_b:
        return False
    if has_lat_a and not has_cyr_a and has_cyr_b and not has_lat_b:
        return False
    if _is_text_duplicate(ta, tb):
        return True
    try:
        ratio = difflib.SequenceMatcher(None, ta, tb).ratio()
        if ratio >= TEXT_SIMILARITY_THRESHOLD:
            return True
    except Exception:
        pass
    shorter, longer = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(shorter) < 4:
        return False
    if len(longer) - len(shorter) > 6:
        return False
    common = 0
    for c in shorter:
        if c.isalnum() and c in longer:
            common += 1
    total = sum(1 for c in shorter if c.isalnum())
    if total == 0:
        return False
    return common / float(total) >= 0.75


# ============================================================
# ГЕОМЕТРИЯ
# ============================================================

def _blocks_fingerprint(blocks) -> str:
    h = hashlib.md5()
    for b in blocks:
        h.update(b.get('text', '').encode('utf-8', errors='ignore'))
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


def _stable_block_key(b: dict) -> str:
    x = b.get('x', 0)
    y = b.get('y', 0)
    x_grid = (int(x) // STABLE_KEY_GRID) * STABLE_KEY_GRID
    y_grid = (int(y) // STABLE_KEY_GRID) * STABLE_KEY_GRID
    h_md5 = hashlib.md5()
    h_md5.update(f"{x_grid},{y_grid}".encode())
    return h_md5.hexdigest()[:12]


def _iou(a: dict, b: dict) -> float:
    ax1 = a.get('x', 0)
    ay1 = a.get('y', 0)
    ax2 = ax1 + a.get('w', 0)
    ay2 = ay1 + a.get('h', 0)
    bx1 = b.get('x', 0)
    by1 = b.get('y', 0)
    bx2 = bx1 + b.get('w', 0)
    by2 = by1 + b.get('h', 0)
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = ix2 - ix1
    ih = iy2 - iy1
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / union


def _center_distance(a: dict, b: dict) -> float:
    ax = a.get('x', 0) + a.get('w', 0) / 2.0
    ay = a.get('y', 0) + a.get('h', 0) / 2.0
    bx = b.get('x', 0) + b.get('w', 0) / 2.0
    by = b.get('y', 0) + b.get('h', 0) / 2.0
    return math.hypot(ax - bx, ay - by)


def _area(b: dict) -> float:
    return float(b.get('w', 0)) * float(b.get('h', 0))


def _deduplicate_blocks(blocks):
    if len(blocks) <= 1:
        return blocks
    sorted_blocks = sorted(blocks, key=lambda b: _area(b), reverse=True)
    result = []
    for b in sorted_blocks:
        is_dup = False
        for existing in result:
            if _iou(b, existing) >= DEDUP_IOU_THRESHOLD:
                is_dup = True
                break
        if not is_dup:
            result.append(b)
    return result


def _deduplicate_by_text(blocks):
    if len(blocks) <= 1:
        return blocks
    sorted_blocks = sorted(blocks, key=_area, reverse=True)
    result = []
    for b in sorted_blocks:
        b_text = b.get('text', '')
        if not b_text:
            result.append(b)
            continue
        is_dup = False
        for existing in result:
            e_text = existing.get('text', '')
            if not e_text:
                continue
            if _is_text_duplicate(b_text, e_text):
                is_dup = True
                break
        if not is_dup:
            result.append(b)
    return result