# src/pc_streamer.py
"""Захват выбранного окна Windows через PrintWindow + MJPEG + ADB reverse."""

import cv2
import numpy as np
import ctypes
import threading
import time
import queue
import http.server
import socketserver
import subprocess
import sys

import win32gui
import win32ui

from src.config import (
    PC_TO_PHONE_PORT,
    PC_OCR_PORT,
    PC_DEBUG_PORT,
    FPS,
    JPEG_QUALITY,
    MAX_STREAM_WIDTH,
    FIRST_FRAME_TIMEOUT,
)
from src.debug_logger import logger as dbg


class WindowStreamer:
    """Трансляция выбранного окна через PrintWindow + MJPEG + ADB reverse."""

    def __init__(self):
        self.hwnd = None
        self.window_title = None
        self.selected_device = None

        self.streaming = False
        self.server = None
        self.server_thread = None

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

    def set_window(self, hwnd, title):
        self.hwnd = hwnd
        self.window_title = title
        print(f"[PC-Stream] ✅ Окно установлено: {title} (HWND={hwnd})")
        dbg.log_event("pc.stream_set_window", {"hwnd": hwnd, "title": title})

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
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            return None

        try:
            rect = win32gui.GetWindowRect(self.hwnd)
        except Exception:
            return None

        width = rect[2] - rect[0]
        height = rect[3] - rect[1]
        if width <= 0 or height <= 0:
            return None

        if not self._ensure_gdi(width, height):
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
            return self._capture_with_bitblt(width, height)

        try:
            bmpinfo = gdi['bitmap'].GetInfo()
            bmpstr = gdi['bitmap'].GetBitmapBits(True)

            img = np.frombuffer(bmpstr, dtype=np.uint8)
            img = img.reshape((bmpinfo['bmHeight'], bmpinfo['bmWidth'], 4))
            frame = img[:, :, :3].copy()
        except Exception:
            return None

        h, w = frame.shape[:2]
        if w > MAX_STREAM_WIDTH:
            scale = MAX_STREAM_WIDTH / w
            new_w = MAX_STREAM_WIDTH
            new_h = int(h * scale)
            frame = cv2.resize(frame, (new_w, new_h),
                               interpolation=cv2.INTER_LINEAR)

        return frame

    def _capture_with_bitblt(self, width, height):
        try:
            gdi = self._gdi
            hwnd_dc = win32gui.GetWindowDC(self.hwnd)
            try:
                src_dc = win32ui.CreateDCFromHandle(hwnd_dc)
                gdi['compatible_dc'].BitBlt(
                    (0, 0), (width, height), src_dc, (0, 0), 0x00CC0020
                )

                bmpinfo = gdi['bitmap'].GetInfo()
                bmpstr = gdi['bitmap'].GetBitmapBits(True)

                img = np.frombuffer(bmpstr, dtype=np.uint8)
                img = img.reshape((bmpinfo['bmHeight'], bmpinfo['bmWidth'], 4))
                frame = img[:, :, :3].copy()

                h, w = frame.shape[:2]
                if w > MAX_STREAM_WIDTH:
                    scale = MAX_STREAM_WIDTH / w
                    new_w = MAX_STREAM_WIDTH
                    new_h = int(h * scale)
                    frame = cv2.resize(frame, (new_w, new_h),
                                       interpolation=cv2.INTER_LINEAR)
                return frame
            finally:
                win32gui.ReleaseDC(self.hwnd, hwnd_dc)
        except Exception:
            return None

    def capture_loop(self):
        print("[PC-Stream] 🎥 Поток захвата запущен")
        dbg.log_event("pc.capture_loop_start", {"fps": FPS})
        frame_interval = 1.0 / FPS
        last_log_time = time.time()
        frame_count = 0
        first_frame_logged = False
        window_lost_logged = False

        while self.streaming:
            t0 = time.time()

            try:
                if not self.hwnd or not win32gui.IsWindow(self.hwnd):
                    if not window_lost_logged:
                        print("[PC-Stream] ⚠️ Окно закрыто или потеряно")
                        dbg.log_event("pc.window_lost", {"hwnd": self.hwnd})
                        window_lost_logged = True
                    time.sleep(0.5)
                    continue

                with dbg.timer("pc.capture_frame"):
                    frame = self.capture_window_frame()

                if frame is not None:
                    if not first_frame_logged:
                        print("[PC-Stream] ✅ Первый кадр захвачен")
                        dbg.log_event("pc.first_frame_captured")
                        first_frame_logged = True
                        window_lost_logged = False

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
                        # print убран, чтобы не спамить в консоль
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
                        # print убран, чтобы не спамить в консоль
                        dbg.log_event("pc.encode_fps", {"fps": round(fps, 2)})
                        encode_count = 0
                        last_log_time = time.time()
            except Exception as e:
                print(f"[PC-Stream] ⚠️ Ошибка кодирования: {e}")
                dbg.log_event("pc.encode_error", {"error": str(e)})

        print("[PC-Stream] 🖼️ Поток кодирования остановлен")
        dbg.log_event("pc.encode_loop_stop")

    def start_http_server(self):
        parent = self

        class MJPEGHandler(http.server.BaseHTTPRequestHandler):
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
                    self.end_headers()

                    last_counter = -1
                    sent_count = 0
                    t_last = time.time()
                    try:
                        while parent.streaming:
                            with parent.frame_lock:
                                jpeg = parent.current_frame_jpeg
                                counter = parent.frame_counter

                            if jpeg is None or counter == last_counter:
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
                            except (BrokenPipeError, ConnectionResetError):
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

    def setup_adb_reverse(self):
        """
        Настраивает ADB reverse и ПРОВЕРЯЕТ, что он применился.
        """
        ports = (PC_TO_PHONE_PORT, PC_OCR_PORT, PC_DEBUG_PORT)
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

        # Убеждаемся, что ADB-демон запущен
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

        try:
            # Удаляем старые reverse (если есть)
            for port in ports:
                subprocess.run(
                    ["adb", "-s", self.selected_device, "reverse",
                     "--remove", f"tcp:{port}"],
                    capture_output=True, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW
                    if sys.platform == "win32" else 0
                )

            # Настраиваем новые
            ok_all = True
            for port in ports:
                with dbg.timer("pc.adb_reverse_setup", {"port": port}):
                    result = subprocess.run(
                        ["adb", "-s", self.selected_device, "reverse",
                         f"tcp:{port}", f"tcp:{port}"],
                        capture_output=True, text=True, timeout=10,
                        creationflags=subprocess.CREATE_NO_WINDOW
                        if sys.platform == "win32" else 0
                    )
                if result.returncode == 0:
                    print(f"[PC-Stream] ✅ ADB reverse tcp:{port} → tcp:{port}")
                else:
                    print(f"[PC-Stream] ❌ Ошибка ADB reverse tcp:{port}: "
                          f"{result.stderr}")
                    dbg.log_event("pc.adb_reverse_fail",
                                  {"port": port, "stderr": result.stderr})
                    ok_all = False

            # ПРОВЕРКА: запрашиваем список reverse
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
                            print(f"[PC-Stream] ⚠️ ВНИМАНИЕ: настроено {len(lines)} из {len(ports)} reverse!")
                    else:
                        print("[PC-Stream] ❌ ADB reverse --list пуст! Reverse не применился!")
                        print("[PC-Stream]    Возможные причины:")
                        print("[PC-Stream]    - Старая версия ADB (обновите Platform Tools)")
                        print("[PC-Stream]    - ADB-демон падает")
                        print("[PC-Stream]    - Устройство не поддерживает reverse")
                        ok_all = False
                else:
                    print(f"[PC-Stream] ⚠️ Не удалось выполнить reverse --list: "
                          f"{check.stderr}")
            except Exception as e:
                print(f"[PC-Stream] ⚠️ Ошибка проверки reverse --list: {e}")

            return ok_all
        except Exception as e:
            print(f"[PC-Stream] ❌ Ошибка ADB reverse: {e}")
            dbg.log_event("pc.adb_reverse_exception", {"error": str(e)})
            return False

    def remove_adb_reverse(self):
        try:
            if self.selected_device:
                for port in (PC_TO_PHONE_PORT, PC_OCR_PORT, PC_DEBUG_PORT):
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
        """
        Возвращает строку с текущим списком ADB reverse.
        Используется для диагностики (без UI-кнопки).
        """
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

    def start(self):
        if self.streaming:
            print("[PC-Stream] ⚠️ Стример уже запущен")
            return False

        if not self.hwnd:
            print("[PC-Stream] ❌ Окно не установлено")
            return False

        if not win32gui.IsWindow(self.hwnd):
            print("[PC-Stream] ❌ Окно не существует")
            return False

        print(f"[PC-Stream] ▶ Запуск трансляции: {self.window_title}")
        dbg.log_event("pc.stream_start", {"hwnd": self.hwnd, "title": self.window_title})

        with dbg.timer("pc.stream_start_total"):
            if not self.start_http_server():
                return False

            self.streaming = True

            self.capture_thread = threading.Thread(target=self.capture_loop, daemon=True)
            self.capture_thread.start()

            self.encode_thread = threading.Thread(target=self.encode_loop, daemon=True)
            self.encode_thread.start()

            print("[PC-Stream] ⏳ Ожидание первого кадра...")
            for _ in range(FIRST_FRAME_TIMEOUT * 10):
                with self.frame_lock:
                    if self.current_frame_jpeg is not None:
                        break
                time.sleep(0.1)

            with self.frame_lock:
                if self.current_frame_jpeg is None:
                    print(f"[PC-Stream] ❌ Первый кадр не получен")
                    dbg.log_event("pc.first_frame_timeout")
                    self.streaming = False
                    return False

            print("[PC-Stream] ✅ Первый кадр получен")
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