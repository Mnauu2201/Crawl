"""Worker: Tải Douyin video → Whisper → Dịch → Xuất SRT."""
import sys, json, time, io, threading, subprocess
from pathlib import Path
from config import *

try:
    from groq_client import GroqClient, GroqAllKeysFailedError
    _GROQ_AVAILABLE = True
except ImportError:
    _GROQ_AVAILABLE = False

class DouyinWorker:
    STEPS = ["⬇ Tải", "🎵 Audio", "🤖 Whisper", "🌐 Dịch", "💾 SRT"]

    def __init__(self, log, done, progress, step):
        self.log = log; self.done = done
        self.progress = progress; self.step = step
        self._stop = False

    def stop(self): self._stop = True

    def run(self, urls, out_dir, model, use_groq=False):
        ok = 0; n = len(urls)

        # Khởi tạo GroqClient 1 LẦN cho cả batch (không phải mỗi video),
        # để giữ nguyên trạng thái xoay vòng/cooldown key xuyên suốt —
        # tránh việc video sau lại thử lại đúng key vừa bị rate-limit ở
        # video trước. Lỗi khởi tạo (thiếu groq_client.py / thiếu key) →
        # tự rớt về chạy local cho TOÀN BỘ batch, có log rõ lý do.
        groq_client = None
        if use_groq:
            if not _GROQ_AVAILABLE:
                self.log("⚠ groq_client.py không tìm thấy — dùng Whisper/Dịch "
                        "local (offline) cho toàn bộ batch.", WARN)
                use_groq = False
            else:
                try:
                    groq_client = GroqClient()
                    self.log(f"☁ Dùng Groq API ({len(groq_client.pool)} key) cho "
                            f"Whisper + Dịch — tự rớt về local nếu Groq lỗi.", ACCENT2)
                except Exception as e:
                    self.log(f"⚠ Không khởi tạo được Groq ({e}) — dùng Whisper/Dịch "
                            f"local (offline) cho toàn bộ batch.", WARN)
                    use_groq = False

        for i, url in enumerate(urls, 1):
            if self._stop: self.log("⏹ Dừng.", WARN); break
            self.log(f"\n{'─'*46}", SUBTEXT)
            self.log(f"[{i}/{n}] {url[:65]}", TEXT)
            self.progress(0.0, f"Video {i}/{n}...")
            if self._one(url, Path(out_dir), model, use_groq, groq_client): ok += 1
        self.log(f"\n{'='*46}", ACCENT)
        self.log(f"XONG: {ok}/{n} video thành công!", SUCCESS)
        self.progress(1.0, f"Hoàn tất {ok}/{n} ✅")
        self.done()

    def _one(self, url, out_dir, model, use_groq=False, groq_client=None):
        import re as re_
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            ytdlp = get_ytdlp()

            # 1. TẢI
            self.step(0); self.progress(0.01, "Đang tải video...")
            self.log("⬇ Tai video...", ACCENT2)
            r = subprocess.run(ytdlp + ["--print", "%(title).60s", "--no-download", url],
                               capture_output=True, text=True)
            title = "".join(c for c in (r.stdout.strip() or "video")
                            if c.isalnum() or c in "_- ")[:40].strip() or "video"
            out_path = out_dir / f"{title}_original.mp4"

            def dl(cmd):
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     bufsize=0, encoding="utf-8", errors="replace")
                buf = ""; last = ""
                while True:
                    if self._stop: p.terminate(); return -999, ""
                    ch = p.stdout.read(1)
                    if not ch:
                        if p.poll() is not None: break
                        continue
                    if ch in ("\r", "\n"):
                        line = buf.strip(); buf = ""
                        if not line: continue
                        last = line
                        m = re_.search(r"(\d+\.?\d*)%\s+of\s+([\d\.]+\S*)", line)
                        if m and "download" in line.lower():
                            pct = float(m.group(1))
                            self.progress(pct/100*0.13+0.01,
                                         f"Đang tải... {pct:.0f}% ({m.group(2)})")
                    else: buf += ch
                p.wait(); return p.returncode, last

            rc, last = dl(ytdlp + ["-f", "best[ext=mp4]/best", "-o", str(out_path),
                                    "--no-playlist", "--progress", url])
            if rc == -999: return False
            if rc != 0:
                rc2, last = dl(ytdlp + ["-o", str(out_path), "--progress", url])
                if rc2 == -999: return False
                if rc2 != 0: raise RuntimeError(last[-150:])

            # Tìm file video đã tải — KHÔNG dùng mtime vì yt-dlp bỏ qua
            # download nếu file đã tồn tại (mtime cũ hơn SRT/JSON từ lần
            # chạy trước → sorted by mtime chọn nhầm file).
            # Ưu tiên: out_path chính xác → glob "_original.*" → glob video.
            VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".avi"}
            if out_path.exists():
                vid = out_path
            else:
                cands = [p for p in out_dir.iterdir()
                         if p.suffix.lower() in VIDEO_EXT and "_original" in p.name]
                if not cands:
                    cands = [p for p in out_dir.iterdir()
                             if p.suffix.lower() in VIDEO_EXT]
                if not cands:
                    raise RuntimeError("Không tìm thấy file video sau khi tải")
                vid = max(cands, key=lambda p: p.stat().st_mtime)
            self.progress(0.14, f"Tải xong! ({vid.stat().st_size//1024//1024} MB)")
            self.log(f"  ✅ {vid.name}", SUCCESS)

            # 2. AUDIO
            self.step(1); self.progress(0.15, "Tách audio...")
            self.log("🎵 Tach audio...", ACCENT2)
            wav = vid.with_suffix(".wav")
            subprocess.run(["ffmpeg", "-y", "-i", str(vid),
                            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                            str(wav)], capture_output=True)
            self.log("  ✅ Tach audio xong", SUCCESS)

            # 3. WHISPER
            self.step(2); self.progress(0.20, "Đang nhận dạng giọng nói...")
            segs = None

            if use_groq and groq_client:
                self.log(f"🤖 Whisper qua Groq API (cloud)...", ACCENT2)
                t0 = time.time()
                try:
                    segs = groq_client.transcribe(
                        str(wav), language="zh",
                        log=lambda m: self.log(f"  {m}", WARN))
                    self.progress(0.57, f"Groq Whisper xong: {len(segs)} đoạn ✅")
                    self.log(f"  ✅ {len(segs)} doan (Groq, {time.time()-t0:.1f}s)", SUCCESS)
                except Exception as e:
                    self.log(f"  ⚠ Groq transcribe lỗi ({e}) — chuyển sang "
                            f"Whisper local (offline)...", WARN)
                    segs = None

            if segs is None:
                # ── Nhánh Whisper LOCAL (offline) — y nguyên code gốc ──
                self.progress(0.20, "Whisper đang tải model...")
                self.log(f"🤖 Whisper local ({model})...", ACCENT2)
                dur_r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                        "format=duration", "-of",
                                        "default=noprint_wrappers=1:nokey=1", str(wav)],
                                       capture_output=True, text=True)
                try: aud_dur = float(dur_r.stdout.strip())
                except: aud_dur = 0
                speed = {"tiny": 40, "base": 15, "small": 8, "medium": 4, "large": 2}
                est = aud_dur / speed.get(model, 10) if aud_dur > 0 else 120

                import whisper as _w
                _ev = threading.Event(); _res = [None, None]
                def _wrun():
                    try:
                        _res[0] = _w.load_model(model).transcribe(
                            str(wav), language="zh", task="transcribe", verbose=False)
                    except Exception as e: _res[1] = e
                    finally: _ev.set()
                threading.Thread(target=_wrun, daemon=True).start()
                old_err = sys.stderr; sys.stderr = io.StringIO()
                ws = time.time()
                while not _ev.wait(timeout=0.5):
                    if self._stop: sys.stderr = old_err; _ev.wait(); return False
                    el = time.time() - ws; fr = min(el / max(est, 1), 0.97)
                    bar = "█" * int(fr*18) + "░" * (18 - int(fr*18))
                    self.progress(0.20 + fr*0.35, f"Whisper [{bar}] {int(fr*100)}%")
                sys.stderr = old_err
                if _res[1]: raise _res[1]
                segs = _res[0].get("segments", [])
                self.progress(0.57, f"Whisper xong: {len(segs)} đoạn ✅")
                self.log(f"  ✅ {len(segs)} doan (local)", SUCCESS)

            wav.unlink(missing_ok=True)

            # 4. DỊCH
            self.step(3); self.progress(0.58, "Bắt đầu dịch...")
            translated = []

            if use_groq and groq_client:
                self.log("🌐 Dịch qua Groq LLM (cả batch, có ngữ cảnh)...", ACCENT2)
                t0 = time.time()
                try:
                    texts = [seg["text"].strip() for seg in segs if seg["text"].strip()]
                    vi_list = groq_client.translate_batch(
                        texts, source_lang="zh", target_lang="vi",
                        log=lambda m: self.log(f"  {m}", WARN))
                    idx = 0
                    for seg in segs:
                        txt = seg["text"].strip()
                        if not txt: continue
                        translated.append({"start": seg["start"], "end": seg["end"],
                                           "zh": txt, "vi": vi_list[idx]})
                        idx += 1
                    self.progress(0.93, f"Groq dịch xong {len(translated)} đoạn")
                    self.log(f"  ✅ Dich xong {len(translated)} doan "
                            f"(Groq, {time.time()-t0:.1f}s)", SUCCESS)
                except Exception as e:
                    self.log(f"  ⚠ Groq dịch lỗi ({e}) — chuyển sang "
                            f"GoogleTranslator (offline-ish, dịch từng câu)...", WARN)
                    translated = []

            if not translated:
                # ── Nhánh GoogleTranslator — y nguyên code gốc ──
                self.log("🌐 Dich qua GoogleTranslator (từng câu)...", ACCENT2)
                from deep_translator import GoogleTranslator
                tr = GoogleTranslator(source="zh-CN", target="vi")
                n2 = len(segs)
                for si, seg in enumerate(segs):
                    if self._stop: return False
                    txt = seg["text"].strip()
                    if not txt: continue
                    try: vi = tr.translate(txt); time.sleep(0.25)
                    except: vi = txt
                    translated.append({"start": seg["start"], "end": seg["end"],
                                       "zh": txt, "vi": vi})
                    self.progress(0.58 + 0.35*(si/max(n2, 1)), f"Dịch {si+1}/{n2} đoạn")
                self.log(f"  ✅ Dich xong {len(translated)} doan (GoogleTranslator)", SUCCESS)

            # 5. LƯU SRT
            self.step(4); self.progress(0.95, "Lưu SRT...")
            stem = vid.stem.replace("_original", "")
            (out_dir / f"{stem}_segments.json").write_text(
                json.dumps(translated, ensure_ascii=False, indent=2), encoding="utf-8")
            def fmt(s):
                ms = int(s*1000); h, ms = divmod(ms, 3600000)
                m2, ms = divmod(ms, 60000); sc, ms = divmod(ms, 1000)
                return f"{h:02d}:{m2:02d}:{sc:02d},{ms:03d}"
            lines = []
            for i2, seg in enumerate(translated, 1):
                lines += [str(i2), f"{fmt(seg['start'])} --> {fmt(seg['end'])}", seg["vi"], ""]
            srt = out_dir / f"{stem}_vi.srt"
            srt.write_text("\n".join(lines), encoding="utf-8")
            self.progress(1.0, "Hoàn thành ✅")
            self.log(f"✨ XONG!  SRT: {srt.name}", SUCCESS)
            self.log("   → Import SRT vao CapCut", ACCENT2)
            return True
        except Exception as e:
            import traceback
            self.log(f"❌ Lỗi: {e}", ERR)
            self.log(traceback.format_exc(), ERR)
            return False