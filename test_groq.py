"""test_groq.py — Test groq_client.py với key thật.

CHẠY: đặt file này cùng thư mục với groq_client.py + groq_keys.txt, rồi:

    python test_groq.py

Mỗi bước in rõ "✅ PASS" hoặc "❌ FAIL" kèm lý do — không cần đọc traceback
để biết có chạy được không.
"""
import sys
import time
from pathlib import Path

from groq_client import GroqClient, GroqAllKeysFailedError


def step(title):
    print(f"\n{'─'*60}\n{title}\n{'─'*60}")


def ok(msg):
    print(f"✅ PASS — {msg}")


def fail(msg):
    print(f"❌ FAIL — {msg}")


# ═══════════════════════════════════════════════════════════════
# Bước 0: Load key
# ═══════════════════════════════════════════════════════════════
step("Bước 0: Load API key từ groq_keys.txt")
try:
    client = GroqClient()
    n = len(client.pool)
    if n >= 1:
        ok(f"Load được {n} key")
    else:
        fail("File groq_keys.txt rỗng hoặc không tìm thấy")
        sys.exit(1)
except Exception as e:
    fail(f"Không load được key: {e}")
    print("   → Kiểm tra file groq_keys.txt có cùng thư mục với file test này không,")
    print("     và mỗi dòng đúng 1 key (không có khoảng trắng/ký tự thừa).")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════
# Bước 1: Dịch batch (rẻ, nhanh, test được auth + parse JSON)
# ═══════════════════════════════════════════════════════════════
step("Bước 1: Test dịch batch (translate_batch)")
mau_zh = ["你好，今天天气很好。", "这个视频太好看了，一定要看到最后！", "谢谢大家的支持。"]
t0 = time.time()
try:
    ket_qua = client.translate_batch(mau_zh, source_lang="zh", target_lang="vi")
    dt = time.time() - t0
    print(f"   Input  ({len(mau_zh)} câu):", mau_zh)
    print(f"   Output ({len(ket_qua)} câu):", ket_qua)
    print(f"   Thời gian: {dt:.2f}s")
    if len(ket_qua) == len(mau_zh) and all(ket_qua) and ket_qua != mau_zh:
        ok("Dịch đúng số câu, có nội dung tiếng Việt, không phải bản gốc")
    else:
        fail("Kết quả không khớp số câu hoặc rỗng hoặc dịch y nguyên bản gốc "
             "(có thể model trả format sai, hoặc đã fallback do lỗi)")
except GroqAllKeysFailedError as e:
    fail(f"TOÀN BỘ key đều lỗi/rate-limit: {e}")
except Exception as e:
    fail(f"Lỗi không mong đợi: {e}")


# ═══════════════════════════════════════════════════════════════
# Bước 2: Viết lại tiêu đề giật tít (rewrite_title)
# ═══════════════════════════════════════════════════════════════
step("Bước 2: Test viết lại tiêu đề (rewrite_title)")
mau_caption = ("今天分享一个超级简单的家常菜做法，只需要三步，"
              "新手也能轻松做出餐厅级别的味道，赶紧收藏起来吧！#美食 #家常菜")
t0 = time.time()
try:
    title = client.rewrite_title(mau_caption)
    dt = time.time() - t0
    print(f"   Caption gốc: {mau_caption}")
    print(f"   Tiêu đề mới: {title}")
    print(f"   Thời gian: {dt:.2f}s")
    if title and title != mau_caption and len(title) < 150:
        ok("Tạo được tiêu đề mới, ngắn gọn, khác bản gốc")
    else:
        fail("Tiêu đề trả về giống bản gốc hoặc quá dài — có thể đã fallback do lỗi")
except Exception as e:
    fail(f"Lỗi không mong đợi: {e}")


# ═══════════════════════════════════════════════════════════════
# Bước 3: Transcribe (CẦN 1 file audio thật, bỏ qua nếu không có)
# ═══════════════════════════════════════════════════════════════
step("Bước 3: Test transcribe (Whisper API) — cần file audio mẫu")
AUDIO_TEST_PATH = "test_audio.wav"   # đổi path này thành file .wav thật của bạn
p = Path(AUDIO_TEST_PATH)
if not p.exists():
    print(f"   ⏭ BỎ QUA — không tìm thấy '{AUDIO_TEST_PATH}'.")
    print(f"   → Để test bước này: tách thử 1 đoạn audio ngắn (5-10s) bằng ffmpeg")
    print(f"     từ 1 video Douyin bất kỳ, đặt tên '{AUDIO_TEST_PATH}' cạnh file này:")
    print(f"     ffmpeg -i video.mp4 -t 10 -vn -ar 16000 -ac 1 {AUDIO_TEST_PATH}")
else:
    t0 = time.time()
    try:
        segs = client.transcribe(str(p), language="zh")
        dt = time.time() - t0
        print(f"   Số đoạn (segments): {len(segs)}")
        for s in segs[:3]:
            print(f"     [{s['start']:.1f}s-{s['end']:.1f}s] {s['text']}")
        print(f"   Thời gian: {dt:.2f}s")
        if segs and all("start" in s and "end" in s and "text" in s for s in segs):
            ok(f"Transcribe ra {len(segs)} đoạn, đúng format "
               f"(start/end/text) — nhanh hơn Whisper CPU rất nhiều nếu "
               f"{dt:.1f}s này << thời gian audio gốc")
        else:
            fail("Không có segment nào hoặc thiếu field start/end/text")
    except GroqAllKeysFailedError as e:
        fail(f"TOÀN BỘ key đều lỗi/rate-limit: {e}")
    except Exception as e:
        fail(f"Lỗi không mong đợi: {e}")


step("XONG — đọc lại các dòng ✅/❌ ở trên để biết bước nào ổn, bước nào cần xem lại")