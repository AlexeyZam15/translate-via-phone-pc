# src/gui_help_windows.py
"""Миксин HelpWindowsMixin — окна-инструкции.

Содержит методы:
  - _show_connect_instructions / _close_connect_window
    / _check_connect_and_maybe_close
  - show_help_window / _close_help_window / _insert_help_text

Все методы используют поля, созданные в SimplePhoneTranslator.__init__:
  self.root, self._connect_window, self._help_window,
  self.adb_ready, self.selected_device, self.log.
"""

import tkinter as tk

from src.debug_logger import logger as dbg


class HelpWindowsMixin:
    """Окна-инструкции: подключение Android и общая справка."""

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
            ("ok", "Если приложения нет — кнопка станет «УСТАНОВИТЬ»."),
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
    # ОБЩАЯ ИНСТРУКЦИЯ
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
            ("", "Если приложения нет — кнопка станет \"⤓ УСТАНОВИТЬ\"."),
            ("", "Нажмите её, дождитесь установки, затем нажмите НАЧАТЬ."),
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