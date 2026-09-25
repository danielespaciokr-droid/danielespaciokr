"""Tkinter GUI: pick a photo, draw what to remove, and let Photoshop remove it."""

from __future__ import annotations

import math
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont
from typing import Callable, List, Optional, Tuple

from . import __version__, api, settings
from . import shapes as sh
from .batch import DEFAULT_OUTPUT_FOLDER, BatchJob, BatchRunner
from .photoshop import PhotoshopError, ScriptFailed

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageTk
except ImportError:  # pragma: no cover - reported by main()
    Image = None

APP_TITLE = "PS Remover - 사진 속 영역 지우기"
PHOTO_TYPES = [
    ("사진", "*.jpg *.jpeg *.png *.tif *.tiff *.psd *.bmp *.gif *.webp *.heic *.heif"),
    ("모든 파일", "*.*"),
]
OUTPUT_TYPES = [("JPG", "*.jpg *.jpeg"), ("PNG", "*.png"), ("TIFF", "*.tif *.tiff"), ("Photoshop", "*.psd")]
TOOLS = (("rect", "사각형", "r"), ("ellipse", "타원", "e"), ("lasso", "올가미", "l"), ("brush", "브러시", "b"))
OVERLAY = (255, 48, 48)
EDGE = (255, 230, 0)
PREVIEW_MAX_SIDE = 2400  # longest side of the in-memory preview
MAX_ZOOM = 8.0

METHOD_HINTS = {
    "content-aware": "내용 인식 채우기: 주변 내용으로 자연스럽게 채웁니다.",
    "action": "Photoshop [제거] 버튼: 영역을 선택한 뒤 녹화해 둔 동작으로 [제거] 버튼과 같은 작업을 합니다. "
              "처음이면 [설정...]을 보세요.",
    "transparent": "투명하게 잘라내기: 선택한 부분을 투명하게 지우고 PNG로 저장합니다.",
}

ANCHOR_HELP = ("모서리의 워터마크나 날짜처럼 자리가 정해진 영역에 알맞습니다. 기준 위치에서 떨어진 거리를 지키므로, "
               "세로 사진이나 크기가 다른 사진에서도 같은 모서리에 놓입니다.")

BATCH_NO_PRESET = ("저장된 공통 영역이 없습니다. 메인 창에서 기준 사진을 열고 지울 영역을 그린 뒤 "
                   "[지금 영역 저장...]으로 공통 영역을 만드세요.")

ACTION_INTRO = (
    "Photoshop에는 작업 표시줄의 버튼을 스크립트로 누르는 기능이 없습니다. 대신 [제거] 버튼을 누르는 과정을 "
    "Photoshop '동작'으로 한 번 녹화해 두면, 이 도구가 영역을 선택한 뒤 그 동작을 실행합니다. "
    "[제거] 버튼 대신 생성형 채우기 등 다른 작업을 녹화해도 됩니다."
)

HELP_TEXT = """\
1. [사진 열기]로 사진을 고릅니다.
2. 도구를 골라 사진 위에 지울 부분을 표시합니다.
   - 사각형(R), 타원(E): 드래그 (Shift를 누르면 정사각형/원)
   - 올가미(L): 지울 부분을 자유롭게 둘러싸기
   - 브러시(B): 지울 부분을 칠하기 ([ 와 ] 로 크기 조절)
   - '빼기'를 고른 뒤 그리면 그 부분은 선택에서 빠집니다.
   - 실수했으면 되돌리기(Ctrl+Z)를 누르세요.
3. [Photoshop에서 지우기]를 누르면 Photoshop이 실행되어 사진을 열고,
   같은 영역을 선택해 지운 다음 결과를 새 파일로 저장합니다.
   원본 사진은 바뀌지 않습니다.

여러 사진에서 같은 자리를 지우려면 (공통 영역)
  기준 사진에 지울 영역을 그리고 [지금 영역 저장...]으로 이름을 붙여 저장합니다.
  [여러 사진 한꺼번에 지우기...]에서 사진 폴더와 공통 영역을 고르고 [시작]을 누르면
  폴더의 사진마다 그 영역을 선택해 지웁니다. 이미 지운 사진은 건너뛰므로 새 사진이
  생길 때마다 [시작]만 다시 누르면 되고, 폴더 감시를 켜 두면 알아서 지웁니다.
  [이 사진에 적용]을 누르면 공통 영역이 지금 사진의 어디에 놓이는지 볼 수 있습니다.

Photoshop 작업 표시줄의 [제거] 버튼으로 지우려면
  지우는 방식에서 'Photoshop [제거] 버튼'을 고르세요.
  처음 한 번은 [설정...]의 안내대로 [제거] 버튼 누르기를 동작으로 녹화해야 합니다.

Photoshop의 선택 도구로 직접 고르고 싶다면
  ① [Photoshop에서 열기]로 사진을 엽니다. 영역을 그려 두었다면 그 영역이 선택된 채로 열립니다.
     Photoshop에서 선택을 다듬거나 새로 선택하고
  ② [Photoshop 선택 영역 지우기]를 누르세요. (Photoshop에서 Ctrl+Z로 되돌릴 수 있습니다)
"""


class RemoverApp:
    def __init__(self, root: tk.Tk, photo: Optional[str] = None):
        self.root = root
        self.photo_path: Optional[Path] = None
        self.image_size: Optional[Tuple[int, int]] = None  # the size Photoshop will open
        self.preview = None  # downscaled Pillow image, already rotated like Photoshop does
        self.preview_error: Optional[str] = None
        self.shapes: List[sh.Shape] = []
        self.scale = 1.0
        self.offset = (0, 0)
        self._base = None
        self._base_key = None
        self._photo_image = None
        self._drag: Optional[dict] = None
        self._jobs: "queue.Queue" = queue.Queue()
        self._busy = False
        self._render_after = None
        self._output_auto = True
        self._setting_output = False
        self._confirmed_output: Optional[Path] = None  # chosen in a save dialog that already asked about overwriting

        self.tool = tk.StringVar(value="rect")
        self.mode = tk.StringVar(value="add")
        self.brush_size = tk.DoubleVar(value=40)
        self.brush_label = tk.StringVar(value="40 px")
        self.method = tk.StringVar(value="content-aware")
        self.action_set = tk.StringVar(value=api.DEFAULT_ACTION_SET)
        self.action_name = tk.StringVar(value=api.DEFAULT_ACTION_NAME)
        self.action_status = tk.StringVar()
        self._action_dialog: Optional[tk.Toplevel] = None
        self.expand = tk.StringVar(value="4")
        self.feather = tk.StringVar(value="0")
        self.subject = tk.BooleanVar(value=False)
        self.keep_open = tk.BooleanVar(value=True)
        self.output = tk.StringVar()
        self.preset = tk.StringVar()
        self._batch_window: Optional[BatchWindow] = None
        self.status = tk.StringVar(value="[사진 열기]로 지울 부분이 있는 사진을 고르세요.")
        # The tool is used again and again, so the removal settings carry over to the next run.
        self._restore_preferences()
        root.protocol("WM_DELETE_WINDOW", self.close)

        self._build_menu()
        self._build_toolbar()
        self._build_statusbar()
        self._build_options()
        self._build_canvas()
        self._bind_keys()
        self._refresh_presets()
        self.method.trace_add("write", lambda *_: self._on_method_change())
        self.output.trace_add("write", lambda *_: self._on_output_edited())
        self.brush_size.trace_add("write", lambda *_: self.brush_label.set(f"{self._brush_diameter()} px"))
        if photo:
            root.after(50, lambda: self.load_photo(photo))

    # ------------------------------------------------------------ layout

    def _build_menu(self) -> None:
        accel = "Cmd" if sys.platform == "darwin" else "Ctrl"
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="사진 열기...", accelerator=f"{accel}+O", command=self.choose_photo)
        file_menu.add_separator()
        file_menu.add_command(label="선택 영역 저장...", command=self.save_selection)
        file_menu.add_command(label="선택 영역 불러오기...", command=self.load_selection)
        file_menu.add_command(label="Photoshop 스크립트(.jsx)로 내보내기...", command=self.export_jsx)
        file_menu.add_separator()
        file_menu.add_command(label="끝내기", command=self.close)
        menubar.add_cascade(label="파일", menu=file_menu)
        edit_menu = tk.Menu(menubar, tearoff=False)
        edit_menu.add_command(label="되돌리기", accelerator=f"{accel}+Z", command=self.undo)
        edit_menu.add_command(label="선택 해제", command=self.clear_shapes)
        menubar.add_cascade(label="편집", menu=edit_menu)
        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="사용 방법", command=lambda: messagebox.showinfo("사용 방법", HELP_TEXT))
        help_menu.add_command(label="정보", command=lambda: messagebox.showinfo(
            APP_TITLE, f"PS Remover {__version__}\nPhotoshop을 실행해 사진 속 선택 영역을 지웁니다."))
        menubar.add_cascade(label="도움말", menu=help_menu)
        self.root.config(menu=menubar)

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(10, 8, 10, 6))
        bar.pack(side=tk.TOP, fill=tk.X)
        # Packed first so a narrow window clips the brush slider, not these.
        ttk.Button(bar, text="선택 해제", command=self.clear_shapes).pack(side=tk.RIGHT)
        ttk.Button(bar, text="되돌리기", command=self.undo).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(bar, text="사진 열기", command=self.choose_photo).pack(side=tk.LEFT)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        for value, label, key in TOOLS:
            ttk.Radiobutton(bar, text=label, value=value, variable=self.tool,
                            style="Toolbutton", command=self._clear_cursor).pack(side=tk.LEFT, padx=1)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        ttk.Radiobutton(bar, text="추가 +", value="add", variable=self.mode, style="Toolbutton").pack(side=tk.LEFT, padx=1)
        ttk.Radiobutton(bar, text="빼기 -", value="subtract", variable=self.mode, style="Toolbutton").pack(side=tk.LEFT, padx=1)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        ttk.Label(bar, text="브러시").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Scale(bar, from_=4, to=200, variable=self.brush_size, length=100).pack(side=tk.LEFT)
        ttk.Label(bar, textvariable=self.brush_label, width=7).pack(side=tk.LEFT, padx=(4, 0))

    def _build_canvas(self) -> None:
        self.canvas = tk.Canvas(self.root, background="#2b2b2b", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda e: self._schedule_render())
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._draw_cursor)
        self.canvas.bind("<Leave>", lambda e: self._clear_cursor())

    def _build_options(self) -> None:
        panel = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        panel.pack(side=tk.BOTTOM, fill=tk.X)
        panel.columnconfigure(1, weight=1)

        ttk.Label(panel, text="지우는 방식").grid(row=0, column=0, sticky="w", padx=(0, 10))
        methods = ttk.Frame(panel)
        methods.grid(row=0, column=1, columnspan=3, sticky="w")
        ttk.Radiobutton(methods, text="내용 인식 채우기", value="content-aware",
                        variable=self.method).pack(side=tk.LEFT)
        ttk.Radiobutton(methods, text="Photoshop [제거] 버튼", value="action",
                        variable=self.method).pack(side=tk.LEFT, padx=(16, 0))
        ttk.Button(methods, text="설정...", command=self.open_action_settings).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Radiobutton(methods, text="투명하게 잘라내기 (PNG)", value="transparent",
                        variable=self.method).pack(side=tk.LEFT, padx=(16, 0))

        ttk.Label(panel, text="선택 다듬기").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(6, 0))
        refine = ttk.Frame(panel)
        refine.grid(row=1, column=1, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(refine, text="넓히기").pack(side=tk.LEFT)
        ttk.Spinbox(refine, from_=0, to=100, width=5, textvariable=self.expand).pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(refine, text="px").pack(side=tk.LEFT)
        ttk.Label(refine, text="부드럽게").pack(side=tk.LEFT, padx=(16, 0))
        ttk.Spinbox(refine, from_=0, to=250, increment=0.5, width=5,
                    textvariable=self.feather).pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(refine, text="px").pack(side=tk.LEFT)
        ttk.Checkbutton(refine, text="그린 영역 안에서 Photoshop '피사체 선택'으로 대상만 고르기",
                        variable=self.subject).pack(side=tk.LEFT, padx=(16, 0))

        ttk.Label(panel, text="저장 위치").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=(6, 0))
        ttk.Entry(panel, textvariable=self.output).grid(row=2, column=1, sticky="ew", pady=(6, 0))
        ttk.Button(panel, text="바꾸기...", command=self.choose_output).grid(row=2, column=2, padx=(6, 0), pady=(6, 0))
        ttk.Checkbutton(panel, text="끝나면 결과를 Photoshop에 열어 두기",
                        variable=self.keep_open).grid(row=2, column=3, sticky="w", padx=(12, 0), pady=(6, 0))

        ttk.Label(panel, text="공통 영역").grid(row=3, column=0, sticky="w", padx=(0, 10), pady=(6, 0))
        common = ttk.Frame(panel)
        common.grid(row=3, column=1, columnspan=3, sticky="w", pady=(6, 0))
        self.preset_box = ttk.Combobox(common, textvariable=self.preset, state="readonly", width=18)
        self.preset_box.pack(side=tk.LEFT)
        ttk.Button(common, text="이 사진에 적용", command=self.apply_preset).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(common, text="지금 영역 저장...", command=self.save_preset).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(common, text="삭제", command=self.delete_preset).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(common, text="여러 사진 한꺼번에 지우기...", style="Accent.TButton",
                   command=self.open_batch_window).pack(side=tk.LEFT, padx=(16, 0))

        actions = ttk.Frame(panel)
        actions.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        self.run_button = ttk.Button(actions, text="▶  Photoshop에서 지우기", style="Accent.TButton",
                                     command=self.run_remove)
        self.run_button.pack(side=tk.LEFT)
        ttk.Label(actions, text="Photoshop에서 직접 선택하기:").pack(side=tk.LEFT, padx=(20, 6))
        self.open_button = ttk.Button(actions, text="① Photoshop에서 열기", command=self.run_open)
        self.open_button.pack(side=tk.LEFT)
        self.current_button = ttk.Button(actions, text="② Photoshop 선택 영역 지우기", command=self.run_remove_current)
        self.current_button.pack(side=tk.LEFT, padx=(6, 0))

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(10, 2, 10, 8))
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=140)
        ttk.Label(bar, textvariable=self.status, anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)

    def _bind_keys(self) -> None:
        mod = "Command" if sys.platform == "darwin" else "Control"
        self.root.bind_all(f"<{mod}-o>", lambda e: self.choose_photo())
        self.root.bind_all(f"<{mod}-z>", self._undo_key)
        for value, _, key in TOOLS:
            self.canvas.bind(f"<KeyPress-{key}>", lambda e, v=value: self._set_tool(v))
        self.canvas.bind("<bracketleft>", lambda e: self._resize_brush(-5))
        self.canvas.bind("<bracketright>", lambda e: self._resize_brush(5))
        self.canvas.bind("<Escape>", lambda e: self._cancel_drag())

    # ------------------------------------------------------------ photo

    def choose_photo(self) -> None:
        if self._busy:
            return
        initial = str(self.photo_path.parent) if self.photo_path else None
        path = filedialog.askopenfilename(title="사진 열기", filetypes=PHOTO_TYPES, initialdir=initial)
        if path:
            self.load_photo(path)

    def load_photo(self, path) -> None:
        path = Path(path)
        if not path.is_file():
            messagebox.showerror(APP_TITLE, f"사진 파일을 찾을 수 없습니다:\n{path}")
            return
        try:
            self.preview, self.image_size = load_preview(path)
            self.preview_error = None
        except Exception as exc:  # noqa: BLE001 - any decoder error means "no preview"
            self.preview, self.image_size = None, None
            self.preview_error = str(exc) or exc.__class__.__name__
        self.photo_path = path
        self.shapes = []
        self._base_key = None
        self._set_output(api.default_output_path(path, self.method.get()), auto=True)
        self.root.title(f"{path.name} - {APP_TITLE}")
        if self.preview is None:
            self.status.set("이 파일은 미리보기를 표시할 수 없습니다. 아래 'Photoshop에서 직접 선택하기'를 이용하세요.")
        else:
            width, height = self.image_size
            self.status.set(f"{path.name} ({width}x{height}) - 지울 부분을 사진 위에 그리세요. "
                            "(단축키: R 사각형, E 타원, L 올가미, B 브러시, [ ] 브러시 크기)")
        self._schedule_render()

    # ------------------------------------------------------------ drawing

    def _schedule_render(self) -> None:
        if self._render_after is not None:
            self.root.after_cancel(self._render_after)
        self._render_after = self.root.after(15, self._render)

    def _render(self) -> None:
        self._render_after = None
        canvas = self.canvas
        canvas.delete("photo")
        width, height = max(canvas.winfo_width(), 1), max(canvas.winfo_height(), 1)
        if self.preview is None:
            if not self.photo_path:
                text = "[사진 열기]를 눌러 사진을 고르세요."
            else:
                text = f"미리보기를 표시할 수 없습니다.\n({self.preview_error})\n\n'① Photoshop에서 열기'로 사진을 연 뒤\nPhotoshop에서 직접 선택할 수 있습니다."
            canvas.create_text(width / 2, height / 2, text=text, fill="#d0d0d0", justify="center",
                               width=max(width - 40, 100), tags=("photo",))
            return
        img_w, img_h = self.image_size
        margin = 12
        scale = min((width - 2 * margin) / img_w, (height - 2 * margin) / img_h, MAX_ZOOM)
        scale = max(scale, 0.01)
        shown = (max(1, round(img_w * scale)), max(1, round(img_h * scale)))
        self.scale = scale
        self.offset = ((width - shown[0]) // 2, (height - shown[1]) // 2)
        if self._base_key != shown:
            self._base = fit_preview(self.preview, shown)
            self._base_key = shown
        composed = compose_overlay(self._base, sh.render_mask(self.shapes, shown, scale))
        self._photo_image = ImageTk.PhotoImage(composed)
        canvas.create_image(*self.offset, anchor="nw", image=self._photo_image, tags=("photo",))
        canvas.tag_lower("photo")

    def _to_image(self, x: float, y: float) -> Tuple[float, float]:
        return ((x - self.offset[0]) / self.scale, (y - self.offset[1]) / self.scale)

    def _brush_diameter(self) -> int:
        try:
            return int(round(self.brush_size.get()))
        except (tk.TclError, ValueError):
            return 40

    def _on_press(self, event) -> None:
        self.canvas.focus_set()
        if self.preview is None or self._busy:
            return
        tool, x, y = self.tool.get(), event.x, event.y
        if tool in ("rect", "ellipse"):
            create = self.canvas.create_rectangle if tool == "rect" else self.canvas.create_oval
            item = create(x, y, x, y, outline="#ffffff", dash=(4, 3), tags=("temp",))
            self._drag = {"tool": tool, "start": (x, y), "end": (x, y), "item": item}
        elif tool == "lasso":
            item = self.canvas.create_line(x, y, x, y, fill="#ffffff", tags=("temp",))
            self._drag = {"tool": tool, "points": [(x, y)], "item": item}
        else:
            r = self._brush_diameter() / 2
            self.canvas.create_oval(x - r, y - r, x + r, y + r, fill=_hex(OVERLAY), outline="", tags=("temp",))
            self._drag = {"tool": tool, "points": [(x, y)]}

    def _on_drag(self, event) -> None:
        drag = self._drag
        self._draw_cursor(event)
        if not drag:
            return
        x, y = event.x, event.y
        if drag["tool"] in ("rect", "ellipse"):
            x0, y0 = drag["start"]
            if event.state & 0x0001:  # Shift: square / circle
                side = max(abs(x - x0), abs(y - y0))
                x = x0 + (side if x >= x0 else -side)
                y = y0 + (side if y >= y0 else -side)
            drag["end"] = (x, y)
            self.canvas.coords(drag["item"], x0, y0, x, y)
            return
        last = drag["points"][-1]
        if math.hypot(x - last[0], y - last[1]) < 2:
            return
        drag["points"].append((x, y))
        if drag["tool"] == "lasso":
            self.canvas.coords(drag["item"], *[c for point in drag["points"] for c in point])
        else:
            self.canvas.create_line(last[0], last[1], x, y, fill=_hex(OVERLAY), width=self._brush_diameter(),
                                    capstyle=tk.ROUND, tags=("temp",))
        self.canvas.tag_raise("cursor")

    def _on_release(self, event) -> None:
        drag, self._drag = self._drag, None
        if not drag:
            return
        self.canvas.delete("temp")
        shape = self._shape_from_drag(drag)
        if shape is not None:
            self.shapes.append(shape)
            self._selection_changed()

    def _shape_from_drag(self, drag: dict) -> Optional[sh.Shape]:
        mode = self.mode.get()
        try:
            if drag["tool"] in ("rect", "ellipse"):
                (x0, y0), (x1, y1) = drag["start"], drag["end"]
                if abs(x1 - x0) < 3 or abs(y1 - y0) < 3:
                    return None
                return sh.Shape(drag["tool"], [self._to_image(x0, y0), self._to_image(x1, y1)], mode)
            points = [self._to_image(x, y) for x, y in drag["points"]]
            if drag["tool"] == "lasso":
                points = sh.simplify_path(points, 0.75 / self.scale)
                if len(points) < 3 or polygon_area(points) * self.scale ** 2 < 9:
                    return None
                return sh.polygon(points, mode)
            radius = self._brush_diameter() / 2 / self.scale
            return sh.brush(sh.simplify_path(points, max(0.5 / self.scale, radius * 0.2)), radius, mode)
        except sh.SelectionError:
            return None

    def _cancel_drag(self) -> None:
        self._drag = None
        self.canvas.delete("temp")

    def _draw_cursor(self, event) -> None:
        self.canvas.delete("cursor")
        if self.tool.get() != "brush" or self.preview is None:
            return
        r = self._brush_diameter() / 2
        self.canvas.create_oval(event.x - r, event.y - r, event.x + r, event.y + r,
                                outline="#ffffff", tags=("cursor",))

    def _clear_cursor(self) -> None:
        self.canvas.delete("cursor")

    def _set_tool(self, tool: str) -> None:
        self.tool.set(tool)
        self._clear_cursor()

    def _resize_brush(self, delta: int) -> None:
        self.brush_size.set(min(200, max(4, self._brush_diameter() + delta)))

    def _undo_key(self, event) -> None:
        if isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Spinbox)):
            return  # let text fields handle their own keys
        self.undo()

    def undo(self) -> None:
        if self.shapes and not self._busy:
            self.shapes.pop()
            self._selection_changed()

    def clear_shapes(self) -> None:
        if self.shapes and not self._busy:
            self.shapes = []
            self._selection_changed()

    def _selection_changed(self) -> None:
        count = len(self.shapes)
        self.status.set(f"선택한 도형 {count}개 - [Photoshop에서 지우기]를 누르세요." if count
                        else "지울 부분을 사진 위에 그리세요.")
        self._schedule_render()

    # ------------------------------------------------------------ output

    def _set_output(self, path, auto: bool) -> None:
        self._setting_output = True
        try:
            self.output.set(str(path))
        finally:
            self._setting_output = False
        self._output_auto = auto

    def _on_output_edited(self) -> None:
        if not self._setting_output:
            self._output_auto = False

    def _on_method_change(self) -> None:
        method = self.method.get()
        self.status.set(METHOD_HINTS.get(method, ""))
        if self.photo_path and self._output_auto:
            self._set_output(api.default_output_path(self.photo_path, method), auto=True)
        elif method == "transparent" and self.output.get().lower().endswith((".jpg", ".jpeg")):
            self.status.set("JPG는 투명도를 저장할 수 없습니다. 투명하게 남기려면 저장 위치를 .png로 바꾸세요.")

    def choose_output(self) -> None:
        current = Path(self.output.get()) if self.output.get().strip() else None
        folder = current.parent if current else (self.photo_path.parent if self.photo_path else None)
        path = filedialog.asksaveasfilename(
            title="결과를 저장할 위치", filetypes=OUTPUT_TYPES,
            initialdir=str(folder) if folder else None, initialfile=current.name if current else "",
            defaultextension=current.suffix if current and current.suffix else ".jpg",
        )
        if path:
            self._set_output(path, auto=False)
            self._confirmed_output = Path(path)

    def _resolve_output(self, required: bool = True) -> Optional[Tuple[Optional[Path], bool]]:
        """(output path, overwrite) for a job, or None when the user backed out."""
        text = self.output.get().strip()
        if self.photo_path and (self._output_auto or not text):
            output = api.default_output_path(self.photo_path, self.method.get())
            self._set_output(output, auto=True)
            return output, False
        if not text:
            if required:
                messagebox.showerror(APP_TITLE, "저장 위치를 입력하세요.")
                return None
            return None, False
        output = Path(text).expanduser()
        if not output.is_absolute() and self.photo_path:
            output = self.photo_path.parent / output
        if output.suffix.lower() not in api.SAVE_EXTENSIONS:
            messagebox.showerror(APP_TITLE, "저장 형식은 JPG, PNG, TIFF, PSD 중 하나여야 합니다.")
            return None
        if not output.exists():
            return output, False
        if self.photo_path and _same_file(output, self.photo_path):
            question = "결과로 원본 사진을 덮어씁니다. 원본은 되돌릴 수 없습니다. 계속할까요?"
        elif output == self._confirmed_output:
            return output, True
        else:
            question = f"{output.name} 파일이 이미 있습니다. 덮어쓸까요?"
        if not messagebox.askyesno(APP_TITLE, question, icon="warning"):
            return None
        return output, True

    def _options(self) -> Optional[api.RemoveOptions]:
        try:
            options = api.RemoveOptions(
                method=self.method.get(),
                action_set=self.action_set.get().strip(),
                action_name=self.action_name.get().strip(),
                expand=int(float(self.expand.get() or 0)),
                feather=float(self.feather.get() or 0),
                subject=self.subject.get(),
                keep_open=self.keep_open.get(),
            )
            options.validate()
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, f"옵션 값을 확인하세요.\n{exc}")
            return None
        return options

    # ------------------------------------------------------------ jobs

    def run_remove(self) -> None:
        if self._busy:
            return
        if not self.photo_path:
            messagebox.showinfo(APP_TITLE, "먼저 [사진 열기]로 사진을 고르세요.")
            return
        if not sh.has_area(self.shapes) and not self.subject.get():
            messagebox.showinfo(APP_TITLE, "지울 부분을 사진 위에 그리거나 '피사체 선택'을 켜세요.")
            return
        options = self._options()
        resolved = self._resolve_output() if options else None
        if not resolved:
            return
        output, overwrite = resolved
        photo, area = self.photo_path, sh.Area(list(self.shapes), self.image_size)
        self._start(
            "Photoshop에서 지우는 중입니다... (Photoshop을 처음 실행할 때는 시간이 걸립니다)",
            lambda: api.remove_area(photo, area, output, options, overwrite=overwrite),
            self._removed,
        )

    def run_open(self) -> None:
        if self._busy:
            return
        if not self.photo_path:
            messagebox.showinfo(APP_TITLE, "먼저 [사진 열기]로 사진을 고르세요.")
            return
        photo, area, options = self.photo_path, None, None
        if sh.has_area(self.shapes):
            # Select the drawn area too, e.g. to press Photoshop's own Remove button next.
            options = self._options()
            if options is None:
                return
            area = sh.Area(list(self.shapes), self.image_size)
        self._start("Photoshop에서 사진을 여는 중입니다...", lambda: api.open_photo(photo, area, options), self._opened)

    def _opened(self, result: dict) -> None:
        if result.get("selectionBounds"):
            self.status.set("Photoshop에서 사진을 열고 그린 영역을 선택했습니다. Photoshop 작업 표시줄의 [제거]를 누르거나, "
                            "선택을 다듬은 뒤 [② Photoshop 선택 영역 지우기]를 누르세요.")
        else:
            self.status.set("Photoshop에서 사진을 열었습니다. Photoshop에서 지울 부분을 선택한 뒤 "
                            "[② Photoshop 선택 영역 지우기]를 누르세요.")

    def run_remove_current(self) -> None:
        if self._busy:
            return
        options = self._options()
        resolved = self._resolve_output(required=False) if options else None
        if not resolved:
            return
        output, _ = resolved
        self._start("Photoshop의 선택 영역을 지우는 중입니다...",
                    lambda: api.remove_current_selection(options, output), self._removed_current)

    def _start(self, message: str, job: Callable[[], dict], on_success: Callable[[dict], None]) -> None:
        self._set_busy(True)
        self.status.set(message)
        self.save_preferences()

        def work() -> None:
            try:
                self._jobs.put((on_success, job(), None))
            except Exception as exc:  # noqa: BLE001 - reported to the user
                self._jobs.put((on_success, None, exc))

        threading.Thread(target=work, daemon=True).start()
        self.root.after(100, self._poll_jobs)

    def _poll_jobs(self) -> None:
        try:
            on_success, result, error = self._jobs.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll_jobs)
            return
        self._set_busy(False)
        if error is not None:
            self._show_error(error)
        else:
            on_success(result)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for button in (self.run_button, self.open_button, self.current_button):
            button.state(["disabled"] if busy else ["!disabled"])
        if busy:
            self.progress.pack(side=tk.RIGHT)
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()

    def _removed(self, result: dict) -> None:
        self._after_success(f"완료했습니다. 저장한 파일: {result.get('output')}", result)

    def _removed_current(self, result: dict) -> None:
        text = "Photoshop에서 선택 영역을 지웠습니다. (Photoshop에서 Ctrl+Z로 되돌릴 수 있습니다)"
        if result.get("output"):
            text += f" 저장한 파일: {result['output']}"
        self._after_success(text, result)

    def _after_success(self, text: str, result: dict) -> None:
        warnings = result.get("warnings") or []
        self.status.set(text)
        if self.photo_path and self._output_auto:
            # The next run gets a fresh file name instead of failing on this one.
            self._set_output(api.default_output_path(self.photo_path, self.method.get()), auto=True)
        if warnings:
            messagebox.showinfo(APP_TITLE, text + "\n\n참고:\n" + "\n".join(f"- {w}" for w in warnings))

    def _show_error(self, error: BaseException) -> None:
        if isinstance(error, ScriptFailed):
            text = str(error)
            if error.result.get("leftOpen"):
                text += "\n\n작업하던 사진은 Photoshop에 열어 두었습니다. Photoshop에서 직접 마무리할 수 있습니다."
            warnings = error.result.get("warnings") or []
            if warnings:
                text += "\n\n참고:\n" + "\n".join(f"- {w}" for w in warnings)
        elif isinstance(error, (PhotoshopError, api.JobError, sh.SelectionError, OSError)):
            text = str(error)
        else:
            text = f"예상하지 못한 오류가 났습니다: {error!r}"
        self.status.set("실패: " + text.splitlines()[0])
        messagebox.showerror(APP_TITLE, text)

    # ------------------------------------------------------ preferences

    _TEXT_PREFERENCES = ("method", "action_set", "action_name", "expand", "feather", "preset")
    _FLAG_PREFERENCES = ("subject", "keep_open")

    def _restore_preferences(self) -> None:
        saved = settings.load_settings("main")
        for name in self._TEXT_PREFERENCES:
            value = saved.get(name)
            if isinstance(value, str) and value.strip() and (name != "method" or value in api.METHODS):
                getattr(self, name).set(value)
        for name in self._FLAG_PREFERENCES:
            if isinstance(saved.get(name), bool):
                getattr(self, name).set(saved[name])

    def save_preferences(self) -> None:
        values = {name: getattr(self, name).get() for name in self._TEXT_PREFERENCES + self._FLAG_PREFERENCES}
        try:
            settings.save_settings("main", values)
        except OSError:
            pass  # remembering the choices is a convenience

    def close(self) -> None:
        self.save_preferences()
        self.root.destroy()

    # ------------------------------------------------------ common areas

    def _refresh_presets(self, select: Optional[str] = None) -> None:
        names = settings.list_presets()
        self.preset_box.configure(values=names)
        if select is not None:
            self.preset.set(select)
        elif self.preset.get() not in names:
            self.preset.set(names[0] if names else "")

    def save_preset(self) -> Optional["PresetDialog"]:
        if self.preview is None or not sh.has_area(self.shapes):
            messagebox.showinfo(APP_TITLE, "먼저 사진을 열고, 여러 사진에서 지울 공통 영역을 그리세요.")
            return None
        return PresetDialog(self)

    def apply_preset(self) -> None:
        name = self.preset.get()
        if not name:
            messagebox.showinfo(APP_TITLE, "저장된 공통 영역이 없습니다. 영역을 그린 뒤 [지금 영역 저장...]을 누르세요.")
            return
        if self.preview is None:
            messagebox.showinfo(APP_TITLE, "먼저 미리보기가 되는 사진을 여세요.")
            return
        try:
            area = settings.load_preset(name)
            self.shapes = area.on_photo(self.image_size)
        except sh.SelectionError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self._schedule_render()
        self.status.set(f"공통 영역 '{name}'을(를) 이 사진에 놓았습니다 ({area.describe()}).")

    def delete_preset(self) -> None:
        name = self.preset.get()
        if not name or not messagebox.askyesno(APP_TITLE, f"공통 영역 '{name}'을(를) 삭제할까요?"):
            return
        settings.delete_preset(name)
        self._refresh_presets()
        self.status.set(f"공통 영역 '{name}'을(를) 삭제했습니다.")

    def open_batch_window(self) -> None:
        if self._batch_window is not None and self._batch_window.window.winfo_exists():
            self._batch_window.window.lift()
            return
        self._batch_window = BatchWindow(self)

    def method_summary(self) -> str:
        names = {"content-aware": "내용 인식 채우기", "action": "Photoshop [제거] 버튼", "transparent": "투명하게 잘라내기"}
        text = f"{names.get(self.method.get(), self.method.get())}, 넓히기 {self.expand.get()}px"
        if self.feather.get().strip() not in ("", "0", "0.0"):
            text += f", 부드럽게 {self.feather.get()}px"
        if self.subject.get():
            text += ", 피사체 선택"
        return text

    # ------------------------------------------------------ recorded action

    def open_action_settings(self) -> None:
        """Explain how to record the Remove button as an action, and check for it."""
        if self._action_dialog is not None and self._action_dialog.winfo_exists():
            self._action_dialog.lift()
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Photoshop [제거] 버튼 쓰기")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        self._action_dialog = dialog
        body = ttk.Frame(dialog, padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=ACTION_INTRO, justify="left", wraplength=560).grid(
            row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(body, text=api.ACTION_SETUP_HELP, justify="left", wraplength=560).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(10, 12))
        ttk.Label(body, text="세트 이름").grid(row=2, column=0, sticky="w")
        ttk.Entry(body, textvariable=self.action_set, width=18).grid(row=2, column=1, sticky="w", padx=(6, 16))
        ttk.Label(body, text="동작 이름").grid(row=2, column=2, sticky="w")
        ttk.Entry(body, textvariable=self.action_name, width=18).grid(row=2, column=3, sticky="w", padx=(6, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=3, column=0, columnspan=4, sticky="w", pady=(12, 0))
        ttk.Button(buttons, text="Photoshop에서 확인", command=self.check_action).pack(side=tk.LEFT)
        ttk.Button(buttons, text="닫기", command=dialog.destroy).pack(side=tk.LEFT, padx=(6, 0))
        self.action_status.set("")
        ttk.Label(body, textvariable=self.action_status, justify="left", wraplength=560).grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(10, 0))

    def check_action(self) -> None:
        if self._busy:
            return
        action_set, action_name = self.action_set.get().strip(), self.action_name.get().strip()
        if not action_set or not action_name:
            self.action_status.set("세트 이름과 동작 이름을 입력하세요.")
            return
        self.action_status.set("Photoshop에서 동작을 찾는 중입니다...")
        self._start("Photoshop에서 동작을 찾는 중입니다...",
                    lambda: api.find_recorded_action(action_set, action_name),
                    lambda info: self._action_checked(action_set, action_name, info))

    def _action_checked(self, action_set: str, action_name: str, info: dict) -> None:
        label = f"'{action_set} > {action_name}'"
        if info.get("found") and info.get("stepCount") != 0:
            steps = ", ".join(info.get("steps") or []) or "(알 수 없음)"
            text = f"동작 {label}을(를) 찾았습니다. 녹화된 단계: {steps}"
            self.method.set("action")
        elif info.get("found"):
            text = f"동작 {label}에 녹화된 단계가 없습니다. 다시 녹화하세요."
        elif info.get("setFound"):
            text = f"'{action_set}' 세트는 있지만 '{action_name}' 동작이 없습니다."
        else:
            text = f"Photoshop에 '{action_set}' 세트가 없습니다. 위 방법대로 녹화하세요."
        self.action_status.set(text)
        self.status.set(text)

    # ------------------------------------------------------ selection files

    def save_selection(self) -> None:
        if not self.shapes:
            messagebox.showinfo(APP_TITLE, "저장할 선택 영역이 없습니다.")
            return
        initial = f"{self.photo_path.stem}_선택영역.json" if self.photo_path else "선택영역.json"
        path = filedialog.asksaveasfilename(title="선택 영역 저장", defaultextension=".json", initialfile=initial,
                                            filetypes=[("선택 영역", "*.json")])
        if path:
            sh.save_area(path, sh.Area(list(self.shapes), self.image_size))
            self.status.set(f"선택 영역을 저장했습니다: {path}  (명령줄에서 --selection 으로 여러 사진에 쓸 수 있습니다)")

    def load_selection(self) -> None:
        if self.preview is None:
            messagebox.showinfo(APP_TITLE, "먼저 미리보기가 되는 사진을 여세요.")
            return
        path = filedialog.askopenfilename(title="선택 영역 불러오기", filetypes=[("선택 영역", "*.json")])
        if not path:
            return
        try:
            loaded = sh.load_area(path).on_photo(self.image_size)
        except sh.SelectionError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self.shapes = loaded
        self._selection_changed()

    def export_jsx(self) -> None:
        if not self.photo_path or (not sh.has_area(self.shapes) and not self.subject.get()):
            messagebox.showinfo(APP_TITLE, "사진을 열고 지울 부분을 먼저 그리세요.")
            return
        options = self._options()
        resolved = self._resolve_output() if options else None
        if not resolved:
            return
        output, _ = resolved
        path = filedialog.asksaveasfilename(title="Photoshop 스크립트로 내보내기", defaultextension=".jsx",
                                            initialfile=f"{self.photo_path.stem}_지우기.jsx",
                                            filetypes=[("Photoshop 스크립트", "*.jsx")])
        if not path:
            return
        try:
            config = api.build_remove_config(self.photo_path, output, sh.Area(list(self.shapes), self.image_size),
                                             options)
            written = api.export_script(path, config)
        except (api.JobError, OSError) as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        messagebox.showinfo(APP_TITLE, f"스크립트를 저장했습니다:\n{written}\n\n"
                                       "Photoshop의 [파일 > 스크립트 > 찾아보기...]에서 이 파일을 실행하세요.")


class PresetDialog:
    """Save the drawn shapes as a named common area."""

    def __init__(self, app: RemoverApp):
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title("공통 영역 저장")
        self.window.transient(app.root)
        self.window.resizable(False, False)
        self.name = tk.StringVar(value=app.preset.get())
        self.fit = tk.StringVar(value="anchor")
        self.anchor = tk.StringVar(value=sh.ANCHOR_LABELS[sh.auto_anchor(app.shapes, app.image_size)])

        body = ttk.Frame(self.window, padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="이름").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(body, textvariable=self.name, width=30)
        entry.grid(row=0, column=1, columnspan=2, sticky="w", padx=(8, 0))
        ttk.Label(body, text="크기나 방향이 다른 사진에서는", justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(14, 4))
        ttk.Radiobutton(body, text="기준 위치에 맞추기:", value="anchor", variable=self.fit).grid(
            row=2, column=0, columnspan=2, sticky="w")
        ttk.Combobox(body, textvariable=self.anchor, values=list(sh.ANCHOR_LABELS.values()), state="readonly",
                     width=12).grid(row=3, column=0, columnspan=3, sticky="w", padx=(24, 0))
        ttk.Label(body, text=ANCHOR_HELP, wraplength=440, justify="left").grid(
            row=4, column=0, columnspan=3, sticky="w", padx=(24, 0), pady=(2, 6))
        ttk.Radiobutton(body, text="사진 크기에 비례해서 늘리기", value="stretch", variable=self.fit).grid(
            row=5, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(body, text="크기나 가로세로 비율이 같은 사진에만 쓰기", value="exact", variable=self.fit).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(2, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=7, column=0, columnspan=3, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="저장", command=self.save).pack(side=tk.LEFT)
        ttk.Button(buttons, text="취소", command=self.window.destroy).pack(side=tk.LEFT, padx=(6, 0))
        self.window.bind("<Return>", lambda e: self.save())
        self.window.bind("<Escape>", lambda e: self.window.destroy())
        entry.focus_set()

    def save(self) -> None:
        try:
            name = settings.check_preset_name(self.name.get())
        except sh.SelectionError as exc:
            messagebox.showerror("공통 영역 저장", str(exc), parent=self.window)
            return
        if any(existing.casefold() == name.casefold() for existing in settings.list_presets()) and \
                not messagebox.askyesno("공통 영역 저장", f"'{name}' 공통 영역을 덮어쓸까요?", parent=self.window):
            return
        fit = self.fit.get()
        anchor = next(key for key, label in sh.ANCHOR_LABELS.items() if label == self.anchor.get())
        try:
            area = sh.Area(list(self.app.shapes), self.app.image_size, fit, anchor if fit == "anchor" else None)
            settings.save_preset(name, area)
        except (OSError, sh.SelectionError) as exc:
            messagebox.showerror("공통 영역 저장", str(exc), parent=self.window)
            return
        self.app._refresh_presets(select=name)
        self.app.status.set(f"공통 영역 '{name}'을(를) 저장했습니다. [여러 사진 한꺼번에 지우기...]에서 쓸 수 있습니다.")
        self.window.destroy()


class BatchWindow:
    """Remove a common area from every photo in a folder, once or whenever new photos arrive."""

    MAX_LOG_LINES = 2000

    def __init__(self, app: RemoverApp):
        self.app = app
        saved = settings.load_settings("batch")
        self.window = tk.Toplevel(app.root)
        self.window.title("여러 사진 한꺼번에 지우기")
        self.window.minsize(640, 460)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.preset = tk.StringVar(value=saved.get("preset") or app.preset.get())
        self.input_dir = tk.StringVar(value=saved.get("input_dir", ""))
        self.output_dir = tk.StringVar(value=saved.get("output_dir", ""))
        self.skip_done = tk.BooleanVar(value=saved.get("skip_done", True))
        self.watch = tk.BooleanVar(value=saved.get("watch", False))
        self.interval = tk.StringVar(value=str(saved.get("interval", 10)))
        self.method = tk.StringVar(value=app.method_summary())
        self.status = tk.StringVar(value="사진 폴더와 공통 영역을 고른 뒤 [시작]을 누르세요.")
        self._events: "queue.Queue" = queue.Queue()
        self._runner: Optional[BatchRunner] = None
        self._poll_after: Optional[str] = None
        self._build()
        self.window.bind("<FocusIn>", lambda e: self.method.set(self.app.method_summary()))

    def _build(self) -> None:
        body = ttk.Frame(self.window, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(7, weight=1)

        ttk.Label(body, text="공통 영역").grid(row=0, column=0, sticky="w")
        self.preset_box = ttk.Combobox(body, textvariable=self.preset, values=settings.list_presets(),
                                       state="readonly", width=24)
        self.preset_box.grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(body, text="사진 폴더").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(body, textvariable=self.input_dir).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        ttk.Button(body, text="찾아보기...", command=lambda: self._choose_dir(self.input_dir)).grid(
            row=1, column=2, padx=(6, 0), pady=(8, 0))
        ttk.Label(body, text="저장 폴더").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(body, textvariable=self.output_dir).grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        ttk.Button(body, text="찾아보기...", command=lambda: self._choose_dir(self.output_dir)).grid(
            row=2, column=2, padx=(6, 0), pady=(8, 0))
        ttk.Label(body, text=f"비워 두면 사진 폴더 안의 '{DEFAULT_OUTPUT_FOLDER}' 폴더에 저장합니다.",
                  foreground="#666666").grid(row=3, column=1, columnspan=2, sticky="w", padx=(8, 0))
        ttk.Label(body, text="지우는 방식").grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Label(body, textvariable=self.method).grid(row=4, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(8, 0))

        repeat = ttk.Frame(body)
        repeat.grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Checkbutton(repeat, text="이미 지운 사진은 건너뛰기 (저장 폴더에 결과가 있는 사진)",
                        variable=self.skip_done).pack(anchor="w")
        watch = ttk.Frame(repeat)
        watch.pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(watch, text="끝난 뒤에도 폴더를 지켜보다가 새 사진이 들어오면 자동으로 지우기, 확인 간격",
                        variable=self.watch).pack(side=tk.LEFT)
        ttk.Spinbox(watch, from_=2, to=3600, width=5, textvariable=self.interval).pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(watch, text="초").pack(side=tk.LEFT)

        controls = ttk.Frame(body)
        controls.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(12, 6))
        self.start_button = ttk.Button(controls, text="▶  시작", style="Accent.TButton", command=self.start)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(controls, text="멈춤", command=self.stop, state="disabled")
        self.stop_button.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(controls, text="저장 폴더 열기", command=self.open_output).pack(side=tk.LEFT, padx=(6, 0))
        self.progress = ttk.Progressbar(controls, mode="determinate", length=180)
        self.progress.pack(side=tk.RIGHT)

        log_frame = ttk.Frame(body)
        log_frame.grid(row=7, column=0, columnspan=3, sticky="nsew")
        self.log = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Label(body, textvariable=self.status, wraplength=600, justify="left").grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def _choose_dir(self, variable: tk.StringVar) -> None:
        path = filedialog.askdirectory(parent=self.window, initialdir=variable.get() or self.input_dir.get() or None)
        if path:
            variable.set(path)

    @property
    def running(self) -> bool:
        return self._runner is not None

    def start(self) -> None:
        if self.running:
            return
        if not self.preset.get():
            messagebox.showinfo("여러 사진 한꺼번에 지우기", BATCH_NO_PRESET, parent=self.window)
            return
        if not self.input_dir.get().strip():
            messagebox.showinfo("여러 사진 한꺼번에 지우기", "사진이 들어 있는 폴더를 고르세요.", parent=self.window)
            return
        options = self.app._options()
        if options is None:
            return
        try:
            interval = max(2.0, float(self.interval.get()))
            output = self.output_dir.get().strip()
            job = BatchJob(Path(self.input_dir.get().strip()), settings.load_preset(self.preset.get()),
                           Path(output) if output else None, options, skip_done=self.skip_done.get())
        except (ValueError, sh.SelectionError) as exc:  # JobError is a ValueError
            messagebox.showerror("여러 사진 한꺼번에 지우기", str(exc), parent=self.window)
            return
        self._save_settings()
        self.app.save_preferences()
        self.method.set(self.app.method_summary())
        self._runner = BatchRunner(job, self._events.put)
        self._log(f"시작: {job.input_dir} → {job.output_dir} ('{self.preset.get()}', {self.method.get()})")
        self._set_running(True)
        watch = self.watch.get()
        threading.Thread(target=self._run, args=(self._runner, watch, interval), daemon=True).start()
        self._poll_after = self.window.after(150, self._poll)

    def _run(self, runner: BatchRunner, watch: bool, interval: float) -> None:
        try:
            runner.run(watch=watch, interval=interval)
        except Exception as exc:  # noqa: BLE001 - shown in the log instead of vanishing with the thread
            self._events.put({"type": "finished", "done": runner.done, "failed": len(runner.failed),
                              "error": f"예상하지 못한 오류: {exc!r}"})

    def stop(self) -> None:
        if self._runner is not None:
            self._runner.stop()
            self.status.set("지금 처리 중인 사진을 끝내고 멈춥니다...")

    def _poll(self) -> None:
        self._poll_after = None
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            self._handle(event)
        if self.running:
            self._poll_after = self.window.after(150, self._poll)

    def _handle(self, event: dict) -> None:
        kind = event["type"]
        if kind == "start":
            self.progress.configure(maximum=event["count"], value=event["index"])
            self.status.set(f"{event['index'] + 1}/{event['count']}  {event['photo'].name} 지우는 중...")
        elif kind == "done":
            self.progress.configure(value=float(self.progress.cget("value")) + 1)
            self._log(f"완료  {event['photo'].name} → {Path(event['result'].get('output') or '').name}")
            for warning in event["result"].get("warnings") or []:
                self._log(f"      참고: {warning}")
        elif kind == "failed":
            self.progress.configure(value=float(self.progress.cget("value")) + 1)
            self._log(f"실패  {event['photo'].name}: {event['error']}")
        elif kind == "error":
            self._log(f"오류  {event['error']}")
        elif kind == "waiting":
            self._log("새 사진을 기다리는 중...")
            self.status.set("새 사진을 기다리고 있습니다. 사진 폴더에 사진을 넣으면 자동으로 지웁니다.")
        elif kind == "finished":
            self._runner = None
            self._set_running(False)
            summary = f"끝났습니다: {event['done']}장 완료, {event['failed']}장 실패"
            if event.get("error"):
                summary += f" (멈춘 이유: {event['error']})"
            elif not event["done"] and not event["failed"]:
                summary = "새로 지울 사진이 없습니다."
            self._log(summary)
            self.status.set(summary)

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > self.MAX_LOG_LINES:
            self.log.delete("1.0", f"{lines - self.MAX_LOG_LINES}.0")
        self.log.configure(state="disabled")
        self.log.see("end")

    def _set_running(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        if running:
            self.progress.configure(value=0, maximum=1)
            self.status.set("시작합니다. Photoshop을 처음 실행할 때는 시간이 걸립니다...")

    def open_output(self) -> None:
        output = self.output_dir.get().strip()
        folder = Path(output) if output else (Path(self.input_dir.get().strip()) / DEFAULT_OUTPUT_FOLDER
                                              if self.input_dir.get().strip() else None)
        if folder is None or not folder.is_dir():
            messagebox.showinfo("여러 사진 한꺼번에 지우기", "저장 폴더가 아직 없습니다.", parent=self.window)
            return
        open_folder(folder)

    def _save_settings(self) -> None:
        try:
            settings.save_settings("batch", {
                "preset": self.preset.get(), "input_dir": self.input_dir.get().strip(),
                "output_dir": self.output_dir.get().strip(), "skip_done": self.skip_done.get(),
                "watch": self.watch.get(), "interval": self.interval.get(),
            })
        except OSError:
            pass  # remembering the choices is a convenience

    def close(self) -> None:
        if self.running:
            if not messagebox.askyesno("여러 사진 한꺼번에 지우기", "처리를 멈추고 창을 닫을까요?", parent=self.window):
                return
            self.stop()  # the photo in progress finishes in the background
        if self._poll_after is not None:
            self.window.after_cancel(self._poll_after)
        self._save_settings()
        self.window.destroy()


# ---------------------------------------------------------------- images


def load_preview(path, max_side: int = PREVIEW_MAX_SIDE):
    """Return ``(preview, (width, height))``.

    The size is the full image size as Photoshop will open it, i.e. after
    applying the EXIF orientation; the preview is a smaller copy of it.
    """
    _register_heif()
    with Image.open(path) as img:
        width, height = img.size
        if img.getexif().get(0x0112, 1) in (5, 6, 7, 8):  # rotated by 90 degrees
            width, height = height, width
        img.draft("RGB", (max_side, max_side))  # JPEG: decode at a reduced size
        preview = _displayable(ImageOps.exif_transpose(img))
    preview.thumbnail((max_side, max_side))
    return preview, (width, height)


def _displayable(img):
    if img.mode in ("RGB", "RGBA"):
        return img
    if img.mode.startswith("I"):  # 16/32-bit grayscale
        return img.convert("I").point(lambda v: v * (1 / 256)).convert("L").convert("RGB")
    if img.mode == "F":
        return img.convert("L").convert("RGB")
    alpha = img.mode in ("LA", "PA", "RGBa", "La") or (img.mode == "P" and "transparency" in img.info)
    return img.convert("RGBA" if alpha else "RGB")


def fit_preview(preview, size: Tuple[int, int]):
    """Resize the preview for display; transparent areas get a checkerboard."""
    resample = Image.LANCZOS if size[0] <= preview.width else Image.BICUBIC
    img = preview.resize(size, resample)
    if img.mode != "RGBA":
        return img
    board = checkerboard(size)
    board.paste(img, (0, 0), img)
    return board


def checkerboard(size: Tuple[int, int], cell: int = 10):
    board = Image.new("RGB", size, (204, 204, 204))
    draw = ImageDraw.Draw(board)
    for y in range(0, size[1], cell):
        for x in range((y // cell % 2) * cell, size[0], 2 * cell):
            draw.rectangle([x, y, x + cell - 1, y + cell - 1], fill=(245, 245, 245))
    return board


def compose_overlay(base, mask):
    """Tint the selected area red and outline it."""
    if mask.getbbox() is None:
        return base
    tinted = Image.blend(base, Image.new("RGB", base.size, OVERLAY), 0.45)
    out = Image.composite(tinted, base, mask)
    out.paste(EDGE, mask=mask.filter(ImageFilter.FIND_EDGES))
    return out


def polygon_area(points) -> float:
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]))) / 2


def _register_heif() -> None:
    try:
        from pillow_heif import register_heif_opener
    except ImportError:
        return
    register_heif_opener()


def open_folder(path: Path) -> None:
    """Show ``path`` in Explorer / Finder / the desktop's file manager."""
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - opening a folder the user chose
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % rgb


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _setup_style(root: tk.Tk) -> None:
    style = ttk.Style(root)
    if sys.platform.startswith("linux") and "clam" in style.theme_names():
        style.theme_use("clam")
    bold = tkfont.nametofont("TkDefaultFont").copy()
    bold.configure(weight="bold")
    style.configure("Accent.TButton", font=bold, padding=(16, 6))
    root._ps_remover_fonts = [bold]  # Tk fonts disappear when garbage collected


def _enable_windows_dpi_awareness() -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - older Windows: stay blurry but working
        pass


def main(photo: Optional[str] = None) -> int:
    if Image is None:
        print("GUI를 쓰려면 Pillow가 필요합니다: pip install Pillow", file=sys.stderr)
        return 1
    if sys.platform == "win32":
        _enable_windows_dpi_awareness()
    root = tk.Tk()
    root.title(APP_TITLE)
    width = min(1180, root.winfo_screenwidth() - 60)
    height = min(820, root.winfo_screenheight() - 100)
    root.geometry(f"{width}x{height}")
    root.minsize(min(940, width), min(600, height))
    _setup_style(root)
    RemoverApp(root, photo)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
