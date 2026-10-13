# src/gui_settings_window.py
"""Миксин SettingsWindowMixin — окно настроек.

Содержит методы:
  - open_settings_window / _close_settings_window / _reset_settings
  - _sync_status_on_open
  - _build_settings_controls / _build_slider / _on_slider_changed
  - _on_debug_changed / _on_auto_launch_changed / _on_profile_changed
  - _on_setting_widget_changed / _schedule_push_settings
  - _set_combo_value / _on_setting_changed
  - _push_settings_to_android

Все методы используют поля, созданные в SimplePhoneTranslator.__init__:
  self.root, self._settings_window, self._settings_window_widgets,
  self._suppress_slider_events, self._push_timer, self._push_delay_ms,
  self.adb_status, self.window_status, self.stream_status,
  self.pc_streamer, self.streaming, self.selected_device,
  self.log.
"""

import threading
import tkinter as tk
from tkinter import ttk

from src.config import (
    LANGUAGES,
    TRANSLATE_PROFILES,
    TRANSLATE_PROFILE_LABELS,
)
from src.settings_manager import settings_manager
from src import adb_utils
from src.debug_logger import logger as dbg


class SettingsWindowMixin:
    """Окно настроек и обработка изменений."""

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