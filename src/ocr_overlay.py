# src/ocr_overlay.py
"""Прозрачный оверлей поверх исходного окна ПК.

Показывает только текст перевода (и фон под ним),
не дублирует кадр ПК. Позиция и размер = клиентская область
исходного окна (target_hwnd).
"""

import hashlib
import time
import traceback
import tkinter as tk
import tkinter.font as tkfont

import win32gui

from src.ocr_receiver import OcrReceiver
from src.debug_logger import logger as dbg

# Цвет-ключ прозрачности. Всё, что нарисовано этим цветом,
# становится полностью прозрачным (видно окно под оверлеем).
TRANSPARENT_COLOR = "#010101"


def _blocks_fingerprint(blocks) -> str:
    """Хэш содержимого блоков — чтобы не перерисовывать без изменений."""
    h = hashlib.md5()
    for b in blocks:
        h.update(str(b.get('text', '')).encode('utf-8', errors='ignore'))
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


class OcrOverlayWindow:
    """Прозрачное окно поверх исходного окна ПК."""

    MIN_FONT_SIZE = 6
    MAX_FONT_SIZE = 72
    POLL_INTERVAL_MS = 250  # опрос OCR-блоков
    POSITION_INTERVAL_MS = 400  # слежение за позицией окна

    def __init__(self, root, receiver: OcrReceiver, target_hwnd: int,
                 pc_streamer=None, on_close=None):
        self.root = root
        self.receiver = receiver
        self.target_hwnd = target_hwnd
        self.pc_streamer = pc_streamer
        self.on_close = on_close

        # Создаём прозрачное окно поверх всего
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

        # Кэши
        self._font_cache = {}
        self._fitted_cache = {}

        # Состояние
        self._last_fingerprint = None
        self._last_mode = None
        self._last_alpha = None
        self._last_frame_size = (0, 0)

        # Кэш rect окна
        self._cached_client_rect = None  # (x, y, w, h)
        self._last_client_rect_ts = 0.0

        # Таймеры
        self._poll_timer = None
        self._position_timer = None

        # Стартуем
        dbg.log_event("pc.overlay_create", {"target_hwnd": target_hwnd})
        self._update_position()
        self._poll()
        self._position_loop()

    # ============================================================
    # Слежение за позицией исходного окна
    # ============================================================

    def _get_client_rect_on_screen(self, use_cache: bool = True):
        """
        Возвращает координаты клиентской области исходного окна
        на экране: (x, y, width, height) или None.

        use_cache=True — использует закэшированный rect, если он моложе
        POSITION_INTERVAL_MS.
        """
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
        """Подгоняет окно оверлея под клиентскую область исходного окна."""
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
            self.window.deiconify()
            self.window.geometry(f"{w}x{h}+{x}+{y}")
            self.window.lift()
            try:
                self.window.attributes('-topmost', True)
            except Exception:
                pass
        except Exception as e:
            dbg.log_event("pc.overlay_set_pos_error", {"error": str(e)})

    def _position_loop(self):
        try:
            self._update_position()
        except Exception:
            pass
        try:
            self._position_timer = self.root.after(
                self.POSITION_INTERVAL_MS, self._position_loop
            )
        except Exception:
            pass

    # ============================================================
    # Опрос OCR-блоков
    # ============================================================

    def _poll(self):
        try:
            self._refresh()
        except Exception as e:
            dbg.log_event("pc.overlay_poll_error", {
                "error": str(e),
                "traceback": traceback.format_exc(),
            })
        try:
            self._poll_timer = self.root.after(self.POLL_INTERVAL_MS, self._poll)
        except Exception:
            pass

    def _refresh(self):
        if not win32gui.IsWindow(self.target_hwnd):
            return
        if win32gui.IsIconic(self.target_hwnd):
            return

        blocks, (fw, fh) = self.receiver.get_blocks()
        mode = self.receiver.get_mode()
        alpha = self.receiver.get_alpha()

        # Проверяем, изменилось ли содержимое блоков
        fp = _blocks_fingerprint(blocks)
        same = (
                fp == self._last_fingerprint
                and mode == self._last_mode
                and alpha == self._last_alpha
                and (fw, fh) == self._last_frame_size
        )
        if same:
            return

        self._last_fingerprint = fp
        self._last_mode = mode
        self._last_alpha = alpha
        self._last_frame_size = (fw, fh)

        if not blocks:
            self.canvas.delete("all")
            return

        self._redraw(blocks, fw, fh, mode, alpha)

    # ============================================================
    # Отрисовка
    # ============================================================

    @staticmethod
    def _has_letters(s: str) -> bool:
        for c in s:
            if c.isalpha():
                return True
        return False

    def _redraw(self, blocks, frame_w, frame_h, mode, alpha):
        """Рисует блоки на canvas. Координаты OCR → пиксели клиентской области."""
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

        self.canvas.delete("all")

        if mode == "debug":
            self._draw_debug(blocks, scale_x, scale_y)
        else:
            self._draw_translation(blocks, scale_x, scale_y, alpha)

    def _draw_translation(self, blocks, scale_x, scale_y, alpha):
        """Режим перевода: прямоугольники фона + текст."""
        for b in blocks:
            bx = b.get('x', 0)
            by = b.get('y', 0)
            bw = b.get('w', 0)
            bh = b.get('h', 0)
            text = b.get('text', '')
            if bw <= 0 or bh <= 0 or not text:
                continue
            if not self._has_letters(text):
                continue

            x1 = bx * scale_x
            y1 = by * scale_y
            x2 = (bx + bw) * scale_x
            y2 = (by + bh) * scale_y

            # Фон под текстом — полупрозрачный через stipple
            self.canvas.create_rectangle(
                x1, y1, x2, y2,
                fill="#000000",
                outline="",
                stipple="gray75",
                tags=("bg",)
            )

            self._draw_fitted_text(
                text, x1 + 2, y1 + 2,
                max(1, (x2 - x1) - 4),
                max(1, (y2 - y1) - 4)
            )

    def _draw_debug(self, blocks, scale_x, scale_y):
        """Режим отладки: рамки + текст + уверенность."""
        for b in blocks:
            bx = b.get('x', 0)
            by = b.get('y', 0)
            bw = b.get('w', 0)
            bh = b.get('h', 0)
            text = b.get('text', '')
            conf = b.get('confidence', 0.0)
            if bw <= 0 or bh <= 0:
                continue

            x1 = bx * scale_x
            y1 = by * scale_y
            x2 = (bx + bw) * scale_x
            y2 = (by + bh) * scale_y

            init_x = b.get('initial_x', bx)
            init_y = b.get('initial_y', by)
            init_w = b.get('initial_w', bw)
            init_h = b.get('initial_h', bh)
            ix1 = init_x * scale_x
            iy1 = init_y * scale_y
            ix2 = (init_x + init_w) * scale_x
            iy2 = (init_y + init_h) * scale_y

            self.canvas.create_rectangle(
                ix1, iy1, ix2, iy2,
                outline="#00ff00", width=2
            )
            self.canvas.create_rectangle(
                x1, y1, x2, y2,
                outline="#ff0000", width=2
            )
            if text:
                self.canvas.create_text(
                    x1 + 2, y1 - 4, text=text[:40],
                    fill="#ffff00", anchor="sw",
                    font=("Consolas", 10, "bold")
                )
            try:
                conf_val = float(conf)
                if conf_val > 0:
                    self.canvas.create_text(
                        x2 + 6, y1 + 2,
                        text=f"{conf_val * 100:.0f}%",
                        fill="#00ff00", anchor="nw",
                        font=("Consolas", 10, "bold")
                    )
            except (TypeError, ValueError):
                pass

    # ============================================================
    # Шрифты и подгонка текста
    # ============================================================

    def _get_font(self, size: int) -> tkfont.Font:
        f = self._font_cache.get(size)
        if f is None:
            f = tkfont.Font(family="Arial", size=size, weight="bold")
            self._font_cache[size] = f
        return f

    def _draw_fitted_text(self, text, x, y, max_w, max_h):
        """
        Рисует текст, подбирая размер шрифта под размеры блока.
        Использует кэш fitted-размера по ключу (text, max_w, max_h).
        """
        max_w = max(1, int(max_w))
        max_h = max(1, int(max_h))

        key = (text, max_w, max_h)
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

    # ============================================================
    # Управление окном
    # ============================================================

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
        except Exception:
            pass

    def destroy(self):
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
            self.window.destroy()
        except Exception:
            pass
        dbg.log_event("pc.overlay_destroy")