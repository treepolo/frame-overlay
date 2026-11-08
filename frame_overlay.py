"""幀數標註工具：在影片每一幀上疊加幀號 + 浮水印（同一個底框，逐幀用 Pillow 繪製）。
需要：Python 3 + Pillow（pip install pillow）+ ffmpeg/ffprobe（已加入 PATH）。
設定自動存到同資料夾的 settings.json。
"""
import json
import os
import shutil
import subprocess
import collections
import hashlib
import multiprocessing
import queue
import threading
import time
from functools import lru_cache
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser

from PIL import Image, ImageDraw, ImageFont, ImageTk

# 以 pythonw（無主控台）執行時，每次呼叫 ffmpeg/ffprobe 都會跳出終端機視窗，這裡統一隱藏
if os.name == "nt":
    _orig_popen_init = subprocess.Popen.__init__

    def _quiet_popen_init(self, *args, **kwargs):
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
        _orig_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _quiet_popen_init

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
PREVIEW_PATH = os.path.join(APP_DIR, "_preview_frame.png")
EXPORT_LOG_PATH = os.path.join(APP_DIR, "_export_log.txt")
FONT_DIRS = [
    r"C:\Windows\Fonts",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
]

# 浮水印內容固定，字級與間距依幀號字級等比例計算（幀號 68 : 浮水印 32 : 間距 8）
WM_TEXT = "IG@treepolooo"
BASE_NUM_SIZE = 68
WM_RATIO = 32 / BASE_NUM_SIZE
GAP_RATIO = 8 / BASE_NUM_SIZE

DEFAULTS = {
    "input": "",
    "output": "",
    "font_file": r"C:\Windows\Fonts\arial.ttf",
    "font_size": 68,
    "font_color": "#FFFFFF",
    "border_on": True,
    "border_width": 3,
    "border_color": "#000000",
    "box_on": True,
    "box_color": "#000000",
    "box_opacity": 0.6,
    "box_padding": 12,
    "x_frac": 0.03,   # 底框左上角位置，佔影片寬度的比例
    "y_frac": 0.03,   # 佔影片高度的比例
    "prefix": "",
    "suffix": "",
}

PREVIEW_MAX_W = 720
PREVIEW_MAX_H = 560
HANDLE = 8  # 縮放把手的點選範圍（預覽像素）


# ---------- 設定 / 檔案 ----------
def load_settings():
    cfg = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_settings(cfg):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return None
    except OSError as e:
        return str(e)


def list_fonts():
    """回傳 {字型名稱: 完整路徑}。"""
    found = {}
    for d in FONT_DIRS:
        if d and os.path.isdir(d):
            for name in os.listdir(d):
                if name.lower().endswith((".ttf", ".otf")):
                    found.setdefault(os.path.splitext(name)[0], os.path.join(d, name))
    return found


def probe_video(path):
    """回傳 (寬, 高, 幀率字串)，失敗回傳 None。"""
    ffp = shutil.which("ffprobe")
    if not ffp:
        return None
    r = subprocess.run([ffp, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height,r_frame_rate",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    try:
        w, h, fps = r.stdout.strip().split(",")
        return int(w), int(h), fps
    except ValueError:
        return None


def extract_frame(path):
    ff = shutil.which("ffmpeg")
    if not ff:
        return None
    r = subprocess.run([ff, "-y", "-i", path, "-frames:v", "1", PREVIEW_PATH],
                       capture_output=True)
    return PREVIEW_PATH if r.returncode == 0 and os.path.isfile(PREVIEW_PATH) else None


@lru_cache(maxsize=32)
def load_font(path, size):
    return ImageFont.truetype(path, size)


def parse_fps(s):
    """'30000/1001' 或 '30/1' 轉成浮點數，失敗時回傳 30。"""
    try:
        if "/" in s:
            num, den = s.split("/")
            return float(num) / float(den)
        return float(s)
    except (ValueError, ZeroDivisionError):
        return 30.0


PROXY_DIR = os.path.join(APP_DIR, "_proxy")


def make_proxy(src, total, progress=None):
    """產生預覽用的低解析度檔（高度 720、每幀都是關鍵幀），播放與跳轉會快很多。
    已存在且來源未變時直接重用。失敗回傳 None。"""
    st = os.stat(src)
    key = f"{os.path.abspath(src)}|{st.st_size}|{int(st.st_mtime)}"
    out = os.path.join(PROXY_DIR, hashlib.md5(key.encode("utf-8")).hexdigest() + ".mp4")
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    ff = shutil.which("ffmpeg")
    if not ff:
        return None
    os.makedirs(PROXY_DIR, exist_ok=True)
    tmp = out + ".part.mp4"
    cmd = [ff, "-v", "error", "-nostats", "-progress", "pipe:1", "-y", "-i", src,
           "-an", "-vf", "scale=-2:720", "-c:v", "libx264", "-preset", "ultrafast",
           "-crf", "23", "-g", "1", "-pix_fmt", "yuv420p", tmp]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace")
    for line in p.stdout:
        if line.startswith("frame=") and progress and total:
            try:
                progress(int(line.split("=", 1)[1]), total)
            except ValueError:
                pass
    p.wait()
    if p.returncode != 0 or not os.path.isfile(tmp):
        return None
    os.replace(tmp, out)
    return out


def grab_frame(path, n, fps, w, h):
    """取得第 n 幀（已縮放成 w×h 的 RGB 原始資料），失敗回傳 None。"""
    ff = shutil.which("ffmpeg")
    if not ff:
        return None
    r = subprocess.run([ff, "-v", "error", "-ss", f"{n / fps:.4f}", "-i", path,
                        "-frames:v", "1", "-vf", f"scale={w}:{h}",
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True)
    return r.stdout if len(r.stdout) == w * h * 3 else None


def frame_total(path, fps):
    """估算總幀數（用於進度條）；無法得知時回傳 0。"""
    ffp = shutil.which("ffprobe")
    if not ffp:
        return 0
    r = subprocess.run([ffp, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=nb_frames,duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    nb, dur = 0, 0.0
    for p in r.stdout.strip().split(","):
        try:
            nb = int(p)
            continue
        except ValueError:
            pass
        try:
            dur = float(p)
        except ValueError:
            pass
    if nb > 0:
        return nb
    return round(dur * parse_fps(fps)) if dur > 0 else 0


def rgba(hex_color, alpha=255):
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


# ---------- 繪製（預覽與輸出共用） ----------
def block_metrics(cfg, n, s):
    """計算底框尺寸（已乘上縮放 s）。回傳 dict。
    浮水印使用幀號的字型、字色與描邊，字級 = 幀號字級 × 32/68，間距 = 幀號字級 × 8/68。"""
    size = cfg["font_size"]
    fN = load_font(cfg["font_file"], max(1, round(size * s)))
    swN = round(cfg["border_width"] * s) if cfg["border_on"] else 0
    na, nd = fN.getmetrics()
    lhN = na + nd
    num_text = f"{cfg['prefix']}{n}{cfg['suffix']}"
    numW = fN.getlength(num_text) + 2 * swN

    fW = load_font(cfg["font_file"], max(1, round(size * WM_RATIO * s)))
    swW = swN
    wa, wd = fW.getmetrics()
    lhW = wa + wd
    wmW = fW.getlength(WM_TEXT) + 2 * swW
    gap = size * GAP_RATIO * s

    inner = max(numW, wmW)          # 兩行共用寬度：底框寬度 = 較長的那一行
    pad = cfg["box_padding"] * s
    inner_h = lhN + gap + lhW
    return {
        "fN": fN, "swN": swN, "lhN": lhN, "numW": numW, "num_text": num_text,
        "fW": fW, "swW": swW, "lhW": lhW, "wmW": wmW, "gap": gap,
        "inner": inner, "pad": pad,
        "w": inner + 2 * pad, "h": inner_h + 2 * pad,
    }


def draw_overlay(ov, cfg, n, s, X, Y, H):
    """在 RGBA 圖 ov 上畫出底框與兩行文字。
    X, Y：底框左上角（ov 像素）；s：縮放；H：畫面高度（用來決定浮水印放上或下）。
    回傳底框範圍 (x0, y0, x1, y1)。"""
    m = block_metrics(cfg, n, s)
    d = ImageDraw.Draw(ov)
    x0, y0 = X, Y
    x1, y1 = X + m["w"], Y + m["h"]
    if cfg["box_on"]:
        d.rectangle((x0, y0, x1, y1),
                    fill=rgba(cfg["box_color"], round(255 * float(cfg["box_opacity"]))))

    pad, inner = m["pad"], m["inner"]
    # 幀號中心在上半部 → 浮水印在下方；下半部 → 在上方
    wm_below = (Y + m["h"] / 2) < H / 2
    if wm_below:
        num_top = Y + pad
        wm_top = num_top + m["lhN"] + m["gap"]
    else:
        wm_top = Y + pad
        num_top = wm_top + m["lhW"] + m["gap"]

    border_color = rgba(cfg["border_color"])
    font_fill = rgba(cfg["font_color"])

    # 兩行都置中於 inner 寬度內
    num_x = X + pad + (inner - m["numW"]) / 2 + m["swN"]
    d.text((num_x, num_top), m["num_text"], font=m["fN"],
           fill=font_fill, stroke_width=m["swN"], stroke_fill=border_color)

    wm_x = X + pad + (inner - m["wmW"]) / 2 + m["swW"]
    d.text((wm_x, wm_top), WM_TEXT, font=m["fW"],
           fill=font_fill, stroke_width=m["swW"], stroke_fill=border_color)
    return x0, y0, x1, y1


# ---------- 匯出 ----------
def encode_args(codec):
    if codec == "h264_nvenc":   # NVIDIA GPU 編碼
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
    return ["-c:v", "libx264", "-crf", "18", "-preset", "medium"]


def render_chunk(cfg, start, frames, vw, vh):
    """工作程序：疊加一段幀，回傳 (合成後的 RGB 資料, 幀數)。"""
    X, Y = cfg["x_frac"] * vw, cfg["y_frac"] * vh
    out = []
    for i, raw in enumerate(frames):
        frame = Image.frombytes("RGB", (vw, vh), raw).convert("RGBA")
        ov = Image.new("RGBA", (vw, vh), (0, 0, 0, 0))
        draw_overlay(ov, cfg, start + i, 1.0, X, Y, vh)
        out.append(Image.alpha_composite(frame, ov).convert("RGB").tobytes())
    return b"".join(out), len(frames)


def _export_pass(cfg, ff, vw, vh, fps, total, codec, progress, workers):
    """回傳 (是否成功, 錯誤記錄)。"""
    size = vw * vh * 3
    chunk = 16
    dec = subprocess.Popen([ff, "-v", "error", "-i", cfg["input"],
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    with open(EXPORT_LOG_PATH, "w", encoding="utf-8") as log:
        enc = subprocess.Popen([ff, "-y", "-loglevel", "error",
                                "-f", "rawvideo", "-pix_fmt", "rgb24",
                                "-s", f"{vw}x{vh}", "-r", fps, "-i", "-",
                                "-i", cfg["input"],
                                "-map", "0:v", "-map", "1:a?"]
                               + encode_args(codec)
                               + ["-pix_fmt", "yuv420p", "-c:a", "copy", cfg["output"]],
                               stdin=subprocess.PIPE, stderr=log)
        pool = multiprocessing.Pool(workers)
        pending = collections.deque()
        done = 0
        error = None

        def flush_one():
            nonlocal done
            data, count = pending.popleft().get()
            enc.stdin.write(data)
            done += count
            if progress:
                progress(done, total)

        try:
            start = 0
            while True:
                frames = []
                for _ in range(chunk):
                    raw = dec.stdout.read(size)
                    if len(raw) < size:
                        break
                    frames.append(raw)
                if not frames:
                    break
                pending.append(pool.apply_async(render_chunk, (cfg, start, frames, vw, vh)))
                start += len(frames)
                while len(pending) >= workers * 2:   # 限制記憶體用量
                    flush_one()
                if len(frames) < chunk:
                    break
            while pending:
                flush_one()
        except Exception as e:      # 包含編碼器失敗造成的 BrokenPipe
            error = str(e)
        finally:
            pool.terminate()
            pool.join()
            try:
                enc.stdin.close()
            except OSError:
                pass
            enc.wait()
            dec.stdout.close()
            dec.wait()

    ok = error is None and enc.returncode == 0
    with open(EXPORT_LOG_PATH, encoding="utf-8", errors="replace") as log:
        return ok, log.read()[-2000:] or (error or "")


_nvenc_ok = None   # 快取：這個程序內是否可用 NVENC（None 表示尚未檢查）


def nvenc_available(ff):
    """實際用一張小圖試編 h264_nvenc，比對驅動版本更可靠。結果會快取。"""
    global _nvenc_ok
    if _nvenc_ok is None:
        r = subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256",
                            "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-"],
                           capture_output=True)
        _nvenc_ok = r.returncode == 0
    return _nvenc_ok


def run_export(cfg, progress=None):
    ff = shutil.which("ffmpeg")
    if not ff:
        return False, "找不到 ffmpeg，請安裝並加入系統 PATH。"
    info = probe_video(cfg["input"])
    if not info:
        return False, "無法讀取影片資訊。"
    vw, vh, fps = info
    total = frame_total(cfg["input"], fps)
    workers = max(1, (os.cpu_count() or 2) - 1)

    if nvenc_available(ff):
        ok, msg = _export_pass(cfg, ff, vw, vh, fps, total, "h264_nvenc", progress, workers)
        if ok:
            return True, cfg["output"]
    # 沒有可用的 NVIDIA 編碼器，或 GPU 編碼中途失敗：改用 CPU 編碼
    ok, msg = _export_pass(cfg, ff, vw, vh, fps, total, "libx264", progress, workers)
    return (True, cfg["output"]) if ok else (False, msg)


# ---------- 介面 ----------
class App:
    def __init__(self, root):
        self.root = root
        root.title("幀數標註工具")
        root.resizable(False, False)
        self.cfg = load_settings()
        self.fonts = list_fonts()
        cur_name = os.path.splitext(os.path.basename(self.cfg["font_file"]))[0]
        if self.cfg["font_file"] and cur_name not in self.fonts:
            self.fonts[cur_name] = self.cfg["font_file"]

        self.vars = {}
        self.colors = {k: self.cfg[k] for k in ("font_color", "border_color", "box_color")}
        self.color_btns = {}
        self.xf = float(self.cfg["x_frac"])
        self.yf = float(self.cfg["y_frac"])
        self.vw, self.vh = 1920, 1080
        self.cw, self.ch = PREVIEW_MAX_W, round(PREVIEW_MAX_W * 9 / 16)
        self.base = None
        self.hit = None          # 底框範圍 (x0, y0, x1, y1)，預覽座標
        self.mode = None
        self.grab = (0, 0)
        self.scale_ref = None
        self.photo = None
        self.playing = False
        self.play_after = None
        self.play_proc = None
        self.play_stop = None
        self.play_queue = None
        self.cur_frame = 0
        self.fps = 30.0
        self.ui_queue = queue.Queue()
        self.total_frames = 0
        self.seek_dragging = False
        self.seek_token = 0
        self.load_token = 0
        self.video_path = None
        self.play_path = None

        frm = ttk.Frame(root, padding=12)
        frm.grid(row=0, column=0, sticky="n")
        self.frm = frm
        self.r = 0

        # 檔案
        self.section("檔案")
        self.file_row("輸入影片", "input", self.pick_input)
        self.file_row("輸出影片", "output", self.pick_output)

        # 字型（幀號與浮水印共用）
        self.section("字型")
        self.var("font_name", "str", cur_name)
        cb = ttk.Combobox(frm, textvariable=self.vars["font_name"],
                          values=sorted(self.fonts), state="readonly", width=34)
        self.put("字型", cb, span=2)
        ttk.Button(frm, text="瀏覽字型…", command=self.pick_font).grid(
            row=self.r - 1, column=3, padx=4, sticky="w")
        self.var("font_size", "int")
        sp = ttk.Spinbox(frm, from_=8, to=400, textvariable=self.vars["font_size"], width=8)
        sp.bind("<Return>", self.enforce_font_min)
        sp.bind("<FocusOut>", self.enforce_font_min)
        self.put("幀數字大小", sp)
        self.color_row("字色", "font_color")

        # 描邊（幀號與浮水印共用）
        self.section("描邊")
        self.var("border_on", "bool")
        self.var("border_width", "int")
        self.put("", ttk.Checkbutton(frm, text="啟用描邊", variable=self.vars["border_on"]))
        self.put("寬度", ttk.Spinbox(frm, from_=0, to=30, textvariable=self.vars["border_width"], width=8))
        self.color_row("描邊色", "border_color")

        # 底框
        self.section("底框")
        self.var("box_on", "bool")
        self.var("box_opacity", "float")
        self.var("box_padding", "int")
        self.put("", ttk.Checkbutton(frm, text="啟用底框", variable=self.vars["box_on"]))
        self.color_row("框色", "box_color")
        self.put("不透明度", ttk.Spinbox(frm, from_=0, to=1, increment=0.05,
                                        textvariable=self.vars["box_opacity"], width=8))
        self.put("內距", ttk.Spinbox(frm, from_=0, to=100, textvariable=self.vars["box_padding"], width=8))

        # 幀號文字
        self.section("幀號文字")
        self.var("prefix", "str")
        self.var("suffix", "str")
        self.put("前綴", ttk.Entry(frm, textvariable=self.vars["prefix"], width=20))
        self.put("後綴", ttk.Entry(frm, textvariable=self.vars["suffix"], width=20))

        # 浮水印（固定內容，樣式跟隨幀號）

        # 按鈕
        btns = ttk.Frame(frm)
        btns.grid(row=self.r, column=0, columnspan=4, pady=(12, 0), sticky="ew")
        ttk.Button(btns, text="儲存設定", command=self.save).pack(side="left")
        self.run_btn = ttk.Button(btns, text="開始輸出", command=self.start)
        self.run_btn.pack(side="right")
        self.status = ttk.Label(frm, text="")
        self.status.grid(row=self.r + 1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.pbar = ttk.Progressbar(frm, length=400, mode="determinate", maximum=100)
        self.pbar.grid(row=self.r + 2, column=0, columnspan=4, sticky="ew", pady=(4, 0))

        # 右側：預覽
        right = ttk.Frame(root, padding=(0, 12, 12, 12))
        right.grid(row=0, column=1, sticky="n")
        ttk.Label(right, text="預覽（第一幀）", font=("", 10, "bold")).pack(anchor="w")
        self.canvas = tk.Canvas(right, width=self.cw, height=self.ch,
                                bg="#202020", highlightthickness=0)
        self.canvas.pack(pady=(4, 4))
        ttk.Label(right, text="拖曳底框移動位置；拖右下角藍色方塊或滾輪縮放字級").pack(anchor="w")
        self.seek = ttk.Scale(right, from_=0, to=1, length=self.cw)
        self.seek.pack(anchor="w", pady=(6, 0))
        self.seek.bind("<ButtonPress-1>", self.seek_press)
        self.seek.bind("<B1-Motion>", self.seek_move)
        self.seek.bind("<ButtonRelease-1>", self.seek_release)
        ctrl = ttk.Frame(right)
        ctrl.pack(anchor="w", pady=(6, 0))
        self.play_btn = ttk.Button(ctrl, text="▶ 播放", command=self.toggle_play)
        self.play_btn.pack(side="left")
        ttk.Button(ctrl, text="⏹ 停止", command=self.stop_play).pack(side="left", padx=6)
        self.frame_label = ttk.Label(ctrl, text="")
        self.frame_label.pack(side="left")
        ttk.Button(right, text="水平垂直置中", command=self.center).pack(anchor="w", pady=(6, 0))

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "mode", None))
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<MouseWheel>", self.on_wheel)

        # 所有設定變動時即時更新預覽
        for v in self.vars.values():
            v.trace_add("write", self.render_preview)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.sync_output(self.cfg["input"])
        self._poll()
        self.load_video(self.cfg["input"])

    # ---------- 背景執行緒 → 介面（Tk 只能在主執行緒操作） ----------
    def post(self, fn, *args):
        self.ui_queue.put((fn, args))

    def _poll(self):
        try:
            while True:
                fn, args = self.ui_queue.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    # ---------- 版面小工具 ----------
    def section(self, title):
        ttk.Label(self.frm, text=title, font=("", 10, "bold")).grid(
            row=self.r, column=0, columnspan=4, sticky="w", pady=(10, 2))
        self.r += 1

    def put(self, label, widget, span=1):
        ttk.Label(self.frm, text=label).grid(row=self.r, column=0, sticky="w", padx=(0, 8), pady=2)
        widget.grid(row=self.r, column=1, columnspan=span, sticky="w", pady=2)
        self.r += 1

    def var(self, key, kind, value=None):
        cls = {"str": tk.StringVar, "int": tk.IntVar,
               "float": tk.DoubleVar, "bool": tk.BooleanVar}[kind]
        v = cls(value=self.cfg[key] if value is None else value)
        self.vars[key] = v
        return v

    def color_row(self, label, key):
        btn = tk.Button(self.frm, width=8, bg=self.colors[key],
                        command=lambda k=key: self.pick_color(k))
        self.color_btns[key] = btn
        self.put(label, btn)

    def file_row(self, label, key, picker):
        self.var(key, "str")
        ttk.Label(self.frm, text=label).grid(row=self.r, column=0, sticky="w", padx=(0, 8), pady=2)
        ttk.Entry(self.frm, textvariable=self.vars[key], width=44).grid(
            row=self.r, column=1, columnspan=2, sticky="w", pady=2)
        ttk.Button(self.frm, text="選擇…", command=picker).grid(row=self.r, column=3, padx=4)
        self.r += 1

    # ---------- 字級下限 ----------
    def font_min(self):
        """短邊 1080 時最小字級為 74，其他解析度依短邊等比例換算。"""
        return max(1, round(74 * min(self.vw, self.vh) / 1080))

    def enforce_font_min(self, *_):
        try:
            v = int(self.vars["font_size"].get())
        except (tk.TclError, ValueError):
            v = 0
        if v < self.font_min():
            self.vars["font_size"].set(self.font_min())

    # ---------- 預覽 ----------
    def load_video(self, path):
        self.pause()
        self.cur_frame = 0
        self.load_token += 1
        token = self.load_token
        self.video_path = path
        self.play_path = None
        if not path or not os.path.isfile(path):
            self._apply_video(token, None, None)
            return
        self.status.configure(text="載入影片中…")
        self.pbar.configure(mode="indeterminate")
        self.pbar.start(12)

        def worker():
            info = probe_video(path)
            frame = extract_frame(path) if info else None
            total = frame_total(path, info[2]) if info else 0
            proxy = None
            if info:
                proxy = make_proxy(path, total, lambda n, t: self.post(self._set_proxy_progress, n, t))
            self.post(self._apply_video, token, info, frame, total, proxy)

        threading.Thread(target=worker, daemon=True).start()

    def _set_proxy_progress(self, n, total):
        if self.pbar.cget("mode") != "determinate":
            self.pbar.stop()
            self.pbar.configure(mode="determinate")
        pct = min(100.0, n * 100.0 / total)
        self.pbar.configure(value=pct)
        self.status.configure(text=f"準備預覽檔：{n} / {total} 幀（{pct:.0f}%）")

    def _apply_video(self, token, info, frame, total=0, proxy=None):
        if token != self.load_token:     # 已經換了別的影片，忽略舊的載入結果
            return
        self.pbar.stop()
        self.pbar.configure(mode="determinate", value=0)
        self.base = None
        self.total_frames = total
        self.play_path = proxy or self.video_path
        self.seek.configure(to=max(1, total - 1))
        self.seek.set(0)
        if info:
            self.vw, self.vh = info[0], info[1]
            self.fps = parse_fps(info[2])
            if frame:
                with Image.open(frame) as im:
                    self.base = im.convert("RGBA")
            self.status.configure(text=f"影片已載入：{os.path.basename(self.video_path or '')}")
        ratio = self.vw / self.vh
        if ratio >= PREVIEW_MAX_W / PREVIEW_MAX_H:
            cw = PREVIEW_MAX_W
            ch = round(PREVIEW_MAX_W / ratio)
        else:
            ch = PREVIEW_MAX_H
            cw = round(PREVIEW_MAX_H * ratio)
        self.cw, self.ch = cw, ch
        if self.base is not None:
            self.base = self.base.resize((cw, ch))
        self.canvas.configure(width=cw, height=ch)
        self.enforce_font_min()
        self.render_preview()

    def render_preview(self, *_):
        if not hasattr(self, "canvas") or self.playing:
            return
        try:
            cfg = self.collect()
        except (ValueError, KeyError):
            return
        W, H = self.cw, self.ch
        s = W / self.vw
        self._clamp(cfg, 0, s)
        base = self.base if self.base is not None else Image.new("RGBA", (W, H), (70, 70, 70, 255))
        ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        x0, y0, x1, y1 = draw_overlay(ov, cfg, 0, s, self.xf * W, self.yf * H, H)  # 範例幀號 0
        self.hit = (x0, y0, x1, y1)

        d = ImageDraw.Draw(ov)
        blue = (0, 160, 255, 255)
        d.rectangle((x0, y0, x1, y1), outline=blue)
        d.rectangle((x1 - 4, y1 - 4, x1 + 4, y1 + 4), fill=blue)

        self.photo = ImageTk.PhotoImage(Image.alpha_composite(base, ov).convert("RGB"))
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

    def _clamp(self, cfg, n, s):
        """底框必須完整留在畫面內（右側、下方也一樣）。"""
        W, H = self.cw, self.ch
        m = block_metrics(cfg, n, s)
        self.xf = min(max(self.xf, 0.0), max(0.0, (W - m["w"]) / W))
        self.yf = min(max(self.yf, 0.0), max(0.0, (H - m["h"]) / H))

    def toggle_play(self):
        if self.playing:
            self.pause()
        else:
            self.play()

    def play(self):
        path = self.play_path or self.vars["input"].get()
        if not path or not os.path.isfile(path):
            self.status.configure(text="請先選擇有效的輸入影片。")
            return
        ff = shutil.which("ffmpeg")
        if not ff or not self.fps:
            self.status.configure(text="無法播放：請確認 ffmpeg 已安裝。")
            return
        self.playing = True
        self.play_btn.configure(text="⏸ 暫停")

        start = self.cur_frame
        size = self.cw * self.ch * 3
        cmd = [ff, "-v", "error", "-ss", f"{start / self.fps:.4f}", "-i", path,
               "-vf", f"scale={self.cw}:{self.ch}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        stop = threading.Event()
        q = queue.Queue(maxsize=24)
        self.play_proc, self.play_stop, self.play_queue = proc, stop, q

        self.play_pending = collections.deque()
        self.play_eof = False
        self.play_start = start
        self.play_t0 = time.monotonic()

        def put(item):
            while not stop.is_set():
                try:
                    q.put(item, timeout=0.1)
                    return
                except queue.Full:
                    pass

        def reader():
            idx = start
            try:
                while not stop.is_set():
                    raw = proc.stdout.read(size)
                    if len(raw) < size:
                        break
                    put((idx, raw))
                    idx += 1
            except (OSError, ValueError):
                pass
            finally:
                put(None)

        threading.Thread(target=reader, daemon=True).start()
        self._tick()

    def _tick(self):
        """依影片原本的時間軸顯示：目前時間對應到第幾幀，就顯示那一幀（落後時跳幀，維持即時速度）。"""
        self.play_after = None
        if not self.playing:
            return
        # 把解碼好的幀搬到待顯示清單（最多 8 幀，超過就讓解碼端等待）
        while len(self.play_pending) < 8 and not self.play_eof:
            try:
                item = self.play_queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self.play_eof = True
            else:
                self.play_pending.append(item)

        target = self.play_start + (time.monotonic() - self.play_t0) * self.fps
        show = None
        while self.play_pending and self.play_pending[0][0] <= target:
            show = self.play_pending.popleft()
        if show is not None:
            self.cur_frame = show[0]
            self._show_frame(show[1], show[0])

        if self.play_eof and not self.play_pending:   # 影片播完
            self.pause()
            self.cur_frame = 0
            self.seek.set(0)
            self.render_preview()
            return
        self.play_after = self.root.after(10, self._tick)

    def _show_frame(self, raw, n):
        try:
            cfg = self.collect()
        except (ValueError, KeyError):
            return
        s = self.cw / self.vw
        self._clamp(cfg, n, s)
        img = Image.frombytes("RGB", (self.cw, self.ch), raw).convert("RGBA")
        ov = Image.new("RGBA", (self.cw, self.ch), (0, 0, 0, 0))
        self.hit = draw_overlay(ov, cfg, n, s, self.xf * self.cw, self.yf * self.ch, self.ch)
        self.photo = ImageTk.PhotoImage(Image.alpha_composite(img, ov).convert("RGB"))
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.frame_label.configure(text=f"幀 {n}")
        if not self.seek_dragging:
            self.seek.set(n)

    def pause(self):
        self.playing = False
        if self.play_after:
            self.root.after_cancel(self.play_after)
            self.play_after = None
        if self.play_stop:
            self.play_stop.set()
            self.play_stop = None
        if self.play_proc:
            proc = self.play_proc
            try:
                proc.kill()
            except Exception:
                pass
            threading.Thread(target=proc.wait, daemon=True).start()   # 不在介面執行緒上等待
            self.play_proc = None
        self.play_queue = None
        if hasattr(self, "play_btn"):
            self.play_btn.configure(text="▶ 播放")

    def stop_play(self):
        self.pause()
        self.cur_frame = 0
        self.seek.set(0)
        self.frame_label.configure(text="")
        self.render_preview()

    # ---------- 進度條跳轉 ----------
    def seek_press(self, e):
        self.seek_dragging = True
        self._set_seek_from_x(e.x)

    def seek_move(self, e):
        if self.seek_dragging:
            self._set_seek_from_x(e.x)

    def seek_release(self, e):
        if self.seek_dragging:
            self.seek_dragging = False
            self.seek_to(round(float(self.seek.get())))

    def _set_seek_from_x(self, x):
        w = max(1, self.seek.winfo_width())
        frac = min(max(x / w, 0.0), 1.0)
        value = frac * max(1, self.total_frames - 1)
        self.seek.set(value)
        self.frame_label.configure(text=f"幀 {round(value)}")

    def seek_to(self, n):
        path = self.play_path or self.vars["input"].get()
        if not path or not os.path.isfile(path) or not self.total_frames:
            return
        was_playing = self.playing
        self.pause()
        n = max(0, min(int(n), self.total_frames - 1))
        self.cur_frame = n
        self.seek.set(n)
        self.seek_token += 1
        token = self.seek_token
        w, h, fps = self.cw, self.ch, self.fps

        def worker():
            raw = grab_frame(path, n, fps, w, h)
            self.post(self._after_seek, token, raw, n, was_playing)

        threading.Thread(target=worker, daemon=True).start()

    def _after_seek(self, token, raw, n, was_playing):
        if token != self.seek_token:      # 已有更新的跳轉要求
            return
        if raw:
            self._show_frame(raw, n)
        if was_playing:
            self.play()

    def _in_handle(self, x, y):
        return abs(x - self.hit[2]) <= HANDLE and abs(y - self.hit[3]) <= HANDLE

    def _in_box(self, x, y):
        x0, y0, x1, y1 = self.hit
        return x0 <= x <= x1 and y0 <= y <= y1

    def on_press(self, e):
        if not self.hit:
            return
        if self._in_handle(e.x, e.y):
            self.mode = "scale"
            self.scale_ref = (self.hit[2], self.hit[0], int(self.vars["font_size"].get()))
        elif self._in_box(e.x, e.y):
            self.mode = "move"
            self.grab = (e.x - self.hit[0], e.y - self.hit[1])
        else:
            self.mode = None

    def on_drag(self, e):
        if self.mode == "move":
            gx, gy = self.grab
            self.xf = min(max((e.x - gx) / self.cw, 0.0), 1.0)
            self.yf = min(max((e.y - gy) / self.ch, 0.0), 1.0)
            self.render_preview()
        elif self.mode == "scale" and self.scale_ref:
            right0, left, size0 = self.scale_ref
            if right0 - left <= 0:
                return
            f = max(e.x - left, 1) / (right0 - left)
            self.vars["font_size"].set(int(min(max(round(size0 * f), self.font_min()), 400)))

    def on_motion(self, e):
        cur = ""
        if self.hit:
            if self._in_handle(e.x, e.y):
                cur = "sizing"
            elif self._in_box(e.x, e.y):
                cur = "fleur"
        self.canvas.configure(cursor=cur)

    def on_wheel(self, e):
        if not self.hit:
            return
        try:
            size = int(self.vars["font_size"].get())
        except tk.TclError:
            return
        step = 2 if e.delta > 0 else -2
        self.vars["font_size"].set(min(max(size + step, self.font_min()), 400))

    def center(self):
        if not self.hit:
            return
        x0, y0, x1, y1 = self.hit
        self.xf = ((self.cw - (x1 - x0)) / 2) / self.cw
        self.yf = ((self.ch - (y1 - y0)) / 2) / self.ch
        self.render_preview()

    # ---------- 其他事件 ----------
    def pick_color(self, key):
        c = colorchooser.askcolor(color=self.colors[key], parent=self.root)[1]
        if c:
            self.colors[key] = c.upper()
            self.color_btns[key].configure(bg=c)
            self.render_preview()

    def pick_input(self):
        p = filedialog.askopenfilename(filetypes=[("影片", "*.mp4 *.mov *.mkv *.avi"), ("所有檔案", "*.*")])
        if p:
            self.vars["input"].set(p)
            self.sync_output(p)
            self.load_video(p)

    def sync_output(self, input_path):
        """輸出路徑 = 輸入檔所在資料夾 / 輸入檔名_標註.mp4"""
        if not input_path:
            return
        folder = os.path.dirname(input_path)
        stem = os.path.splitext(os.path.basename(input_path))[0]
        self.vars["output"].set(os.path.join(folder, stem + "_標註.mp4"))

    def pick_output(self):
        p = filedialog.asksaveasfilename(defaultextension=".mp4", filetypes=[("MP4", "*.mp4")])
        if p:
            self.vars["output"].set(p)

    def pick_font(self):
        p = filedialog.askopenfilename(filetypes=[("字型", "*.ttf *.otf")])
        if p:
            name = os.path.splitext(os.path.basename(p))[0]
            self.fonts[name] = p
            cb_values = sorted(self.fonts)
            self.vars["font_name"].set(name)
            for w in self.frm.winfo_children():
                if isinstance(w, ttk.Combobox):
                    w.configure(values=cb_values)

    def collect(self):
        try:
            cfg = {k: v.get() for k, v in self.vars.items() if k != "font_name"}
        except tk.TclError:
            raise ValueError("有數值欄位格式錯誤（請輸入數字）。")
        cfg["font_file"] = self.fonts[self.vars["font_name"].get()]
        cfg.update(self.colors)
        cfg["x_frac"] = self.xf
        cfg["y_frac"] = self.yf
        cfg["font_size"] = max(self.font_min(), int(cfg["font_size"]))
        return cfg

    def save(self):
        try:
            cfg = self.collect()
        except ValueError as e:
            messagebox.showerror("錯誤", str(e))
            return False
        err = save_settings(cfg)
        if err:
            messagebox.showerror("儲存失敗", err)
            return False
        self.status.configure(text="設定已儲存")
        return True

    def on_close(self):
        self.pause()
        try:
            save_settings(self.collect())
        except ValueError:
            pass
        self.root.destroy()

    def start(self):
        try:
            cfg = self.collect()
        except ValueError as e:
            messagebox.showerror("錯誤", str(e))
            return
        if not cfg["input"] or not os.path.isfile(cfg["input"]):
            messagebox.showerror("錯誤", "請選擇有效的輸入影片。")
            return
        if not cfg["output"]:
            messagebox.showerror("錯誤", "請指定輸出路徑。")
            return
        save_settings(cfg)
        self.run_btn.configure(state="disabled")
        self.status.configure(text="輸出中，請稍候…")
        self.pbar.configure(mode="determinate", value=0)

        def progress(n, total):
            pct = min(100.0, n * 100.0 / total) if total else 0.0
            text = (f"輸出中：{n} / {total} 幀（{pct:.0f}%）" if total
                    else f"輸出中：已處理 {n} 幀…")
            self.post(lambda: (self.pbar.configure(value=pct),
                               self.status.configure(text=text)))

        def worker():
            ok, msg = run_export(cfg, progress)
            self.post(self.done, ok, msg)

        threading.Thread(target=worker, daemon=True).start()

    def done(self, ok, msg):
        self.run_btn.configure(state="normal")
        self.pbar.configure(value=100 if ok else 0)
        if ok:
            self.status.configure(text="完成：" + msg)
            messagebox.showinfo("完成", "已輸出：\n" + msg)
        else:
            self.status.configure(text="失敗")
            messagebox.showerror("輸出失敗", msg)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
