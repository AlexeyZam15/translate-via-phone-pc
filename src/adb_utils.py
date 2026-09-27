# src/adb_utils.py
"""Утилиты для работы с ADB."""

import os
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
# APK: поиск / проверка / установка
# ============================================================

def get_apk_path():
    """
    Ищет APK-файл рядом с приложением.

    Порядок поиска:
      1) корень приложения (APP_DIR / папка exe) — сюда пользователь
         кладёт app-debug.apk;
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


def is_package_installed(serial: str,
                         package: str = ANDROID_PACKAGE) -> bool:
    """
    Проверяет, установлен ли пакет на устройстве.
    Возвращает True/False. При ошибке — False.
    """
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
    # pm list packages com.example.foo вернёт "package:com.example.foo"
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("package:") and line[8:].strip() == package:
            return True
    return False


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
    result = _run_adb(args, timeout=180)  # установка может быть долгой
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


# ============================================================
# Запуск Android-приложения и сервиса
# ============================================================

def launch_android_app(serial: str,
                       package: str = ANDROID_PACKAGE,
                       activity: str = ANDROID_MAIN_ACTIVITY,
                       auto_start_ocr: bool = False) -> bool:
    """
    Запускает MainActivity Android-приложения (или выводит на передний план,
    если оно уже запущено).

    Если auto_start_ocr=True — передаёт extra auto_start_ocr=true,
    MainActivity сам запустит PcOcrService.
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
    """Запускает PcOcrService напрямую через am start-foreground-service."""
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
    """Останавливает PcOcrService через am stop-service."""
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
    """
    Принудительно останавливает Android-приложение (включая PcOcrService).
    Используется при остановке трансляции или закрытии ПК-приложения.
    """
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
    """
    Отправляет настройки на Android через am broadcast.
    Android-приложение должно иметь BroadcastReceiver,
    слушающий action=com.example.screentranslator.SETTINGS
    с extra settings_json.
    """
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