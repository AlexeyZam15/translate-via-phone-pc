# src/settings_manager.py
"""Менеджер настроек: источник истины — settings.json рядом с main.py.

Потокобезопасен. Используется:
  - simple_gui.py — для отображения и изменения настроек;
  - settings_server.py — для отдачи/приёма настроек от Android.

Android при старте делает GET /settings и применяет.
При изменении настроек на Android — POST /settings, менеджер сохраняет.
При изменении настроек в GUI — менеджер сохраняет и (опционально)
отправляет на Android через ADB broadcast (см. adb_utils).
"""

import json
import os
import threading
from typing import Any, Callable, Dict, List

from src.config import (
    SETTINGS_FILE,
    DEFAULT_SETTINGS,
    TRANSLATE_PROFILES,
    LANGUAGES,
)


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
    return True


class SettingsManager:
    """Хранит настройки, читает/пишет settings.json."""

    def __init__(self, path: str = SETTINGS_FILE):
        self._path = path
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = dict(DEFAULT_SETTINGS)
        self._listeners: List[Callable[[str, Any], None]] = []
        self.load()
        # Явно сохраняем при инициализации — файл создаётся сразу
        # при первом запуске, чтобы пользователь видел settings.json.
        self.save()

    # ------------------------------------------------------------
    # Чтение / запись файла
    # ------------------------------------------------------------

    def load(self) -> None:
        """Загружает settings.json.

        Отсутствующие или невалидные ключи берёт из DEFAULT_SETTINGS.
        """
        with self._lock:
            data = dict(DEFAULT_SETTINGS)
            if os.path.exists(self._path):
                try:
                    with open(self._path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        for k, v in loaded.items():
                            if k not in DEFAULT_SETTINGS:
                                continue
                            if _is_valid_value(k, v):
                                data[k] = v
                            # иначе — оставляем дефолт из DEFAULT_SETTINGS
                except Exception:
                    pass
            self._data = data

    def save(self) -> None:
        """Сохраняет текущие настройки в settings.json."""
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self._path), exist_ok=True)
                with open(self._path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
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
        with self._lock:
            return dict(self._data)

    def to_json(self) -> str:
        with self._lock:
            return json.dumps(self._data, ensure_ascii=False)

    def from_json(self, text: str) -> None:
        """Применяет настройки из JSON-строки (от Android)."""
        try:
            obj = json.loads(text)
        except Exception:
            return
        if not isinstance(obj, dict):
            return
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