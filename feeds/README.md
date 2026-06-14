# feeds/ — dòng tin AI gần thời gian thực (tự động)

Thư mục này do `scripts/fetch_news.py` tạo ra (chạy bởi
`.github/workflows/fetch-news.yml`, mặc định mỗi 2 giờ).

- `latest.json` — dữ liệu máy đọc: danh sách tin từ nguồn sơ cấp/uy tín, kèm
  timestamp ISO chính xác, đã lọc theo cửa sổ thời gian và khử trùng lặp.
- `latest.md`   — bản tóm tắt cho người đọc, mới nhất lên đầu.

**Đừng sửa tay** hai file trên — chúng bị ghi đè mỗi lần chạy. Khi viết báo cáo
trong `reports/`, hãy đọc `latest.json` làm "nguồn sự thật" về *điều gì đã xảy
ra và vào lúc nào*, rồi đối chiếu chéo bằng web search (xem `CLAUDE.md`).

Hai file này nằm ngoài `reports/` nên **không kích hoạt** workflow gửi Telegram.
