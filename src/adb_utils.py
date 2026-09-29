# src/adb_utils.py
"""Утилиты для работы с ADB."""

import glob
import hashlib
import os
import re
import shutil
import socket
import subprocess
import sys

CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
ADB_TIMEOUT = 15  # секунд

ANDROID_PACKAGE = "com.example.screentranslator"
ANDROID_MAIN_ACTIVITY = ".MainActivity"
ANDROID_PC_OCR_SERVICE = ".PcOcrService"

# Кандидаты имён APK — ищем рядом с приложением
ANDROID_APK_NAME = "app-debug.apk"
ANDROID_APK_CANDIDATES = [
    "app-debug.apk",
    "app-release.apk",
    "ScreenTranslator.apk",
    "screentranslator.apk",
]

_aapt_cache = None  # кэш пути к aapt


def _get_app_dir() -> str:
    """Папка приложения (exe или корень проекта)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # src/adb_utils.py → на два уровня вверх = корень проекта
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check_adb_available() -> bool:
    """Проверяет, доступен ли adb в PATH."""
    return shutil.which("adb") is not None or shutil.which("adb.exe") is not None


def _run_adb(args, timeout=ADB_TIMEOUT):
    """
    Запускает adb с указанными аргументами.
    Возвращает (returncode, stdout, stderr) или None при ошибке/таймауте.
    """
    try:
        proc = subprocess.Popen(
            ["adb"] + args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        print("❌ adb.exe не найден в PATH. Установите Android Platform Tools "
              "и добавьте platform-tools в PATH.")
        return None
    except Exception as e:
        print(f"⚠️ Не удалось запустить adb: {e}")
        return None

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        print(f"⚠️ adb {' '.join(args)} timed out after {timeout} seconds")
        try:
            proc.kill()
            proc.communicate(timeout=2)
        except Exception:
            pass
        return None
    except Exception as e:
        print(f"⚠️ Ошибка при вызове adb {' '.join(args)}: {e}")
        try:
            proc.kill()
        except Exception:
            pass
        return None


def _run_adb_bytes(args, timeout=ADB_TIMEOUT):
    """
    Запускает adb и возвращает (returncode, stdout_bytes, stderr_bytes).
    Для команд, где нужен бинарный вывод (pull и т.п.).
    """
    try:
        proc = subprocess.Popen(
            ["adb"] + args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        return None
    except Exception:
        return None

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.communicate(timeout=2)
        except Exception:
            pass
        return None
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return None


def scan_devices():
    """Возвращает список ADB-устройств (USB первыми, потом Wi-Fi)."""
    usb_devices = []
    wifi_devices = []

    result = _run_adb(["devices"])
    if result is None:
        return []

    returncode, stdout, stderr = result
    if returncode != 0:
        print(f"⚠️ adb devices вернул код {returncode}: {stderr.strip()}")
        return []

    lines = stdout.strip().split('\n')
    for line in lines[1:]:
        if line.strip() and '\tdevice' in line:
            serial = line.split('\t')[0].strip()
            if not serial:
                continue
            if ':' in serial:
                wifi_devices.append(serial)
            else:
                usb_devices.append(serial)

    return usb_devices + wifi_devices


def check_device_present(serial: str) -> bool:
    """Быстрая проверка, что конкретное устройство всё ещё подключено."""
    if not serial:
        return False
    return serial in scan_devices()


def get_local_ip():
    """Возвращает локальный IP-адрес ПК."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ============================================================
# APK: поиск
# ============================================================

def get_apk_path():
    """
    Ищет APK-файл рядом с приложением.

    Порядок поиска:
      1) корень приложения (APP_DIR / папка exe);
      2) подпапка apk/;
      3) подпапка dist/ (на случай сборки из исходников).

    Возвращает полный путь к APK или None.
    """
    app_dir = _get_app_dir()
    search_dirs = [
        app_dir,
        os.path.join(app_dir, "apk"),
        os.path.join(app_dir, "dist"),
    ]
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for name in ANDROID_APK_CANDIDATES:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return None


# ============================================================
# APK: хэш
# ============================================================

def apk_sha256(apk_path) -> str:
    """
    Считает SHA-256 APK-файла. Возвращает hex-строку.
    При ошибке — пустую строку.
    """
    if not apk_path or not os.path.isfile(apk_path):
        return ""
    try:
        h = hashlib.sha256()
        with open(apk_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        print(f"⚠️ Не удалось посчитать хэш APK: {e}")
        return ""


def get_remote_apk_hash(serial: str,
                        package: str = ANDROID_PACKAGE) -> str:
    """
    Возвращает SHA-256 APK, установленного на устройстве.
    Работает так:
      1) adb shell pm path <package>   → путь к base.apk
      2) adb pull <path> -             → поток в stdout
      3) считаем SHA-256
    При ошибке — пустую строку.
    """
    if not serial:
        return ""

    # 1) Путь к APK на устройстве
    result = _run_adb(["-s", serial, "shell", "pm", "path", package],
                      timeout=10)
    if result is None:
        return ""
    rc, out, err = result
    if rc != 0:
        return ""

    apk_remote_path = ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            apk_remote_path = line[8:].strip()
            break
    if not apk_remote_path:
        return ""

    # 2) adb pull <path> -   → бинарный поток в stdout
    res = _run_adb_bytes(["-s", serial, "pull", apk_remote_path, "-"],
                         timeout=60)
    if res is None:
        return ""
    rc, stdout, stderr = res
    if rc != 0:
        return ""

    try:
        return hashlib.sha256(stdout).hexdigest()
    except Exception:
        return ""


# ============================================================
# APK: установка / удаление
# ============================================================

def install_android_app(serial: str, apk_path: str,
                        reinstall: bool = True,
                        package: str = ANDROID_PACKAGE):
    """
    Устанавливает APK на устройство через adb install.
    Возвращает (ok: bool, message: str).
    """
    if not serial:
        return False, "Устройство не выбрано"
    if not apk_path or not os.path.isfile(apk_path):
        return False, f"APK не найден: {apk_path}"

    args = ["-s", serial, "install"]
    if reinstall:
        args.append("-r")
    args.append(apk_path)

    print(f"[ADB] 📦 Установка APK: {os.path.basename(apk_path)}")
    result = _run_adb(args, timeout=180)
    if result is None:
        return False, "adb install не ответил (таймаут)"

    rc, out, err = result
    combined = (out + "\n" + err).strip()
    ok = (rc == 0) and ("Success" in combined) and ("Failure" not in combined)

    if ok:
        print(f"[ADB] ✅ APK установлен: {os.path.basename(apk_path)}")
        return True, "Success"
    else:
        print(f"[ADB] ⚠️ Ошибка установки APK: rc={rc}, "
              f"out={out.strip()[:300]}, err={err.strip()[:300]}")
        return False, combined[:500]


def uninstall_android_app(serial: str,
                          package: str = ANDROID_PACKAGE):
    """Удаляет пакет с устройства. Возвращает (ok, message)."""
    if not serial:
        return False, "Устройство не выбрано"
    result = _run_adb(["-s", serial, "uninstall", package], timeout=60)
    if result is None:
        return False, "adb uninstall не ответил (таймаут)"
    rc, out, err = result
    combined = (out + "\n" + err).strip()
    ok = (rc == 0) and ("Success" in combined)
    return ok, combined[:500]


def is_package_installed(serial: str,
                         package: str = ANDROID_PACKAGE) -> bool:
    """Проверяет, установлен ли пакет. Возвращает True/False."""
    if not serial:
        return False
    result = _run_adb(
        ["-s", serial, "shell", "pm", "list", "packages", package],
        timeout=10,
    )
    if result is None:
        return False
    rc, out, err = result
    if rc != 0:
        return False
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("package:") and line[8:].strip() == package:
            return True
    return False


def ensure_app_installed(serial: str, apk_path: str,
                         known_hash: str = "",
                         package: str = ANDROID_PACKAGE):
    """
    Версионный контроль по SHA-256 хэшу APK.

    Возвращает (ok: bool, action: str, message: str, new_hash: str).
    action:
      "none"       — приложение уже актуально, ничего не делаем
      "install"    — приложения не было, установили
      "reinstall"  — установили поверх (install -r), данные сохранены
      "update"     — удалили старую и поставили новую (данные потеряны)
      "error"      — ошибка

    Логика:
      1) Нет приложения             → install
      2) Хэш APK == known_hash      → none
      3) Хэш отличается:
         a) install -r              → если ок → reinstall
         b) uninstall + install     → update
      4) Не смогли посчитать хэш    → install -r (best effort)
    """
    if not serial:
        return False, "error", "Устройство не выбрано", ""
    if not apk_path or not os.path.isfile(apk_path):
        return False, "error", f"APK не найден: {apk_path}", ""

    apk_hash = apk_sha256(apk_path)
    if not apk_hash:
        return False, "error", "Не удалось посчитать хэш APK", ""

    installed = is_package_installed(serial, package)

    if not installed:
        print(f"[ADB] ℹ️ Приложение не установлено — ставим APK")
        ok, msg = install_android_app(serial, apk_path, reinstall=False)
        return ok, ("install" if ok else "error"), msg, (apk_hash if ok else "")

    # Установлено — сравниваем хэши
    if known_hash and apk_hash == known_hash:
        print(f"[ADB] ✅ Хэш APK совпадает с сохранённым — "
              f"приложение актуально")
        return True, "none", "APK актуален", apk_hash

    # Хэш неизвестен или отличается.
    # Если known_hash пуст — возможно, это первый запуск после обновления
    # логики; попробуем узнать хэш APK на устройстве.
    if not known_hash:
        remote_hash = get_remote_apk_hash(serial, package)
        if remote_hash and remote_hash == apk_hash:
            print(f"[ADB] ✅ Хэш на устройстве совпадает с APK — "
                  f"запоминаем и работаем")
            return True, "none", "APK актуален", apk_hash

    print(f"[ADB] 🔄 Хэш APK изменился — обновляем приложение")

    # install -r (сохраняет данные)
    ok, msg = install_android_app(serial, apk_path, reinstall=True)
    if ok:
        return True, "reinstall", msg, apk_hash

    # Не вышло — сносим и ставим заново
    print(f"[ADB] ⚠️ install -r не сработал, переустанавливаем с нуля")
    ok_un, msg_un = uninstall_android_app(serial, package)
    if not ok_un:
        return False, "error", f"Не удалось удалить: {msg_un}", ""

    ok, msg = install_android_app(serial, apk_path, reinstall=False)
    return ok, ("update" if ok else "error"), msg, (apk_hash if ok else "")


# ============================================================
# Запуск Android-приложения и сервиса
# ============================================================

def launch_android_app(serial: str,
                       package: str = ANDROID_PACKAGE,
                       activity: str = ANDROID_MAIN_ACTIVITY,
                       auto_start_ocr: bool = False) -> bool:
    """
    Запускает MainActivity Android-приложения.
    Если auto_start_ocr=True — передаёт extra auto_start_ocr=true.
    """
    if not serial:
        return False
    args = ["-s", serial, "shell", "am", "start", "-n",
            f"{package}/{activity}"]
    if auto_start_ocr:
        args += ["--ez", "auto_start_ocr", "true"]

    result = _run_adb(args, timeout=10)
    if result is None:
        return False
    rc, out, err = result
    ok = (rc == 0) and ("Error" not in (out + err)) and ("Exception" not in (out + err))
    if ok:
        print(f"[ADB] ✅ Android-приложение запущено: {package}/{activity} "
              f"(auto_start_ocr={auto_start_ocr})")
    else:
        print(f"[ADB] ⚠️ Не удалось запустить Android-приложение: "
              f"rc={rc}, out={out.strip()[:200]}, err={err.strip()[:200]}")
    return ok


def start_pc_ocr_service(serial: str,
                         package: str = ANDROID_PACKAGE,
                         service: str = ANDROID_PC_OCR_SERVICE,
                         engine: str = "ML_KIT") -> bool:
    """Запускает PcOcrService напрямую."""
    if not serial:
        return False
    args = ["-s", serial, "shell", "am", "start-foreground-service",
            "-n", f"{package}/{service}",
            "--es", "engine", engine]
    result = _run_adb(args, timeout=10)
    if result is None:
        return False
    rc, out, err = result
    ok = (rc == 0) and ("Error" not in (out + err))
    if ok:
        print(f"[ADB] ✅ PcOcrService запущен напрямую (engine={engine})")
    else:
        print(f"[ADB] ⚠️ Не удалось запустить PcOcrService: "
              f"rc={rc}, out={out.strip()[:200]}, err={err.strip()[:200]}")
    return ok


def stop_pc_ocr_service(serial: str,
                        package: str = ANDROID_PACKAGE,
                        service: str = ANDROID_PC_OCR_SERVICE) -> bool:
    """Останавливает PcOcrService."""
    if not serial:
        return False
    args = ["-s", serial, "shell", "am", "stop-service",
            "-n", f"{package}/{service}"]
    result = _run_adb(args, timeout=10)
    if result is None:
        return False
    rc, out, err = result
    if rc == 0:
        print(f"[ADB] ✅ PcOcrService остановлен")
    return rc == 0


def force_stop_android_app(serial: str,
                           package: str = ANDROID_PACKAGE) -> bool:
    """Принудительно останавливает Android-приложение."""
    if not serial:
        return False
    args = ["-s", serial, "shell", "am", "force-stop", package]
    result = _run_adb(args, timeout=10)
    if result is None:
        return False
    rc, out, err = result
    ok = rc == 0
    if ok:
        print(f"[ADB] ✅ Android-приложение остановлено (force-stop)")
    else:
        print(f"[ADB] ⚠️ Не удалось остановить Android-приложение: "
              f"rc={rc}, err={err.strip()[:200]}")
    return ok


def push_settings_to_android(serial: str,
                             settings_json: str,
                             package: str = ANDROID_PACKAGE,
                             silent: bool = True) -> bool:
    """Отправляет настройки на Android через am broadcast."""
    if not serial:
        return False
    args = ["-s", serial, "shell", "am", "broadcast",
            "-a", f"{package}.SETTINGS",
            "--es", "settings_json", settings_json]
    result = _run_adb(args, timeout=10)
    if result is None:
        return False
    rc, out, err = result
    ok = rc == 0
    if not ok:
        print(f"[ADB] ⚠️ Не удалось отправить настройки: rc={rc}, "
              f"err={err.strip()[:200]}")
    elif not silent:
        print(f"[ADB] ✅ Настройки отправлены на Android")
    return ok