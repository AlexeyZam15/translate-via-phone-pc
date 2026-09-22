# src/adb_utils.py
"""Утилиты для работы с ADB."""

import shutil
import socket
import subprocess
import sys

CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
ADB_TIMEOUT = 15  # секунд


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