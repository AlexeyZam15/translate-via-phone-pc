# main.py
"""
Точка входа. Запускает упрощённый GUI translate_via_phone.
Все модули лежат в папке src/.

ИЗМЕНЕНО:
  - В начале __main__ вызывается kill_other_instances() —
    убивает все другие запущенные экземпляры приложения
    ДО открытия debug.log и ДО создания tk.Tk().
  - Это устраняет: несколько окон/оверлеев, конфликт портов,
    обрезание debug.log вторым процессом.
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

    # ИЗМЕНЕНО: убиваем другие экземпляры ДО открытия debug.log
    # и ДО создания tk.Tk(). Иначе второй процесс обрежет debug.log
    # (открывается в режиме "w") и создаст второй оверлей/окно.
    try:
        from src.singleton import kill_other_instances
        killed = kill_other_instances()
        if killed:
            print(f"[Main] ✅ Завершено старых экземпляров: {killed}")
            print()
    except Exception as e:
        # Не критично, если не удалось — продолжаем запуск.
        print(f"[Main] ⚠️ Не удалось завершить старые экземпляры: {e}")
        print()

    if not check_dependencies():
        sys.exit(1)

    _LOG_FILE = open(os.path.join(APP_DIR, "debug.log"), "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, _LOG_FILE)
    sys.stderr = _Tee(sys.__stderr__, _LOG_FILE)

    from src.config import APP_DIR as _APP_DIR, DEBUG_LOG_FILE
    from src.debug_logger import logger as dbg
    from src.text_logger import init_text_log

    dbg.log_event("pc.app_start", {
        "app_dir": _APP_DIR,
        "debug_log_file": DEBUG_LOG_FILE,
    })

    # ИЗМЕНЕНО: ocr_text.log пересоздаётся при старте приложения,
    # как debug.log и debug_timing.jsonl.
    init_text_log()

    import tkinter as tk
    from src.simple_gui import SimplePhoneTranslator

    app = None
    root = None
    try:
        root = tk.Tk()
        app = SimplePhoneTranslator(root)
        root.mainloop()
    except KeyboardInterrupt:
        # ИЗМЕНЕНО: обработка Ctrl+C — корректное завершение приложения
        print("\n⏹️ Получен Ctrl+C, завершаем работу...")
        dbg.log_event("pc.app_keyboard_interrupt")
        if app is not None:
            try:
                app.on_closing()
            except Exception as e:
                print(f"⚠️ Ошибка при завершении: {e}")
        # on_closing уже вызывает root.destroy(), но на случай если
        # он не дошёл до этого — пробуем ещё раз
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
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