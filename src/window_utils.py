# src/window_utils.py
"""Утилиты для перечисления окон Windows."""

import os
import ctypes
import win32gui
import win32api
import win32con
import win32process

from src.debug_logger import logger as dbg

try:
    import psutil
except ImportError:
    psutil = None

# Заголовки, которые нужно исключить (наши окна).
# ИЗМЕНЕНО: сравнение теперь по подстроке (см. _check_window),
# поэтому "Translate via Phone (Не отвечает)" тоже отфильтруется.
EXCLUDED_TITLES = {
    "Simple Phone Translator",
    "Translate via Phone",
    "Перевод",
    "Окно перевода",
    "Мини-бар",
    "Tk",
}

# Классы окон, которые нельзя захватывать
EXCLUDED_CLASSES = {
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "SysListView32",
    "TaskManagerWindow",
    "MultitaskingViewFrame",
    "ConsoleWindowClass",
    "PseudoConsoleWindow",
    "CASCADIA_HOSTING_WINDOW_CLASS",
}

# Процессы, окна которых нельзя захватывать
EXCLUDED_PROCESSES = {
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "conhost.exe",
    "windowsterminal.exe",
    "openconsole.exe",
    "wt.exe",
    "python.exe",
    "pythonw.exe",
}

_process_name_cache = {}


def get_process_name_by_hwnd(hwnd: int, default: str = "?") -> str:
    if not hwnd or not win32gui.IsWindow(hwnd):
        return default
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid or pid <= 0:
            return default
    except Exception:
        return default
    if pid in _process_name_cache:
        return _process_name_cache[pid]
    name = _get_process_name_by_pid(pid)
    if name != default:
        _process_name_cache[pid] = name
    return name


def _get_process_name_by_pid(pid: int) -> str:
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            return proc.name()
        except Exception:
            pass
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


def _check_window(hwnd: int, our_hwnds):
    if not hwnd:
        return False, "hwnd=0"
    try:
        if not win32gui.IsWindow(hwnd):
            return False, "not a window"
    except Exception as e:
        return False, f"IsWindow error: {e}"

    if our_hwnds and hwnd in our_hwnds:
        return False, "our own window"

    try:
        if not win32gui.IsWindowVisible(hwnd):
            return False, "not visible"
    except Exception:
        return False, "IsWindowVisible error"

    try:
        if win32gui.IsIconic(hwnd):
            return False, "minimized"
    except Exception:
        return False, "IsIconic error"

    try:
        title = win32gui.GetWindowText(hwnd)
    except Exception:
        return False, "GetWindowText error"

    if not title or len(title) < 1:
        return False, "no title"

    # ИЗМЕНЕНО: сравнение по подстроке (без учёта регистра),
    # чтобы отфильтровать "Translate via Phone (Не отвечает)" и подобные.
    title_lower = title.lower()
    for excluded in EXCLUDED_TITLES:
        if excluded.lower() in title_lower:
            return False, f"excluded title: {title!r}"

    try:
        class_name = win32gui.GetClassName(hwnd)
    except Exception:
        return False, "GetClassName error"

    if class_name in EXCLUDED_CLASSES:
        return False, f"excluded class: {class_name!r}"

    try:
        process = get_process_name_by_hwnd(hwnd, default="?")
        if process.lower() in EXCLUDED_PROCESSES:
            return False, f"excluded process: {process!r}"
    except Exception:
        pass

    try:
        rect = win32gui.GetWindowRect(hwnd)
        width = rect[2] - rect[0]
        height = rect[3] - rect[1]
    except Exception:
        return False, "GetWindowRect error"

    if width < 50 or height < 50:
        return False, f"too small: {width}x{height}"

    return True, ""


def get_foreground_window(our_hwnds=None):
    try:
        hwnd = win32gui.GetForegroundWindow()
    except Exception as e:
        dbg.log_event("pc.foreground_error", {"error": str(e)})
        return None

    if not hwnd:
        return None

    ok, reason = _check_window(hwnd, our_hwnds)
    if not ok:
        try:
            title = win32gui.GetWindowText(hwnd)
        except Exception:
            title = "?"
        dbg.log_event("pc.foreground_rejected", {
            "hwnd": hwnd,
            "title": title,
            "reason": reason,
        })
        return None

    return hwnd


def get_active_windows(our_hwnd: int = None):
    windows = []
    our = [our_hwnd] if our_hwnd else None

    def enum_callback(hwnd, _):
        try:
            ok, _reason = _check_window(hwnd, our)
            if not ok:
                return True
            title = win32gui.GetWindowText(hwnd)
            rect = win32gui.GetWindowRect(hwnd)
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]
            process = get_process_name_by_hwnd(hwnd, default="?")
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