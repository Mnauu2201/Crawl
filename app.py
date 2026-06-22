"""DouyinViet - Main app: 2 tab Douyin→SRT và Facebook Crawler."""
import sys, os, time, threading, subprocess
from pathlib import Path
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
        self.geometry("1100x720"); self.minsize(920, 620)
        self.configure(fg_color=BG)
        self._dy_urls = []; self._dy_worker = None; self._dy_t0 = None
        self._fb_videos = []; self._fb_vars = {}; self._fb_worker = None
        self._tt_videos = []; self._tt_vars = {}; self._tt_worker = None
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
        parent.columnconfigure(1, weight=0, minsize=255)
        parent.rowconfigure(0, weight=1)

        # ── Left ──────────────────────────────────────────────
        L = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10)
        L.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=4)
        L.columnconfigure(0, weight=1); L.rowconfigure(4, weight=1)

        ctk.CTkLabel(L, text="Danh sách URL Douyin",
                     font=("Segoe UI", 12, "bold"), text_color=TEXT
                     ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 4))

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

        self.dy_list = ctk.CTkTextbox(L, fg_color=PANEL, text_color=TEXT,
                                       font=("Consolas", 10), height=90,
                                       border_color="#333355", border_width=1)
        self.dy_list.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 6))
        self.dy_list.configure(state="disabled")

        # Progress bar
        pg = ctk.CTkFrame(L, fg_color=PANEL, corner_radius=8)
        pg.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 6))
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

        ctk.CTkLabel(L, text="Log", font=("Segoe UI", 11, "bold"), text_color=TEXT
                     ).grid(row=3, column=0, sticky="nw", padx=12, pady=(2, 1))
        self.dy_log = ctk.CTkTextbox(L, fg_color=PANEL, text_color=TEXT,
                                      font=("Consolas", 10),
                                      border_color="#333355", border_width=1)
        self.dy_log.grid(row=4, column=0, sticky="nsew", padx=12, pady=(0, 10))
        self.dy_log.configure(state="disabled")

        # ── Right ─────────────────────────────────────────────
        R = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10, width=255)
        R.grid(row=0, column=1, sticky="nsew", pady=4)
        R.columnconfigure(0, weight=1)

        _lbl(R, 0, "⚙ Cài đặt", 14, TEXT, True)
        _lbl(R, 1, "📁 Output folder")
        or2 = ctk.CTkFrame(R, fg_color="transparent")
        or2.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))
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
                     ).grid(row=5, column=0, sticky="w", padx=12, pady=(0, 8))

        self.dy_use_groq = ctk.BooleanVar(value=False)
        groq_status = "có sẵn ✅" if _GROQ_AVAILABLE_APP else "CHƯA có groq_client.py ⚠"
        ctk.CTkCheckBox(R, text="☁ Dùng Groq API (nhanh, cần internet + key)",
                        variable=self.dy_use_groq, font=("Segoe UI", 11),
                        text_color=TEXT, border_color=ACCENT2, fg_color=ACCENT2
                        ).grid(row=6, column=0, sticky="w", padx=12, pady=(0, 2))
        ctk.CTkLabel(R, text=f"groq_client.py: {groq_status}  •  tự rớt về local nếu lỗi",
                     font=("Segoe UI", 9), text_color=SUBTEXT, justify="left"
                     ).grid(row=7, column=0, sticky="w", padx=12, pady=(0, 8))

        _sep(R, 8)

        info = ctk.CTkFrame(R, fg_color=PANEL, corner_radius=8)
        info.grid(row=9, column=0, sticky="ew", padx=12, pady=(4, 10))
        info.columnconfigure(0, weight=1)
        for i, ln in enumerate(["📦 Output:", "• video_original.mp4",
                                  "• video_vi.srt  ← CapCut",
                                  "• video_segments.json"]):
            ctk.CTkLabel(info, text=ln, font=("Segoe UI", 10 if i > 0 else 11),
                         text_color=TEXT if i == 0 else SUBTEXT, justify="left"
                         ).grid(row=i, column=0, sticky="w", padx=10, pady=1)

        _sep(R, 10)
        self.dy_start_btn = ctk.CTkButton(R, text="▶  Bắt đầu", height=42,
                                           font=("Segoe UI", 13, "bold"),
                                           fg_color=ACCENT, hover_color="#5a4dd4",
                                           command=self._dy_start)
        self.dy_start_btn.grid(row=11, column=0, sticky="ew", padx=12, pady=(6, 4))
        self.dy_stop_btn = ctk.CTkButton(R, text="⏹  Dừng", height=32,
                                          font=("Segoe UI", 11),
                                          fg_color="#3a1a1a", hover_color="#5a2222",
                                          state="disabled", command=self._dy_stop)
        self.dy_stop_btn.grid(row=12, column=0, sticky="ew", padx=12, pady=(0, 12))

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
    def _dy_add(self):
        u = self.dy_entry.get().strip()
        if u and u not in self._dy_urls:
            self._dy_urls.append(u); self._dy_refresh()
        self.dy_entry.delete(0, "end")

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
        if not self._dy_urls:
            messagebox.showwarning("Chưa có URL", "Thêm ít nhất 1 URL Douyin!"); return
        self.dy_start_btn.configure(state="disabled")
        self.dy_stop_btn.configure(state="normal")
        self.dy_log.configure(state="normal"); self.dy_log.delete("1.0", "end")
        self.dy_log.configure(state="disabled")
        self.dy_pbar.set(0); self._dy_step_fn(-1)
        self._dy_t0 = time.time()
        w = DouyinWorker(
            log=lambda m, c=None: self.after(0, self._dy_log_fn, m, c),
            done=lambda: self.after(0, self._dy_done),
            progress=lambda v, l: self.after(0, self._dy_prog_fn, v, l),
            step=lambda i: self.after(0, self._dy_step_fn, i),
        )
        self._dy_worker = w
        threading.Thread(target=w.run, daemon=True, kwargs=dict(
            urls=list(self._dy_urls),
            out_dir=self.dy_out.get(),
            model=self.dy_model.get(),
            use_groq=self.dy_use_groq.get())).start()

    def _dy_stop(self):
        if self._dy_worker: self._dy_worker.stop()
        self.dy_stop_btn.configure(state="disabled")

    def _dy_done(self):
        self.dy_start_btn.configure(state="normal")
        self.dy_stop_btn.configure(state="disabled")

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