# src/ocr_overlay.py
"""Класс OcrOverlayWindow — собирается из миксинов.

Модуль-фасад. Вся логика вынесена в:
  - overlay_constants   — константы
  - overlay_utils       — текстовые + геометрические утилиты
  - overlay_core        — OverlayCoreMixin (жизненный цикл, окно, polling,
                          стабилизация блоков)
  - overlay_render      — OverlayRenderMixin (подбор шрифта, отрисовка)

Импорт `from src.ocr_overlay import OcrOverlayWindow` работает как раньше.
"""

from src.overlay_core import OverlayCoreMixin
from src.overlay_render import OverlayRenderMixin


class OcrOverlayWindow(OverlayCoreMixin, OverlayRenderMixin):
    """Прозрачное окно поверх исходного окна ПК.

    Все методы и атрибуты предоставляются миксинами:
      - OverlayCoreMixin   — жизненный цикл, позиционирование, polling,
                             стабилизация блоков.
      - OverlayRenderMixin — подбор шрифта, отрисовка (translation + debug).

    Порядок наследования: OverlayCoreMixin первым (его __init__ создаёт
    окно, canvas, все атрибуты). OverlayRenderMixin добавляет методы
    отрисовки, которые используют атрибуты, созданные в Core.
    """
    pass