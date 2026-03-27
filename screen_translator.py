#!/usr/bin/env python3
"""
screen_translate_selector_modern.py

A small pinnable always-on-top icon for screen translation.

Changes in this version
-----------------------
- Added explicit OCR language support
- Default OCR language is Finnish (`fin`)
- Added CLI flag: --ocr-lang
- Added result window field for OCR language
- OCR now uses `-l <lang>` when calling Tesseract
- Preprocessing tuned to preserve Nordic/Finnish glyph details a bit better
- Added a clearer startup error if the requested Tesseract language data is missing

Important
---------
To recognize Finnish characters correctly, Tesseract must have Finnish trained data installed.
Typical path:
C:\\Program Files\\Tesseract-OCR\\tessdata\\fin.traineddata
"""

from __future__ import annotations

import argparse
import ctypes
import os
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk, messagebox
from dataclasses import dataclass
from typing import Optional, Tuple

import pyperclip
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import mss

try:
    from deep_translator import GoogleTranslator
except Exception:
    GoogleTranslator = None

try:
    import deepl
except Exception:
    deepl = None


DEFAULT_TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
LAUNCHER_SIZE = 76
RESULT_W = 920
RESULT_H = 720
TRANSPARENT_COLOR = "#00ff00"

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class Theme:
    BG = "#0b1020"
    BG_ELEVATED = "#11182d"
    BG_PANEL = "#151d36"
    BG_INPUT = "#0e1528"
    BG_SOFT = "#1a2443"

    TEXT = "#eef4ff"
    TEXT_MUTED = "#9ba9c6"
    TEXT_DIM = "#7f8cab"

    ACCENT = "#73e0ff"
    ACCENT_2 = "#8b7cff"
    ACCENT_3 = "#ff9d66"
    SUCCESS = "#7ef0b3"
    DANGER = "#ff7d89"

    BORDER = "#223055"
    BORDER_SOFT = "#293860"

    OCR_TEXT = "#dfe8ff"
    TRANS_TEXT = "#f6f7ff"

    FONT = "Segoe UI"
    FONT_MONO = "Consolas"


@dataclass
class AppConfig:
    source_lang: str = "auto"
    target_lang: str = "en"
    tesseract_cmd: Optional[str] = None
    ocr_psm: int = 6
    preprocess_scale: int = 3
    ocr_lang: str = "fin"


def get_virtual_screen_geometry() -> Tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return left, top, width, height


class TranslatorBackend:
    def __init__(self, source_lang: str, target_lang: str):
        self.source_lang = source_lang
        self.target_lang = target_lang
        self._deepl_client = None

        api_key = os.getenv("DEEPL_API_KEY", "").strip()
        if api_key and deepl is not None:
            self._deepl_client = deepl.Translator(api_key)

        if self._deepl_client is None and GoogleTranslator is None:
            raise RuntimeError(
                "No translation backend available. Install `deep-translator`, "
                "or install `deepl` and set DEEPL_API_KEY."
            )

    def translate(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""

        if self._deepl_client is not None:
            source = None if self.source_lang == "auto" else self.source_lang.upper()
            target = self.target_lang.upper()
            result = self._deepl_client.translate_text(
                text,
                source_lang=source,
                target_lang=target,
            )
            return str(result)

        source = "auto" if self.source_lang == "auto" else self.source_lang
        return GoogleTranslator(source=source, target=self.target_lang).translate(text)


def parse_args() -> AppConfig:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="auto")
    parser.add_argument("--target", default="en")
    parser.add_argument("--tesseract-cmd", default=None)
    parser.add_argument("--psm", type=int, default=6)
    parser.add_argument("--scale", type=int, default=3)
    parser.add_argument("--ocr-lang", default="fin")
    args = parser.parse_args()

    return AppConfig(
        source_lang=args.source,
        target_lang=args.target,
        tesseract_cmd=args.tesseract_cmd,
        ocr_psm=max(3, min(13, args.psm)),
        preprocess_scale=max(1, min(5, args.scale)),
        ocr_lang=(args.ocr_lang or "fin").strip(),
    )


class SelectionOverlay:
    def __init__(self, app: "ScreenTranslatorSelectorApp"):
        self.app = app
        self.start_x = 0
        self.start_y = 0
        self.rect_id = None
        self.label_id = None
        self.dim_label_id = None

        self.virtual_left, self.virtual_top, self.virtual_width, self.virtual_height = get_virtual_screen_geometry()

        self.win = tk.Toplevel(app.launcher)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.24)
        self.win.configure(bg="black")
        self.win.geometry(
            f"{self.virtual_width}x{self.virtual_height}+{self.virtual_left}+{self.virtual_top}"
        )

        self.canvas = tk.Canvas(self.win, bg="black", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.win.bind("<Escape>", lambda e: self.hide())

    def refresh_geometry(self) -> None:
        self.virtual_left, self.virtual_top, self.virtual_width, self.virtual_height = get_virtual_screen_geometry()
        self.win.geometry(
            f"{self.virtual_width}x{self.virtual_height}+{self.virtual_left}+{self.virtual_top}"
        )

    def show(self) -> None:
        self.refresh_geometry()
        self.canvas.delete("all")
        self.rect_id = None
        self.label_id = None
        self.dim_label_id = None
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def hide(self) -> None:
        self.win.withdraw()

    def on_press(self, event) -> None:
        self.start_x = event.x
        self.start_y = event.y

        self.canvas.create_rectangle(
            event.x - 1, event.y - 1, event.x + 1, event.y + 1,
            outline="#7de7ff", width=5
        )
        self.rect_id = self.canvas.create_rectangle(
            event.x,
            event.y,
            event.x,
            event.y,
            outline="#7de7ff",
            width=2,
            fill="#5ab6ff",
            stipple="gray25",
        )
        self.label_id = self.canvas.create_text(
            event.x + 12,
            event.y - 12,
            anchor="sw",
            text="",
            fill="white",
            font=(Theme.FONT, 11, "bold"),
        )
        self.dim_label_id = self.canvas.create_text(
            event.x + 12,
            event.y + 12,
            anchor="nw",
            text="Drag to capture",
            fill="#d6f8ff",
            font=(Theme.FONT, 10),
        )

    def on_drag(self, event) -> None:
        if not self.rect_id:
            return
        x1, y1 = self.start_x, self.start_y
        x2, y2 = event.x, event.y
        self.canvas.coords(self.rect_id, x1, y1, x2, y2)

        w = abs(x2 - x1)
        h = abs(y2 - y1)
        lx = min(x1, x2) + 12
        ly = min(y1, y2) - 12
        self.canvas.coords(self.label_id, lx, ly)
        self.canvas.coords(self.dim_label_id, lx, min(y1, y2) + 12)
        self.canvas.itemconfigure(self.label_id, text=f"{w} × {h}")
        self.canvas.itemconfigure(self.dim_label_id, text="Release to translate")

    def on_release(self, event) -> None:
        x1, y1 = self.start_x, self.start_y
        x2, y2 = event.x, event.y

        left = min(x1, x2) + self.virtual_left
        top = min(y1, y2) + self.virtual_top
        width = abs(x2 - x1)
        height = abs(y2 - y1)

        self.hide()

        if width < 10 or height < 10:
            self.app.set_status("Selection too small.")
            self.app.show_launcher_again()
            return

        self.app.capture_and_translate_region(left, top, width, height)


class RoundedCanvas(tk.Canvas):
    def create_round_rectangle(self, x1, y1, x2, y2, radius=8, **kwargs):
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        return self.create_polygon(points, smooth=True, splinesteps=24, **kwargs)


class DarkText(tk.Text):
    def __init__(self, master, fg: str, **kwargs):
        super().__init__(
            master,
            bg=Theme.BG_INPUT,
            fg=fg,
            insertbackground=Theme.TEXT,
            selectbackground=Theme.ACCENT_2,
            selectforeground=Theme.TEXT,
            highlightthickness=1,
            highlightbackground=Theme.BORDER,
            highlightcolor=Theme.ACCENT,
            relief="flat",
            bd=0,
            padx=14,
            pady=12,
            wrap="word",
            undo=False,
            **kwargs,
        )


class ScreenTranslatorSelectorApp:
    def __init__(self, config: AppConfig):
        self.config = config
        self.last_ocr_text = ""
        self.last_translation = ""
        self.last_status = "Ready"
        self.is_busy = False
        self._drag_start = None
        self._launcher_state = "normal"
        self._launcher_items: dict[str, int] = {}

        self._configure_tesseract()
        self.translator = TranslatorBackend(config.source_lang, config.target_lang)

        self.launcher = tk.Tk()
        self.launcher.title("Translate")
        self.launcher.geometry(f"{LAUNCHER_SIZE}x{LAUNCHER_SIZE}+60+120")
        self.launcher.overrideredirect(True)
        self.launcher.attributes("-topmost", True)
        self.launcher.configure(bg=TRANSPARENT_COLOR)
        self.launcher.wm_attributes("-transparentcolor", TRANSPARENT_COLOR)
        self.launcher.protocol("WM_DELETE_WINDOW", self.quit_app)

        self.style = ttk.Style()
        self._apply_theme()

        self.build_launcher()
        self.build_result_window()
        self.selector = SelectionOverlay(self)

        self.launcher.bind("<Control-Shift-q>", lambda e: self.quit_app())
        self.launcher.bind("<Control-Shift-t>", lambda e: self.start_selection())
        self.launcher.bind("<Control-Shift-c>", lambda e: self.copy_translation())

        self.schedule_status_refresh()

    def _apply_theme(self) -> None:
        self.style.theme_use("clam")

        self.style.configure(".", background=Theme.BG, foreground=Theme.TEXT, font=(Theme.FONT, 10))
        self.style.configure("Root.TFrame", background=Theme.BG)
        self.style.configure("Panel.TFrame", background=Theme.BG_ELEVATED)
        self.style.configure("Card.TFrame", background=Theme.BG_PANEL, relief="flat")
        self.style.configure("Toolbar.TFrame", background=Theme.BG_ELEVATED)
        self.style.configure("Header.TFrame", background=Theme.BG_ELEVATED)
        self.style.configure("HeaderTitle.TLabel", background=Theme.BG_ELEVATED, foreground=Theme.TEXT, font=(Theme.FONT, 19, "bold"))
        self.style.configure("HeaderSub.TLabel", background=Theme.BG_ELEVATED, foreground=Theme.TEXT_MUTED, font=(Theme.FONT, 10))
        self.style.configure("FieldLabel.TLabel", background=Theme.BG_ELEVATED, foreground=Theme.TEXT_MUTED, font=(Theme.FONT, 9, "bold"))
        self.style.configure("SectionTitle.TLabel", background=Theme.BG_PANEL, foreground=Theme.TEXT, font=(Theme.FONT, 11, "bold"))
        self.style.configure("SectionMeta.TLabel", background=Theme.BG_PANEL, foreground=Theme.TEXT_DIM, font=(Theme.FONT, 9))
        self.style.configure("Status.TLabel", background=Theme.BG_ELEVATED, foreground=Theme.TEXT, font=(Theme.FONT, 10))
        self.style.configure("Hint.TLabel", background=Theme.BG_ELEVATED, foreground=Theme.TEXT_DIM, font=(Theme.FONT, 9))

        self.style.configure(
            "Modern.TButton",
            background=Theme.BG_SOFT,
            foreground=Theme.TEXT,
            bordercolor=Theme.BORDER,
            focusthickness=0,
            focuscolor=Theme.BG_SOFT,
            lightcolor=Theme.BG_SOFT,
            darkcolor=Theme.BG_SOFT,
            padding=(12, 8),
            relief="flat",
            font=(Theme.FONT, 9, "bold"),
        )
        self.style.map(
            "Modern.TButton",
            background=[("active", "#23325c"), ("pressed", "#1b2850")],
            foreground=[("disabled", Theme.TEXT_DIM)],
            bordercolor=[("active", Theme.ACCENT)],
        )

        self.style.configure(
            "Accent.TButton",
            background=Theme.ACCENT_2,
            foreground="#ffffff",
            bordercolor=Theme.ACCENT_2,
            lightcolor=Theme.ACCENT_2,
            darkcolor=Theme.ACCENT_2,
            focuscolor=Theme.ACCENT_2,
            padding=(12, 8),
            relief="flat",
            font=(Theme.FONT, 9, "bold"),
        )
        self.style.map(
            "Accent.TButton",
            background=[("active", "#9a8eff"), ("pressed", "#7c6dff")],
            bordercolor=[("active", Theme.ACCENT)],
        )

        self.style.configure(
            "Modern.TEntry",
            fieldbackground=Theme.BG_INPUT,
            foreground=Theme.TEXT,
            insertcolor=Theme.TEXT,
            bordercolor=Theme.BORDER,
            lightcolor=Theme.BORDER,
            darkcolor=Theme.BORDER,
            padding=7,
            relief="flat",
        )
        self.style.map("Modern.TEntry", bordercolor=[("focus", Theme.ACCENT)], lightcolor=[("focus", Theme.ACCENT)])

    def _configure_tesseract(self) -> None:
        cmd = self.config.tesseract_cmd or os.getenv("TESSERACT_CMD") or DEFAULT_TESSERACT_CMD
        if os.path.exists(cmd):
            pytesseract.pytesseract.tesseract_cmd = cmd

    def build_launcher(self) -> None:
        self.launcher_canvas = RoundedCanvas(
            self.launcher,
            width=LAUNCHER_SIZE,
            height=LAUNCHER_SIZE,
            bg=TRANSPARENT_COLOR,
            highlightthickness=0,
            bd=0,
        )
        self.launcher_canvas.pack(fill="both", expand=True)

        self._launcher_items["shadow"] = self.launcher_canvas.create_oval(
            12, 14, LAUNCHER_SIZE - 4, LAUNCHER_SIZE - 2,
            fill="#060b15", outline=""
        )
        self._launcher_items["outer"] = self.launcher_canvas.create_oval(
            8, 8, LAUNCHER_SIZE - 8, LAUNCHER_SIZE - 8,
            fill="#111d37", outline="#9cf0ff", width=2
        )
        self._launcher_items["ring"] = self.launcher_canvas.create_oval(
            12, 12, LAUNCHER_SIZE - 12, LAUNCHER_SIZE - 12,
            fill="#172646", outline="#304673", width=1
        )
        self._launcher_items["inner"] = self.launcher_canvas.create_oval(
            17, 17, LAUNCHER_SIZE - 17, LAUNCHER_SIZE - 17,
            fill="#0c1530", outline="#25375d", width=1
        )
        self._launcher_items["orb"] = self.launcher_canvas.create_oval(
            23, 23, LAUNCHER_SIZE - 23, LAUNCHER_SIZE - 23,
            fill="#101f43", outline="#3f63a7", width=1
        )
        self._launcher_items["frame_top"] = self.launcher_canvas.create_line(
            25, 24, 35, 24, fill="#90e9ff", width=2, capstyle=tk.ROUND
        )
        self._launcher_items["frame_left"] = self.launcher_canvas.create_line(
            24, 25, 24, 35, fill="#90e9ff", width=2, capstyle=tk.ROUND
        )
        self._launcher_items["frame_bottom"] = self.launcher_canvas.create_line(
            51, 52, 41, 52, fill="#8e82ff", width=2, capstyle=tk.ROUND
        )
        self._launcher_items["frame_right"] = self.launcher_canvas.create_line(
            52, 51, 52, 41, fill="#8e82ff", width=2, capstyle=tk.ROUND
        )
        self._launcher_items["scan_bar"] = self.launcher_canvas.create_round_rectangle(
            28, 34, 48, 42, radius=4,
            fill="#7de7ff", outline=""
        ) if hasattr(self.launcher_canvas, "create_round_rectangle") else self.launcher_canvas.create_rectangle(
            28, 34, 48, 42, fill="#7de7ff", outline=""
        )
        self._launcher_items["scan_glow"] = self.launcher_canvas.create_line(
            29, 38, 47, 38, fill="#ecfdff", width=2, capstyle=tk.ROUND
        )
        self._launcher_items["spark1"] = self.launcher_canvas.create_oval(
            50, 16, 58, 24,
            fill="#b0f6ff", outline=""
        )
        self._launcher_items["spark2"] = self.launcher_canvas.create_oval(
            56, 23, 62, 29,
            fill="#ffc08f", outline=""
        )
        self._launcher_items["spark3"] = self.launcher_canvas.create_oval(
            18, 51, 22, 55,
            fill="#88e6ff", outline=""
        )

        self._update_launcher_visuals()

        self.launcher_canvas.bind("<ButtonPress-1>", self.on_launcher_press)
        self.launcher_canvas.bind("<B1-Motion>", self.on_launcher_drag)
        self.launcher_canvas.bind("<ButtonRelease-1>", self.on_launcher_release)
        self.launcher_canvas.bind("<ButtonPress-3>", self.show_launcher_menu)
        self.launcher_canvas.bind("<Enter>", self.on_launcher_enter)
        self.launcher_canvas.bind("<Leave>", self.on_launcher_leave)

    def _update_launcher_visuals(self) -> None:
        palette = {
            "normal": {
                "outer_fill": "#111d37",
                "outer_outline": "#9cf0ff",
                "ring_fill": "#18274a",
                "ring_outline": "#2e4271",
                "inner_fill": "#0d1630",
                "inner_outline": "#25375d",
                "orb_fill": "#101f43",
                "orb_outline": "#3f63a7",
                "spark1": "#b0f6ff",
                "spark2": "#ffc08f",
                "spark3": "#88e6ff",
                "scan_top": "#ecfdff",
                "scan_mid": "#7de7ff",
                "frame_cyan": "#90e9ff",
                "frame_violet": "#8e82ff",
            },
            "hover": {
                "outer_fill": "#162446",
                "outer_outline": "#c2f6ff",
                "ring_fill": "#1d2f58",
                "ring_outline": "#45649e",
                "inner_fill": "#10204a",
                "inner_outline": "#32518b",
                "orb_fill": "#15305f",
                "orb_outline": "#6289d5",
                "spark1": "#d0fbff",
                "spark2": "#ffd5b2",
                "spark3": "#a6f1ff",
                "scan_top": "#ffffff",
                "scan_mid": "#a4efff",
                "frame_cyan": "#c5f7ff",
                "frame_violet": "#b9acff",
            },
            "dragging": {
                "outer_fill": "#0f1831",
                "outer_outline": "#74e5ff",
                "ring_fill": "#152241",
                "ring_outline": "#29406b",
                "inner_fill": "#0b1428",
                "inner_outline": "#213251",
                "orb_fill": "#0d1a38",
                "orb_outline": "#35548a",
                "spark1": "#8cecff",
                "spark2": "#ffab73",
                "spark3": "#6addff",
                "scan_top": "#dbfbff",
                "scan_mid": "#69dcff",
                "frame_cyan": "#7ee6ff",
                "frame_violet": "#9388ff",
            },
        }[self._launcher_state]

        for item_name, fill_key, outline_key in [
            ("outer", "outer_fill", "outer_outline"),
            ("ring", "ring_fill", "ring_outline"),
            ("inner", "inner_fill", "inner_outline"),
        ]:
            self.launcher_canvas.itemconfigure(
                self._launcher_items[item_name],
                fill=palette[fill_key],
                outline=palette[outline_key],
            )

        self.launcher_canvas.itemconfigure(self._launcher_items["orb"], fill=palette["orb_fill"], outline=palette["orb_outline"])
        self.launcher_canvas.itemconfigure(self._launcher_items["spark1"], fill=palette["spark1"])
        self.launcher_canvas.itemconfigure(self._launcher_items["spark2"], fill=palette["spark2"])
        self.launcher_canvas.itemconfigure(self._launcher_items["spark3"], fill=palette["spark3"])
        self.launcher_canvas.itemconfigure(self._launcher_items["scan_bar"], fill=palette["scan_mid"])
        self.launcher_canvas.itemconfigure(self._launcher_items["scan_glow"], fill=palette["scan_top"])
        self.launcher_canvas.itemconfigure(self._launcher_items["frame_top"], fill=palette["frame_cyan"])
        self.launcher_canvas.itemconfigure(self._launcher_items["frame_left"], fill=palette["frame_cyan"])
        self.launcher_canvas.itemconfigure(self._launcher_items["frame_bottom"], fill=palette["frame_violet"])
        self.launcher_canvas.itemconfigure(self._launcher_items["frame_right"], fill=palette["frame_violet"])

    def set_launcher_state(self, state: str) -> None:
        if state == self._launcher_state:
            return
        self._launcher_state = state
        self._update_launcher_visuals()

    @staticmethod
    def _clamp(value: int, lower: int, upper: int) -> int:
        return max(lower, min(value, upper))

    def move_launcher_to(self, x: int, y: int) -> None:
        left, top, width, height = get_virtual_screen_geometry()
        max_x = left + width - LAUNCHER_SIZE
        max_y = top + height - LAUNCHER_SIZE
        x = self._clamp(x, left, max_x)
        y = self._clamp(y, top, max_y)
        self.launcher.geometry(f"+{x}+{y}")

    def build_result_window(self) -> None:
        self.result = tk.Toplevel(self.launcher)
        self.result.title("Screen Translation")
        self.result.geometry(f"{RESULT_W}x{RESULT_H}+740+120")
        self.result.configure(bg=Theme.BG)
        self.result.attributes("-topmost", True)

        outer = ttk.Frame(self.result, style="Root.TFrame", padding=16)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer, style="Header.TFrame", padding=14)
        header.pack(fill="x", pady=(0, 12))

        title_col = ttk.Frame(header, style="Header.TFrame")
        title_col.pack(side="left", fill="x", expand=True)

        ttk.Label(title_col, text="Screen Translator", style="HeaderTitle.TLabel").pack(anchor="w")
        ttk.Label(
            title_col,
            text="Capture text from any monitor, run OCR, and translate it instantly.",
            style="HeaderSub.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        hint_box = tk.Canvas(header, width=172, height=42, bg=Theme.BG_ELEVATED, highlightthickness=0, bd=0)
        hint_box.pack(side="right")
        hint_box.create_rectangle(2, 6, 170, 36, outline=Theme.BORDER_SOFT, fill=Theme.BG_PANEL, width=1)
        hint_box.create_oval(14, 14, 26, 26, fill=Theme.ACCENT, outline="")
        hint_box.create_text(38, 20, anchor="w", text="Ctrl+Shift+T", fill=Theme.TEXT, font=(Theme.FONT, 9, "bold"))
        hint_box.create_text(38, 31, anchor="w", text="start selection", fill=Theme.TEXT_MUTED, font=(Theme.FONT, 8))

        toolbar = ttk.Frame(outer, style="Toolbar.TFrame", padding=12)
        toolbar.pack(fill="x", pady=(0, 12))

        left_controls = ttk.Frame(toolbar, style="Toolbar.TFrame")
        left_controls.pack(side="left")

        ttk.Label(left_controls, text="Source", style="FieldLabel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(left_controls, text="Target", style="FieldLabel.TLabel").grid(row=0, column=2, sticky="w", padx=(10, 0))
        ttk.Label(left_controls, text="OCR lang", style="FieldLabel.TLabel").grid(row=0, column=4, sticky="w", padx=(10, 0))

        self.source_var = tk.StringVar(value=self.config.source_lang)
        self.target_var = tk.StringVar(value=self.config.target_lang)
        self.ocr_lang_var = tk.StringVar(value=self.config.ocr_lang)

        self.source_entry = ttk.Entry(left_controls, width=10, textvariable=self.source_var, style="Modern.TEntry")
        self.source_entry.grid(row=1, column=0, sticky="w", pady=(5, 0))

        swap_label = tk.Label(
            left_controls,
            text="→",
            bg=Theme.BG_ELEVATED,
            fg=Theme.TEXT_MUTED,
            font=(Theme.FONT, 12, "bold"),
        )
        swap_label.grid(row=1, column=1, padx=8, pady=(5, 0))

        self.target_entry = ttk.Entry(left_controls, width=10, textvariable=self.target_var, style="Modern.TEntry")
        self.target_entry.grid(row=1, column=2, sticky="w", padx=(10, 0), pady=(5, 0))

        self.ocr_lang_entry = ttk.Entry(left_controls, width=12, textvariable=self.ocr_lang_var, style="Modern.TEntry")
        self.ocr_lang_entry.grid(row=1, column=4, sticky="w", padx=(10, 0), pady=(5, 0))

        help_label = ttk.Label(
            left_controls,
            text="Examples: fin, eng, fin+eng",
            style="Hint.TLabel",
        )
        help_label.grid(row=2, column=0, columnspan=5, sticky="w", pady=(6, 0))

        actions = ttk.Frame(toolbar, style="Toolbar.TFrame")
        actions.pack(side="right")

        ttk.Button(actions, text="Apply", style="Modern.TButton", command=self.apply_language_settings).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Select Area", style="Accent.TButton", command=self.start_selection).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Copy", style="Modern.TButton", command=self.copy_translation).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Hide Window", style="Modern.TButton", command=self.result.withdraw).pack(side="left")

        content = ttk.Frame(outer, style="Root.TFrame")
        content.pack(fill="both", expand=True)

        content.grid_columnconfigure(0, weight=1)
        content.grid_columnconfigure(1, weight=1)
        content.grid_rowconfigure(0, weight=1)

        ocr_card = ttk.Frame(content, style="Card.TFrame", padding=12)
        ocr_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        trans_card = ttk.Frame(content, style="Card.TFrame", padding=12)
        trans_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        self._build_text_card(
            ocr_card,
            title="Detected text",
            subtitle="Raw OCR output after preprocessing",
            fg=Theme.OCR_TEXT,
            font=(Theme.FONT_MONO, 10),
            attr_name="ocr_text",
        )
        self._build_text_card(
            trans_card,
            title="Translation",
            subtitle="Translated result ready to copy",
            fg=Theme.TRANS_TEXT,
            font=(Theme.FONT, 11),
            attr_name="translation_text",
        )

        footer = ttk.Frame(outer, style="Toolbar.TFrame", padding=(12, 10))
        footer.pack(fill="x", pady=(12, 0))

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(footer, textvariable=self.status_var, style="Status.TLabel").pack(side="left")
        ttk.Label(
            footer,
            text="Right-click the floating icon for shortcuts",
            style="Hint.TLabel",
        ).pack(side="right")

    def _build_text_card(self, parent, title: str, subtitle: str, fg: str, font, attr_name: str) -> None:
        top = ttk.Frame(parent, style="Card.TFrame")
        top.pack(fill="x", pady=(0, 10))

        ttk.Label(top, text=title, style="SectionTitle.TLabel").pack(anchor="w")
        ttk.Label(top, text=subtitle, style="SectionMeta.TLabel").pack(anchor="w", pady=(2, 0))

        divider = tk.Frame(parent, bg=Theme.BORDER, height=1)
        divider.pack(fill="x", pady=(0, 10))

        text = DarkText(parent, fg=fg, font=font)
        text.pack(fill="both", expand=True)
        setattr(self, attr_name, text)

    def schedule_status_refresh(self) -> None:
        self.status_var.set(self.last_status)
        self.launcher.after(150, self.schedule_status_refresh)

    def set_status(self, text: str) -> None:
        self.last_status = text

    def on_launcher_press(self, event) -> None:
        self._drag_start = (
            event.x_root,
            event.y_root,
            self.launcher.winfo_x(),
            self.launcher.winfo_y(),
            time.time(),
        )
        self.set_launcher_state("dragging")

    def on_launcher_drag(self, event) -> None:
        if not self._drag_start:
            return
        sx, sy, ox, oy, _ = self._drag_start
        nx = ox + (event.x_root - sx)
        ny = oy + (event.y_root - sy)
        self.move_launcher_to(nx, ny)

    def on_launcher_release(self, event) -> None:
        if not self._drag_start:
            return
        sx, sy, ox, oy, press_t = self._drag_start
        moved = abs(event.x_root - sx) + abs(event.y_root - sy)
        elapsed = time.time() - press_t
        self._drag_start = None
        self.set_launcher_state("hover")

        if moved < 6 and elapsed < 0.35:
            self.start_selection()

    def on_launcher_enter(self, event) -> None:
        if self._drag_start:
            return
        self.set_launcher_state("hover")

    def on_launcher_leave(self, event) -> None:
        if self._drag_start:
            return
        self.set_launcher_state("normal")

    def show_launcher_menu(self, event) -> None:
        menu = tk.Menu(
            self.launcher,
            tearoff=0,
            bg=Theme.BG_PANEL,
            fg=Theme.TEXT,
            activebackground=Theme.BG_SOFT,
            activeforeground=Theme.TEXT,
            relief="flat",
            bd=0,
            font=(Theme.FONT, 10),
        )
        menu.add_command(label="Select Area", command=self.start_selection)
        menu.add_command(label="Show Result Window", command=self.show_result_window)
        menu.add_command(label="Copy Translation", command=self.copy_translation)
        menu.add_separator()
        menu.add_command(label="Quit", command=self.quit_app)
        menu.tk_popup(event.x_root, event.y_root)

    def show_result_window(self) -> None:
        self.result.deiconify()
        self.result.lift()
        self.result.attributes("-topmost", True)

    def show_launcher_again(self) -> None:
        self.launcher.deiconify()
        self.move_launcher_to(self.launcher.winfo_x(), self.launcher.winfo_y())
        self.launcher.lift()
        self.launcher.attributes("-topmost", True)

    def apply_language_settings(self) -> None:
        try:
            source = self.source_var.get().strip() or "auto"
            target = self.target_var.get().strip() or "en"
            ocr_lang = self.ocr_lang_var.get().strip() or "fin"

            self.translator = TranslatorBackend(source, target)
            self.config.source_lang = source
            self.config.target_lang = target
            self.config.ocr_lang = ocr_lang

            self.set_status(f"Languages updated: OCR={ocr_lang}, translate {source} → {target}")
        except Exception as e:
            self.set_status(f"Language update failed: {e}")
            messagebox.showerror("Error", str(e))

    def start_selection(self) -> None:
        if self.is_busy:
            return
        self.set_status("Selection mode active. Drag an area on any monitor.")
        self.launcher.withdraw()
        self.result.withdraw()
        self.launcher.after(80, self.selector.show)

    def capture_and_translate_region(self, left: int, top: int, width: int, height: int) -> None:
        if self.is_busy:
            return
        worker = threading.Thread(
            target=self._capture_translate_worker,
            args=(left, top, width, height),
            daemon=True,
        )
        worker.start()

    def _grab_region(self, left: int, top: int, width: int, height: int) -> Image.Image:
        monitor = {"left": left, "top": top, "width": width, "height": height}
        with mss.mss() as sct:
            shot = sct.grab(monitor)
            return Image.frombytes("RGB", shot.size, shot.rgb)

    def _capture_translate_worker(self, left: int, top: int, width: int, height: int) -> None:
        self.is_busy = True
        try:
            self.set_status(f"Capturing selection at {left},{top} ({width}×{height})…")
            image = self._grab_region(left, top, width, height)

            self.set_status("Preprocessing image for OCR…")
            processed = self.preprocess_for_ocr(image)

            self.set_status(f"Running OCR ({self.config.ocr_lang})…")
            ocr_text = self.run_ocr(processed)
            self.last_ocr_text = ocr_text
            self.result.after(0, self._update_text_widget, self.ocr_text, ocr_text)

            if not ocr_text.strip():
                self.last_translation = ""
                self.result.after(0, self._update_text_widget, self.translation_text, "")
                self.set_status("No text detected.")
                return

            self.set_status("Translating…")
            translated = self.translator.translate(ocr_text)
            self.last_translation = translated
            self.result.after(0, self._update_text_widget, self.translation_text, translated)
            self.set_status("Done.")
        except RuntimeError as e:
            err = f"{type(e).__name__}: {e}"
            self.set_status(err)
            self.result.after(0, self._update_text_widget, self.translation_text, f"ERROR\n\n{err}")
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            self.set_status(err)
            self.result.after(0, self._update_text_widget, self.translation_text, f"ERROR\n\n{err}")
        finally:
            self.is_busy = False
            self.launcher.after(0, self.show_launcher_again)
            self.result.after(0, self.show_result_window)

    def preprocess_for_ocr(self, image: Image.Image) -> Image.Image:
        scale = self.config.preprocess_scale
        if scale > 1:
            image = image.resize((image.width * scale, image.height * scale), Image.Resampling.LANCZOS)

        gray = ImageOps.grayscale(image)
        gray = ImageOps.autocontrast(gray, cutoff=1)
        gray = gray.filter(ImageFilter.MedianFilter(size=3))
        gray = ImageEnhance.Sharpness(gray).enhance(2.0)
        gray = ImageEnhance.Contrast(gray).enhance(1.35)

        # Less aggressive threshold than before; preserves diacritics better
        bw = gray.point(lambda p: 255 if p > 170 else 0)
        return bw

    def run_ocr(self, image: Image.Image) -> str:
        ocr_lang = (self.config.ocr_lang or "fin").strip()

        custom_config = f"--oem 3 --psm {self.config.ocr_psm} -l {ocr_lang}"
        try:
            text = pytesseract.image_to_string(image, config=custom_config)
        except pytesseract.TesseractError as e:
            msg = str(e)
            if "Failed loading language" in msg or "Error opening data file" in msg:
                raise RuntimeError(
                    f"Tesseract language data for '{ocr_lang}' is missing. "
                    f"Install the corresponding traineddata file (for Finnish: fin.traineddata) "
                    f"into your Tesseract tessdata folder."
                ) from e
            raise

        return self.clean_ocr_text(text)

    @staticmethod
    def clean_ocr_text(text: str) -> str:
        replacements = {
            "a¨": "ä",
            "A¨": "Ä",
            "o¨": "ö",
            "O¨": "Ö",
        }
        for bad, good in replacements.items():
            text = text.replace(bad, good)

        lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
        cleaned = []
        prev_blank = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if not prev_blank:
                    cleaned.append("")
                prev_blank = True
                continue
            prev_blank = False
            cleaned.append(stripped)
        return "\n".join(cleaned).strip()

    @staticmethod
    def _update_text_widget(widget: tk.Text, text: str) -> None:
        widget.delete("1.0", "end")
        widget.insert("1.0", text)

    def copy_translation(self) -> None:
        if not self.last_translation.strip():
            self.set_status("Nothing to copy.")
            return
        pyperclip.copy(self.last_translation)
        self.set_status("Translation copied to clipboard.")

    def quit_app(self) -> None:
        self.launcher.destroy()

    def run(self) -> None:
        self.launcher.mainloop()


def main() -> int:
    try:
        app = ScreenTranslatorSelectorApp(parse_args())
        app.run()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
