# src/overlay_render.py
"""OverlayRenderMixin — подбор шрифта и отрисовка блоков.
Миксин для OcrOverlayWindow.
"""

import tkinter.font as tkfont

from src.debug_logger import logger as dbg

from src.overlay_constants import (
    ALPHA_SOLID_THRESHOLD, ALPHA_TRANSPARENT_THRESHOLD,
    VISIBLE_MISS_LIMIT, SIZE_ROUND_TO, TEXT_WIDTH_TOLERANCE,
    GEOMETRY_RECREATE_THRESHOLD, SINGLE_LINE_MAX_CHARS,
    MIN_SINGLE_LINE_FONT, TRUNCATE_ELLIPSIS,
)

from src.overlay_utils import (
    _is_suspicious_text, _is_hud_or_short, _is_fragment_of_known,
    _stable_block_key,
)


class OverlayRenderMixin:
    """Подбор шрифта и отрисовка блоков (translation + debug)."""

    # ============================================================
    # ШРИФТ
    # ============================================================

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

    def _measure_text(self, text, font, width):
        if width == 0:
            try:
                w = font.measure(text)
                h = font.metrics("linespace")
                return (w, h)
            except Exception:
                return None
        try:
            font_size = font.cget("size")
        except Exception:
            font_size = 0
        key = (text, int(font_size), int(width))
        with self._measure_cache_lock:
            cached = self._measure_cache.get(key)
        if cached is not None:
            return cached
        try:
            item = self.canvas.create_text(
                -10000, -10000, text=text, anchor="nw",
                font=font, width=width if width > 0 else 0
            )
            bbox = self.canvas.bbox(item)
            self.canvas.delete(item)
            if bbox is None:
                return None
            result = (bbox[2] - bbox[0], bbox[3] - bbox[1])
            with self._measure_cache_lock:
                if len(self._measure_cache) > 8000:
                    self._measure_cache.clear()
                self._measure_cache[key] = result
            return result
        except Exception:
            return None

    def _fits(self, text, size, mode, max_w, max_h):
        if mode == "truncate":
            return True
        font = self._get_font(size)
        width = 0 if mode == "single" else max_w
        measured = self._measure_text(text, font, width)
        if measured is None:
            return False
        text_w, text_h = measured
        return (text_w <= max_w + TEXT_WIDTH_TOLERANCE
                and text_h <= max_h)

    def _truncate_to_fit(self, text, font, max_w):
        if not text:
            return text
        ellipsis = TRUNCATE_ELLIPSIS
        try:
            ell_w = font.measure(ellipsis)
        except Exception:
            return text
        if ell_w > max_w:
            return ellipsis
        words = text.split()
        if not words:
            return ellipsis
        result = ""
        for w in words:
            candidate = (result + " " + w).strip() if result else w
            try:
                cand_w = font.measure(candidate)
            except Exception:
                break
            if cand_w + ell_w <= max_w:
                result = candidate
            else:
                break
        if not result:
            lo = 0
            hi = len(words[0])
            best = ""
            while lo <= hi:
                mid = (lo + hi) // 2
                candidate = words[0][:mid] + ellipsis
                try:
                    w = font.measure(candidate)
                except Exception:
                    break
                if w <= max_w:
                    best = candidate
                    lo = mid + 1
                else:
                    hi = mid - 1
            return best if best else ellipsis
        if len(result) < len(text):
            return result + ellipsis
        return result

    def _draw_fitted_text_tagged(self, text, x, y, max_w, max_h, tag):
        max_w = max(1, int(max_w))
        max_h = max(1, int(max_h))
        cache_key = (text, max_w, max_h)
        cached = self._fitted_cache.get(cache_key)
        if cached is not None:
            size, mode = cached
            if not self._fits(text, size, mode, max_w, max_h):
                size, mode = self._fit_text_size(text, max_w, max_h)
                self._fitted_cache[cache_key] = (size, mode)
        else:
            size, mode = self._fit_text_size(text, max_w, max_h)
            if len(self._fitted_cache) > 8000:
                self._fitted_cache.clear()
            self._fitted_cache[cache_key] = (size, mode)
        font = self._get_font(size)
        if mode == "single":
            self.canvas.create_text(
                x, y, text=text, fill="#ffffff", font=font,
                anchor="nw", tags=(tag,))
        elif mode == "multi":
            self.canvas.create_text(
                x, y, text=text, fill="#ffffff", font=font,
                anchor="nw", width=max_w, tags=(tag,))
        else:
            truncated = self._truncate_to_fit(text, font, max_w)
            self.canvas.create_text(
                x, y, text=truncated, fill="#ffffff", font=font,
                anchor="nw", tags=(tag,))

    def _fit_text_size(self, text, max_w, max_h):
        is_long = len(text) > SINGLE_LINE_MAX_CHARS
        try:
            if not is_long:
                size = min(self.MAX_FONT_SIZE, max_h)
                size = max(size, MIN_SINGLE_LINE_FONT)
                while size >= MIN_SINGLE_LINE_FONT:
                    font = self._get_font(size)
                    measured = self._measure_text(text, font, 0)
                    if measured is None:
                        size -= 1
                        continue
                    text_w, text_h = measured
                    if (text_w <= max_w + TEXT_WIDTH_TOLERANCE
                            and text_h <= max_h):
                        return size, "single"
                    size -= 1
            size = min(self.MAX_FONT_SIZE, max_h)
            size = max(size, self.MIN_FONT_SIZE)
            while size >= self.MIN_FONT_SIZE:
                font = self._get_font(size)
                measured = self._measure_text(text, font, max_w)
                if measured is None:
                    size -= 1
                    continue
                text_w, text_h = measured
                if (text_w <= max_w + TEXT_WIDTH_TOLERANCE
                        and text_h <= max_h):
                    return size, "multi"
                size -= 1
            return self.MIN_FONT_SIZE, "truncate"
        except Exception:
            return self.MIN_FONT_SIZE, "truncate"

    def _draw_fitted_text(self, text, x, y, max_w, max_h):
        max_w = max(1, int(max_w))
        max_h = max(1, int(max_h))
        cache_key = (text, max_w, max_h)
        cached = self._fitted_cache.get(cache_key)
        if cached is not None:
            size, mode = cached
            if not self._fits(text, size, mode, max_w, max_h):
                size, mode = self._fit_text_size(text, max_w, max_h)
                self._fitted_cache[cache_key] = (size, mode)
        else:
            size, mode = self._fit_text_size(text, max_w, max_h)
            if len(self._fitted_cache) > 8000:
                self._fitted_cache.clear()
            self._fitted_cache[cache_key] = (size, mode)
        font = self._get_font(size)
        if mode == "single":
            self.canvas.create_text(x, y, text=text, fill="#ffffff",
                                    font=font, anchor="nw")
        elif mode == "multi":
            self.canvas.create_text(x, y, text=text, fill="#ffffff",
                                    font=font, anchor="nw", width=max_w)
        else:
            truncated = self._truncate_to_fit(text, font, max_w)
            self.canvas.create_text(x, y, text=truncated, fill="#ffffff",
                                    font=font, anchor="nw")

    # ============================================================
    # RENDER
    # ============================================================

    def _redraw(self, blocks, frame_w, frame_h, mode, alpha, ocr_padding=0):
        if not self.target_hwnd:
            return
        rect = self._get_client_rect_on_screen(use_cache=True)
        if rect is None:
            return
        _, _, client_w, client_h = rect
        if frame_w <= 0 or frame_h <= 0:
            return
        if self._last_drawn_mode != mode:
            self._fitted_cache.clear()
            self._rendered_blocks = {}
            self._block_tags = {}
            self._hidden_block_tags = set()
            self._last_drawn_mode = mode
        scale_x = client_w / frame_w
        scale_y = client_h / frame_h
        if mode == "debug":
            self._draw_debug(blocks, scale_x, scale_y,
                             show_confidence=True, ocr_padding=ocr_padding)
        elif mode == "ocr":
            self._clear_all_rendered()
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
        try:
            self.canvas.delete("debug")
        except Exception:
            pass
        fill_mode = self._stipple_from_alpha(alpha)
        create_rect = self.canvas.create_rectangle
        all_texts = []
        for b in blocks:
            t = b.get('text', '')
            if t:
                all_texts.append(t)
        new_keys = set()
        visible_blocks = []
        for b in blocks:
            if b.get('miss_count', 0) >= VISIBLE_MISS_LIMIT:
                continue
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
            x1 = b.get('x', 0) * scale_x
            y1 = b.get('y', 0) * scale_y
            x2 = (b.get('x', 0) + bw) * scale_x
            y2 = (b.get('y', 0) + bh) * scale_y
            key = b.get('stable_key', '') or _stable_block_key(b)
            new_keys.add(key)
            visible_blocks.append((key, text, x1, y1, x2, y2))
        old_keys = set(self._rendered_blocks.keys())
        for key in old_keys - new_keys:
            self._delete_block_items(key)
        for key, text, x1, y1, x2, y2 in visible_blocks:
            bg_tag = f"b_{key}_bg"
            txt_tag = f"b_{key}_txt"
            prev = self._rendered_blocks.get(key)
            if prev is None or prev.get('mode') != 'translate':
                self._delete_block_items(key)
                self._create_block_items(key, text, x1, y1, x2, y2,
                                         fill_mode, alpha)
                continue
            geometry_changed = (
                    abs(prev['x'] - x1) > GEOMETRY_RECREATE_THRESHOLD or
                    abs(prev['y'] - y1) > GEOMETRY_RECREATE_THRESHOLD or
                    abs(prev['x2'] - x2) > GEOMETRY_RECREATE_THRESHOLD or
                    abs(prev['y2'] - y2) > GEOMETRY_RECREATE_THRESHOLD
            )
            text_changed = prev['text'] != text
            alpha_changed = prev.get('alpha') != alpha
            stipple_changed = prev.get('stipple') != fill_mode
            if geometry_changed:
                self._delete_block_items(key)
                self._create_block_items(key, text, x1, y1, x2, y2,
                                         fill_mode, alpha)
                continue
            if text_changed:
                try:
                    self.canvas.delete(txt_tag)
                except Exception:
                    pass
                self._draw_fitted_text_tagged(
                    text, x1 + 2, y1 + 2,
                    max(1, (x2 - x1) - 4), max(1, (y2 - y1) - 4),
                    txt_tag)
                prev['text'] = text
            if alpha_changed or stipple_changed:
                try:
                    self.canvas.delete(bg_tag)
                except Exception:
                    pass
                if fill_mode == "solid":
                    create_rect(x1, y1, x2, y2, fill="#000000",
                                outline="", tags=(bg_tag, "bg"))
                elif fill_mode is not None:
                    create_rect(x1, y1, x2, y2, fill="#000000",
                                outline="", stipple=fill_mode,
                                tags=(bg_tag, "bg"))
                try:
                    self.canvas.tag_raise(txt_tag)
                except Exception:
                    pass
                prev['alpha'] = alpha
                prev['stipple'] = fill_mode

    def _delete_block_items(self, key):
        try:
            self.canvas.delete(f"b_{key}_bg")
            self.canvas.delete(f"b_{key}_txt")
        except Exception:
            pass
        self._rendered_blocks.pop(key, None)
        self._block_tags.pop(key, None)
        self._hidden_block_tags.discard(key)

    def _create_block_items(self, key, text, x1, y1, x2, y2,
                            fill_mode, alpha):
        bg_tag = f"b_{key}_bg"
        txt_tag = f"b_{key}_txt"
        if fill_mode == "solid":
            self.canvas.create_rectangle(
                x1, y1, x2, y2, fill="#000000", outline="",
                tags=(bg_tag, "bg"))
        elif fill_mode is not None:
            self.canvas.create_rectangle(
                x1, y1, x2, y2, fill="#000000", outline="",
                stipple=fill_mode, tags=(bg_tag, "bg"))
        self._draw_fitted_text_tagged(
            text, x1 + 2, y1 + 2,
            max(1, (x2 - x1) - 4), max(1, (y2 - y1) - 4), txt_tag)
        self._rendered_blocks[key] = {
            'text': text, 'x': x1, 'y': y1, 'x2': x2, 'y2': y2,
            'mode': 'translate', 'alpha': alpha, 'stipple': fill_mode,
        }
        self._block_tags[key] = (x1, y1, x2, y2)

    def _draw_debug(self, blocks, scale_x, scale_y, show_confidence=True,
                    ocr_padding=0):
        for key in list(self._rendered_blocks.keys()):
            if self._rendered_blocks[key].get('mode') != 'debug':
                self._delete_block_items(key)
        try:
            self.canvas.delete("debug")
        except Exception:
            pass
        for key in list(self._rendered_blocks.keys()):
            if self._rendered_blocks[key].get('mode') == 'debug':
                self._rendered_blocks.pop(key, None)
                self._block_tags.pop(key, None)
                self._hidden_block_tags.discard(key)
        create_rect = self.canvas.create_rectangle
        create_text = self.canvas.create_text
        label_font = ("Consolas", 10, "bold")
        try:
            label_line_height = tkfont.Font(
                family="Consolas", size=10, weight="bold"
            ).metrics("linespace")
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
            text = b.get('text', '')
            if text and _is_suspicious_text(text):
                continue
            if self._is_block_too_small(b):
                continue
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
            key = b.get('stable_key', '') or _stable_block_key(b)
            tag = f"d_{key}"
            create_rect(gx1, gy1, gx2, gy2, outline="#00ff00",
                        width=2, tags=(tag, "debug"))
            create_rect(x1, y1, x2, y2, outline="#ff0000",
                        width=2, tags=(tag, "debug"))
            if text:
                max_text_w = max(120, int(canvas_w - x1 - 10))
                max_text_w = min(max_text_w, 600)
                max_text_w = max(max_text_w, 80)
                approx_chars_per_line = max(1, int(max_text_w / 7))
                approx_lines = min(
                    MAX_LABEL_LINES,
                    max(1, (len(text) + approx_chars_per_line - 1)
                        // approx_chars_per_line))
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
                create_rect(label_x, label_y,
                            label_x + label_w, label_y + label_h,
                            fill="#000000", outline="#444444",
                            width=1, tags=(tag, "debug"))
                create_text(label_x + label_pad_x,
                            label_y + label_pad_y,
                            text=text, fill="#ffff00",
                            anchor="nw", font=label_font,
                            width=max_text_w, tags=(tag, "debug"))
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
                                    text=conf_text, fill="#00ff00",
                                    anchor="nw",
                                    font=("Consolas", 10, "bold"),
                                    tags=(tag, "debug"))
                except (TypeError, ValueError):
                    pass
            self._rendered_blocks[key] = {
                'text': text, 'x': x1, 'y': y1,
                'x2': x2, 'y2': y2, 'mode': 'debug',
                'alpha': 0, 'stipple': None,
            }
            self._block_tags[key] = (x1, y1, x2, y2)