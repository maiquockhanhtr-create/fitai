# FITAI — Bản triển khai Web công khai (Đã siết bảo mật)

## 1. Bảo mật đã tích hợp sẵn
- **Giấu sạch API Key & bỏ ô nhập key từ trình duyệt:** Key chỉ lấy từ biến môi trường trên máy chủ (`.env` hoặc Environment Variables trên Cloud).
- **Giới hạn tần suất gọi (Rate Limit theo IP):** Mặc định tối đa `8` lượt / `10 phút` / IP (`RATE_LIMIT_PER_WINDOW`, `RATE_LIMIT_WINDOW_SEC`), chống gọi dồn làm tốn tài nguyên/credit.
- **Khóa CORS theo tên miền khi lên mạng thật:** Đặt `ALLOWED_ORIGINS=https://ten-mien-cua-ban.com` trong biến môi trường khi deploy.
- **Ẩn chi tiết lỗi nội bộ & thêm Security Headers:** Không lộ thông tin hệ thống hay lỗi nhà cung cấp chi tiết ra giao diện người dùng; không lưu ảnh lâu dài trên đĩa.

## 2. Đưa lên Web cho người khác dùng thử
1. Đẩy thư mục `fitai_project` lên GitHub (đảm bảo file `.env` không được đẩy lên).
2. Tạo dịch vụ Web Python trên Render / Railway / Fly.io trỏ lệnh khởi chạy:
   ```bash
   uvicorn server:app --host 0.0.0.0 --port $PORT
   ```
3. Khai báo biến môi trường trên nền tảng host:
   - `ALLOWED_ORIGINS` = link/tên miền web của bạn
   - `FASHN_API_KEY` (hoặc `HF_TOKEN`) nếu có
   - `RATE_LIMIT_PER_WINDOW` = `8`
   - `RATE_LIMIT_WINDOW_SEC` = `600`

## 3. Lộ trình sau này làm App
- Giữ nguyên server FastAPI làm API trung tâm (`/api/tryon`), sau đó kết nối ứng dụng Flutter / React Native / WebView tới URL HTTPS của web này.
