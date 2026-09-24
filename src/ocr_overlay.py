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
  текст остаётся. Микро-сдвиги координат от OCR (в пределах
  STABLE_MATCH_TOLERANCE_PX) игнорируются — блок не дёргается.
  Если блок не появляется STABLE_MISS_LIMIT раз подряд — удаляется.

Режимы:
  "translate" — рисовать перевод (фон + текст)
  "debug"     — рисовать рамки + OCR-текст + confidence
  "ocr"       — устаревший, не рисуется (см. _redraw)
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

# Пороги для краевых случаев прозрачности
ALPHA_SOLID_THRESHOLD = 240  # >= — сплошная заливка
ALPHA_TRANSPARENT_THRESHOLD = 15  # <= — фон не рисуется

# Стабилизация блоков
STABLE_MATCH_TOLERANCE_PX = 40  # допуск по расстоянию центров
STABLE_MISS_LIMIT = 2  # сколько пропусков до удаления блока

# Если данных от OCR нет дольше этого времени — не перерисовываем
STALE_DATA_TIMEOUT = 2.0


def _blocks_fingerprint(blocks) -> str:
    """Быстрый хэш содержимого блоков для детекции изменений."""
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


def _center(block):
    """Возвращает центр блока (cx, cy) по x/y/w/h."""
    return (
        block.get('x', 0) + block.get('w', 0) // 2,
        block.get('y', 0) + block.get('h', 0) // 2,
    )


class OcrOverlayWindow:
    """Прозрачное окно поверх исходного окна ПК."""

    MIN_FONT_SIZE = 6
    MAX_FONT_SIZE = 72
    POLL_INTERVAL_MS = 100  # опрос OCR-блоков (быстрее реакция)
    POSITION_INTERVAL_MS = 250  # слежение за позицией окна

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

        # Стабильные блоки — то, что реально нарисовано на экране.
        self._stable_blocks = []

        # Состояние предыдущей отрисовки
        self._last_fingerprint = None
        self._last_mode = None
        self._last_alpha = None
        self._last_frame_size = (0, 0)

        # Кэш rect окна
        self._cached_client_rect = None
        self._last_client_rect_ts = 0.0

        # Таймеры
        self._poll_timer = None
        self._position_timer = None

        # Стартуем
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

    # ============================================================
    # Смена целевого окна
    # ============================================================

    def set_target_hwnd(self, hwnd: int):
        """Меняет целевое окно. Сбрасывает стабильные блоки и кэш."""
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
        self._stable_blocks = []
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

    # ============================================================
    # Слежение за позицией исходного окна
    # ============================================================

    def _get_client_rect_on_screen(self, use_cache: bool = True):
        """Возвращает (x, y, w, h) клиентской области или None."""
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
            if self.target_hwnd:
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
        if not self.target_hwnd:
            return
        if not win32gui.IsWindow(self.target_hwnd):
            return
        if win32gui.IsIconic(self.target_hwnd):
            return

        # Если данные от OCR устарели — не трогаем оверлей.
        last_ts = getattr(self.receiver, 'last_received_ts', 0.0)
        if last_ts > 0 and (time.time() - last_ts) > STALE_DATA_TIMEOUT:
            return

        blocks, (fw, fh) = self.receiver.get_blocks()
        mode = self.receiver.get_mode()
        alpha = self.receiver.get_alpha()

        # --- Пустой результат: текст исчез, очищаем всё сразу ---
        if not blocks:
            if self._stable_blocks:
                self._stable_blocks = []
                self._last_fingerprint = None
                self.canvas.delete("all")
                dbg.log_event("pc.overlay_cleared")
            return

        # --- Стабилизация блоков ---
        self._stabilize_blocks(blocks)

        # --- Проверка: изменилось ли что-то для отрисовки? ---
        fp = _blocks_fingerprint(self._stable_blocks)
        if (fp == self._last_fingerprint
                and mode == self._last_mode
                and alpha == self._last_alpha
                and (fw, fh) == self._last_frame_size):
            return

        self._last_fingerprint = fp
        self._last_mode = mode
        self._last_alpha = alpha
        self._last_frame_size = (fw, fh)

        self._redraw(self._stable_blocks, fw, fh, mode, alpha)

    def _stabilize_blocks(self, new_blocks):
        """
        Стабилизирует блоки: микро-сдвиги координат игнорируются,
        блок «схватывается» при первом появлении и держит позицию,
        пока текст остаётся.
        """
        tol = STABLE_MATCH_TOLERANCE_PX
        tol2 = tol * tol

        used_new = [False] * len(new_blocks)
        new_stable = []

        for sb in self._stable_blocks:
            sb_text = sb.get('text', '')
            sb_cx, sb_cy = _center(sb)

            best_idx = -1
            best_d2 = tol2
            for i, nb in enumerate(new_blocks):
                if used_new[i]:
                    continue
                if nb.get('text', '') != sb_text:
                    continue
                nb_cx, nb_cy = _center(nb)
                dx = nb_cx - sb_cx
                dy = nb_cy - sb_cy
                d2 = dx * dx + dy * dy
                if d2 <= best_d2:
                    best_d2 = d2
                    best_idx = i

            if best_idx >= 0:
                nb = new_blocks[best_idx]
                used_new[best_idx] = True
                sb['confidence'] = nb.get('confidence', sb.get('confidence', 0.0))
                sb['miss_count'] = 0
                new_stable.append(sb)
            else:
                miss = sb.get('miss_count', 0) + 1
                if miss <= STABLE_MISS_LIMIT:
                    sb['miss_count'] = miss
                    new_stable.append(sb)

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
            }
            new_stable.append(sb)

        self._stable_blocks = new_stable

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

        # ИЗМЕНЕНО: режим "ocr" больше не используется — при неготовом
        # переводчике Android вообще ничего не шлёт. Но на всякий случай
        # (старый клиент) — не рисуем ничего.
        if mode == "debug":
            self._draw_debug(blocks, scale_x, scale_y, show_confidence=True)
        elif mode == "ocr":
            # Ничего не рисуем — это сырой OCR, не перевод.
            pass
        else:
            self._draw_translation(blocks, scale_x, scale_y, alpha)

    def _stipple_from_alpha(self, alpha: int):
        """
        Режим заливки фона:
          None     — не рисовать (полная прозрачность, alpha <= 15)
          "solid"  — сплошная заливка (полная непрозрачность, alpha >= 240)
          строка   — имя stipple-паттерна
        """
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
        """Режим перевода: прямоугольники фона + текст."""
        fill_mode = self._stipple_from_alpha(alpha)
        create_rect = self.canvas.create_rectangle
        has_letters = self._has_letters
        draw_text = self._draw_fitted_text

        for b in blocks:
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

            if fill_mode == "solid":
                create_rect(x1, y1, x2, y2, fill="#000000",
                            outline="", tags=("bg",))
            elif fill_mode is not None:
                create_rect(x1, y1, x2, y2, fill="#000000",
                            outline="", stipple=fill_mode, tags=("bg",))

            draw_text(text, x1 + 2, y1 + 2,
                      max(1, (x2 - x1) - 4), max(1, (y2 - y1) - 4))

    def _draw_debug(self, blocks, scale_x, scale_y, show_confidence=True):
        """Режим отладки: рамки + текст + (опционально) уверенность.

        ИЗМЕНЕНО:
          - показывается ПОЛНЫЙ OCR-текст (раньше было text[:40] — обрезался);
          - если над рамкой нет места (верхний край окна), текст рисуется
            ВНУТРИ рамки, а не над ней;
          - задана width у create_text — длинный текст переносится по словам;
          - под текстом рисуется полупрозрачный фон для читаемости;
          - ограничение MAX_LABEL_LINES = 4 строки, чтобы текст не закрывал
            собой всю рамку.
        """
        create_rect = self.canvas.create_rectangle
        create_text = self.canvas.create_text

        # Размеры шрифта и отступы для подписи
        label_font = ("Consolas", 10, "bold")
        try:
            label_line_height = tkfont.Font(family="Consolas", size=10,
                                            weight="bold").metrics("linespace")
        except Exception:
            label_line_height = 14
        label_pad_x = 6
        label_pad_y = 4
        MAX_LABEL_LINES = 4

        # Ширина canvas — чтобы ограничить перенос текста
        try:
            canvas_w = self.canvas.winfo_width()
            if canvas_w <= 1:
                canvas_w = self.canvas.winfo_reqwidth()
        except Exception:
            canvas_w = 800

        for b in blocks:
            bw = b.get('w', 0)
            bh = b.get('h', 0)
            if bw <= 0 or bh <= 0:
                continue

            x1 = b.get('x', 0) * scale_x
            y1 = b.get('y', 0) * scale_y
            x2 = (b.get('x', 0) + bw) * scale_x
            y2 = (b.get('y', 0) + bh) * scale_y

            ix1 = b.get('initial_x', b.get('x', 0)) * scale_x
            iy1 = b.get('initial_y', b.get('y', 0)) * scale_y
            ix2 = (b.get('initial_x', b.get('x', 0))
                   + b.get('initial_w', bw)) * scale_x
            iy2 = (b.get('initial_y', b.get('y', 0))
                   + b.get('initial_h', bh)) * scale_y

            create_rect(ix1, iy1, ix2, iy2, outline="#00ff00", width=2)
            create_rect(x1, y1, x2, y2, outline="#ff0000", width=2)

            text = b.get('text', '')
            if text:
                # Ширина для переноса: не больше ширины canvas и не больше
                # ширины рамки + запас (чтобы текст мог вылезать за рамку
                # горизонтально, но не уходил за экран).
                max_text_w = max(120, int(canvas_w - x1 - 10))
                # Ограничим сверху — чтобы подпись не растягивалась
                # на пол-экрана из-за одной длинной строки без пробелов.
                max_text_w = min(max_text_w, 600)
                max_text_w = max(max_text_w, 80)

                # Оцениваем высоту подписи (максимум MAX_LABEL_LINES строк).
                # Точную высоту Tkinter вернёт только после create_text,
                # поэтому берём оценку по длине текста и ширине.
                approx_chars_per_line = max(1, int(max_text_w / 7))
                approx_lines = min(
                    MAX_LABEL_LINES,
                    max(1, (len(text) + approx_chars_per_line - 1)
                        // approx_chars_per_line)
                )
                label_h = approx_lines * label_line_height + label_pad_y * 2
                label_w = max_text_w + label_pad_x * 2

                # Позиция подписи:
                #   по умолчанию — НАД рамкой (anchor="nw", y = y1 - label_h),
                #   но если сверху не хватает места — ВНУТРИ рамки
                #   (y = y1 + 2), с выравниванием по левому краю рамки.
                if y1 - label_h >= 0:
                    label_x = x1
                    label_y = y1 - label_h
                else:
                    label_x = x1
                    label_y = y1 + 2

                # Не даём подписи уйти за правый край canvas
                if label_x + label_w > canvas_w:
                    label_x = max(0, canvas_w - label_w - 2)

                # Фон под текстом — полупрозрачный чёрный
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
                        # Если справа нет места — рисуем слева от рамки
                        if conf_x + 40 > canvas_w:
                            conf_x = max(0, x1 - 44)
                        # Если сверху нет места — чуть ниже
                        if conf_y < 0:
                            conf_y = y2 + 2
                        create_text(conf_x, conf_y,
                                    text=conf_text,
                                    fill="#00ff00", anchor="nw",
                                    font=("Consolas", 10, "bold"))
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