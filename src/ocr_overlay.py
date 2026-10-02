# src/ocr_overlay.py
"""Прозрачный оверлей поверх исходного окна ПК.

Показывает только текст перевода (и фон под ним),
не дублирует кадр ПК. Позиция и размер = клиентская область
исходного окна (target_hwnd).

Поддерживает динамическую смену целевого окна (set_target_hwnd).

Прозрачность фона управляется параметром alpha (0..255):
  alpha >= 240  — фон полностью непрозрачный (сплошная заливка)
  alpha <= 15   — фон полностью прозрачный (не рисуется)
  иначе         — фон рисуется через stipple с градацией

Стабилизация блоков:
  Блок фиксируется при первом появлении и держит позицию, пока
  находится на том же месте. Сопоставление нового блока со старым
  идёт по IoU (Intersection over Union) прямоугольников.

  Текст в стабильном блоке обновляется в двух случаях:
    1. Новый текст точно совпадает со старым.
    2. Новый текст отличается, но приходит STABLE_TEXT_UPDATE_THRESHOLD
       раз подряд (гистерезис) — реальная смена на экране.

ИЗМЕНЕНО:
  - STABLE_MISS_LIMIT = 2: блок держится 2 кадра после исчезновения.
  - miss_count УМЕНЬШАЕТСЯ при совпадении, а не сбрасывается в 0.
  - При scene_changed очищается self._stable_blocks.
  - POS_CHANGE_THRESHOLD = 3: позиция обновляется при сдвиге 3+ px.
  - SIZE_CHANGE_THRESHOLD = 5: размер обновляется при 5+ px.
  - STABLE_TEXT_UPDATE_THRESHOLD = 2 (было 8): текст обновляется
    через 2 кадра (~330 мс) вместо 8 (~1.3 сек).

ИЗМЕНЕНО (скрытие отдельных блоков под курсором):
  - Убрано скрытие всего окна через -alpha 0.0 / withdraw.
  - Каждому блоку присваивается canvas-tag "blk_{idx}".
  - _poll_cursor_over_blocks() каждые 100 мс пересчитывает,
    какие блоки под курсором, и скрывает их через state='hidden'.
    Ушедшие из-под курсора — возвращает state='normal'.
  - _block_tags и _hidden_block_tags сбрасываются в _redraw
    перед canvas.delete("all").

ИЗМЕНЕНО (реакция на смену настроек):
  - Подписка на settings_manager.add_listener.
  - При изменении ocr_padding / debug / overlay_alpha выставляется
    _force_redraw = True и сбрасывается fingerprint, чтобы следующий
    _refresh гарантированно перерисовал (даже если STALE_DATA_TIMEOUT).
  - В destroy слушатель снимается.

ИЗМЕНЕНО (фильтр цифровых блоков):
  - Функция _is_digits_only_block УДАЛЕНА. Цифры и двоеточия
    удаляются на Android в normalizeText → stripDigitsAndColons,
    и на ПК уже приходит очищенный текст ("Time" вместо
    "Time 38:17:16"). Пустые после очистки блоки отбрасываются
    на Android по isEmpty.
"""

import hashlib
import math
import time
import traceback
import tkinter as tk
import tkinter.font as tkfont

import win32gui
import win32con

from src.ocr_receiver import OcrReceiver
from src.debug_logger import logger as dbg
from src.settings_manager import settings_manager

TRANSPARENT_COLOR = "#010101"

ALPHA_SOLID_THRESHOLD = 240
ALPHA_TRANSPARENT_THRESHOLD = 15

STABLE_MISS_LIMIT = 2

# ИЗМЕНЕНО: 8 → 2. Обновление текста через 2 кадра (~330 мс),
# а не через 8 (~1.3 сек). Устраняет "зависание" старого текста.
STABLE_TEXT_UPDATE_THRESHOLD = 2

STABLE_IOU_THRESHOLD = 0.3

DEDUP_IOU_THRESHOLD = 0.8
DEDUP_TEXT_MAX_DIST = 30.0

SIZE_CHANGE_THRESHOLD = 5
POS_CHANGE_THRESHOLD = 3
CENTER_MATCH_DIST = 60.0
SIZE_ROUND_TO = 8
FRAME_SIZE_TOLERANCE = 0.05

STALE_DATA_TIMEOUT = 2.0

EMPTY_FRAME_STREAK_TO_CLEAR = 2

# Ключи настроек, при изменении которых нужно принудительно
# перерисовать оверлей (даже если нет нового OCR-пакета).
_REDRAW_TRIGGER_KEYS = ("ocr_padding", "debug", "overlay_alpha")


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


class OcrOverlayWindow:
    """Прозрачное окно поверх исходного окна ПК."""

    MIN_FONT_SIZE = 6
    MAX_FONT_SIZE = 72
    POLL_INTERVAL_MS = 100
    POSITION_INTERVAL_MS = 250

    def __init__(self, root, receiver: OcrReceiver, target_hwnd: int,
                 pc_streamer=None, on_close=None):
        self.root = root
        self.receiver = receiver
        self.target_hwnd = target_hwnd
        self.pc_streamer = pc_streamer
        self.on_close = on_close

        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.attributes('-topmost', True)
        try:
            self.window.attributes('-transparentcolor', TRANSPARENT_COLOR)
        except Exception:
            try:
                self.window.attributes('-alpha', 0.85)
            except Exception:
                pass
        self.window.configure(bg=TRANSPARENT_COLOR)
        self.window.protocol("WM_DELETE_WINDOW", self._on_window_close)

        self.canvas = tk.Canvas(
            self.window,
            bg=TRANSPARENT_COLOR,
            highlightthickness=0,
            bd=0
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self._font_cache = {}
        self._fitted_cache = {}

        self._stable_blocks = []

        self._last_fingerprint = None
        self._last_mode = None
        self._last_alpha = None
        self._last_frame_size = (0, 0)
        self._last_ocr_padding = None

        self._empty_frame_streak = 0

        self._stable_frame_size = (0, 0)

        self._cached_client_rect = None
        self._last_client_rect_ts = 0.0

        self._poll_timer = None
        self._position_timer = None

        # Для скрытия отдельных блоков под курсором.
        self._block_tags = {}
        self._hidden_block_tags = set()

        # Флаг принудительной перерисовки при смене настроек.
        self._force_redraw = False

        # Подписка на изменения настроек.
        try:
            settings_manager.add_listener(self._on_settings_changed)
        except Exception:
            pass

        dbg.log_event("pc.overlay_create", {"target_hwnd": target_hwnd})
        if target_hwnd:
            self._update_position()
        else:
            try:
                self.window.withdraw()
            except Exception:
                pass
        self._poll()
        self._position_loop()

    def _on_settings_changed(self, key, value):
        """Реакция на изменение настроек из settings_manager.

        При изменении визуальных параметров (ocr_padding, debug,
        overlay_alpha) — выставляем _force_redraw и сбрасываем
        fingerprint, чтобы следующий _refresh гарантированно
        перерисовал оверлей.
        """
        if key not in _REDRAW_TRIGGER_KEYS:
            return
        self._force_redraw = True
        self._last_fingerprint = None
        self._last_ocr_padding = None
        self._last_alpha = None
        self._last_mode = None

    def _force_topmost(self):
        try:
            if not self.window or not self.window.winfo_exists():
                return
            hwnd = int(self.window.winfo_id())
            if hwnd == 0:
                return
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_TOPMOST,
                0, 0, 0, 0,
                win32con.SWP_NOMOVE |
                win32con.SWP_NOSIZE |
                win32con.SWP_NOACTIVATE
            )
        except Exception:
            pass

    def set_target_hwnd(self, hwnd: int):
        if hwnd == self.target_hwnd:
            return
        print(f"[Overlay] 🔄 Смена целевого окна: {self.target_hwnd} → {hwnd}")
        dbg.log_event("pc.overlay_target_changed",
                      {"old": self.target_hwnd, "new": hwnd})
        self.target_hwnd = hwnd
        self._cached_client_rect = None
        self._last_client_rect_ts = 0.0
        self._last_fingerprint = None
        self._last_frame_size = (0, 0)
        self._stable_frame_size = (0, 0)
        self._stable_blocks = []
        self._empty_frame_streak = 0

        self._block_tags = {}
        self._hidden_block_tags = set()

        try:
            self.canvas.delete("all")
        except Exception:
            pass
        try:
            if hwnd:
                self._update_position()
            else:
                self.window.withdraw()
        except Exception:
            pass

    def _get_client_rect_on_screen(self, use_cache: bool = True):
        now = time.time()
        if (use_cache
                and self._cached_client_rect is not None
                and now - self._last_client_rect_ts < 0.2):
            return self._cached_client_rect

        if not self.target_hwnd or not win32gui.IsWindow(self.target_hwnd):
            return None
        try:
            if win32gui.IsIconic(self.target_hwnd):
                return None
            client = win32gui.GetClientRect(self.target_hwnd)
            w = client[2] - client[0]
            h = client[3] - client[1]
            if w <= 0 or h <= 0:
                return None
            left, top = win32gui.ClientToScreen(self.target_hwnd, (0, 0))
            rect = (left, top, w, h)
            self._cached_client_rect = rect
            self._last_client_rect_ts = now
            return rect
        except Exception as e:
            dbg.log_event("pc.overlay_get_rect_error", {"error": str(e)})
            return None

    def _update_position(self):
        if not self.target_hwnd:
            try:
                self.window.withdraw()
            except Exception:
                pass
            return
        rect = self._get_client_rect_on_screen(use_cache=False)
        if rect is None:
            try:
                self.window.withdraw()
            except Exception:
                pass
            self._cached_client_rect = None
            return
        x, y, w, h = rect
        try:
            was_withdrawn = False
            try:
                state = self.window.state()
                if state == 'withdrawn':
                    was_withdrawn = True
            except Exception:
                pass

            self.window.deiconify()
            self.window.geometry(f"{w}x{h}+{x}+{y}")
            self.window.lift()
            try:
                self.window.attributes('-topmost', True)
            except Exception:
                pass
            self._force_topmost()

            if was_withdrawn:
                self._last_fingerprint = None
        except Exception as e:
            dbg.log_event("pc.overlay_set_pos_error", {"error": str(e)})

    def _position_loop(self):
        try:
            if self.target_hwnd:
                self._update_position()
                self._force_topmost()
        except Exception:
            pass
        try:
            self._position_timer = self.root.after(
                self.POSITION_INTERVAL_MS, self._position_loop
            )
        except Exception:
            pass

    def _poll(self):
        try:
            self._refresh()
        except Exception as e:
            dbg.log_event("pc.overlay_poll_error", {
                "error": str(e),
                "traceback": traceback.format_exc(),
            })
        try:
            self._poll_cursor_over_blocks()
        except Exception as e:
            dbg.log_event("pc.overlay_poll_cursor_error", {"error": str(e)})
        try:
            self._poll_timer = self.root.after(self.POLL_INTERVAL_MS, self._poll)
        except Exception:
            pass

    def _poll_cursor_over_blocks(self):
        """Скрывает блоки, над которыми сейчас курсор, показывает ушедшие.

        Работает через canvas-теги: каждому блоку присвоен tag "blk_{idx}",
        и прямоугольник фона, и текст имеют этот tag. itemconfigure
        применяет state='hidden'/'normal' ко всем элементам с этим тегом.
        """
        if not self._block_tags:
            if self._hidden_block_tags:
                self._hidden_block_tags = set()
            return

        try:
            if not self.window or not self.window.winfo_exists():
                return
            if self.window.state() == 'withdrawn':
                return
        except Exception:
            return

        try:
            px, py = self.window.winfo_pointerxy()
            wx = self.window.winfo_rootx()
            wy = self.window.winfo_rooty()
        except Exception:
            return

        lx = px - wx
        ly = py - wy

        now_hidden = set()
        for tag, (x1, y1, x2, y2) in self._block_tags.items():
            if x1 <= lx <= x2 and y1 <= ly <= y2:
                now_hidden.add(tag)

        if now_hidden == self._hidden_block_tags:
            return

        try:
            for tag in now_hidden - self._hidden_block_tags:
                self.canvas.itemconfigure(tag, state='hidden')
            for tag in self._hidden_block_tags - now_hidden:
                self.canvas.itemconfigure(tag, state='normal')
        except Exception:
            pass

        self._hidden_block_tags = now_hidden

    def _stabilize_frame_size(self, fw: int, fh: int):
        if fw <= 0 or fh <= 0:
            return self._stable_frame_size

        sw, sh = self._stable_frame_size
        if sw <= 0 or sh <= 0:
            self._stable_frame_size = (fw, fh)
            return (fw, fh)

        dw = abs(fw - sw) / float(sw)
        dh = abs(fh - sh) / float(sh)

        if dw <= FRAME_SIZE_TOLERANCE and dh <= FRAME_SIZE_TOLERANCE:
            return (sw, sh)

        self._stable_frame_size = (fw, fh)
        return (fw, fh)

    def _refresh(self):
        # Читаем и сбрасываем флаг принудительной перерисовки.
        force = self._force_redraw
        self._force_redraw = False

        if not self.target_hwnd:
            return
        if not win32gui.IsWindow(self.target_hwnd):
            return
        if win32gui.IsIconic(self.target_hwnd):
            return

        if not force:
            last_ts = getattr(self.receiver, 'last_received_ts', 0.0)
            if last_ts > 0 and (time.time() - last_ts) > STALE_DATA_TIMEOUT:
                return

        blocks, (raw_fw, raw_fh) = self.receiver.get_blocks()
        mode = self.receiver.get_mode()
        alpha = self.receiver.get_alpha()
        try:
            ocr_padding = int(settings_manager.get("ocr_padding", 0) or 0)
        except Exception:
            ocr_padding = 0

        fw, fh = self._stabilize_frame_size(raw_fw, raw_fh)

        if not blocks:
            self._empty_frame_streak += 1
            if self._empty_frame_streak >= EMPTY_FRAME_STREAK_TO_CLEAR:
                if self._stable_blocks:
                    self._stable_blocks = []
                    self._last_fingerprint = None
                    self.canvas.delete("all")
                    self._block_tags = {}
                    self._hidden_block_tags = set()
                    dbg.log_event("pc.overlay_cleared",
                                  {"reason": "empty_frames"})
            else:
                if self._stable_blocks:
                    self._last_fingerprint = None
                    self._redraw(self._stable_blocks, fw, fh, mode, alpha,
                                 ocr_padding)
            return

        self._empty_frame_streak = 0

        prev_stable_count = len(self._stable_blocks)
        self._stabilize_blocks(blocks)

        if prev_stable_count > 0 and len(self._stable_blocks) == 0:
            dbg.log_event("pc.overlay_scene_changed",
                          {"new_blocks": len(blocks)})
            self._last_fingerprint = None
            self.canvas.delete("all")
            self._block_tags = {}
            self._hidden_block_tags = set()
            self._stabilize_blocks(blocks)

        self._stable_blocks = _deduplicate_by_text(self._stable_blocks)
        self._stable_blocks = _deduplicate_blocks(self._stable_blocks)

        fp = _blocks_fingerprint(self._stable_blocks)

        if (not force
                and fp == self._last_fingerprint
                and mode == self._last_mode
                and alpha == self._last_alpha
                and (fw, fh) == self._last_frame_size
                and ocr_padding == self._last_ocr_padding):
            return

        self._last_fingerprint = fp
        self._last_mode = mode
        self._last_alpha = alpha
        self._last_frame_size = (fw, fh)
        self._last_ocr_padding = ocr_padding

        self._redraw(self._stable_blocks, fw, fh, mode, alpha, ocr_padding)

    def _stabilize_blocks(self, new_blocks):
        if not new_blocks:
            return

        used_new = [False] * len(new_blocks)
        new_stable = []
        matched_any = False

        for sb in self._stable_blocks:
            sb_text = sb.get('text', '')
            sb_text_norm = _normalize_text_for_dedup(sb_text)

            best_idx = -1
            best_iou = STABLE_IOU_THRESHOLD

            for i, nb in enumerate(new_blocks):
                if used_new[i]:
                    continue
                iou = _iou(sb, nb)
                if iou >= best_iou:
                    best_iou = iou
                    best_idx = i

            if best_idx < 0 and sb_text_norm:
                best_center_dist = CENTER_MATCH_DIST
                for i, nb in enumerate(new_blocks):
                    if used_new[i]:
                        continue
                    nb_text_norm = _normalize_text_for_dedup(nb.get('text', ''))
                    if not nb_text_norm:
                        continue
                    if not _is_text_duplicate(sb_text_norm, nb_text_norm):
                        continue
                    dist = _center_distance(sb, nb)
                    if dist < best_center_dist:
                        best_center_dist = dist
                        best_idx = i

            if best_idx < 0 and sb_text_norm:
                for i, nb in enumerate(new_blocks):
                    if used_new[i]:
                        continue
                    nb_text_norm = _normalize_text_for_dedup(nb.get('text', ''))
                    if nb_text_norm and _is_text_duplicate(
                            sb_text_norm, nb_text_norm):
                        best_idx = i
                        break

            if best_idx < 0:
                miss = sb.get('miss_count', 0) + 1
                if miss <= STABLE_MISS_LIMIT:
                    sb['miss_count'] = miss
                    new_stable.append(sb)
                continue

            used_new[best_idx] = True
            matched_any = True
            nb = new_blocks[best_idx]
            nb_text = nb.get('text', '')
            nb_conf = nb.get('confidence', sb.get('confidence', 0.0))

            old_miss = sb.get('miss_count', 0)
            if old_miss > 0:
                sb['miss_count'] = max(0, old_miss - 1)
            else:
                sb['miss_count'] = 0

            if nb_text == sb_text:
                sb['confidence'] = nb_conf
                sb['pending_text'] = ''
                sb['pending_count'] = 0
                self._smooth_block_geometry(sb, nb)
                new_stable.append(sb)
                continue

            pending_text = sb.get('pending_text', '')
            pending_count = sb.get('pending_count', 0)

            if nb_text == pending_text:
                pending_count += 1
            else:
                pending_text = nb_text
                pending_count = 1

            if pending_count >= STABLE_TEXT_UPDATE_THRESHOLD:
                sb['text'] = nb_text
                sb['confidence'] = nb_conf
                sb['pending_text'] = ''
                sb['pending_count'] = 0
                self._smooth_block_geometry(sb, nb)
                sb['initial_x'] = nb.get('initial_x', nb.get('x', 0))
                sb['initial_y'] = nb.get('initial_y', nb.get('y', 0))
                sb['initial_w'] = nb.get('initial_w', nb.get('w', 0))
                sb['initial_h'] = nb.get('initial_h', nb.get('h', 0))
            else:
                sb['confidence'] = nb_conf
                sb['pending_text'] = pending_text
                sb['pending_count'] = pending_count
                self._smooth_block_geometry(sb, nb)

            new_stable.append(sb)

        if (not matched_any
                and self._stable_blocks
                and len(new_blocks) >= 1):
            dbg.log_event("pc.overlay_scene_changed_strict",
                          {"new_blocks": len(new_blocks)})
            new_stable = []

        for i, nb in enumerate(new_blocks):
            if used_new[i]:
                continue
            sb = {
                'text': nb.get('text', ''),
                'x': nb.get('x', 0),
                'y': nb.get('y', 0),
                'w': nb.get('w', 0),
                'h': nb.get('h', 0),
                'confidence': nb.get('confidence', 0.0),
                'initial_x': nb.get('initial_x', nb.get('x', 0)),
                'initial_y': nb.get('initial_y', nb.get('y', 0)),
                'initial_w': nb.get('initial_w', nb.get('w', 0)),
                'initial_h': nb.get('initial_h', nb.get('h', 0)),
                'miss_count': 0,
                'pending_text': '',
                'pending_count': 0,
            }
            new_stable.append(sb)

        self._stable_blocks = new_stable

    @staticmethod
    def _smooth_block_geometry(sb: dict, nb: dict):
        old_x = sb.get('x', 0)
        old_y = sb.get('y', 0)
        old_w = sb.get('w', 0)
        old_h = sb.get('h', 0)

        new_x = nb.get('x', old_x)
        new_y = nb.get('y', old_y)
        new_w = nb.get('w', old_w)
        new_h = nb.get('h', old_h)

        if abs(new_x - old_x) >= POS_CHANGE_THRESHOLD:
            sb['x'] = new_x
        if abs(new_y - old_y) >= POS_CHANGE_THRESHOLD:
            sb['y'] = new_y

        if abs(new_w - old_w) >= SIZE_CHANGE_THRESHOLD:
            sb['w'] = new_w
        if abs(new_h - old_h) >= SIZE_CHANGE_THRESHOLD:
            sb['h'] = new_h

    @staticmethod
    def _has_letters(s: str) -> bool:
        for c in s:
            if c.isalpha():
                return True
        return False

    def _redraw(self, blocks, frame_w, frame_h, mode, alpha, ocr_padding=0):
        if not self.target_hwnd:
            return
        rect = self._get_client_rect_on_screen(use_cache=True)
        if rect is None:
            return
        _, _, client_w, client_h = rect

        if frame_w <= 0 or frame_h <= 0:
            return

        scale_x = client_w / frame_w
        scale_y = client_h / frame_h

        self._block_tags = {}
        self._hidden_block_tags = set()

        self.canvas.delete("all")

        if mode == "debug":
            self._draw_debug(blocks, scale_x, scale_y, show_confidence=True,
                             ocr_padding=ocr_padding)
        elif mode == "ocr":
            pass
        else:
            self._draw_translation(blocks, scale_x, scale_y, alpha)

    def _stipple_from_alpha(self, alpha: int):
        if alpha <= ALPHA_TRANSPARENT_THRESHOLD:
            return None
        if alpha >= ALPHA_SOLID_THRESHOLD:
            return "solid"
        if alpha < 64:
            return "gray12"
        elif alpha < 128:
            return "gray25"
        elif alpha < 192:
            return "gray50"
        else:
            return "gray75"

    def _draw_translation(self, blocks, scale_x, scale_y, alpha):
        fill_mode = self._stipple_from_alpha(alpha)
        create_rect = self.canvas.create_rectangle
        has_letters = self._has_letters

        for idx, b in enumerate(blocks):
            text = b.get('text', '')
            if not text or not has_letters(text):
                continue

            bw = b.get('w', 0)
            bh = b.get('h', 0)
            if bw <= 0 or bh <= 0:
                continue

            x1 = b.get('x', 0) * scale_x
            y1 = b.get('y', 0) * scale_y
            x2 = (b.get('x', 0) + bw) * scale_x
            y2 = (b.get('y', 0) + bh) * scale_y

            tag = f"blk_{idx}"

            if fill_mode == "solid":
                create_rect(x1, y1, x2, y2, fill="#000000",
                            outline="", tags=(tag, "bg"))
            elif fill_mode is not None:
                create_rect(x1, y1, x2, y2, fill="#000000",
                            outline="", stipple=fill_mode, tags=(tag, "bg"))

            self._draw_fitted_text_tagged(
                text, x1 + 2, y1 + 2,
                max(1, (x2 - x1) - 4), max(1, (y2 - y1) - 4), tag
            )

            self._block_tags[tag] = (x1, y1, x2, y2)

    def _draw_debug(self, blocks, scale_x, scale_y, show_confidence=True,
                    ocr_padding=0):
        create_rect = self.canvas.create_rectangle
        create_text = self.canvas.create_text

        label_font = ("Consolas", 10, "bold")
        try:
            label_line_height = tkfont.Font(family="Consolas", size=10,
                                            weight="bold").metrics("linespace")
        except Exception:
            label_line_height = 14
        label_pad_x = 6
        label_pad_y = 4
        MAX_LABEL_LINES = 4

        try:
            canvas_w = self.canvas.winfo_width()
            if canvas_w <= 1:
                canvas_w = self.canvas.winfo_reqwidth()
        except Exception:
            canvas_w = 800

        try:
            pad = max(0, min(50, int(ocr_padding))) / 100.0
        except Exception:
            pad = 0.0

        for b in blocks:
            bw = b.get('w', 0)
            bh = b.get('h', 0)
            if bw <= 0 or bh <= 0:
                continue

            x1 = b.get('x', 0) * scale_x
            y1 = b.get('y', 0) * scale_y
            x2 = (b.get('x', 0) + bw) * scale_x
            y2 = (b.get('y', 0) + bh) * scale_y

            pad_x = bw * pad
            pad_y = bh * pad
            gx1 = (b.get('x', 0) - pad_x) * scale_x
            gy1 = (b.get('y', 0) - pad_y) * scale_y
            gx2 = (b.get('x', 0) + bw + pad_x) * scale_x
            gy2 = (b.get('y', 0) + bh + pad_y) * scale_y

            create_rect(gx1, gy1, gx2, gy2, outline="#00ff00", width=2)
            create_rect(x1, y1, x2, y2, outline="#ff0000", width=2)

            text = b.get('text', '')
            if text:
                max_text_w = max(120, int(canvas_w - x1 - 10))
                max_text_w = min(max_text_w, 600)
                max_text_w = max(max_text_w, 80)

                approx_chars_per_line = max(1, int(max_text_w / 7))
                approx_lines = min(
                    MAX_LABEL_LINES,
                    max(1, (len(text) + approx_chars_per_line - 1)
                        // approx_chars_per_line)
                )
                label_h = approx_lines * label_line_height + label_pad_y * 2
                label_w = max_text_w + label_pad_x * 2

                if y1 - label_h >= 0:
                    label_x = x1
                    label_y = y1 - label_h
                else:
                    label_x = x1
                    label_y = y1 + 2

                if label_x + label_w > canvas_w:
                    label_x = max(0, canvas_w - label_w - 2)

                create_rect(
                    label_x, label_y,
                    label_x + label_w, label_y + label_h,
                    fill="#000000", outline="#444444", width=1
                )
                create_text(
                    label_x + label_pad_x,
                    label_y + label_pad_y,
                    text=text,
                    fill="#ffff00",
                    anchor="nw",
                    font=label_font,
                    width=max_text_w
                )

            if show_confidence:
                conf = b.get('confidence', 0.0)
                try:
                    conf_val = float(conf)
                    if conf_val > 0:
                        conf_text = f"{conf_val * 100:.0f}%"
                        conf_x = x2 + 6
                        conf_y = y1 + 2
                        if conf_x + 40 > canvas_w:
                            conf_x = max(0, x1 - 44)
                        if conf_y < 0:
                            conf_y = y2 + 2
                        create_text(conf_x, conf_y,
                                    text=conf_text,
                                    fill="#00ff00", anchor="nw",
                                    font=("Consolas", 10, "bold"))
                except (TypeError, ValueError):
                    pass

    def _get_font(self, size: int) -> tkfont.Font:
        f = self._font_cache.get(size)
        if f is None:
            f = tkfont.Font(family="Arial", size=size, weight="bold")
            self._font_cache[size] = f
        return f

    @staticmethod
    def _round_size(v: int) -> int:
        if v <= 0:
            return 0
        return max(SIZE_ROUND_TO, (v // SIZE_ROUND_TO) * SIZE_ROUND_TO)

    def _draw_fitted_text_tagged(self, text, x, y, max_w, max_h, tag):
        """Как _draw_fitted_text, но создаёт текст с указанным tag'ом."""
        max_w = max(1, int(max_w))
        max_h = max(1, int(max_h))

        rounded_w = self._round_size(max_w)
        rounded_h = self._round_size(max_h)

        key = (text, rounded_w, rounded_h)
        cached = self._fitted_cache.get(key)
        if cached is not None:
            size = cached
        else:
            size = min(self.MAX_FONT_SIZE, max_h)
            size = max(size, self.MIN_FONT_SIZE)

            while size > self.MIN_FONT_SIZE:
                font = self._get_font(size)
                item = self.canvas.create_text(
                    x, y, text=text, fill="#ffffff", font=font,
                    anchor="nw", width=max_w
                )
                bbox = self.canvas.bbox(item)
                self.canvas.delete(item)
                if bbox is None:
                    size -= 1
                    continue
                text_h = bbox[3] - bbox[1]
                if text_h <= max_h:
                    break
                size -= 1

            if len(self._fitted_cache) > 2000:
                self._fitted_cache.clear()
            self._fitted_cache[key] = size

        font = self._get_font(size)
        self.canvas.create_text(
            x, y, text=text, fill="#ffffff", font=font,
            anchor="nw", width=max_w, tags=(tag,)
        )

    def _draw_fitted_text(self, text, x, y, max_w, max_h):
        max_w = max(1, int(max_w))
        max_h = max(1, int(max_h))

        rounded_w = self._round_size(max_w)
        rounded_h = self._round_size(max_h)

        key = (text, rounded_w, rounded_h)
        cached = self._fitted_cache.get(key)
        if cached is not None:
            size = cached
        else:
            size = min(self.MAX_FONT_SIZE, max_h)
            size = max(size, self.MIN_FONT_SIZE)

            while size > self.MIN_FONT_SIZE:
                font = self._get_font(size)
                item = self.canvas.create_text(
                    x, y, text=text, fill="#ffffff", font=font,
                    anchor="nw", width=max_w
                )
                bbox = self.canvas.bbox(item)
                self.canvas.delete(item)
                if bbox is None:
                    size -= 1
                    continue
                text_h = bbox[3] - bbox[1]
                if text_h <= max_h:
                    break
                size -= 1

            if len(self._fitted_cache) > 2000:
                self._fitted_cache.clear()
            self._fitted_cache[key] = size

        font = self._get_font(size)
        self.canvas.create_text(
            x, y, text=text, fill="#ffffff", font=font,
            anchor="nw", width=max_w
        )

    def _on_window_close(self):
        if self.on_close is not None:
            try:
                self.on_close()
            except Exception:
                pass
        else:
            self.hide()

    def hide(self):
        try:
            self.window.withdraw()
        except Exception:
            pass

    def show(self):
        try:
            self.window.deiconify()
            self.window.lift()
            self._force_topmost()
            self._last_fingerprint = None
        except Exception:
            pass

    def destroy(self):
        try:
            settings_manager.remove_listener(self._on_settings_changed)
        except Exception:
            pass

        try:
            if self._poll_timer is not None:
                self.root.after_cancel(self._poll_timer)
                self._poll_timer = None
        except Exception:
            pass
        try:
            if self._position_timer is not None:
                self.root.after_cancel(self._position_timer)
                self._position_timer = None
        except Exception:
            pass
        self._block_tags = {}
        self._hidden_block_tags = set()
        try:
            self.window.destroy()
        except Exception:
            pass
        dbg.log_event("pc.overlay_destroy")