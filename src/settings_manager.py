# src/settings_manager.py
"""Менеджер настроек: источник истины — settings.json рядом с main.py.

Потокобезопасен. Используется:
  - simple_gui.py — для отображения и изменения настроек;
  - settings_server.py — для отдачи/приёма настроек от Android.

Android при старте делает GET /settings и применяет.
При изменении настроек на Android — POST /settings, менеджер сохраняет.
При изменении настроек в GUI — менеджер сохраняет и (опционально)
отправляет на Android через ADB broadcast (см. adb_utils).

ИЗМЕНЕНО:
  - Версионирование схемы settings.json. Поле "_schema_version"
    пишется в файл в save() и НЕ отдаётся наружу через all()/to_json().
  - В load(): если версия в файле не совпадает с текущей
    SETTINGS_SCHEMA_VERSION — выполняется миграция: берутся дефолты
    из DEFAULT_SETTINGS, переносятся только ключи из
    SETTINGS_MIGRATION_PRESERVE_KEYS (например, last_installed_apk_hash),
    файл перезаписывается.
  - Это позволяет менять DEFAULT_SETTINGS (например, ocr_padding
    по умолчанию 0 → 11) и гарантированно применять новые дефолты
    на существующих установках БЕЗ ручной правки settings.json.
"""

import json
import os
import threading
from typing import Any, Callable, Dict, List

from src.config import (
    SETTINGS_FILE,
    DEFAULT_SETTINGS,
    SETTINGS_SCHEMA_VERSION,
    SETTINGS_MIGRATION_PRESERVE_KEYS,
    TRANSLATE_PROFILES,
    LANGUAGES,
)

# Ключ версии схемы в JSON. Начинается с "_", чтобы визуально
# отличать метаданные от пользовательских настроек.
_SCHEMA_KEY = "_schema_version"


def _is_valid_value(key: str, value: Any) -> bool:
    """Проверяет валидность значения для ключа."""
    if key == "translate_profile":
        return value in TRANSLATE_PROFILES
    if key == "source_lang":
        return value in LANGUAGES
    if key == "target_lang":
        return value in LANGUAGES and value != "auto"
    if key in ("confidence",):
        return isinstance(value, int) and 0 <= value <= 100
    if key in ("ocr_padding",):
        return isinstance(value, int) and 0 <= value <= 50
    if key in ("overlay_alpha",):
        return isinstance(value, int) and 0 <= value <= 100
    if key in ("debug", "auto_launch_android"):
        return isinstance(value, bool)
    if key == "last_installed_apk_hash":
        return isinstance(value, str)
    return True


class SettingsManager:
    """Хранит настройки, читает/пишет settings.json."""

    def __init__(self, path: str = SETTINGS_FILE):
        self._path = path
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = dict(DEFAULT_SETTINGS)
        self._listeners: List[Callable[[str, Any], None]] = []
        self.load()
        self.save()

    # ------------------------------------------------------------
    # Чтение / запись файла
    # ------------------------------------------------------------

    def load(self) -> None:
        """Загружает settings.json с учётом версии схемы.

        - Файла нет: используем дефолты, версия схемы запишется в save().
        - Файл есть, _schema_version совпадает: обычная загрузка.
        - Файл есть, _schema_version не совпадает или отсутствует:
          миграция — берём DEFAULT_SETTINGS, переносим ключи из
          SETTINGS_MIGRATION_PRESERVE_KEYS, сохраняем.
        """
        with self._lock:
            # Файла нет — дефолты, ничего не мигрируем.
            if not os.path.exists(self._path):
                self._data = dict(DEFAULT_SETTINGS)
                return

            raw: Dict[str, Any] = {}
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    raw = loaded
            except Exception:
                # Файл битый — используем дефолты.
                self._data = dict(DEFAULT_SETTINGS)
                return

            file_version = raw.get(_SCHEMA_KEY, 0)
            try:
                file_version = int(file_version)
            except (TypeError, ValueError):
                file_version = 0

            if file_version != SETTINGS_SCHEMA_VERSION:
                # Миграция схемы: новые дефолты, только preserve-ключи
                # переносятся из старого файла.
                migrated = dict(DEFAULT_SETTINGS)
                for k in SETTINGS_MIGRATION_PRESERVE_KEYS:
                    if k in raw and _is_valid_value(k, raw[k]):
                        migrated[k] = raw[k]
                self._data = migrated
                print(
                    f"[Settings] 🔄 Миграция схемы: "
                    f"v{file_version} → v{SETTINGS_SCHEMA_VERSION}, "
                    f"применены дефолты "
                    f"({', '.join(k for k in DEFAULT_SETTINGS)})"
                )
                # save() запишет файл с новым _schema_version.
                self.save()
                return

            # Версия совпадает — обычная загрузка.
            data = dict(DEFAULT_SETTINGS)
            for k, v in raw.items():
                if k == _SCHEMA_KEY:
                    continue
                if k not in DEFAULT_SETTINGS:
                    continue
                if _is_valid_value(k, v):
                    data[k] = v
            self._data = data

    def save(self) -> None:
        """Сохраняет текущие настройки в settings.json.

        Всегда добавляет "_schema_version" с текущей версией схемы.
        """
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self._path), exist_ok=True)
                obj = dict(self._data)
                obj[_SCHEMA_KEY] = SETTINGS_SCHEMA_VERSION
                with open(self._path, "w", encoding="utf-8") as f:
                    json.dump(obj, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    # ------------------------------------------------------------
    # Доступ к значениям
    # ------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any, notify: bool = True) -> bool:
        """Устанавливает значение, сохраняет, уведомляет слушателей.

        Возвращает True, если значение реально изменилось.
        """
        with self._lock:
            if key not in DEFAULT_SETTINGS:
                return False
            if not _is_valid_value(key, value):
                return False
            if self._data.get(key) == value:
                return False
            self._data[key] = value
            self.save()
        if notify:
            self._notify(key, value)
        return True

    def update(self, values: Dict[str, Any], notify: bool = True) -> List[str]:
        """Обновляет сразу несколько ключей, сохраняет один раз.

        Возвращает список реально изменённых ключей.
        """
        changed: List[str] = []
        with self._lock:
            for k, v in values.items():
                if k not in DEFAULT_SETTINGS:
                    continue
                if not _is_valid_value(k, v):
                    continue
                if self._data.get(k) != v:
                    self._data[k] = v
                    changed.append(k)
            if changed:
                self.save()
        if notify:
            for k in changed:
                self._notify(k, self._data.get(k))
        return changed

    def all(self) -> Dict[str, Any]:
        """Возвращает пользовательские настройки (без _schema_version)."""
        with self._lock:
            return dict(self._data)

    def to_json(self) -> str:
        """JSON без _schema_version — для отправки на Android."""
        with self._lock:
            return json.dumps(self._data, ensure_ascii=False)

    def from_json(self, text: str) -> None:
        """Применяет настройки из JSON-строки (от Android).

        Поле _schema_version игнорируется.
        """
        try:
            obj = json.loads(text)
        except Exception:
            return
        if not isinstance(obj, dict):
            return
        # _schema_version не даём менять снаружи.
        obj.pop(_SCHEMA_KEY, None)
        self.update(obj)

    def reset_to_defaults(self) -> List[str]:
        """Сбрасывает все настройки к DEFAULT_SETTINGS.

        Возвращает список изменённых ключей.
        """
        return self.update(dict(DEFAULT_SETTINGS))

    # ------------------------------------------------------------
    # Слушатели (GUI подписывается, чтобы обновлять контролы)
    # ------------------------------------------------------------

    def add_listener(self, cb: Callable[[str, Any], None]) -> None:
        with self._lock:
            if cb not in self._listeners:
                self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[str, Any], None]) -> None:
        with self._lock:
            try:
                self._listeners.remove(cb)
            except ValueError:
                pass

    def _notify(self, key: str, value: Any) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(key, value)
            except Exception:
                pass


# Синглтон
settings_manager = SettingsManager()