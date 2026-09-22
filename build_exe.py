# build_exe.py
"""
Сборка translate_via_phone в .exe (PyInstaller)

Запуск: python build_exe.py
"""

import PyInstaller.__main__
import os
import sys
import shutil
import warnings

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
    'aiohttp', 'requests', 'urllib3',
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


def clean_old_build():
    if CLEAN_BEFORE_BUILD:
        for folder in [OUTPUT_DIR, BUILD_DIR]:
            if os.path.exists(folder):
                try:
                    shutil.rmtree(folder)
                    print(f"🗑️  Удалена папка: {folder}")
                except Exception as e:
                    print(f"⚠️  Не удалось удалить {folder}: {e}")


def build_exe():
    print("=" * 60)
    print(f"🚀 СБОРКА {APP_NAME}")
    print("=" * 60)

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

        # --- psutil (для определения имени процесса) ---
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
        '--hidden-import=src.window_utils',
    ])

    args.extend([
        '--collect-all=cv2',
        '--collect-all=numpy',
        '--collect-all=PIL',
        '--collect-all=psutil',
        '--collect-submodules=src',
        '--collect-data=src',
    ])

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
    output_app_dir = f"{OUTPUT_DIR}/{APP_NAME}"
    if not os.path.exists(output_app_dir):
        return

    print("\n📦 Копирование внешних утилит...")

    adb_src = shutil.which("adb.exe")
    if not adb_src and os.path.exists("adb.exe"):
        adb_src = "adb.exe"

    if adb_src:
        try:
            shutil.copy2(adb_src, os.path.join(output_app_dir, "adb.exe"))
            print(f"   ✅ adb.exe ← {adb_src}")
        except Exception as e:
            print(f"   ⚠️  Не удалось скопировать adb.exe: {e}")
    else:
        print("   ⚠️  adb.exe не найден (ни рядом, ни в PATH).")


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

    clean_old_build()
    build_exe()
    copy_external_tools()
    show_size_info()

    print("\n" + "=" * 60)
    print("🎉 ГОТОВО!")
    print(f"   • Запуск: {OUTPUT_DIR}\\{APP_NAME}\\{APP_NAME}.exe")
    print("=" * 60)