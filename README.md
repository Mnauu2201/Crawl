# 🎬 DouyinViet Automation Tool

Tự động tải video Douyin → Nhận dạng tiếng Trung → Dịch sang tiếng Việt → Burn phụ đề vào video.

**100% miễn phí, chạy offline, không cần API key.**

---

## ⚙️ Cài đặt (1 lần duy nhất)

### Bước 1 – Cài ffmpeg

| Hệ điều hành | Lệnh |
|---|---|
| Ubuntu/Debian | `sudo apt install ffmpeg` |
| macOS | `brew install ffmpeg` |
| Windows | Tải tại [ffmpeg.org](https://ffmpeg.org/download.html), thêm vào PATH |

### Bước 2 – Cài Python packages

```bash
pip install -r requirements.txt
```

> Lần đầu chạy Whisper sẽ tự tải model (~140MB cho `base`). Cần internet.

---

## 🚀 Cách dùng

### Xử lý 1 video
```bash
python main.py https://www.douyin.com/video/ID_VIDEO
```

### Xử lý nhiều video cùng lúc
```bash
python main.py url1 url2 url3
```

### Xử lý từ file danh sách (khuyên dùng cho batch lớn)
```bash
# Thêm URL vào file urls.txt (mỗi dòng 1 URL)
python main.py -f urls.txt
```

### Tùy chọn thêm
```bash
# Dùng model tốt hơn (chậm hơn nhưng chính xác hơn)
python main.py -m small -f urls.txt

# Chỉ định thư mục output
python main.py -o ./videos_viet url1 url2
```

---

## 🤖 Chọn model Whisper

| Model | RAM cần | Tốc độ | Độ chính xác |
|---|---|---|---|
| `tiny` | ~1 GB | Rất nhanh | Thấp |
| `base` | ~1 GB | Nhanh | Trung bình ✅ |
| `small` | ~2 GB | Vừa | Tốt |
| `medium` | ~5 GB | Chậm | Rất tốt |
| `large` | ~10 GB | Rất chậm | Tốt nhất |

**Khuyên dùng:** `base` để thử, `small` để dùng thật.

---

## 📁 Output

Mỗi video tạo ra:
```
output/
├── TenVideo_FINAL.mp4          ← Video với phụ đề tiếng Việt (UP LÊN ĐÂY)
├── TenVideo_vi.srt             ← File phụ đề (có thể dùng riêng)
├── TenVideo_segments.json      ← Bản dịch chi tiết từng đoạn
└── TenVideo_original.mp4       ← Video gốc (có thể xóa)
```

---

## ❓ Xử lý lỗi thường gặp

**Video Douyin bị lỗi tải:** Thử link rút gọn từ app Douyin (nút Chia sẻ → Copy link)

**Dịch bị rate limit:** Tăng `time.sleep(0.3)` lên `time.sleep(1)` trong `main.py` dòng 100

**RAM không đủ cho Whisper:** Dùng model `tiny` hoặc `base`

**Phụ đề không khớp:** Nếu video có accent vùng miền → dùng model `small` hoặc lớn hơn

---

## 💡 Tips workflow

1. **Batch qua đêm:** Thêm 20-30 URL vào `urls.txt`, chạy trước khi ngủ
2. **Kiểm tra bản dịch:** Xem file `.json` để sửa thủ công trước khi burn
3. **Nhiều page khác nhau:** Tạo thư mục riêng cho mỗi niche: `-o ./cooking`, `-o ./beauty`...
