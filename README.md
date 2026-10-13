# Translate via Phone — ПК-компонент

Клиент-серверная система синхронизации текста между ПК и Android-устройством.
ПК-компонент выполняет захват активного окна Windows, передачу видеопотока
на Android-устройство, приём результатов OCR, машинный перевод и отображение
перевода в прозрачном оверлее поверх исходного окна.

Связанный компонент: Android-приложение ScreenTranslator2 (см. отдельный README).

---

## Назначение

Система предназначена для перевода текста в произвольных окнах Windows
(игры, браузеры, прикладные программы), не предоставляющих API для перевода.
Архитектурное решение:

1. ПК захватывает активное окно и передаёт видеопоток на Android-устройство.
2. Android-устройство выполняет OCR средствами ML Kit и передаёт
   распознанные текстовые блоки на ПК.
3. ПК выполняет машинный перевод (CTranslate2, модель Opus-MT INT8)
   и отображает результат в оверлее.

OCR вынесен на Android по причине более высокого качества распознавания
UI-текста средствами ML Kit по сравнению с Tesseract/EasyOCR, а также
отсутствия требований к GPU на стороне ПК. ПК-компонент отвечает
за захват, транспорт, перевод и отображение.

---

## Механизм выбора окна

Захватывается активное окно (foreground window). Ручной выбор окна
не предусмотрен. Фоновый поток window_watcher_loop с интервалом 0.3 с
запрашивает текущее foreground-окно и при изменении переключает захват.

Обоснование решения:

- Захват автоматически следует за сменой фокуса без вмешательства пользователя.
- В сценариях с частой сменой окон (лаунчер, игра, меню) ручной выбор
  потребовал бы повторной настройки.
- Отсутствует необходимость хранить состояние выбранного окна между запусками.

Собственные окна приложения, консоли и терминалы исключены из захвата
списками EXCLUDED_TITLES, EXCLUDED_CLASSES, EXCLUDED_PROCESSES
в window_utils.py, а также списком HWND собственных окон (_get_our_hwnds()).

---

## Функциональные возможности

Захват и передача:

- Захват клиентской области активного окна (без рамки и заголовка)
  через PrintWindow с fallback на BitBlt.
- Автоматическое отслеживание смены активного окна.
- Предварительная конвертация в grayscale перед JPEG-кодированием
  для повышения качества OCR.
- MJPEG-поток (порт 5005) с SO_KEEPALIVE и TCP_NODELAY.

Приём данных:

- HTTP-сервер приёма OCR-результатов (порт 5006) с дедупликацией
  по фингерпринту кадра.
- HTTP-сервер приёма debug-метрик от Android (порт 5007).

Перевод и настройки:

- HTTP-сервер перевода CTranslate2 + Opus-MT INT8 (порт 5008).
- HTTP-сервер настроек с двусторонней синхронизацией (порт 5009).
- Профили качества перевода: speed, balanced, quality.

Отображение:

- Прозрачный оверлей на Tkinter поверх целевого окна.
- Стабилизация блоков между кадрами (IoU, containment, miss_count).
- Подбор размера шрифта под габариты блока.
- Фильтрация нерелевантных блоков (HUD, фрагменты, смешанные алфавиты).
- Режимы отображения: translate (перевод) и debug (рамки, confidence).

Инфраструктура:

- Автоматическая настройка adb reverse для пяти портов (параллельно).
- Версионный контроль APK по SHA-256.
- Автозапуск Android-приложения через ADB.
- Миграция схемы settings.json.
- Debug-логгер с агрегацией таймингов (avg/min/max/p95).
- Текстовый лог ocr_text.log (OCR, TR-IN, TR-OUT).
- Singleton: завершение предыдущих экземпляров при старте.
- Сборка в .exe через PyInstaller с включением adb.exe и APK.

---

## Архитектура

```

┌────────────────────────── ПК (Python) ──────────────────────────┐
│ │
│ WindowStreamer (pc_streamer.py)                                 │
│ ├─ window_watcher_loop: отслеживание foreground-окна (0.3 с)  │
│ ├─ capture_loop: PrintWindow → numpy (клиентская область)     │
│ ├─ encode_loop: grayscale → JPEG │
│ └─ HTTP /stream :5005 ──────────────┐ │
│ │ MJPEG │
│ OcrReceiver (ocr_receiver.py)           │ │
│ └─ HTTP POST /ocr :5006 ◄────────────┼──── блоки OCR │
│ │ │
│ TranslateServer (translate_server.py)   │ │
│ └─ HTTP POST /translate :5008 ◄───────┼──── тексты на перевод │
│ │ │
│ SettingsServer (settings_server.py)     │ │
│ └─ HTTP GET/POST /settings :5009 ◄────┼──── настройки │
│ │ │
│ DebugReceiver (ocr_receiver.py)         │ │
│ └─ HTTP POST /debug :5007 ◄───────────┘ │
│ │
│ OcrOverlayWindow (ocr_overlay.py + миксины)                     │
│ └─ Tkinter Toplevel поверх целевого окна │
│ │
│ SimplePhoneTranslator (simple_gui.py) — GUI, оркестрация │
└──────────────────────────────────────────────────────────────────┘
▲
│ ADB reverse tcp:5005..5009
▼
┌──────────────────── Android (ScreenTranslator2) ────────────────┐
│ MJPEGReader → ML Kit OCR → OcrBlockStabilizer → OcrSender │
│ BenchmarkTranslator (HTTP :5008 + ML Kit fallback)             │
│ PcOcrService — основной сервис │
└──────────────────────────────────────────────────────────────────┘

```

### Последовательность обработки кадра

1. window_watcher_loop определяет активное окно.
2. capture_loop захватывает клиентскую область окна (PrintWindow).
3. encode_loop конвертирует изображение в grayscale и кодирует JPEG.
4. MJPEG-сервер передаёт кадр на Android-устройство.
5. Android: MJPEGReader принимает кадр, MlKitOcrEngine выполняет OCR.
6. Android: OcrBlockStabilizer стабилизирует блоки, OcrSender
   отправляет JSON на ПК (POST /ocr).
7. OcrReceiver принимает блоки, дедуплицирует, сохраняет в памяти.
8. OcrOverlayWindow в цикле (_poll, 30 мс) извлекает блоки,
   стабилизирует, фильтрует и отображает.
9. В режиме translate Android отправляет тексты на POST /translate,
   получает перевод, отображает локально; ПК параллельно может
   отображать перевод в оверлее.

---

## Технологический стек

Язык: Python 3.10+

GUI: Tkinter, ttk.Combobox, tk.Canvas

Захват экрана: pywin32 (win32gui, win32ui, win32con, win32api,
win32process), ctypes.windll.user32.PrintWindow

Обработка изображений: OpenCV (cv2), NumPy, Pillow

HTTP: http.server.ThreadingHTTPServer, socketserver, socket

Машинный перевод: ctranslate2, sentencepiece
(модель Opus-MT en→ru INT8)

ADB: subprocess, adb.exe (Android Platform Tools)

Сборка: PyInstaller (--windowed, --noconsole, UPX)

Вспомогательные: hashlib, json, threading, queue,
concurrent.futures, psutil

---

## Структура проекта

```

translate_via_phone_2/
├── main.py # точка входа, singleton, debug.log
├── build_exe.py # сборка .exe (PyInstaller)
├── window_borderless.py # отдельная утилита: F10 → borderless
├── test_translators.py # сравнение argos / ct2 / libretranslate
├── test_ct2_params.py # бенчмарк параметров CTranslate2
├── app_icon.ico
├── app-debug.apk # APK для установки на устройство
├── settings.json # настройки (создаётся автоматически)
├── ocr_text.log # лог OCR / TR-IN / TR-OUT
├── debug.log # stdout/stderr, события
├── debug_timing.jsonl # тайминги (JSONL)
├── models/
│ └── opus-mt-en-ru-ct2-int8-orig/
│ ├── model.bin
│ ├── config.json
│ ├── source.spm
│ └── target.spm
└── src/
├── __init__.py
├── config.py # константы, порты, дефолты
├── singleton.py # kill_other_instances()
├── adb_utils.py # adb: scan, install, launch, reverse
├── debug_logger.py # DebugLogger + агрегация таймингов
├── text_logger.py # ocr_text.log
├── settings_manager.py # источник истины settings.json
├── settings_server.py # HTTP-сервер настроек (5009)
├── translate_server.py # HTTP-сервер перевода (5008)
├── ocr_receiver.py # OcrReceiver (5006), DebugReceiver (5007)
├── pc_streamer.py # WindowStreamer (захват, MJPEG, adb)
├── overlay_constants.py # константы оверлея
├── overlay_utils.py # текстовые и геометрические утилиты
├── overlay_core.py # жизненный цикл оверлея, стабилизация
├── overlay_render.py # отрисовка оверлея
├── ocr_overlay.py # фасад OcrOverlayWindow (миксины)
├── simple_gui.py # главное окно, оркестрация
└── window_utils.py # перечисление и фильтрация окон Windows

```

---

## Порты

| Порт | Назначение                              |
|------|-----------------------------------------|
| 5005 | MJPEG-поток на Android                  |
| 5006 | Приём OCR-результатов от Android        |
| 5007 | Приём debug-метрик от Android           |
| 5008 | HTTP-сервер перевода (CTranslate2)      |
| 5009 | HTTP-сервер настроек                    |

Все порты пробрасываются на Android через adb reverse.

---

## Конфигурация

Файл settings.json (в корне проекта). Схема версионирована
(поле _schema_version). При несовпадении версии выполняется миграция:
берутся значения DEFAULT_SETTINGS, из старого файла переносятся только
ключи из SETTINGS_MIGRATION_PRESERVE_KEYS.

Ключевые параметры:

- debug — режим отображения OCR-рамок.
- translate_profile — speed, balanced, quality.
- confidence — порог уверенности OCR (0–100).
- ocr_padding — отступ OCR (0–50 процентов).
- overlay_alpha — прозрачность фона перевода (0–100 процентов).
- source_lang, target_lang — языки.
- auto_launch_android — автозапуск Android-приложения.
- last_installed_apk_hash — SHA-256 установленного APK.

---

## Сборка

Требования:

- Python 3.10+
- PyInstaller
- adb.exe (Android Platform Tools) в PATH или рядом с проектом
- app-debug.apk рядом с build_exe.py
- models/opus-mt-en-ru-ct2-int8-orig/ с файлами model.bin,
  config.json, source.spm, target.spm

Команда:

    python build_exe.py

Результат: dist/translate_via_phone/translate_via_phone.exe
с автоматически скопированными adb.exe и APK.

---

## Ограничения

- Захват невозможен для приложений в эксклюзивном полноэкранном
  режиме (DirectX/OpenGL) — результат чёрный экран. Требуется
  оконный режим без рамки.
- Права процессов должны совпадать: если целевое окно запущено
  от администратора, ПК-приложение также должно быть запущено
  от администратора (ограничение UIPI в Windows).
- adb reverse не работает при подключении устройства по Wi-Fi.

---

## Реализованные технические решения

- Захват клиентской области окна (GetClientRect + обрезка bitmap)
  вместо захвата всего окна, что устраняет смещение OCR-зон.
- Grayscale перед JPEG-кодированием для повышения контраста
  и снижения шума при OCR.
- Fallback с PrintWindow на BitBlt при неудаче PrintWindow.
- Дедупликация OCR-результатов по MD5-фингерпринту кадра.
- Стабилизация блоков между кадрами: удержание при частичном
  совпадении текста (containment, prefix match), сглаживание
  геометрии (POS_CHANGE_THRESHOLD, SIZE_CHANGE_THRESHOLD,
  BBOX_SETTLE_FRAMES).
- Разделение оверлея на миксины (overlay_core, overlay_render)
  с фасадом OcrOverlayWindow.
- Подбор размера шрифта под габариты блока: single-line,
  multi-line, truncate с эллипсисом.
- Фильтрация мусора: HUD (цифры + короткие буквы), фрагменты
  известных текстов, смешанные алфавиты, редкие символы,
  скопления согласных.
- Миграция схемы settings.json с сохранением ключей из
  SETTINGS_MIGRATION_PRESERVE_KEYS.
- Агрегация таймингов в debug-логгере по окну (avg/min/max/p95)
  вместо записи каждого события.
- Параллельная настройка adb reverse для пяти портов через
  ThreadPoolExecutor.
- Версионный контроль APK по SHA-256: установка только при
  изменении хэша; при расхождении — install -r, при неудаче —
  uninstall + install.
- Singleton-механизм kill_other_instances() для предотвращения
  дублирования окон, оверлеев и конфликтов портов.
- Текстовый лог с тремя каналами: OCR (распознанный текст),
  TR-IN (вход переводчика), TR-OUT (пары вход-выход), с
  блокировкой по хэшу.

---

## Зависимости между компонентами

- adb.exe требуется для: scan_devices, install_android_app,
  launch_android_app, force_stop_android_app, adb reverse,
  push_settings_to_android.
- Модель Opus-MT INT8 (models/opus-mt-en-ru-ct2-int8-orig)
  требуется для работы TranslateServer. Без неё сервер перевода
  не запускается, Android переходит на ML Kit fallback.
- app-debug.apk требуется для установки Android-приложения
  на устройство. Без него кнопка НАЧАТЬ выдаёт ошибку.