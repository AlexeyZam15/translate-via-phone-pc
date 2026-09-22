# main.py
"""
Точка входа. Запускает упрощённый GUI translate_via_phone.
Все модули лежат в папке src/.
"""

import os
import sys


def _get_app_dir() -> str:
    """Возвращает папку приложения (скрипта или exe)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _get_app_dir()

if not getattr(sys, "frozen", False):
    if APP_DIR not in sys.path:
        sys.path.insert(0, APP_DIR)


class _Tee:
    """Дублирует вывод в несколько потоков (консоль + файл)."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass


_LOG_FILE = open(os.path.join(APP_DIR, "debug.log"), "w", encoding="utf-8")
sys.stdout = _Tee(sys.__stdout__, _LOG_FILE)
sys.stderr = _Tee(sys.__stderr__, _LOG_FILE)


def check_dependencies():
    """Проверяет наличие обязательных библиотек. Возвращает True/False."""
    missing = []
    try:
        from PIL import Image, ImageTk  # noqa: F401
    except ImportError:
        missing.append("Pillow")
    try:
        import cv2  # noqa: F401
    except ImportError:
        missing.append("opencv-python")
    try:
        import numpy  # noqa: F401
    except ImportError:
        missing.append("numpy")

    if missing:
        print(f"❌ Ошибка: отсутствуют библиотеки: {', '.join(missing)}")
        print(f"   Установите: pip install {' '.join(missing)}")
        return False

    return True


if __name__ == "__main__":
    print("=" * 60)
    print("  translate_via_phone")
    print("  Захват выбранного окна ПК → трансляция на телефон")
    print("  → OCR + перевод на телефоне → оверлей на ПК")
    print("=" * 60)
    print()

    if not check_dependencies():
        sys.exit(1)

    from src.config import APP_DIR as _APP_DIR, DEBUG_LOG_FILE
    from src.debug_logger import logger as dbg

    dbg.log_event("pc.app_start", {
        "app_dir": _APP_DIR,
        "debug_log_file": DEBUG_LOG_FILE,
    })

    import tkinter as tk
    from src.simple_gui import SimplePhoneTranslator

    try:
        root = tk.Tk()
        app = SimplePhoneTranslator(root)
        root.mainloop()
    except Exception as e:
        dbg.log_event("pc.app_fatal", {"error": str(e)})
        raise
    finally:
        dbg.log_event("pc.app_stop")
        dbg.flush()
        try:
            _LOG_FILE.close()
        except Exception:
            pass