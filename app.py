import sys, os, time, threading, subprocess
from pathlib import Path
import tkinter as tk
import customtkinter as ctk
from tkinter import filedialog, messagebox

from config import *
from douyin_worker import DouyinWorker
from fb_crawler import FBCrawler
from tiktok_crawler import TikTokCrawler

try:
    import groq_client as _gc
    _GROQ_AVAILABLE_APP = True
except ImportError:
    _GROQ_AVAILABLE_APP = False

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ── Giọng TTS — hiển thị label đẹp, map nội bộ sang key edge-tts ──
_VOICE_LABELS = ["Giọng nữ (HoaiMy)", "Giọng nam (NamMinh)"]
_VOICE_KEY    = {"Giọng nữ (HoaiMy)": "nu", "Giọng nam (NamMinh)": "nam"}

# ── Kích thước khung preview ─────────────────────────────────────
# Douyin/TikTok luôn là video dọc (9:16) — nếu ép vào canvas ngang cố
# định 336×189 thì ảnh nền BỊ MÉO và mọi % chiều cao (blur zone,
# MarginV scale) tính sai theo tỉ lệ khung sai, không phải tỉ lệ thật
# của video. Đây là nguyên nhân gốc khiến overlay preview (xanh/cam)
# không khớp vị trí thật trong video_FINAL.mp4.
# Giải pháp: cố định CHIỀU RỘNG khung, để CHIỀU CAO tự tính theo đúng
# aspect ratio video đang xử lý (mặc định 9:16 khi chưa có video thật).
#
# Fix 2026-07-15: 220px quá nhỏ để đọc được chữ Trung/vị trí sub khi
# chưa mở fullscreen — người dùng phải mở rộng mới canh được vùng
# blur/sub, mất tác dụng của preview inline. Tăng lên 420px (gần gấp
# đôi) — vẫn đủ nhỏ để nằm gọn trong cột trái cạnh danh sách URL, không
# đụng panel cài đặt bên phải (xem _dy_resize_canvas cho phần co giãn
# thêm theo độ rộng cửa sổ thật).
_PREV_BOX_W = 420      # độ rộng MẶC ĐỊNH khung preview (trước: 220)
_PREV_BOX_W_MIN = 260  # không co nhỏ hơn mức này dù cửa sổ hẹp
_PREV_BOX_W_MAX = 560  # không phình to hơn mức này dù cửa sổ rất rộng
_PREV_BOX_H_MAX = 760  # chiều cao tối đa cho phép (giới hạn UI, trước: 391)

def _prev_dims(video_w, video_h, box_w=None):
    """Tính (canvas_w, canvas_h) sao cho khớp đúng aspect ratio video,
    không vượt quá box_w (mặc định _PREV_BOX_W) × _PREV_BOX_H_MAX.
    box_w cho phép truyền độ rộng động (theo cửa sổ thật) — xem
    _dy_on_resize."""
    if box_w is None:
        box_w = _PREV_BOX_W
    video_w = max(1, video_w); video_h = max(1, video_h)
    ar = video_w / video_h
    w = box_w
    h = int(round(w / ar))
    if h > _PREV_BOX_H_MAX:
        h = _PREV_BOX_H_MAX
        w = int(round(h * ar))
    return max(40, w), max(40, h)

def _lbl(parent, row, text, size=11, color=SUBTEXT, bold=False):
    ctk.CTkLabel(parent, text=text,
                 font=("Segoe UI", size, "bold" if bold else "normal"),
                 text_color=color
                 ).grid(row=row, column=0, sticky="w", padx=12, pady=(6, 2))

def _sep(parent, row):
    ctk.CTkFrame(parent, fg_color="#333355", height=1
                 ).grid(row=row, column=0, sticky="ew", padx=12, pady=6)

def _number_field(parent, row, int_var, color=ACCENT2, min_v=1, max_v=300, width=90):
    """Ô nhập số nguyên (thay cho thanh trượt) — chỉ cho gõ chữ số.

    QUAN TRỌNG: đồng bộ vào int_var NGAY KHI GÕ (qua trace_add trên
    str_var), không chỉ chờ FocusOut/Enter — vì CTkButton không chắc kéo
    focus khỏi Entry khi bấm, nên nếu chỉ đồng bộ lúc rời ô, bấm nút ngay
    sau khi gõ số (chưa click ra ngoài/Enter) sẽ đọc nhầm giá trị cũ.
    Việc CLAMP (ép về khoảng hợp lệ, vd khi gõ "0" hoặc để trống) vẫn chỉ
    áp dụng lúc rời ô/Enter, để không cản trở lúc đang gõ dở (vd gõ "1"
    rồi "10" — không muốn bị ép về min ngay sau ký tự đầu).
    """
    wrap = ctk.CTkFrame(parent, fg_color="transparent")
    wrap.grid(row=row, column=0, sticky="ew", padx=12, pady=(0, 8))

    str_var = ctk.StringVar(value=str(int_var.get()))
    vcmd = (parent.register(lambda s: s == "" or s.isdigit()), "%P")
    entry = ctk.CTkEntry(wrap, textvariable=str_var, width=width, height=32,
                          fg_color=PANEL, text_color=TEXT, border_color=color,
                          border_width=1, font=("Segoe UI", 12, "bold"),
                          justify="center", validate="key", validatecommand=vcmd)
    entry.pack(side="left")

    def _sync_live(*_a):
        raw = str_var.get().strip()
        # "0"/rỗng/đang gõ dở → KHÔNG ghi đè int_var, giữ giá trị hợp lệ
        # gần nhất cho đến khi người dùng gõ xong 1 số > 0.
        if raw.isdigit() and int(raw) > 0:
            int_var.set(min(int(raw), max_v))

    def _clamp(event=None):
        raw = str_var.get().strip()
        try:
            n = int(raw) if raw else min_v
        except ValueError:
            n = min_v
        n = max(min_v, min(max_v, n))
        int_var.set(n); str_var.set(str(n))

    str_var.trace_add("write", _sync_live)
    entry.bind("<FocusOut>", _clamp)
    entry.bind("<Return>", _clamp)
    ctk.CTkLabel(wrap, text=f"(khoảng {min_v}–{max_v}, không nhập 0)",
                 font=("Segoe UI", 9), text_color=SUBTEXT
                 ).pack(side="left", padx=(8, 0))
    return entry

# ══════════════════════════════════════════════════════════════════
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("DouyinViet")
        self.geometry("1240x860"); self.minsize(1020, 720)
        self.configure(fg_color=BG)
        self._dy_urls = []; self._dy_worker = None; self._dy_t0 = None
        self._dy_downloaded_vid = None   # set by on_video_ready, read by Phase 2
        # ── Embedded player state ─────────────────────────────────
        self._player_cap      = None   # cv2.VideoCapture
        self._player_running  = False  # playback loop active
        self._player_paused   = True   # True = paused
        self._player_fps      = 30.0
        self._player_total    = 0      # total frame count
        self._player_pos      = 0      # current frame index
        self._player_thread   = None
        self._player_lock     = threading.Lock()  # bảo vệ _player_cap khỏi
        # race condition: player_loop (thread nền) và scrub/toggle/fullscreen
        # (GUI thread) trước đây cùng gọi .set()/.read() trên 1 VideoCapture
        # không thread-safe cùng lúc → giật/lag khi click nút play hoặc mở
        # rộng preview trong lúc đang phát.
        self._fs_win           = None  # Toplevel fullscreen đang mở (nếu có)
        self._player_video_path = None  # path video đang load trong player
        self._fb_videos = []; self._fb_vars = {}; self._fb_worker = None
        self._tt_videos = []; self._tt_vars = {}; self._tt_worker = None
        # Preview state
        self._preview_pil_orig = None     # PIL Image gốc (full-size)
        self._preview_photo_ref = None    # PhotoImage ref (chống GC)
        self._preview_video_w = 1920
        self._preview_video_h = 1080
        self._prev_box_w = _PREV_BOX_W  # độ rộng khung preview hiện tại,
        # cập nhật động theo độ rộng cửa sổ thật qua _dy_on_resize
        self._build()

    # ── Header ───────────────────────────────────────────────────
    def _build(self):
        hdr = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0, height=52)
        hdr.pack(fill="x")
        ctk.CTkLabel(hdr, text="🎬 DouyinViet",
                     font=("Segoe UI", 19, "bold"), text_color=ACCENT
                     ).pack(side="left", padx=18, pady=10)
        ctk.CTkLabel(hdr, text="Douyin → SRT  |  Facebook Crawler",
                     font=("Segoe UI", 12), text_color=SUBTEXT).pack(side="left")
        ctk.CTkButton(hdr, text="📂 Mở output", width=110, height=30,
                      fg_color=CARD, hover_color="#333344", font=("Segoe UI", 11),
                      command=self._open_out).pack(side="right", padx=14, pady=10)

        # Tabs — đặt trong frame để căn giữa đúng
        tab_wrap = ctk.CTkFrame(self, fg_color=BG)
        tab_wrap.pack(fill="both", expand=True, padx=12, pady=(4, 10))
        tab_wrap.rowconfigure(0, weight=1)
        tab_wrap.columnconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(tab_wrap, fg_color=PANEL,
                                   segmented_button_fg_color=CARD,
                                   segmented_button_selected_color=ACCENT,
                                   segmented_button_selected_hover_color="#5a4dd4",
                                   text_color=TEXT, anchor="nw")
        self.tabs.grid(row=0, column=0, sticky="nsew")
        self.tabs.add("🎬  Douyin → SRT")
        self.tabs.add("📘  Facebook Crawler")
        self.tabs.add("🎵  TikTok Crawler")
        self._build_douyin(self.tabs.tab("🎬  Douyin → SRT"))
        self._build_facebook(self.tabs.tab("📘  Facebook Crawler"))
        self._build_tiktok(self.tabs.tab("🎵  TikTok Crawler"))

    # ════════════════════════════════════════════════════════════
    # TAB 1: DOUYIN
    # ════════════════════════════════════════════════════════════
    def _build_douyin(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=0, minsize=270)
        parent.rowconfigure(0, weight=1)

        # ── Left panel (scrollable — đảm bảo mọi phần luôn xem được dù
        # window nhỏ hay preview/progress chiếm nhiều chỗ, log không bị
        # ép co lại đến mức không đọc nổi) ───────────────────────────
        L_outer = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10)
        L_outer.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=4)
        L_outer.columnconfigure(0, weight=1)
        L_outer.rowconfigure(0, weight=1)
        L = ctk.CTkScrollableFrame(L_outer, fg_color="transparent", corner_radius=0)
        L.grid(row=0, column=0, sticky="nsew")
        L.columnconfigure(0, weight=1)

        # Row 0: header
        ctk.CTkLabel(L, text="Danh sách URL Douyin",
                     font=("Segoe UI", 12, "bold"), text_color=TEXT
                     ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 4))

        # Row 1: URL input
        inp = ctk.CTkFrame(L, fg_color="transparent")
        inp.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 6))
        inp.columnconfigure(0, weight=1)
        self.dy_entry = ctk.CTkEntry(inp, placeholder_text="Dán URL Douyin...",
                                      fg_color=PANEL, border_color=ACCENT, border_width=1,
                                      text_color=TEXT, height=36, font=("Segoe UI", 12))
        self.dy_entry.grid(row=0, column=0, sticky="ew")
        self.dy_entry.bind("<Return>", lambda e: self._dy_add())
        br = ctk.CTkFrame(inp, fg_color="transparent")
        br.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        for txt, cmd, fg, hv in [
            ("+ Thêm", self._dy_add, ACCENT, "#5a4dd4"),
            ("📂 Import .txt", self._dy_import, CARD, "#333344"),
            ("🗑 Xóa", self._dy_clear, "#3a1a1a", "#5a2222"),
        ]:
            ctk.CTkButton(br, text=txt, command=cmd, fg_color=fg, hover_color=hv,
                          height=30, font=("Segoe UI", 11)).pack(side="left", padx=(0, 5))

        # Row 2: URL list
        self.dy_list = ctk.CTkTextbox(L, fg_color=PANEL, text_color=TEXT,
                                       font=("Consolas", 10), height=72,
                                       border_color="#333355", border_width=1)
        self.dy_list.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 6))
        self.dy_list.configure(state="disabled")

        # Row 3: Preview canvas
        prev_wrap = ctk.CTkFrame(L, fg_color=PANEL, corner_radius=8)
        prev_wrap.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 6))
        prev_wrap.columnconfigure(0, weight=1)

        prev_hdr = ctk.CTkFrame(prev_wrap, fg_color="transparent")
        prev_hdr.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 2))
        ctk.CTkLabel(prev_hdr, text="🎬 Preview  ",
                     font=("Segoe UI", 10, "bold"), text_color=ACCENT2
                     ).pack(side="left")
        ctk.CTkLabel(prev_hdr,
                     text="(vùng cam = blur sub Trung  |  vùng xanh = vị trí sub Việt)",
                     font=("Segoe UI", 9), text_color=SUBTEXT
                     ).pack(side="left")
        ctk.CTkButton(prev_hdr, text="🔄 Tải frame", width=82, height=24,
                      fg_color=CARD, hover_color="#333344", font=("Segoe UI", 9),
                      command=self._dy_load_preview_frame
                      ).pack(side="right", padx=(0, 2))
        # Mở video_original.mp4 bằng player mặc định của Windows — để user
        # scrub qua video thật, xác nhận vị trí sub Trung trước khi chỉnh %.
        self.dy_play_btn = ctk.CTkButton(
            prev_hdr, text="▶ Xem video", width=82, height=24,
            fg_color="#1a3a1a", hover_color="#2a5a2a",
            font=("Segoe UI", 9), state="disabled",
            command=self._dy_play_video)
        self.dy_play_btn.pack(side="right", padx=(0, 4))

        # Dòng trạng thái fetch — báo đang tải / lỗi (vd: cần cookies,
        # Playwright không bắt được URL...) ngay tại chỗ thay vì chỉ
        # nằm im trong Log khiến người dùng không để ý.
        self._dy_preview_status_lbl = ctk.CTkLabel(
            prev_wrap, text="", font=("Segoe UI", 9), text_color=WARN, anchor="w")
        self._dy_preview_status_lbl.grid(row=4, column=0, sticky="ew",
                                         padx=8, pady=(0, 4))

        # Canvas khởi tạo với tỉ lệ dọc mặc định 9:16 — đúng aspect ratio
        # thật của Douyin/TikTok thay vì 16:9 ngang sai trước đây. Sẽ
        # resize động (_dy_resize_canvas) khi load được frame thật.
        _init_w, _init_h = _prev_dims(self._preview_video_w, self._preview_video_h, self._prev_box_w)
        self.dy_canvas = tk.Canvas(prev_wrap, bg="#0f0f13",
                                    width=_init_w, height=_init_h,
                                    highlightthickness=1,
                                    highlightbackground="#333355")
        self.dy_canvas.grid(row=1, column=0, padx=6, pady=(0, 2))
        self._dy_draw_preview()  # vẽ placeholder

        # Co giãn khung preview theo độ rộng cửa sổ thật (trong khoảng
        # _PREV_BOX_W_MIN..MAX) — trước đây cố định 220px luôn, quá nhỏ
        # để đọc chữ Trung/canh vùng blur khi chưa mở fullscreen. Bind
        # trên prev_wrap (không phải canvas) vì canvas tự thay đổi kích
        # thước theo _prev_box_w, bind lên chính nó sẽ gây vòng lặp.
        self._prev_last_resize_w = None
        prev_wrap.bind("<Configure>", self._dy_on_prev_wrap_resize)

        # ── Player controls (row 2) — hiện sau khi có video ───────
        # Canvas đóng vai trò màn hình player — không dùng widget riêng,
        # OpenCV decode frame → PIL → PhotoImage → canvas.create_image().
        # Nút ⛶ mở rộng = Toplevel fullscreen với video_original.mp4.
        player_bar = ctk.CTkFrame(prev_wrap, fg_color="transparent")
        player_bar.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 4))
        player_bar.columnconfigure(1, weight=1)  # slider takes the middle

        self.dy_play_pause_btn = ctk.CTkButton(
            player_bar, text="▶", width=28, height=24,
            fg_color=CARD, hover_color="#333344",
            font=("Segoe UI", 11), state="disabled",
            command=self._dy_toggle_play)
        self.dy_play_pause_btn.grid(row=0, column=0, padx=(0, 4))

        self._player_pos_var = tk.DoubleVar(value=0.0)
        self.dy_scrub = ctk.CTkSlider(
            player_bar, variable=self._player_pos_var,
            from_=0, to=1, height=16,
            progress_color=ACCENT2, button_color=ACCENT2,
            button_hover_color=ACCENT2,
            command=self._dy_scrub_seek)
        self.dy_scrub.configure(state="disabled")
        self.dy_scrub.grid(row=0, column=1, sticky="ew", padx=4)

        self.dy_time_lbl = ctk.CTkLabel(
            player_bar, text="0:00/0:00", width=68,
            font=("Consolas", 9), text_color=SUBTEXT)
        self.dy_time_lbl.grid(row=0, column=2, padx=(4, 2))

        ctk.CTkButton(
            player_bar, text="⛶", width=26, height=24,
            fg_color=CARD, hover_color="#333344",
            font=("Segoe UI", 11),
            command=self._dy_fullscreen_player).grid(row=0, column=3, padx=(2, 0))

        # Row 3: status label (was row 2, shifted down for player bar)

        # Row 4: Progress bar + steps
        pg = ctk.CTkFrame(L, fg_color=PANEL, corner_radius=8)
        pg.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 6))
        pg.columnconfigure(0, weight=1)
        sr = ctk.CTkFrame(pg, fg_color="transparent")
        sr.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 3))
        self._dy_slbls = []
        for s in DouyinWorker.STEPS:
            l = ctk.CTkLabel(sr, text=s, font=("Segoe UI", 9), text_color=SUBTEXT)
            l.pack(side="left", expand=True); self._dy_slbls.append(l)
        self.dy_pbar = ctk.CTkProgressBar(pg, height=9, progress_color=ACCENT,
                                           fg_color="#2a2a3a")
        self.dy_pbar.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 3))
        self.dy_pbar.set(0)
        self.dy_plbl = ctk.CTkLabel(pg, text="Chờ...", font=("Segoe UI", 10),
                                     text_color=SUBTEXT)
        self.dy_plbl.grid(row=2, column=0, pady=(0, 6))

        # Row 5: Log textbox (expands)
        ctk.CTkLabel(L, text="Log", font=("Segoe UI", 11, "bold"), text_color=TEXT
                     ).grid(row=5, column=0, sticky="nw", padx=12, pady=(2, 1))
        self.dy_log = ctk.CTkTextbox(L, fg_color=PANEL, text_color=TEXT,
                                      font=("Consolas", 10), height=140,
                                      border_color="#333355", border_width=1)
        self.dy_log.grid(row=5, column=0, sticky="nsew", padx=12, pady=(20, 10))
        self.dy_log.configure(state="disabled")

        # ── Right panel (scrollable) ───────────────────────────
        R_outer = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10, width=270)
        R_outer.grid(row=0, column=1, sticky="nsew", pady=4)
        R_outer.columnconfigure(0, weight=1)
        R_outer.rowconfigure(0, weight=1)

        R = ctk.CTkScrollableFrame(R_outer, fg_color="transparent",
                                    corner_radius=0, width=255)
        R.grid(row=0, column=0, sticky="nsew")
        R.columnconfigure(0, weight=1)

        # ── Settings ──────────────────────────────────────────
        _lbl(R, 0, "⚙ Cài đặt", 13, TEXT, True)
        _lbl(R, 1, "📁 Output folder")
        or2 = ctk.CTkFrame(R, fg_color="transparent")
        or2.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))
        or2.columnconfigure(0, weight=1)
        self.dy_out = ctk.StringVar(value="./output")
        ctk.CTkEntry(or2, textvariable=self.dy_out, fg_color=PANEL, text_color=TEXT,
                     border_color=ACCENT, border_width=1, height=30,
                     font=("Segoe UI", 10)).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(or2, text="...", width=30, height=30, fg_color=PANEL,
                      hover_color="#333344",
                      command=lambda: self._pick(self.dy_out)
                      ).grid(row=0, column=1, padx=(3, 0))

        _lbl(R, 3, "🤖 Whisper model")
        self.dy_model = ctk.StringVar(value="base")
        ctk.CTkOptionMenu(R, variable=self.dy_model,
                          values=["tiny", "base", "small", "medium", "large"],
                          fg_color=PANEL, button_color=ACCENT,
                          dropdown_fg_color=PANEL, text_color=TEXT,
                          font=("Segoe UI", 11), height=32
                          ).grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 2))
        ctk.CTkLabel(R, text="tiny=nhanh  base=cân bằng  small=tốt",
                     font=("Segoe UI", 9), text_color=SUBTEXT
                     ).grid(row=5, column=0, sticky="w", padx=12, pady=(0, 4))

        self.dy_use_groq = ctk.BooleanVar(value=False)
        groq_status = "có sẵn ✅" if _GROQ_AVAILABLE_APP else "CHƯA có groq_client.py ⚠"
        ctk.CTkCheckBox(R, text="☁ Dùng Groq API (cloud, nhanh)",
                        variable=self.dy_use_groq, font=("Segoe UI", 10),
                        text_color=TEXT, border_color=ACCENT2, fg_color=ACCENT2
                        ).grid(row=6, column=0, sticky="w", padx=12, pady=(0, 2))
        ctk.CTkLabel(R, text=f"groq_client: {groq_status}",
                     font=("Segoe UI", 9), text_color=SUBTEXT, justify="left"
                     ).grid(row=7, column=0, sticky="w", padx=12, pady=(0, 4))

        _lbl(R, 8, "🍪 Cookies Douyin (nếu cần)")
        ck_dy = ctk.CTkFrame(R, fg_color="transparent")
        ck_dy.grid(row=9, column=0, sticky="ew", padx=12, pady=(0, 2))
        ck_dy.columnconfigure(0, weight=1)
        self.dy_cookies = ctk.StringVar(value="")
        ctk.CTkEntry(ck_dy, textvariable=self.dy_cookies,
                     placeholder_text="Để trống nếu video public",
                     fg_color=PANEL, text_color=TEXT,
                     border_color="#444466", border_width=1,
                     height=28, font=("Segoe UI", 10)
                     ).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(ck_dy, text="...", width=28, height=28,
                      fg_color=PANEL, hover_color="#333344",
                      command=self._dy_pick_cookies
                      ).grid(row=0, column=1, padx=(3, 0))

        _sep(R, 10)

        # ── Hậu kỳ ────────────────────────────────────────────
        ctk.CTkLabel(R, text="🎬 Xử lý hậu kỳ (tuỳ chọn)",
                     font=("Segoe UI", 11, "bold"), text_color=TEXT
                     ).grid(row=11, column=0, sticky="w", padx=12, pady=(4, 6))

        # --- Blur sub gốc ---
        self.dy_blur = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(R, text="Làm mờ sub gốc tiếng Trung (18% dưới)",
                        variable=self.dy_blur, font=("Segoe UI", 10),
                        text_color=TEXT, border_color=WARN, fg_color=WARN,
                        command=self._dy_toggle_blur
                        ).grid(row=12, column=0, sticky="w", padx=12, pady=(0, 2))

        # Hint label khi blur chưa bật
        self._blur_hint = ctk.CTkLabel(R,
            text="  ↳ Tick để cài vùng blur sub Trung",
            font=("Segoe UI", 8), text_color=SUBTEXT, anchor="w")
        self._blur_hint.grid(row=13, column=0, sticky="w", padx=12, pady=(0, 2))

        blur_ctrl = ctk.CTkFrame(R, fg_color=PANEL, corner_radius=6)
        blur_ctrl.columnconfigure(1, weight=1)

        self.dy_blur_top = ctk.DoubleVar(value=72.0)
        self.dy_blur_bot = ctk.DoubleVar(value=92.0)
        self._build_pct_row(blur_ctrl, 0, "Từ:", self.dy_blur_top, WARN)
        self._build_pct_row(blur_ctrl, 1, "Đến:", self.dy_blur_bot, WARN)
        ctk.CTkLabel(blur_ctrl,
                     text="(% chiều cao video tính từ trên xuống)",
                     font=("Segoe UI", 8), text_color=SUBTEXT
                     ).grid(row=2, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 4))

        # Lưu ref để toggle show/hide
        self._blur_ctrl_frame = blur_ctrl
        # Ẩn mặc định, chỉ hiện khi tick
        # (không grid lần đầu — _dy_toggle_blur sẽ quản lý)

        # Row 14 placeholder — burn sẽ tự grid sau blur_ctrl tùy trạng thái
        # --- Burn sub ---
        self.dy_burn = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(R, text="Burn sub tiếng Việt → _FINAL.mp4",
                        variable=self.dy_burn, font=("Segoe UI", 10),
                        text_color=TEXT, border_color=ACCENT, fg_color=ACCENT,
                        command=self._dy_toggle_burn
                        ).grid(row=14, column=0, sticky="w", padx=12, pady=(0, 2))

        # Hint label khi burn chưa bật
        self._burn_hint = ctk.CTkLabel(R,
            text="  ↳ Tick để cài font size và margin sub",
            font=("Segoe UI", 8), text_color=SUBTEXT, anchor="w")
        self._burn_hint.grid(row=15, column=0, sticky="w", padx=12, pady=(0, 2))

        sub_ctrl = ctk.CTkFrame(R, fg_color=PANEL, corner_radius=6)
        sub_ctrl.columnconfigure(1, weight=1)

        self.dy_font_size = ctk.DoubleVar(value=22)
        self.dy_margin_v  = ctk.DoubleVar(value=25)
        self._build_slider_row(sub_ctrl, 0, "FontSize:", self.dy_font_size,
                               ACCENT, from_=8, to_=80, unit="px", width_label=58)
        self._build_slider_row(sub_ctrl, 1, "MarginV:", self.dy_margin_v,
                               ACCENT, from_=0, to_=150, unit="px", width_label=58)
        ctk.CTkLabel(sub_ctrl, text="(MarginV: khoảng cách sub → đáy video, đơn vị px)",
                     font=("Segoe UI", 8), text_color=SUBTEXT
                     ).grid(row=2, column=0, columnspan=6, padx=6, pady=(0, 4), sticky="w")

        # Lưu ref để toggle show/hide
        self._sub_ctrl_frame = sub_ctrl

        # --- TTS ---
        self.dy_tts = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(R, text="Lồng tiếng TTS tiếng Việt (edge-tts)",
                        variable=self.dy_tts, font=("Segoe UI", 10),
                        text_color=TEXT, border_color=SUCCESS, fg_color=SUCCESS
                        ).grid(row=16, column=0, sticky="w", padx=12, pady=(0, 4))

        tts_opts = ctk.CTkFrame(R, fg_color=PANEL, corner_radius=6)
        tts_opts.grid(row=17, column=0, sticky="ew", padx=16, pady=(0, 8))
        tts_opts.columnconfigure(1, weight=1)

        ctk.CTkLabel(tts_opts, text="Giọng:", font=("Segoe UI", 9),
                     text_color=SUBTEXT).grid(row=0, column=0, padx=(6, 2), pady=(6, 2), sticky="w")
        self.dy_voice = ctk.StringVar(value=_VOICE_LABELS[0])
        # dynamic_resizing=False + width=160 để label đầy đủ không bị cắt giữa chừng
        ctk.CTkOptionMenu(tts_opts, variable=self.dy_voice,
                          values=_VOICE_LABELS, height=28,
                          width=160,
                          dynamic_resizing=False,
                          fg_color=CARD, button_color=ACCENT,
                          dropdown_fg_color=PANEL, text_color=TEXT,
                          dropdown_text_color=TEXT,
                          font=("Segoe UI", 10),
                          anchor="w",
                          ).grid(row=0, column=1, padx=(2, 6), pady=(6, 2), sticky="ew")

        ctk.CTkLabel(tts_opts, text="Âm gốc:", font=("Segoe UI", 9),
                     text_color=SUBTEXT).grid(row=1, column=0, padx=(6, 2), pady=(2, 6))
        self.dy_orig_vol = ctk.StringVar(value="0.15")
        vcmd2 = (R_outer.register(
            lambda s: s == "" or all(c in "0123456789." for c in s)), "%P")
        ctk.CTkEntry(tts_opts, textvariable=self.dy_orig_vol,
                     width=54, height=26, justify="center",
                     fg_color=CARD, text_color=TEXT,
                     border_color=SUBTEXT, border_width=1,
                     font=("Segoe UI", 10),
                     validate="key", validatecommand=vcmd2
                     ).grid(row=1, column=1, padx=(0, 4), pady=(2, 6), sticky="w")
        ctk.CTkLabel(tts_opts, text="(0.0 = tắt, 0.15 = giữ nhẹ, 1.0 = nguyên)",
                     font=("Segoe UI", 8), text_color=SUBTEXT
                     ).grid(row=1, column=2, columnspan=2, padx=(0, 6), pady=(2, 6),
                            sticky="w")

        _sep(R, 18)

        # Output info
        info = ctk.CTkFrame(R, fg_color=PANEL, corner_radius=8)
        info.grid(row=19, column=0, sticky="ew", padx=12, pady=(4, 10))
        info.columnconfigure(0, weight=1)
        for i, ln in enumerate(["📦 Output:", "• video_original.mp4",
                                  "• video_vi.srt  ← CapCut",
                                  "• video_segments.json",
                                  "• video_FINAL.mp4  ← nếu bật Burn"]):
            ctk.CTkLabel(info, text=ln, font=("Segoe UI", 10 if i > 0 else 11),
                         text_color=TEXT if i == 0 else SUBTEXT, justify="left"
                         ).grid(row=i, column=0, sticky="w", padx=10, pady=1)

        # ── Phase 1 / Phase 2 — ghim ngoài scroll area ────────────
        btn_frame = ctk.CTkFrame(R_outer, fg_color=CARD, corner_radius=0)
        btn_frame.grid(row=1, column=0, sticky="ew")
        btn_frame.columnconfigure(0, weight=1)

        # Phase 1: Tải video + whisper + dịch + SRT — KHÔNG burn/blur/TTS
        # Xong thì kích hoạt Phase 2 và show preview frame thật để user chỉnh
        self.dy_start_btn = ctk.CTkButton(
            btn_frame,
            text="⬇  Tải + SRT",
            height=40,
            font=("Segoe UI", 12, "bold"),
            fg_color=ACCENT, hover_color="#5a4dd4",
            command=self._dy_start)
        self.dy_start_btn.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 3))

        # Phase 2: Đọc slider hiện tại → áp dụng blur/TTS/burn lên file đã tải
        # Disabled cho tới khi Phase 1 hoàn tất ít nhất 1 video
        self.dy_apply_btn = ctk.CTkButton(
            btn_frame,
            text="🎬  Áp dụng & Xuất video",
            height=40,
            font=("Segoe UI", 12, "bold"),
            fg_color="#2a5a2a", hover_color="#3a7a3a",
            state="disabled",
            command=self._dy_apply)
        self.dy_apply_btn.grid(row=1, column=0, sticky="ew", padx=12, pady=(3, 3))

        self.dy_stop_btn = ctk.CTkButton(
            btn_frame,
            text="⏹  Dừng",
            height=28,
            font=("Segoe UI", 10),
            fg_color="#3a1a1a", hover_color="#5a2222",
            state="disabled",
            command=self._dy_stop)
        self.dy_stop_btn.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))

    # ════════════════════════════════════════════════════════════
    # TAB 2: FACEBOOK
    # ════════════════════════════════════════════════════════════
    def _build_facebook(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=0, minsize=270)
        parent.rowconfigure(0, weight=1)

        # ── Left: danh sách video ──────────────────────────────
        L = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10)
        L.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=4)
        L.columnconfigure(0, weight=1); L.rowconfigure(2, weight=1)

        tb = ctk.CTkFrame(L, fg_color="transparent")
        tb.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        self.fb_count = ctk.CTkLabel(tb, text="Chưa quét",
                                      font=("Segoe UI", 12, "bold"), text_color=TEXT)
        self.fb_count.pack(side="left")
        for txt, cmd in [("☐ Bỏ chọn", self._fb_deselect),
                          ("☑ Chọn tất", self._fb_select_all)]:
            ctk.CTkButton(tb, text=txt, width=88, height=26,
                          fg_color=CARD, hover_color="#333344",
                          font=("Segoe UI", 10), command=cmd
                          ).pack(side="right", padx=(3, 0))

        pg2 = ctk.CTkFrame(L, fg_color=PANEL, corner_radius=8)
        pg2.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 6))
        pg2.columnconfigure(0, weight=1)
        self.fb_pbar = ctk.CTkProgressBar(pg2, height=9,
                                           progress_color=ACCENT2, fg_color="#2a2a3a")
        self.fb_pbar.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 3))
        self.fb_pbar.set(0)
        self.fb_plbl = ctk.CTkLabel(pg2, text="Chờ...",
                                     font=("Segoe UI", 10), text_color=SUBTEXT)
        self.fb_plbl.grid(row=1, column=0, pady=(0, 6))

        # Scrollable video list
        self.fb_scroll = ctk.CTkScrollableFrame(L, fg_color=PANEL, corner_radius=8)
        self.fb_scroll.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 10))
        self.fb_scroll.columnconfigure(0, weight=1)
        self._fb_empty_lbl = ctk.CTkLabel(
            self.fb_scroll,
            text=("Nhập URL fanpage bên phải → bấm 🔍 Quét\n\n"
                  "Ví dụ:\nhttps://www.facebook.com/profile.php?id=xxx&sk=reels_tab\n"
                  "https://www.facebook.com/pagename/reels"),
            font=("Segoe UI", 12), text_color=SUBTEXT, justify="center")
        self._fb_empty_lbl.pack(expand=True, pady=40)

        # ── Right: settings ────────────────────────────────────
        R_outer = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10, width=270)
        R_outer.grid(row=0, column=1, sticky="nsew", pady=4)
        R_outer.columnconfigure(0, weight=1)
        R_outer.rowconfigure(0, weight=1)

        # Toàn bộ nội dung panel phải nằm trong khung cuộn được, để
        # log/nút bấm không bao giờ bị cắt mất phía dưới khi cửa sổ
        # thấp hoặc nội dung dài hơn chiều cao hiển thị.
        R = ctk.CTkScrollableFrame(R_outer, fg_color="transparent", corner_radius=0)
        R.grid(row=0, column=0, sticky="nsew")
        R.columnconfigure(0, weight=1)

        _lbl(R, 0, "📘 Facebook Crawler", 14, TEXT, True)
        _lbl(R, 1, "URL fanpage / reels_tab")
        self.fb_url = ctk.CTkEntry(R, placeholder_text="facebook.com/profile.php?id=…&sk=reels_tab",
                                    fg_color=PANEL, border_color=ACCENT2, border_width=1,
                                    text_color=TEXT, height=34, font=("Segoe UI", 10))
        self.fb_url.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))

        _lbl(R, 3, "🔒 File cookies.txt")
        ck = ctk.CTkFrame(R, fg_color="transparent")
        ck.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 2))
        ck.columnconfigure(0, weight=1)
        self.fb_cookies = ctk.StringVar(value="")
        ctk.CTkEntry(ck, textvariable=self.fb_cookies,
                     placeholder_text="Không bắt buộc",
                     fg_color=PANEL, text_color=TEXT, border_color="#444466",
                     border_width=1, height=28, font=("Segoe UI", 10)
                     ).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(ck, text="...", width=28, height=28,
                      fg_color=PANEL, hover_color="#333344",
                      command=self._fb_pick_cookies
                      ).grid(row=0, column=1, padx=(3, 0))
        ctk.CTkLabel(R, text="Lấy cookies: dùng extension\n'Get cookies.txt LOCALLY' trên Chrome",
                     font=("Segoe UI", 9), text_color=SUBTEXT, justify="left"
                     ).grid(row=5, column=0, sticky="w", padx=12, pady=(2, 8))

        _lbl(R, 6, "📄 Số reel tối đa")
        self.fb_limit = ctk.IntVar(value=50)
        _number_field(R, 7, self.fb_limit, color=ACCENT2, min_v=1, max_v=300)

        _sep(R, 8)
        self.fb_crawl_btn = ctk.CTkButton(R, text="🔍  Quét fanpage", height=40,
                                           font=("Segoe UI", 12, "bold"),
                                           fg_color=ACCENT2, hover_color="#2a8fb0",
                                           text_color="#000000",
                                           command=self._fb_crawl)
        self.fb_crawl_btn.grid(row=9, column=0, sticky="ew", padx=12, pady=(4, 4))

        _sep(R, 10)
        _lbl(R, 11, "📁 Thư mục lưu video")
        ov = ctk.CTkFrame(R, fg_color="transparent")
        ov.grid(row=12, column=0, sticky="ew", padx=12, pady=(0, 8))
        ov.columnconfigure(0, weight=1)
        self.fb_out = ctk.StringVar(value="./facebook")
        ctk.CTkEntry(ov, textvariable=self.fb_out, fg_color=PANEL, text_color=TEXT,
                     border_color=ACCENT, border_width=1, height=28,
                     font=("Segoe UI", 10)).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(ov, text="...", width=28, height=28, fg_color=PANEL,
                      hover_color="#333344",
                      command=lambda: self._pick(self.fb_out)
                      ).grid(row=0, column=1, padx=(3, 0))

        self.fb_dl_btn = ctk.CTkButton(R, text="⬇  Tải video đã chọn", height=40,
                                        font=("Segoe UI", 12, "bold"),
                                        fg_color=ACCENT, hover_color="#5a4dd4",
                                        state="disabled", command=self._fb_download)
        self.fb_dl_btn.grid(row=13, column=0, sticky="ew", padx=12, pady=(0, 4))
        self.fb_stop_btn = ctk.CTkButton(R, text="⏹  Dừng", height=30,
                                          font=("Segoe UI", 11),
                                          fg_color="#3a1a1a", hover_color="#5a2222",
                                          state="disabled", command=self._fb_stop)
        self.fb_stop_btn.grid(row=14, column=0, sticky="ew", padx=12, pady=(0, 8))

        _sep(R, 15)
        ctk.CTkLabel(R, text="Log", font=("Segoe UI", 10, "bold"),
                     text_color=SUBTEXT).grid(row=16, column=0, sticky="w", padx=12)
        self.fb_log = ctk.CTkTextbox(R, fg_color=PANEL, text_color=TEXT,
                                      font=("Consolas", 9), height=160,
                                      border_color="#333355", border_width=1)
        self.fb_log.grid(row=17, column=0, sticky="ew", padx=12, pady=(2, 16))
        self.fb_log.configure(state="disabled")

    # ════════════════════════════════════════════════════════════
    # TAB 3: TIKTOK
    # ════════════════════════════════════════════════════════════
    def _build_tiktok(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=0, minsize=270)
        parent.rowconfigure(0, weight=1)

        # ── Left: danh sách video ──────────────────────────────
        L = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10)
        L.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=4)
        L.columnconfigure(0, weight=1); L.rowconfigure(2, weight=1)

        tb = ctk.CTkFrame(L, fg_color="transparent")
        tb.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        self.tt_count = ctk.CTkLabel(tb, text="Chưa quét",
                                      font=("Segoe UI", 12, "bold"), text_color=TEXT)
        self.tt_count.pack(side="left")
        for txt, cmd in [("☐ Bỏ chọn", self._tt_deselect),
                          ("☑ Chọn tất", self._tt_select_all)]:
            ctk.CTkButton(tb, text=txt, width=88, height=26,
                          fg_color=CARD, hover_color="#333344",
                          font=("Segoe UI", 10), command=cmd
                          ).pack(side="right", padx=(3, 0))

        pg3 = ctk.CTkFrame(L, fg_color=PANEL, corner_radius=8)
        pg3.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 6))
        pg3.columnconfigure(0, weight=1)
        self.tt_pbar = ctk.CTkProgressBar(pg3, height=9,
                                           progress_color=SUCCESS, fg_color="#2a2a3a")
        self.tt_pbar.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 3))
        self.tt_pbar.set(0)
        self.tt_plbl = ctk.CTkLabel(pg3, text="Chờ...",
                                     font=("Segoe UI", 10), text_color=SUBTEXT)
        self.tt_plbl.grid(row=1, column=0, pady=(0, 6))

        # Scrollable video list
        self.tt_scroll = ctk.CTkScrollableFrame(L, fg_color=PANEL, corner_radius=8)
        self.tt_scroll.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 10))
        self.tt_scroll.columnconfigure(0, weight=1)
        self._tt_empty_lbl = ctk.CTkLabel(
            self.tt_scroll,
            text=("Nhập URL kênh TikTok bên phải → bấm 🔍 Quét\n\n"
                  "Ví dụ:\nhttps://www.tiktok.com/@tenuser"),
            font=("Segoe UI", 12), text_color=SUBTEXT, justify="center")
        self._tt_empty_lbl.pack(expand=True, pady=40)

        # ── Right: settings ────────────────────────────────────
        R_outer = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10, width=270)
        R_outer.grid(row=0, column=1, sticky="nsew", pady=4)
        R_outer.columnconfigure(0, weight=1)
        R_outer.rowconfigure(0, weight=1)

        R = ctk.CTkScrollableFrame(R_outer, fg_color="transparent", corner_radius=0)
        R.grid(row=0, column=0, sticky="nsew")
        R.columnconfigure(0, weight=1)

        _lbl(R, 0, "🎵 TikTok Crawler", 14, TEXT, True)
        _lbl(R, 1, "URL kênh TikTok")
        self.tt_url = ctk.CTkEntry(R, placeholder_text="tiktok.com/@tenuser",
                                    fg_color=PANEL, border_color=SUCCESS, border_width=1,
                                    text_color=TEXT, height=34, font=("Segoe UI", 10))
        self.tt_url.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))

        _lbl(R, 3, "🔒 File cookies.txt")
        ck3 = ctk.CTkFrame(R, fg_color="transparent")
        ck3.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 2))
        ck3.columnconfigure(0, weight=1)
        self.tt_cookies = ctk.StringVar(value="")
        ctk.CTkEntry(ck3, textvariable=self.tt_cookies,
                     placeholder_text="Không bắt buộc (kênh public)",
                     fg_color=PANEL, text_color=TEXT, border_color="#444466",
                     border_width=1, height=28, font=("Segoe UI", 10)
                     ).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(ck3, text="...", width=28, height=28,
                      fg_color=PANEL, hover_color="#333344",
                      command=self._tt_pick_cookies
                      ).grid(row=0, column=1, padx=(3, 0))
        ctk.CTkLabel(R, text="Chỉ cần nếu kênh private/giới hạn tuổi",
                     font=("Segoe UI", 9), text_color=SUBTEXT, justify="left"
                     ).grid(row=5, column=0, sticky="w", padx=12, pady=(2, 8))

        _lbl(R, 6, "📄 Số video tối đa")
        self.tt_limit = ctk.IntVar(value=50)
        _number_field(R, 7, self.tt_limit, color=SUCCESS, min_v=1, max_v=300)

        _sep(R, 8)
        self.tt_crawl_btn = ctk.CTkButton(R, text="🔍  Quét kênh", height=40,
                                           font=("Segoe UI", 12, "bold"),
                                           fg_color=SUCCESS, hover_color="#3d8b40",
                                           command=self._tt_crawl)
        self.tt_crawl_btn.grid(row=9, column=0, sticky="ew", padx=12, pady=(4, 4))

        _sep(R, 10)
        _lbl(R, 11, "📁 Thư mục lưu video")
        ov3 = ctk.CTkFrame(R, fg_color="transparent")
        ov3.grid(row=12, column=0, sticky="ew", padx=12, pady=(0, 8))
        ov3.columnconfigure(0, weight=1)
        self.tt_out = ctk.StringVar(value="./tiktok")
        ctk.CTkEntry(ov3, textvariable=self.tt_out, fg_color=PANEL, text_color=TEXT,
                     border_color=ACCENT, border_width=1, height=28,
                     font=("Segoe UI", 10)).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(ov3, text="...", width=28, height=28, fg_color=PANEL,
                      hover_color="#333344",
                      command=lambda: self._pick(self.tt_out)
                      ).grid(row=0, column=1, padx=(3, 0))

        self.tt_dl_btn = ctk.CTkButton(R, text="⬇  Tải video đã chọn", height=40,
                                        font=("Segoe UI", 12, "bold"),
                                        fg_color=ACCENT, hover_color="#5a4dd4",
                                        state="disabled", command=self._tt_download)
        self.tt_dl_btn.grid(row=13, column=0, sticky="ew", padx=12, pady=(0, 4))
        self.tt_stop_btn = ctk.CTkButton(R, text="⏹  Dừng", height=30,
                                          font=("Segoe UI", 11),
                                          fg_color="#3a1a1a", hover_color="#5a2222",
                                          state="disabled", command=self._tt_stop)
        self.tt_stop_btn.grid(row=14, column=0, sticky="ew", padx=12, pady=(0, 8))

        _sep(R, 15)
        ctk.CTkLabel(R, text="Log", font=("Segoe UI", 10, "bold"),
                     text_color=SUBTEXT).grid(row=16, column=0, sticky="w", padx=12)
        self.tt_log = ctk.CTkTextbox(R, fg_color=PANEL, text_color=TEXT,
                                      font=("Consolas", 9), height=160,
                                      border_color="#333355", border_width=1)
        self.tt_log.grid(row=17, column=0, sticky="ew", padx=12, pady=(2, 16))
        self.tt_log.configure(state="disabled")

    # ════════════════════════════════════════════════════════════
    # HELPERS
    # ════════════════════════════════════════════════════════════
    def _pick(self, var):
        d = filedialog.askdirectory()
        if d: var.set(d)

    def _open_out(self):
        tab = self.tabs.get()
        if "Facebook" in tab:
            p = Path(self.fb_out.get())
        elif "TikTok" in tab:
            p = Path(self.tt_out.get())
        else:
            p = Path(self.dy_out.get())
        p.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32": os.startfile(str(p))
        else: subprocess.Popen(["xdg-open", str(p)])

    # ── Douyin actions ────────────────────────────────────────
    # ── Slider + Entry cho tham số % ──────────────────────────
    def _build_slider_row(self, parent, row, label, dvar, color,
                          from_=0, to_=100, unit="%", width_label=32,
                          on_change=None):
        """Hàng: [Label] [Slider from_-to_] [Entry][unit] — slider và entry
        đồng bộ nhau, thay đổi nào cũng gọi on_change (mặc định: cập nhật
        preview overlay). Tổng quát hoá từ _build_pct_row gốc để dùng được
        cho cả % (blur) lẫn px (FontSize/MarginV) — chỉ khác range/đơn vị.
        """
        on_change = on_change or self._dy_draw_preview
        frm = ctk.CTkFrame(parent, fg_color="transparent")
        frm.grid(row=row, column=0, columnspan=6, sticky="ew", padx=4, pady=2)
        frm.columnconfigure(1, weight=1)

        ctk.CTkLabel(frm, text=label, width=width_label, font=("Segoe UI", 9),
                     text_color=SUBTEXT, anchor="e").grid(row=0, column=0, padx=(4, 2))

        sl = ctk.CTkSlider(frm, variable=dvar, from_=from_, to=to_,
                           progress_color=color, button_color=color,
                           button_hover_color=color, height=16,
                           command=lambda v: self.after(0, on_change))
        sl.grid(row=0, column=1, sticky="ew", padx=4)

        str_v = ctk.StringVar(value=str(int(round(dvar.get()))))
        _updating = [False]

        def _dvar_to_str(*_):
            if _updating[0]: return
            new = str(int(round(dvar.get())))
            if str_v.get() != new:
                str_v.set(new)

        def _str_to_dvar(*_):
            if _updating[0]: return
            try:
                v = max(from_, min(to_, float(str_v.get())))
                _updating[0] = True
                dvar.set(v)
                _updating[0] = False
                self.after(0, on_change)
            except ValueError:
                pass

        dvar.trace_add("write", _dvar_to_str)

        ent = ctk.CTkEntry(frm, textvariable=str_v, width=38, height=24,
                           font=("Segoe UI", 9), fg_color=CARD, text_color=TEXT,
                           border_color="#444466", border_width=1, justify="center")
        ent.grid(row=0, column=2, padx=2)

        def _commit(*_):
            try:
                v = max(from_, min(to_, float(str_v.get())))
                _updating[0] = True
                dvar.set(v)
                str_v.set(str(int(round(v))))
                _updating[0] = False
                self.after(0, on_change)
            except ValueError:
                str_v.set(str(int(round(dvar.get()))))

        ent.bind("<FocusOut>", _commit)
        ent.bind("<Return>", _commit)

        ctk.CTkLabel(frm, text=unit, font=("Segoe UI", 9),
                     text_color=SUBTEXT).grid(row=0, column=3, padx=(1, 4))

    def _build_pct_row(self, parent, row, label, dvar, color):
        """Wrapper giữ nguyên chữ ký cũ (range 0-100, đơn vị %) — chỗ gọi
        cũ (blur Từ/Đến) không cần đổi gì."""
        self._build_slider_row(parent, row, label, dvar, color,
                               from_=0, to_=100, unit="%")

    # ── Preview canvas helpers ─────────────────────────────────
    def _dy_on_prev_wrap_resize(self, event):
        """Bắt sự kiện Configure của prev_wrap (chạy khi cửa sổ đổi kích
        thước) để co giãn khung preview theo không gian thật. Trước đây
        khung cố định 220px — quá nhỏ để đọc chữ Trung/canh vùng blur khi
        chưa mở fullscreen (2026-07-15)."""
        # event.width là độ rộng của prev_wrap; trừ padding 2 bên (mỗi
        # bên ~6-8px như đang dùng ở canvas.grid) để không tràn khung.
        new_w = max(_PREV_BOX_W_MIN, min(_PREV_BOX_W_MAX, event.width - 20))
        # Chỉ redraw khi thay đổi đủ lớn (>8px) — tránh vẽ lại liên tục
        # với từng sự kiện Configure nhỏ lẻ (kéo cửa sổ tạo rất nhiều
        # sự kiện liên tiếp), giữ UI mượt khi resize.
        if (self._prev_last_resize_w is not None
                and abs(new_w - self._prev_last_resize_w) < 8):
            return
        self._prev_last_resize_w = new_w
        self._prev_box_w = new_w
        pw, ph = _prev_dims(self._preview_video_w, self._preview_video_h, new_w)
        self.dy_canvas.configure(width=pw, height=ph)
        # Nếu đang có video/frame thật đã load, decode lại theo kích
        # thước mới trên thread nền (không đồng bộ) — kéo cửa sổ liên
        # tục có thể bắn nhiều sự kiện Configure dồn dập, decode đồng bộ
        # ngay trên GUI thread ở đây dễ lặp lại đúng lỗi "No Response"
        # đã fix trước đó.
        if self._player_cap is not None:
            self._resize_req_seq = getattr(self, "_resize_req_seq", 0) + 1
            req_id = self._resize_req_seq
            frame_idx = self._player_pos

            def _bg():
                result = self._dy_decode_frame(frame_idx, pw, ph, seek=True)
                if result is None or req_id != self._resize_req_seq:
                    return
                idx, img = result
                self.after(0, self._dy_apply_decoded_frame, idx, img)
            threading.Thread(target=_bg, daemon=True).start()
        else:
            self._dy_draw_preview()

    def _dy_overlay_layer(self, pw, ph):
        """Tạo layer RGBA (cam=blur, xanh=sub) theo đúng kích thước
        (pw, ph) hiện tại — TÁCH RIÊNG khỏi _dy_draw_preview để dùng
        chung được cho cả frame tĩnh (pause) LẪN frame đang phát.

        Fix 'chớp chớp khi bật blur+sub' (2026-07-15): trước đây overlay
        chỉ được vẽ trong _dy_draw_preview, còn lúc video đang PHÁT thì
        _dy_apply_decoded_frame/_dy_player_show_frame vẽ thẳng frame gốc
        KHÔNG có overlay lên canvas ở tốc độ ~30fps. Hai đường vẽ giành
        nhau cùng 1 canvas → overlay xuất hiện rồi bị frame sạch xoá
        liên tục, nhìn như chớp và không định vị được vùng blur/sub.
        Giải pháp: mọi nơi vẽ frame lên canvas đều phải composite qua
        hàm này trước khi hiển thị, kể cả trong lúc phát.

        Fix lag/giật khi phát+fullscreen (2026-07-15 lần 2): vẽ overlay
        bằng PIL ImageDraw (rectangle+text) TỐN CPU đáng kể nếu tính lại
        mỗi frame ở ~30fps trong lúc phát — trong khi giá trị blur/sub
        hầu như KHÔNG đổi giữa các frame liên tiếp (chỉ đổi khi người
        dùng kéo slider). Cache kết quả theo key gồm mọi tham số ảnh
        hưởng tới hình dạng overlay; chỉ vẽ lại khi key đổi."""
        key = (
            pw, ph,
            self.dy_blur.get(), round(self.dy_blur_top.get(), 1), round(self.dy_blur_bot.get(), 1),
            self.dy_burn.get(), round(self.dy_margin_v.get(), 1), round(self.dy_font_size.get(), 1),
            self._preview_video_h,
        )
        cache = getattr(self, "_overlay_cache", None)
        if cache is not None and cache[0] == key:
            return cache[1]

        from PIL import Image, ImageDraw
        ov = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
        if not hasattr(self, 'dy_blur'):
            return ov
        d = ImageDraw.Draw(ov)

        # ── Overlay blur zone (cam) ───────────────────────────
        if self.dy_blur.get():
            tp = max(0.0, min(0.99, self.dy_blur_top.get() / 100))
            bp = max(tp + 0.005, min(1.0, self.dy_blur_bot.get() / 100))
            y1 = int(ph * tp)
            y2 = int(ph * bp)
            d.rectangle([0, y1, pw, y2], fill=(255, 140, 0, 90))
            d.rectangle([0, y1, pw - 1, y2 - 1],
                        outline=(255, 160, 30, 220), width=2)
            d.rectangle([2, y1 + 2, 2 + 140, y1 + 14], fill=(0, 0, 0, 140))
            d.text((4, y1 + 3),
                   f"Blur: {int(round(self.dy_blur_top.get()))}% → {int(round(self.dy_blur_bot.get()))}%",
                   fill=(255, 200, 60, 255))

        # ── Overlay sub position (xanh lá) ───────────────────
        if self.dy_burn.get():
            mv = max(0, self.dy_margin_v.get())
            fs = max(8, self.dy_font_size.get())
            vid_h = max(1, self._preview_video_h)
            scale = ph / vid_h
            mv_scaled = max(2, round(mv * scale))
            fs_scaled = max(6, round(fs * scale * 1.1))
            y_bot = ph - mv_scaled
            y_top = max(0, y_bot - fs_scaled)
            d.rectangle([10, y_top, pw - 10, y_bot], fill=(0, 200, 60, 70))
            d.rectangle([10, y_top, pw - 11, y_bot - 1],
                        outline=(50, 230, 80, 220), width=1)
            lbl = f"MarginV={mv:.0f}px Size={fs:.0f}px"
            lbl_w = min(pw - 20, len(lbl) * 5 + 4)
            d.rectangle([10, y_top + 1, 10 + lbl_w, y_top + 13], fill=(0, 0, 0, 140))
            d.text((12, y_top + 2), lbl, fill=(100, 255, 120, 255))
        self._overlay_cache = (key, ov)
        return ov

    def _dy_draw_preview(self):
        """Vẽ lại overlay lên canvas preview (chạy trên GUI thread).

        Fix 'preview nhảy về thumbnail khi kéo blur/sub' (2026-07-15):
        hàm này trước đây LUÔN vẽ từ self._preview_pil_orig (ảnh tĩnh lấy
        1 lần lúc đầu, ví dụ frame đầu video) — khi player đã load video
        và người dùng đang xem/dừng ở 1 frame khác rồi kéo slider blur/
        sub, preview bị vẽ đè về ảnh tĩnh đó thay vì frame đang xem, gây
        cảm giác "nhảy về thumbnail" và không canh được đúng điểm. Giờ
        nếu player đã có video (_player_cap tồn tại), việc kéo slider sẽ
        vẽ lại đúng FRAME HIỆN TẠI của player (_player_pos) kèm overlay,
        không đụng tới _preview_pil_orig nữa."""
        # Guard: các vars này được khai báo ở phần dưới _build_douyin,
        # nhưng hàm này được gọi sớm hơn (lúc vẽ placeholder canvas).
        if not hasattr(self, 'dy_blur'):
            return

        if self._player_cap is not None:
            # Player đã load video — vẽ lại frame hiện tại (không seek,
            # không đổi vị trí phát) thay vì ảnh thumbnail tĩnh.
            if self._player_running and not self._player_paused:
                # Đang PHÁT: không tự seek ở đây — _dy_player_loop (thread
                # nền) đang liên tục .read() trên cùng _player_cap; gọi
                # thêm .set() đồng bộ trên GUI thread ở đây sẽ tranh chấp
                # VideoCapture với thread đó → giật/lag (đặc biệt rõ khi
                # mở fullscreen). Cứ để player_loop tự vẽ overlay mới ở
                # frame kế tiếp (nó cũng dùng _dy_overlay_layer, luôn đọc
                # giá trị slider mới nhất).
                return
            self._dy_player_show_frame(self._player_pos)
            return

        # Kích thước khung TÍNH THEO ĐÚNG TỈ LỆ video thật — khử méo
        # hình hoàn toàn so với canvas cố định 16:9 trước đây (vốn ép
        # video dọc 9:16 của Douyin vào khung ngang, làm sai lệch mọi %
        # và mọi quy đổi px→preview).
        pw, ph = _prev_dims(self._preview_video_w, self._preview_video_h, self._prev_box_w)
        # Resize canvas widget nếu tỉ lệ video vừa đổi (lần đầu load frame
        # thật, hoặc đổi sang video khác có tỉ lệ khác)
        if (self.dy_canvas.winfo_reqwidth(), self.dy_canvas.winfo_reqheight()) != (pw, ph):
            self.dy_canvas.configure(width=pw, height=ph)

        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            # PIL chưa cài — vẽ placeholder text thuần bằng tkinter
            self.dy_canvas.delete("all")
            self.dy_canvas.create_text(pw // 2, ph // 2,
                                        text="pip install Pillow\nđể xem preview",
                                        fill="#666688", font=("Segoe UI", 10),
                                        justify="center")
            return

        # ── Tạo ảnh nền ──────────────────────────────────────
        if self._preview_pil_orig is not None:
            bg = self._preview_pil_orig.copy().resize(
                (pw, ph), Image.LANCZOS).convert("RGBA")
        else:
            bg = Image.new("RGBA", (pw, ph), (20, 20, 35, 255))
            g = ImageDraw.Draw(bg)
            for x in range(0, pw, 24):
                g.line([(x, 0), (x, ph)], fill=(35, 35, 55, 255))
            for y in range(0, ph, 24):
                g.line([(0, y), (pw, y)], fill=(35, 35, 55, 255))
            g.text((max(4, pw // 2 - 72), ph // 2 - 10),
                   "Dán link rồi bấm 📁 Tải frame\nhoặc chờ tải xong để xem preview",
                   fill=(90, 90, 130, 255))

        ov = self._dy_overlay_layer(pw, ph)

        # ── Composite và hiển thị ─────────────────────────────
        final = Image.alpha_composite(bg, ov).convert("RGB")
        from PIL import ImageTk
        photo = ImageTk.PhotoImage(final)
        self.dy_canvas.delete("all")
        self.dy_canvas.create_image(0, 0, anchor="nw", image=photo)
        self._preview_photo_ref = photo   # giữ ref, chống GC

    def _dy_load_preview_frame(self):
        """Ưu tiên lấy lại frame từ link Douyin đầu tiên trong danh sách
        (nếu có) — đúng nút bấm cho trường hợp tự động fetch lỗi lần đầu
        và người dùng muốn thử lại. Nếu danh sách rỗng, cho chọn file
        video local như trước."""
        if self._dy_urls:
            self._dy_auto_fetch_preview(self._dy_urls[0])
            return
        p = filedialog.askopenfilename(
            title="Chọn video để lấy frame preview",
            filetypes=[("Video", "*.mp4 *.webm *.mkv *.mov *.avi"),
                       ("All", "*.*")])
        if p:
            threading.Thread(target=self._dy_extract_frame,
                             args=(p,), daemon=True).start()

    def _dy_extract_frame(self, video_path):
        """Extract frame thứ 2 bằng ffmpeg, lưu vào self._preview_pil_orig."""
        import tempfile, os
        ok = False
        err = ""
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp.close()
            r = subprocess.run([
                "ffmpeg", "-y", "-ss", "00:00:01",
                "-i", str(video_path),
                "-vframes", "1",
                "-q:v", "2", tmp.name
            ], capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                from PIL import Image
                img = Image.open(tmp.name).convert("RGB")
                self._preview_pil_orig = img
                self._preview_video_w, self._preview_video_h = img.size
                ok = True
            else:
                err = (r.stderr or "")[-200:].strip() or "ffmpeg lỗi không rõ"
        except Exception as e:
            err = str(e)[:200]
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
        self.after(0, self._dy_draw_preview)
        if ok:
            self.after(0, lambda: self._dy_preview_status(""))
        else:
            self.after(0, lambda: self._dy_preview_status(
                f"⚠ Không lấy được frame ({err[:60]})"))

    def _dy_pick_cookies(self):
        p = filedialog.askopenfilename(
            title="Chọn file cookies Douyin",
            filetypes=[("Cookies", "*.txt"), ("All files", "*.*")])
        if p: self.dy_cookies.set(p)

    def _dy_toggle_blur(self):
        """Hiện/ẩn blur_ctrl khi tick/bỏ tick checkbox blur."""
        if self.dy_blur.get():
            self._blur_hint.grid_remove()
            self._blur_ctrl_frame.grid(row=13, column=0, sticky="ew",
                                       padx=16, pady=(0, 8))
        else:
            self._blur_ctrl_frame.grid_remove()
            self._blur_hint.grid()
        self._dy_draw_preview()

    def _dy_toggle_burn(self):
        """Hiện/ẩn sub_ctrl khi tick/bỏ tick checkbox burn."""
        if self.dy_burn.get():
            self._burn_hint.grid_remove()
            self._sub_ctrl_frame.grid(row=15, column=0, sticky="ew",
                                      padx=16, pady=(0, 8))
        else:
            self._sub_ctrl_frame.grid_remove()
            self._burn_hint.grid()
        self._dy_draw_preview()

    def _dy_add(self):
        u = self.dy_entry.get().strip()
        if u and u not in self._dy_urls:
            was_empty = len(self._dy_urls) == 0
            self._dy_urls.append(u); self._dy_refresh()
            # Tự động lấy frame thật từ link Douyin đầu tiên — đúng yêu
            # cầu "điền link vào là hiện video luôn" thay vì phải bấm
            # 📁 Tải frame rồi chọn file local riêng.
            if was_empty and self._preview_pil_orig is None:
                self._dy_auto_fetch_preview(u)
        self.dy_entry.delete(0, "end")

    def _dy_auto_fetch_preview(self, douyin_url):
        """Lấy frame thật từ link Douyin chạy nền — không block UI.
        Dùng Playwright bắt URL stream thật rồi rút 1 frame bằng ffmpeg,
        không tải nguyên file video chỉ để xem trước vị trí blur/sub."""
        self._dy_preview_status("⏳ Đang lấy frame preview từ link...")
        threading.Thread(target=self._dy_auto_fetch_preview_worker,
                         args=(douyin_url,), daemon=True).start()

    def _dy_auto_fetch_preview_worker(self, douyin_url):
        import tempfile, os
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.close()
        try:
            w = DouyinWorker(log=lambda *a, **k: None,
                             done=lambda: None,
                             progress=lambda *a, **k: None,
                             step=lambda *a, **k: None)
            ok, err = w.get_preview_frame(douyin_url, tmp.name)
            if ok:
                from PIL import Image
                img = Image.open(tmp.name).convert("RGB")
                self._preview_pil_orig = img
                self._preview_video_w, self._preview_video_h = img.size
                self.after(0, self._dy_draw_preview)
                self.after(0, lambda: self._dy_preview_status(""))
            else:
                self.after(0, lambda: self._dy_preview_status(
                    f"⚠ Không lấy được frame tự động ({err[:60]}) — "
                    "bấm 📁 Tải frame để chọn file local"))
        except Exception as e:
            self.after(0, lambda: self._dy_preview_status(
                f"⚠ Lỗi lấy frame: {str(e)[:60]}"))
        finally:
            try: os.unlink(tmp.name)
            except Exception: pass

    def _dy_preview_status(self, text):
        """Hiện trạng thái fetch frame ngay trong khung preview header."""
        if hasattr(self, '_dy_preview_status_lbl'):
            self._dy_preview_status_lbl.configure(text=text)

    def _dy_import(self):
        p = filedialog.askopenfilename(filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if not p: return
        for ln in open(p, encoding="utf-8"):
            u = ln.strip()
            if u and not u.startswith("#") and u not in self._dy_urls:
                self._dy_urls.append(u)
        self._dy_refresh()

    def _dy_clear(self): self._dy_urls.clear(); self._dy_refresh()

    def _dy_refresh(self):
        self.dy_list.configure(state="normal")
        self.dy_list.delete("1.0", "end")
        for i, u in enumerate(self._dy_urls, 1):
            self.dy_list.insert("end", f"{i}. {u}\n")
        self.dy_list.configure(state="disabled")

    def _dy_log_fn(self, msg, col=None):
        self.dy_log.configure(state="normal")
        self.dy_log.insert("end", msg + "\n"); self.dy_log.see("end")
        self.dy_log.configure(state="disabled")

    def _dy_prog_fn(self, v, lbl):
        self.dy_pbar.set(v); pct = int(v * 100)
        eta = ""
        if self._dy_t0 and v > 0.03:
            el = time.time() - self._dy_t0
            if v < 0.999:
                s = int(el / v * (1 - v)); m, s = divmod(s, 60)
                eta = f"  còn ~{m}p{s:02d}s"
            em, es = divmod(int(el), 60); eta += f"  |  đã {em}p{es:02d}s"
        self.dy_plbl.configure(text=f"{pct}%  |  {lbl}{eta}")

    def _dy_step_fn(self, idx):
        for i, l in enumerate(self._dy_slbls):
            l.configure(text_color=SUCCESS if i < idx else (ACCENT2 if i == idx else SUBTEXT))

    def _dy_start(self):
        """Phase 1: Tải video + Whisper + Dịch + SRT.
        Không chạy blur/TTS/burn — user chỉnh slider trong khi xem
        preview rồi mới bấm 🎬 Áp dụng & Xuất video."""
        if not self._dy_urls:
            messagebox.showwarning("Chưa có URL", "Thêm ít nhất 1 URL Douyin!"); return
        self.dy_start_btn.configure(state="disabled")
        self.dy_apply_btn.configure(state="disabled")
        self.dy_stop_btn.configure(state="normal")
        self.dy_log.configure(state="normal"); self.dy_log.delete("1.0", "end")
        self.dy_log.configure(state="disabled")
        self.dy_pbar.set(0); self._dy_step_fn(-1)
        self._dy_t0 = time.time()
        self._dy_downloaded_vid = None   # reset trước mỗi Phase 1 mới

        def _on_ready(p):
            self._dy_downloaded_vid = p   # ghi nhớ để Phase 2 dùng
            self.after(0, self._dy_on_video_ready, p)

        w = DouyinWorker(
            log=lambda m, c=None: self.after(0, self._dy_log_fn, m, c),
            done=lambda: self.after(0, self._dy_done),
            progress=lambda v, l: self.after(0, self._dy_prog_fn, v, l),
            step=lambda i: self.after(0, self._dy_step_fn, i),
            on_video_ready=lambda p: self.after(0, _on_ready, p),
        )
        self._dy_worker = w
        threading.Thread(target=w.run, daemon=True, kwargs=dict(
            urls=list(self._dy_urls),
            out_dir=self.dy_out.get(),
            model=self.dy_model.get(),
            use_groq=self.dy_use_groq.get(),
            # Phase 1 — tất cả post-processing tắt hết
            do_blur=False, do_tts=False, do_burn=False,
            voice=_VOICE_KEY.get(self.dy_voice.get(), "nu"),
            orig_vol=float(self.dy_orig_vol.get() or "0.15"),
            cookies_file=self.dy_cookies.get() or None,
        )).start()

    def _dy_apply(self):
        """Phase 2: Đọc slider hiện tại → áp dụng blur/TTS/burn lên
        video_original.mp4 và SRT đã có từ Phase 1.
        Không tải lại, không transcribe lại — chỉ hậu kỳ."""
        vid = getattr(self, "_dy_downloaded_vid", None)
        out_dir = self.dy_out.get()
        if not vid or not Path(vid).exists():
            # Thử tìm _original.mp4 gần nhất trong output dir
            out_p = Path(out_dir)
            cands = sorted(out_p.glob("*_original.mp4"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            if not cands:
                messagebox.showwarning(
                    "Chưa có video",
                    "Chưa tải video nào.\nBấm ⬇ Tải + SRT trước."); return
            vid = cands[0]

        self.dy_apply_btn.configure(state="disabled")
        self.dy_start_btn.configure(state="disabled")
        self.dy_stop_btn.configure(state="normal")

        # Reset UI — nếu không reset thì thanh progress vẫn 100% từ Phase 1,
        # user tưởng Phase 2 chưa chạy hoặc đã xong ngay, không thấy log gì
        self.dy_pbar.set(0)
        self.dy_plbl.configure(text="Đang áp dụng hậu kỳ...")
        self._dy_step_fn(-1)

        self.dy_log.configure(state="normal")
        self.dy_log.delete("1.0", "end")
        self.dy_log.insert("end",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🎬  ÁP DỤNG HẬU KỲ — blur / TTS / burn\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
        self.dy_log.configure(state="disabled")
        self._dy_t0 = time.time()

        w = DouyinWorker(
            log=lambda m, c=None: self.after(0, self._dy_log_fn, m, c),
            done=lambda: self.after(0, self._dy_done),
            progress=lambda v, l: self.after(0, self._dy_prog_fn, v, l),
            step=lambda i: self.after(0, self._dy_step_fn, i),
        )
        self._dy_worker = w

        def _run_postprocess_safe():
            """Wrap run_postprocess_only bằng try/except — nếu code trong
            douyin_worker.py raise exception ngay từ đầu (import lỗi, key
            kwargs sai, file không tồn tại...), thread nền chết ÂM THẦM và
            UI đứng mãi ở 'Đang áp dụng hậu kỳ...' không có log gì, không
            cách nào biết nguyên nhân. Bắt exception ở đây để LUÔN in ra
            log và gọi done() (mở lại nút bấm) dù lỗi gì xảy ra."""
            try:
                w.run_postprocess_only(
                    vid_path=vid,
                    out_dir=out_dir,
                    model=self.dy_model.get(),
                    use_groq=self.dy_use_groq.get(),
                    do_blur=self.dy_blur.get(),
                    do_tts=self.dy_tts.get(),
                    do_burn=self.dy_burn.get(),
                    voice=_VOICE_KEY.get(self.dy_voice.get(), "nu"),
                    orig_vol=float(self.dy_orig_vol.get() or "0.15"),
                    blur_top_pct=self.dy_blur_top.get() / 100,
                    blur_bot_pct=self.dy_blur_bot.get() / 100,
                    margin_v=int(max(0, self.dy_margin_v.get())),
                    font_size=int(max(8, self.dy_font_size.get())),
                )
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                self.after(0, self._dy_log_fn,
                           f"❌ LỖI khi áp dụng hậu kỳ: {e}\n{tb}", "error")
                self.after(0, self._dy_done)

        threading.Thread(target=_run_postprocess_safe, daemon=True).start()

    def _dy_stop(self):
        if self._dy_worker: self._dy_worker.stop()
        self.dy_stop_btn.configure(state="disabled")

    def _dy_on_video_ready(self, video_path):
        self._dy_downloaded_vid = video_path
        self._dy_preview_status("⏳ Video đã tải xong — đang khởi động player...")
        if hasattr(self, "dy_play_btn"):
            self.dy_play_btn.configure(state="normal")
        # Load vào embedded player (extract frame đầu tiên làm preview
        # thumbnail đồng thời khởi tạo OpenCV capture cho play/scrub).
        # Chạy trên thread riêng vì OpenCV open có thể block vài giây.
        threading.Thread(target=self._dy_load_player_thread,
                         args=(video_path,), daemon=True).start()

    def _dy_load_player_thread(self, video_path):
        """Chạy trên background thread — OpenCV open + first frame decode.
        Kết quả được đẩy về GUI thread qua self.after()."""
        try:
            import cv2
            cap = cv2.VideoCapture(str(video_path))
            if not cap.isOpened():
                self.after(0, lambda: self._dy_preview_status(
                    "⚠ OpenCV không mở được video — pip install opencv-python"))
                # Fallback: extract frame thủ công bằng ffmpeg như cũ
                self.after(0, lambda: threading.Thread(
                    target=self._dy_extract_frame,
                    args=(video_path,), daemon=True).start())
                return

            fps    = cap.get(cv2.CAP_PROP_FPS) or 30.0
            total  = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            vid_w  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            vid_h  = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            cap.set(1, 0)
            ret, frame = cap.read()
            cap.release()

            if not ret:
                self.after(0, lambda: self._dy_preview_status(
                    "⚠ Không decode được frame đầu — thử Tải frame thủ công"))
                return

            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            from PIL import Image
            img = Image.fromarray(frame_rgb)

            def _apply():
                self._preview_pil_orig    = img
                self._preview_video_w     = vid_w
                self._preview_video_h     = vid_h
                self._player_fps          = fps
                self._player_total        = total
                self._player_pos          = 0
                self._player_paused       = True
                self._player_running      = True
                # Re-open capture on GUI side, dưới lock để không đụng
                # thread nào khác đang seek/read trên _player_cap
                new_cap = cv2.VideoCapture(str(video_path))
                with self._player_lock:
                    if self._player_cap:
                        self._player_cap.release()
                    self._player_cap = new_cap
                self._player_video_path = video_path
                self.dy_play_pause_btn.configure(state="normal", text="▶")
                self.dy_scrub.configure(state="normal")
                self._player_pos_var.set(0.0)
                tot_s = int(total / fps)
                self.dy_time_lbl.configure(
                    text=f"0:00/{tot_s//60}:{tot_s%60:02d}")
                self._dy_draw_preview()
                self._dy_preview_status("")
            self.after(0, _apply)

        except Exception as e:
            self.after(0, lambda: self._dy_preview_status(f"⚠ Player lỗi: {e}"))
            self.after(0, lambda: threading.Thread(
                target=self._dy_extract_frame,
                args=(video_path,), daemon=True).start())

    # ── Embedded video player ─────────────────────────────────────
    def _dy_load_player(self, video_path):
        """Mở video vào embedded player — dùng OpenCV decode frame thành
        PIL Image rồi hiển thị lên canvas. Không cần libvlc hay tkVideoPlayer.
        Được gọi sau khi video tải xong (on_video_ready) hoặc khi user
        bấm ▶ Xem video."""
        try:
            import cv2
        except ImportError:
            messagebox.showinfo(
                "Cần OpenCV",
                "pip install opencv-python\nđể dùng player nhúng trong tool.")
            return

        # Dừng player đang chạy nếu có (join để chắc chắn thread cũ đã
        # thoát trước khi chạm vào cap, tránh 2 thread cùng đọc 1 cap đã
        # release ở dưới)
        self._player_running = False
        self._player_paused  = True
        if self._player_thread and self._player_thread.is_alive():
            self._player_thread.join(timeout=1.0)

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            self._dy_preview_status("⚠ Không mở được video trong player")
            return

        with self._player_lock:
            if self._player_cap:
                self._player_cap.release()
            self._player_cap = cap
        self._player_video_path = video_path
        self._player_fps     = cap.get(cv2.CAP_PROP_FPS) or 30.0
        self._player_total   = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self._player_pos     = 0
        self._player_paused  = True
        self._player_running = True

        # Cập nhật kích thước canvas theo video thật
        vid_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        vid_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self._preview_video_w = vid_w
        self._preview_video_h = vid_h

        # Enable controls
        self.dy_play_pause_btn.configure(state="normal", text="▶")
        self.dy_scrub.configure(state="normal")
        self._player_pos_var.set(0.0)

        # Hiện frame đầu tiên làm thumbnail
        self._dy_player_show_frame(0)

    def _dy_decode_frame(self, frame_idx, target_w, target_h, seek=True):
        """Decode + resize 1 frame — hàm THUẦN TÍNH TOÁN, PHẢI chạy trên
        thread nền, không bao giờ gọi trực tiếp từ GUI thread. Trả về
        (frame_idx, PIL.Image) hoặc None nếu lỗi.

        Đây là fix chính cho "No Response" 2026-07-15: bản cũ gọi
        cap.set()+cap.read()+cv2.cvtColor+PIL resize(LANCZOS) bên TRONG
        self.after() callback — tức là chạy thẳng trên GUI/event thread
        của Tkinter. Fix: decode+resize chạy hết trên thread nền, GUI
        thread chỉ nhận ảnh đã dựng xong để vẽ (việc rẻ, không block).

        seek=True (mặc định): gọi cap.set(CAP_PROP_POS_FRAMES) trước khi
        đọc — dùng cho nhảy cóc (scrub, mở fullscreen, load lần đầu).
        seek=False: bỏ qua .set(), chỉ .read() — VideoCapture tự động
        đọc frame KẾ TIẾP theo con trỏ nội bộ sau lần .read() trước, nên
        khi phát tuần tự không cần seek lại mỗi frame.

        Fix giật video 2026-07-15 (lần 3): gọi .set() ngay cả khi
        frame_idx là "frame kế tiếp" (phát bình thường) vẫn ép decoder
        seek lại từ keyframe gần nhất trên codec nén (H.264...) — cực kỳ
        lãng phí, khiến tốc độ decode không đuổi kịp FPS thật → giật dù
        GUI thread không còn bị block. .set() giờ chỉ gọi khi seek=True."""
        import cv2
        from PIL import Image
        with self._player_lock:
            if not self._player_cap:
                return None
            if seek:
                self._player_cap.set(1, frame_idx)  # CAP_PROP_POS_FRAMES
            ret, frame = self._player_cap.read()
        if not ret:
            return None
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(frame_rgb).resize((target_w, target_h), Image.BILINEAR)
        return (frame_idx, img)


    def _dy_player_show_frame(self, frame_idx):
        """Seek tới frame_idx và hiện lên canvas. An toàn gọi trực tiếp từ
        GUI thread (scrub, click) — decode chạy đồng bộ ở đây vì đây là
        thao tác 1-lần, không phải vòng lặp phát liên tục (khác với
        _dy_player_loop, xem hàm đó để biết cách chạy khi đang phát)."""
        if not self._player_cap:
            return
        try:
            from PIL import ImageTk
        except ImportError:
            return

        pw, ph = _prev_dims(self._preview_video_w, self._preview_video_h, self._prev_box_w)
        result = self._dy_decode_frame(frame_idx, pw, ph)
        if result is None:
            return
        _, img = result

        if (self.dy_canvas.winfo_reqwidth(), self.dy_canvas.winfo_reqheight()) != (pw, ph):
            self.dy_canvas.configure(width=pw, height=ph)

        # Composite overlay blur/sub lên frame — cùng đường vẽ với
        # preview tĩnh, tránh lệch/chớp khi seek trong lúc pause.
        from PIL import Image
        ov = self._dy_overlay_layer(pw, ph)
        final = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
        photo = ImageTk.PhotoImage(final)
        self.dy_canvas.delete("all")
        self.dy_canvas.create_image(0, 0, anchor="nw", image=photo)
        self._preview_photo_ref = photo

        # Update scrub + time label
        self._player_pos = frame_idx
        frac = frame_idx / max(1, self._player_total - 1)
        self._player_pos_var.set(frac)
        cur_s = int(frame_idx / self._player_fps)
        tot_s = int(self._player_total / self._player_fps)
        self.dy_time_lbl.configure(
            text=f"{cur_s//60}:{cur_s%60:02d}/{tot_s//60}:{tot_s%60:02d}")

    def _dy_apply_decoded_frame(self, frame_idx, img):
        """Nhận ảnh ĐÃ decode+resize xong từ thread nền và vẽ lên canvas.
        Chỉ làm việc nhẹ (tạo PhotoImage + vẽ) — an toàn chạy trên GUI
        thread qua self.after(), không bao giờ block đáng kể."""
        try:
            from PIL import ImageTk
        except ImportError:
            return
        if not self._player_running or self._player_cap is None:
            return
        pw, ph = _prev_dims(self._preview_video_w, self._preview_video_h, self._prev_box_w)
        if (self.dy_canvas.winfo_reqwidth(), self.dy_canvas.winfo_reqheight()) != (pw, ph):
            self.dy_canvas.configure(width=pw, height=ph)
        # Composite overlay blur/sub lên MỖI frame đang phát — đây là fix
        # chính cho lỗi "chớp chớp, không thấy overlay khi bật blur+sub":
        # trước đây hàm này vẽ img gốc không overlay ở ~30fps, xoá mất
        # overlay do _dy_draw_preview vẽ trước đó. Giờ dùng chung
        # _dy_overlay_layer() với preview tĩnh nên overlay LUÔN hiện,
        # kể cả khi video đang chạy.
        from PIL import Image
        ov = self._dy_overlay_layer(pw, ph)
        final = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
        photo = ImageTk.PhotoImage(final)
        self.dy_canvas.delete("all")
        self.dy_canvas.create_image(0, 0, anchor="nw", image=photo)
        self._preview_photo_ref = photo

        self._player_pos = frame_idx
        frac = frame_idx / max(1, self._player_total - 1)
        self._player_pos_var.set(frac)
        cur_s = int(frame_idx / self._player_fps)
        tot_s = int(self._player_total / self._player_fps)
        self.dy_time_lbl.configure(
            text=f"{cur_s//60}:{cur_s%60:02d}/{tot_s//60}:{tot_s%60:02d}")

    def _dy_toggle_play(self):
        """Play/Pause toggle. Khởi động vòng lặp phát trên thread riêng."""
        if not self._player_cap:
            return
        if self._fs_win is not None and self._fs_win.winfo_exists():
            # Fullscreen đang mở và đang có loop riêng của nó — không cho
            # player nhúng ở màn chính chạy song song (nguồn giật cũ).
            self._fs_win.lift()
            self._fs_win.focus_force()
            return
        if self._player_paused:
            self._player_paused = False
            self.dy_play_pause_btn.configure(text="⏸")
            if not (self._player_thread and self._player_thread.is_alive()):
                self._player_thread = threading.Thread(
                    target=self._dy_player_loop, daemon=True)
                self._player_thread.start()
        else:
            self._player_paused = True
            self.dy_play_pause_btn.configure(text="▶")

    def _dy_player_loop(self):
        """Vòng lặp phát trên thread NỀN. Decode + resize (việc NẶNG)
        chạy hoàn toàn ở đây, ngoài GUI thread. Chỉ đẩy ảnh đã xong lên
        GUI thread qua self.after() để vẽ (việc NHẸ).

        Fix giật video 2026-07-15 (lần 3): dùng seek=False — con trỏ đọc
        nội bộ của cv2.VideoCapture tự động trỏ tới frame kế tiếp sau mỗi
        .read(), nên chỉ cần .read() liên tục là đủ, không cần .set() lại
        mỗi lần (ép decode lại từ keyframe, rất chậm trên codec nén →
        đây là nguyên nhân giật). Dùng biến `pos` CỤC BỘ tự tăng trong
        vòng lặp (không đọc lại self._player_pos mỗi vòng) vì
        self._player_pos chỉ được cập nhật TRỄ trên GUI thread qua
        .after() — đọc lại nó ngay ở thread nền có thể thấy giá trị cũ
        và vô tình phát lại/bỏ frame."""
        import time as _time
        pw, ph = _prev_dims(self._preview_video_w, self._preview_video_h, self._prev_box_w)
        frame_ms = 1.0 / self._player_fps
        pos = self._player_pos
        while self._player_running and not self._player_paused:
            t0 = _time.perf_counter()
            pos += 1
            if pos >= self._player_total:
                # Hết video — pause và quay về frame cuối
                self._player_paused = True
                self.after(0, lambda: self.dy_play_pause_btn.configure(text="▶"))
                break
            result = self._dy_decode_frame(pos, pw, ph, seek=False)
            if result is not None:
                idx, img = result
                self.after(0, self._dy_apply_decoded_frame, idx, img)
            elapsed = _time.perf_counter() - t0
            sleep = frame_ms - elapsed
            # Nếu decode 1 frame đã lâu hơn frame_ms (thường xảy ra khi
            # seek trên codec nén), KHÔNG dồn frame — bỏ sleep, chạy tiếp
            # ngay ở tốc độ decode thật thay vì cố đuổi kịp FPS gốc, để
            # không tích luỹ hàng đợi .after() lớn dần theo thời gian.
            if sleep > 0:
                _time.sleep(sleep)

    def _dy_scrub_seek(self, val):
        """Scrub slider moved — pause và seek tới vị trí tương ứng.
        Chạy decode trên thread nền (không phải trực tiếp trong callback
        của CTkSlider, vốn chạy trên GUI thread) — nếu người dùng kéo
        thanh trượt nhanh/liên tục, mỗi lần kéo trước đây trigger 1 lần
        seek+decode+resize ĐỒNG BỘ ngay trên GUI thread, dễ dồn ứ giống
        hệt lỗi ở player loop. Đánh số thứ tự request để chỉ áp dụng kết
        quả của request mới nhất, bỏ qua kết quả trễ của các request cũ
        hơn khi user đã kéo tiếp."""
        if not self._player_cap:
            return
        self._player_paused = True
        self.dy_play_pause_btn.configure(text="▶")
        target = int(float(val) * (self._player_total - 1))
        self._scrub_req_seq = getattr(self, "_scrub_req_seq", 0) + 1
        req_id = self._scrub_req_seq

        def _bg():
            pw, ph = _prev_dims(self._preview_video_w, self._preview_video_h, self._prev_box_w)
            result = self._dy_decode_frame(target, pw, ph)
            if result is None:
                return
            idx, img = result
            if req_id != self._scrub_req_seq:
                return  # user đã kéo tiếp, kết quả này đã cũ, bỏ qua
            self.after(0, self._dy_apply_decoded_frame, idx, img)

        threading.Thread(target=_bg, daemon=True).start()

    def _dy_fullscreen_player(self):
        """Mở Toplevel fullscreen với canvas lớn — giống nút expand của CapCut.
        Có thể thu nhỏ lại bằng phím Esc hoặc nút X.

        Fix 2026-07-15: trước đây _fs_loop (thread riêng cho fullscreen) và
        _dy_player_loop (thread của player nhúng ở màn hình chính) có thể
        chạy song song, cả 2 cùng gọi .set()/.read() trên chung 1 cv2.
        VideoCapture không thread-safe → giật hình, đơ UI khi bấm nút mở
        rộng. Fix: (1) dừng hẳn player nhúng trước khi mở fullscreen,
        (2) mọi truy cập _player_cap đều qua self._player_lock,
        (3) chỉ 1 fullscreen window tồn tại tại một thời điểm."""
        if not self._player_cap:
            # Fallback: mở bằng system player nếu chưa có video trong player
            vid = getattr(self, "_dy_downloaded_vid", None)
            if not vid or not Path(vid).exists():
                out_p = Path(self.dy_out.get())
                cands = sorted(out_p.glob("*_original.mp4"),
                               key=lambda p: p.stat().st_mtime, reverse=True)
                vid = cands[0] if cands else None
            if vid:
                try:
                    if sys.platform == "win32": os.startfile(str(vid))
                    elif sys.platform == "darwin": subprocess.Popen(["open", str(vid)])
                    else: subprocess.Popen(["xdg-open", str(vid)])
                except Exception: pass
            return

        # Nếu đã có cửa sổ fullscreen mở sẵn, chỉ đưa lên trên thay vì mở
        # cửa sổ thứ hai (mở 2 cửa sổ cùng lúc là nguồn giật/lag khác).
        if self._fs_win is not None and self._fs_win.winfo_exists():
            self._fs_win.lift()
            self._fs_win.focus_force()
            return

        # Dừng player nhúng ở màn hình chính — chỉ 1 loop được chạy tại
        # một thời điểm, join để chắc chắn thread cũ đã thoát hẳn.
        self._player_paused = True
        if self._player_thread and self._player_thread.is_alive():
            self._player_thread.join(timeout=1.0)
        self.dy_play_pause_btn.configure(text="▶")

        win = tk.Toplevel(self)
        self._fs_win = win
        win.title("DouyinViet — Video Player")
        win.configure(bg="#0f0f13")
        win.attributes("-fullscreen", True)

        def _fs_close():
            self._player_paused = True
            self._fs_win = None
            win.destroy()

        win.bind("<Escape>", lambda e: _fs_close())
        win.protocol("WM_DELETE_WINDOW", _fs_close)

        scr_w = win.winfo_screenwidth()
        scr_h = win.winfo_screenheight()
        vid_w = self._preview_video_w or 1080
        vid_h = self._preview_video_h or 1920
        ar = vid_w / vid_h
        if scr_w / scr_h < ar:
            cw, ch = scr_w, int(scr_w / ar)
        else:
            cw, ch = int(scr_h * ar), scr_h

        fs_canvas = tk.Canvas(win, bg="#0f0f13", width=cw, height=ch,
                              highlightthickness=0)
        fs_canvas.pack(expand=True)

        close_btn = ctk.CTkButton(win, text="✕ Đóng (Esc)", width=100, height=28,
                                   fg_color="#2a1a1a", hover_color="#5a2222",
                                   font=("Segoe UI", 10),
                                   command=_fs_close)
        close_btn.pack(pady=6)

        fs_photo_ref = [None]
        fs_thread = [None]

        def _fs_apply(frame_idx, img):
            """Việc NHẸ — chỉ tạo PhotoImage + vẽ. An toàn trên GUI thread."""
            if self._fs_win is None:
                return
            from PIL import Image, ImageTk
            # Composite overlay blur/sub — cùng cách với dy_canvas, để
            # fullscreen cũng dùng canh vùng blur/sub được (trước đây
            # chỉ hiện frame sạch, không overlay, trong cả fullscreen).
            ov = self._dy_overlay_layer(cw, ch)
            final = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
            photo = ImageTk.PhotoImage(final)
            fs_canvas.delete("all")
            fs_canvas.create_image(0, 0, anchor="nw", image=photo)
            fs_photo_ref[0] = photo
            self._player_pos = frame_idx
            frac = frame_idx / max(1, self._player_total - 1)
            fs_pos.set(frac)
            self._player_pos_var.set(frac)

        def _fs_show(frame_idx):
            """Dùng cho seek 1 lần (mở cửa sổ, scrub) — decode ở thread
            nền rồi áp dụng, không block GUI thread."""
            if self._fs_win is None or not self._player_cap:
                return
            def _bg():
                result = self._dy_decode_frame(frame_idx, cw, ch)
                if result is None or self._fs_win is None:
                    return
                idx, img = result
                self._fs_win.after(0, _fs_apply, idx, img)
            threading.Thread(target=_bg, daemon=True).start()

        def _fs_loop():
            """Vòng lặp phát fullscreen trên thread NỀN — decode+resize
            (việc nặng) chạy hết ở đây, chỉ đẩy ảnh xong lên GUI thread
            qua _fs_apply (việc nhẹ). Cùng nguyên tắc + cùng fix giật
            (seek=False, pos cục bộ) như _dy_player_loop — xem đó để
            biết lý do."""
            import time as _t
            frame_ms = 1.0 / self._player_fps
            pos = self._player_pos
            while (self._fs_win is not None and self._player_running
                   and not self._player_paused):
                t0 = _t.perf_counter()
                pos += 1
                if pos >= self._player_total:
                    self._player_paused = True
                    if self._fs_win is not None:
                        self._fs_win.after(0, lambda: fs_pp.configure(text="▶"))
                    break
                result = self._dy_decode_frame(pos, cw, ch, seek=False)
                if result is not None and self._fs_win is not None:
                    idx, img = result
                    self._fs_win.after(0, _fs_apply, idx, img)
                elapsed = _t.perf_counter() - t0
                s = frame_ms - elapsed
                if s > 0: _t.sleep(s)

        # Controls bar in fullscreen
        ctrl = ctk.CTkFrame(win, fg_color="#111118", corner_radius=0)
        ctrl.pack(fill="x", side="bottom")
        ctrl.columnconfigure(1, weight=1)

        def _fs_toggle():
            if self._player_paused:
                self._player_paused = False
                fs_pp.configure(text="⏸")
                if not (fs_thread[0] and fs_thread[0].is_alive()):
                    fs_thread[0] = threading.Thread(target=_fs_loop, daemon=True)
                    fs_thread[0].start()
            else:
                self._player_paused = True
                fs_pp.configure(text="▶")

        fs_pp = ctk.CTkButton(ctrl, text="▶", width=32, height=28,
                               fg_color=CARD, hover_color="#333344",
                               font=("Segoe UI", 14), command=_fs_toggle)
        fs_pp.grid(row=0, column=0, padx=8, pady=6)

        fs_pos = tk.DoubleVar(value=self._player_pos_var.get())

        def _fs_scrub(val):
            self._player_paused = True
            fs_pp.configure(text="▶")
            target = int(float(val) * (self._player_total - 1))
            _fs_show(target)

        fs_sl = ctk.CTkSlider(ctrl, variable=fs_pos, from_=0, to=1,
                               height=18, progress_color=ACCENT2,
                               button_color=ACCENT2,
                               command=_fs_scrub)
        fs_sl.grid(row=0, column=1, sticky="ew", padx=8)

        # Show current frame immediately
        _fs_show(self._player_pos)

    def _dy_play_video(self):
        """Nút ▶ lớn cạnh Preview. Trước đây luôn load lại player từ đầu
        mỗi lần bấm — nếu player đã sẵn sàng (video đã load, ví dụ ngay
        sau khi tải xong ở _dy_on_video_ready) thì bấm nút này chỉ hiện
        lại frame đầu chứ không phát, khiến người dùng tưởng nút không
        hoạt động. Fix: nếu player đã có cap sẵn cho đúng video này thì
        toggle play/pause luôn; chỉ load lại khi chưa có player hoặc
        player đang trỏ tới video khác."""
        vid = getattr(self, "_dy_downloaded_vid", None)
        if not vid or not Path(vid).exists():
            out_p = Path(self.dy_out.get())
            cands = sorted(out_p.glob("*_original.mp4"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            if not cands:
                messagebox.showinfo("Chưa có video",
                                    "Chưa tải video nào.\nBấm ⬇ Tải + SRT trước.")
                return
            vid = cands[0]

        already_loaded = (self._player_cap is not None
                           and str(getattr(self, "_player_video_path", "")) == str(vid))
        if already_loaded:
            self._dy_toggle_play()
        else:
            self._player_video_path = vid
            self._dy_load_player(vid)



    def _dy_done(self):
        self.dy_start_btn.configure(state="normal")
        self.dy_stop_btn.configure(state="disabled")
        has_vid = (getattr(self, "_dy_downloaded_vid", None) or
                   any(Path(self.dy_out.get()).glob("*_original.mp4")))
        if has_vid:
            self.dy_apply_btn.configure(state="normal")
            if hasattr(self, "dy_play_btn"):
                self.dy_play_btn.configure(state="normal")

    # ── Facebook actions ──────────────────────────────────────
    def _fb_log_fn(self, msg, col=None):
        self.fb_log.configure(state="normal")
        self.fb_log.insert("end", msg + "\n"); self.fb_log.see("end")
        self.fb_log.configure(state="disabled")

    def _fb_prog_fn(self, v, lbl):
        self.fb_pbar.set(v)
        self.fb_plbl.configure(text=f"{int(v*100)}%  |  {lbl}")

    def _fb_pick_cookies(self):
        p = filedialog.askopenfilename(filetypes=[("Cookies", "*.txt"), ("All", "*.*")])
        if p: self.fb_cookies.set(p)

    def _fb_crawl(self):
        url = self.fb_url.get().strip()
        if not url:
            messagebox.showwarning("Thiếu URL", "Nhập URL fanpage!"); return
        self.fb_crawl_btn.configure(state="disabled")
        self.fb_dl_btn.configure(state="disabled")
        self.fb_stop_btn.configure(state="normal")
        self.fb_pbar.set(0); self.fb_count.configure(text="Đang quét...")
        for w in self.fb_scroll.winfo_children(): w.destroy()
        self._fb_videos = []; self._fb_vars = {}
        ctk.CTkLabel(self.fb_scroll, text="Đang quét...",
                     font=("Segoe UI", 12), text_color=SUBTEXT).pack(pady=40)

        worker = FBCrawler(
            log=lambda m, c=None: self.after(0, self._fb_log_fn, m, c),
            done=lambda: self.after(0, self._fb_crawl_done),
            progress=lambda v, l: self.after(0, self._fb_prog_fn, v, l),
        )
        self._fb_worker = worker
        threading.Thread(target=worker.crawl, daemon=True, kwargs=dict(
            page_url=url,
            max_videos=max(1, min(300, self.fb_limit.get())),
            cookies_file=self.fb_cookies.get(),
            result_fn=lambda vids: self.after(0, self._fb_show, vids),
            out_dir=self.fb_out.get().strip() or None,
        )).start()

    def _load_thumb(self, url, label):
        """Tải thumbnail trong background thread rồi gắn vào label."""
        def _run():
            try:
                import requests
                from PIL import Image
                import io as _io2
                resp = requests.get(url, timeout=6,
                                    headers={"User-Agent": "Mozilla/5.0"})
                img = Image.open(_io2.BytesIO(resp.content)).convert("RGB")
                img = img.resize((90, 50), Image.LANCZOS)
                ctk_img = ctk.CTkImage(img, size=(90, 50))
                self.after(0, lambda: label.configure(image=ctk_img, text=""))
                label._img_ref = ctk_img   # prevent GC
            except: pass
        threading.Thread(target=_run, daemon=True).start()

    def _fb_show(self, videos):
        self._fb_videos = videos
        for w in self.fb_scroll.winfo_children(): w.destroy()
        self._fb_vars = {}
        if not videos:
            ctk.CTkLabel(self.fb_scroll,
                         text="Không tìm thấy video.\n\nThử:\n• Dùng URL kết thúc bằng &sk=reels_tab\n• Cung cấp file cookies đã đăng nhập",
                         font=("Segoe UI", 11), text_color=WARN, justify="center"
                         ).pack(pady=30)
            return
        self.fb_count.configure(text=f"Tìm thấy {len(videos)} video — tick chọn rồi bấm Tải")
        n_done = sum(1 for v in videos if v.get("downloaded"))
        if n_done:
            self.fb_count.configure(
                text=(f"Tìm thấy {len(videos)} video "
                      f"({n_done} đã tải trước đó, đã tự bỏ tick)"))

        # Kiểm tra PIL có sẵn không
        try:
            from PIL import Image
            has_pil = True
        except ImportError:
            has_pil = False

        for i, vid in enumerate(videos):
            is_done = vid.get("downloaded", False)
            var = ctk.BooleanVar(value=not is_done)  # Đã tải → bỏ tick sẵn
            self._fb_vars[vid["url"]] = var

            row = ctk.CTkFrame(self.fb_scroll,
                               fg_color=(CARD if i % 2 == 0 else "#1e1e2a")
                                        if not is_done else "#16261a",
                               corner_radius=6, height=62)
            row.pack(fill="x", pady=2, padx=2)
            row.columnconfigure(2, weight=1)
            row.pack_propagate(False)

            # Checkbox
            ctk.CTkCheckBox(row, variable=var, text="", width=28
                            ).grid(row=0, column=0, padx=(8, 4), pady=10, sticky="w")

            # Thumbnail
            thumb_lbl = ctk.CTkLabel(row, text="▶", width=90, height=50,
                                      fg_color=PANEL, corner_radius=4,
                                      font=("Segoe UI", 18), text_color=SUBTEXT)
            thumb_lbl.grid(row=0, column=1, padx=(0, 8), pady=6)
            if has_pil and vid.get("thumb"):
                self._load_thumb(vid["thumb"], thumb_lbl)

            # Info
            info = ctk.CTkFrame(row, fg_color="transparent")
            info.grid(row=0, column=2, sticky="nsew", pady=6, padx=(0, 8))
            info.columnconfigure(0, weight=1)

            views = f"👁 {vid['views']}" if vid.get("views") else ""
            vid_id = vid["id"][:18]
            title_txt = vid.get("title", "") or f"Reel #{i+1}"
            badge = "  ✅ Đã tải" if is_done else ""
            ctk.CTkLabel(info, text=f"#{i+1}  {title_txt}{badge}",
                         font=("Segoe UI", 12, "bold"),
                         text_color=SUCCESS if is_done else TEXT,
                         anchor="w", wraplength=420, justify="left"
                         ).pack(fill="x")
            ctk.CTkLabel(info, text=f"{views}   🔗 …/reel/{vid_id}",
                         font=("Segoe UI", 9), text_color=SUBTEXT,
                         anchor="w").pack(fill="x")

    def _fb_crawl_done(self):
        self.fb_crawl_btn.configure(state="normal")
        self.fb_stop_btn.configure(state="disabled")
        if self._fb_vars:
            self.fb_dl_btn.configure(state="normal")

    def _fb_select_all(self):
        for v in self._fb_vars.values(): v.set(True)

    def _fb_deselect(self):
        for v in self._fb_vars.values(): v.set(False)

    def _fb_download(self):
        selected = [(url, next((x["title"] for x in self._fb_videos if x["url"] == url), ""))
                    for url, var in self._fb_vars.items() if var.get()]
        if not selected:
            messagebox.showwarning("Chưa chọn", "Tick chọn ít nhất 1 video!"); return
        self.fb_dl_btn.configure(state="disabled")
        self.fb_crawl_btn.configure(state="disabled")
        self.fb_stop_btn.configure(state="normal")
        self.fb_pbar.set(0)
        worker = FBCrawler(
            log=lambda m, c=None: self.after(0, self._fb_log_fn, m, c),
            done=lambda: self.after(0, self._fb_dl_done),
            progress=lambda v, l: self.after(0, self._fb_prog_fn, v, l),
        )
        self._fb_worker = worker
        threading.Thread(target=worker.download, daemon=True, kwargs=dict(
            items=selected,
            out_dir=self.fb_out.get(),
            cookies_file=self.fb_cookies.get(),
        )).start()

    def _fb_dl_done(self):
        self.fb_dl_btn.configure(state="normal")
        self.fb_crawl_btn.configure(state="normal")
        self.fb_stop_btn.configure(state="disabled")

    def _fb_stop(self):
        if self._fb_worker: self._fb_worker.stop()
        self.fb_stop_btn.configure(state="disabled")

    # ── TikTok actions ─────────────────────────────────────────
    def _tt_log_fn(self, msg, col=None):
        self.tt_log.configure(state="normal")
        self.tt_log.insert("end", msg + "\n"); self.tt_log.see("end")
        self.tt_log.configure(state="disabled")

    def _tt_prog_fn(self, v, lbl):
        self.tt_pbar.set(v)
        self.tt_plbl.configure(text=f"{int(v*100)}%  |  {lbl}")

    def _tt_pick_cookies(self):
        p = filedialog.askopenfilename(filetypes=[("Cookies", "*.txt"), ("All", "*.*")])
        if p: self.tt_cookies.set(p)

    def _tt_crawl(self):
        url = self.tt_url.get().strip()
        if not url:
            messagebox.showwarning("Thiếu URL", "Nhập URL kênh TikTok!"); return
        self.tt_crawl_btn.configure(state="disabled")
        self.tt_dl_btn.configure(state="disabled")
        self.tt_stop_btn.configure(state="normal")
        self.tt_pbar.set(0); self.tt_count.configure(text="Đang quét...")
        for w in self.tt_scroll.winfo_children(): w.destroy()
        self._tt_videos = []; self._tt_vars = {}
        ctk.CTkLabel(self.tt_scroll, text="Đang quét...",
                     font=("Segoe UI", 12), text_color=SUBTEXT).pack(pady=40)

        worker = TikTokCrawler(
            log=lambda m, c=None: self.after(0, self._tt_log_fn, m, c),
            done=lambda: self.after(0, self._tt_crawl_done),
            progress=lambda v, l: self.after(0, self._tt_prog_fn, v, l),
        )
        self._tt_worker = worker
        threading.Thread(target=worker.crawl, daemon=True, kwargs=dict(
            profile_url=url,
            max_videos=max(1, min(300, self.tt_limit.get())),
            cookies_file=self.tt_cookies.get(),
            result_fn=lambda vids: self.after(0, self._tt_show, vids),
            out_dir=self.tt_out.get().strip() or None,
        )).start()

    def _tt_show(self, videos):
        self._tt_videos = videos
        for w in self.tt_scroll.winfo_children(): w.destroy()
        self._tt_vars = {}
        if not videos:
            ctk.CTkLabel(self.tt_scroll,
                         text="Không tìm thấy video.\n\nKiểm tra lại URL kênh\n"
                              "(vd: https://www.tiktok.com/@tenuser)",
                         font=("Segoe UI", 11), text_color=WARN, justify="center"
                         ).pack(pady=30)
            return
        self.tt_count.configure(text=f"Tìm thấy {len(videos)} video — tick chọn rồi bấm Tải")
        n_done = sum(1 for v in videos if v.get("downloaded"))
        if n_done:
            self.tt_count.configure(
                text=(f"Tìm thấy {len(videos)} video "
                      f"({n_done} đã tải trước đó, đã tự bỏ tick)"))

        try:
            from PIL import Image
            has_pil = True
        except ImportError:
            has_pil = False

        for i, vid in enumerate(videos):
            is_done = vid.get("downloaded", False)
            var = ctk.BooleanVar(value=not is_done)
            self._tt_vars[vid["url"]] = var

            row = ctk.CTkFrame(self.tt_scroll,
                               fg_color=(CARD if i % 2 == 0 else "#1e1e2a")
                                        if not is_done else "#16261a",
                               corner_radius=6, height=62)
            row.pack(fill="x", pady=2, padx=2)
            row.columnconfigure(2, weight=1)
            row.pack_propagate(False)

            ctk.CTkCheckBox(row, variable=var, text="", width=28
                            ).grid(row=0, column=0, padx=(8, 4), pady=10, sticky="w")

            thumb_lbl = ctk.CTkLabel(row, text="▶", width=90, height=50,
                                      fg_color=PANEL, corner_radius=4,
                                      font=("Segoe UI", 18), text_color=SUBTEXT)
            thumb_lbl.grid(row=0, column=1, padx=(0, 8), pady=6)
            if has_pil and vid.get("thumb"):
                self._load_thumb(vid["thumb"], thumb_lbl)

            info = ctk.CTkFrame(row, fg_color="transparent")
            info.grid(row=0, column=2, sticky="nsew", pady=6, padx=(0, 8))
            info.columnconfigure(0, weight=1)

            views = f"👁 {vid['views']}" if vid.get("views") else ""
            vid_id = vid["id"][:18]
            title_txt = vid.get("title", "") or f"TikTok #{i+1}"
            badge = "  ✅ Đã tải" if is_done else ""
            ctk.CTkLabel(info, text=f"#{i+1}  {title_txt}{badge}",
                         font=("Segoe UI", 12, "bold"),
                         text_color=SUCCESS if is_done else TEXT,
                         anchor="w", wraplength=420, justify="left"
                         ).pack(fill="x")
            ctk.CTkLabel(info, text=f"{views}   🔗 …/video/{vid_id}",
                         font=("Segoe UI", 9), text_color=SUBTEXT,
                         anchor="w").pack(fill="x")

    def _tt_crawl_done(self):
        self.tt_crawl_btn.configure(state="normal")
        self.tt_stop_btn.configure(state="disabled")
        if self._tt_vars:
            self.tt_dl_btn.configure(state="normal")

    def _tt_select_all(self):
        for v in self._tt_vars.values(): v.set(True)

    def _tt_deselect(self):
        for v in self._tt_vars.values(): v.set(False)

    def _tt_download(self):
        selected = [(url, next((x["title"] for x in self._tt_videos if x["url"] == url), ""))
                    for url, var in self._tt_vars.items() if var.get()]
        if not selected:
            messagebox.showwarning("Chưa chọn", "Tick chọn ít nhất 1 video!"); return
        self.tt_dl_btn.configure(state="disabled")
        self.tt_crawl_btn.configure(state="disabled")
        self.tt_stop_btn.configure(state="normal")
        self.tt_pbar.set(0)
        worker = TikTokCrawler(
            log=lambda m, c=None: self.after(0, self._tt_log_fn, m, c),
            done=lambda: self.after(0, self._tt_dl_done),
            progress=lambda v, l: self.after(0, self._tt_prog_fn, v, l),
        )
        self._tt_worker = worker
        threading.Thread(target=worker.download, daemon=True, kwargs=dict(
            items=selected,
            out_dir=self.tt_out.get(),
            cookies_file=self.tt_cookies.get(),
        )).start()

    def _tt_dl_done(self):
        self.tt_dl_btn.configure(state="normal")
        self.tt_crawl_btn.configure(state="normal")
        self.tt_stop_btn.configure(state="disabled")

    def _tt_stop(self):
        if self._tt_worker: self._tt_worker.stop()
        self.tt_stop_btn.configure(state="disabled")


if __name__ == "__main__":
    try: import customtkinter
    except ImportError: print("pip install customtkinter"); sys.exit(1)
    App().mainloop()