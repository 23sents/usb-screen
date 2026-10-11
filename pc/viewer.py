#!/usr/bin/env python3
"""USB Screen — окно-«телефон» на ПК: экран Android по USB-кабелю + управление мышью/клавиатурой.

Запуск:  python3 viewer.py [PIN]

Окно без рамок системы: сам телефон со скруглёнными углами и стеклянная панель кнопок под ним.
  • тащить за рамку телефона, за панель кнопок или за пустое место — перемещает окно
  • тянуть за края и углы окна — менять размер (пропорции сохраняются); Ctrl + колесо — увеличить/уменьшить
  • правая кнопка на рамке/панели — меню «Закрыть»;  Ctrl+Q — выход
  • на экране телефона: клик — тап, зажать и вести — свайп, колесо — прокрутка,
    правая кнопка — «Назад», средняя — «Домой», клавиатура печатает в поле ввода
"""
import math
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time

try:
    import cairo
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GdkPixbuf, GLib, Gtk
except (ImportError, ValueError) as err:
    sys.exit("Не хватает GTK3/Cairo (%s).\n"
             "Установите: sudo apt install python3-gi python3-gi-cairo python3-cairo gir1.2-gtk-3.0 adb" % err)

PORT = 38517            # такой же, как в Android-приложении
MAX_FRAME = 16_000_000

# ---- геометрия (все размеры — доли ширины экрана телефона) ----
BZ = 0.018              # толщина рамки телефона
MARGIN = 0.036          # поле вокруг (тень + зона растягивания)
GAP = 0.035             # зазор между телефоном и панелью кнопок
BAR = 0.13              # высота панели кнопок
RS = 0.10               # радиус скругления экрана
PRESETS = [(1, "480p"), (2, "720p"), (3, "1080p")]
BUTTONS = ["back", "home", "recents", "notif", "quality"]


# ====================================================================== adb
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
            return "Телефон не разрешил этот компьютер: подтвердите запрос «Разрешить отладку по USB» на телефоне."
        if "no permissions" in states:
            return "Нет прав на USB-устройство. Попробуйте: adb kill-server, затем переподключите кабель."
        if "offline" in states:
            return "Телефон «offline»: переподключите кабель или переключите отладку по USB."
        return ("Телефон по кабелю не найден. Проверьте: кабель передаёт данные, "
                "режим USB «Передача файлов», включена «Отладка по USB».")
    r = subprocess.run([adb, "-s", ready[0], "forward", "tcp:%d" % PORT, "tcp:%d" % PORT],
                       capture_output=True, text=True, timeout=20)
    if r.returncode:
        return "adb forward не удался: %s" % (r.stderr.strip() or r.stdout.strip())
    return None


# ====================================================================== канал
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


# ====================================================================== рисование
def rrect(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def put_text(cr, s, x, y, size, bold=False, alpha=1.0, center=False, shadow=True):
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                        cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(size)
    if center:
        ext = cr.text_extents(s)
        x -= ext.width / 2 + ext.x_bearing
    if shadow:
        cr.set_source_rgba(0, 0, 0, 0.55 * alpha)
        cr.move_to(x + 1, y + 1)
        cr.show_text(s)
    cr.set_source_rgba(1, 1, 1, alpha)
    cr.move_to(x, y)
    cr.show_text(s)


def wrap_text(cr, s, size, maxw):
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(size)
    lines, cur = [], ""
    for word in s.split():
        t = (cur + " " + word).strip()
        if not cur or cr.text_extents(t).width <= maxw:
            cur = t
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def draw_icon(cr, kind, cx, cy, s, label=""):
    cr.set_source_rgba(1, 1, 1, 0.95)
    cr.set_line_width(max(1.6, s * 0.26))
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    if kind == "back":
        cr.move_to(cx + s * 0.35, cy - s * 0.85)
        cr.line_to(cx - s * 0.45, cy)
        cr.line_to(cx + s * 0.35, cy + s * 0.85)
        cr.stroke()
    elif kind == "home":
        cr.new_path()
        cr.arc(cx, cy, s * 0.85, 0, 2 * math.pi)
        cr.stroke()
    elif kind == "recents":
        rrect(cr, cx - s * 0.8, cy - s * 0.8, s * 1.6, s * 1.6, s * 0.38)
        cr.stroke()
    elif kind == "notif":
        for dy, half in ((-0.7, 0.9), (0.0, 0.6), (0.7, 0.9)):
            cr.move_to(cx - s * half, cy + s * dy)
            cr.line_to(cx + s * half, cy + s * dy)
            cr.stroke()
    elif kind == "quality":
        put_text(cr, label, cx, cy + s * 0.5, s * 1.35, bold=True, center=True, shadow=False)


class Ui:
    """Раскладка и отрисовка. Не зависит от Gtk — только cairo (удобно проверять в картинку)."""

    def __init__(self):
        self.aspect = 2.05          # высота/ширина экрана телефона
        self.px = self.py = self.pw = self.ph = 0.0
        self.bar = (0, 0, 0, 0)
        self.btn_rects = []

    @staticmethod
    def units(aspect):
        wu = 1 + 2 * BZ + 2 * MARGIN
        hu = aspect + 2 * BZ + GAP + BAR + 2 * MARGIN
        return wu, hu

    def layout(self, W, H):
        wu, hu = self.units(self.aspect)
        pw = min(W / wu, H / hu)
        x0 = (W - pw * wu) / 2
        y0 = (H - pw * hu) / 2
        m, bz = MARGIN * pw, BZ * pw
        self.pw, self.ph = pw, pw * self.aspect
        self.px, self.py = x0 + m + bz, y0 + m + bz
        self.bx, self.by = x0 + m, y0 + m
        self.bw, self.bh = pw + 2 * bz, self.ph + 2 * bz
        barh = BAR * pw
        self.bar = (self.bx, self.by + self.bh + GAP * pw, self.bw, barh)
        bx, by, bw, bh = self.bar
        slot = bw / len(BUTTONS)
        self.btn_rects = [(bx + i * slot, by, slot, bh) for i in range(len(BUTTONS))]

    def hit(self, x, y):
        """-> ('screen',) | ('btn', i) | ('move',)"""
        if self.px <= x <= self.px + self.pw and self.py <= y <= self.py + self.ph:
            return ("screen",)
        for i, (rx, ry, rw, rh) in enumerate(self.btn_rects):
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                return ("btn", i)
        return ("move",)

    def to_norm(self, x, y, clamp=False):
        nx, ny = (x - self.px) / self.pw, (y - self.py) / self.ph
        if clamp:
            return min(1.0, max(0.0, nx)), min(1.0, max(0.0, ny))
        return (nx, ny) if 0 <= nx <= 1 and 0 <= ny <= 1 else None

    # ------------------------------------------------------------------ draw
    def draw(self, cr, W, H, paint_frame, connected, fps, state, qlabel, hover, pressed):
        self.layout(W, H)
        pw, ph = self.pw, self.ph
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        r_screen = RS * pw
        r_out = r_screen + BZ * pw

        # тень телефона
        for i in range(9, 0, -1):
            e = i * 0.0032 * pw
            rrect(cr, self.bx - e, self.by - e + 0.012 * pw, self.bw + 2 * e, self.bh + 2 * e, r_out + e)
            cr.set_source_rgba(0, 0, 0, 0.035)
            cr.fill()

        # рамка (корпус)
        rrect(cr, self.bx, self.by, self.bw, self.bh, r_out)
        g = cairo.LinearGradient(0, self.by, 0, self.by + self.bh)
        g.add_color_stop_rgba(0, 0.16, 0.16, 0.19, 1)
        g.add_color_stop_rgba(1, 0.04, 0.04, 0.05, 1)
        cr.set_source(g)
        cr.fill()
        rrect(cr, self.bx + 0.5, self.by + 0.5, self.bw - 1, self.bh - 1, r_out)
        g = cairo.LinearGradient(0, self.by, 0, self.by + self.bh)
        g.add_color_stop_rgba(0, 1, 1, 1, 0.55)
        g.add_color_stop_rgba(0.5, 1, 1, 1, 0.12)
        g.add_color_stop_rgba(1, 1, 1, 1, 0.30)
        cr.set_source(g)
        cr.set_line_width(1.3)
        cr.stroke()

        # экран
        cr.save()
        rrect(cr, self.px, self.py, pw, ph, r_screen)
        cr.clip()
        if paint_frame is not None and connected:
            cr.translate(self.px, self.py)
            paint_frame(cr, pw, ph)
        else:
            g = cairo.LinearGradient(0, self.py, pw * 0.4, self.py + ph)
            g.add_color_stop_rgb(0, 0.08, 0.10, 0.22)
            g.add_color_stop_rgb(1, 0.20, 0.10, 0.34)
            cr.set_source(g)
            cr.paint()
        cr.restore()
        rrect(cr, self.px, self.py, pw, ph, r_screen)
        cr.set_source_rgba(1, 1, 1, 0.10)
        cr.set_line_width(1)
        cr.stroke()

        k = max(0.75, min(1.5, pw / 320))
        cy_scr = self.py + ph / 2
        if connected:
            put_text(cr, "%d fps" % fps, self.px + 0.045 * pw, self.py + 0.072 * pw,
                     max(11, 0.036 * pw), bold=True)
        else:
            put_text(cr, "USB Screen", self.px + pw / 2, cy_scr - 58 * k, 24 * k, bold=True, center=True)
            put_text(cr, "Введите PIN из приложения и нажмите Enter", self.px + pw / 2, cy_scr - 32 * k,
                     12.5 * k, alpha=0.8, center=True)
            for i, line in enumerate(wrap_text(cr, state, 13 * k, pw * 0.82)):
                put_text(cr, line, self.px + pw / 2, cy_scr + 52 * k + i * 18 * k, 13 * k,
                         alpha=0.9, center=True)

        self._draw_bar(cr, qlabel, hover, pressed)

    def _draw_bar(self, cr, qlabel, hover, pressed):
        bx, by, bw, bh = self.bar
        # «стекло»: тёмная полупрозрачная основа + блик + светлая кромка
        rrect(cr, bx, by, bw, bh, bh / 2)
        g = cairo.LinearGradient(0, by, 0, by + bh)
        g.add_color_stop_rgba(0, 0.30, 0.31, 0.38, 0.62)
        g.add_color_stop_rgba(1, 0.10, 0.10, 0.15, 0.70)
        cr.set_source(g)
        cr.fill_preserve()
        cr.save()
        cr.clip()
        g = cairo.LinearGradient(0, by, 0, by + bh * 0.62)
        g.add_color_stop_rgba(0, 1, 1, 1, 0.34)
        g.add_color_stop_rgba(1, 1, 1, 1, 0.0)
        cr.set_source(g)
        cr.rectangle(bx, by, bw, bh * 0.62)
        cr.fill()
        cr.restore()
        rrect(cr, bx + 0.5, by + 0.5, bw - 1, bh - 1, bh / 2)
        g = cairo.LinearGradient(0, by, 0, by + bh)
        g.add_color_stop_rgba(0, 1, 1, 1, 0.65)
        g.add_color_stop_rgba(0.5, 1, 1, 1, 0.18)
        g.add_color_stop_rgba(1, 1, 1, 1, 0.38)
        cr.set_source(g)
        cr.set_line_width(1.2)
        cr.stroke()

        s = bh * 0.22
        for i, (kind, (rx, ry, rw, rh)) in enumerate(zip(BUTTONS, self.btn_rects)):
            cx, cy = rx + rw / 2, ry + rh / 2
            if i == pressed or i == hover:
                cr.new_path()
                cr.arc(cx, cy, bh * 0.41, 0, 2 * math.pi)
                cr.set_source_rgba(1, 1, 1, 0.30 if i == pressed else 0.15)
                cr.fill()
            draw_icon(cr, kind, cx, cy, s, qlabel)


# ====================================================================== приложение
CSS = b"""
window { background-color: transparent; }
entry.glass {
  background-image: none; background-color: rgba(255,255,255,0.14); color: white;
  border: 1px solid rgba(255,255,255,0.40); border-radius: 22px;
  padding: 6px 16px; min-height: 22px; font-size: 20px; box-shadow: none; caret-color: white;
}
entry.glass:focus { background-color: rgba(255,255,255,0.20); border-color: rgba(255,255,255,0.75); }
"""

CURSORS = {
    "NW": ("nw-resize", Gdk.WindowEdge.NORTH_WEST), "N": ("n-resize", Gdk.WindowEdge.NORTH),
    "NE": ("ne-resize", Gdk.WindowEdge.NORTH_EAST), "W": ("w-resize", Gdk.WindowEdge.WEST),
    "E": ("e-resize", Gdk.WindowEdge.EAST), "SW": ("sw-resize", Gdk.WindowEdge.SOUTH_WEST),
    "S": ("s-resize", Gdk.WindowEdge.SOUTH), "SE": ("se-resize", Gdk.WindowEdge.SOUTH_EAST),
}


class App:
    def __init__(self, pin=""):
        self.ui = Ui()
        self.flock = threading.Lock()
        self.latest = None
        self.pixbuf = None
        self.state = "Ищу телефон…"
        self.connected = False
        self.fps = 0
        self.frames = 0
        self.fps_at = time.time()
        self.qidx = 1
        self.hover = -1
        self.pressed = -1
        self.cursor_name = None
        self.down = None
        self.last_wheel = 0.0
        self.ent_top = None
        self.link = Link(self._on_frame, lambda s: GLib.idle_add(self.set_state, s))
        self._build()
        GLib.timeout_add(15, self._tick)
        if pin:
            self.entry.set_text(pin)
            GLib.timeout_add(400, lambda: (self.connect(), False)[1])
        else:
            threading.Thread(target=self._check_adb, daemon=True).start()

    # ---------------------------------------------------------------- окно
    def _build(self):
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        win = self.win = Gtk.Window(title="USB Screen")
        win.set_decorated(False)
        win.set_app_paintable(True)
        scr = win.get_screen()
        vis = scr.get_rgba_visual()
        if vis is not None and scr.is_composited():
            win.set_visual(vis)
        else:
            print("Нет композитинга рабочего стола: прозрачные углы окна будут чёрными.")
        win.connect("destroy", Gtk.main_quit)
        win.connect("key-press-event", self.on_key)

        area = self.area = Gtk.DrawingArea()
        area.set_can_focus(True)
        area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                        Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK |
                        Gdk.EventMask.SMOOTH_SCROLL_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        area.connect("draw", self.on_draw)
        area.connect("size-allocate", self.on_size)
        area.connect("button-press-event", self.on_press)
        area.connect("button-release-event", self.on_release)
        area.connect("motion-notify-event", self.on_motion)
        area.connect("leave-notify-event", self.on_leave)
        area.connect("scroll-event", self.on_scroll)

        entry = self.entry = Gtk.Entry()
        entry.get_style_context().add_class("glass")
        entry.set_placeholder_text("PIN")
        entry.set_max_length(6)
        entry.set_width_chars(8)
        entry.set_alignment(0.5)
        entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
        entry.set_halign(Gtk.Align.CENTER)
        entry.set_valign(Gtk.Align.START)
        entry.connect("activate", lambda e: self.connect())

        overlay = Gtk.Overlay()
        overlay.add(area)
        overlay.add_overlay(entry)
        win.add(overlay)

        # стартовый размер под экран ПК
        sw, sh = scr.get_width(), scr.get_height()
        wu, hu = Ui.units(self.ui.aspect)
        pw = min(380.0, (sh - 110) / hu, (sw - 40) / wu)
        win.set_default_size(int(pw * wu), int(pw * hu))
        self._apply_hints()
        win.show_all()
        entry.grab_focus()

    def _apply_hints(self):
        wu, hu = Ui.units(self.ui.aspect)
        geo = Gdk.Geometry()
        geo.min_aspect = geo.max_aspect = wu / hu
        geo.min_width = 200
        geo.min_height = int(200 * hu / wu)
        self.win.set_geometry_hints(None, geo, Gdk.WindowHints.ASPECT | Gdk.WindowHints.MIN_SIZE)

    def set_aspect(self, new_a):
        """Экран повернули/сменился формат: подгоняем окно, сохраняя длинную сторону."""
        old_a, old_pw = self.ui.aspect, max(self.ui.pw, 100.0)
        long_side = max(old_pw, old_pw * old_a)
        pw = long_side if new_a <= 1 else long_side / new_a
        scr = self.win.get_screen()
        wu, hu = Ui.units(new_a)
        pw = min(pw, (scr.get_height() - 110) / hu, (scr.get_width() - 40) / wu)
        self.ui.aspect = new_a
        self._apply_hints()
        self.win.resize(int(pw * wu), int(pw * hu))

    # ---------------------------------------------------------------- состояние
    def set_state(self, text):
        self.state = text
        self.connected = text == "Подключено"
        if self.connected:
            self.entry.hide()
            self.area.grab_focus()
        else:
            self.entry.show()
            self.pixbuf = None if not self.link.sock else self.pixbuf
        self.area.queue_draw()
        return False

    def popup(self, msg):
        dlg = Gtk.MessageDialog(transient_for=self.win, modal=True, message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.OK, text="USB Screen")
        dlg.format_secondary_text(msg)
        dlg.run()
        dlg.destroy()
        return False

    def _check_adb(self):
        err = adb_forward()
        GLib.idle_add(self.set_state, err or "Телефон найден по кабелю. Введите PIN из приложения.")

    def connect(self):
        pin = self.entry.get_text().strip()
        if not pin:
            self.set_state("Введите PIN из приложения на телефоне (он виден после «Начать трансляцию»).")
            return
        self.set_state("Ищу телефон по кабелю…")
        threading.Thread(target=self._connect_worker, args=(pin,), daemon=True).start()

    def _connect_worker(self, pin):
        err = adb_forward()
        if err:
            GLib.idle_add(self.set_state, err)
            GLib.idle_add(self.popup, err)
            return
        self.link.connect(pin)

    # ---------------------------------------------------------------- кадры
    def _on_frame(self, data):
        self.frames += 1
        with self.flock:
            self.latest = data

    def _tick(self):
        with self.flock:
            data, self.latest = self.latest, None
        dirty = False
        if data:
            try:
                ld = GdkPixbuf.PixbufLoader()
                ld.write(data)
                ld.close()
                pb = ld.get_pixbuf()
                if pb is not None:
                    self.pixbuf = pb
                    a = pb.get_height() / pb.get_width()
                    if abs(a - self.ui.aspect) > 0.02:
                        self.set_aspect(a)
                    dirty = True
            except GLib.Error:
                pass
        now = time.time()
        if now - self.fps_at >= 1:
            self.fps = round(self.frames / (now - self.fps_at))
            self.frames = 0
            self.fps_at = now
            dirty = True
        if dirty:
            self.area.queue_draw()
        return True

    def _paint_frame(self, cr, pw, ph):
        pb = self.pixbuf
        if pb is None:
            return
        cr.scale(pw / pb.get_width(), ph / pb.get_height())
        Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
        cr.paint()

    # ---------------------------------------------------------------- рисование/раскладка
    def on_draw(self, widget, cr):
        W, H = widget.get_allocated_width(), widget.get_allocated_height()
        self.ui.draw(cr, W, H, self._paint_frame, self.connected and self.pixbuf is not None,
                     self.fps, self.state, PRESETS[self.qidx][1], self.hover, self.pressed)
        return False

    def on_size(self, widget, alloc):
        self.ui.layout(alloc.width, alloc.height)
        top = int(self.ui.py + self.ui.ph / 2 - 20)
        if top != self.ent_top:
            self.ent_top = top
            self.entry.set_margin_top(max(top, 0))

    # ---------------------------------------------------------------- мышь
    @staticmethod
    def edge_at(x, y, W, H):
        E, C = 7, 20
        left = x < E or (x < C and (y < E or y > H - E))
        right = x > W - E or (x > W - C and (y < E or y > H - E))
        top = y < E or (y < C and (x < E or x > W - E))
        bottom = y > H - E or (y > H - C and (x < E or x > W - E))
        name = ("N" if top else "S" if bottom else "") + ("W" if left else "E" if right else "")
        return name or None

    def _set_cursor(self, name):
        if name != self.cursor_name:
            self.cursor_name = name
            gw = self.area.get_window()
            if gw:
                gw.set_cursor(Gdk.Cursor.new_from_name(Gdk.Display.get_default(), name))

    def on_motion(self, w, ev):
        W, H = w.get_allocated_width(), w.get_allocated_height()
        edge = self.edge_at(ev.x, ev.y, W, H)
        hit = self.ui.hit(ev.x, ev.y)
        if edge:
            self._set_cursor(CURSORS[edge][0])
        elif hit[0] == "btn":
            self._set_cursor("pointer")
        else:
            self._set_cursor("default")
        hover = hit[1] if hit[0] == "btn" and not edge else -1
        if hover != self.hover:
            self.hover = hover
            w.queue_draw()
        return False

    def on_leave(self, w, ev):
        if self.hover != -1:
            self.hover = -1
            w.queue_draw()
        return False

    def on_press(self, w, ev):
        W, H = w.get_allocated_width(), w.get_allocated_height()
        self.area.grab_focus()
        edge = self.edge_at(ev.x, ev.y, W, H)
        if edge and ev.button == 1:
            self.win.begin_resize_drag(CURSORS[edge][1], ev.button, int(ev.x_root), int(ev.y_root), ev.time)
            return True
        hit = self.ui.hit(ev.x, ev.y)
        if hit[0] == "screen":
            if ev.button == 1:
                self.down = (ev.x, ev.y, time.time())
            elif ev.button == 3:
                self.link.send("back")
            elif ev.button == 2:
                self.link.send("home")
        elif hit[0] == "btn" and ev.button == 1:
            self.pressed = hit[1]
            w.queue_draw()
        elif ev.button == 1:
            self.win.begin_move_drag(ev.button, int(ev.x_root), int(ev.y_root), ev.time)
        elif ev.button == 3:
            menu = Gtk.Menu()
            item = Gtk.MenuItem(label="Закрыть")
            item.connect("activate", lambda i: Gtk.main_quit())
            menu.append(item)
            menu.show_all()
            menu.popup_at_pointer(ev)
        return True

    def on_release(self, w, ev):
        if self.pressed >= 0:
            idx, self.pressed = self.pressed, -1
            w.queue_draw()
            hit = self.ui.hit(ev.x, ev.y)
            if hit == ("btn", idx):
                self.press_button(BUTTONS[idx])
            return True
        if self.down and ev.button == 1:
            x0, y0, t0 = self.down
            self.down = None
            p0 = self.ui.to_norm(x0, y0, True)
            p1 = self.ui.to_norm(ev.x, ev.y, True)
            ms = int((time.time() - t0) * 1000)
            if math.hypot(ev.x - x0, ev.y - y0) < 8:
                self.link.send("tap %.4f %.4f %d" % (p0[0], p0[1], min(max(ms, 40), 1500)))
            else:
                self.link.send("swipe %.4f %.4f %.4f %.4f %d" %
                               (p0[0], p0[1], p1[0], p1[1], min(max(ms, 80), 2000)))
        return True

    def press_button(self, kind):
        if kind == "quality":
            self.qidx = (self.qidx + 1) % len(PRESETS)
            self.link.send("q %d" % PRESETS[self.qidx][0])
            self.area.queue_draw()
        else:
            self.link.send(kind)

    def on_scroll(self, w, ev):
        if ev.state & Gdk.ModifierType.CONTROL_MASK:        # Ctrl + колесо — масштаб окна
            d = self._scroll_dir(ev)
            if d:
                W, H = self.win.get_size()
                f = 1.07 if d > 0 else 1 / 1.07
                scr = self.win.get_screen()
                f = min(f, (scr.get_width() - 20) / W, (scr.get_height() - 60) / H)
                self.win.resize(max(200, int(W * f)), max(int(200 * H / W), int(H * f)))
            return True
        if self.ui.hit(ev.x, ev.y)[0] != "screen":
            return False
        now = time.time()
        d = self._scroll_dir(ev)
        if not d or now - self.last_wheel < 0.15:
            return True
        self.last_wheel = now
        p = self.ui.to_norm(ev.x, ev.y)
        a, b = (0.35, 0.65) if d > 0 else (0.65, 0.35)
        self.link.send("swipe %.4f %.4f %.4f %.4f 180" % (p[0], a, p[0], b))
        return True

    @staticmethod
    def _scroll_dir(ev):
        """+1 — колесо вверх, -1 — вниз, 0 — нет."""
        if ev.direction == Gdk.ScrollDirection.UP:
            return 1
        if ev.direction == Gdk.ScrollDirection.DOWN:
            return -1
        if ev.direction == Gdk.ScrollDirection.SMOOTH:
            ok, dx, dy = ev.get_scroll_deltas()
            if ok and abs(dy) > 0.01:
                return 1 if dy < 0 else -1
        return 0

    # ---------------------------------------------------------------- клавиатура
    def on_key(self, w, ev):
        if ev.state & Gdk.ModifierType.CONTROL_MASK and ev.keyval in (Gdk.KEY_q, Gdk.KEY_Q):
            Gtk.main_quit()
            return True
        if self.entry.get_visible() and w.get_focus() is self.entry:
            return False                                    # печатаем PIN
        name = Gdk.keyval_name(ev.keyval)
        if name == "BackSpace":
            self.link.send("bs")
        elif name in ("Return", "KP_Enter"):
            self.link.send("enter")
        elif name == "Escape":
            self.link.send("back")
        elif ev.string and ev.string.isprintable():
            self.link.send("t " + " ".join("%x" % ord(c) for c in ev.string))
        else:
            return False
        return True


if __name__ == "__main__":
    App(sys.argv[1] if len(sys.argv) > 1 else "")
    Gtk.main()
