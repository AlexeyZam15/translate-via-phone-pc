# src/window_utils.py
"""Утилиты для перечисления окон Windows."""

import os
import ctypes
import win32gui
import win32api
import win32con
import win32process

try:
    import psutil
except ImportError:
    psutil = None

# Заголовки, которые нужно исключить (наши окна)
EXCLUDED_TITLES = {
    "Simple Phone Translator",
    "Перевод",
    "Окно перевода",
    "Мини-бар",
    "Tk",
}

EXCLUDED_CLASSES = {
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "SysListView32",
    "TaskManagerWindow",
    "MultitaskingViewFrame",
}

# Кэш имён процессов (pid -> name)
_process_name_cache = {}


def get_process_name_by_hwnd(hwnd: int, default: str = "?") -> str:
    """
    Возвращает имя процесса (exe) по HWND.
    Пробует несколько способов:
      1. psutil (если установлен)
      2. QueryFullProcessImageNameW (Win32 API)
      3. GetModuleFileNameEx (Win32 API)
      4. Toolhelp32Snapshot (Win32 API)
      5. Возвращает default
    """
    if not hwnd or not win32gui.IsWindow(hwnd):
        return default

    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid or pid <= 0:
            return default
    except Exception:
        return default

    # Проверяем кэш
    if pid in _process_name_cache:
        return _process_name_cache[pid]

    name = _get_process_name_by_pid(pid)

    # Кэшируем
    if name != default:
        _process_name_cache[pid] = name

    return name


def _get_process_name_by_pid(pid: int) -> str:
    """Возвращает имя процесса по PID, используя несколько способов."""

    # === Способ 1: psutil ===
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            return proc.name()
        except Exception:
            pass

    # === Способ 2: QueryFullProcessImageNameW ===
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        process_handle = win32api.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if process_handle:
            try:
                exe_path = ctypes.create_unicode_buffer(1024)
                size = ctypes.c_uint32(ctypes.sizeof(exe_path))
                if ctypes.windll.kernel32.QueryFullProcessImageNameW(
                        process_handle, 0, exe_path, ctypes.byref(size)
                ):
                    if exe_path.value:
                        return os.path.basename(exe_path.value)
            finally:
                win32api.CloseHandle(process_handle)
    except Exception:
        pass

    # === Способ 3: GetModuleFileNameEx ===
    try:
        PROCESS_QUERY_INFORMATION = 0x0400
        PROCESS_VM_READ = 0x0010
        process_handle = win32api.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid
        )
        if process_handle:
            try:
                exe_path = win32process.GetModuleFileNameEx(process_handle, 0)
                if exe_path:
                    return os.path.basename(exe_path)
            finally:
                win32api.CloseHandle(process_handle)
    except Exception:
        pass

    # === Способ 4: Toolhelp32Snapshot ===
    try:
        TH32CS_SNAPPROCESS = 0x00000002
        snapshot = win32api.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot and snapshot != -1:
            try:
                entry = win32process.Process32First(snapshot)
                while entry:
                    if entry.th32ProcessID == pid:
                        return entry.szExeFile
                    entry = win32process.Process32Next(snapshot)
            finally:
                win32api.CloseHandle(snapshot)
    except Exception:
        pass

    return "?"


def get_active_windows(our_hwnd: int = None):
    """
    Возвращает список видимых окон с заголовками.

    Args:
        our_hwnd: HWND нашего главного окна (исключается из списка).

    Returns:
        list[dict]: [{'hwnd': int, 'title': str, 'process': str, 'width': int, 'height': int}, ...]
    """
    windows = []

    def enum_callback(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if hwnd == our_hwnd:
                return True

            title = win32gui.GetWindowText(hwnd)
            if not title or len(title) < 1:
                return True
            if title in EXCLUDED_TITLES:
                return True

            class_name = win32gui.GetClassName(hwnd)
            if class_name in EXCLUDED_CLASSES:
                return True

            rect = win32gui.GetWindowRect(hwnd)
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]
            if width < 50 or height < 50:
                return True

            process = get_process_name_by_hwnd(hwnd, default="?")

            # Исключаем свои окна по имени процесса
            if process.lower() in ("python.exe", "pythonw.exe"):
                # Но не исключаем, если пользователь хочет захватить python-скрипт
                # (это может быть нужное окно)
                pass

            windows.append({
                'hwnd': hwnd,
                'title': title,
                'process': process,
                'width': width,
                'height': height,
            })
        except Exception:
            pass
        return True

    win32gui.EnumWindows(enum_callback, None)

    windows.sort(key=lambda w: (w['process'].lower(), w['title'].lower()))
    return windows