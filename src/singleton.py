# src/singleton.py
"""Завершение других экземпляров приложения при старте нового.

Используется в main.py в самом начале __main__ — ДО открытия debug.log
и ДО создания tk.Tk(), чтобы:

  - не было нескольких одновременно открытых окон/оверлеев;
  - не было конфликта на портах 5005–5009;
  - не обрезался debug.log вторым процессом (он открывается в режиме "w").

Определение «своих» процессов:

  * frozen (.exe):  proc.exe() == sys.executable
                    (полный путь к translate_via_phone.exe)

  * скрипт (python): proc.exe() == sys.executable (python.exe / pythonw.exe)
                     И в proc.cmdline() есть абсолютный путь к текущему main.py

Текущий процесс (os.getpid()) всегда исключается.

Порядок действий для каждого найденного процесса:
  1) terminate() — мягкое завершение;
  2) подождать до KILL_TIMEOUT секунд;
  3) если ещё живой — kill() — жёсткое завершение.

ИЗМЕНЕНО:
  - _ensure_psutil(): автоматическая установка psutil через pip
    при старте, если он не установлен. В frozen-режиме не пытается
    (там psutil уже включён в сборку).
  - _kill_without_psutil() (ранее _kill_via_taskkill): fallback без psutil
    через wmic process get ProcessId,Name,CommandLine /format:csv,
    а если wmic недоступен — через powershell Get-CimInstance Win32_Process.
    Теперь РЕАЛЬНО ищет и frozen exe, и python.exe с main.py в cmdline
    (старая версия для скриптов ничего не делала).
"""

import csv
import importlib
import io
import os
import subprocess
import sys
import time

# psutil подгружается лениво, после _ensure_psutil().
psutil = None

# Сколько секунд ждать после terminate() до kill() для каждого процесса.
KILL_TIMEOUT = 1.5

# Сколько секунд суммарно ждать, пока все умрут.
TOTAL_WAIT = 3.0

# Сколько секунд ждать установку psutil через pip.
PIP_TIMEOUT = 60.0

# Имя текущего main.py (для поиска в cmdline).
_MAIN_SCRIPT_NAME = "main.py"


def _get_app_dir() -> str:
    """Папка приложения (exe или корень проекта)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # src/singleton.py → на два уровня вверх = корень проекта
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_signature():
    """Возвращает (exe_path, main_script_path, is_frozen)."""
    is_frozen = bool(getattr(sys, "frozen", False))
    exe_path = os.path.abspath(sys.executable)
    if is_frozen:
        main_script_path = None
    else:
        main_script_path = os.path.abspath(
            os.path.join(_get_app_dir(), _MAIN_SCRIPT_NAME)
        )
    return exe_path, main_script_path, is_frozen


# ============================================================
# Установка psutil
# ============================================================

def _try_pip_install(args, timeout=PIP_TIMEOUT):
    """Запускает pip с аргументами. Возвращает True при rc == 0."""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) \
        if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip"] + args,
            capture_output=True, text=True, timeout=timeout,
            creationflags=creationflags,
        )
    except FileNotFoundError:
        return False
    except subprocess.TimeoutExpired:
        print(f"[Singleton] ⚠️ pip {' '.join(args)} — таймаут "
              f"{timeout:.0f} с")
        return False
    except Exception as e:
        print(f"[Singleton] ⚠️ Ошибка запуска pip: {e}")
        return False

    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        short = err[-1] if err else f"rc={proc.returncode}"
        print(f"[Singleton] ⚠️ pip {' '.join(args)} → {short}")
        return False
    return True


def _try_import_psutil():
    """Пробует импортировать psutil. Возвращает модуль или None."""
    try:
        importlib.invalidate_caches()
    except Exception:
        pass
    try:
        return importlib.import_module("psutil")
    except Exception:
        return None


def _ensure_psutil() -> bool:
    """Гарантирует доступность psutil.

    Порядок:
      1) уже импортируется → True.
      2) frozen (.exe) → не пытаемся ставить (нет pip / менеджера пакетов),
         возвращаем False.
      3) pip install --user psutil.
      4) pip install psutil.
      5) повторный import.

    Возвращает True, если psutil доступен.
    """
    global psutil

    # 1) Уже есть?
    mod = _try_import_psutil()
    if mod is not None:
        psutil = mod
        return True

    # 2) Frozen — не пытаемся.
    if getattr(sys, "frozen", False):
        print("[Singleton] ⚠️ psutil не установлен (frozen-режим, "
              "установка невозможна)")
        return False

    print("[Singleton] ⚠️ psutil не установлен, пробую установить…")

    # 3) --user
    if _try_pip_install(["install", "--user", "psutil"]):
        mod = _try_import_psutil()
        if mod is not None:
            psutil = mod
            print(f"[Singleton] ✅ psutil установлен "
                  f"(--user): {getattr(mod, '__version__', '?')}")
            return True

    # 4) Обычная установка (может потребовать прав).
    if _try_pip_install(["install", "psutil"]):
        mod = _try_import_psutil()
        if mod is not None:
            psutil = mod
            print(f"[Singleton] ✅ psutil установлен: "
                  f"{getattr(mod, '__version__', '?')}")
            return True

    print("[Singleton] ❌ Не удалось установить psutil — "
          "использую fallback (wmic/powershell)")
    return False


# ============================================================
# Общие утилиты
# ============================================================

def _cmdline_has_main_script(cmdline, main_script_path: str) -> bool:
    """Проверяет, есть ли в cmdline абсолютный путь к нашему main.py."""
    if not cmdline or not main_script_path:
        return False
    target_norm = os.path.normcase(os.path.normpath(main_script_path))
    for arg in cmdline:
        try:
            arg_norm = os.path.normcase(
                os.path.normpath(os.path.abspath(arg))
            )
        except Exception:
            continue
        if arg_norm == target_norm:
            return True
    return False


def _is_our_process(proc, sig) -> bool:
    """Определяет, является ли процесс другим экземпляром нашего приложения."""
    exe_path, main_script_path, is_frozen = sig

    try:
        if proc.pid == os.getpid():
            return False
    except Exception:
        return False

    try:
        proc_exe = proc.exe()
    except Exception:
        return False

    if not proc_exe:
        return False

    try:
        proc_exe_norm = os.path.normcase(os.path.normpath(proc_exe))
        exe_norm = os.path.normcase(os.path.normpath(exe_path))
    except Exception:
        return False

    if proc_exe_norm != exe_norm:
        return False

    if is_frozen:
        return True

    try:
        cmdline = proc.cmdline()
    except Exception:
        return False

    return _cmdline_has_main_script(cmdline, main_script_path)


def _kill_process(proc) -> bool:
    """terminate() → ожидание → kill() при необходимости."""
    pid = None
    try:
        pid = proc.pid
    except Exception:
        pass

    try:
        proc.terminate()
    except Exception:
        pass

    try:
        proc.wait(timeout=KILL_TIMEOUT)
        return True
    except Exception:
        pass

    try:
        proc.kill()
    except Exception:
        pass

    try:
        proc.wait(timeout=KILL_TIMEOUT)
        return True
    except Exception:
        pass

    if pid is not None:
        print(f"[Singleton] ⚠️ Не удалось завершить процесс PID={pid}")
    return False


# ============================================================
# Fallback без psutil: wmic / powershell
# ============================================================

def _run_silent(args, timeout=15):
    """subprocess.run с подавлением окна на Windows."""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) \
        if sys.platform == "win32" else 0
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            creationflags=creationflags,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None


def _query_wmic() -> list:
    """Возвращает список dict'ов с ключами Name, ProcessId, CommandLine.

    Использует: wmic process get ProcessId,Name,CommandLine /format:csv
    Возвращает [] при любой ошибке или если wmic недоступен.
    """
    res = _run_silent(
        ["wmic", "process", "get",
         "ProcessId,Name,CommandLine", "/format:csv"],
        timeout=20,
    )
    if res is None or res.returncode != 0 or not res.stdout:
        return []

    rows = []
    reader = csv.reader(io.StringIO(res.stdout))
    header = None
    for row in reader:
        if not row:
            continue
        # wmic вставляет пустые строки и строку заголовка "Node,CommandLine,Name,ProcessId"
        if header is None:
            header = [c.strip() for c in row]
            continue
        if len(row) != len(header):
            continue
        try:
            rec = dict(zip(header, row))
        except Exception:
            continue
        name = (rec.get("Name") or "").strip()
        pid_str = (rec.get("ProcessId") or "").strip()
        cmdline = (rec.get("CommandLine") or "").strip()
        if not name or not pid_str:
            continue
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        rows.append({"Name": name, "ProcessId": pid, "CommandLine": cmdline})
    return rows


def _query_powershell() -> list:
    """Fallback для Windows 11+, где wmic удалён.

    Использует:
      powershell -NoProfile -Command
        "Get-CimInstance Win32_Process |
         Select-Object ProcessId,Name,CommandLine |
         ConvertTo-Csv -NoTypeInformation"

    Возвращает [] при ошибке.
    """
    ps_cmd = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,Name,CommandLine | "
        "ConvertTo-Csv -NoTypeInformation"
    )
    res = _run_silent(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
        timeout=25,
    )
    if res is None or res.returncode != 0 or not res.stdout:
        return []

    rows = []
    reader = csv.reader(io.StringIO(res.stdout))
    header = None
    for row in reader:
        if not row:
            continue
        if header is None:
            header = [c.strip().strip('"') for c in row]
            continue
        if len(row) != len(header):
            continue
        try:
            rec = {k: v for k, v in zip(header, row)}
        except Exception:
            continue
        name = (rec.get("Name") or "").strip()
        pid_str = (rec.get("ProcessId") or "").strip()
        cmdline = (rec.get("CommandLine") or "").strip()
        if not name or not pid_str:
            continue
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        rows.append({"Name": name, "ProcessId": pid, "CommandLine": cmdline})
    return rows


def _matches_target(rec: dict, sig) -> bool:
    """Проверяет, является ли запись из wmic/powershell нашим процессом."""
    exe_path, main_script_path, is_frozen = sig
    our_pid = os.getpid()

    pid = rec.get("ProcessId")
    if pid is None or pid == our_pid:
        return False

    name = (rec.get("Name") or "").strip()
    if not name:
        return False

    if is_frozen:
        exe_basename = os.path.basename(exe_path)
        return name.lower() == exe_basename.lower()

    # Скрипт: ищем python.exe / pythonw.exe с нашим main.py в cmdline.
    name_lower = name.lower()
    if name_lower not in ("python.exe", "pythonw.exe"):
        return False

    cmdline = (rec.get("CommandLine") or "").strip()
    if not cmdline:
        return False

    if main_script_path:
        # 1) Пробуем найти точный абсолютный путь.
        try:
            tokens = _split_cmdline(cmdline)
        except Exception:
            tokens = []
        for tok in tokens:
            try:
                tok_norm = os.path.normcase(
                    os.path.normpath(os.path.abspath(tok))
                )
                target_norm = os.path.normcase(
                    os.path.normpath(main_script_path)
                )
                if tok_norm == target_norm:
                    return True
            except Exception:
                continue

        # 2) Фолбэк: проверяем, что в cmdline есть имя "main.py"
        #    и в нём встречается путь к каталогу проекта.
        if _MAIN_SCRIPT_NAME.lower() in cmdline.lower():
            app_dir_norm = os.path.normcase(
                os.path.normpath(_get_app_dir())
            )
            if app_dir_norm in os.path.normcase(cmdline):
                return True

    return False


def _split_cmdline(cmdline: str) -> list:
    """Разбивает командную строку на токены, учитывая кавычки Windows."""
    tokens = []
    cur = []
    in_quotes = False
    for c in cmdline:
        if c == '"':
            in_quotes = not in_quotes
            continue
        if not in_quotes and c in (" ", "\t"):
            if cur:
                tokens.append("".join(cur))
                cur = []
            continue
        cur.append(c)
    if cur:
        tokens.append("".join(cur))
    return tokens


def _taskkill(pid: int) -> bool:
    res = _run_silent(["taskkill", "/F", "/PID", str(pid)], timeout=10)
    return res is not None and res.returncode == 0


def _kill_without_psutil() -> int:
    """Fallback без psutil: wmic, затем powershell Get-CimInstance.

    Ищет и frozen exe, и python.exe с нашим main.py в CommandLine.
    Возвращает число убитых процессов.
    """
    if sys.platform != "win32":
        return 0

    sig = _get_signature()

    rows = _query_wmic()
    source = "wmic"
    if not rows:
        rows = _query_powershell()
        source = "powershell"

    if not rows:
        print("[Singleton] ⚠️ Ни wmic, ни powershell недоступны — "
              "старые экземпляры не будут закрыты")
        print("[Singleton]    Установите psutil вручную: "
              "pip install psutil")
        return 0

    targets = []
    for rec in rows:
        try:
            if _matches_target(rec, sig):
                targets.append(rec)
        except Exception:
            continue

    if not targets:
        return 0

    print(f"[Singleton] 🔄 Найдено других экземпляров "
          f"({source}): {len(targets)}")
    killed = 0
    for rec in targets:
        pid = rec.get("ProcessId")
        if _taskkill(pid):
            print(f"[Singleton] ✅ Завершён процесс PID={pid} "
                  f"({rec.get('Name', '?')})")
            killed += 1
        else:
            print(f"[Singleton] ⚠️ Не удалось завершить PID={pid}")

    if killed:
        # Дать ОС время освободить порты/файлы.
        time.sleep(0.5)

    return killed


# ============================================================
# Основная функция
# ============================================================

def kill_other_instances() -> int:
    """Убивает все другие экземпляры приложения. Возвращает число убитых.

    Сначала пытается использовать psutil (при необходимости ставит его
    через pip). Если psutil недоступен — fallback на wmic/powershell.
    """
    if _ensure_psutil():
        return _kill_with_psutil()
    return _kill_without_psutil()


def _kill_with_psutil() -> int:
    """Основной путь: psutil.process_iter."""
    sig = _get_signature()
    victims = []

    try:
        for proc in psutil.process_iter(["pid", "exe", "cmdline"]):
            try:
                if _is_our_process(proc, sig):
                    victims.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:
                continue
    except Exception as e:
        print(f"[Singleton] ⚠️ Ошибка перечисления процессов: {e}")
        return 0

    if not victims:
        return 0

    print(f"[Singleton] 🔄 Найдено других экземпляров: {len(victims)}")
    killed = 0
    for proc in victims:
        try:
            pid = proc.pid
        except Exception:
            pid = "?"
        if _kill_process(proc):
            print(f"[Singleton] ✅ Завершён процесс PID={pid}")
            killed += 1

    # Дать ОС время освободить порты/файлы.
    deadline = time.time() + TOTAL_WAIT
    while time.time() < deadline:
        try:
            still_alive = [p for p in victims if p.is_running()]
        except Exception:
            still_alive = []
        if not still_alive:
            break
        time.sleep(0.1)

    if killed:
        print(f"[Singleton] ✅ Завершено экземпляров: {killed}")
    else:
        print("[Singleton] ⚠️ Не удалось завершить ни один экземпляр")

    return killed