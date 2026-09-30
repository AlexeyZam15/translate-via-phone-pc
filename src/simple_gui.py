# src/simple_gui.py
"""Упрощённый GUI translate_via_phone.

ИЗМЕНЕНО:
  - При остановке трансляции или закрытии ПК-приложения
    автоматически останавливается Android-приложение (force-stop).
  - Кнопка НАЧАТЬ всегда активна.
  - Версионный контроль по SHA-256 хэшу APK.
  - overlay_alpha по умолчанию = 0.
  - УДАЛЕНО: настройка "Автоматический оконный режим без рамки"
    и все упоминания о borderless fullscreen.
  - Добавлен флаг _closing для идемпотентности on_closing.
"""

import os
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from src.pc_streamer import WindowStreamer
from src.ocr_receiver import OcrReceiver, DebugReceiver
from src.ocr_overlay import OcrOverlayWindow
from src.translate_server import TranslateServer
from src.settings_server import SettingsServer
from src.settings_manager import settings_manager
from src.config import (
    LANGUAGES,
    TRANSLATE_PROFILES,
    TRANSLATE_PROFILE_LABELS,
)
from src import adb_utils
from src.debug_logger import logger as dbg


class SimplePhoneTranslator:
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

        self.pc_streamer = None
        self.ocr_receiver = None
        self.debug_receiver = None
        self.ocr_overlay = None
        self.translate_server = None
        self.settings_server = None

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
    # ОКНО НАСТРОЕК
    # ============================================================

    def open_settings_window(self):
        if self._settings_window is not None:
            try:
                if self._settings_window.winfo_exists():
                    self._settings_window.lift()
                    self._settings_window.focus_force()
                    return
            except Exception:
                self._settings_window = None

        w = tk.Toplevel(self.root)
        self._settings_window = w
        w.title("⚙ Настройки")
        w.geometry("820x780")
        w.minsize(700, 600)
        w.configure(bg="#1e1e1e")
        w.transient(self.root)
        w.protocol("WM_DELETE_WINDOW", self._close_settings_window)

        header = tk.Frame(w, bg="#1e1e1e")
        header.pack(fill=tk.X, padx=20, pady=(20, 8))
        tk.Label(header, text="⚙ Настройки", font=("Arial", 18, "bold"),
                 bg="#1e1e1e", fg="#4CAF50").pack(side=tk.LEFT)

        tk.Frame(w, bg="#3c3c3c", height=1).pack(fill=tk.X, padx=20)

        canvas = tk.Canvas(w, bg="#1e1e1e", highlightthickness=0)
        scrollbar = tk.Scrollbar(w, orient="vertical", command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg="#1e1e1e")

        scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas_window_id = canvas.create_window(
            (0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfig(canvas_window_id, width=e.width)
        )

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                    padx=(20, 0), pady=12)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, pady=12)

        def _on_mousewheel(event):
            try:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            except Exception:
                pass

        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        self._build_settings_controls(scroll_frame)
        self._sync_status_on_open()

        bottom = tk.Frame(w, bg="#1e1e1e")
        bottom.pack(fill=tk.X, padx=20, pady=(0, 20))

        help_btn = tk.Button(
            bottom, text="❓ Инструкция", command=self.show_help_window,
            font=("Arial", 11), bg="#607D8B", fg="white",
            relief=tk.FLAT, padx=20, pady=8, cursor="hand2"
        )
        help_btn.pack(side=tk.LEFT)

        close_btn = tk.Button(
            bottom, text="Закрыть", command=self._close_settings_window,
            font=("Arial", 11, "bold"), bg="#4CAF50", fg="white",
            relief=tk.FLAT, padx=40, pady=8, cursor="hand2"
        )
        close_btn.pack(side=tk.RIGHT)

    def _close_settings_window(self):
        try:
            self.root.unbind_all("<MouseWheel>")
        except Exception:
            pass

        if self._settings_window is not None:
            try:
                self._settings_window.destroy()
            except Exception:
                pass
            self._settings_window = None
            self._settings_window_widgets = {}

        self.adb_status = None
        self.window_status = None
        self.stream_status = None

    def _reset_settings(self):
        settings_manager.reset_to_defaults()
        self._push_settings_to_android()
        self._close_settings_window()
        self.root.after(50, self.open_settings_window)

    def _sync_status_on_open(self):
        self._update_status_labels()

        try:
            if self.window_status is not None:
                if self.pc_streamer is not None and self.pc_streamer.hwnd:
                    title = self.pc_streamer.window_title or "?"
                    short = title if len(title) <= 40 else title[:37] + "..."
                    self.window_status.config(text=f"🟢 Окно: {short}")
                else:
                    self.window_status.config(text="🪟 Окно: активное (авто)")
        except Exception:
            pass

        try:
            if self.stream_status is not None:
                if self.streaming:
                    self.stream_status.config(text="🟢 Трансляция: запущена")
                else:
                    self.stream_status.config(text="🔴 Трансляция: остановлена")
        except Exception:
            pass

    def _build_settings_controls(self, parent):
        wg = self._settings_window_widgets

        tk.Label(parent, text="Автозапуск", font=("Arial", 12, "bold"),
                 bg="#1e1e1e", fg="#2196F3").pack(anchor=tk.W, pady=(0, 6))

        auto_var = tk.BooleanVar(
            value=bool(settings_manager.get("auto_launch_android", True)))
        auto_cb = tk.Checkbutton(
            parent,
            text="Автоматически запускать Android-приложение",
            variable=auto_var,
            command=lambda: self._on_auto_launch_changed(auto_var.get()),
            bg="#1e1e1e", fg="#e0e0e0", selectcolor="#1e1e1e",
            activebackground="#1e1e1e", activeforeground="#e0e0e0",
            font=("Arial", 11), anchor=tk.W, justify=tk.LEFT
        )
        auto_cb.pack(anchor=tk.W, pady=(0, 12))
        wg["auto_launch_android"] = auto_cb

        tk.Frame(parent, bg="#3c3c3c", height=1).pack(fill=tk.X, pady=8)

        debug_var = tk.BooleanVar(value=bool(settings_manager.get("debug", False)))
        debug_cb = tk.Checkbutton(
            parent, text="Debug (показать OCR-рамки)", variable=debug_var,
            command=lambda: self._on_debug_changed(debug_var.get()),
            bg="#1e1e1e", fg="#e0e0e0", selectcolor="#1e1e1e",
            activebackground="#1e1e1e", activeforeground="#e0e0e0",
            font=("Arial", 11), anchor=tk.W
        )
        debug_cb.pack(anchor=tk.W, pady=(4, 10))
        wg["debug"] = debug_cb

        tk.Label(parent, text="Качество перевода:", font=("Arial", 11, "bold"),
                 bg="#1e1e1e", fg="#e0e0e0").pack(anchor=tk.W, pady=(8, 4))

        current_profile = settings_manager.get("translate_profile", "balanced")
        if current_profile not in TRANSLATE_PROFILE_LABELS:
            current_profile = "balanced"
        current_label = TRANSLATE_PROFILE_LABELS[current_profile]

        profile_values = [TRANSLATE_PROFILE_LABELS[p]
                          for p in TRANSLATE_PROFILES
                          if p in TRANSLATE_PROFILE_LABELS]

        profile_var = tk.StringVar(value=current_label)
        profile_combo = ttk.Combobox(
            parent, textvariable=profile_var, values=profile_values,
            font=("Arial", 11)
        )
        profile_combo.pack(fill=tk.X, pady=(0, 10))
        profile_combo.set(current_label)
        profile_combo.config(state="readonly")
        profile_combo.bind(
            "<<ComboboxSelected>>",
            lambda e: self._on_profile_changed(profile_combo.get())
        )
        wg["translate_profile"] = profile_combo

        self._build_slider(parent, "confidence",
                           "Порог уверенности", 0, 100, "%",
                           settings_manager.get("confidence", 0))
        self._build_slider(parent, "ocr_padding",
                           "Отступ OCR", 0, 50, "%",
                           settings_manager.get("ocr_padding", 0))
        self._build_slider(parent, "overlay_alpha",
                           "Прозрачность фона перевода", 0, 100, "%",
                           settings_manager.get("overlay_alpha", 0))

        tk.Frame(parent, bg="#3c3c3c", height=1).pack(fill=tk.X, pady=8)
        tk.Label(parent, text="Исходный язык:", font=("Arial", 11, "bold"),
                 bg="#1e1e1e", fg="#e0e0e0").pack(anchor=tk.W, pady=(4, 4))
        cur_src = settings_manager.get("source_lang", "auto")
        if cur_src not in LANGUAGES:
            cur_src = "auto"
        src_var = tk.StringVar(value=cur_src)
        src_combo = ttk.Combobox(parent, textvariable=src_var,
                                 values=LANGUAGES, font=("Arial", 11))
        src_combo.pack(fill=tk.X, pady=(0, 10))
        src_combo.set(cur_src)
        src_combo.config(state="readonly")
        src_combo.bind("<<ComboboxSelected>>",
                       lambda e: self._on_setting_widget_changed(
                           "source_lang", src_var.get()))
        wg["source_lang"] = src_combo

        tk.Label(parent, text="Язык перевода:", font=("Arial", 11, "bold"),
                 bg="#1e1e1e", fg="#e0e0e0").pack(anchor=tk.W, pady=(4, 4))
        cur_tgt = settings_manager.get("target_lang", "ru")
        tgt_values = [l for l in LANGUAGES if l != "auto"]
        if cur_tgt not in tgt_values:
            cur_tgt = "ru"
        tgt_var = tk.StringVar(value=cur_tgt)
        tgt_combo = ttk.Combobox(parent, textvariable=tgt_var,
                                 values=tgt_values, font=("Arial", 11))
        tgt_combo.pack(fill=tk.X, pady=(0, 10))
        tgt_combo.set(cur_tgt)
        tgt_combo.config(state="readonly")
        tgt_combo.bind("<<ComboboxSelected>>",
                       lambda e: self._on_setting_widget_changed(
                           "target_lang", tgt_var.get()))
        wg["target_lang"] = tgt_combo

        tk.Frame(parent, bg="#3c3c3c", height=1).pack(fill=tk.X, pady=8)
        tk.Label(parent, text="Статус", font=("Arial", 12, "bold"),
                 bg="#1e1e1e", fg="#2196F3").pack(anchor=tk.W, pady=(4, 6))

        self.adb_status = tk.Label(parent, text="🔴 ADB: не выбрано",
                                   font=("Arial", 11), bg="#1e1e1e",
                                   fg="#e0e0e0", anchor=tk.W)
        self.adb_status.pack(anchor=tk.W, pady=2)

        self.refresh_btn = tk.Button(
            parent, text="🔄 Обновить устройства",
            command=self.refresh_devices_manual,
            font=("Arial", 10), bg="#2196F3", fg="white",
            relief=tk.FLAT, padx=10, pady=4, cursor="hand2"
        )
        self.refresh_btn.pack(anchor=tk.W, pady=(4, 8))

        self.window_status = tk.Label(parent, text="🪟 Окно: активное (авто)",
                                      font=("Arial", 11), bg="#1e1e1e",
                                      fg="#e0e0e0", anchor=tk.W)
        self.window_status.pack(anchor=tk.W, pady=2)

        self.stream_status = tk.Label(parent, text="🔴 Трансляция: остановлена",
                                      font=("Arial", 11), bg="#1e1e1e",
                                      fg="#e0e0e0", anchor=tk.W)
        self.stream_status.pack(anchor=tk.W, pady=2)

        tk.Frame(parent, bg="#3c3c3c", height=1).pack(fill=tk.X, pady=12)
        reset_btn = tk.Button(
            parent, text="↺ Сбросить настройки", command=self._reset_settings,
            font=("Arial", 11), bg="#FF9800", fg="white",
            relief=tk.FLAT, padx=20, pady=8, cursor="hand2"
        )
        reset_btn.pack(anchor=tk.W, pady=(0, 8))

    def _build_slider(self, parent, key, label, from_, to, suffix, value):
        wg = self._settings_window_widgets
        frame = tk.Frame(parent, bg="#1e1e1e")
        frame.pack(fill=tk.X, pady=(10, 0))

        value_label = tk.Label(frame, text=f"{label}: {value}{suffix}",
                               font=("Arial", 11), bg="#1e1e1e", fg="#e0e0e0")
        value_label.pack(anchor=tk.W)

        var = tk.IntVar(value=int(value))
        slider = tk.Scale(
            frame, from_=from_, to=to, orient=tk.HORIZONTAL,
            variable=var, showvalue=False,
            bg="#1e1e1e", fg="#e0e0e0", troughcolor="#3c3c3c",
            highlightthickness=0, bd=0, sliderrelief=tk.FLAT,
            activebackground="#4CAF50",
            command=lambda v: self._on_slider_changed(
                key, int(float(v)), value_label, label, suffix)
        )
        slider.pack(fill=tk.X, pady=(4, 0))

        wg[key] = {"var": var, "slider": slider, "label": value_label,
                   "text": label, "suffix": suffix}

    def _on_slider_changed(self, key, value, value_label, label, suffix):
        if self._suppress_slider_events:
            return
        value_label.config(text=f"{label}: {value}{suffix}")
        self._on_setting_widget_changed(key, value)

    # ============================================================
    # ОБРАБОТЧИКИ
    # ============================================================

    def _on_debug_changed(self, value):
        self._on_setting_widget_changed("debug", bool(value))

    def _on_auto_launch_changed(self, value):
        self._on_setting_widget_changed("auto_launch_android", bool(value))

    def _on_profile_changed(self, label):
        for k, v in TRANSLATE_PROFILE_LABELS.items():
            if v == label:
                self._on_setting_widget_changed("translate_profile", k)
                return

    def _on_setting_widget_changed(self, key, value):
        changed = settings_manager.set(key, value)
        if changed:
            self._schedule_push_settings()

    def _schedule_push_settings(self):
        if self._push_timer is not None:
            try:
                self.root.after_cancel(self._push_timer)
            except Exception:
                pass
        self._push_timer = self.root.after(
            self._push_delay_ms, self._push_settings_to_android)

    def _set_combo_value(self, combo, value):
        try:
            combo.config(state="normal")
            combo.set(value)
            combo.config(state="readonly")
        except Exception:
            pass

    def _on_setting_changed(self, key, value):
        if self._settings_window is None:
            return
        try:
            if not self._settings_window.winfo_exists():
                return
        except Exception:
            return

        wg = self._settings_window_widgets
        self._suppress_slider_events = True
        try:
            if key == "translate_profile":
                label = TRANSLATE_PROFILE_LABELS.get(value)
                if label and "translate_profile" in wg:
                    self._set_combo_value(wg["translate_profile"], label)
            elif key == "source_lang":
                if "source_lang" in wg:
                    self._set_combo_value(wg["source_lang"], value)
            elif key == "target_lang":
                if "target_lang" in wg:
                    self._set_combo_value(wg["target_lang"], value)
            elif key in wg and isinstance(wg[key], dict) and "var" in wg[key]:
                try:
                    wg[key]["var"].set(int(value))
                except Exception:
                    pass
                try:
                    wg[key]["label"].config(
                        text=f"{wg[key]['text']}: {value}{wg[key]['suffix']}")
                except Exception:
                    pass
        finally:
            self._suppress_slider_events = False

    def _push_settings_to_android(self):
        self._push_timer = None
        if not self.selected_device:
            return
        try:
            json_str = settings_manager.to_json()
            threading.Thread(
                target=adb_utils.push_settings_to_android,
                args=(self.selected_device, json_str),
                daemon=True
            ).start()
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
                self._update_status_labels()
                self._update_start_btn_state()
                return

            usb_devices = [d for d in devices if ':' not in d]
            if usb_devices:
                new_device = usb_devices[0]
                if new_device != self.selected_device:
                    self.selected_device = new_device
                    self.log(f"✅ Найдено USB устройство: {new_device}")
                self.adb_ready = True
                self._update_status_labels()
                self._update_start_btn_state()
                return

            new_device = devices[0]
            if new_device != self.selected_device:
                self.selected_device = new_device
                self.log(f"✅ Найдено устройство: {new_device}")
            self.adb_ready = True
            self._update_status_labels()
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
        if self.streaming:
            try:
                self.start_btn.config(text="⏹\nОСТАНОВИТЬ", bg="#f44336")
            except Exception:
                pass
            return

        if self.adb_ready and self.selected_device is not None:
            try:
                self.start_btn.config(text="▶\nНАЧАТЬ", bg="#4CAF50")
            except Exception:
                pass
        else:
            try:
                self.start_btn.config(text="▶\nНАЧАТЬ", bg="#607D8B")
            except Exception:
                pass

    # ============================================================
    # ОКНО-ИНСТРУКЦИЯ ПО ПОДКЛЮЧЕНИЮ
    # ============================================================

    def _show_connect_instructions(self):
        if self._connect_window is not None:
            try:
                if self._connect_window.winfo_exists():
                    self._connect_window.lift()
                    self._connect_window.focus_force()
                    return
            except Exception:
                self._connect_window = None

        w = tk.Toplevel(self.root)
        self._connect_window = w
        w.title("📱 Подключение Android-устройства")
        w.geometry("640x520")
        w.minsize(520, 420)
        w.configure(bg="#1e1e1e")
        w.transient(self.root)
        w.protocol("WM_DELETE_WINDOW", self._close_connect_window)

        header = tk.Label(w, text="📱 Как подключить Android-устройство",
                          font=("Arial", 15, "bold"),
                          bg="#1e1e1e", fg="#4CAF50")
        header.pack(pady=(18, 6))

        tk.Frame(w, bg="#3c3c3c", height=1).pack(fill=tk.X, padx=20)

        text_frame = tk.Frame(w, bg="#1e1e1e")
        text_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=12)

        scrollbar = tk.Scrollbar(text_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        help_text = tk.Text(text_frame, wrap=tk.WORD,
                            font=("Segoe UI", 10),
                            bg="#2d2d2d", fg="#e0e0e0",
                            insertbackground="white",
                            relief=tk.FLAT, padx=15, pady=10,
                            yscrollcommand=scrollbar.set)
        help_text.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=help_text.yview)

        help_text.tag_configure("h1", font=("Segoe UI", 13, "bold"),
                                foreground="#4CAF50",
                                spacing1=10, spacing3=5)
        help_text.tag_configure("h2", font=("Segoe UI", 11, "bold"),
                                foreground="#2196F3",
                                spacing1=8, spacing3=3)
        help_text.tag_configure("ok", foreground="#4CAF50")
        help_text.tag_configure("warn", foreground="#FF9800")

        lines = [
            ("h1", "Шаг 1. Включите отладку по USB на телефоне"),
            ("", "Откройте «Настройки» → «О телефоне» → 7 раз нажмите"),
            ("", "на «Номер сборки». Появится раздел «Для разработчиков»."),
            ("", "Зайдите в «Для разработчиков» и включите"),
            ("", "«Отладка по USB» (USB debugging)."),
            ("", ""),
            ("h1", "Шаг 2. Подключите телефон к ПК кабелем"),
            ("", "Важно: кабель должен поддерживать передачу данных,"),
            ("", "а не только зарядку. Оригинальный кабель — лучший выбор."),
            ("", ""),
            ("h1", "Шаг 3. Разрешите отладку на телефоне"),
            ("", "При первом подключении телефон покажет диалог"),
            ("", "«Разрешить отладку по USB?». Нажмите «Разрешить»."),
            ("", "Если галочка «Всегда разрешать с этого компьютера» —"),
            ("", "поставьте её, чтобы не подтверждать каждый раз."),
            ("", ""),
            ("ok", "После подключения нажмите НАЧАТЬ ещё раз."),
            ("ok", "Если приложения нет — программа предложит его установить."),
        ]

        for marker, line in lines:
            if marker == "h1":
                help_text.insert(tk.END, line + "\n", "h1")
            elif marker == "h2":
                help_text.insert(tk.END, line + "\n", "h2")
            elif marker == "ok":
                help_text.insert(tk.END, line + "\n", "ok")
            elif marker == "warn":
                help_text.insert(tk.END, line + "\n", "warn")
            else:
                help_text.insert(tk.END, line + "\n")

        help_text.config(state=tk.DISABLED)

        bottom = tk.Frame(w, bg="#1e1e1e")
        bottom.pack(fill=tk.X, padx=20, pady=(0, 16))

        refresh_btn = tk.Button(
            bottom, text="🔄 Проверить подключение",
            command=self._check_connect_and_maybe_close,
            font=("Arial", 11), bg="#2196F3", fg="white",
            relief=tk.FLAT, padx=20, pady=8, cursor="hand2"
        )
        refresh_btn.pack(side=tk.LEFT)

        close_btn = tk.Button(
            bottom, text="Закрыть",
            command=self._close_connect_window,
            font=("Arial", 11, "bold"), bg="#4CAF50", fg="white",
            relief=tk.FLAT, padx=30, pady=8, cursor="hand2"
        )
        close_btn.pack(side=tk.RIGHT)

        w.bind("<Escape>", lambda e: self._close_connect_window())
        w.focus_force()

    def _close_connect_window(self):
        if self._connect_window is not None:
            try:
                self._connect_window.destroy()
            except Exception:
                pass
            self._connect_window = None

    def _check_connect_and_maybe_close(self):
        self.refresh_devices_manual()
        if self.adb_ready and self.selected_device:
            self.log("✅ Устройство подключено")
            self._close_connect_window()

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
        # ИЗМЕНЕНО: "Проверка APK" → "Установка APK".
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
        self._update_start_btn_state()

        if ok and new_hash:
            try:
                changed = settings_manager.set(
                    "last_installed_apk_hash", new_hash)
                if changed:
                    self.log(f"💾 Запомнен хэш APK: {new_hash[:12]}…")
            except Exception:
                pass

        if ok:
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
        else:
            self.start_streaming()

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
            if known_hash:
                current_hash = adb_utils.apk_sha256(apk_path)
                if current_hash and current_hash == known_hash:
                    self.log("✅ APK актуален — старт")
                    self._start_streaming_after_checks()
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
                self._start_streaming_after_checks()
                return

            def _after_update(ok, action, msg, new_hash):
                if ok:
                    self._start_streaming_after_checks()
                else:
                    messagebox.showerror(
                        "Ошибка обновления",
                        f"Не удалось обновить APK:\n\n{msg}",
                        parent=self.root,
                    )

            self._ensure_app_async(apk_path, known_hash, _after_update)
            return

        answer = messagebox.askyesno(
            "Установить приложение?",
            f"На Android-устройстве не найдено приложение ScreenTranslator.\n\n"
            f"APK: {apk_size_mb:.1f} МБ\n\n"
            f"Установить его сейчас через ADB?",
            parent=self.root,
        )
        if not answer:
            self.log("ℹ️ Установка отменена пользователем")
            return

        def _after_install(ok, action, msg, new_hash):
            if ok:
                self._start_streaming_after_checks()
            else:
                messagebox.showerror(
                    "Ошибка установки",
                    f"Не удалось установить APK:\n\n{msg}",
                    parent=self.root,
                )

        self._ensure_app_async(apk_path, "", _after_install)

    def _start_streaming_after_checks(self):
        self._reset_android_stopped_flag()

        if bool(settings_manager.get("auto_launch_android", True)):
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

            # ИЗМЕНЕНО: проверяем, не создан ли уже оверлей.
            # Если создан и окно существует — переиспользуем его,
            # а не создаём новое. Это устраняет "несколько оверлеев".
            if self.ocr_overlay is not None:
                try:
                    if self.ocr_overlay.window.winfo_exists():
                        # Переиспользуем существующий оверлей
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
    # ИНСТРУКЦИЯ (общая)
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

        header = tk.Label(self._help_window,
                          text="📖 Инструкция по использованию",
                          font=("Arial", 16, "bold"),
                          bg="#1e1e1e", fg="#4CAF50")
        header.pack(pady=(15, 5))

        tk.Frame(self._help_window, bg="#3c3c3c",
                 height=1).pack(fill=tk.X, padx=20)

        text_frame = tk.Frame(self._help_window, bg="#1e1e1e")
        text_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        scrollbar = tk.Scrollbar(text_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        help_text = tk.Text(text_frame, wrap=tk.WORD, font=("Segoe UI", 10),
                            bg="#2d2d2d", fg="#e0e0e0",
                            insertbackground="white",
                            relief=tk.FLAT, padx=15, pady=10,
                            yscrollcommand=scrollbar.set)
        help_text.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=help_text.yview)

        help_text.tag_configure("h1", font=("Segoe UI", 13, "bold"),
                                foreground="#4CAF50",
                                spacing1=10, spacing3=5)
        help_text.tag_configure("h2", font=("Segoe UI", 11, "bold"),
                                foreground="#2196F3",
                                spacing1=8, spacing3=3)
        help_text.tag_configure("ok", foreground="#4CAF50")
        help_text.tag_configure("warn", foreground="#FF9800")
        help_text.tag_configure("err", foreground="#F44336")

        self._insert_help_text(help_text)
        help_text.config(state=tk.DISABLED)

        close_btn = tk.Button(self._help_window, text="Закрыть",
                              command=self._close_help_window,
                              font=("Arial", 10, "bold"), bg="#4CAF50",
                              fg="white", relief=tk.FLAT, padx=30, pady=8,
                              cursor="hand2")
        close_btn.pack(pady=(0, 15))

        self._help_window.bind("<Escape>",
                               lambda e: self._close_help_window())
        self._help_window.protocol("WM_DELETE_WINDOW",
                                   self._close_help_window)
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
            ("===H1===", "Как пользоваться"),
            ("", ""),
            ("===H2===", "1. Подключите телефон к ПК по USB"),
            ("", "На телефоне должно быть разрешено \"Отладка по USB\"."),
            ("", "Подробнее — нажмите НАЧАТЬ без подключённого телефона."),
            ("", ""),
            ("===H2===", "2. Нажмите \"▶ НАЧАТЬ\""),
            ("", "Если устройство не подключено — откроется инструкция."),
            ("", "Если приложения нет — программа предложит установить APK."),
            ("", "Если APK изменился — предложит обновить."),
            ("", "Если APK тот же — сразу запустится трансляция."),
            ("", ""),
            ("===H2===", "3. Перевод появится на ПК"),
            ("", "Поверх исходного окна появится перевод."),
            ("", "Просто переключите фокус на нужное окно —"),
            ("", "трансляция подхватит его автоматически."),
            ("", ""),
            ("===H2===", "4. Остановка"),
            ("", "При нажатии \"⏹ ОСТАНОВИТЬ\" или закрытии программы"),
            ("", "OCR на телефоне автоматически выключится."),
            ("", ""),
            ("===H1===", "⚠️ Важные ограничения"),
            ("", ""),
            ("===H2===", "1. Захват только в режиме \"окно без рамки\""),
            ("", "Программа не может корректно захватывать приложения,"),
            ("", "работающие в эксклюзивном полноэкранном режиме"),
            ("", "(DirectX/OpenGL). В этом случае захват даёт чёрный экран."),
            ("", ""),
            ("", "Решение: запускайте приложения в режиме \"окно без рамки\"."),
            ("", "Автопереключение удалено из программы."),
            ("", ""),
            ("===H2===", "2. Права администратора: они должны СОВПАДАТЬ у обоих приложений"),
            ("", ""),
            ("===WARN===", "Это касается ОБОИХ приложений:"),
            ("", "• нашей программы (которая захватывает экран),"),
            ("", "• целевого приложения (игры, браузера, программы),"),
            ("", "  окно которого мы захватываем."),
            ("", ""),
            ("", "В Windows действует защита UIPI (User Interface Privilege Isolation):"),
            ("", "процесс с низкими правами НЕ МОЖЕТ захватить окно процесса"),
            ("", "с более высокими правами. Проще говоря:"),
            ("", ""),
            ("===ERR===", "❌ Если целевое приложение запущено ОТ АДМИНИСТРАТОРА,"),
            ("===ERR===", "   а наша программа — БЕЗ админа — будет ЧЁРНЫЙ ЭКРАН."),
            ("", ""),
            ("===OK===", "✅ Если права совпадают (оба с админом или оба без) — всё работает."),
            ("", ""),
            ("===H2===", "Что делать?"),
            ("", "Вариант 1 (рекомендуется): запускайте ОБЕ программы БЕЗ админа."),
            ("", "  Это безопаснее и работает в 90% случаев."),
            ("", ""),
            ("", "Вариант 2: если целевое приложение требует админа"),
            ("", "  (например, некоторые игры с античитом или системные утилиты),"),
            ("", "  тогда запустите и нашу программу от имени администратора."),
            ("", ""),
            ("===OK===", "Правило: права у обоих приложений должны быть ОДИНАКОВЫМИ."),
            ("", ""),
            ("===H1===", "Настройки (шестерёнка ⚙)"),
            ("", ""),
            ("===H2===", "Автоматически запускать Android-приложение"),
            ("", "Если включено — при нажатии НАЧАТЬ на телефоне"),
            ("", "автоматически откроется приложение и запустится OCR."),
            ("", ""),
            ("===H2===", "Debug (показать OCR-рамки)"),
            ("", "Показывает рамки распознанных блоков."),
            ("", ""),
            ("===H2===", "Качество перевода"),
            ("", "Скорость — самый быстрый режим, минимальная задержка."),
            ("", "Баланс — компромисс между скоростью и качеством."),
            ("", "Качество — самый точный перевод, но медленнее."),
            ("", ""),
            ("===H1===", "Настройки сохраняются автоматически"),
            ("", ""),
            ("", "Все настройки сохраняются в файл settings.json"),
            ("", "рядом с программой и применяются при следующем запуске."),
        ]

        for marker, line in lines:
            if marker == "===H1===":
                text_widget.insert(tk.END, line + "\n", "h1")
            elif marker == "===H2===":
                text_widget.insert(tk.END, line + "\n", "h2")
            elif marker == "===WARN===":
                text_widget.insert(tk.END, line + "\n", "warn")
            elif marker == "===OK===":
                text_widget.insert(tk.END, line + "\n", "ok")
            elif marker == "===ERR===":
                text_widget.insert(tk.END, line + "\n", "err")
            else:
                text_widget.insert(tk.END, line + "\n")

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