"""groq_client.py — Tích hợp Groq API, DÙNG CHUNG cho toàn bộ pipeline.

2 việc chính:
  1. transcribe()      — thay Whisper local (CPU, chậm) bằng Groq Whisper
                          API (hosted, nhanh hơn nhiều, free tier rộng)
  2. translate_batch()  — thay GoogleTranslator dịch từng câu rời bằng
                          Groq LLM dịch cả batch có ngữ cảnh, tự nhiên hơn
  3. rewrite_title()    — (tuỳ chọn) viết lại caption gốc thành tiêu đề
                          giật tít tiếng Việt, phục vụ reup

Hỗ trợ NHIỀU API KEY (20-30 key) — xoay vòng (round-robin) + tự "nghỉ"
(cooldown) key nào dính rate-limit (HTTP 429), chuyển sang key khác ngay
trong cùng 1 request thay vì báo lỗi luôn. Vì free tier Groq giới hạn
request/phút khá chặt THEO TỪNG KEY, có nhiều key gần như loại bỏ hẳn vấn
đề rate-limit nếu xoay vòng đúng cách.

═══════════════════════════════════════════════════════════════════════
CÁCH DÙNG
═══════════════════════════════════════════════════════════════════════

1. Tạo file `groq_keys.txt` trong thư mục gốc project (cùng cấp app.py),
   mỗi dòng 1 API key, dòng bắt đầu bằng `#` bị bỏ qua:

       gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
       gsk_yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy
       gsk_zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz
       ... (20-30 dòng)

   Hoặc đặt biến môi trường GROQ_API_KEYS, các key cách nhau bằng dấu phẩy
   (ưu tiên cao hơn file nếu cả 2 cùng tồn tại).

   ⚠️ THÊM `groq_keys.txt` VÀO `.gitignore` — không commit key thật lên git.

2. Dùng trong code:

       from groq_client import GroqClient
       client = GroqClient()

       # Transcribe — CÙNG FORMAT segments với openai-whisper local,
       # nên thay thế trực tiếp được trong douyin_worker.py/main.py.
       segments = client.transcribe("audio.wav", language="zh")
       # → [{"start": 0.0, "end": 2.3, "text": "你好"}, ...]

       # Dịch cả batch 1 lần, có ngữ cảnh:
       texts = [s["text"] for s in segments]
       translated = client.translate_batch(texts, target_lang="vi")
       # → ["Xin chào", ...] — cùng độ dài, cùng thứ tự với texts

       # (Tuỳ chọn) viết lại tiêu đề giật tít cho reup:
       title = client.rewrite_title("caption gốc dài dòng...")
"""
import os
import json
import time
import threading
from pathlib import Path

import requests

API_BASE = "https://api.groq.com/openai/v1"
WHISPER_MODEL = "whisper-large-v3-turbo"
LLM_MODEL = "llama-3.3-70b-versatile"

KEYS_FILE_DEFAULT = Path(__file__).parent / "groq_keys.txt"


class GroqAllKeysFailedError(Exception):
    """Đã thử hết toàn bộ key (rate-limit hoặc lỗi mạng) mà vẫn không xong."""


# ═══════════════════════════════════════════════════════════════════
# Quản lý pool nhiều key: xoay vòng + cooldown khi bị rate-limit
# ═══════════════════════════════════════════════════════════════════
class _KeyPool:
    def __init__(self, keys):
        if not keys:
            raise ValueError(
                "Không có API key Groq nào. Tạo file groq_keys.txt (mỗi dòng "
                "1 key) hoặc đặt biến môi trường GROQ_API_KEYS.")
        # Loại key trùng, giữ thứ tự
        self._keys = list(dict.fromkeys(k.strip() for k in keys if k.strip()))
        self._idx = 0
        self._cooldown_until = {k: 0.0 for k in self._keys}
        self._lock = threading.Lock()

    def __len__(self):
        return len(self._keys)

    def get_key(self):
        """Trả về 1 key — ưu tiên key không đang cooldown, xoay vòng đều."""
        with self._lock:
            now = time.time()
            n = len(self._keys)
            for _ in range(n):
                k = self._keys[self._idx]
                self._idx = (self._idx + 1) % n
                if self._cooldown_until[k] <= now:
                    return k
            # Tất cả đều đang cooldown → vẫn trả 1 key (key hết cooldown
            # sớm nhất), để caller tự quyết định thử tiếp hay dừng.
            return min(self._cooldown_until, key=self._cooldown_until.get)

    def mark_rate_limited(self, key, cooldown_sec=60):
        with self._lock:
            self._cooldown_until[key] = time.time() + cooldown_sec

    def n_available(self):
        now = time.time()
        with self._lock:
            return sum(1 for t in self._cooldown_until.values() if t <= now)


def _load_keys(keys_file=None):
    """Thứ tự ưu tiên: biến môi trường GROQ_API_KEYS → file (tham số hoặc
    groq_keys.txt mặc định)."""
    env = os.environ.get("GROQ_API_KEYS", "")
    if env.strip():
        return [k for k in env.split(",") if k.strip()]
    f = Path(keys_file) if keys_file else KEYS_FILE_DEFAULT
    if f.exists():
        return [
            line.strip() for line in f.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    return []


def _parse_json_array(text):
    """Parse JSON array từ output LLM — có fallback nếu model lỡ bọc thêm
    code fence ```json ... ``` hoặc thêm chữ thừa trước/sau mảng."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.strip()
    try:
        arr = json.loads(text)
        if isinstance(arr, list):
            return [str(x).strip() for x in arr]
    except Exception:
        pass
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            arr = json.loads(text[start:end + 1])
            if isinstance(arr, list):
                return [str(x).strip() for x in arr]
        except Exception:
            pass
    return None


# ═══════════════════════════════════════════════════════════════════
# Client chính
# ═══════════════════════════════════════════════════════════════════
class GroqClient:
    def __init__(self, keys_file=None, max_retries_per_request=None):
        keys = _load_keys(keys_file)
        self.pool = _KeyPool(keys)
        # Mặc định: thử tối đa = số key đang có (xoay hết 1 vòng) trước
        # khi báo lỗi hẳn, nhưng tối thiểu 3 lần kể cả ít key.
        self.max_retries = max_retries_per_request or max(3, len(self.pool))

    # ── 1. Whisper transcription ─────────────────────────────────
    def transcribe(self, audio_path, language="zh", model=WHISPER_MODEL, log=None):
        """Trả về list[{"start","end","text"}] — CÙNG FORMAT với
        `whisper.load_model(...).transcribe(...)["segments"]` của
        openai-whisper local, để code gọi phía sau (douyin_worker.py,
        main.py) không cần đổi gì ngoài chỗ gọi transcribe.
        """
        log = log or (lambda *a, **k: None)
        last_err = None
        for attempt in range(self.max_retries):
            key = self.pool.get_key()
            try:
                with open(audio_path, "rb") as f:
                    r = requests.post(
                        f"{API_BASE}/audio/transcriptions",
                        headers={"Authorization": f"Bearer {key}"},
                        files={"file": (Path(audio_path).name, f, "audio/wav")},
                        data={"model": model, "language": language,
                              "response_format": "verbose_json"},
                        timeout=120,
                    )
                if r.status_code == 429:
                    self.pool.mark_rate_limited(key)
                    log(f"  ⚠ Key Groq #{attempt+1} bị rate-limit, đổi key khác "
                        f"({self.pool.n_available()}/{len(self.pool)} key còn rảnh)...")
                    continue
                r.raise_for_status()
                data = r.json()
                segs = data.get("segments") or []
                return [{"start": float(s.get("start", 0.0)),
                        "end": float(s.get("end", 0.0)),
                        "text": (s.get("text") or "").strip()} for s in segs]
            except requests.exceptions.RequestException as e:
                last_err = e
                log(f"  ⚠ Lỗi mạng/Groq lần {attempt+1}: {e}")
                continue
        raise GroqAllKeysFailedError(
            f"Đã thử {self.max_retries} lần (xoay {len(self.pool)} key) đều "
            f"thất bại. Lỗi cuối: {last_err}")

    # ── 2. Dịch batch có ngữ cảnh ─────────────────────────────────
    def translate_batch(self, texts, source_lang="zh", target_lang="vi",
                        model=LLM_MODEL, batch_size=25, log=None):
        """Dịch danh sách câu CÙNG LÚC (có ngữ cảnh trước-sau, tự nhiên hơn
        dịch từng câu rời của GoogleTranslator). Trả về list cùng độ dài,
        cùng thứ tự với `texts`. Tự chia theo batch_size để tránh prompt
        quá dài / model trả thiếu dòng. Nếu 1 batch lỗi hẳn sau khi thử
        hết key, fallback giữ nguyên text gốc cho batch đó (không làm gãy
        cả pipeline vì 1 đoạn dịch lỗi).
        """
        log = log or (lambda *a, **k: None)
        out = []
        for i in range(0, len(texts), batch_size):
            chunk = texts[i:i + batch_size]
            out.extend(self._translate_chunk(chunk, source_lang, target_lang,
                                              model, log))
        return out

    def _translate_chunk(self, chunk, source_lang, target_lang, model, log):
        src_name = "Trung" if source_lang.lower().startswith("zh") else source_lang
        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(chunk))
        prompt = (
            f"Dịch chính xác {len(chunk)} câu sau từ tiếng {src_name} sang "
            f"tiếng Việt tự nhiên, đúng văn phong video ngắn mạng xã hội. "
            f"CHỈ trả về 1 JSON array gồm đúng {len(chunk)} chuỗi, đúng thứ "
            f"tự, KHÔNG thêm giải thích, KHÔNG đánh số trong chuỗi kết quả.\n\n"
            f"{numbered}"
        )
        last_err = None
        for attempt in range(self.max_retries):
            key = self.pool.get_key()
            try:
                r = requests.post(
                    f"{API_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {key}",
                            "Content-Type": "application/json"},
                    json={"model": model,
                          "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0.3},
                    timeout=60,
                )
                if r.status_code == 429:
                    self.pool.mark_rate_limited(key)
                    log(f"  ⚠ Key Groq bị rate-limit lúc dịch, đổi key khác...")
                    continue
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
                arr = _parse_json_array(content)
                if arr and len(arr) == len(chunk):
                    return arr
                last_err = f"model trả {len(arr) if arr else 0}/{len(chunk)} dòng"
            except requests.exceptions.RequestException as e:
                last_err = e
            except (KeyError, IndexError) as e:
                last_err = f"response không đúng format: {e}"
        log(f"  ❌ Dịch 1 batch ({len(chunk)} câu) lỗi sau {self.max_retries} "
            f"lần thử ({last_err}) — giữ nguyên text gốc cho batch này")
        return list(chunk)

    # ── 3. (Tuỳ chọn) viết lại tiêu đề giật tít cho reup ──────────
    def rewrite_title(self, caption, model=LLM_MODEL, log=None):
        """Viết lại caption gốc (TQ/Anh/...) thành 1 tiêu đề tiếng Việt
        ngắn gọn, tự nhiên kiểu mạng xã hội. Trả về caption gốc nếu lỗi —
        không làm gãy pipeline vì 1 tiêu đề lỗi.
        """
        log = log or (lambda *a, **k: None)
        prompt = (
            "Viết lại đoạn caption sau thành 1 tiêu đề tiếng Việt ngắn gọn "
            "(dưới 100 ký tự), tự nhiên kiểu mạng xã hội, giữ đúng nội dung "
            "chính, KHÔNG thêm dấu ngoặc kép, KHÔNG giải thích, CHỈ trả đúng "
            f"1 dòng tiêu đề:\n\n{caption[:500]}"
        )
        for attempt in range(self.max_retries):
            key = self.pool.get_key()
            try:
                r = requests.post(
                    f"{API_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {key}",
                            "Content-Type": "application/json"},
                    json={"model": model,
                          "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0.7},
                    timeout=30,
                )
                if r.status_code == 429:
                    self.pool.mark_rate_limited(key)
                    continue
                r.raise_for_status()
                title = r.json()["choices"][0]["message"]["content"].strip()
                title = title.strip('"').strip("'").strip()
                return title[:200] if title else caption
            except (requests.exceptions.RequestException, KeyError, IndexError) as e:
                log(f"  ⚠ Lỗi viết lại tiêu đề lần {attempt+1}: {e}")
                continue
        return caption  # fallback: giữ nguyên caption gốc