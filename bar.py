"""Always-on-top calorie bar.

The target (the number before K) is editable. The fill runs from orange
to green as current approaches the target. The pill is drawn with a
soft alpha edge so the outline is not a stair-stepped oval.
"""

import base64
import ctypes
import io
import math
import re
import struct
import sys
import threading
import time
import tkinter as tk
import zlib
from ctypes import wintypes
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from listen import AntSession


ORANGE = (255, 152, 0)
GREEN = (139, 195, 74)
WHITE = (255, 255, 255)
INK = (17, 17, 17)
GOOD = (46, 150, 70)
BAD = (196, 48, 42)
WIDTH = 460
HEIGHT = 52
GLOW_PAD = 18
FONT_PATH = Path(r"C:\Windows\Fonts\segoeuib.ttf")


def _asset(name: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / name
# 10 seconds while running from source, 60 seconds in the packaged build.
LINK_HOLD_SECONDS = 60 if getattr(sys, "frozen", False) else 10
LINK_OFF = "off"
LINK_SEARCHING = "searching"
LINK_ON = "on"

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
ULW_ALPHA = 0x00000002
AC_SRC_ALPHA = 0x01


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _Size(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _Blend(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def _enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()


def _hwnd(widget: tk.Misc) -> int:
    widget.update_idletasks()
    child = widget.winfo_id()
    parent = ctypes.windll.user32.GetParent(child)
    return parent or child


def _apply_alpha(hwnd: int, image: Image.Image, origin: tuple[int, int]) -> None:
    """Show an RGBA image as the window, with a real per-pixel alpha edge."""
    image = image.convert("RGBA")
    width, height = image.size
    raw = image.tobytes("raw", "BGRA")
    stride = width * 4
    flipped = bytearray()
    for y in range(height - 1, -1, -1):
        flipped.extend(raw[y * stride : (y + 1) * stride])
    for i in range(0, len(flipped), 4):
        alpha = flipped[i + 3]
        if alpha < 255:
            flipped[i] = flipped[i] * alpha // 255
            flipped[i + 1] = flipped[i + 1] * alpha // 255
            flipped[i + 2] = flipped[i + 2] * alpha // 255

    get_long = ctypes.windll.user32.GetWindowLongPtrW
    set_long = ctypes.windll.user32.SetWindowLongPtrW
    get_long.restype = ctypes.c_void_p
    set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    style = get_long(hwnd, GWL_EXSTYLE) or 0
    set_long(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)

    header = _BitmapInfoHeader()
    header.biSize = ctypes.sizeof(_BitmapInfoHeader)
    header.biWidth = width
    header.biHeight = height
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = 0

    screen = ctypes.windll.user32.GetDC(0)
    bits = ctypes.c_void_p()
    bitmap = ctypes.windll.gdi32.CreateDIBSection(screen, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    ctypes.memmove(bits, bytes(flipped), len(flipped))
    memory = ctypes.windll.gdi32.CreateCompatibleDC(screen)
    previous = ctypes.windll.gdi32.SelectObject(memory, bitmap)

    blend = _Blend(0, 0, 255, AC_SRC_ALPHA)
    destination = _Point(origin[0], origin[1])
    source = _Point(0, 0)
    size = _Size(width, height)
    update = ctypes.windll.user32.UpdateLayeredWindow
    update.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(_Point),
        ctypes.POINTER(_Size),
        ctypes.c_void_p,
        ctypes.POINTER(_Point),
        wintypes.DWORD,
        ctypes.POINTER(_Blend),
        wintypes.DWORD,
    ]
    update.restype = wintypes.BOOL
    ok = update(hwnd, screen, ctypes.byref(destination), ctypes.byref(size), memory, ctypes.byref(source), 0, ctypes.byref(blend), ULW_ALPHA)
    if not ok:
        raise ctypes.WinError()

    ctypes.windll.gdi32.SelectObject(memory, previous)
    ctypes.windll.gdi32.DeleteObject(bitmap)
    ctypes.windll.gdi32.DeleteDC(memory)
    ctypes.windll.user32.ReleaseDC(0, screen)


class CalorieBar:
    def __init__(self, target: int = 700, current: float = 0) -> None:
        self.target = max(1, int(target))
        self.current = max(0.0, float(current))
        self._editor = None
        self._target_box = (0, 0, 0, 0)
        self._power: list[tuple[float, float]] = []
        self._trace: list[tuple[float, float | None, float | None]] = []
        self._samples: list[tuple[float, float]] = []
        self._link = LINK_SEARCHING
        self._linked_at = 0.0
        self._last_power_at: float | None = None
        self._ant = AntSession()
        self._slot = 0
        self._scroll = None
        self._leaving = ""
        self._entering = ""
        self._drag_origin = (0, 0)
        self._pulsing = False
        self._close_icon = Image.open(io.BytesIO(base64.b64decode(_close_png(_asset("closeb.svg"), 64)))).resize((30, 30), Image.Resampling.LANCZOS)
        self._font = ImageFont.truetype(str(FONT_PATH), 14)

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.geometry(f"{WIDTH + 2 * GLOW_PAD}x{HEIGHT + 2 * GLOW_PAD}+{80 - GLOW_PAD}+{80 - GLOW_PAD}")
        self.root.bind("<ButtonPress-1>", self._press)
        self.root.bind("<B1-Motion>", self._drag)
        self.root.bind("<Motion>", self._hover)
        self._redraw()
        self._animate()
        self._ant.start(self._ant_link, self._ant_watts)

    def set_link(self, state: str) -> None:
        """off, searching, or on. The green mark yields to the time estimate after LINK_HOLD_SECONDS."""
        self._link = state
        if state == LINK_ON:
            self._linked_at = time.monotonic()
        self._redraw()

    def _mark_color(self) -> tuple[int, int, int] | None:
        if self._link == LINK_OFF:
            return (168, 168, 168)
        if self._link == LINK_SEARCHING:
            wave = 0.5 + 0.5 * math.sin(time.monotonic() * 2.2)
            light = (150, 210, 235)
            deep = (15, 105, 185)
            return tuple(round(a + (b - a) * wave) for a, b in zip(light, deep))
        if self._link == LINK_ON and time.monotonic() - self._linked_at < LINK_HOLD_SECONDS:
            return (56, 158, 72)
        return None

    def _animate(self) -> None:
        if self._mark_color() is None:
            self.root.after(10000, self._flip_slot)
            return
        self._redraw()
        delay = 80 if self._link == LINK_SEARCHING else 500
        self.root.after(delay, self._animate)

    def _flip_slot(self) -> None:
        if self._mark_color() is not None:
            self._animate()
            return
        rate = _burn_rate(self._samples)
        remaining = self.target - self.current
        self._leaving = self._slot_view(remaining, rate)
        self._slot = (self._slot + 1) % 3
        self._entering = self._slot_view(remaining, rate)
        self._scroll_started = time.monotonic()
        self._tick_scroll()

    def _tick_scroll(self) -> None:
        elapsed = time.monotonic() - self._scroll_started
        duration = 0.45
        t = min(1.0, elapsed / duration)
        self._scroll = t * t * (3 - 2 * t)
        self._redraw()
        if t < 1:
            self.root.after(16, self._tick_scroll)
            return
        self._scroll = None
        self._redraw()
        self.root.after(10000, self._flip_slot)

    def set_kcal(self, kcal: float) -> None:
        self.current = max(0.0, float(kcal))
        now = time.monotonic()
        self._samples.append((now, self.current))
        self._samples = [(stamp, value) for stamp, value in self._samples if now - stamp <= 30]
        self._redraw()
        self._sync_pulse()

    def note_power(self, watts: float) -> None:
        now = time.monotonic()
        self._power.append((now, max(0.0, float(watts))))
        self._power = [(stamp, value) for stamp, value in self._power if now - stamp <= 150]
        if self._slot == 2 and self._scroll is None:
            self._redraw()

    def add_kcal(self, kcal: float) -> None:
        self.set_kcal(self.current + kcal)

    def mainloop(self) -> None:
        try:
            self.root.mainloop()
        finally:
            self._ant.stop()

    def _ant_link(self, state: str) -> None:
        try:
            self.root.after(0, lambda: self.set_link(state))
        except tk.TclError:
            pass

    def _ant_watts(self, watts: float) -> None:
        try:
            self.root.after(0, lambda: self._apply_watts(watts))
        except tk.TclError:
            pass

    def _apply_watts(self, watts: float) -> None:
        now = time.monotonic()
        previous = self._last_power_at
        self._last_power_at = now
        if previous is not None:
            elapsed = now - previous
            if 0 < elapsed < 5:
                self.add_kcal(max(0.0, watts) * elapsed / 4184)
        self.note_power(watts)

    def _redraw(self) -> None:
        fraction = min(1.0, self.current / self.target)
        fill = _mix(ORANGE, GREEN, fraction)
        rate = _burn_rate(self._samples)
        remaining = self.target - self.current
        self._note_trace(remaining, rate)
        label, marker = self._slot_view(remaining, rate)
        image, self._target_box = _render_bar(
            self.current,
            self.target,
            fill,
            self._font,
            self._close_icon,
            label,
            self._mark_color(),
            None if self._scroll is None else (self._leaving, self._entering, self._scroll),
            marker,
        )
        _apply_alpha(_hwnd(self.root), image, (self.root.winfo_x(), self.root.winfo_y()))

    def _slot_view(self, remaining: float, rate: float | None) -> tuple[str, tuple | None]:
        if self._slot == 1:
            text = _hourly_text(remaining, rate)
            current = _hourly_value(remaining, rate)
            previous = self._value_ago(2)
            kind = "hour"
        elif self._slot == 2:
            text, current, previous = _stability_text(self._power)
            kind = "dev"
        else:
            text = _eta_text(remaining, rate)
            current = _eta_minutes(remaining, rate)
            previous = self._value_ago(1)
            kind = "eta"
        return text, _move_marker(kind, current, previous)

    def _note_trace(self, remaining: float, rate: float | None) -> None:
        now = time.monotonic()
        row = (now, _eta_minutes(remaining, rate), _hourly_value(remaining, rate))
        if self._trace and now - self._trace[-1][0] < 1:
            self._trace[-1] = row
            return
        self._trace.append(row)
        self._trace = [item for item in self._trace if now - item[0] <= 45]

    def _value_ago(self, field: int) -> float | None:
        target = time.monotonic() - 30
        earlier = [item for item in self._trace if item[0] <= target]
        if not earlier or target - earlier[-1][0] > 15:
            return None
        return earlier[-1][field]

    def _hover(self, event: tk.Event) -> None:
        on_close = _hit_close(event.x, event.y)
        on_target = _inside(event.x, event.y, self._target_box)
        self.root.configure(cursor="hand2" if on_close or on_target else "")

    def _press(self, event: tk.Event) -> None:
        if _hit_close(event.x, event.y):
            self.root.after(0, self.root.destroy)
            return
        if _inside(event.x, event.y, self._target_box):
            self._begin_edit()
            return
        self._drag_origin = (event.x_root, event.y_root)

    def _drag(self, event: tk.Event) -> None:
        if _hit_close(event.x, event.y) or self._editor is not None:
            return
        dx = event.x_root - self._drag_origin[0]
        dy = event.y_root - self._drag_origin[1]
        self._drag_origin = (event.x_root, event.y_root)
        x = self.root.winfo_x() + dx
        y = self.root.winfo_y() + dy
        self.root.geometry(f"+{x}+{y}")
        self._redraw()

    def _begin_edit(self) -> None:
        if self._editor is not None:
            return
        left, top, width, height = self._target_box
        fraction = min(1.0, self.current / self.target)
        fill = _mix(ORANGE, GREEN, fraction)
        color = fill if left < int(WIDTH * fraction) else "#ffffff"
        editor = tk.Toplevel(self.root)
        editor.overrideredirect(True)
        editor.attributes("-topmost", True)
        editor.geometry(f"{max(36, width + 8)}x{height + 4}+{self.root.winfo_x() + left - 2}+{self.root.winfo_y() + top - 2}")
        entry = tk.Entry(
            editor,
            font=("Segoe UI", 14, "bold"),
            fg="#111111",
            bg=color,
            relief="flat",
            justify="left",
            insertbackground="#111111",
            borderwidth=0,
            highlightthickness=0,
        )
        entry.insert(0, str(self.target))
        entry.selection_range(0, "end")
        entry.pack(fill="both", expand=True)
        entry.focus_set()
        entry.bind("<Return>", self._commit_target)
        entry.bind("<FocusOut>", self._commit_target)
        self._editor = editor
        self._entry = entry

    def _commit_target(self, event=None) -> None:
        if self._editor is None:
            return
        raw = self._entry.get().strip()
        if raw.isdigit() and int(raw) > 0:
            self.target = int(raw)
        editor = self._editor
        self._editor = None
        editor.destroy()
        self._redraw()
        self._sync_pulse()

    def _sync_pulse(self) -> None:
        reached = self.current >= self.target
        if reached and not self._pulsing:
            self._pulsing = True
            self.root.after(50, self._pulse)
            return
        if not reached:
            self._pulsing = False

    def _pulse(self) -> None:
        if not self._pulsing or self.current < self.target:
            self._pulsing = False
            return
        self._redraw()
        self.root.after(50, self._pulse)


def _slot_text(index: int, remaining: float, kcal_per_second: float | None, power: list[tuple[float, float]]) -> str:
    if index == 1:
        return _hourly_text(remaining, kcal_per_second)
    if index == 2:
        return _stability_text(power)[0]
    return _eta_text(remaining, kcal_per_second)


def _stability_text(power: list[tuple[float, float]]) -> tuple[str, float | None, float | None]:
    """Mean watts of recent 30 s blocks, and their mean absolute deviation.

    The third value is the same deviation for the window one block earlier.
    """
    current = _block_deviation(power, 1)
    previous = _block_deviation(power, 2)
    if current is None:
        return "~ 188 Вт ±5%", None, None
    average, deviation = current
    return f"{int(round(average))} Вт ±{int(round(deviation))}%", deviation, previous[1] if previous else None


def _block_deviation(power: list[tuple[float, float]], first: int) -> tuple[float, float] | None:
    now = time.monotonic()
    means = []
    for older in range(first, first + 4):
        end = now - older * 30
        start = end - 30
        values = [watts for stamp, watts in power if start < stamp <= end]
        if values:
            means.append(sum(values) / len(values))
    if len(means) < 2 or sum(means) <= 0:
        return None
    average = sum(means) / len(means)
    deviation = sum(abs(value - average) for value in means) / len(means) / average * 100
    return average, deviation


def _move_marker(kind: str, current: float | None, previous: float | None) -> tuple[str, tuple[int, int, int]] | None:
    if current is None or previous is None or abs(current - previous) < 0.5:
        return None
    rising = current > previous
    if kind == "hour":
        return ("up", GOOD) if rising else ("down", BAD)
    return ("up", BAD) if rising else ("down", GOOD)


def _eta_minutes(remaining: float, kcal_per_second: float | None) -> float | None:
    if remaining <= 0 or kcal_per_second is None or kcal_per_second <= 0:
        return None
    return remaining / kcal_per_second / 60


def _hourly_value(remaining: float, kcal_per_second: float | None) -> float | None:
    if kcal_per_second is None or kcal_per_second <= 0:
        return None
    return kcal_per_second * 3600


def _burn_rate(samples: list[tuple[float, float]]) -> float | None:
    if len(samples) < 2:
        return None
    start, end = samples[0], samples[-1]
    elapsed = end[0] - start[0]
    gained = end[1] - start[1]
    if elapsed < 5 or gained <= 0:
        return None
    return gained / elapsed


def _hourly_text(remaining: float, kcal_per_second: float | None) -> str:
    if kcal_per_second is not None and kcal_per_second > 0:
        per_hour = int(round(kcal_per_second * 3600))
    elif remaining > 0:
        per_hour = int(round(remaining / (40 / 60)))
    else:
        per_hour = 0
    return f"~ {per_hour} К/ч"


def _eta_text(remaining: float, kcal_per_second: float | None) -> str:
    if remaining <= 0:
        return "0 мин"
    if kcal_per_second is None or kcal_per_second <= 0:
        return "~ 40 мин"
    minutes = int(round(remaining / kcal_per_second / 60))
    if minutes < 1:
        return "< 1 мин"
    if minutes < 60:
        return f"~ {minutes} мин"
    hours, mins = divmod(minutes, 60)
    return f"~ {hours} ч {mins:02d} мин"


_PILL_CACHE: dict[tuple[int, str], Image.Image] = {}


def _base_pill(fill_to: int, fill: str) -> Image.Image:
    cached = _PILL_CACHE.get((fill_to, fill))
    if cached is not None:
        return cached.copy()
    scale = 4
    big = Image.new("RGBA", (WIDTH * scale, HEIGHT * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)
    draw.rounded_rectangle((0, 0, WIDTH * scale - 1, HEIGHT * scale - 1), radius=HEIGHT * scale // 2, fill=WHITE + (255,))
    if fill_to > 0:
        color = Image.new("RGBA", big.size, _rgb(fill) + (255,))
        mask = Image.new("L", big.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, WIDTH * scale - 1, HEIGHT * scale - 1), radius=HEIGHT * scale // 2, fill=255)
        clip = Image.new("L", big.size, 0)
        ImageDraw.Draw(clip).rectangle((0, 0, fill_to * scale, HEIGHT * scale), fill=255)
        color.putalpha(Image.frombytes("L", big.size, bytes(a & b for a, b in zip(mask.tobytes(), clip.tobytes()))))
        big = Image.alpha_composite(big, color)
    image = big.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    _PILL_CACHE.clear()
    _PILL_CACHE[(fill_to, fill)] = image
    return image.copy()


def _render_bar(
    current: float,
    target: int,
    fill: str,
    font: ImageFont.FreeTypeFont,
    close_icon: Image.Image,
    eta: str,
    mark: tuple[int, int, int] | None,
    scroll: tuple | None = None,
    marker: tuple | None = None,
):
    fill_to = int(WIDTH * min(1.0, current / target))
    image = _base_pill(fill_to, fill)
    canvas = ImageDraw.Draw(image)
    ascent, descent = font.getmetrics()
    text_top = (HEIGHT - ascent - descent) // 2
    prefix = f"{int(current)} / "
    canvas.text((22, text_top), prefix, font=font, fill=INK + (255,))
    prefix_width = int(font.getlength(prefix))
    target_text = str(target)
    target_left = 22 + prefix_width
    canvas.text((target_left, text_top), target_text, font=font, fill=INK + (255,))
    target_width = int(font.getlength(target_text))
    percent = int(round(100 * current / target)) if target else 0
    canvas.text((target_left + target_width, text_top), f" K ({percent}%)", font=font, fill=INK + (255,))
    if mark is not None:
        _draw_right(image, font, "ANT+", mark, close_icon, None)
    elif scroll is not None:
        _draw_scroll(image, font, scroll[0], scroll[1], scroll[2], close_icon)
    else:
        _draw_right(image, font, eta, INK, close_icon, marker)
    icon_x = WIDTH - HEIGHT // 2 - close_icon.width // 2
    icon_y = (HEIGHT - close_icon.height) // 2
    image.alpha_composite(close_icon, (icon_x, icon_y))
    image = _with_glow(image, current >= target)
    return image, (target_left + GLOW_PAD, text_top + GLOW_PAD, target_width, ascent + descent)


def _arrow_font() -> ImageFont.FreeTypeFont:
    for path, size in (
        (Path(r"C:\Windows\Fonts\seguibl.ttf"), 18),
        (Path(r"C:\Windows\Fonts\segoeuib.ttf"), 18),
    ):
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.truetype(str(FONT_PATH), 18)


ARROW_FONT = _arrow_font()


def _line_width(font: ImageFont.FreeTypeFont, label: str, marker: tuple | None) -> int:
    extra = int(ARROW_FONT.getlength(" ↑")) + 2 if marker else 0
    return int(font.getlength(label)) + extra


def _paint_line(draw: ImageDraw.ImageDraw, right: int, top: float, label: str, marker: tuple | None, font: ImageFont.FreeTypeFont, color: tuple[int, int, int]) -> None:
    arrow = ""
    arrow_color = color
    if marker is not None:
        direction, arrow_color = marker
        arrow = "↑" if direction == "up" else "↓"
    text_width = int(font.getlength(label))
    arrow_width = int(ARROW_FONT.getlength(arrow)) + 2 if arrow else 0
    text_x = right - arrow_width - (6 if arrow else 0) - text_width
    draw.text((text_x, top), label, font=font, fill=color + (255,))
    if not arrow:
        return
    ascent, descent = font.getmetrics()
    arrow_ascent, arrow_descent = ARROW_FONT.getmetrics()
    text_center = top + (ascent + descent) / 2
    arrow_top = text_center - (arrow_ascent + arrow_descent) / 2
    arrow_x = text_x + text_width + 6
    for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
        draw.text((arrow_x + dx, arrow_top + dy), arrow, font=ARROW_FONT, fill=arrow_color + (255,))


def _draw_right(
    image: Image.Image,
    font: ImageFont.FreeTypeFont,
    label: str,
    color: tuple[int, int, int],
    close_icon: Image.Image,
    marker: tuple | None,
) -> None:
    ascent, descent = font.getmetrics()
    text_top = (HEIGHT - ascent - descent) // 2
    width = _line_width(font, label, marker)
    right = WIDTH - HEIGHT // 2 - close_icon.width // 2 - 12
    _paint_line(ImageDraw.Draw(image), right, text_top, label, marker, font, color)


def _draw_scroll(image: Image.Image, font: ImageFont.FreeTypeFont, leaving: tuple, entering: tuple, progress: float, close_icon: Image.Image) -> None:
    ascent, descent = font.getmetrics()
    line = ascent + descent
    travel = line + 8
    text_top = (HEIGHT - line) // 2
    width = max(_line_width(font, leaving[0], leaving[1]), _line_width(font, entering[0], entering[1])) + 2
    right_edge = WIDTH - HEIGHT // 2 - close_icon.width // 2 - 12
    left = right_edge - width
    strip = image.crop((left, 0, left + width, HEIGHT))
    layer = strip.copy()
    draw = ImageDraw.Draw(layer)
    local_right = width
    for (text, marker), y in (
        (leaving, text_top - progress * travel),
        (entering, text_top + (1 - progress) * travel),
    ):
        _paint_line(draw, local_right, y, text, marker, font, INK)
    band = Image.new("L", strip.size, 0)
    ImageDraw.Draw(band).rectangle((0, text_top - 2, width, text_top + line + 2), fill=255)
    image.paste(Image.composite(layer, strip, band), (left, 0))


def _glow_phase() -> float:
    return 0.5 + 0.5 * math.sin(time.monotonic() * 2.2)


def _with_glow(pill: Image.Image, active: bool) -> Image.Image:
    """Keep a constant margin so the bar does not jump when the halo appears."""
    canvas = Image.new("RGBA", (WIDTH + 2 * GLOW_PAD, HEIGHT + 2 * GLOW_PAD), (0, 0, 0, 0))
    if active:
        phase = _glow_phase()
        scale = 3
        big_w = canvas.width * scale
        big_h = canvas.height * scale
        mask = Image.new("L", (big_w, big_h), 0)
        draw = ImageDraw.Draw(mask)
        peak = 0.45 + 0.55 * phase
        for distance in range(GLOW_PAD, 0, -1):
            closeness = 1 - (distance - 1) / GLOW_PAD
            alpha = int(100 * peak * closeness ** 1.5)
            inset = (GLOW_PAD - distance) * scale
            radius = (HEIGHT // 2 + distance) * scale
            draw.rounded_rectangle(
                (inset, inset, big_w - 1 - inset, big_h - 1 - inset),
                radius=radius,
                fill=alpha,
            )
        color = (186, 235, 120)
        halo = Image.new("RGBA", (big_w, big_h), color + (0,))
        halo.putalpha(mask)
        canvas.alpha_composite(halo.resize(canvas.size, Image.Resampling.LANCZOS))
    canvas.alpha_composite(pill, (GLOW_PAD, GLOW_PAD))
    return canvas


def _hit_close(x: int, y: int) -> bool:
    cx = GLOW_PAD + WIDTH - HEIGHT // 2
    cy = GLOW_PAD + HEIGHT // 2
    return (x - cx) ** 2 + (y - cy) ** 2 <= 16 ** 2


def _inside(x: int, y: int, box: tuple[int, int, int, int]) -> bool:
    left, top, width, height = box
    return left <= x <= left + width and top <= y <= top + height


def _rgb(color: str) -> tuple[int, int, int]:
    return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))


def _close_png(path: Path, size: int) -> str:
    """Rasterize closeb.svg (ring plus two strokes) to a PNG PhotoImage payload."""
    text = path.read_text(encoding="utf-8")
    box = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', text)
    width = float(box.group(1))
    height = float(box.group(2))
    starts = [(float(x), float(y)) for x, y in re.findall(r"M([\d.]+),([\d.]+)", text)]
    radii = []
    for token in re.findall(r"[cC](-?[\d.]+),(-?[\d.]+),?(-?[\d.]+)", text):
        radii.append(abs(float(token[2])))
    radii = radii[:2]
    cx = starts[0][0]
    cy = starts[0][1] + radii[0]
    inner, outer = sorted(radii)
    stroke = float(re.search(r"stroke-width:\s*([\d.]+)", text).group(1))
    lines = [
        tuple(float(n) for n in coords)
        for coords in re.findall(
            r'x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)"',
            text,
        )
    ]

    samples = 4
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            cover = 0
            for sy in range(samples):
                for sx in range(samples):
                    px = (x + (sx + 0.5) / samples) / size * width
                    py = (y + (sy + 0.5) / samples) / size * height
                    dist = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
                    hit = inner <= dist <= outer
                    if not hit:
                        for x1, y1, x2, y2 in lines:
                            hit = _segment_distance(px, py, x1, y1, x2, y2) <= stroke / 2
                            if hit:
                                break
                    cover += int(hit)
            alpha = round(255 * cover / (samples * samples))
            raw.extend((0, 0, 0, alpha))

    return base64.b64encode(_png(size, size, bytes(raw))).decode("ascii")


def _segment_distance(px, py, x1, y1, x2, y2) -> float:
    dx, dy = x2 - x1, y2 - y1
    length = dx * dx + dy * dy
    if length == 0:
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length))
    qx, qy = x1 + t * dx, y1 + t * dy
    return ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5


def _png(width: int, height: int, rgba: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rgba, 9)) + chunk(b"IEND", b"")


def _mix(start, end, t: float) -> str:
    t = min(1.0, max(0.0, t))
    channels = [round(a + (b - a) * t) for a, b in zip(start, end)]
    return f"#{channels[0]:02x}{channels[1]:02x}{channels[2]:02x}"


def main() -> None:
    try:
        _enable_dpi_awareness()
        CalorieBar(target=700, current=0).mainloop()
    except Exception:
        import traceback
        from tkinter import messagebox

        traceback.print_exc()
        messagebox.showerror("CalorieBar", traceback.format_exc())


if __name__ == "__main__":
    main()
