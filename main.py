#!/usr/bin/env python3
"""
DouyinViet - Tu dong tai, dich, long tieng va them phu de tieng Viet cho video Douyin
"""

import os
import sys
import json
import time
import shutil
import asyncio
import subprocess
import argparse
from pathlib import Path
from datetime import timedelta

class C:
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    CYAN   = "\033[96m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"

def log(msg, color=C.RESET):  print(f"{color}{msg}{C.RESET}")
def ok(msg):   log(f"  OK  {msg}", C.GREEN)
def warn(msg): log(f"  !!  {msg}", C.YELLOW)
def err(msg):  log(f"  XX  {msg}", C.RED)
def step(msg): log(f"\n{'─'*50}\n  {msg}", C.CYAN)

try:
    from groq_client import GroqClient, GroqAllKeysFailedError
    _GROQ_AVAILABLE = True
except ImportError:
    _GROQ_AVAILABLE = False

_GROQ_CLIENT = None  # khởi tạo 1 lần, dùng chung cho cả batch CLI (giữ
                     # trạng thái xoay vòng/cooldown key xuyên suốt)

def _get_groq_client():
    global _GROQ_CLIENT
    if _GROQ_CLIENT is None:
        _GROQ_CLIENT = GroqClient()
    return _GROQ_CLIENT

# ─── Tim lenh yt-dlp ─────────────────────────────────────────────
def get_ytdlp_cmd():
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"], capture_output=True)
    if r.returncode == 0:
        return [sys.executable, "-m", "yt_dlp"]
    return None

# ─── Kiem tra dependencies ───────────────────────────────────────
def check_deps(need_tts=False):
    step("Kiem tra cong cu can thiet...")
    missing = []

    if shutil.which("ffmpeg"):
        ok("ffmpeg da san sang")
    else:
        err("ffmpeg chua cai -> Tai tai https://ffmpeg.org/download.html roi them vao PATH")
        missing.append("ffmpeg")

    if get_ytdlp_cmd():
        ok("yt-dlp da san sang")
    else:
        err("yt-dlp chua cai -> pip install yt-dlp")
        missing.append("yt-dlp")

    packages = {
        "whisper":         "pip install openai-whisper",
        "deep_translator": "pip install deep-translator",
        "tqdm":            "pip install tqdm",
    }
    if need_tts:
        packages["edge_tts"] = "pip install edge-tts"

    for pkg, hint in packages.items():
        try:
            __import__(pkg)
            ok(f"{pkg} da san sang")
        except ImportError:
            err(f"{pkg} chua cai -> {hint}")
            missing.append(pkg)

    if missing:
        log("\nCai tat ca bang lenh sau:", C.YELLOW)
        log("pip install openai-whisper deep-translator tqdm edge-tts yt-dlp", C.YELLOW)
        sys.exit(1)

# ─── Tai video ───────────────────────────────────────────────────
def download_video(url: str, output_dir: Path) -> Path:
    step(f"Dang tai video: {url[:60]}...")
    output_dir.mkdir(parents=True, exist_ok=True)
    ytdlp = get_ytdlp_cmd()

    r = subprocess.run(
        ytdlp + ["--print", "%(title).50s", "--no-download", url],
        capture_output=True, text=True
    )
    title = r.stdout.strip() or "video"
    title = "".join(c for c in title if c.isalnum() or c in "_- ")[:40].strip() or "video"
    out_path = output_dir / f"{title}_original.mp4"

    cmd = ytdlp + ["-f", "best[ext=mp4]/best", "-o", str(out_path), "--no-playlist", url]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        cmd2 = ytdlp + ["-o", str(out_path), url]
        proc2 = subprocess.run(cmd2, capture_output=True, text=True)
        if proc2.returncode != 0:
            raise RuntimeError(f"Loi tai video:\n{proc2.stderr}")

    found = list(output_dir.glob(f"{title}_original*"))
    if not found:
        found = sorted(output_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    if not found:
        raise RuntimeError("Khong tim thay file da tai")

    f = found[0]
    ok(f"Da tai: {f.name} ({f.stat().st_size // 1024 // 1024} MB)")
    return f

# ─── Tach audio ──────────────────────────────────────────────────
def extract_audio(video_path: Path) -> Path:
    step("Tach audio tu video...")
    audio_path = video_path.with_suffix(".wav")
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(audio_path)
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Loi tach audio:\n{proc.stderr}")
    ok(f"Audio: {audio_path.name}")
    return audio_path

# ─── Transcribe bang Whisper ─────────────────────────────────────
def transcribe(audio_path: Path, model_size: str = "base", use_groq: bool = False) -> list:
    if use_groq and _GROQ_AVAILABLE:
        step("Nhan dang giong noi qua Groq API (cloud)...")
        try:
            client = _get_groq_client()
            segments = client.transcribe(str(audio_path), language="zh",
                                         log=lambda m: warn(m))
            ok(f"Nhan dang xong (Groq): {len(segments)} doan")
            return segments
        except Exception as e:
            warn(f"Groq transcribe loi ({e}) -> chuyen sang Whisper local...")
    elif use_groq and not _GROQ_AVAILABLE:
        warn("groq_client.py khong tim thay -> dung Whisper local.")

    step(f"Nhan dang giong noi (Whisper {model_size}, local)...")
    warn("Buoc nay mat vai phut tuy do dai video va CPU")
    import whisper
    model = whisper.load_model(model_size)
    result = model.transcribe(str(audio_path), language="zh", task="transcribe", verbose=False)
    segments = result.get("segments", [])
    ok(f"Nhan dang xong (local): {len(segments)} doan")
    return segments

# ─── Dich sang tieng Viet ────────────────────────────────────────
def translate_segments(segments: list, use_groq: bool = False) -> list:
    if use_groq and _GROQ_AVAILABLE:
        step("Dich qua Groq LLM (ca batch, co ngu canh)...")
        try:
            client = _get_groq_client()
            texts = [seg["text"].strip() for seg in segments if seg["text"].strip()]
            vi_list = client.translate_batch(texts, source_lang="zh", target_lang="vi",
                                             log=lambda m: warn(m))
            translated = []; idx = 0
            for seg in segments:
                text_zh = seg["text"].strip()
                if not text_zh:
                    continue
                translated.append({"start": seg["start"], "end": seg["end"],
                                   "zh": text_zh, "vi": vi_list[idx]})
                idx += 1
            ok(f"Dich xong (Groq) {len(translated)} doan")
            return translated
        except Exception as e:
            warn(f"Groq dich loi ({e}) -> chuyen sang GoogleTranslator...")
    elif use_groq and not _GROQ_AVAILABLE:
        warn("groq_client.py khong tim thay -> dung GoogleTranslator.")

    step("Dich tieng Trung -> tieng Viet (GoogleTranslator, tung cau)...")
    from deep_translator import GoogleTranslator
    from tqdm import tqdm

    translator = GoogleTranslator(source="zh-CN", target="vi")
    translated = []

    for seg in tqdm(segments, desc="  Dich", ncols=60):
        text_zh = seg["text"].strip()
        if not text_zh:
            continue
        try:
            text_vi = translator.translate(text_zh)
            time.sleep(0.3)
        except Exception as e:
            warn(f"Loi dich: {e}")
            text_vi = text_zh

        translated.append({
            "start": seg["start"],
            "end":   seg["end"],
            "zh":    text_zh,
            "vi":    text_vi,
        })

    ok(f"Dich xong (GoogleTranslator) {len(translated)} doan")
    return translated

# ─── Tao SRT ─────────────────────────────────────────────────────
def srt_time(seconds: float) -> str:
    ms = int(seconds * 1000)
    h, ms  = divmod(ms, 3600000)
    m, ms  = divmod(ms, 60000)
    s, ms  = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def write_srt(segments: list, out_path: Path):
    step("Tao file phu de .srt...")
    lines = []
    for i, seg in enumerate(segments, 1):
        lines += [str(i), f"{srt_time(seg['start'])} --> {srt_time(seg['end'])}", seg["vi"], ""]
    out_path.write_text("\n".join(lines), encoding="utf-8")
    ok(f"Phu de: {out_path.name}")
    return out_path

# ─── LAM MO SUB TRUNG GOC ────────────────────────────────────────
def blur_original_subs(video_path: Path, output_path: Path, blur_height_pct: float = 0.18) -> Path:
    """
    Lam mo vung duoi cua video (noi thuong co sub Trung).
    blur_height_pct: ty le chieu cao vung can mo (mac dinh 18% phia duoi)
    """
    step(f"Lam mo sub Trung goc (vung duoi {int(blur_height_pct*100)}% video)...")

    # Lay kich thuoc video
    probe = subprocess.run([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json", str(video_path)
    ], capture_output=True, text=True)

    info = json.loads(probe.stdout)
    w = info["streams"][0]["width"]
    h = info["streams"][0]["height"]
    blur_h = int(h * blur_height_pct)
    blur_y = h - blur_h

    # Filter: cat vung sub, blur manh, overlay len video goc
    vf = (
        f"[0:v]crop={w}:{blur_h}:0:{blur_y},boxblur=luma_radius=25:luma_power=3[blurred];"
        f"[0:v][blurred]overlay=0:{blur_y}"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vf", vf,
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "copy",
        str(output_path)
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Loi blur sub:\n{proc.stderr[-400:]}")
    ok(f"Da lam mo sub Trung: {output_path.name}")
    return output_path

# ─── LONG TIENG VIET (edge-tts) ──────────────────────────────────
VOICES = {
    "nu":  "vi-VN-HoaiMyNeural",    # Giong nu mien Nam
    "nam": "vi-VN-NamMinhNeural",   # Giong nam mien Nam
}

async def _gen_tts_segment(text: str, voice: str, out_path: Path):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(out_path))

def generate_tts_audio(segments: list, output_dir: Path, voice_key: str = "nu") -> Path:
    """
    Tao file audio tieng Viet tu cac doan dich.
    Moi doan duoc dat dung vi tri thoi gian tuong ung trong video.
    """
    step(f"Tao giong doc tieng Viet (edge-tts, giong {voice_key})...")
    voice = VOICES.get(voice_key, VOICES["nu"])

    tmp_dir = output_dir / "_tts_tmp"
    tmp_dir.mkdir(exist_ok=True)

    # Lay thoi luong video tu segment cuoi
    total_duration = max(seg["end"] for seg in segments) + 1.0

    # Tao file am thanh im lang co chieu dai bang video
    silent_path = tmp_dir / "silent.wav"
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono",
        "-t", str(total_duration),
        "-acodec", "pcm_s16le",
        str(silent_path)
    ], capture_output=True)

    # Tao TTS cho tung doan
    segment_files = []
    print("  Tao TTS tung doan...")
    for i, seg in enumerate(segments):
        text_vi = seg["vi"].strip()
        if not text_vi:
            continue
        seg_path = tmp_dir / f"seg_{i:04d}.mp3"
        try:
            asyncio.run(_gen_tts_segment(text_vi, voice, seg_path))
            segment_files.append((seg["start"], seg_path))
            time.sleep(0.1)
        except Exception as e:
            warn(f"Loi TTS doan {i}: {e}")

    if not segment_files:
        raise RuntimeError("Khong tao duoc file TTS nao")

    # Dung ffmpeg amix de gop cac clip TTS vao dung thoi diem
    # Tao filter_complex
    inputs = ["-i", str(silent_path)]
    filter_parts = ["[0:a]"]   # silent lam nen
    
    for idx, (start_t, seg_path) in enumerate(segment_files):
        inputs += ["-i", str(seg_path)]
        inp_idx = idx + 1
        filter_parts.append(
            f"[{inp_idx}:a]adelay={int(start_t*1000)}|{int(start_t*1000)}[d{idx}]"
        )

    # Mix tat ca vao 1 track
    delay_labels = "".join(f"[d{i}]" for i in range(len(segment_files)))
    mix_count = len(segment_files) + 1  # silent + cac doan
    filter_str = ";".join(filter_parts[1:])  # delay filters
    filter_str += f";[0:a]{delay_labels}amix=inputs={mix_count}:duration=first:dropout_transition=0[aout]"

    tts_final = output_dir / "_tts_final.wav"
    cmd = (
        ["ffmpeg", "-y"]
        + inputs
        + ["-filter_complex", filter_str,
           "-map", "[aout]",
           "-acodec", "pcm_s16le", "-ar", "44100",
           str(tts_final)]
    )
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Loi gop TTS:\n{proc.stderr[-400:]}")

    # Don dep thu muc tam
    shutil.rmtree(tmp_dir, ignore_errors=True)
    ok(f"Tao giong doc xong: {tts_final.name}")
    return tts_final

def mix_tts_into_video(video_path: Path, tts_audio: Path, output_path: Path,
                        original_volume: float = 0.15) -> Path:
    """
    Gop TTS vao video.
    original_volume: am luong goc (0.0 = tat, 0.15 = giu nhe, 1.0 = giu nguyen)
    """
    step("Gop giong doc vao video...")
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(tts_audio),
        "-filter_complex",
        f"[0:a]volume={original_volume}[orig];[1:a]volume=1.0[tts];[orig][tts]amix=inputs=2:duration=first[aout]",
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k",
        str(output_path)
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Loi mix audio:\n{proc.stderr[-400:]}")
    ok(f"Da gop giong doc: {output_path.name}")
    return output_path

# ─── Burn phu de vao video ───────────────────────────────────────
def burn_subtitles(video_path: Path, srt_path: Path, output_path: Path, font_size: int = 22):
    step("Ghep phu de vao video...")

    style = (
        "FontName=Arial,"
        f"FontSize={font_size},"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,"
        "BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=25"
    )

    srt_str = str(srt_path.resolve()).replace("\\", "/")
    if sys.platform == "win32":
        srt_str = srt_str.replace(":", "\\:")

    vf = f"subtitles='{srt_str}':force_style='{style}'"

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vf", vf,
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "copy",
        str(output_path)
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Loi burn sub:\n{proc.stderr[-400:]}")
    ok(f"Video co phu de: {output_path.name}")
    return output_path

# ─── Xu ly chinh ─────────────────────────────────────────────────
def process_url(url: str, output_dir: Path, model_size: str,
                do_blur: bool = True, do_tts: bool = False,
                voice: str = "nu", orig_vol: float = 0.15, use_groq: bool = False):
    try:
        stem_dir = output_dir
        stem_dir.mkdir(parents=True, exist_ok=True)

        # 1. Tai video
        video_path = download_video(url, stem_dir)
        stem = video_path.stem.replace("_original", "")

        # 2. Tach audio & transcribe
        audio_path = extract_audio(video_path)
        segments   = transcribe(audio_path, model_size, use_groq=use_groq)
        translated = translate_segments(segments, use_groq=use_groq)
        audio_path.unlink(missing_ok=True)

        # Luu JSON
        (stem_dir / f"{stem}_segments.json").write_text(
            json.dumps(translated, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 3. Tao SRT
        srt_path = stem_dir / f"{stem}_vi.srt"
        write_srt(translated, srt_path)

        # 4. Lam mo sub Trung goc (neu can)
        working_video = video_path
        if do_blur:
            blurred = stem_dir / f"{stem}_blurred.mp4"
            blur_original_subs(working_video, blurred)
            working_video = blurred

        # 5. Tao giong doc tieng Viet (neu can)
        if do_tts:
            tts_audio  = generate_tts_audio(translated, stem_dir, voice)
            tts_mixed  = stem_dir / f"{stem}_with_tts.mp4"
            mix_tts_into_video(working_video, tts_audio, tts_mixed, orig_vol)
            tts_audio.unlink(missing_ok=True)
            working_video = tts_mixed

        # 6. Burn phu de tieng Viet
        final_path = stem_dir / f"{stem}_FINAL.mp4"
        burn_subtitles(working_video, srt_path, final_path)

        # Don dep file trung gian
        for tmp in ["_blurred.mp4", "_with_tts.mp4"]:
            p = stem_dir / f"{stem}{tmp}"
            p.unlink(missing_ok=True)

        log(f"\n{'='*50}", C.GREEN)
        log(f"  HOAN THANH!", C.GREEN)
        log(f"  => {final_path}", C.GREEN)
        log(f"{'='*50}", C.GREEN)
        return True

    except Exception as e:
        err(f"Loi xu ly {url}: {e}")
        import traceback; traceback.print_exc()
        return False

# ─── CLI ─────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="DouyinViet - Dich + long tieng video Douyin sang tieng Viet",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("urls", nargs="*", help="URL video Douyin")
    parser.add_argument("-f", "--file",    help="File .txt chua danh sach URL")
    parser.add_argument("-o", "--output",  default="./output", help="Thu muc output")
    parser.add_argument("-m", "--model",   default="base",
                        choices=["tiny","base","small","medium","large"],
                        help="Model Whisper (mac dinh: base)")
    parser.add_argument("--no-blur",       action="store_true",
                        help="Khong lam mo sub Trung goc")
    parser.add_argument("--tts",           action="store_true",
                        help="Them giong doc tieng Viet (edge-tts)")
    parser.add_argument("--voice",         default="nu", choices=["nu","nam"],
                        help="Giong doc: nu (mac dinh) hoac nam")
    parser.add_argument("--orig-vol",      type=float, default=0.15,
                        help="Am luong goc khi co TTS (0.0=tat han, 0.15=nhe, 1.0=giu nguyen)")
    parser.add_argument("--groq",          action="store_true",
                        help="Dung Groq API (cloud, nhanh) thay Whisper local + "
                             "GoogleTranslator. Can file groq_keys.txt. Tu dong "
                             "rot ve local neu Groq loi/het quota.")
    args = parser.parse_args()

    print("""
╔══════════════════════════════════════════╗
║   DouyinViet Automation Tool v2         ║
║   Douyin -> Blur + TTS + Phu de Viet   ║
╚══════════════════════════════════════════╝
""")

    check_deps(need_tts=args.tts)

    urls = list(args.urls)
    if args.file:
        fp = Path(args.file)
        if not fp.exists():
            err(f"Khong tim thay file: {args.file}"); sys.exit(1)
        extra = [l.strip() for l in fp.read_text().splitlines()
                 if l.strip() and not l.startswith("#")]
        urls += extra
        ok(f"Doc {len(extra)} URL tu {args.file}")

    if not urls:
        log("\nCach dung:")
        log("  # Chi phu de tieng Viet + lam mo sub Trung:", C.CYAN)
        log("  python main.py https://v.douyin.com/xxx", C.CYAN)
        log("\n  # Them giong doc tieng Viet (nu):", C.CYAN)
        log("  python main.py --tts https://v.douyin.com/xxx", C.CYAN)
        log("\n  # Giong doc nam, giu 10% am goc:", C.CYAN)
        log("  python main.py --tts --voice nam --orig-vol 0.1 url", C.CYAN)
        log("\n  # Bat nhieu video:", C.CYAN)
        log("  python main.py --tts -f urls.txt", C.CYAN)
        sys.exit(0)

    output_dir = Path(args.output)
    log(f"  Output      : {output_dir.resolve()}")
    log(f"  Model       : {args.model}")
    log(f"  Lam mo sub  : {'Tat' if args.no_blur else 'Bat'}")
    log(f"  Long tieng  : {'Bat - giong ' + args.voice if args.tts else 'Tat'}")
    if args.tts:
        log(f"  Am luong goc: {args.orig_vol}")
    log(f"  So video    : {len(urls)}")
    if args.groq:
        log(f"  Engine      : {'Groq API (cloud)' if _GROQ_AVAILABLE else 'Groq KHONG CO SAN -> dung local'}")

    success = 0
    for i, url in enumerate(urls, 1):
        log(f"\n{'─'*50}")
        log(f"  [{i}/{len(urls)}] {url[:70]}", C.BOLD)
        if process_url(
            url, output_dir, args.model,
            do_blur=not args.no_blur,
            do_tts=args.tts,
            voice=args.voice,
            orig_vol=args.orig_vol,
            use_groq=args.groq
        ):
            success += 1

    log(f"\n{'='*50}")
    log(f"  Ket qua: {success}/{len(urls)} thanh cong",
        C.GREEN if success == len(urls) else C.YELLOW)

if __name__ == "__main__":
    main()