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
from tkinter import messagebox

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
        return "adb не найден. Установите: sudo apt install adb"
    try:
        out = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=20).stdout
    except Exception as err:
        return "Ошибка adb: %s" % err
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("*") or line.startswith("List of"):
            continue
        rows.append(line.split(None, 1))
    ready = [r[0] for r in rows if len(r) > 1 and r[1].strip() == "device"]
    if not ready:
        states = " ".join(r[1] for r in rows if len(r) > 1)
        if "unauthorized" in states:
            return "Телефон не разрешил этот компьютер: на телефоне подтвердите запрос «Разрешить отладку по USB»."
        if "no permissions" in states:
            return "Нет прав на USB-устройство. Попробуйте: adb kill-server, затем переподключите кабель."
        if "offline" in states:
            return "Телефон «offline»: переподключите кабель или переключите отладку по USB."
        return ("Телефон по кабелю не найден. Проверьте: кабель умеет передавать данные, "
                "режим USB «Передача файлов», включена «Отладка по USB».")
    r = subprocess.run([adb, "-s", ready[0], "forward", "tcp:%d" % PORT, "tcp:%d" % PORT],
                       capture_output=True, text=True, timeout=20)
    if r.returncode:
        return "adb forward не удался: %s" % (r.stderr.strip() or r.stdout.strip())
    return None


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

    def __init__(self, pin=""):
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
        self.state_text = "Введите PIN из приложения на телефоне и нажмите «Подключить»"
        self.frames = 0
        self.fps = 0
        self.fps_at = time.time()

        bar = tk.Frame(self.root)
        bar.pack(side="top", fill="x")
        tk.Label(bar, text="PIN:").pack(side="left", padx=(6, 0))
        self.pin_var = tk.StringVar(value=pin)
        self.pin_entry = tk.Entry(bar, textvariable=self.pin_var, width=8, font=("TkDefaultFont", 12))
        self.pin_entry.pack(side="left", padx=3, pady=3)
        self.pin_entry.bind("<Return>", lambda e: self.connect())
        tk.Button(bar, text="Подключить", command=self.connect).pack(side="left", padx=2, pady=2)
        for text, cmd in (("◀", "back"), ("●", "home"), ("▣", "recents"), ("☰", "notif")):
            tk.Button(bar, text=text, width=2, command=lambda c=cmd: self.link.send(c)).pack(side="left", padx=1, pady=2)
        self.qvar = tk.StringVar(value="Качество: среднее")
        tk.OptionMenu(bar, self.qvar, *self.PRESETS, command=self._quality).pack(side="left", padx=4)

        self.status = tk.Label(self.root, text=self.state_text, anchor="w", justify="left", wraplength=420)
        self.status.pack(side="top", fill="x", padx=6, pady=2)

        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0, width=420, height=780)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<ButtonPress-2>", lambda e: self.link.send("home"))
        self.canvas.bind("<ButtonPress-3>", lambda e: self.link.send("back"))
        self.canvas.bind("<Button-4>", lambda e: self._wheel(e, +1))
        self.canvas.bind("<Button-5>", lambda e: self._wheel(e, -1))
        self.canvas.bind("<MouseWheel>", lambda e: self._wheel(e, 1 if e.delta > 0 else -1))
        self.root.bind("<Key>", self._key)
        self.root.after(15, self._tick)
        if pin:
            self.root.after(400, self.connect)
        else:
            self.root.after(300, lambda: threading.Thread(target=self._check_adb, daemon=True).start())
            self.pin_entry.focus_set()

    # ---- подключение ----
    def _check_adb(self):
        err = adb_forward()
        self.states.put(err or "Телефон найден по кабелю. Введите PIN из приложения и нажмите «Подключить».")

    def connect(self):
        pin = self.pin_var.get().strip()
        if not pin:
            self.states.put("Введите PIN из приложения на телефоне (он виден после «Начать трансляцию»).")
            self.pin_entry.focus_set()
            return
        self.states.put("Ищу телефон по кабелю…")
        threading.Thread(target=self._connect_worker, args=(pin,), daemon=True).start()

    def _connect_worker(self, pin):
        err = adb_forward()
        if err:
            self.states.put("!" + err)
            return
        self.link.connect(pin)

    # ---- приём кадров ----
    def _on_frame(self, data):
        self.frames += 1
        with self.flock:
            self.latest = data

    def _on_resize(self, e):
        self.status.config(wraplength=max(e.width - 12, 120))
        self._redraw()

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
        popup = None
        while not self.states.empty():
            msg = self.states.get()
            if msg.startswith("!"):
                msg = msg[1:]
                popup = msg
            self.state_text = msg
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
        if popup:
            messagebox.showerror("USB Screen", popup)
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
        self.canvas.focus_set()
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
        if e.widget is self.pin_entry:      # печатаем PIN, а не на телефоне
            return
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
    App(sys.argv[1] if len(sys.argv) > 1 else "").root.mainloop()
