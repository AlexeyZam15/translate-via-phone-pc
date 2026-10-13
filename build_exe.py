# build_exe.py
"""
Сборка translate_via_phone в .exe (PyInstaller)

Запуск: python build_exe.py

ИЗМЕНЕНО:
  - src.translate_server в hidden-imports.
  - src.settings_manager, src.settings_server в hidden-imports.
  - src.gui_settings_window, src.gui_help_windows в hidden-imports
    (после разделения simple_gui.py на 3 файла).
  - ctranslate2 + sentencepiece для HTTP-перевода (офлайн).
  - Модель через --add-data=models;models.
  - googletrans/argostranslate НЕ включены.
  - Копирование app-debug.apk рядом с exe (для установки на Android).
  - ЗАРАНЕЕ ПРОВЕРЯЕТСЯ ДОСТУП: перед сборкой завершаются
    запущенные экземпляры translate_via_phone.exe, а также
    adb.exe, запущенный из dist (иначе WinError 5 при
    перезаписи adb.exe / удалении dist).
  - _safe_rmtree: удаляет файлы по одному, при PermissionError
    переименовывает залоченный файл в *.locked_<ts> и продолжает.
    В конце выводит список файлов, которые не удалось удалить.
  - _kill_processes_using_path: через psutil находит процессы,
    чей exe() или cwd() лежит внутри указанной папки, и завершает их.
"""

import PyInstaller.__main__
import os
import sys
import shutil
import time
import warnings
import subprocess

warnings.filterwarnings('ignore', category=DeprecationWarning)
warnings.filterwarnings('ignore', category=UserWarning)

os.environ['PYTHONWARNINGS'] = 'ignore'

APP_NAME = "translate_via_phone"
APP_ICON = "app_icon.ico"
OUTPUT_DIR = "dist"
BUILD_DIR = "build"
MAIN_SCRIPT = "main.py"

CLEAN_BEFORE_BUILD = True

EXTERNAL_TOOLS = ["adb.exe"]

# Кандидаты APK — ищем рядом с проектом и копируем в dist
APK_CANDIDATES = [
    "app-debug.apk",
    "app-release.apk",
    "ScreenTranslator.apk",
    "screentranslator.apk",
]

EXCLUDED_MODULES = [
    'PyQt5', 'PySide2', 'PySide6', 'PyQt6',
    'matplotlib', 'mpl_toolkits',
    'pandas', 'sklearn', 'scikit-learn',
    'tensorflow', 'keras', 'theano',
    'torch', 'torchvision', 'torchaudio',
    'scipy', 'skimage', 'shapely', 'pyclipper',
    'easyocr', 'playwright',
    'test', 'tests', 'pytest',
    'doc', 'docs', 'examples', 'demo',
    'jupyter', 'notebook', 'ipykernel', 'ipython',
    'nbconvert', 'nbformat',
    'django', 'flask', 'fastapi', 'tornado',
    'aiohttp',
    'starlette', 'uvicorn',
    'sqlite3', 'psycopg2', 'mysql', 'pymongo', 'sqlalchemy',
    'cryptography', 'openssl', 'pycryptodome',
    'mayavi', 'vtk',
    'setuptools', 'pip',
    'pkg_resources', 'site', 'venv',
    'lib2to3', 'ensurepip',
    'numpy.f2py',
    'numpy.f2py.tests',
    'numpy.testing',
    'numpy.distutils',
]

USE_UPX = True
UPX_PATH = "upx"

# Имена процессов, которые могут держать файлы в dist.
# translate_via_phone.exe — наше приложение, запущенное из dist.
# adb.exe проверяется отдельно (только если запущен из dist).
PROCESS_NAMES_TO_KILL = [
    f"{APP_NAME}.exe",
]


# ============================================================
# ПРОВЕРКА И ОСВОБОЖДЕНИЕ ФАЙЛОВ
# ============================================================

def _is_process_running(name: str) -> bool:
    """Проверяет, запущен ли процесс с таким именем."""
    name_lower = name.lower()
    try:
        import psutil
        for p in psutil.process_iter(['name']):
            try:
                pname = (p.info.get('name') or '').lower()
                if pname == name_lower:
                    return True
            except Exception:
                continue
        return False
    except ImportError:
        pass

    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
            if sys.platform == "win32" else 0,
        )
        if result.returncode == 0 and name_lower in result.stdout.lower():
            return True
    except Exception:
        pass
    return False


def _kill_process(name: str) -> int:
    """Завершает все процессы с таким именем. Возвращает число убитых."""
    killed = 0
    name_lower = name.lower()

    try:
        import psutil
        for p in psutil.process_iter(['name']):
            try:
                pname = (p.info.get('name') or '').lower()
                if pname == name_lower:
                    p.kill()
                    killed += 1
            except Exception:
                continue
        if killed > 0:
            return killed
    except ImportError:
        pass

    try:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", name],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
            if sys.platform == "win32" else 0,
        )
        if result.returncode == 0:
            killed = 1
    except Exception:
        pass
    return killed


def _kill_processes_using_path(path: str) -> int:
    """Завершает процессы, чей exe() или cwd() лежит внутри path.

    Нужно, чтобы убить adb.exe, запущенный из dist\\translate_via_phone,
    и любой python.exe с cwd внутри dist.

    Возвращает число убитых процессов. Текущий процесс не трогает.
    """
    if not path:
        return 0
    abs_path = os.path.abspath(path).lower()
    if not os.path.isdir(abs_path):
        return 0

    killed = 0
    self_pid = os.getpid()

    try:
        import psutil
    except ImportError:
        # Без psutil не можем проверить путь. Пробуем taskkill
        # по имени adb.exe — но это может убить чужой adb.
        # Лучше предупредить.
        print("[Сборка] ⚠️ psutil не установлен — не могу проверить пути "
              "процессов. Если сборка падает на удалении dist, "
              "закройте adb.exe вручную (диспетчер задач).")
        return 0

    for p in psutil.process_iter(['pid', 'name', 'exe', 'cwd']):
        try:
            if p.pid == self_pid:
                continue
            info = p.info
            proc_exe = (info.get('exe') or '').lower()
            proc_cwd = (info.get('cwd') or '').lower()

            in_path = False
            if proc_exe and proc_exe.startswith(abs_path):
                in_path = True
            if proc_cwd and proc_cwd.startswith(abs_path):
                in_path = True

            if in_path:
                pname = info.get('name') or '?'
                print(f"[Сборка] ⚠️ Процесс {pname} (PID {p.pid}) "
                      f"держит {abs_path}")
                try:
                    p.kill()
                    killed += 1
                except Exception as e:
                    print(f"[Сборка] ⚠️ Не удалось завершить "
                          f"{pname} (PID {p.pid}): {e}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue

    return killed


def _ensure_no_running_instance():
    """Завершает процессы, которые могут держать файлы в dist.

    Проверяет:
      - translate_via_phone.exe (наше приложение, запущенное из dist).
      - Любой процесс с exe()/cwd() внутри dist (в т.ч. adb.exe
        из dist\\translate_via_phone\\adb.exe).
    """
    print("[Сборка] 🔍 Проверка запущенных экземпляров приложения...")
    any_found = False

    for proc_name in PROCESS_NAMES_TO_KILL:
        if _is_process_running(proc_name):
            any_found = True
            print(f"[Сборка] ⚠️ Найден запущенный процесс: {proc_name}")
            killed = _kill_process(proc_name)
            if killed > 0:
                print(f"[Сборка] ✅ Завершено процессов: {killed}")
            else:
                print(f"[Сборка] ❌ Не удалось завершить {proc_name}. "
                      f"Закройте приложение вручную и повторите сборку.")
                sys.exit(1)

    # Убить процессы, чей exe/cwd внутри dist (adb.exe из dist и т.п.)
    killed_by_path = _kill_processes_using_path(OUTPUT_DIR)
    if killed_by_path > 0:
        any_found = True
        print(f"[Сборка] ✅ Завершено процессов по пути: {killed_by_path}")

    if any_found:
        time.sleep(1.0)
    else:
        print("[Сборка] ✅ Запущенных экземпляров не найдено.")


def _check_write_access(path: str) -> bool:
    """Проверяет, можно ли писать в файл или папку."""
    try:
        if os.path.isdir(path):
            test_file = os.path.join(path, ".write_test_tmp")
            with open(test_file, "w"):
                pass
            try:
                os.remove(test_file)
            except Exception:
                pass
            return True
        else:
            if not os.path.exists(path):
                parent = os.path.dirname(path) or "."
                return _check_write_access(parent)
            with open(path, "a"):
                pass
            return True
    except PermissionError:
        return False
    except Exception:
        return False


def _try_remove_file(path: str, locked_list: list) -> bool:
    """Пытается удалить один файл.

    При PermissionError пробует переименовать в *.locked_<ts>.
    Если и это не удалось — добавляет путь в locked_list.
    Возвращает True, если файл удалён или переименован.
    """
    try:
        os.remove(path)
        return True
    except PermissionError:
        pass
    except FileNotFoundError:
        return True
    except Exception as e:
        print(f"[Сборка] ⚠️ Не удалось удалить {path}: {e}")

    # Пробуем переименовать — часто работает, даже если файл залочен.
    try:
        ts = int(time.time())
        renamed = f"{path}.locked_{ts}"
        os.rename(path, renamed)
        print(f"[Сборка] ⚠️ Файл залочен, переименован: "
              f"{os.path.basename(path)} → {os.path.basename(renamed)}")
        # Пытаемся удалить переименованный файл (может уже не быть залочен).
        try:
            os.remove(renamed)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"[Сборка] ⚠️ Не удалось переименовать {path}: {e}")

    locked_list.append(path)
    return False


def _safe_rmtree(folder: str) -> bool:
    """Удаляет папку. Сначала пытается shutil.rmtree, при неудаче —
    удаляет файлы по одному (с переименованием залоченных).

    Возвращает True, если папка удалена полностью.
    """
    if not os.path.exists(folder):
        return True

    # Быстрый путь: rmtree.
    try:
        shutil.rmtree(folder)
        print(f"🗑️  Удалена папка: {folder}")
        return True
    except PermissionError:
        pass
    except Exception as e:
        print(f"⚠️  Не удалось удалить {folder}: {e}")

    # Медленный путь: сначала завершаем процессы, потом удаляем по файлам.
    print(f"⚠️  Папка {folder} занята, освобождаю...")
    _ensure_no_running_instance()
    time.sleep(0.5)

    try:
        shutil.rmtree(folder)
        print(f"🗑️  Удалена папка: {folder}")
        return True
    except PermissionError:
        pass
    except Exception as e:
        print(f"⚠️  Повторное удаление {folder} не удалось: {e}")

    # Удаляем по файлам.
    locked_files = []
    for root, dirs, files in os.walk(folder, topdown=False):
        for name in files:
            _try_remove_file(os.path.join(root, name), locked_files)
        for name in dirs:
            d = os.path.join(root, name)
            try:
                os.rmdir(d)
            except OSError:
                pass

    # Последняя попытка удалить корень.
    try:
        os.rmdir(folder)
        print(f"🗑️  Удалена папка: {folder}")
        return True
    except OSError:
        pass

    if locked_files:
        print(f"❌ Не удалось удалить {len(locked_files)} файл(ов) "
              f"в {folder}:")
        for f in locked_files[:20]:
            print(f"   • {f}")
        if len(locked_files) > 20:
            print(f"   ... и ещё {len(locked_files) - 20}")
        print("   Закройте процессы, которые держат эти файлы "
              "(например, adb.exe из dist), и повторите сборку.")
        return False

    # Файлы удалились, но корень остался (например, .locked_* файлы).
    if os.path.exists(folder):
        try:
            shutil.rmtree(folder, ignore_errors=True)
        except Exception:
            pass

    if os.path.exists(folder):
        print(f"❌ Папка {folder} всё ещё существует.")
        return False

    print(f"🗑️  Удалена папка: {folder}")
    return True


def clean_old_build():
    if not CLEAN_BEFORE_BUILD:
        return
    for folder in [OUTPUT_DIR, BUILD_DIR]:
        if os.path.exists(folder):
            if not _safe_rmtree(folder):
                sys.exit(1)


def _find_apk() -> str:
    """Ищет APK рядом с build_exe.py."""
    root = os.path.dirname(os.path.abspath(__file__))
    for name in APK_CANDIDATES:
        p = os.path.join(root, name)
        if os.path.isfile(p):
            return p
    return ""


def build_exe():
    print("=" * 60)
    print(f"🚀 СБОРКА {APP_NAME}")
    print("=" * 60)

    _ensure_no_running_instance()

    args = [
        MAIN_SCRIPT,
        f'--name={APP_NAME}',
        '--windowed',
        '--noconsole',
        '--noconfirm',
        f'--distpath={OUTPUT_DIR}',
        f'--workpath={BUILD_DIR}',
        '--paths=.',
    ]

    for module in EXCLUDED_MODULES:
        args.append(f'--exclude-module={module}')

    if USE_UPX:
        upx_found = shutil.which(UPX_PATH)
        if upx_found:
            args.append(f'--upx-dir={os.path.dirname(upx_found)}')
            print(f"✅ UPX найден: {upx_found}")
        else:
            print("⚠️  UPX не найден, сжатие будет стандартным")

    args.extend([
        # --- Windows API ---
        '--hidden-import=win32gui',
        '--hidden-import=win32api',
        '--hidden-import=win32con',
        '--hidden-import=win32ui',
        '--hidden-import=win32process',
        '--hidden-import=win32file',

        # --- psutil ---
        '--hidden-import=psutil',

        # --- OpenCV / numpy / Pillow ---
        '--hidden-import=cv2',
        '--hidden-import=numpy',
        '--hidden-import=numpy.core',
        '--hidden-import=numpy._core',
        '--hidden-import=PIL',
        '--hidden-import=PIL.Image',
        '--hidden-import=PIL.ImageDraw',
        '--hidden-import=PIL.ImageTk',

        # --- Tkinter ---
        '--hidden-import=tkinter',
        '--hidden-import=tkinter.ttk',
        '--hidden-import=tkinter.scrolledtext',
        '--hidden-import=tkinter.messagebox',
        '--hidden-import=tkinter.filedialog',

        # --- HTTP-сервер ---
        '--hidden-import=http',
        '--hidden-import=http.server',
        '--hidden-import=http.client',
        '--hidden-import=socketserver',
        '--hidden-import=socket',
        '--hidden-import=ssl',
        '--hidden-import=select',

        # --- Стандартные ---
        '--hidden-import=ctypes',
        '--hidden-import=ctypes.wintypes',
        '--hidden-import=queue',
        '--hidden-import=threading',
        '--hidden-import=json',
        '--hidden-import=re',
        '--hidden-import=hashlib',
        '--hidden-import=tempfile',
        '--hidden-import=time',
        '--hidden-import=shutil',
        '--hidden-import=subprocess',
        '--hidden-import=configparser',
        '--hidden-import=logging',
        '--hidden-import=pathlib',
        '--hidden-import=datetime',
        '--hidden-import=collections',
        '--hidden-import=itertools',
        '--hidden-import=functools',
        '--hidden-import=typing',
        '--hidden-import=dataclasses',
        '--hidden-import=enum',
        '--hidden-import=abc',
        '--hidden-import=contextlib',
        '--hidden-import=weakref',
        '--hidden-import=struct',
        '--hidden-import=math',
        '--hidden-import=random',
        '--hidden-import=binascii',
        '--hidden-import=zlib',
        '--hidden-import=gzip',
        '--hidden-import=bz2',
        '--hidden-import=lzma',
        '--hidden-import=xml',
        '--hidden-import=xml.etree',
        '--hidden-import=xml.etree.ElementTree',
        '--hidden-import=html',
        '--hidden-import=html.parser',
        '--hidden-import=urllib',
        '--hidden-import=urllib.parse',
        '--hidden-import=urllib.request',

        # --- Пакет src/ ---
        '--hidden-import=src',
        '--hidden-import=src.config',
        '--hidden-import=src.debug_logger',
        '--hidden-import=src.adb_utils',
        '--hidden-import=src.pc_streamer',
        '--hidden-import=src.ocr_receiver',
        '--hidden-import=src.ocr_overlay',
        '--hidden-import=src.simple_gui',
        '--hidden-import=src.gui_settings_window',
        '--hidden-import=src.gui_help_windows',
        '--hidden-import=src.window_utils',
        '--hidden-import=src.translate_server',
        '--hidden-import=src.settings_manager',
        '--hidden-import=src.settings_server',

        # --- HTTP-перевод (CTranslate2 + SentencePiece) ---
        '--hidden-import=ctranslate2',
        '--hidden-import=sentencepiece',
    ])

    args.extend([
        '--collect-all=cv2',
        '--collect-all=numpy',
        '--collect-all=PIL',
        '--collect-all=psutil',
        '--collect-submodules=src',
        '--collect-data=src',
        '--collect-all=ctranslate2',
        '--collect-all=sentencepiece',
    ])

    models_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
    if os.path.isdir(models_dir):
        args.append(f'--add-data={models_dir};models')
        print(f"✅ Модель добавлена: {models_dir}")
    else:
        print("⚠️  Папка models/ не найдена — HTTP-перевод не будет работать в exe")

    apk_path = _find_apk()
    if apk_path:
        args.append(f'--add-data={apk_path};.')
        print(f"✅ APK добавлен в сборку: {os.path.basename(apk_path)}")
    else:
        print("⚠️  APK не найден рядом с build_exe.py — "
              "установка приложения из exe не сработает. "
              "Положите app-debug.apk рядом с build_exe.py.")

    if os.path.exists(APP_ICON):
        args.append(f'--icon={APP_ICON}')
        print(f"✅ Иконка: {APP_ICON}")

    cv2_excludes = [
        'cv2.ml', 'cv2.ocl', 'cv2.optflow', 'cv2.plot', 'cv2.quality',
        'cv2.saliency', 'cv2.sfm', 'cv2.stereo', 'cv2.superres',
        'cv2.text', 'cv2.tracking', 'cv2.videostab', 'cv2.xfeatures2d',
        'cv2.ximgproc', 'cv2.xphoto', 'cv2.aruco', 'cv2.bgsegm',
        'cv2.bioinspired', 'cv2.ccm', 'cv2.cuda', 'cv2.dnn_superres',
        'cv2.dpm', 'cv2.face', 'cv2.fisheye', 'cv2.flann', 'cv2.gapi',
    ]
    for module in cv2_excludes:
        args.append(f'--exclude-module={module}')

    args.append('--strip')
    args.append('--log-level=ERROR')

    print("\n⏳ Начинается сборка. Это может занять 3–10 минут.")
    print("=" * 60 + "\n")

    try:
        PyInstaller.__main__.run(args)
        print("\n" + "=" * 60)
        print("✅ СБОРКА ЗАВЕРШЕНА УСПЕШНО!")
        print(f"📁 Готовый .exe: {OUTPUT_DIR}\\{APP_NAME}\\{APP_NAME}.exe")
        print("=" * 60)
    except Exception as e:
        print(f"\n❌ ОШИБКА СБОРКИ: {e}")
        sys.exit(1)


def copy_external_tools():
    """Копирует adb.exe и APK рядом с exe (страховка)."""
    output_app_dir = f"{OUTPUT_DIR}/{APP_NAME}"
    if not os.path.exists(output_app_dir):
        return

    print("\n📦 Копирование внешних утилит...")

    adb_src = shutil.which("adb.exe")
    if not adb_src and os.path.exists("adb.exe"):
        adb_src = "adb.exe"

    if adb_src:
        dst = os.path.join(output_app_dir, "adb.exe")
        if not _check_write_access(dst):
            print(f"   ⚠️  {dst} занят, освобождаю...")
            _ensure_no_running_instance()
            time.sleep(0.5)
        if not _check_write_access(dst):
            print(f"   ❌ Нет доступа к {dst}. "
                  f"Закройте приложение и повторите сборку.")
            sys.exit(1)
        try:
            shutil.copy2(adb_src, dst)
            print(f"   ✅ adb.exe ← {adb_src}")
        except Exception as e:
            print(f"   ❌ Не удалось скопировать adb.exe: {e}")
            sys.exit(1)
    else:
        print("   ⚠️  adb.exe не найден (ни рядом, ни в PATH).")

    apk_src = _find_apk()
    if apk_src:
        dst = os.path.join(output_app_dir, os.path.basename(apk_src))
        if not os.path.exists(dst):
            if not _check_write_access(dst):
                print(f"   ⚠️  {dst} занят, освобождаю...")
                _ensure_no_running_instance()
                time.sleep(0.5)
            if not _check_write_access(dst):
                print(f"   ❌ Нет доступа к {dst}.")
                sys.exit(1)
            try:
                shutil.copy2(apk_src, dst)
                print(f"   ✅ {os.path.basename(apk_src)} ← {apk_src}")
            except Exception as e:
                print(f"   ❌ Не удалось скопировать APK: {e}")
                sys.exit(1)
        else:
            print(f"   ✅ APK уже на месте: {os.path.basename(apk_src)}")
    else:
        print("   ⚠️  APK не найден — установка приложения из exe не сработает.")


def show_size_info():
    app_dir = os.path.join(OUTPUT_DIR, APP_NAME)
    exe_path = os.path.join(app_dir, f"{APP_NAME}.exe")

    if os.path.exists(exe_path):
        size_mb = os.path.getsize(exe_path) / (1024 * 1024)
        print(f"\n📊 Размер .exe: {size_mb:.2f} MB")

        if os.path.exists(app_dir):
            total_size = 0
            file_count = 0
            for root, dirs, files in os.walk(app_dir):
                for f in files:
                    total_size += os.path.getsize(os.path.join(root, f))
                    file_count += 1
            total_mb = total_size / (1024 * 1024)
            print(f"📊 Общий размер папки: {total_mb:.2f} MB ({file_count} файлов)")


if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════════╗
║   🛠️  СБОРЩИК TRANSLATE VIA PHONE                       ║
║   Захват выбранного окна ПК → трансляция на телефон     ║
╚══════════════════════════════════════════════════════════╝
""")

    if not os.path.exists(MAIN_SCRIPT):
        print(f"❌ Не найден {MAIN_SCRIPT}")
        sys.exit(1)

    if not os.path.isdir("src"):
        print("❌ Не найдена папка src/")
        sys.exit(1)

    _ensure_no_running_instance()

    clean_old_build()
    build_exe()
    copy_external_tools()
    show_size_info()

    print("\n" + "=" * 60)
    print("🎉 ГОТОВО!")
    print(f"   • Запуск: {OUTPUT_DIR}\\{APP_NAME}\\{APP_NAME}.exe")
    print("=" * 60)