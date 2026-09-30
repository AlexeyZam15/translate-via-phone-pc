# src/pc_streamer.py
"""Захват активного окна Windows через PrintWindow + MJPEG + ADB reverse.

ИЗМЕНЕНО:
  - set_active_hwnd НЕ сбрасывает current_frame_jpeg (клиент видит старый кадр).
  - MJPEG-стрим ловит ConnectionAbortedError/ConnectionResetError/BrokenPipeError.
  - SO_KEEPALIVE + TCP_NODELAY для HTTP-сервера.
  - Добавлен порт PC_SETTINGS_PORT (5009) в ADB reverse.
  - setup_adb_reverse теперь ПАРАЛЛЕЛЬНЫЙ.
  - INTER_AREA вместо INTER_LINEAR — меньше артефактов на тексте
    при уменьшении окна (важно для OCR).
  - capture_window_frame теперь захватывает КЛИЕНТСКУЮ область
    окна (GetClientRect), а не всё окно (GetWindowRect).
  - УДАЛЕНО: автопереключение окна в безрамочный режим
    (borderless fullscreen). Теперь стример не трогает стили окна.
"""

import cv2
import numpy as np
import ctypes
import socket
import threading
import time
import queue
import http.server
import socketserver
import subprocess
import sys
import concurrent.futures

import win32gui
import win32ui
import win32con
import win32api

from src.config import (
    PC_TO_PHONE_PORT,
    PC_OCR_PORT,
    PC_DEBUG_PORT,
    PC_TRANSLATE_PORT,
    PC_SETTINGS_PORT,
    FPS,
    JPEG_QUALITY,
    MAX_STREAM_WIDTH,
    FIRST_FRAME_TIMEOUT,
)
from src.debug_logger import logger as dbg
from src import window_utils
from src.settings_manager import settings_manager

WINDOW_WATCH_INTERVAL = 0.3


class WindowStreamer:
    """Трансляция активного окна через PrintWindow + MJPEG + ADB reverse."""

    def __init__(self):
        self.hwnd = None
        self.window_title = None
        self.selected_device = None

        self.streaming = False
        self.server = None
        self.server_thread = None

        self._our_hwnds = []
        self._our_hwnds_lock = threading.Lock()

        self._on_hwnd_changed = None

        self._gdi = {
            'hdc': None,
            'mfc_dc': None,
            'compatible_dc': None,
            'bitmap': None,
            'width': 0,
            'height': 0,
        }

        self.raw_frame_queue = queue.Queue(maxsize=2)

        self.frame_lock = threading.Lock()
        self.current_frame_jpeg = None
        self.current_frame_raw = None
        self.frame_counter = 0

        self.capture_thread = None
        self.encode_thread = None
        self.window_watcher_thread = None

        self._window_lost = False

    # ============================================================
    # Настройка
    # ============================================================

    def set_our_hwnds(self, hwnds):
        with self._our_hwnds_lock:
            self._our_hwnds = [h for h in hwnds if h]

    def set_on_hwnd_changed(self, callback):
        self._on_hwnd_changed = callback

    def _get_our_hwnds(self):
        with self._our_hwnds_lock:
            return list(self._our_hwnds)

    def set_active_hwnd(self, hwnd, title):
        """ИЗМЕНЕНО: не сбрасываем current_frame_jpeg — клиент видит старый кадр,
        пока новый не будет захвачен."""
        if hwnd == self.hwnd:
            return
        old = self.hwnd
        self.hwnd = hwnd
        self.window_title = title
        self._window_lost = False

        try:
            self._release_gdi()
        except Exception:
            pass

        print(f"[PC-Stream] 🔄 Активное окно: {title} (HWND={hwnd}, было {old})")
        dbg.log_event("pc.window_changed", {"hwnd": hwnd, "title": title, "old": old})
        if self._on_hwnd_changed is not None:
            try:
                self._on_hwnd_changed(hwnd, title)
            except Exception as e:
                print(f"[PC-Stream] ⚠️ on_hwnd_changed error: {e}")

    def set_device(self, device_serial):
        self.selected_device = device_serial
        print(f"[PC-Stream] ✅ Устройство: {device_serial}")
        dbg.log_event("pc.stream_set_device", {"serial": device_serial})

    def get_raw_frame(self):
        with self.frame_lock:
            if self.current_frame_raw is None:
                return None
            return self.current_frame_raw.copy()

    def get_jpeg(self):
        with self.frame_lock:
            return self.current_frame_jpeg

    # ============================================================
    # GDI
    # ============================================================

    def _release_gdi(self):
        gdi = self._gdi
        try:
            if gdi['compatible_dc']:
                gdi['compatible_dc'].DeleteDC()
            if gdi['mfc_dc']:
                gdi['mfc_dc'].DeleteDC()
            if gdi['hdc'] and self.hwnd:
                win32gui.ReleaseDC(self.hwnd, gdi['hdc'])
            if gdi['bitmap']:
                win32gui.DeleteObject(gdi['bitmap'].GetHandle())
        except Exception:
            pass

        self._gdi = {
            'hdc': None, 'mfc_dc': None, 'compatible_dc': None,
            'bitmap': None, 'width': 0, 'height': 0,
        }

    def _ensure_gdi(self, width, height):
        gdi = self._gdi
        if (gdi['hdc'] is not None
                and gdi['width'] == width
                and gdi['height'] == height):
            return True

        self._release_gdi()

        try:
            hdc = win32gui.GetDC(self.hwnd)
            mfc_dc = win32ui.CreateDCFromHandle(hdc)
            compatible_dc = mfc_dc.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
            compatible_dc.SelectObject(bitmap)

            self._gdi = {
                'hdc': hdc, 'mfc_dc': mfc_dc, 'compatible_dc': compatible_dc,
                'bitmap': bitmap, 'width': width, 'height': height,
            }
            return True
        except Exception as e:
            print(f"[PC-Stream] ⚠️ Ошибка создания GDI: {e}")
            dbg.log_event("pc.gdi_create_error", {"error": str(e)})
            self._release_gdi()
            return False

    def capture_window_frame(self):
        """
        Захватывает КЛИЕНТСКУЮ область окна (без рамки и заголовка).

        PrintWindow рисует всё окно в GDI-битмап размером win_w x win_h,
        затем numpy-массив обрезается до client_w x client_h со смещением
        (offset_x, offset_y). Это устраняет смещение OCR-зон, потому что
        OcrOverlayWindow позиционируется именно по клиентской области.
        """
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            return None

        try:
            window_rect = win32gui.GetWindowRect(self.hwnd)
            client_rect = win32gui.GetClientRect(self.hwnd)
        except Exception:
            return None

        win_w = window_rect[2] - window_rect[0]
        win_h = window_rect[3] - window_rect[1]
        client_w = client_rect[2] - client_rect[0]
        client_h = client_rect[3] - client_rect[1]

        if win_w <= 0 or win_h <= 0 or client_w <= 0 or client_h <= 0:
            return None

        try:
            client_left, client_top = win32gui.ClientToScreen(self.hwnd, (0, 0))
        except Exception:
            client_left, client_top = 0, 0

        offset_x = client_left - window_rect[0]
        offset_y = client_top - window_rect[1]

        if not self._ensure_gdi(win_w, win_h):
            return None

        gdi = self._gdi

        try:
            result = ctypes.windll.user32.PrintWindow(
                self.hwnd,
                gdi['compatible_dc'].GetSafeHdc(),
                0x2
            )
        except Exception:
            return None

        if not result:
            return self._capture_with_bitblt(
                win_w, win_h, client_w, client_h, offset_x, offset_y
            )

        try:
            bmpinfo = gdi['bitmap'].GetInfo()
            bmpstr = gdi['bitmap'].GetBitmapBits(True)

            img = np.frombuffer(bmpstr, dtype=np.uint8)
            img = img.reshape((bmpinfo['bmHeight'], bmpinfo['bmWidth'], 4))
            frame = img[:, :, :3].copy()
        except Exception:
            return None

        frame = frame[offset_y:offset_y + client_h, offset_x:offset_x + client_w]

        h, w = frame.shape[:2]
        if w > MAX_STREAM_WIDTH:
            scale = MAX_STREAM_WIDTH / w
            new_w = MAX_STREAM_WIDTH
            new_h = int(h * scale)
            frame = cv2.resize(frame, (new_w, new_h),
                               interpolation=cv2.INTER_AREA)

        return frame

    def _capture_with_bitblt(self, win_w, win_h, client_w, client_h,
                             offset_x, offset_y):
        """
        Захват окна через BitBlt (fallback).
        Возвращает КЛИЕНТСКУЮ область (обрезает по offset_x/offset_y).
        """
        try:
            gdi = self._gdi
            hwnd_dc = win32gui.GetWindowDC(self.hwnd)
            try:
                src_dc = win32ui.CreateDCFromHandle(hwnd_dc)
                gdi['compatible_dc'].BitBlt(
                    (0, 0), (win_w, win_h), src_dc, (0, 0), 0x00CC0020
                )

                bmpinfo = gdi['bitmap'].GetInfo()
                bmpstr = gdi['bitmap'].GetBitmapBits(True)

                img = np.frombuffer(bmpstr, dtype=np.uint8)
                img = img.reshape((bmpinfo['bmHeight'], bmpinfo['bmWidth'], 4))
                frame = img[:, :, :3].copy()

                frame = frame[offset_y:offset_y + client_h,
                offset_x:offset_x + client_w]

                h, w = frame.shape[:2]
                if w > MAX_STREAM_WIDTH:
                    scale = MAX_STREAM_WIDTH / w
                    new_w = MAX_STREAM_WIDTH
                    new_h = int(h * scale)
                    frame = cv2.resize(frame, (new_w, new_h),
                                       interpolation=cv2.INTER_AREA)
                return frame
            finally:
                win32gui.ReleaseDC(self.hwnd, hwnd_dc)
        except Exception:
            return None

    # ============================================================
    # Наблюдение за активным окном
    # ============================================================

    def window_watcher_loop(self):
        print("[PC-Stream] 👁️ Наблюдение за активным окном запущено")
        dbg.log_event("pc.window_watcher_start")
        while self.streaming:
            try:
                our = self._get_our_hwnds()
                fg = window_utils.get_foreground_window(our_hwnds=our)
                if fg is not None and fg != self.hwnd:
                    try:
                        title = win32gui.GetWindowText(fg)
                    except Exception:
                        title = "?"
                    self.set_active_hwnd(fg, title)
            except Exception as e:
                dbg.log_event("pc.window_watcher_error", {"error": str(e)})
            time.sleep(WINDOW_WATCH_INTERVAL)
        print("[PC-Stream] 👁️ Наблюдение за активным окном остановлено")
        dbg.log_event("pc.window_watcher_stop")

    # ============================================================
    # Захват / кодирование
    # ============================================================

    def capture_loop(self):
        print("[PC-Stream] 🎥 Поток захвата запущен")
        dbg.log_event("pc.capture_loop_start", {"fps": FPS})
        frame_interval = 1.0 / FPS
        last_log_time = time.time()
        frame_count = 0
        first_frame_logged = False

        while self.streaming:
            t0 = time.time()

            try:
                if not self.hwnd or not win32gui.IsWindow(self.hwnd):
                    if not self._window_lost:
                        print("[PC-Stream] ⚠️ Окно не выбрано, ожидание активного...")
                        dbg.log_event("pc.window_lost", {"hwnd": self.hwnd})
                        self._window_lost = True
                    time.sleep(0.1)
                    continue

                with dbg.timer("pc.capture_frame"):
                    frame = self.capture_window_frame()

                if frame is not None:
                    if not first_frame_logged:
                        print("[PC-Stream] ✅ Первый кадр захвачен")
                        dbg.log_event("pc.first_frame_captured")
                        first_frame_logged = True

                    if self._window_lost:
                        self._window_lost = False

                    if self.raw_frame_queue.full():
                        try:
                            self.raw_frame_queue.get_nowait()
                        except queue.Empty:
                            pass
                    try:
                        self.raw_frame_queue.put_nowait(frame)
                    except queue.Full:
                        pass

                    frame_count += 1

                    if time.time() - last_log_time >= 5:
                        fps = frame_count / (time.time() - last_log_time)
                        dbg.log_event("pc.capture_fps", {"fps": round(fps, 2)})
                        frame_count = 0
                        last_log_time = time.time()
            except Exception as e:
                print(f"[PC-Stream] ⚠️ Ошибка захвата: {e}")
                dbg.log_event("pc.capture_error", {"error": str(e)})

            elapsed = time.time() - t0
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

        print("[PC-Stream] 🎥 Поток захвата остановлен")
        dbg.log_event("pc.capture_loop_stop")

    def encode_loop(self):
        print("[PC-Stream] 🖼️ Поток кодирования запущен")
        dbg.log_event("pc.encode_loop_start", {"quality": JPEG_QUALITY})
        encode_params = [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
        last_log_time = time.time()
        encode_count = 0

        while self.streaming:
            try:
                frame = self.raw_frame_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                with dbg.timer("pc.encode_raw_copy"):
                    with self.frame_lock:
                        self.current_frame_raw = frame.copy()

                with dbg.timer("pc.encode_jpeg"):
                    ok, jpeg = cv2.imencode('.jpg', frame, encode_params)

                if ok:
                    jpeg_bytes = jpeg.tobytes()
                    with self.frame_lock:
                        self.current_frame_jpeg = jpeg_bytes
                        self.frame_counter += 1
                    encode_count += 1

                    dbg.log_timing("pc.encode_jpeg_size_kb", len(jpeg_bytes) / 1024.0)

                    if time.time() - last_log_time >= 5:
                        fps = encode_count / (time.time() - last_log_time)
                        dbg.log_event("pc.encode_fps", {"fps": round(fps, 2)})
                        encode_count = 0
                        last_log_time = time.time()
            except Exception as e:
                print(f"[PC-Stream] ⚠️ Ошибка кодирования: {e}")
                dbg.log_event("pc.encode_error", {"error": str(e)})

        print("[PC-Stream] 🖼️ Поток кодирования остановлен")
        dbg.log_event("pc.encode_loop_stop")

    # ============================================================
    # HTTP-сервер
    # ============================================================

    def start_http_server(self):
        parent = self

        class MJPEGHandler(http.server.BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                try:
                    self.connection.setsockopt(
                        socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    self.connection.setsockopt(
                        socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except Exception:
                    pass

            def do_GET(self):
                if self.path == '/' or self.path == '/index.html':
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(parent.get_html_page().encode('utf-8'))
                    return

                if self.path == '/stream':
                    client_addr = self.client_address
                    print(f"[PC-Stream] 📡 MJPEG client connected: {client_addr}")
                    dbg.log_event("pc.mjpeg_client_connected",
                                  {"addr": str(client_addr)})

                    self.send_response(200)
                    self.send_header('Content-Type',
                                     'multipart/x-mixed-replace; boundary=--frame')
                    self.send_header('Cache-Control',
                                     'no-cache, no-store, must-revalidate')
                    self.send_header('Pragma', 'no-cache')
                    self.send_header('Expires', '0')
                    self.send_header('Connection', 'keep-alive')
                    self.end_headers()

                    last_counter = -1
                    sent_count = 0
                    t_last = time.time()
                    empty_cycles = 0
                    try:
                        while parent.streaming:
                            with parent.frame_lock:
                                jpeg = parent.current_frame_jpeg
                                counter = parent.frame_counter

                            if jpeg is None:
                                empty_cycles += 1
                                if empty_cycles > 6000:
                                    print("[PC-Stream] ⚠️ Нет кадров 30 сек, "
                                          "закрываем MJPEG")
                                    break
                                time.sleep(0.005)
                                continue
                            empty_cycles = 0

                            if counter == last_counter:
                                time.sleep(0.005)
                                continue
                            last_counter = counter

                            try:
                                self.wfile.write(b'--frame\r\n')
                                self.wfile.write(b'Content-Type: image/jpeg\r\n')
                                self.wfile.write(
                                    f'Content-Length: {len(jpeg)}\r\n\r\n'.encode()
                                )
                                self.wfile.write(jpeg)
                                self.wfile.write(b'\r\n')
                                sent_count += 1
                            except (BrokenPipeError,
                                    ConnectionResetError,
                                    ConnectionAbortedError,
                                    OSError) as e:
                                print(f"[PC-Stream] ⚠️ MJPEG send error "
                                      f"({type(e).__name__}): {e}")
                                dbg.log_event("pc.mjpeg_send_error",
                                              {"error": str(e),
                                               "type": type(e).__name__})
                                break

                            if time.time() - t_last >= 5:
                                sent_count = 0
                                t_last = time.time()
                    except Exception as e:
                        print(f"[PC-Stream] ⚠️ MJPEG stream error: {e}")
                        dbg.log_event("pc.mjpeg_stream_error", {"error": str(e)})
                    finally:
                        print(f"[PC-Stream] 📡 MJPEG client disconnected: {client_addr}")
                        dbg.log_event("pc.mjpeg_client_disconnected",
                                      {"addr": str(client_addr)})
                    return

                self.send_error(404)

            def log_message(self, fmt, *args):
                pass

        class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
            allow_reuse_address = True
            daemon_threads = True

        try:
            with dbg.timer("pc.http_server_start"):
                self.server = ThreadedTCPServer(('0.0.0.0', PC_TO_PHONE_PORT),
                                                MJPEGHandler)
                self.server_thread = threading.Thread(
                    target=self.server.serve_forever, daemon=True
                )
                self.server_thread.start()
            print(f"[PC-Stream] ✅ HTTP-сервер запущен на порту {PC_TO_PHONE_PORT}")
            dbg.log_event("pc.http_server_started", {"port": PC_TO_PHONE_PORT})
            return True
        except Exception as e:
            print(f"[PC-Stream] ❌ Ошибка HTTP-сервера: {e}")
            dbg.log_event("pc.http_server_error", {"error": str(e)})
            return False

    def get_html_page(self):
        return """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>Окно ПК</title>
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
        background: #000;
        display: flex;
        align-items: center;
        justify-content: center;
        height: 100vh;
        width: 100vw;
        overflow: hidden;
    }
    #stream {
        max-width: 100%;
        max-height: 100%;
        width: auto;
        height: auto;
        object-fit: contain;
    }
</style>
</head>
<body>
<img id="stream" src="/stream" alt="Окно ПК">
</body>
</html>"""

    # ============================================================
    # ADB reverse
    # ============================================================

    def setup_adb_reverse(self):
        """ИЗМЕНЕНО: параллельная настройка всех портов."""
        ports = (PC_TO_PHONE_PORT, PC_OCR_PORT, PC_DEBUG_PORT,
                 PC_TRANSLATE_PORT, PC_SETTINGS_PORT)
        print(f"[PC-Stream] 🔗 Настройка ADB reverse: " +
              ", ".join(f"tcp:{p}" for p in ports))

        if not self.selected_device:
            print("[PC-Stream] ❌ Серийник устройства не определён")
            dbg.log_event("pc.adb_reverse_error", {"reason": "no_device"})
            return False

        if ':' in self.selected_device:
            print("[PC-Stream] ❌ ADB reverse не работает с Wi-Fi")
            dbg.log_event("pc.adb_reverse_error", {"reason": "wifi"})
            return False

        try:
            print("[PC-Stream] 🔄 Запуск ADB-демона...")
            subprocess.run(
                ["adb", "start-server"],
                capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW
                if sys.platform == "win32" else 0
            )
        except Exception as e:
            print(f"[PC-Stream] ⚠️ Не удалось запустить ADB-демон: {e}")

        def _remove_one(port):
            try:
                subprocess.run(
                    ["adb", "-s", self.selected_device, "reverse",
                     "--remove", f"tcp:{port}"],
                    capture_output=True, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW
                    if sys.platform == "win32" else 0
                )
            except Exception:
                pass

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
                list(ex.map(_remove_one, ports))
        except Exception:
            pass

        def _setup_one(port):
            try:
                result = subprocess.run(
                    ["adb", "-s", self.selected_device, "reverse",
                     f"tcp:{port}", f"tcp:{port}"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW
                    if sys.platform == "win32" else 0
                )
                return port, result.returncode, result.stderr
            except Exception as e:
                return port, -1, str(e)

        ok_all = True
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
                results = list(ex.map(_setup_one, ports))
            for port, rc, err in results:
                if rc == 0:
                    print(f"[PC-Stream] ✅ ADB reverse tcp:{port} → tcp:{port}")
                else:
                    print(f"[PC-Stream] ❌ Ошибка ADB reverse tcp:{port}: {err}")
                    dbg.log_event("pc.adb_reverse_fail",
                                  {"port": port, "stderr": err})
                    ok_all = False
        except Exception as e:
            print(f"[PC-Stream] ❌ Ошибка параллельного ADB reverse: {e}")
            dbg.log_event("pc.adb_reverse_exception", {"error": str(e)})
            return False

        print("[PC-Stream] 🔍 Проверка ADB reverse --list...")
        try:
            check = subprocess.run(
                ["adb", "-s", self.selected_device, "reverse", "--list"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW
                if sys.platform == "win32" else 0
            )
            if check.returncode == 0:
                lines = [l for l in check.stdout.strip().split("\n") if l.strip()]
                if lines:
                    print("[PC-Stream] 📋 ADB reverse --list:")
                    for line in lines:
                        print(f"[PC-Stream]    {line}")
                    if len(lines) < len(ports):
                        print(f"[PC-Stream] ⚠️ ВНИМАНИЕ: настроено "
                              f"{len(lines)} из {len(ports)} reverse!")
                else:
                    print("[PC-Stream] ❌ ADB reverse --list пуст! "
                          "Reverse не применился!")
                    ok_all = False
            else:
                print(f"[PC-Stream] ⚠️ Не удалось выполнить reverse --list: "
                      f"{check.stderr}")
        except Exception as e:
            print(f"[PC-Stream] ⚠️ Ошибка проверки reverse --list: {e}")

        return ok_all

    def remove_adb_reverse(self):
        try:
            if self.selected_device:
                for port in (PC_TO_PHONE_PORT, PC_OCR_PORT, PC_DEBUG_PORT,
                             PC_TRANSLATE_PORT, PC_SETTINGS_PORT):
                    subprocess.run(
                        ["adb", "-s", self.selected_device, "reverse",
                         "--remove", f"tcp:{port}"],
                        capture_output=True, timeout=5,
                        creationflags=subprocess.CREATE_NO_WINDOW
                        if sys.platform == "win32" else 0
                    )
            print("[PC-Stream] ✅ ADB reverse удалён")
        except Exception:
            pass

    def check_adb_reverse(self) -> str:
        if not self.selected_device:
            return "Устройство не выбрано"
        try:
            result = subprocess.run(
                ["adb", "-s", self.selected_device, "reverse", "--list"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW
                if sys.platform == "win32" else 0
            )
            if result.returncode == 0:
                lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
                if lines:
                    return "\n".join(lines)
                return "(пусто — reverse не настроен)"
            return f"Ошибка: {result.stderr}"
        except Exception as e:
            return f"Ошибка: {e}"

    # ============================================================
    # Запуск / остановка
    # ============================================================

    def start(self):
        if self.streaming:
            print("[PC-Stream] ⚠️ Стример уже запущен")
            return False

        our = self._get_our_hwnds()
        fg = window_utils.get_foreground_window(our_hwnds=our)
        if fg is not None:
            try:
                title = win32gui.GetWindowText(fg)
            except Exception:
                title = "?"
            self.hwnd = fg
            self.window_title = title
            print(f"[PC-Stream] ▶ Стартовое активное окно: {title}")
            dbg.log_event("pc.stream_start", {"hwnd": fg, "title": title})
        else:
            self.hwnd = None
            self.window_title = None
            print("[PC-Stream] ⏳ Активное окно не найдено — ждём появления...")
            dbg.log_event("pc.stream_start_waiting")

        self._window_lost = False

        with dbg.timer("pc.stream_start_total"):
            if not self.start_http_server():
                return False

            self.streaming = True

            self.capture_thread = threading.Thread(target=self.capture_loop, daemon=True)
            self.capture_thread.start()

            self.encode_thread = threading.Thread(target=self.encode_loop, daemon=True)
            self.encode_thread.start()

            self.window_watcher_thread = threading.Thread(
                target=self.window_watcher_loop, daemon=True
            )
            self.window_watcher_thread.start()

            print("[PC-Stream] ⏳ Ожидание первого кадра...")
            got_first = False
            for _ in range(FIRST_FRAME_TIMEOUT * 10):
                with self.frame_lock:
                    if self.current_frame_jpeg is not None:
                        got_first = True
                        break
                time.sleep(0.1)

            if got_first:
                print("[PC-Stream] ✅ Первый кадр получен")
            else:
                print("[PC-Stream] ⚠️ Первый кадр не получен за "
                      f"{FIRST_FRAME_TIMEOUT}с — продолжаем в режиме ожидания")
                dbg.log_event("pc.first_frame_timeout_but_running")

            self.setup_adb_reverse()
        return True

    def stop(self):
        print("[PC-Stream] ⏹ Остановка трансляции ПК...")
        dbg.log_event("pc.stream_stop")

        self.streaming = False

        if self.capture_thread:
            try:
                self.capture_thread.join(timeout=1)
            except Exception:
                pass
            self.capture_thread = None

        if self.encode_thread:
            try:
                self.encode_thread.join(timeout=1)
            except Exception:
                pass
            self.encode_thread = None

        if self.window_watcher_thread:
            try:
                self.window_watcher_thread.join(timeout=1)
            except Exception:
                pass
            self.window_watcher_thread = None

        self._release_gdi()

        if self.server:
            try:
                def _shutdown():
                    try:
                        self.server.shutdown()
                        self.server.server_close()
                    except Exception:
                        pass

                t = threading.Thread(target=_shutdown, daemon=True)
                t.start()
                t.join(timeout=1.5)
                print("[PC-Stream] ✅ HTTP-сервер остановлен")
            except Exception:
                pass
            self.server = None

        with self.frame_lock:
            self.current_frame_jpeg = None
            self.current_frame_raw = None
            self.frame_counter = 0

        try:
            threading.Thread(target=self.remove_adb_reverse, daemon=True).start()
        except Exception:
            pass

        print("[PC-Stream] 👋 Трансляция ПК остановлена")