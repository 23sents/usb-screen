#!/usr/bin/env python3
"""USB Screen — окно с экраном телефона и управление им мышью/клавиатурой (по USB-кабелю).

Запуск:  python3 viewer.py
Мышь = палец: клик — тап, зажать и вести — свайп, правая кнопка — «Назад»,
средняя — «Домой», колесо — прокрутка. Клавиатура печатает в поле ввода на телефоне.
"""
import io
import math
import queue
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import simpledialog

try:
    from PIL import Image, ImageTk
except ImportError:
    sys.exit("Не хватает Pillow:  sudo apt install python3-pil python3-pil.imagetk python3-tk")

PORT = 38517            # такой же, как в Android-приложении
MAX_FRAME = 16_000_000
RESAMPLE = getattr(Image, "Resampling", Image).BILINEAR


def adb_forward():
    """Пробрасывает порт телефона на localhost. Возвращает текст ошибки или None."""
    adb = shutil.which("adb")
    if not adb:
        return "adb не найден: sudo apt install adb"
    try:
        out = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=15).stdout
    except Exception as err:
        return "adb: %s" % err
    rows = [l.split() for l in out.strip().splitlines()[1:] if l.strip()]
    ready = [r[0] for r in rows if len(r) > 1 and r[1] == "device"]
    if not ready:
        if any(len(r) > 1 and r[1] == "unauthorized" for r in rows):
            return "Телефон не разрешил этот компьютер: подтвердите запрос «Разрешить отладку по USB»"
        return "Телефон не найден: кабель, режим «Передача файлов», «Отладка по USB»"
    r = subprocess.run([adb, "-s", ready[0], "forward", "tcp:%d" % PORT, "tcp:%d" % PORT],
                       capture_output=True, text=True, timeout=15)
    return r.stderr.strip() or None if r.returncode else None


class Link:
    """TCP-канал к телефону: кадры [тип 1 байт][длина 4 байта][данные]; команды — текстовые строки."""

    def __init__(self, on_frame, on_state):
        self.on_frame = on_frame
        self.on_state = on_state
        self.sock = None
        self.lock = threading.Lock()

    def connect(self, pin):
        self.close()
        try:
            s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.sendall(("auth %s\n" % pin).encode())
            s.settimeout(None)
        except OSError as err:
            self.on_state("Не удалось подключиться: %s" % err)
            return
        self.sock = s
        self.on_state("Подключаюсь…")
        threading.Thread(target=self._reader, args=(s,), daemon=True).start()

    def close(self):
        s, self.sock = self.sock, None
        if s:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    @staticmethod
    def _read_exact(s, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return bytes(buf)

    def _reader(self, s):
        got = False
        try:
            while True:
                hdr = self._read_exact(s, 5)
                size = struct.unpack(">I", hdr[1:])[0]
                if size > MAX_FRAME:
                    raise EOFError
                payload = self._read_exact(s, size)
                if hdr[0] == 1:
                    if not got:
                        got = True
                        self.on_state("Подключено")
                    self.on_frame(payload)
        except (EOFError, OSError):
            pass
        finally:
            if self.sock is s:
                self.sock = None
                self.on_state("Отключено" if got else
                              "Телефон не ответил: запущена ли трансляция и верный ли PIN?")
            try:
                s.close()
            except OSError:
                pass

    def send(self, line):
        s = self.sock
        if s is None:
            return
        with self.lock:
            try:
                s.sendall((line + "\n").encode())
            except OSError:
                pass


class App:
    PRESETS = {"Качество: низкое": 1, "Качество: среднее": 2, "Качество: высокое": 3}

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("USB Screen")
        self.states = queue.SimpleQueue()
        self.link = Link(self._on_frame, self.states.put)
        self.flock = threading.Lock()
        self.latest = None
        self.frame_img = None
        self.photo = None
        self.ox = self.oy = self.nw = self.nh = 0
        self.down = None
        self.last_wheel = 0.0
        self.pin = ""
        self.state_text = "Не подключено"
        self.frames = 0
        self.fps = 0
        self.fps_at = time.time()

        bar = tk.Frame(self.root)
        bar.pack(side="top", fill="x")
        tk.Button(bar, text="⟳ Подключить", command=self.connect).pack(side="left", padx=2, pady=2)
        for text, cmd in (("◀", "back"), ("●", "home"), ("▣", "recents"), ("☰", "notif")):
            tk.Button(bar, text=text, width=3, command=lambda c=cmd: self.link.send(c)).pack(side="left", padx=1, pady=2)
        self.qvar = tk.StringVar(value="Качество: среднее")
        tk.OptionMenu(bar, self.qvar, *self.PRESETS, command=self._quality).pack(side="left", padx=6)
        self.status = tk.Label(bar, text=self.state_text, anchor="e")
        self.status.pack(side="right", padx=6)

        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0, width=380, height=800)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<ButtonPress-2>", lambda e: self.link.send("home"))
        self.canvas.bind("<ButtonPress-3>", lambda e: self.link.send("back"))
        self.canvas.bind("<Button-4>", lambda e: self._wheel(e, +1))
        self.canvas.bind("<Button-5>", lambda e: self._wheel(e, -1))
        self.canvas.bind("<MouseWheel>", lambda e: self._wheel(e, 1 if e.delta > 0 else -1))
        self.root.bind("<Key>", self._key)
        self.root.after(30, self._tick)
        self.root.after(400, self.connect)

    # ---- подключение ----
    def connect(self):
        err = adb_forward()
        if err:
            self.state_text = err
            self.status.config(text=err)
            return
        pin = simpledialog.askstring("PIN", "Введите PIN, показанный в приложении на телефоне:",
                                     initialvalue=self.pin, parent=self.root)
        if not pin:
            return
        self.pin = pin.strip()
        self.state_text = "Подключаюсь…"
        self.status.config(text=self.state_text)
        threading.Thread(target=self.link.connect, args=(self.pin,), daemon=True).start()

    # ---- приём кадров ----
    def _on_frame(self, data):
        self.frames += 1
        with self.flock:
            self.latest = data

    def _tick(self):
        with self.flock:
            data, self.latest = self.latest, None
        if data:
            try:
                img = Image.open(io.BytesIO(data))
                img.load()
                self.frame_img = img
                self._redraw()
            except Exception:
                pass
        changed = False
        while not self.states.empty():
            self.state_text = self.states.get()
            changed = True
        now = time.time()
        if now - self.fps_at >= 1:
            self.fps = round(self.frames / (now - self.fps_at))
            self.frames = 0
            self.fps_at = now
            changed = True
        if changed:
            live = self.link.sock is not None and self.state_text == "Подключено"
            self.status.config(text=self.state_text + (" · %d fps" % self.fps if live else ""))
        self.root.after(15, self._tick)

    def _redraw(self):
        img = self.frame_img
        if img is None:
            return
        cw, ch = max(self.canvas.winfo_width(), 1), max(self.canvas.winfo_height(), 1)
        iw, ih = img.size
        sc = min(cw / iw, ch / ih)
        self.nw, self.nh = max(int(iw * sc), 1), max(int(ih * sc), 1)
        self.ox, self.oy = (cw - self.nw) // 2, (ch - self.nh) // 2
        self.photo = ImageTk.PhotoImage(img.resize((self.nw, self.nh), RESAMPLE))
        self.canvas.delete("all")
        self.canvas.create_image(self.ox, self.oy, anchor="nw", image=self.photo)

    # ---- ввод ----
    def _norm(self, x, y, clamp=False):
        if not self.nw:
            return None
        nx, ny = (x - self.ox) / self.nw, (y - self.oy) / self.nh
        if clamp:
            return min(1.0, max(0.0, nx)), min(1.0, max(0.0, ny))
        return (nx, ny) if 0 <= nx <= 1 and 0 <= ny <= 1 else None

    def _press(self, e):
        self.down = (e.x, e.y, time.time()) if self._norm(e.x, e.y) else None

    def _release(self, e):
        if not self.down:
            return
        x0, y0, t0 = self.down
        self.down = None
        p0 = self._norm(x0, y0, True)
        p1 = self._norm(e.x, e.y, True)
        ms = int((time.time() - t0) * 1000)
        if math.hypot(e.x - x0, e.y - y0) < 8:
            self.link.send("tap %.4f %.4f %d" % (p0[0], p0[1], min(max(ms, 40), 1500)))
        else:
            self.link.send("swipe %.4f %.4f %.4f %.4f %d" % (p0[0], p0[1], p1[0], p1[1], min(max(ms, 80), 2000)))

    def _wheel(self, e, direction):
        now = time.time()
        if now - self.last_wheel < 0.15:
            return
        self.last_wheel = now
        p = self._norm(e.x, e.y)
        if not p:
            return
        a, b = (0.35, 0.65) if direction > 0 else (0.65, 0.35)
        self.link.send("swipe %.4f %.4f %.4f %.4f 180" % (p[0], a, p[0], b))

    def _key(self, e):
        if e.keysym == "BackSpace":
            self.link.send("bs")
        elif e.keysym in ("Return", "KP_Enter"):
            self.link.send("enter")
        elif e.keysym == "Escape":
            self.link.send("back")
        elif e.char and e.char.isprintable():
            self.link.send("t " + " ".join("%x" % ord(c) for c in e.char))

    def _quality(self, name):
        self.link.send("q %d" % self.PRESETS[name])


if __name__ == "__main__":
    App().root.mainloop()
