# src/overlay_core.py
"""OverlayCoreMixin — жизненный цикл, позиционирование окна,
polling, стабилизация блоков. Миксин для OcrOverlayWindow.

ИЗМЕНЕНО (v4):
  - УБРАН вызов _deduplicate_by_text в _refresh.
    Раньше он схлопывал ВСЕ блоки с одинаковым текстом, оставляя
    только один. В меню игры есть несколько строк "Время", "Очистить
    данные", "Кледр" в разных местах экрана — они схлопывались в
    один блок, и на оверлее показывался только один экземпляр
    каждого текста. Теперь все блоки показываются как есть, как в
    ocr_test_runner.py (на статичных overlay-файлах).
  - _deduplicate_blocks (по IoU) оставлен — он удаляет только
    настоящие дубликаты с близкими координатами.

Сохранено от v3:
  - При смене mode (translate ↔ debug) сбрасывается _stable_blocks
    и _clear_all_rendered вызывается.
  - При _apply_settings_change сбрасываются _stable_blocks и
    _empty_frame_streak.
  - При смене target_hwnd сбрасывается _stable_blocks.
"""

import threading
import time
import traceback
import tkinter as tk

import win32gui
import win32con

from src.debug_logger import logger as dbg
from src.settings_manager import settings_manager

from src.overlay_constants import (
    TRANSPARENT_COLOR,
    STABLE_MISS_LIMIT, VISIBLE_MISS_LIMIT,
    STABLE_TEXT_UPDATE_THRESHOLD, STABLE_IOU_THRESHOLD,
    POS_CHANGE_THRESHOLD, SIZE_CHANGE_THRESHOLD,
    BBOX_SETTLE_FRAMES, CENTER_MATCH_DIST,
    FRAME_SIZE_TOLERANCE, STALE_DATA_TIMEOUT,
    EMPTY_FRAME_STREAK_TO_CLEAR, _REDRAW_TRIGGER_KEYS,
    MIN_BLOCK_AREA, FRAGMENT_KNOWN_MISS_LIMIT,
    SETTINGS_DEBOUNCE_MS,
)

from src.overlay_utils import (
    _is_suspicious_text, _is_hud_or_short, _is_fragment_of_known,
    _texts_close_enough, _is_text_duplicate, _normalize_text_for_dedup,
    _blocks_fingerprint, _stable_block_key, _iou, _center_distance,
    _deduplicate_blocks,
)


class OverlayCoreMixin:
    """Жизненный цикл, позиционирование, polling, стабилизация блоков."""

    MIN_FONT_SIZE = 6
    MAX_FONT_SIZE = 72
    POLL_INTERVAL_MS = 30
    POSITION_INTERVAL_MS = 250

    def __init__(self, root, receiver, target_hwnd, pc_streamer=None,
                 on_close=None):
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
            self.window, bg=TRANSPARENT_COLOR,
            highlightthickness=0, bd=0
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self._font_cache = {}
        self._fitted_cache = {}
        self._measure_cache = {}
        self._measure_cache_lock = threading.Lock()

        self._stable_blocks = []

        self._last_fingerprint = None
        self._last_mode = None
        self._last_alpha = None
        self._last_frame_size = (0, 0)
        self._last_ocr_padding = None
        self._last_render_signature = None

        self._empty_frame_streak = 0
        self._stable_frame_size = (0, 0)

        self._cached_client_rect = None
        self._last_client_rect_ts = 0.0

        self._poll_timer = None
        self._position_timer = None
        self._settings_debounce_timer = None

        self._block_tags = {}
        self._hidden_block_tags = set()
        self._rendered_blocks = {}

        self._force_redraw = False
        self._last_drawn_mode = None

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
        if key not in _REDRAW_TRIGGER_KEYS:
            return
        try:
            if self._settings_debounce_timer is not None:
                self.root.after_cancel(self._settings_debounce_timer)
        except Exception:
            pass
        try:
            self._settings_debounce_timer = self.root.after(
                SETTINGS_DEBOUNCE_MS, self._apply_settings_change
            )
        except Exception:
            self._apply_settings_change()

    def _apply_settings_change(self):
        self._settings_debounce_timer = None
        self._force_redraw = True
        self._last_fingerprint = None
        self._last_ocr_padding = None
        self._last_alpha = None
        self._last_mode = None
        self._last_render_signature = None
        # ИЗМЕНЕНО: сбрасываем стабилизацию, чтобы при следующем
        # пакете в новом режиме не удержались старые тексты.
        self._stable_blocks = []
        self._empty_frame_streak = 0

    def _force_topmost(self):
        try:
            if not self.window or not self.window.winfo_exists():
                return
            hwnd = int(self.window.winfo_id())
            if hwnd == 0:
                return
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE |
                win32con.SWP_NOACTIVATE
            )
        except Exception:
            pass

    def _clear_all_rendered(self):
        try:
            self.canvas.delete("all")
        except Exception:
            pass
        self._rendered_blocks = {}
        self._block_tags = {}
        self._hidden_block_tags = set()
        self._last_render_signature = None

    def set_target_hwnd(self, hwnd: int):
        if hwnd == self.target_hwnd:
            return
        print(f"[Overlay] Смена целевого окна: {self.target_hwnd} -> {hwnd}")
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
        self._clear_all_rendered()
        try:
            if hwnd:
                self._update_position()
            else:
                self.window.withdraw()
        except Exception:
            pass

    def _get_client_rect_on_screen(self, use_cache: bool = True):
        now = time.time()
        if (use_cache and self._cached_client_rect is not None
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
                if self.window.state() == 'withdrawn':
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
                "error": str(e), "traceback": traceback.format_exc()})
        try:
            self._poll_cursor_over_blocks()
        except Exception as e:
            dbg.log_event("pc.overlay_poll_cursor_error", {"error": str(e)})
        try:
            self._poll_timer = self.root.after(self.POLL_INTERVAL_MS, self._poll)
        except Exception:
            pass

    def _poll_cursor_over_blocks(self):
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
        for key, (x1, y1, x2, y2) in self._block_tags.items():
            if x1 <= lx <= x2 and y1 <= ly <= y2:
                now_hidden.add(key)
        if now_hidden == self._hidden_block_tags:
            return
        try:
            for key in now_hidden - self._hidden_block_tags:
                self.canvas.itemconfigure(f"b_{key}_bg", state='hidden')
                self.canvas.itemconfigure(f"b_{key}_txt", state='hidden')
            for key in self._hidden_block_tags - now_hidden:
                self.canvas.itemconfigure(f"b_{key}_bg", state='normal')
                self.canvas.itemconfigure(f"b_{key}_txt", state='normal')
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

    def _filter_blocks_for_draw(self, blocks):
        all_texts = []
        for b in blocks:
            t = b.get('text', '')
            if t:
                all_texts.append(t)
        for sb in self._stable_blocks:
            if sb.get('miss_count', 0) > FRAGMENT_KNOWN_MISS_LIMIT:
                continue
            sb_text = sb.get('text', '')
            if sb_text:
                all_texts.append(sb_text)
        result = []
        for b in blocks:
            text = b.get('text', '')
            if not text or not self._has_letters(text):
                continue
            if _is_suspicious_text(text):
                continue
            if self._is_block_too_small(b):
                continue
            if _is_hud_or_short(text):
                continue
            if _is_fragment_of_known(text, all_texts):
                continue
            bw = b.get('w', 0)
            bh = b.get('h', 0)
            if bw <= 0 or bh <= 0:
                continue
            result.append(b)
        return result

    def _render_signature(self, blocks, mode, alpha, ocr_padding, fw, fh):
        import hashlib
        h = hashlib.md5()
        h.update(f"{mode}|{alpha}|{ocr_padding}|{fw}x{fh}".encode())
        for b in blocks:
            key = b.get('stable_key', '') or ''
            h.update(key.encode())
            h.update(b'|')
            h.update((b.get('text', '') or '').encode('utf-8', errors='ignore'))
            h.update(b'|')
            h.update(str(int(b.get('x', 0)) // 4).encode())
            h.update(b',')
            h.update(str(int(b.get('y', 0)) // 4).encode())
            h.update(b',')
            h.update(str(int(b.get('w', 0)) // 4).encode())
            h.update(b',')
            h.update(str(int(b.get('h', 0)) // 4).encode())
            h.update(b';')
        return h.hexdigest()

    def _refresh(self):
        """ИЗМЕНЕНО: убран _deduplicate_by_text.

        Раньше вызывался _deduplicate_by_text, который удалял ВСЕ
        блоки с одинаковым текстом, оставляя один. В меню игры есть
        несколько строк "Время", "Очистить данные", "Кледр" в разных
        местах экрана — они схлопывались в один блок, и на оверлее
        показывался только один экземпляр каждого текста.

        Теперь оставлен только _deduplicate_blocks (по IoU) — он
        удаляет лишь настоящие дубликаты (одинаковые координаты),
        но не блоки с одинаковым текстом в разных местах. Поведение
        совпадает с ocr_test_runner.py.
        """
        force = self._force_redraw
        self._force_redraw = False
        if not self.target_hwnd:
            return
        if not win32gui.IsWindow(self.target_hwnd):
            return
        if win32gui.IsIconic(self.target_hwnd):
            return

        # Получаем mode до проверки stale-данных.
        mode = self.receiver.get_mode()

        if not force:
            last_ts = getattr(self.receiver, 'last_received_ts', 0.0)
            if last_ts > 0 and (time.time() - last_ts) > STALE_DATA_TIMEOUT:
                return

        # При смене mode сбрасываем стабилизацию.
        if self._last_mode is not None and mode != self._last_mode:
            dbg.log_event("pc.overlay_mode_changed",
                          {"old": self._last_mode, "new": mode})
            self._stable_blocks = []
            self._empty_frame_streak = 0
            self._last_fingerprint = None
            self._last_render_signature = None
            self._clear_all_rendered()

        blocks, (raw_fw, raw_fh) = self.receiver.get_blocks()
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
                    self._clear_all_rendered()
                    dbg.log_event("pc.overlay_cleared",
                                  {"reason": "empty_frames"})
            else:
                if self._stable_blocks:
                    self._last_fingerprint = None
                    self._redraw(self._stable_blocks, fw, fh, mode, alpha,
                                 ocr_padding)
            return
        self._empty_frame_streak = 0
        self._stabilize_blocks(blocks)
        # ИЗМЕНЕНО: убран _deduplicate_by_text — он схлопывал блоки
        # с одинаковым текстом в разных местах экрана.
        self._stable_blocks = _deduplicate_blocks(self._stable_blocks)
        filtered_for_draw = self._filter_blocks_for_draw(self._stable_blocks)
        visible_for_draw = [
            b for b in filtered_for_draw
            if b.get('miss_count', 0) < VISIBLE_MISS_LIMIT
        ]
        fp = _blocks_fingerprint(visible_for_draw)
        if (not force and fp == self._last_fingerprint
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
        sig = self._render_signature(visible_for_draw, mode, alpha,
                                     ocr_padding, fw, fh)
        if not force and sig == self._last_render_signature:
            return
        self._last_render_signature = sig
        self._redraw(visible_for_draw, fw, fh, mode, alpha, ocr_padding)

    def _stabilize_blocks(self, new_blocks):
        """Стабилизация блоков между кадрами.

        Сохранено: при best_idx < 0 (нет точного матча) добавлена
        проверка partial_match. Если какой-то новый блок —
        подстрока старого текста (или наоборот), старый блок
        удерживается: miss_count сбрасывается, а совпавшие новые
        блоки помечаются used_new = True, чтобы не добавляться
        отдельно.
        """
        if not new_blocks:
            return
        used_new = [False] * len(new_blocks)
        new_stable = []
        matched_any = False

        for sb in self._stable_blocks:
            sb_text = sb.get('text', '')
            sb_text_norm = _normalize_text_for_dedup(sb_text)
            sb_key = sb.get('stable_key', '')

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

            # Удержание старого блока при разбиении.
            if best_idx < 0:
                if sb_text_norm:
                    partial_match_idx = -1
                    for i, nb in enumerate(new_blocks):
                        if used_new[i]:
                            continue
                        nb_text_norm = _normalize_text_for_dedup(
                            nb.get('text', ''))
                        if not nb_text_norm:
                            continue
                        if len(nb_text_norm) < 3:
                            continue
                        if (nb_text_norm in sb_text_norm
                                or sb_text_norm in nb_text_norm):
                            partial_match_idx = i
                            break

                    if partial_match_idx >= 0:
                        sb['miss_count'] = 0
                        new_stable.append(sb)
                        for i, nb in enumerate(new_blocks):
                            if used_new[i]:
                                continue
                            nb_text_norm = _normalize_text_for_dedup(
                                nb.get('text', ''))
                            if not nb_text_norm:
                                continue
                            if len(nb_text_norm) < 3:
                                continue
                            if (nb_text_norm in sb_text_norm
                                    or sb_text_norm in nb_text_norm):
                                used_new[i] = True
                        used_new[partial_match_idx] = True
                        matched_any = True
                        continue

                miss = sb.get('miss_count', 0) + 1
                if miss <= STABLE_MISS_LIMIT:
                    sb['miss_count'] = miss
                    new_stable.append(sb)
                continue

            # Точный матч — стандартная обработка.
            used_new[best_idx] = True
            matched_any = True
            nb = new_blocks[best_idx]
            nb_text = nb.get('text', '')
            nb_conf = nb.get('confidence', sb.get('confidence', 0.0))
            old_miss = sb.get('miss_count', 0)
            sb['miss_count'] = max(0, old_miss - 1) if old_miss > 0 else 0
            self._smooth_block_geometry(sb, nb)
            sb['bbox_settled'] = True
            sb['bbox_settle_counter'] = 0
            if not sb_key:
                sb['stable_key'] = _stable_block_key(nb)
            if nb_text == sb_text:
                sb['confidence'] = nb_conf
                sb['pending_text'] = ''
                sb['pending_count'] = 0
            elif _texts_close_enough(sb_text, nb_text):
                sb['confidence'] = nb_conf
                sb['pending_text'] = ''
                sb['pending_count'] = 0
            else:
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
                else:
                    sb['confidence'] = nb_conf
                    sb['pending_text'] = pending_text
                    sb['pending_count'] = pending_count
            new_stable.append(sb)

        if (not matched_any and self._stable_blocks
                and len(new_blocks) >= 1):
            dbg.log_event("pc.overlay_scene_changed_strict",
                          {"new_blocks": len(new_blocks)})
            new_stable = []

        for i, nb in enumerate(new_blocks):
            if used_new[i]:
                continue
            sb = {
                'text': nb.get('text', ''),
                'x': nb.get('x', 0), 'y': nb.get('y', 0),
                'w': nb.get('w', 0), 'h': nb.get('h', 0),
                'confidence': nb.get('confidence', 0.0),
                'initial_x': nb.get('initial_x', nb.get('x', 0)),
                'initial_y': nb.get('initial_y', nb.get('y', 0)),
                'initial_w': nb.get('initial_w', nb.get('w', 0)),
                'initial_h': nb.get('initial_h', nb.get('h', 0)),
                'miss_count': 0,
                'pending_text': '', 'pending_count': 0,
                'bbox_settled': True, 'bbox_settle_counter': 0,
                'stable_key': _stable_block_key(nb),
            }
            new_stable.append(sb)
        self._stable_blocks = new_stable

    @staticmethod
    def _smooth_block_geometry(sb: dict, nb: dict):
        old_x = sb.get('x', 0); old_y = sb.get('y', 0)
        old_w = sb.get('w', 0); old_h = sb.get('h', 0)
        new_x = nb.get('x', old_x); new_y = nb.get('y', old_y)
        new_w = nb.get('w', old_w); new_h = nb.get('h', old_h)
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

    @staticmethod
    def _is_block_too_small(b: dict) -> bool:
        w = b.get('w', 0); h = b.get('h', 0)
        if w <= 0 or h <= 0:
            return True
        return (w * h) < MIN_BLOCK_AREA

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
        try:
            if self._settings_debounce_timer is not None:
                self.root.after_cancel(self._settings_debounce_timer)
                self._settings_debounce_timer = None
        except Exception:
            pass
        self._rendered_blocks = {}
        self._block_tags = {}
        self._hidden_block_tags = set()
        self._last_render_signature = None
        self._fitted_cache = {}
        with self._measure_cache_lock:
            self._measure_cache.clear()
        try:
            self.window.destroy()
        except Exception:
            pass
        dbg.log_event("pc.overlay_destroy")