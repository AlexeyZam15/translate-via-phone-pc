# src/simple_gui.py
"""Упрощённый GUI translate_via_phone.

ИЗМЕНЕНО (разделение на 3 файла):
  - Методы окна настроек вынесены в src/gui_settings_window.py
    (SettingsWindowMixin).
  - Методы окон-инструкций вынесены в src/gui_help_windows.py
    (HelpWindowsMixin).
  - SimplePhoneTranslator наследует оба миксина.

Сохранено:
  - При остановке трансляции или закрытии ПК-приложения
    автоматически останавливается Android-приложение (force-stop).
  - Кнопка НАЧАТЬ всегда активна.
  - Версионный контроль по SHA-256 хэшу APK.
  - overlay_alpha по умолчанию = 0.
  - УДАЛЕНО: настройка "Автоматический оконный режим без рамки".
  - Флаг _closing для идемпотентности on_closing.
  - Кнопка меняет вид на "УСТАНОВИТЬ", если пакет не установлен.
  - Автозапуск Android-приложения после установки APK УБРАН.
  - Диалог "Установить приложение?" убран.
  - Диалог "Обновить приложение?" оставлен.
  - Проверка наличия пакета выполняется в фоновом потоке.
"""

import os
import time
import threading
import tkinter as tk
from tkinter import messagebox

from src.pc_streamer import WindowStreamer
from src.ocr_receiver import OcrReceiver, DebugReceiver
from src.ocr_overlay import OcrOverlayWindow
from src.translate_server import TranslateServer
from src.settings_server import SettingsServer
from src.settings_manager import settings_manager
from src import adb_utils
from src.debug_logger import logger as dbg

from src.gui_settings_window import SettingsWindowMixin
from src.gui_help_windows import HelpWindowsMixin


class SimplePhoneTranslator(SettingsWindowMixin, HelpWindowsMixin):
    def __init__(self, root):
        self.root = root
        self.root.title("Translate via Phone")
        self.root.geometry("420x560")
        self.root.minsize(320, 420)
        self.root.configure(bg="#1e1e1e")
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.streaming = False
        self.adb_ready = False
        self.selected_device = None

        # Состояние наличия пакета на устройстве:
        # None  — неизвестно (проверка не выполнялась).
        # True  — пакет установлен.
        # False — пакет не установлен.
        self._package_installed = None

        self.pc_streamer = None
        self.ocr_receiver = None
        self.debug_receiver = None
        self.ocr_overlay = None
        self.translate_server = None
        self.settings_server = None

        # Поля, используемые миксинами (SettingsWindowMixin,
        # HelpWindowsMixin). Создаются здесь, чтобы миксины могли
        # к ним обращаться.
        self._settings_window = None
        self._help_window = None
        self._connect_window = None
        self._settings_window_widgets = {}

        self.adb_status = None
        self.window_status = None
        self.stream_status = None

        self._suppress_slider_events = False

        self._push_timer = None
        self._push_delay_ms = 300

        self._auto_refresh_timer = None
        self._auto_refresh_interval_ms = 3000

        self._android_stopped = False
        self._installing = False

        self._closing = False

        dbg.log_event("pc.simple_gui_init")
        self.create_widgets()

        settings_manager.add_listener(self._on_setting_changed)

        self.root.after(500, self.refresh_devices_manual)
        self._schedule_auto_refresh()

    # ============================================================
    # ГЛАВНОЕ ОКНО
    # ============================================================

    def create_widgets(self):
        self.settings_btn = tk.Button(
            self.root, text="⚙", command=self.open_settings_window,
            font=("Arial", 20, "bold"), bg="#607D8B", fg="white",
            relief=tk.FLAT, width=3, height=1, cursor="hand2",
            activebackground="#455A64", activeforeground="white"
        )
        self.settings_btn.place(relx=1.0, x=-12, y=12, anchor="ne")

        self.start_btn = tk.Button(
            self.root, text="▶\nНАЧАТЬ", command=self.toggle_streaming,
            font=("Arial", 28, "bold"), bg="#607D8B", fg="white",
            relief=tk.FLAT, cursor="hand2",
            activebackground="#4CAF50", activeforeground="white",
            justify=tk.CENTER
        )
        self.start_btn.place(relx=0.5, rely=0.5, anchor="center",
                             relwidth=0.9, relheight=0.75)

        self.status_label = tk.Label(
            self.root, text="🔴 ADB: не выбрано",
            font=("Arial", 9), bg="#1e1e1e", fg="#888888"
        )
        self.status_label.place(relx=0.5, rely=1.0, y=-8, anchor="s")

    # ============================================================
    # ЛОГ
    # ============================================================

    def log(self, message):
        try:
            print(f"[{time.strftime('%H:%M:%S')}] {message}")
        except Exception:
            pass

    # ============================================================
    # АВТООСТАНОВКА ANDROID OCR
    # ============================================================

    def _auto_stop_android_ocr(self):
        if self._android_stopped:
            return
        self._android_stopped = True

        if not self.selected_device:
            return
        try:
            threading.Thread(
                target=adb_utils.force_stop_android_app,
                args=(self.selected_device,),
                daemon=True
            ).start()
        except Exception:
            pass

    def _reset_android_stopped_flag(self):
        self._android_stopped = False

    # ============================================================
    # ADB / УСТРОЙСТВА
    # ============================================================

    def _schedule_auto_refresh(self):
        if self._auto_refresh_timer is not None:
            try:
                self.root.after_cancel(self._auto_refresh_timer)
            except Exception:
                pass
        self._auto_refresh_timer = self.root.after(
            self._auto_refresh_interval_ms, self.auto_refresh_devices)

    def auto_refresh_devices(self):
        try:
            if not self.streaming:
                if not self.adb_ready:
                    self._do_refresh(silent=True)
                elif self.selected_device:
                    devices = adb_utils.scan_devices()
                    if self.selected_device not in devices:
                        self.log(f"⚠️ Устройство {self.selected_device} отключено")
                        self.adb_ready = False
                        self.selected_device = None
                        self._package_installed = None
                        self._update_status_labels()
                        self._update_start_btn_state()
            else:
                if self.selected_device:
                    devices = adb_utils.scan_devices()
                    if self.selected_device not in devices:
                        self.log(f"⚠️ Устройство {self.selected_device} "
                                 f"отключено во время трансляции")
                        self.stop_streaming()
                        self.adb_ready = False
                        self.selected_device = None
                        self._package_installed = None
                        self._update_status_labels()
                        self._update_start_btn_state()
        except Exception as e:
            self.log(f"⚠️ Ошибка автообновления: {e}")
        finally:
            self._schedule_auto_refresh()

    def refresh_devices_manual(self):
        self.log("🔄 Ручное обновление списка устройств...")
        if not adb_utils.check_adb_available():
            self.log("❌ ADB не найден в PATH!")
            self._update_status_labels()
            self._update_start_btn_state()
            return
        self._do_refresh(silent=False)

    def _do_refresh(self, silent: bool = False):
        with dbg.timer("pc.simple_gui_refresh_devices"):
            devices = adb_utils.scan_devices()

            if not devices:
                if not silent:
                    self.log("⚠️ ADB устройства не найдены. "
                             "Подключите телефон по USB.")
                self.adb_ready = False
                self.selected_device = None
                self._package_installed = None
                self._update_status_labels()
                self._update_start_btn_state()
                return

            usb_devices = [d for d in devices if ':' not in d]
            if usb_devices:
                new_device = usb_devices[0]
                if new_device != self.selected_device:
                    self.selected_device = new_device
                    self._package_installed = None
                    self.log(f"✅ Найдено USB устройство: {new_device}")
                self.adb_ready = True
                self._update_status_labels()
                self._update_start_btn_state()
                self._refresh_package_state_async()
                return

            new_device = devices[0]
            if new_device != self.selected_device:
                self.selected_device = new_device
                self._package_installed = None
                self.log(f"✅ Найдено устройство: {new_device}")
            self.adb_ready = True
            self._update_status_labels()
            self._update_start_btn_state()
            self._refresh_package_state_async()

    def _refresh_package_state_async(self):
        """Фоновая проверка наличия пакета com.example.screentranslator.

        Результат кладётся в self._package_installed, после чего
        в UI-потоке вызывается _update_start_btn_state().
        """
        device = self.selected_device
        if not device:
            return

        def _worker():
            try:
                installed = adb_utils.is_package_installed(
                    device, adb_utils.ANDROID_PACKAGE)
            except Exception:
                installed = None
            self.root.after(
                0, lambda: self._on_package_state_checked(device, installed))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_package_state_checked(self, device, installed):
        # Устройство могло смениться, пока шла проверка.
        if device != self.selected_device:
            return
        self._package_installed = installed
        self._update_start_btn_state()

    def _update_status_labels(self):
        if self.adb_ready and self.selected_device:
            adb_text = f"✅ ADB: {self.selected_device}"
        elif not adb_utils.check_adb_available():
            adb_text = "🔴 ADB: не найден"
        else:
            adb_text = "🔴 ADB: не выбрано"

        try:
            self.status_label.config(text=adb_text)
        except Exception:
            pass
        try:
            if self.adb_status is not None:
                self.adb_status.config(text=adb_text)
        except Exception:
            pass

    def _update_start_btn_state(self):
        """Обновление вида кнопки НАЧАТЬ.

        Логика:
          - streaming                       → "ОСТАНОВИТЬ" (красная).
          - нет устройства / ADB            → "НАЧАТЬ" (серая).
          - устройство есть, пакет не стоит → "УСТАНОВИТЬ" (оранжевая).
          - устройство есть, пакет стоит    → "НАЧАТЬ" (зелёная).
          - пакет проверяется (_package_installed is None) → "НАЧАТЬ".
        """
        if self.streaming:
            try:
                self.start_btn.config(text="⏹\nОСТАНОВИТЬ", bg="#f44336")
            except Exception:
                pass
            return

        if not (self.adb_ready and self.selected_device is not None):
            try:
                self.start_btn.config(text="▶\nНАЧАТЬ", bg="#607D8B")
            except Exception:
                pass
            return

        if self._package_installed is False:
            try:
                self.start_btn.config(text="⤓\nУСТАНОВИТЬ", bg="#FF9800")
            except Exception:
                pass
            return

        try:
            self.start_btn.config(text="▶\nНАЧАТЬ", bg="#4CAF50")
        except Exception:
            pass

    # ============================================================
    # ВЕРСИОННЫЙ КОНТРОЛЬ APK (по хэшу)
    # ============================================================

    def _ensure_app_async(self, apk_path, known_hash, on_done):
        if self._installing:
            return
        if not self.selected_device:
            on_done(False, "error", "Устройство не выбрано", "")
            return

        self._installing = True
        self.log(f"📦 Установка APK: {os.path.basename(apk_path)}")
        try:
            self.start_btn.config(text="⏳\nУСТАНОВКА...", bg="#FF9800")
        except Exception:
            pass

        def _worker():
            ok, action, msg, new_hash = adb_utils.ensure_app_installed(
                self.selected_device, apk_path, known_hash=known_hash)
            self.root.after(0, lambda: self._on_ensure_done(
                ok, action, msg, new_hash, on_done))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_ensure_done(self, ok, action, msg, new_hash, on_done):
        self._installing = False

        if ok and new_hash:
            try:
                changed = settings_manager.set(
                    "last_installed_apk_hash", new_hash)
                if changed:
                    self.log(f"💾 Запомнен хэш APK: {new_hash[:12]}…")
            except Exception:
                pass

        if ok:
            self._package_installed = True
            if action == "none":
                self.log("✅ APK актуален — приложение не переустанавливалось")
            elif action == "install":
                self.log("✅ APK установлен")
            elif action == "reinstall":
                self.log("✅ APK обновлён (данные сохранены)")
            elif action == "update":
                self.log("✅ APK переустановлен (данные сброшены)")
        else:
            self.log(f"❌ Ошибка установки: {msg[:200]}")

        self._update_start_btn_state()

        try:
            on_done(ok, action, msg, new_hash)
        except Exception:
            pass

    # ============================================================
    # ТРАНСЛЯЦИЯ
    # ============================================================

    def toggle_streaming(self):
        if self.streaming:
            self.stop_streaming()
            return

        # Если пакет не установлен — запускаем установку,
        # а не старт трансляции. Кнопка уже называется "УСТАНОВИТЬ".
        if self._package_installed is False:
            self._start_install_flow()
            return

        self.start_streaming()

    def _start_install_flow(self):
        """Запуск установки APK по кнопке "УСТАНОВИТЬ".

        Диалог подтверждения НЕ показывается: пользователь уже
        нажал кнопку "УСТАНОВИТЬ". После успешной установки
        трансляция НЕ стартует, автозапуск Android НЕ выполняется.
        Кнопка становится "НАЧАТЬ".
        """
        if self._installing:
            return

        apk_path = adb_utils.get_apk_path()
        if apk_path is None:
            messagebox.showerror(
                "APK не найден",
                "APK-файл не найден рядом с приложением.\n\n"
                "Положите app-debug.apk рядом с main.py (или .exe).",
                parent=self.root,
            )
            return

        known_hash = str(
            settings_manager.get("last_installed_apk_hash", "") or "")

        def _after_install(ok, action, msg, new_hash):
            if ok:
                self.log("ℹ️ Установка завершена. Нажмите НАЧАТЬ для запуска.")
            else:
                messagebox.showerror(
                    "Ошибка установки",
                    f"Не удалось установить APK:\n\n{msg}",
                    parent=self.root,
                )

        self._ensure_app_async(apk_path, known_hash, _after_install)

    def start_streaming(self):
        if self.streaming:
            return

        if not self.adb_ready or not self.selected_device:
            self._do_refresh(silent=True)
            if not self.adb_ready or not self.selected_device:
                self.log("📱 Устройство не подключено — показываю инструкцию")
                self._show_connect_instructions()
                return

        apk_path = adb_utils.get_apk_path()
        if apk_path is None:
            messagebox.showerror(
                "APK не найден",
                "APK-файл не найден рядом с приложением.\n\n"
                "Положите app-debug.apk рядом с main.py (или .exe).",
                parent=self.root,
            )
            return

        known_hash = str(
            settings_manager.get("last_installed_apk_hash", "") or "")
        apk_size_mb = os.path.getsize(apk_path) / (1024 * 1024)

        installed = adb_utils.is_package_installed(
            self.selected_device, adb_utils.ANDROID_PACKAGE)

        if installed:
            self._package_installed = True
            if known_hash:
                current_hash = adb_utils.apk_sha256(apk_path)
                if current_hash and current_hash == known_hash:
                    self.log("✅ APK актуален — старт")
                    self._start_streaming_after_checks(skip_launch=False)
                    return

            answer = messagebox.askyesno(
                "Обновить приложение?",
                f"APK-файл изменился (новый: {apk_size_mb:.1f} МБ).\n\n"
                f"Обновить приложение на устройстве?",
                parent=self.root,
            )
            if not answer:
                self.log("ℹ️ Пользователь отказался от обновления — "
                         "старт с текущей версией")
                self._start_streaming_after_checks(skip_launch=False)
                return

            def _after_update(ok, action, msg, new_hash):
                if ok:
                    self.log("ℹ️ Обновление завершено. "
                             "Нажмите НАЧАТЬ для запуска.")
                else:
                    messagebox.showerror(
                        "Ошибка обновления",
                        f"Не удалось обновить APK:\n\n{msg}",
                        parent=self.root,
                    )

            self._ensure_app_async(apk_path, known_hash, _after_update)
            return

        # Пакет не установлен — установка без диалога,
        # трансляция не стартует, автозапуск Android не выполняется.
        self._package_installed = False
        self._update_start_btn_state()

        def _after_install(ok, action, msg, new_hash):
            if ok:
                self.log("ℹ️ Установка завершена. "
                         "Нажмите НАЧАТЬ для запуска.")
            else:
                messagebox.showerror(
                    "Ошибка установки",
                    f"Не удалось установить APK:\n\n{msg}",
                    parent=self.root,
                )

        self._ensure_app_async(apk_path, "", _after_install)

    def _start_streaming_after_checks(self, skip_launch: bool = False):
        """Запуск трансляции после проверок APK.

        Параметр skip_launch:
          - False (по умолчанию): выполняется автозапуск Android-
            приложения, если auto_launch_android=True.
          - True: автозапуск Android-приложения НЕ выполняется.

        В текущей версии после установки/обновления APK трансляция
        вообще не стартует, поэтому skip_launch пока не используется
        в этих ветках. Оставлен для совместимости и будущих сценариев.
        """
        self._reset_android_stopped_flag()

        if not skip_launch and bool(
                settings_manager.get("auto_launch_android", True)):
            self.log("📱 Автозапуск Android-приложения...")
            threading.Thread(
                target=self._auto_launch_android, daemon=True
            ).start()

        self.log("🪟 Определение активного окна...")
        self._do_start_streaming()

    def _auto_launch_android(self):
        ok = adb_utils.launch_android_app(
            self.selected_device, auto_start_ocr=True)
        if ok:
            self.log("✅ Android-приложение запущено")
        else:
            self.log("⚠️ Не удалось запустить Android-приложение "
                     "автоматически — запустите вручную")

    def _get_our_hwnds(self):
        hwnds = []
        try:
            hwnds.append(int(self.root.winfo_id()))
        except Exception:
            pass
        if self.ocr_overlay is not None:
            try:
                hwnds.append(int(self.ocr_overlay.window.winfo_id()))
            except Exception:
                pass
        return hwnds

    def on_hwnd_changed(self, hwnd, title):
        try:
            short = title if len(title) <= 40 else title[:37] + "..."
            if self.window_status is not None:
                self.window_status.config(text=f"🟢 Окно: {short}")
        except Exception:
            pass
        if self.ocr_overlay is not None:
            try:
                self.ocr_overlay.set_target_hwnd(hwnd)
            except Exception as e:
                self.log(f"⚠️ Ошибка смены HWND в оверлее: {e}")

    def _do_start_streaming(self):
        self.streaming = True
        self._update_start_btn_state()

        try:
            if self.stream_status is not None:
                self.stream_status.config(text="🟢 Трансляция: запущена")
        except Exception:
            pass

        with dbg.timer("pc.simple_gui_start_stream_total"):
            self.ocr_receiver = OcrReceiver()
            if not self.ocr_receiver.start():
                self.log("❌ Не удалось запустить приёмник OCR")
                self.streaming = False
                self._update_start_btn_state()
                try:
                    if self.stream_status is not None:
                        self.stream_status.config(
                            text="🔴 Трансляция: остановлена")
                except Exception:
                    pass
                self.ocr_receiver = None
                return

            self.debug_receiver = DebugReceiver()
            if not self.debug_receiver.start():
                self.log("⚠️ Debug-сервер не запустился")
                self.debug_receiver = None

            self.translate_server = TranslateServer()
            if not self.translate_server.start():
                self.log("⚠️ Translate-сервер не запустился")
                self.translate_server = None

            self.settings_server = SettingsServer()
            if not self.settings_server.start():
                self.log("⚠️ Settings-сервер не запустился")
                self.settings_server = None

            self.pc_streamer = WindowStreamer()
            self.pc_streamer.set_device(self.selected_device)

            our_hwnds = self._get_our_hwnds()
            self.pc_streamer.set_our_hwnds(our_hwnds)
            self.pc_streamer.set_on_hwnd_changed(self.on_hwnd_changed)

            if self.ocr_overlay is not None:
                try:
                    if self.ocr_overlay.window.winfo_exists():
                        self.ocr_overlay.set_target_hwnd(0)
                        self.ocr_overlay.show()
                    else:
                        self.ocr_overlay = None
                except Exception:
                    self.ocr_overlay = None

            if self.ocr_overlay is None:
                self.ocr_overlay = OcrOverlayWindow(
                    self.root,
                    self.ocr_receiver,
                    target_hwnd=0,
                    pc_streamer=self.pc_streamer,
                    on_close=self.on_overlay_closed
                )
            our_hwnds = self._get_our_hwnds()
            self.pc_streamer.set_our_hwnds(our_hwnds)

            def _start_streamer():
                ok = self.pc_streamer.start()
                if not ok:
                    self.root.after(0, self._on_streamer_start_failed)
                else:
                    try:
                        hwnd = self.pc_streamer.hwnd
                        title = self.pc_streamer.window_title or "?"
                        if hwnd:
                            self.root.after(
                                0, lambda: self.on_hwnd_changed(hwnd, title))
                        else:
                            self.root.after(0, lambda: self._set_window_status(
                                "🪟 Окно: ожидание активного..."))
                    except Exception:
                        pass

            threading.Thread(target=_start_streamer, daemon=True).start()

            self.log("🚀 Запуск трансляции активного окна...")
            self.log(f"📱 Устройство: {self.selected_device}")
            self.log("ℹ️ Android-приложение запустится автоматически")
            self.log("ℹ️ Translate-сервер: порт 5008 (CTranslate2 + Opus-MT)")
            self.log("ℹ️ Settings-сервер: порт 5009")

            dbg.log_event("pc.simple_gui_stream_started")

    def _set_window_status(self, text):
        try:
            if self.window_status is not None:
                self.window_status.config(text=text)
        except Exception:
            pass

    def _on_streamer_start_failed(self):
        self.log("❌ Не удалось запустить трансляцию")
        self.streaming = False
        self._update_start_btn_state()
        try:
            if self.stream_status is not None:
                self.stream_status.config(text="🔴 Трансляция: остановлена")
        except Exception:
            pass
        self._set_window_status("🪟 Окно: активное (авто)")
        self._cleanup_streamer()
        self._update_start_btn_state()

    def _cleanup_streamer(self):
        if self.ocr_overlay:
            try:
                self.ocr_overlay.destroy()
            except Exception:
                pass
            self.ocr_overlay = None

        if self.pc_streamer:
            try:
                self.pc_streamer.stop()
            except Exception:
                pass
            self.pc_streamer = None

        if self.settings_server:
            try:
                self.settings_server.stop()
            except Exception:
                pass
            self.settings_server = None

        if self.translate_server:
            try:
                self.translate_server.stop()
            except Exception:
                pass
            self.translate_server = None

        if self.debug_receiver:
            try:
                self.debug_receiver.stop()
            except Exception:
                pass
            self.debug_receiver = None

        if self.ocr_receiver:
            try:
                self.ocr_receiver.stop()
            except Exception:
                pass
            self.ocr_receiver = None

    def stop_streaming(self):
        if not self.streaming:
            return

        self.streaming = False
        self._update_start_btn_state()
        try:
            if self.stream_status is not None:
                self.stream_status.config(text="🔴 Трансляция: остановлена")
        except Exception:
            pass
        self._set_window_status("🪟 Окно: активное (авто)")

        self._auto_stop_android_ocr()

        threading.Thread(target=self._cleanup_streamer, daemon=True).start()

        self.log("⏹ Трансляция остановлена (Android OCR остановлен)")
        dbg.log_event("pc.simple_gui_stream_stopped")

    def on_overlay_closed(self):
        if self.streaming:
            self.log("🌐 Окно перевода закрыто пользователем")
            self.stop_streaming()

    # ============================================================
    # ЗАВЕРШЕНИЕ
    # ============================================================

    def on_closing(self):
        if getattr(self, '_closing', False):
            return
        self._closing = True

        dbg.log_event("pc.simple_gui_closing")
        self.log("👋 Завершение работы...")

        if self._auto_refresh_timer is not None:
            try:
                self.root.after_cancel(self._auto_refresh_timer)
            except Exception:
                pass
            self._auto_refresh_timer = None

        if self._push_timer is not None:
            try:
                self.root.after_cancel(self._push_timer)
            except Exception:
                pass
            self._push_timer = None

        self._close_help_window()
        self._close_settings_window()
        self._close_connect_window()

        if self.streaming:
            self.streaming = False
            self._auto_stop_android_ocr()
            try:
                threading.Thread(target=self._cleanup_streamer,
                                 daemon=True).start()
            except Exception:
                pass
        else:
            self._auto_stop_android_ocr()

        dbg.log_event("pc.simple_gui_closed")
        dbg.flush()
        try:
            self.root.destroy()
        except Exception:
            pass