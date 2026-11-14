# vtvgo-dl

[English](README.md)

Tải video, cả bộ phim, cả trang, xem lại chương trình truyền hình và ghi kênh trực tiếp từ [vtvgo.vn](https://vtvgo.vn) với **chất lượng cao nhất** (thường là 1080p), kèm **đầy đủ metadata**.

> Chỉ dùng cho mục đích cá nhân, học tập. Tôn trọng điều khoản sử dụng và bản quyền của VTVgo. Không phát tán lại nội dung đã tải.

## Tính năng

- **Chất lượng cao nhất**: script dò mọi luồng không mã hoá mà site cung cấp, kể cả file `master.m3u8` ẩn liệt kê tất cả các mức chất lượng. Luồng có độ phân giải và bitrate cao nhất được chọn.
- **Phim bộ**: link phim bộ tải toàn bộ các tập (`--episodes 1-5,8` để chọn tập), đặt tên `Tên phim - S01E05.mp4`.
- **Cả trang**: `/page/thoi-su` tải mọi mục video của trang đó (`--limit` giới hạn số video mỗi mục, `--recursive` tải cả trang con).
- **Đầy đủ metadata**:
  - tag MP4 (tiêu đề, tên phim, số tập, ngày, đạo diễn, diễn viên, mô tả, link)
  - ảnh bìa nhúng vào video
  - `.info.json` chứa dữ liệu API gốc
  - `.nfo` và `tvshow.nfo` cho Kodi / Jellyfin
  - poster, fanart, thumbnail kích thước gốc
  - `.description.txt`
  - file tổng hợp `videos.csv`
- **Kênh truyền hình**:
  - lịch phát sóng (`--epg`)
  - tải lại chương trình đã phát (`--program`)
  - ghi trực tiếp (`--duration`, hoặc bấm Ctrl+C để dừng)
  - video livestream / restream được tự động ghi lại
- **Nhanh, tải tiếp được**: tải song song bằng aria2c (không có thì dùng requests). Chạy lại đúng lệnh cũ để tải tiếp. Link lỗi được ghi vào `failed.txt`; `-a failed.txt` sẽ tải lại chúng.
- **Đăng nhập**: `--login` mở cửa sổ trình duyệt để *bạn tự* đăng nhập. Sau đó tải được cả video cần tài khoản hoặc cần mua gói.

## Cài đặt

```bash
pip install -r requirements.txt
```

Đặt `ffmpeg.exe` (và `aria2c.exe` nếu có) cạnh `vtvgo-dl.py`, hoặc cài sẵn trong PATH. Nếu thiếu ffmpeg, gói `imageio-ffmpeg` sẽ cung cấp một bản.

## Cách dùng

```bash
python vtvgo-dl.py https://vtvgo.vn/watch/576y --lang vi
```

```bash
python vtvgo-dl.py https://vtvgo.vn/watch/2E8I --episodes 1-10
```

```bash
python vtvgo-dl.py https://vtvgo.vn/page/thoi-su --limit 5
```

```bash
python vtvgo-dl.py -F https://vtvgo.vn/watch/576y
```

| Đầu vào | Ý nghĩa |
|---|---|
| `https://vtvgo.vn/watch/<id>` hoặc `<id>` | một video. Nếu là id phim bộ thì tải mọi tập. |
| `https://vtvgo.vn/page/<slug>` | mọi mục video trên trang đó |
| `https://vtvgo.vn/catalog/node/<id>` hoặc `node:<id>` | một danh sách |
| `https://vtvgo.vn/catalog/<id>` | một mục |
| `https://vtvgo.vn/channel/<id>` | một kênh: ghi trực tiếp, hoặc xem lại với `--program` |

| Tuỳ chọn | |
|---|---|
| `-o THƯ_MỤC` | thư mục lưu (mặc định `downloads`) |
| `-q best / worst / 720` | chất lượng (mặc định `best`) |
| `-F` | chỉ liệt kê các mức chất lượng |
| `-e 1-5,8` | các tập cần tải |
| `--whole-series` | link một tập sẽ tải cả bộ |
| `-n N` | tối đa N video mỗi danh sách / mục |
| `-r` | tải cả trang con |
| `--metadata-only` | chỉ lưu metadata và ảnh |
| `--no-images`, `--no-subs` | bỏ qua ảnh hoặc phụ đề |
| `-j 16` | số đoạn tải song song |
| `--no-aria2c` | tải bằng requests kể cả khi có aria2c |
| `--keep-parts` | giữ lại các đoạn đã tải sau khi ghép |
| `--proxy URL` | proxy cho mọi kết nối |
| `-a file.txt` | đọc danh sách link từ file (mỗi dòng một link, `#` là ghi chú) |
| `--login` / `--logout` | đăng nhập / xoá phiên đã lưu |
| `--lang vi` | thông báo tiếng Việt |
| `--no-color` | tắt màu |

## Kênh truyền hình, xem lại và trực tiếp

```bash
python vtvgo-dl.py --channels
```

```bash
python vtvgo-dl.py https://vtvgo.vn/channel/1 --epg --date yesterday
```

```bash
python vtvgo-dl.py https://vtvgo.vn/channel/1 --program "Thời sự" --date yesterday
```

```bash
python vtvgo-dl.py https://vtvgo.vn/channel/1 --duration 1h30m
```

| Tuỳ chọn | |
|---|---|
| `--channels` | liệt kê mọi kênh và id |
| `--epg` | lịch phát sóng của kênh theo `--date`. `✓` = đã phát xong, tải được. |
| `--date` | `YYYY-MM-DD`, `DD/MM` hoặc `yesterday` (mặc định: hôm nay, giờ Việt Nam) |
| `-p, --program` | xem lại: id chương trình lấy từ `--epg`, từ trong tên, hoặc `all`, cách nhau dấu phẩy |
| `-d, --duration` | trực tiếp: dừng sau `30m`, `1h30m`, `1:00:00`… (mặc định: đến khi bấm Ctrl+C) |
| `--live-from-start` | trực tiếp: ghi từ đoạn cũ nhất mà luồng còn giữ |

Nếu không có `--program`, link kênh sẽ ghi luồng trực tiếp. Bấm Ctrl+C để dừng, bản ghi vẫn được lưu.

Bản ghi trực tiếp và chương trình xem lại được lưu trong `downloads/Channels/<tên kênh>/`, kèm `.info.json` (thông tin kênh, các chương trình phát trong lúc ghi) và `.nfo`.

Khách xem lại được trong 24 giờ. Với tài khoản, nhiều kênh xem lại được tới 8 ngày. Kênh thuộc gói trả phí (VTVcab, SCTV…) cần mua gói đó.

## Đăng nhập và nội dung trả phí

```bash
python vtvgo-dl.py --login
```

Một cửa sổ trình duyệt sẽ mở vtvgo.vn. Bạn tự đăng nhập ở đó, xong thì cửa sổ tự đóng. Phiên (token và device id) được lưu vào `vtvgo-session.json` và có hiệu lực khoảng một ngày. Khi hết hạn, chạy lại `--login`.

Giữ file đó riêng tư vì nó có tác dụng như mật khẩu. `--logout` sẽ xoá file này. Script không bao giờ hỏi hay lưu mật khẩu của bạn.

## Giới hạn

- Một số video chỉ được phép xem tại Việt Nam (`OUTSIDE_VIETNAM`). Ở nước ngoài, cần `--proxy` với proxy Việt Nam, kể cả khi đã đăng nhập.
- Video và kênh có DRM (Widevine / FairPlay) sẽ được báo lỗi và bỏ qua.
- Ghi trực tiếp cần mạng ổn định. Những đoạn bị thiếu sẽ được báo là "hụt".

## Giấy phép

MIT © loooveee
