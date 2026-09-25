# src/simple_gui.py
"""Упрощённый GUI translate_via_phone.

Трансляция всегда идёт с активного (foreground) окна Windows.
Диалог выбора окна убран. При смене активного окна трансляция
переключается автоматически без перезапуска потоков.
"""

import time
import threading
import tkinter as tk
from tkinter import scrolledtext

from src.pc_streamer import WindowStreamer
from src.ocr_receiver import OcrReceiver, DebugReceiver
from src.ocr_overlay import OcrOverlayWindow
from src.translate_server import TranslateServer
from src import adb_utils
from src.debug_logger import logger as dbg


class SimplePhoneTranslator:
    def __init__(self, root):
        self.root = root
        self.root.title("Translate via Phone")
        self.root.geometry("650x620")
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.streaming = False
        self.adb_ready = False

        self.selected_device = None

        self.pc_streamer = None
        self.ocr_receiver = None
        self.debug_receiver = None
        self.ocr_overlay = None
        self.translate_server = None

        self._help_window = None

        self._auto_refresh_timer = None
        self._auto_refresh_interval_ms = 3000

        dbg.log_event("pc.simple_gui_init")
        self.create_widgets()

        self.root.after(500, self.refresh_devices_manual)
        self._schedule_auto_refresh()

    def create_widgets(self):
        title = tk.Label(
            self.root,
            text="Translate via Phone",
            font=("Arial", 18, "bold")
        )
        title.pack(pady=15)

        # Статус
        status_frame = tk.LabelFrame(self.root, text="Статус", padx=10, pady=10)
        status_frame.pack(fill=tk.X, padx=15, pady=5)

        adb_row = tk.Frame(status_frame)
        adb_row.pack(fill=tk.X, anchor=tk.W)

        self.adb_status = tk.Label(
            adb_row, text="🔴 ADB: не выбрано", font=("Arial", 10)
        )
        self.adb_status.pack(side=tk.LEFT, anchor=tk.W)

        self.refresh_btn = tk.Button(
            adb_row,
            text="🔄 Обновить",
            command=self.refresh_devices_manual,
            font=("Arial", 9),
            bg="#2196F3",
            fg="white",
            relief=tk.FLAT,
            padx=8,
            pady=2,
            cursor="hand2"
        )
        self.refresh_btn.pack(side=tk.RIGHT, padx=(10, 0))

        self.window_status = tk.Label(
            status_frame, text="🪟 Окно: активное (авто)", font=("Arial", 10)
        )
        self.window_status.pack(anchor=tk.W, pady=(5, 0))

        self.stream_status = tk.Label(
            status_frame, text="🔴 Трансляция: остановлена", font=("Arial", 10)
        )
        self.stream_status.pack(anchor=tk.W, pady=(5, 0))

        # Большая кнопка
        self.start_btn = tk.Button(
            self.root,
            text="▶  НАЧАТЬ",
            command=self.toggle_streaming,
            font=("Arial", 20, "bold"),
            bg="#9E9E9E",
            fg="white",
            height=2,
            cursor="hand2",
            state=tk.DISABLED
        )
        self.start_btn.pack(fill=tk.X, padx=15, pady=(25, 5))

        # Кнопка "Инструкция"
        self.help_btn = tk.Button(
            self.root,
            text="❓ Инструкция",
            command=self.show_help_window,
            font=("Arial", 11),
            bg="#607D8B",
            fg="white",
            relief=tk.FLAT,
            padx=15,
            pady=6,
            cursor="hand2"
        )
        self.help_btn.pack(fill=tk.X, padx=15, pady=(0, 15))

        # Лог
        log_frame = tk.LabelFrame(self.root, text="Лог", padx=10, pady=10)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=10, font=("Consolas", 9)
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.config(state=tk.DISABLED)

    # ============================================================
    # ADB / УСТРОЙСТВА
    # ============================================================

    def log(self, message):
        try:
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {message}\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        except Exception:
            pass
        print(f"[{time.strftime('%H:%M:%S')}] {message}")

    def _schedule_auto_refresh(self):
        if self._auto_refresh_timer is not None:
            try:
                self.root.after_cancel(self._auto_refresh_timer)
            except Exception:
                pass
        self._auto_refresh_timer = self.root.after(
            self._auto_refresh_interval_ms,
            self.auto_refresh_devices
        )

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
                        self.adb_status.config(text="🔴 ADB: устройство отключено")
                        self._update_start_btn_state()
            else:
                if self.selected_device:
                    devices = adb_utils.scan_devices()
                    if self.selected_device not in devices:
                        self.log(f"⚠️ Устройство {self.selected_device} отключено во время трансляции")
                        self.stop_streaming()
                        self.adb_ready = False
                        self.selected_device = None
                        self.adb_status.config(text="🔴 ADB: устройство отключено")
                        self._update_start_btn_state()
        except Exception as e:
            self.log(f"⚠️ Ошибка автообновления: {e}")
        finally:
            self._schedule_auto_refresh()

    def refresh_devices_manual(self):
        self.log("🔄 Ручное обновление списка устройств...")
        if not adb_utils.check_adb_available():
            self.log("❌ ADB не найден в PATH!")
            self.log("ℹ️ Нажмите '❓ Инструкция' → 'Установка ADB'")
            self.adb_status.config(text="🔴 ADB: не найден в PATH")
            self._update_start_btn_state()
            return
        self._do_refresh(silent=False)

    def _do_refresh(self, silent: bool = False):
        with dbg.timer("pc.simple_gui_refresh_devices"):
            devices = adb_utils.scan_devices()

            if not devices:
                if not silent:
                    self.log("⚠️ ADB устройства не найдены. Подключите телефон по USB.")
                    self.log("ℹ️ Нажмите '❓ Инструкция' → 'Решение проблем'")
                self.adb_status.config(text="🔴 ADB: устройство не найдено")
                self.adb_ready = False
                self.selected_device = None
                self._update_start_btn_state()
                return

            usb_devices = [d for d in devices if ':' not in d]
            if usb_devices:
                new_device = usb_devices[0]
                if new_device != self.selected_device:
                    self.selected_device = new_device
                    self.log(f"✅ Найдено USB устройство: {new_device}")
                self.adb_ready = True
                self.adb_status.config(text=f"✅ ADB: {new_device} (USB)")
                self._update_start_btn_state()
                return

            new_device = devices[0]
            if new_device != self.selected_device:
                self.selected_device = new_device
                self.log(f"✅ Найдено устройство: {new_device}")
            self.adb_ready = True
            self.adb_status.config(text=f"✅ ADB: {new_device}")
            self._update_start_btn_state()

    def _update_start_btn_state(self):
        if self.streaming:
            return
        if self.adb_ready and self.selected_device is not None:
            self.start_btn.config(state=tk.NORMAL, bg="#4CAF50")
        else:
            self.start_btn.config(state=tk.DISABLED, bg="#9E9E9E")

    # ============================================================
    # ТРАНСЛЯЦИЯ
    # ============================================================

    def toggle_streaming(self):
        if self.streaming:
            self.stop_streaming()
        else:
            self.start_streaming()

    def start_streaming(self):
        if self.streaming:
            return

        if not self.adb_ready or not self.selected_device:
            self.log("❌ ADB не готов или устройство не выбрано")
            self._do_refresh(silent=False)
            if not self.adb_ready:
                return

        self.log("🪟 Определение активного окна...")
        self._do_start_streaming()

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
        self.start_btn.config(text="⏹  ОСТАНОВИТЬ", bg="#f44336")
        self.stream_status.config(text="🟢 Трансляция: запущена")

        with dbg.timer("pc.simple_gui_start_stream_total"):
            # 1. Приёмник OCR (порт 5006)
            self.ocr_receiver = OcrReceiver()
            if not self.ocr_receiver.start():
                self.log("❌ Не удалось запустить приёмник OCR")
                self.streaming = False
                self.start_btn.config(text="▶  НАЧАТЬ", bg="#4CAF50")
                self.stream_status.config(text="🔴 Трансляция: остановлена")
                self.ocr_receiver = None
                return

            # 2. Debug-приёмник (порт 5007)
            self.debug_receiver = DebugReceiver()
            if not self.debug_receiver.start():
                self.log("⚠️ Debug-сервер не запустился")
                self.debug_receiver = None

            # 3. Translate-сервер (порт 5008) — CTranslate2
            self.translate_server = TranslateServer()
            if not self.translate_server.start():
                self.log("⚠️ Translate-сервер не запустился (CTranslate2 недоступен)")
                self.translate_server = None

            # 4. MJPEG-стример (порт 5005)
            self.pc_streamer = WindowStreamer()
            self.pc_streamer.set_device(self.selected_device)

            our_hwnds = self._get_our_hwnds()
            self.pc_streamer.set_our_hwnds(our_hwnds)
            self.pc_streamer.set_on_hwnd_changed(self.on_hwnd_changed)

            # 5. Прозрачный ОВЕРЛЕЙ
            self.ocr_overlay = OcrOverlayWindow(
                self.root,
                self.ocr_receiver,
                target_hwnd=0,
                pc_streamer=self.pc_streamer,
                on_close=self.on_overlay_closed
            )
            our_hwnds = self._get_our_hwnds()
            self.pc_streamer.set_our_hwnds(our_hwnds)

            # 6. Запуск стримера в отдельном потоке
            def _start_streamer():
                ok = self.pc_streamer.start()
                if not ok:
                    self.root.after(0, self._on_streamer_start_failed)
                else:
                    try:
                        hwnd = self.pc_streamer.hwnd
                        title = self.pc_streamer.window_title or "?"
                        if hwnd:
                            self.root.after(0, lambda: self.on_hwnd_changed(hwnd, title))
                        else:
                            self.root.after(0, lambda: self.window_status.config(
                                text="🪟 Окно: ожидание активного..."
                            ))
                    except Exception:
                        pass

            threading.Thread(target=_start_streamer, daemon=True).start()

            self.log("🚀 Запуск трансляции активного окна...")
            self.log(f"📱 Устройство: {self.selected_device}")
            self.log("ℹ️ Запустите Android-приложение и нажмите 'Старт OCR ПК'")
            # ИЗМЕНЕНО: текст про Translate-сервер
            self.log("ℹ️ Translate-сервер: порт 5008 (CTranslate2 + Opus-MT)")

            dbg.log_event("pc.simple_gui_stream_started")

    def _on_streamer_start_failed(self):
        self.log("❌ Не удалось запустить трансляцию (HTTP-сервер не поднялся)")
        self.streaming = False
        self.start_btn.config(text="▶  НАЧАТЬ", bg="#4CAF50")
        self.stream_status.config(text="🔴 Трансляция: остановлена")
        self.window_status.config(text="🪟 Окно: активное (авто)")
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
        self.start_btn.config(text="▶  НАЧАТЬ", bg="#4CAF50")
        self.stream_status.config(text="🔴 Трансляция: остановлена")
        self.window_status.config(text="🪟 Окно: активное (авто)")

        threading.Thread(target=self._cleanup_streamer, daemon=True).start()

        self.log("⏹ Трансляция остановлена")
        dbg.log_event("pc.simple_gui_stream_stopped")
        self._update_start_btn_state()

    def on_overlay_closed(self):
        if self.streaming:
            self.log("🌐 Окно перевода закрыто пользователем")
            self.stop_streaming()

    # ============================================================
    # ИНСТРУКЦИЯ
    # ============================================================

    def show_help_window(self):
        if self._help_window is not None:
            try:
                if self._help_window.winfo_exists():
                    self._help_window.lift()
                    self._help_window.focus_force()
                    return
            except Exception:
                self._help_window = None

        self._help_window = tk.Toplevel(self.root)
        self._help_window.title("❓ Инструкция")
        self._help_window.geometry("750x650")
        self._help_window.minsize(600, 500)
        self._help_window.configure(bg="#1e1e1e")
        self._help_window.transient(self.root)

        header = tk.Label(
            self._help_window,
            text="📖 Инструкция по использованию",
            font=("Arial", 16, "bold"),
            bg="#1e1e1e",
            fg="#4CAF50"
        )
        header.pack(pady=(15, 5))

        subtitle = tk.Label(
            self._help_window,
            text="Translate via Phone — перевод экрана ПК через телефон",
            font=("Arial", 10),
            bg="#1e1e1e",
            fg="#888888"
        )
        subtitle.pack(pady=(0, 10))

        tk.Frame(self._help_window, bg="#3c3c3c", height=1).pack(fill=tk.X, padx=20)

        text_frame = tk.Frame(self._help_window, bg="#1e1e1e")
        text_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        scrollbar = tk.Scrollbar(text_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        help_text = tk.Text(
            text_frame,
            wrap=tk.WORD,
            font=("Segoe UI", 10),
            bg="#2d2d2d",
            fg="#e0e0e0",
            insertbackground="white",
            relief=tk.FLAT,
            padx=15,
            pady=10,
            yscrollcommand=scrollbar.set
        )
        help_text.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=help_text.yview)

        help_text.tag_configure("h1", font=("Segoe UI", 13, "bold"), foreground="#4CAF50", spacing1=10, spacing3=5)
        help_text.tag_configure("h2", font=("Segoe UI", 11, "bold"), foreground="#2196F3", spacing1=8, spacing3=3)
        help_text.tag_configure("code", font=("Consolas", 10), background="#1a1a1a", foreground="#FFD700")
        help_text.tag_configure("warn", foreground="#FF9800")
        help_text.tag_configure("ok", foreground="#4CAF50")
        help_text.tag_configure("link", foreground="#64B5F6", underline=True)

        self._insert_help_text(help_text)
        help_text.config(state=tk.DISABLED)

        btn_frame = tk.Frame(self._help_window, bg="#1e1e1e")
        btn_frame.pack(fill=tk.X, padx=20, pady=(0, 15))

        close_btn = tk.Button(
            btn_frame,
            text="Закрыть",
            command=self._close_help_window,
            font=("Arial", 10, "bold"),
            bg="#4CAF50",
            fg="white",
            relief=tk.FLAT,
            padx=30,
            pady=8,
            cursor="hand2"
        )
        close_btn.pack(side=tk.RIGHT)

        self._help_window.bind("<Escape>", lambda e: self._close_help_window())
        self._help_window.protocol("WM_DELETE_WINDOW", self._close_help_window)
        self._help_window.focus_force()

    def _close_help_window(self):
        if self._help_window is not None:
            try:
                self._help_window.destroy()
            except Exception:
                pass
            self._help_window = None

    def _insert_help_text(self, text_widget: tk.Text):
        lines = [
            ("===H1===", "Быстрый старт (3 шага)"),
            ("", ""),
            ("===H2===", "Шаг 1. Подключите телефон по USB"),
            ("", "1. На телефоне: Настройки → О телефоне → 7 раз нажать \"Номер сборки\""),
            ("", "2. Настройки → Для разработчиков → включить \"Отладка по USB\""),
            ("", "3. Подключите телефон к ПК кабелем (не только для зарядки)"),
            ("", "4. На телефоне появится диалог \"Разрешить отладку по USB?\" → РАЗРЕШИТЬ"),
            ("===OK===", "Если всё сделано правильно — появится \"✅ ADB: <серийник> (USB)\""),
            ("", ""),
            ("===H2===", "Шаг 2. Нажмите \"▶ НАЧАТЬ\""),
            ("", "Трансляция начнётся автоматически с активного окна Windows."),
            ("===WARN===", "Просто переключите фокус на нужное окно — трансляция подхватит его сама."),
            ("", "Окно оверлея и окно программы не захватываются автоматически."),
            ("", ""),
            ("===H2===", "Шаг 3. Запустите Android-приложение"),
            ("", "1. Откройте на телефоне приложение ScreenTranslator2"),
            ("", "2. Нажмите \"Старт OCR ПК\""),
            ("", "3. Поверх исходного окна появится перевод"),
            ("", ""),
            ("===H1===", "Как работает автопереключение"),
            ("", ""),
            ("", "Программа каждые 300 мс проверяет активное окно Windows."),
            ("", "Как только вы переключитесь на другое окно — трансляция"),
            ("", "автоматически переключится на него БЕЗ перезапуска потоков."),
            ("", "Это значит, что MJPEG-поток на телефоне не прервётся."),
            ("", ""),
            ("===WARN===", "Окно \"Translate via Phone\" и окно оверлея игнорируются."),
            ("", "Если на момент старта нет подходящего окна — программа"),
            ("", "перейдёт в режим ожидания и подхватит окно, как только"),
            ("", "вы переключитесь на него."),
            ("", ""),
            ("===H1===", "Решение проблем"),
            ("", ""),
            ("===H2===", "❌ ADB устройства не найдены"),
            ("", "Проверьте по порядку:"),
            ("===WARN===", "1. ADB установлен? Откройте cmd и выполните:"),
            ("===CODE===", "adb version"),
            ("", "Если команда не найдена — ADB не в PATH (см. раздел ниже)."),
            ("===WARN===", "2. Телефон подключён? Проверьте кабель и USB-порт."),
            ("===WARN===", "3. Отладка по USB включена?"),
            ("", "Настройки → Для разработчиков → \"Отладка по USB\" → ВКЛ"),
            ("===WARN===", "4. Разрешение на отладку выдано?"),
            ("", "На телефоне должен появиться диалог. Если нет:"),
            ("", "- Отключите USB"),
            ("", "- Настройки → Для разработчиков → \"Отозвать разрешения на отладку по USB\""),
            ("", "- Подключите USB снова → разрешите"),
            ("===WARN===", "5. ADB-сервер завис? Выполните:"),
            ("===CODE===", "adb kill-server"),
            ("===CODE===", "adb start-server"),
            ("===CODE===", "adb devices"),
            ("===WARN===", "6. Телефон в режиме \"Только зарядка\"?"),
            ("", "Опустите шторку → USB-подключение → \"Передача файлов (MTP)\""),
            ("", ""),
            ("===H2===", "❌ Трансляция идёт, но перевод не появляется"),
            ("", "Если Android-приложение запущено, но оверлей не появляется:"),
            ("", ""),
            ("", "1. Проверьте ADB reverse во время трансляции (в отдельном окне):"),
            ("===CODE===", "adb reverse --list"),
            ("", "Должно быть 4 записи (tcp:5005, tcp:5006, tcp:5007, tcp:5008)."),
            ("===WARN===", "Если пусто — ADB reverse не работает. Обновите Platform Tools."),
            ("", ""),
            ("", "2. Проверьте логи Android-приложения:"),
            ("===CODE===", "adb logcat | findstr PcOcrService"),
            ("", "Ищите: PcOcrService started, MJPEG loop error, decode failed."),
            ("", ""),
            ("", "3. В логе ПК должны быть строки:"),
            ("===CODE===", "[PC-Stream] 📡 MJPEG client connected: ..."),
            ("===CODE===", "[Translate-Server] 📥 #1: ... engine=ctranslate2"),
            ("===CODE===", "[OCR-Receiver] 📥 POST /ocr #1: ..."),
            ("", "Если их нет — Android не подключается к ПК."),
            ("", ""),
            ("===H2===", "❌ Захватывается не то окно / чёрный экран"),
            ("", "1. Переключите фокус на нужное окно — трансляция подхватит его автоматически."),
            ("===WARN===", "НЕ переключайтесь на \"Translate via Phone\" — оно игнорируется."),
            ("", "2. Некоторые приложения (игры с DirectX) не захватываются через PrintWindow."),
            ("", "3. Попробуйте другое окно для проверки."),
            ("", ""),
            ("===H1===", "Установка ADB (если не установлен)"),
            ("", ""),
            ("", "1. Скачайте: https://developer.android.com/tools/releases/platform-tools"),
            ("", "2. Распакуйте архив, например, в C:\\platform-tools\\"),
            ("", "3. Добавьте C:\\platform-tools\\ в переменную среды PATH:"),
            ("", "   Win + R → sysdm.cpl → Дополнительно → Переменные среды"),
            ("", "   В \"Системные переменные\" найдите Path → Изменить → Создать"),
            ("===CODE===", "C:\\platform-tools\\"),
            ("", "4. Проверьте в НОВОЙ командной строке:"),
            ("===CODE===", "adb version"),
            ("===OK===", "Альтернатива: положите adb.exe рядом с main.py"),
            ("", ""),
            ("===H1===", "Кнопки программы"),
            ("", ""),
            ("===H2===", "🔄 Обновить"),
            ("", "Пересканировать подключённые устройства. Автообновление каждые 3 сек."),
            ("===H2===", "▶ НАЧАТЬ"),
            ("", "Запустит трансляцию активного окна на телефон."),
            ("===H2===", "⏹ ОСТАНОВИТЬ"),
            ("", "Остановить трансляцию."),
            ("", ""),
            ("===H1===", "Логи"),
            ("", ""),
            ("===CODE===", "debug.log — вывод программы"),
            ("===CODE===", "debug_timing.jsonl — тайминги и события"),
            ("", ""),
            ("===H1===", "Ссылки"),
            ("", ""),
            ("===LINK===", "ADB: https://developer.android.com/tools/releases/platform-tools"),
            ("===LINK===", "ADB docs: https://developer.android.com/studio/command-line/adb"),
        ]

        for marker, line in lines:
            if marker == "===H1===":
                text_widget.insert(tk.END, line + "\n", "h1")
            elif marker == "===H2===":
                text_widget.insert(tk.END, line + "\n", "h2")
            elif marker == "===CODE===":
                text_widget.insert(tk.END, line + "\n", "code")
            elif marker == "===WARN===":
                text_widget.insert(tk.END, line + "\n", "warn")
            elif marker == "===OK===":
                text_widget.insert(tk.END, line + "\n", "ok")
            elif marker == "===LINK===":
                text_widget.insert(tk.END, line + "\n", "link")
            else:
                text_widget.insert(tk.END, line + "\n")

    # ============================================================
    # ЗАВЕРШЕНИЕ
    # ============================================================

    def on_closing(self):
        dbg.log_event("pc.simple_gui_closing")
        self.log("👋 Завершение работы...")

        if self._auto_refresh_timer is not None:
            try:
                self.root.after_cancel(self._auto_refresh_timer)
            except Exception:
                pass
            self._auto_refresh_timer = None

        self._close_help_window()

        if self.streaming:
            self.streaming = False
            try:
                threading.Thread(target=self._cleanup_streamer, daemon=True).start()
            except Exception:
                pass

        dbg.log_event("pc.simple_gui_closed")
        dbg.flush()
        self.root.destroy()