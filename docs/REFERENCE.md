> Tài liệu tham chiếu chi tiết, chuyển từ README cũ. Xem [README.md](../README.md) để có tổng quan. Một số đoạn có thể chưa theo kịp các thay đổi gần đây; khi mâu thuẫn, mã nguồn là nguồn đúng.

# Real-time Budget Management

## Cấu trúc thư mục

```text
.
├── main.py
├── common.py
├── dashboard.py
├── categories.py
├── categories.json
├── ledger_edits.py
├── dashboard/
│   ├── index.html
│   ├── app.css
│   ├── helpers.js
│   └── app.js
├── tests/
├── budget/
│   ├── __init__.py
│   ├── budget_history.py
│   └── income_budget.py
├── notifications/
│   ├── __init__.py
│   ├── budget_alerts.py
│   ├── notification_outbox.py
│   └── weekly_reports.py
├── monitoring/
│   ├── __init__.py
│   ├── history_health.py
│   ├── preflight.py
│   ├── runtime_health.py
│   ├── runtime_log.py
│   ├── sync_state.py
│   └── sync_status.py
├── storage/
│   ├── __init__.py
│   ├── db_backup.py
│   ├── file_lock.py
│   └── instance_lock.py
├── budget_state.sqlite3
├── backups/
└── logs/
```

## Thiết lập máy sạch (Windows)

Cần Python 3.11 trở lên. Từ thư mục dự án, tạo môi trường ảo và cài phần phụ thuộc của dự án:

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Tạo cấu hình cục bộ mà không ghi đè thiết lập hiện có:

```powershell
if (Test-Path -LiteralPath .env) {
    throw '.env already exists; refusing to overwrite existing configuration.'
}
Copy-Item -LiteralPath .env.example -Destination .env -NoClobber -ErrorAction Stop
```

Chỉnh sửa `.env` riêng tư. Đặt `BANK_KEY` và `DEVICE_KEY`; không đưa thông tin xác thực vào quản lý mã nguồn, phiếu công việc hoặc trò chuyện. Điều chỉnh `BUDGET_PERMONTH` nếu cần. `.env.example` chứa giá trị mẫu an toàn cho các thiết lập không bí mật.

## Security

`.gitignore` chỉ bảo vệ các tệp cục bộ chưa được theo dõi; đây không phải mã hóa (not encryption) và không tự untrack tệp đã được Git theo dõi. Không commit `.env`, DB SQLite cục bộ hoặc `backups/`.

**Backup chứa nội dung giao dịch, nhãn và các dòng audit đã lưu** và không được mã hóa. Nội dung có thể chứa tên người nhận hoặc số tài khoản. DB và các bản backup giữ dữ liệu này; OneDrive có thể đồng bộ chúng lên đám mây. Không chia sẻ các tệp này. Chuyển dữ liệu khỏi thư mục đồng bộ nếu không muốn lưu trên đám mây. Cân nhắc BitLocker để bảo vệ ổ đĩa cục bộ; BitLocker không mã hóa riêng bản sao đã đồng bộ lên OneDrive. Muốn bảo vệ file khi chia sẻ hoặc lưu đám mây, cần cơ chế mã hóa file và giữ khóa tách biệt, có kiểm tra khôi phục. Bản cập nhật này không tự mã hóa backup hoặc xóa `note` đã lưu.

Dashboard chỉ lắng nghe trên `127.0.0.1`, không phục vụ project directory, tệp DB, `backups/` hoặc `.env`. Server giữ kiểm tra Host, Origin, fetch context và header riêng cho API; header `X-Budget-Dashboard` không phải cơ chế đăng nhập. Ghi nhãn cần thêm khóa `DASHBOARD_WRITE_KEY`. Dùng trên máy cục bộ đáng tin cậy, qua RDP hoặc SSH tunnel với cùng cổng ở hai đầu, ví dụ `ssh -L 8765:127.0.0.1:8765 user@vps`. **Không mở cổng dashboard ra internet, LAN hoặc reverse proxy công khai.**

Chạy ứng dụng trực tiếp:

```powershell
python main.py
```

CLI đặt stdout và stderr thành UTF-8. `python main.py --help` và `python main.py -h` in tiếng Việt trên console Windows cp1252; không đọc `.env`, gọi API hoặc ghi dữ liệu.

Nhấn `Ctrl+C` để dừng.

## Chạy ứng dụng

```sh
python main.py
```

Ứng dụng kiểm tra giao dịch theo chu kỳ 30 phút; nhấn `Ctrl+C` để dừng. Lệnh này cần cấu hình API trong `.env` và kết nối dịch vụ ngân hàng/Bark.

Chạy kiểm tra offline, không gọi API hoặc dùng DB/backup thật:

```sh
python main.py --self-check
python dashboard.py --self-check
python -m monitoring.runtime_health --self-check
PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 python -m unittest discover -s tests -v
```

Các lệnh self-check dùng lại bộ `unittest`, không duy trì các bộ assertion riêng. Test chỉ dùng dữ liệu tạm, API giả lập và server dashboard trên loopback.

`BUDGET_ALERT_PERCENT` mặc định `80`; `NOTIFICATION_MODE` mặc định `all`, hoặc đặt `thresholds` để chỉ gửi khi chạm ngưỡng/vượt ngân sách. Cấu hình này không đổi tổng kết tuần. `load_dotenv()` đọc `BUDGET_PERMONTH` lúc khởi động; thay đổi chỉ được nhận sau khi khởi động lại. Mức tháng đổi trong ngày có hiệu lực từ ngày mai theo UTC+07:00, tính từ ngày đọc cấu hình, kể cả khi Bank API lỗi. Lịch ngân sách được commit riêng trước khi gọi API; lỗi xác thực giao dịch không hủy lịch này. Lần cấu hình đầu tiên có hiệu lực ngay trong ngày. Nếu không ghi được lịch vào DB, chu kỳ dừng trước khi gọi Bank API. Chỉ tổng kết tuần dùng mức ngân sách lịch sử đã biết; ngày chưa có dữ liệu được nêu là chưa xác định, không tính như ngân sách 0 hay kết luận tiết kiệm/vượt cả tuần.

Phản hồi Bank API/Bark bị giới hạn 1 MiB trước khi decode hoặc parse JSON. Timeout socket là 20 giây cho mỗi lần đọc. Toàn bộ yêu cầu có hạn chót 60 giây; phản hồi nhỏ giọt quá hạn này dừng chu kỳ với lỗi `<dịch vụ> phản hồi quá thời gian.` Khi lấy giao dịch từ Bank API lỗi (mạng, HTTP, JSON, `status` khác `success` hoặc sai cấu trúc), monitor thử lại tối đa 2 lần, cách nhau 5 giây, rồi mới tính kết quả. Nếu một lần thử lại thành công, chu kỳ chạy bình thường: không ghi lỗi `bank_api`, không tăng số lỗi liên tiếp và không gửi cảnh báo. Chỉ khi cả 3 lần đều lỗi, chu kỳ mới ghi lỗi. Mỗi lần thử lại có một dòng log. Lỗi ở bước kiểm tra dữ liệu (`validation`) không được thử lại. Giao dịch phải có `type` là chuỗi `IN` hoặc `OUT` (chấp nhận chữ thường và khoảng trắng ngoài). Thiếu hoặc sai `type` làm cả snapshot bị từ chối; không ghi một phần hay đánh dấu đồng bộ thành công. Nếu Bank API trả lại một `transactionID` đã lưu nhưng khác ngày hoặc số tiền, chu kỳ không bị hủy: bản ghi đã lưu được giữ nguyên, bản mới bị bỏ qua (kể cả giờ và nội dung), các giao dịch khác vẫn được lưu và cảnh báo như thường. Ứng dụng xếp một thông báo `Giao dịch bị ngân hàng thay đổi` cho mỗi cặp giá trị cũ/mới và ghi một dòng log ở mỗi chu kỳ còn lệch. Tổng chi có thể lệch so với sao kê cho tới khi được đối chiếu thủ công.

API trả ít nhất 50 giao dịch sẽ phát cảnh báo rủi ro chạm giới hạn; ứng dụng không đảm bảo phân trang hay đủ toàn bộ lịch sử. Sau một chu kỳ ghi DB thành công, monitor làm mới backup ngày khi có một trong ba điều kiện: chu kỳ lưu giao dịch OUT hoặc IN mới (kể cả mốc ban đầu); chưa có tệp `budget-<hôm nay>.sqlite3` (ngày theo UTC+07:00); hoặc tệp đó cũ hơn 6 giờ. Nếu không, chu kỳ bỏ qua backup, bước `backup` hiện `skipped` và không ghi log. Vì vậy nhãn hoặc chuyển IN sửa trên dashboard có thể vắng khỏi snapshot hôm nay tối đa 6 giờ. Khi làm mới, tệp hôm nay được thay bằng bản sao mới đã qua `integrity_check`, tệp của các ngày trước không đổi; chỉ giữ 7 snapshot hợp lệ, độc lập gần nhất. Backup chứa dữ liệu nhạy cảm, không mã hóa.

Backup đích có `-wal`, `-shm` hoặc `-journal` bị từ chối trước khi đọc hoặc thay tệp chính; cả symlink đứt cũng bị từ chối. Không tự xóa hoặc cách ly sidecar. Snapshot mới dùng `journal_mode=DELETE`, nên độc lập với WAL nguồn. Snapshot cũ độc lập được kiểm tra bằng `immutable=1`, không đổi nội dung hay tạo WAL/SHM; thư mục backup phải riêng tư, không có tiến trình khác ghi vào snapshot. Các snapshot cũ kèm sidecar được bỏ qua khi dọn số lượng, nên có thể còn hơn 7 tệp. `--status` báo backup `error` nếu có sidecar, không kết luận tệp bị hỏng.

Restore có deadline hợp tác 8 giây cho kiểm tra và sao chép SQLite. Mỗi integrity check tối đa 2 giây hoặc phần thời gian còn lại của restore. Timeout không bị coi là bằng chứng DB hỏng và không được publish target chưa hoàn tất. Callback SQLite có thể trễ một nhịp; giới hạn này không cưỡng chế được OS I/O treo. Chưa có worker process cho deadline cứng.

## Ngân sách dự kiến

`BUDGET_PERMONTH` là mức ngân sách của một tháng, tính bằng VND. Đặt số nguyên không âm trong `.env`, ví dụ `BUDGET_PERMONTH=15000000`. Monitor cần biến này khi khởi động.

Mức tháng được chia đều cho các ngày của tháng. Với tháng có `n` ngày và mức `A`, đặt `q, r = divmod(A, n)`. Ngày thứ `i` của tháng nhận `q + 1` nếu `i <= r`. Các ngày còn lại nhận `q`. Ví dụ, tháng 10/2026 có 31 ngày và mức 15.000.000: ngày 1 đến 30 nhận 483.871đ, ngày 31 nhận 483.870đ.

Lần ghi đầu tiên có hiệu lực ngay trong ngày. Khi đổi giá trị, mức mới có hiệu lực từ ngày mai. Các ngày đã qua giữ mức cũ. Monitor đọc biến khi khởi động, nên cần khởi động lại để nhận giá trị mới.

Trước ngày có mức tháng đầu tiên, ngân sách dùng mức ngày cũ trong bảng `budget_history`. Bảng này chỉ được đọc; code mới không ghi vào bảng này. Nếu không có mức nào cho ngày đó, ngân sách là chưa biết.

Cảnh báo và thông báo hằng ngày luôn dùng mức dự kiến này, vì chúng thuộc tháng đang chạy và tháng đang chạy chưa kết thúc. Báo cáo tuần và lịch trên dashboard dùng ngân sách hiệu lực: mức dự kiến này, hoặc ngân sách thật khi tháng đã kết thúc (xem "Hai loại ngân sách"). Báo cáo tuần đã gửi không đổi khi chuyển sang mức tháng.

`BUDGET_PERDAY` không còn được dùng. Nếu biến này còn trong môi trường, monitor ghi một dòng cảnh báo khi khởi động và bỏ qua giá trị.

## Xung đột ngày hoặc số tiền ngân hàng

Khi ngân hàng trả lại mã giao dịch đã lưu với ngày hoặc số tiền khác, monitor giữ bản đã lưu và ghi giá trị cũ/mới vào `transaction_conflicts` trong cùng transaction với cảnh báo. Mỗi cặp giá trị có một khóa `conflict-<sha256>`; thấy lại sẽ tăng `seen_count`, không gửi thêm cảnh báo trùng. Bảng được tạo khi chu kỳ gặp xung đột. Xung đột trước bản cập nhật này chưa được ghi vào bảng; bảng mới bắt đầu rỗng, không dựng lại từ thông báo cũ.

Liệt kê tối đa 50 xung đột đang mở, mới nhất trước; lệnh chỉ đọc:

```sh
python main.py --list-conflicts
```

**Chỉ nhận giá trị ngân hàng sau khi kiểm tra sao kê.** Lệnh sau thay đổi ngày và số tiền của giao dịch đã lưu, không chỉ đổi nội dung hiển thị:

```sh
python main.py --accept-conflict ID --confirm --reason "Đã đối chiếu sao kê"
```

Thay `ID` bằng mã giao dịch trong kết quả liệt kê. Lệnh chọn xung đột đang mở mới nhất của mã đó. Lý do cần 1–200 ký tự sau chuẩn hóa NFC và gộp khoảng trắng; không chứa ký tự điều khiển hoặc điều khiển hướng chữ. Nếu bản đã lưu đã đổi kể từ lúc ghi nhận xung đột, lệnh từ chối thay vì ghi đè.

Lệnh chạy an toàn khi monitor vẫn hoạt động: dùng `BEGIN IMMEDIATE`, chờ khóa SQLite tối đa 30 giây, không lấy khóa single-instance của monitor. Thao tác cập nhật bản ngân hàng, đánh dấu xung đột `accepted`, chuyển các xung đột mở khác của giao dịch thành `superseded` và ghi `edit_audit` trong một transaction; lỗi sẽ rollback. Chạy lại chu kỳ với giá trị đã nhận không tạo xung đột mới. Nếu tổng chi tuần thay đổi, bước báo cáo tuần kế tiếp xếp bản đính chính. Các dấu cảnh báo ngày đã gửi không được tính lại.

## Giao dịch IN

Monitor lưu giao dịch IN từ Bank API vào bảng `incoming_transactions`. Bảng này tách khỏi `transactions`, nên tổng chi không đổi.

IN không đổi cảnh báo hoặc thông báo hằng ngày. IN mới không gửi thông báo. IN chỉ đổi ngân sách khi tháng của nó kết thúc (xem "Hai loại ngân sách").

- Monitor kiểm tra IN chặt như OUT. Một IN sai định dạng làm dừng chu kỳ.
- Giờ và ghi chú còn trống được bổ sung ở lần đồng bộ sau. Giá trị đã lưu không bị ghi đè.
- Một mã giao dịch không được xuất hiện ở cả IN và OUT trong cùng lần đọc.
- Nếu ngân hàng đổi ngày hoặc số tiền của IN đã lưu, monitor giữ số cũ và ghi log. Monitor không ghi vào `transaction_conflicts`.

Bảng `income_state` lưu tháng bắt đầu theo dõi và ngày sớm nhất trong dữ liệu đầu tiên. Monitor tạo dòng này một lần.

Cửa sổ API chỉ có 50 giao dịch gần nhất, gồm cả IN và OUT. IN đã rời khỏi cửa sổ trước khi monitor đọc được sẽ không được lưu.

## Hai loại ngân sách

| | Ngân sách dự kiến | Ngân sách thật |
|---|---|---|
| Nguồn | Biến `BUDGET_PERMONTH` trong `.env` | Các giao dịch IN được tính cho tháng đó |
| Theo ngày | Chia đều cho mọi ngày của tháng | Chia đều cho mọi ngày của tháng |
| Trong tháng đang chạy | Là ngân sách hiệu lực: dùng cho cảnh báo, push hằng ngày, báo cáo tuần và nhãn "Vượt" trên lịch | Chỉ để so sánh: tiền vào, còn lại thật, chênh lệch so với dự kiến |
| Sau khi tháng kết thúc | Bị thay thế | Thay toàn bộ ngân sách dự kiến của tháng đó, ở mọi nơi dùng ngân sách |
| Thay đổi | Đổi biến thì có hiệu lực từ ngày mai | Đổi ngay khi có IN mới hoặc khi chọn "tính cho tháng sau" (xem [Chọn khoản IN tính cho tháng sau](#chọn-khoản-in-tính-cho-tháng-sau)) |

Ngân sách thật của tháng bằng tổng IN có ngày thuộc tháng đó, trừ phần chuyển sang tháng sau, cộng phần chuyển từ tháng trước. Phần dư chia theo quy tắc `q, r = divmod(...)` như ngân sách dự kiến. Tháng trước tháng bắt đầu theo dõi không có ngân sách thật; dashboard ghi "Chưa theo dõi tiền vào".

**Ngân sách hiệu lực.** Đây là số mà lịch, nhãn "Vượt", `days[].budget` của API và báo cáo tuần dùng.

- Một tháng đã kết thúc khi ngày hôm nay (UTC+07:00) từ ngày 1 của tháng sau trở đi. Tháng đang chạy và tháng tương lai chưa kết thúc, nên luôn dùng ngân sách dự kiến.
- Tháng đã kết thúc và từ tháng bắt đầu theo dõi trở đi dùng ngân sách thật, kể cả khi tháng bắt đầu thiếu dữ liệu IN hoặc không có IN nào (khi đó ngân sách thật bằng 0). Dashboard ghi chú tiền vào có thể thiếu ở tháng bắt đầu.
- Tháng đã kết thúc nhưng trước tháng bắt đầu theo dõi giữ ngân sách dự kiến hoặc mức ngày cũ.
- Ngân sách của tháng đã kết thúc vẫn đổi khi có IN đến muộn.
- `summary.budget_basis` là `"actual"` khi tháng đã kết thúc và từ tháng bắt đầu theo dõi trở đi, ngược lại là `"forecast"`. Thẻ tháng ghi "Ngân sách tháng (dự kiến)" hoặc "Ngân sách tháng (đã chốt theo tiền vào)". `/api/day` có `budget_basis` cho tháng của ngày chọn; nhãn ngày ghi "Ngân sách dự kiến ngày" hoặc "Ngân sách ngày (đã chốt)". `/api/today` luôn là `"forecast"`.
- Khi tháng đang xem là tháng bắt đầu theo dõi, `budget_basis` là `"actual"` và dữ liệu IN bắt đầu sau ngày 1, thẻ tháng ghi "Ngân sách đã chốt có thể thiếu tiền vào trước <ngày>." thay cho ghi chú "IN được ghi từ ngày ...".

**Báo cáo tuần.** Báo cáo tuần chỉ tạo bản điều chỉnh khi tổng chi đổi. Chỉ đổi cờ lịch sử (ví dụ sau `--reconcile-history`) không tạo bản điều chỉnh và không gửi thông báo. Chỉ đổi ngân sách tuần, ví dụ khi tháng kết thúc, không tạo bản điều chỉnh. Báo cáo tạo sau khi tháng kết thúc tự dùng ngân sách thật cho các ngày của tháng đó. Bản điều chỉnh luôn ghi ngân sách và cờ lịch sử mới nhất.

Thẻ tháng thêm một số: tiền vào tháng. Khi tháng đang xem là tháng bắt đầu theo dõi và dữ liệu IN đầu tiên bắt đầu sau ngày 1, thẻ ghi "IN được ghi từ ngày ...; tiền vào trước đó có thể thiếu" (hoặc ghi chú ngân sách đã chốt khi tháng đã kết thúc). Thẻ **Hôm nay** thêm tiền vào hôm nay. Panel ngày thêm tổng tiền vào và danh sách IN (giờ, ghi chú, số tiền, và "Tính cho tháng MM/YYYY" khi khoản đó được tính cho tháng sau). Ghi chú IN luôn hiển thị như văn bản.

Quy ước dấu và màu: số tiền OUT có dấu `−` (U+2212) và màu đỏ `--money-out`; số tiền IN có dấu `+` và màu xanh lá `--money-in` (bằng `--forest`). Quy ước này áp dụng cho ô lịch, dòng tổng tuần, thẻ tháng, thẻ hôm nay và panel ngày. Số 0 hiện không dấu. Tìm kiếm và bảng "Chi theo phân loại" giữ số tiền không dấu. `aria-label` và `title` của ô lịch dùng chữ "chi" và "tiền vào", không dùng dấu. Ô lịch vượt ngân sách chỉ có nhãn "Vượt"; không đổi nền, viền hay màu số tiền. Cả hai màu đạt tương phản ít nhất 4,5:1 trên `--surface`, `--selected` và `--forest-soft` ở hai theme.

Các trường API mới (số tiền là chuỗi số nguyên VND hoặc `null`):

| Endpoint | Trường mới |
|---|---|
| `GET /api/calendar` | `days[].income`; `summary.income`, `summary.budget_start_month`, `summary.budget_coverage_from`, `summary.budget_basis` |
| `GET /api/today` | `income`, `budget_basis` |
| `GET /api/day` | `budget_basis`, `income`: `{"total", "count", "transactions": [{"id", "amount", "time", "note", "budget_month", "next_month"}]}` (tối đa 50 dòng, mới nhất trước) hoặc `null` khi chưa có bảng `incoming_transactions` |

## Dashboard lịch chi tiêu

Chạy dashboard ở terminal riêng; giữ monitor chạy nếu cần lấy giao dịch mới:

```sh
python dashboard.py
```

Mở `http://127.0.0.1:8765`. Có thể đổi cổng bằng `python dashboard.py --port 8766`; nhấn `Ctrl+C` để dừng server. Chỉ cần `BANK_KEY` để chọn phạm vi tài khoản, không cần cấu hình Bark để xem. Dashboard không gọi Bank API/Bark và không thay đổi lịch ngân sách hoặc dữ liệu tài chính.

Giao diện gồm lịch tháng, tổng chi, số còn lại trong tháng và khoản chi của ngày được chọn. Thiếu lịch sử ngân sách của ngày đã đến hoặc chưa có ledger thì hiển thị chưa xác định, không tự điền mức cấu hình hiện tại cho quá khứ. Kết quả chỉ dựa trên khoản chi đã lưu, không chứng minh toàn bộ lịch sử ngân hàng.

Ba chỉ số chính gồm chi, còn lại trong tháng và có thể chi/ngày. Hai số phụ gồm ngân sách tháng và ngày chi nhiều nhất. Các định nghĩa ngân sách tháng, còn lại và chi/ngày nằm trong tooltip (`title`) của từng thẻ; màn hình cảm ứng không hiện tooltip. Ngân sách cả tháng cộng mức hiệu lực của tất cả ngày trong tháng, gồm ngày tương lai. Thiếu mức ngân sách ở bất kỳ ngày nào thì tổng ngân sách tháng, số còn lại và mức chi/ngày đều chưa xác định. Số còn lại bằng ngân sách cả tháng trừ tổng chi đã ghi trong tháng; giữ số âm khi vượt. Mức có thể chi/ngày bằng số còn lại chia số ngày còn lại, làm tròn xuống; nếu đã hết ngân sách thì là 0. Không chia khi không còn ngày sau hôm nay. Ngày cuối tháng ghi **Hôm nay là ngày cuối**; chỉ tháng đã kết thúc mới ghi **Tháng đã hết**. Mức này là phân bổ theo ngân sách tháng, không đổi hạn mức từng ngày. Giá trị tiền trong thẻ giữ một dòng, cỡ chữ theo chiều rộng cột. Số cực dài có thể cuộn ngang trong thẻ bằng cảm ứng hoặc bàn phím; không dùng dấu rút gọn và không bẻ chữ số. Tổng chi hiện số khoản chi.

Thẻ **Hôm nay** đọc độc lập với tháng đang xem: đã chi, ngân sách hiệu lực hôm nay và số còn lại có dấu. Thẻ dùng ngày UTC+07:00 từ server. Nút **Xem khoản chi** mở danh sách hôm nay, đổi tháng khi cần và chuyển focus đến tiêu đề panel. Nếu chưa có ledger hoặc ngân sách thì hiển thị chưa biết, không giả số còn lại bằng 0.

Ngày chi nhiều nhất lấy trong phần ngày đã đến; các ngày bằng nhau chọn ngày sớm nhất.

Biểu đồ nhỏ hiển thị chi và ngân sách tích lũy đến hôm nay. Khi thiếu ngân sách ở một ngày, phần ngân sách tích lũy từ ngày đó trở đi không được nối thành số giả. Có bảng số chính xác để đọc bằng bàn phím hoặc screen reader. Biểu đồ và so sánh tháng trước nằm cạnh nhau trên màn hình rộng. SVG giữ tỷ lệ; điểm tròn và vuông không bị bóp trên màn hình nhỏ. Tổng tuần đặt cuối mỗi hàng lịch, chỉ cộng những ngày thuộc tháng đang xem; hàng đầu/cuối có thể là tuần không đủ 7 ngày.

So sánh tháng trước gọi thêm `/api/calendar?month=YYYY-MM`. Trang giữ kết quả tháng trước trong bộ nhớ (không dùng localStorage) và chỉ đọc lại khi mốc đồng bộ ngân hàng đổi, sau 10 phút, hoặc khi bấm **Cập nhật**; vì vậy so sánh có thể chậm hơn các số khác một lượt đọc (60 giây). Với tháng đang chạy, hai phía dùng cùng số ngày tính từ đầu tháng và giới hạn theo tháng ngắn hơn. Với tháng đã kết thúc, so sánh tổng hai tháng. Tháng tương lai hoặc tháng trước chưa có ledger thì chưa so sánh. Mốc tháng trước bằng 0 vẫn có chênh lệch số tiền nhưng không tính phần trăm. Chênh lệch này không chứng minh tiết kiệm thật khi lịch sử chưa đầy đủ.

Danh sách khoản chi hiện số tiền và tỷ trọng trên tổng chi cả ngày, không chỉ tổng của trang đang tải. Tổng chi bằng 0 thì không tính phần trăm. Không hiện ID hoặc số thứ tự. Danh sách hiện giờ và nội dung khi đã có dữ liệu từ lần đồng bộ. Đồng bộ lấy giờ `HH:MM` hoặc `HH:MM:SS` từ `transactionDate` có dạng `DD/MM/YYYY HH:MM[:SS]`, theo ngày Việt Nam đang dùng; ngày không có giờ không được tự gán 00:00. Nội dung lấy từ trường `description` khi API có trả chuỗi hợp lệ. Hai cột nullable `transaction_time` và `note` được thêm trong cùng transaction đồng bộ. Monitor đang chạy cần được khởi động lại để nạp mã mới; migration chạy ở lần đồng bộ tiếp theo của bản mới. Bản ghi cũ chỉ bổ sung trường còn trống khi xuất hiện lại trong cửa sổ API; không đổi số tiền/ngày hoặc tạo thông báo chi mới. Metadata không hợp lệ hoặc dài hơn 500 ký tự để trống, không chặn giao dịch tài chính hợp lệ. Các ký tự điều khiển bị loại bỏ; HTML hiển thị như văn bản. Dashboard đọc được schema cũ mà không thực hiện migration. Chưa xác minh nhà cung cấp luôn trả giờ hoặc `description`; việc hiển thị phụ thuộc payload thực tế, không gọi thêm Bank API để lấy chi tiết. Panel ngày ẩn khi mặc định chọn hôm nay; nút **Xem khoản chi** hoặc bấm một ngày trên lịch đều mở được danh sách. Ô ngày chỉ ở trạng thái đã chọn khi panel mở; ô hôm nay vẫn có dấu ngày hiện tại. Panel đã mở không tự đóng khi cập nhật định kỳ. Ngày không có khoản chi hiển thị dấu “—” trong lịch. Một khoản chi có số tiền 0 vẫn hiện 0 ₫. Công tắc **Mới nhất | Lớn nhất** gọi `/api/day?date=YYYY-MM-DD&offset=0&sort=latest` hoặc `sort=amount`. Mặc định giao diện là Mới nhất; API không có `sort` vẫn dùng amount để giữ tương thích. Backend sắp xếp toàn ngày trước khi phân trang. Mới nhất xếp giờ giảm dần, khoản chưa có giờ ở cuối; ID tăng dần làm khóa phụ. `HH:MM` và `HH:MM:00` được coi là cùng giờ. Nếu cả ngày chưa có giờ hợp lệ, backend dùng thứ tự số tiền giảm dần và giao diện ghi rõ cách thay thế này. Lớn nhất xếp số tiền giảm dần rồi ID tăng dần. Mỗi trang có 50 giao dịch. Chuyển thứ tự tải lại từ trang đầu, giữ lựa chọn khi đổi ngày hoặc cập nhật. Lỗi tải giữ danh sách cũ và trả công tắc về thứ tự đã đọc. Ghi chú **Một số khoản cũ chưa có giờ** chỉ hiện khi cả ngày có lẫn khoản biết/chưa biết giờ; API đếm toàn ngày, không chỉ trang đang tải. Số tiền được gửi dạng chuỗi và định dạng bằng `BigInt` để không làm tròn số nguyên SQLite lớn.

### Nhãn giao dịch và khóa chỉnh sửa

Mỗi giao dịch có tối đa một nhãn dài 1–40 ký tự sau chuẩn hóa NFC, gộp khoảng trắng và bỏ khoảng trắng đầu/cuối. Nhãn không được chứa ký tự điều khiển hoặc điều khiển hướng chữ. Nhãn lưu riêng trong `transaction_labels`; dòng ngân hàng trong `transactions` không thay đổi. Không có ghi chú riêng hoặc nút nhận xung đột trên dashboard.

`DASHBOARD_WRITE_KEY` là cấu hình tùy chọn trong `.env`. Dùng giá trị ngẫu nhiên dài ít nhất 16 ký tự; giữ khóa riêng tư, không đưa vào URL, log hoặc trò chuyện. Không đặt khóa thì dashboard hoàn toàn chỉ đọc: `GET /api/session` trả `{"write_enabled": false}`, `POST /api/labels` trả HTTP 403, không hiện điều khiển sửa và không tạo bảng. Khóa quá ngắn làm CLI tắt chỉnh sửa và in cảnh báo.

Sau khi đặt khóa, khởi động lại dashboard. Nhập khóa vào **Mã chỉnh sửa** rồi chọn **Mở khóa**. Mỗi hàng của ngày có nút **Gán nhãn** hoặc **Sửa nhãn**. Chọn **Lưu** để ghi, **Xóa nhãn** để xóa, **Đóng** để bỏ thao tác đang nhập. Chọn **Khóa lại** để ẩn editor. Khóa chỉ ở trong bộ nhớ trang; ô mật khẩu được xóa ngay sau khi mở khóa. Không lưu khóa vào localStorage, sessionStorage hoặc cookie. Tải lại trang phải nhập khóa lại.

Năm lần gửi khóa sai trong 60 giây khóa mọi yêu cầu ghi, kể cả khóa đúng, cho tới khi các lần sai hết cửa sổ 60 giây (HTTP 429). Phản hồi HTTP 403 khóa lại editor. Lỗi hoặc timeout không được coi là ghi thành công; tải lại trang để kiểm tra trạng thái trước khi thử lại. Chỉ dùng qua máy cục bộ, SSH tunnel hoặc RDP; **không mở cổng ra internet**.

API nhãn:

| Endpoint | Kết quả |
|---|---|
| `GET /api/session` | `{"write_enabled": true}` hoặc `false`; không nhận query |
| `GET /api/labels` | `{"labels": [{"label": "Cà phê", "count": 2}]}`; đếm trong tài khoản, xếp số lượng giảm dần rồi nhãn; thiếu bảng trả danh sách rỗng |
| `POST /api/labels` | JSON bắt buộc cả `transaction_id` và `label`; trả `{"ok": true, "changed": true}` hoặc `false` khi không đổi |

Yêu cầu ghi dùng `Content-Type: application/json`, `X-Budget-Dashboard: 1` và khóa trong header `X-Budget-Write-Key`. Body tối đa 4096 byte. Không nhận trường khác. Gửi `label: null`, chuỗi rỗng hoặc toàn khoảng trắng hợp lệ để xóa nhãn; thiếu `label` trả HTTP 400, không xóa ngầm. Giao dịch không có trong tài khoản trả HTTP 404.

Bản ghi của `GET /api/day` và `GET /api/transactions` có trường `label` là chuỗi hoặc `null`. Bộ lọc **Nhãn** dùng `/api/transactions?label=...`, khớp cả nhãn không phân biệt hoa/thường hoặc dấu; rỗng là tất cả. `filters.label` echo giá trị đã gửi. Tổng và số khoản chỉ tính các hàng khớp trên toàn sổ, không chỉ trang hiển thị; không đổi tổng ngày/tháng hoặc cảnh báo. Nhãn chỉ hiển thị như văn bản, kể cả khi chứa ký hiệu HTML.

Ghi dùng kết nối riêng `mode=rw`, `BEGIN IMMEDIATE`, chờ khóa SQLite tối đa 5 giây. DB bận trả HTTP 503; lỗi rollback và đóng kết nối. Không tạo DB bị thiếu. Bảng `transaction_labels` được tạo ở lần ghi nhãn thành công đầu tiên, không cần migration bảng hiện có. Mỗi thay đổi thật ghi đúng một dòng `edit_audit` với action `set_label` hoặc `clear_label`, giá trị trước/sau và thời gian UTC+07:00. Gửi lại giá trị không đổi không thêm audit. Nhãn và audit commit cùng transaction; không đổi tổng chi, ngân sách hoặc báo cáo.

`transaction_labels`, `transaction_conflicts` và `edit_audit` không được thêm vào danh sách schema bắt buộc của preflight. Các snapshot được tạo sau khi ghi nhãn chứa cả nhãn và audit; backup cũ không tự cập nhật. Giữ DB và backup riêng tư vì nhãn cũng có thể chứa dữ liệu cá nhân.

### Chọn khoản IN tính cho tháng sau

Sau khi mở khóa chỉnh sửa, mỗi khoản IN trong panel ngày có nút **Tính cho tháng sau**. Nút có `aria-pressed="true"` khi khoản đó đang được tính cho tháng sau. Bấm nút để bật hoặc tắt. Khoản được chọn không còn tính cho tháng của nó mà tính cho tháng liền sau. Chỉ dời được đúng một tháng. Không có giới hạn thời điểm: chọn được cả khi tháng của khoản đó đã kết thúc. Khi chưa mở khóa hoặc khi dashboard chỉ đọc, dashboard không hiện nút.

Nút bị tắt trong lúc đang ghi. Khi ghi xong, dashboard tải lại lịch, ngày đang mở và thẻ **Hôm nay**. Khi ghi lỗi, nút giữ trạng thái cũ và thông báo lỗi hiện ở dòng thông báo chỉnh sửa, như lỗi ghi nhãn.

| Endpoint | Kết quả |
|---|---|
| `POST /api/income-shift` | JSON bắt buộc cả `transaction_id` (ID trong `incoming_transactions`) và `next_month` (boolean `true` hoặc `false`); trả `{"ok": true, "changed": true}` hoặc `false` khi không đổi |

Endpoint dùng cùng header, khóa ghi, giới hạn 4096 byte và mã lỗi như `POST /api/labels`: HTTP 400 khi body sai (thiếu hoặc thừa trường, `next_month` không phải boolean), 403 khi chỉ đọc hoặc khóa sai, 429 khi bị khóa tạm, 503 khi DB bận. HTTP 404 khi ID không có trong `incoming_transactions` của tài khoản, kể cả ID của khoản OUT hoặc của tài khoản khác.

Bảng `income_budget_shifts` (`account`, `transaction_id`, `updated_at`) được tạo ở lần chọn thành công đầu tiên. Một dòng nghĩa là khoản IN đó tính cho tháng sau. Bỏ chọn xóa dòng đó. Bỏ chọn khi bảng chưa có không tạo bảng. Dòng ngân hàng trong `incoming_transactions` không đổi. Mỗi thay đổi thật ghi đúng một dòng `edit_audit`: action `shift_income` với `detail` là `{"next_month": true}`, hoặc action `unshift_income` với `{"next_month": false}`. Gửi lại trạng thái không đổi trả `changed: false` và không thêm audit. Preflight nhận biết bảng này nhưng không bắt buộc.

Ảnh hưởng đến ngân sách:

- Ngân sách thật của cả hai tháng đổi ngay ở lần đọc sau. Tháng của khoản IN giảm, tháng sau tăng đúng bằng số tiền của khoản đó. Mọi ngày của hai tháng được chia lại, kể cả các ngày đã qua.
- Nếu một trong hai tháng đã kết thúc, ngân sách hiệu lực của tháng đó cũng đổi, vì tháng đã kết thúc dùng ngân sách thật.
- Ngân sách dự kiến không đổi. Cảnh báo không đổi, vì cảnh báo luôn dùng ngân sách dự kiến của tháng đang chạy.
- Báo cáo tuần đã gửi không được điều chỉnh khi chỉ ngân sách đổi.

Nút **Sáng/Tối** chuyển giao diện và lưu lựa chọn trong trình duyệt. Khi chưa có lựa chọn, theme theo hệ điều hành. Nếu trình duyệt chặn lưu trữ, nút vẫn hoạt động nhưng lựa chọn không được giữ sau khi tải lại. Theme không đổi dữ liệu tài chính.

Đầu trang hiện huy hiệu và thời điểm đồng bộ ngân hàng tương đối, lấy từ `sync.last_success_at`. Đọc lại SQLite không đặt lại mốc ngân hàng. Khi đọc trạng thái lỗi, mốc ngân hàng cũ vẫn hiện và được đánh dấu cũ. Mốc đọc SQLite chỉ nằm trong tooltip; lỗi đọc lịch không đặt lại mốc đọc này. Thông tin vận hành, lỗi kỹ thuật, sáu bước xử lý, hàng đợi thông báo và snapshot nằm trong **Chi tiết kỹ thuật**, đóng mặc định. Một banner lịch sử chỉ hiện khi `gap_suspected` hoặc `history_unknown` là `true`; cờ thiếu hoặc `null` không tự bật banner. Lỗi đọc trạng thái hiện trên huy hiệu và trong chi tiết kỹ thuật; lỗi đọc khoản chi vẫn có cảnh báo riêng. Trang tự đọc lại mỗi 60 giây khi đang mở, không đổi chu kỳ monitor 30 phút. Có nút cập nhật thủ công. Mốc đọc DB không phải mốc đồng bộ ngân hàng. Lỗi tải dữ liệu được hiển thị riêng; bản xem cũ, nếu còn, được đánh dấu cũ chứ không thay bằng số 0.

Lịch dùng ngày UTC+07:00. Ngày không có bản ghi không chứng minh không phát sinh chi tiêu; cờ lịch sử là bằng chứng cấp tài khoản, không xác minh riêng từng ngày. Ngân sách ngày tương lai chỉ dựa trên lịch đã lưu. Chưa có ledger hoặc ngân sách thì hiển thị chưa biết, không tự lấy mức trong `.env` làm lịch sử. Dashboard mặc định chỉ đọc. Khi bật khóa ghi, dashboard chỉ gán, đổi hoặc xóa nhãn; không sửa ngày hoặc số tiền ngân hàng, đổi cấu hình, cách ly outbox, đối soát hoặc xuất CSV. Các lệnh CLI, gồm nhận xung đột ngân hàng, dùng riêng.

Các API đọc dữ liệu từ SQLite dùng `mode=ro`, `query_only` và một read transaction; không tạo bảng nhãn. Truy vấn có deadline hợp tác 3 giây; kiểm tra backup tối đa 2 giây. `quick_check` của DB chạy trên một kết nối chỉ đọc riêng, tối đa 3 giây, và chỉ chạy lại khi tệp DB đổi (kích thước, mtime, bộ đếm thay đổi của SQLite) hoặc sau 10 phút. Hết giờ kiểm tra không bị coi là DB hỏng: dashboard vẫn đọc và thử kiểm tra lại sau. Kết quả `verified`/`invalid` của backup mới nhất cũng được nhớ theo trạng thái tệp tối đa 10 phút; `error`, sidecar và tệp không thường luôn được kiểm tra lại. Các bộ nhớ này chỉ nằm trong tiến trình dashboard, không ghi ra đĩa. Đọc DB WAL vẫn có thể cần sidecar SQLite; đây không phải thao tác sửa dữ liệu tài chính. Server chỉ phục vụ HTML, CSS, `helpers.js`, `app.js` và các API được phép, không phục vụ tệp dự án tùy ý.

Kiểm tra dashboard offline, dùng DB tạm và HTTP server loopback tạm, không đọc `.env` hoặc dữ liệu thật:

```sh
python -B dashboard.py --self-check
```

Kiểm tra phép tính frontend bằng Node.js nếu đã có Node; dashboard vẫn chỉ cần Python để chạy:

```sh
node dashboard/app.js --self-check
```

### Phân loại theo từ khóa

Dashboard gán mỗi khoản chi vào một nhóm khi đọc, không gọi thêm API. Nhóm không được lưu trong DB và không đổi tổng chi, ngân sách hoặc cảnh báo. Nạp ví và chuyển tiền cá nhân vẫn là chi `OUT`. Dashboard không còn nhận diện kênh thanh toán. Thứ tự ưu tiên:

1. Nhãn bạn đã gán cho giao dịch (`category_source` là `label`).
2. Nhóm đầu tiên có từ khóa khớp tên bên nhận (`keyword`).
3. `Chưa phân loại` (`none`).

Bạn tự thêm nhóm vào `categories.json` ở thư mục gốc. Tệp không có nhóm nào (`{"groups": []}`) thì mọi khoản chi chưa có nhãn là `Chưa phân loại`. Ví dụ:

```json
{"groups": [
  {"name": "Ăn uống", "keywords": ["tap hoa", "ca phe", "nha hang"]},
  {"name": "Di chuyển", "keywords": ["gui xe", "xang dau"]}
]}
```

- **Từ khóa tìm trong toàn bộ ghi chú ngân hàng**, gồm cả tên người gửi, tên người nhận và mã kỹ thuật. Ghi chú ZaloPay, MoMo, MBCT và thẻ nước ngoài thường chỉ có mã (`ZaloPay`, `CASHIN`, `MBCT`, `POSRETAIL`, `MID-...`), không có tên bên bán. Với các khoản này, dùng chính các mã đó làm từ khóa, hoặc gán nhãn tay.
- Không phân biệt chữ hoa/thường hoặc dấu. Từ khóa phải khớp nguyên từ: `ca` không khớp `cam`.
- Dùng cụm dài hoặc mã riêng. Từ khóa `quan` khớp cả tên người `DUONG QUOC QUAN`; cụm `quan an` thì không. Không dùng từ có trong mọi ghi chú, như `customer`, `chuyen tien` hoặc tên chủ tài khoản, vì chúng khớp gần như mọi khoản.
- Nhóm đứng trước trong tệp thắng khi nhiều nhóm cùng khớp.
- Giới hạn: tệp tối đa 64 KB và chỉ có khóa `groups`; tối đa 50 nhóm; tối đa 200 từ khóa mỗi nhóm. Tên nhóm dài 1–40 ký tự, không trùng nhau (không phân biệt hoa/thường hoặc dấu) và không là `Chưa phân loại`. Từ khóa dài tối đa 60 ký tự và có ít nhất một chữ hoặc số. Tên và từ khóa không được chứa ký tự điều khiển.
- Tệp thiếu nghĩa là không có quy tắc. Dashboard đọc lại tệp khi tệp đổi, không cần khởi động lại. Nếu tệp sai khi khởi động, dashboard không chạy và in lỗi. Nếu bạn sửa sai khi đang chạy, dashboard giữ quy tắc cũ và in cảnh báo ra stderr một lần cho mỗi lần sửa.

Bản ghi của `GET /api/day` và `GET /api/transactions` có thêm `category` (tên nhóm, nhãn hoặc `Chưa phân loại`) và `category_source`. Bộ lọc **Phân loại** dùng `/api/transactions?category=...`: tối đa 40 ký tự, không phân biệt hoa/thường hoặc dấu, rỗng là tất cả. `Chưa phân loại` chọn các khoản chưa có nhóm. `filters.category` echo giá trị đã gửi. Tổng và số khoản trong kết quả lọc tính trên toàn bộ khoản khớp, không chỉ trang đã tải. Mỗi dòng hiện nhóm khi khoản đó chưa có nhãn.

Bảng **Chi theo phân loại** gọi `/api/categories?month=YYYY-MM`. Kết quả có tổng `spent` và danh sách `categories` gồm tên, số khoản, số tiền; các số tiền là chuỗi số nguyên VND. Chỉ phân loại có khoản chi mới xuất hiện, xếp theo số tiền giảm dần rồi tên. Nhãn chỉ khác nhau về hoa/thường hoặc dấu được gộp vào một dòng, mang tên của biến thể có tổng chi lớn nhất. Tổng các dòng bằng tổng chi tháng trong lịch, theo cùng phạm vi tài khoản. Tỷ trọng dùng tổng chi tháng; tổng bằng 0 thì hiện “—”. Lỗi tải bảng giữ hàng cũ, chỉ báo trong bảng, không đổi banner hoặc kết quả cập nhật thủ công.

## Kiểm thử offline

Chạy từ thư mục dự án; các kiểm thử chỉ dùng DB/backup tạm, không đọc dữ liệu sản xuất hoặc gọi Bank API/Bark. Self-check dashboard khởi chạy HTTP server loopback tạm rồi đóng server/client/thread. Các self-check Python được tách vào `tests/` và chỉ nạp khi yêu cầu rõ ràng. `--help` và `--self-check` không đọc `.env`. Import `main` thông thường có thể nạp `.env`; đặt `PYTHON_DOTENV_DISABLED=1` trước khi chạy unittest để tắt việc này.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 python -m unittest discover -v
node tests/test_dashboard_helpers.cjs
node tests/test_dashboard_harden.cjs
node tests/test_dashboard_comparison.cjs
node tests/test_dashboard_edit.cjs
node tests/test_dashboard_responsive.cjs
```

Trong PowerShell, đặt `$env:PYTHONDONTWRITEBYTECODE = '1'` và `$env:PYTHON_DOTENV_DISABLED = '1'` trước khi chạy `python -m unittest discover -v` hoặc `python -m unittest discover -s tests -v`. Trước khi chạy monitor với cấu hình thật trong cùng terminal, bỏ biến chặn dotenv bằng `Remove-Item Env:PYTHON_DOTENV_DISABLED -ErrorAction SilentlyContinue`.

Các lệnh self-check cũ vẫn được giữ:

```sh
python -B main.py --self-check
python -B dashboard.py --self-check
python -B -m monitoring.runtime_health
node dashboard/app.js --self-check
```

`common.py` giữ múi giờ UTC+07:00 dùng chung. Dashboard dùng `monitoring.sync_status.read_backup_snapshot()`, `newest_backup()` và `snapshot_from_connection()` với connection read-only của chính dashboard; kiểm tra schema tài chính và cấu trúc JSON trạng thái không đổi. Việc tách self-check, helpers và các API nội bộ không đổi nghiệp vụ.

## An toàn vận hành

Kiểm tra trước khi chạy:

```sh
python main.py --preflight
```

Lệnh không gọi API, gửi Bark, tạo log hoặc ghi schema. Lệnh kiểm tra Python, cấu hình, quyền truy cập, dung lượng và DB hiện có. Thiếu DB được chấp nhận khi không có journal mồ côi. DB rỗng, hỏng hoặc sai schema bị từ chối; không tự thay DB. Các bảng tài chính phải có khóa duy nhất `accounts(account)` và `transactions(account, transaction_id)`; partial index không đủ bảo vệ chống trùng. Preflight kiểm tra hai khóa này, không tự tạo hoặc sửa khóa trong DB cũ. Lệnh đọc SQLite bằng `mode=ro`; DB WAL có thể cần tạo sidecar của SQLite. Kiểm tra quyền truy cập chỉ là chỉ báo; thao tác mở thật vẫn có thể lỗi.

Monitor tự chạy kiểm tra này sau khi lấy khóa một tiến trình và trước khi gọi API. Không chạy monitor nếu kiểm tra hoặc mở log thất bại. Dung lượng trống tối thiểu là 16 MiB. Vị trí DB và backup cần thêm ít nhất hai lần kích thước DB cộng 4 MiB nếu mức này lớn hơn. Giới hạn kiểm tra DB là 2 giây, thời gian chờ khóa tối đa 1 giây. Trong phiên chạy, DB đã mất hoặc bị cắt rỗng không được tự tạo lại.

Log nằm trong `logs/monitor.log`. Mỗi tệp tối đa 1 MiB; giữ thêm ba bản `.1`, `.2`, `.3`. Mỗi bản ghi tối đa 16 KiB. Khóa `BANK_KEY`, `DEVICE_KEY` và dạng mã hóa URL được che trước khi ghi console hoặc tệp. Không ghi phản hồi ngân hàng nguyên bản. Log vẫn có thể chứa số tiền và thông tin vận hành; giữ thư mục này riêng tư. Log cũ vượt giới hạn hoặc đường dẫn không an toàn bị từ chối, không tự cắt hoặc xóa. Lỗi ghi log khi đang chạy phát cảnh báo cục bộ, không rollback tài chính.

`--status` bổ sung sáu bước: `bank_api`, `validation`, `financial_save`, `weekly_report`, `outbox`, `backup`. Mỗi bước có kết quả, thời điểm kiểm tra/thử/thành công, mã lỗi an toàn và số lỗi liên tiếp. `skipped` nghĩa là bước chưa chạy trong chu kỳ này; không có nghĩa đã thành công. `healthy` của outbox nghĩa là lượt xử lý không lỗi, không bảo đảm hàng đợi đã hết. `healthy` của backup nghĩa là snapshot hôm nay vừa được tạo hoặc làm mới. Metadata được ghi riêng; lỗi ghi metadata không rollback dữ liệu tài chính đã commit.

Cảnh báo sức khỏe đồng bộ được xếp vào outbox khi có ít nhất ba chu kỳ lỗi liên tiếp (chu kỳ chỉ lỗi sau khi hết các lần thử lại), hoặc một lần đồng bộ lỗi sau hơn 3600 giây kể từ lần thành công cuối. Đúng 3600 giây chưa chạm điều kiện tuổi. Thiếu thời điểm thành công thì dùng số lỗi liên tiếp. Một sự cố chỉ tạo một sự kiện cảnh báo. Lần thành công tiếp theo chỉ đóng sự cố để sự cố sau lại được cảnh báo; monitor không gửi thông báo phục hồi. Trạng thái được lưu trong DB nên restart không tạo lại sự kiện cùng sự cố. Quy tắc gửi ít nhất một lần của outbox vẫn áp dụng; Bark có thể nhận trùng sau sự cố giữa gửi và commit.

Cảnh báo chỉ được đánh giá khi monitor đang chạy và có kết quả chu kỳ. Đây không phải watchdog ngoài tiến trình. Bark lỗi hoặc một sự kiện chặn outbox có thể làm cảnh báo còn chờ; log cục bộ vẫn ghi sự cố. Lỗi riêng của outbox, báo cáo và backup được xem trong `--status`; không biến chúng thành lỗi đồng bộ ngân hàng. Không thêm cấu hình hoặc đổi tần suất thông báo chi tiêu.

## Thông báo và tổng kết tuần

Giao dịch, mốc cảnh báo và sự kiện thông báo được ghi vào SQLite trong cùng giao dịch; ứng dụng commit trước khi gọi Bark. Mỗi lần chạy chỉ gửi tối đa 20 sự kiện đang chờ theo thứ tự ID tăng dần trong số sự kiện đủ điều kiện; sự kiện đã cách ly không được gửi. Với các revision tổng kết tuần, revision sau phải chờ revision trước của cùng tuần; cách ly revision trước vẫn chặn nhóm tuần đó, nhưng không chặn nhóm không liên quan. Nếu gửi Bark lỗi, sự kiện vẫn chờ và được thử lại ở chu kỳ sau, kể cả khi không có giao dịch mới hoặc Bank API lỗi, miễn là cấu hình Bark hợp lệ. Payload thông báo hằng ngày là ảnh chụp tại thời điểm xếp hàng, không tự cập nhật theo số dư mới. Thông báo mới dùng tiêu đề ghi ngày và body hai dòng: tổng chi/ngân sách, cùng số tiền còn hoặc vượt; không thêm ghi chú riêng cho mốc ngưỡng hoặc vượt ngân sách. Ngưỡng, tần suất gửi và tổng kết tuần không đổi; sự kiện đã xếp hàng trước nâng cấp giữ nguyên nội dung.

Chế độ `thresholds` cũng đánh dấu mốc ngưỡng khi sự kiện được xếp hàng, không phải khi Bark xác nhận đã gửi. Outbox bảo đảm gửi ít nhất một lần: nếu tiến trình dừng sau khi Bark nhận thông báo nhưng trước khi SQLite ghi nhận hoàn tất, lần thử lại có thể gửi trùng.

Tổng kết tuần cũng được xếp vào outbox. Mốc tuần khởi tạo được lưu cùng transaction baseline để không bỏ tuần đầu nếu tiến trình dừng trước bước xếp báo cáo. Giao dịch đến muộn có thể tạo phiên bản điều chỉnh (revision) với chênh lệch so với phiên bản trước; nếu chưa gửi, bản gốc phải được xử lý trước bản điều chỉnh trong cùng tuần. Với dữ liệu cũ dùng mốc tổng kết legacy, ứng dụng chỉ lập mốc từ dữ liệu đã quan sát. Không thể khôi phục tuần cũ không có giao dịch lưu hoặc tuần trống trước đó; các tuần trống legacy cũ hơn không xác định được từ riêng mốc này.

Tổng kết tuần mới kèm trạng thái lịch sử giao dịch **hiện tại cấp tài khoản** (`gap_suspected`, `history_unknown`: `0`, `1` hoặc `null`), không phải bằng chứng xác minh riêng tuần đó. Nếu đã biết đủ ngân sách nhưng có cờ `1` hoặc `null`, chênh lệch ngân sách chỉ tạm tính từ dữ liệu đã ghi nhận; cảnh báo thiếu lịch sử ngân sách vẫn độc lập. Cả hai cờ bằng `0` cũng chỉ cho thấy số liệu đã quan sát, không chứng minh tuần đủ giao dịch.

Khi trạng thái lịch sử đổi, tuần đã có bản gốc/điều chỉnh nhận bản điều chỉnh dù số tiền không đổi; sự kiện cũ giữ nguyên, chạy lại với cùng trạng thái không tạo bản trùng. Bản gốc/điều chỉnh cũ chưa kèm trạng thái nhận một bản điều chỉnh cho mỗi tuần đã theo dõi trong lần chạy thành công đầu tiên, kể cả tuần cũ; mốc legacy chỉ dựa trên dữ liệu đã quan sát không tạo bản điều chỉnh chỉ vì trạng thái lịch sử. Cơ chế này không tải bù giao dịch hay tự đối soát lịch sử.

## Trạng thái, outbox và đối soát lịch sử

`BANK_KEY` xác định phạm vi dữ liệu cho các lệnh dưới đây. Đổi `BANK_KEY` sẽ chọn phạm vi khác, không tự chuyển dữ liệu cũ; giới hạn khi xoay khóa vẫn giữ nguyên.

### Xem trạng thái (`--status`)

```sh
python main.py --status
```

Lệnh chỉ đọc, không gọi Bank API/Bark và không ghi cơ sở dữ liệu; có thể chạy khi monitor đang hoạt động. Lệnh hiển thị `last_attempt_at`, `last_success_at`, kết quả cuối (`healthy` hoặc `failed`), độ mới theo lần thử, mã lỗi an toàn cuối, số lỗi liên tiếp, số outbox đã gửi/đang chờ/cách ly, cùng hai cờ lịch sử `gap_suspected` và `history_unknown`. Độ mới là `fresh` khi tuổi của `last_attempt_at` không quá 3600 giây; đúng 3600 giây vẫn là `fresh`, lớn hơn là `stale`. Thiếu, sai định dạng, không có múi giờ hoặc nằm trong tương lai là `unknown`. Độ mới độc lập với kết quả cuối; nó không chứng minh dữ liệu ngân hàng mới. Xem `last_success_at` riêng để biết lần đồng bộ thành công cuối.

`Sao lưu mới nhất` chỉ chọn ứng viên có ngày lớn nhất khớp mẫu `budget-YYYY-MM-DD.sqlite3` trong `backups/`, không tự lùi về bản cũ hơn. Trạng thái là `absent`, `verified`, `invalid` hoặc `error`. Kiểm tra dùng `PRAGMA integrity_check` ở chế độ chỉ đọc, có giới hạn thời gian. `verified` chỉ có nghĩa SQLite toàn vẹn tại thời điểm kiểm tra; không chứng minh backup đầy đủ hoặc được mã hóa.

Nếu ghi trạng thái đồng bộ thất bại nhưng transaction tài chính còn hoạt động, ứng dụng ghi cảnh báo an toàn vào log và vẫn commit dữ liệu. Nếu SQLite đã tự rollback toàn transaction, chu kỳ báo lỗi thay vì đánh dấu đã lưu thành công.

### Liệt kê outbox (`--outbox-list`)

```sh
python main.py --outbox-list
python main.py --outbox-list --limit 25 --after-id 120
```

Lệnh chỉ đọc, không gửi thông báo, và có thể chạy khi monitor đang hoạt động. `--limit` nhận từ `1` đến `500`, mặc định `50`. `--after-id` là con trỏ loại trừ: chỉ liệt kê ID lớn hơn ID đã cho. Dùng `Next cursor` của trang trước cho trang kế tiếp; `none` nghĩa là hết trang. Danh sách thuộc phạm vi `BANK_KEY`, gồm trạng thái, số lần thử và mã lỗi an toàn của từng sự kiện.

Số `120` trong ví dụ chỉ minh họa, không phải ID cần chạy; thay bằng `Next cursor` thực tế.

### Cách ly hoặc gỡ cách ly outbox

Đây là thao tác ghi. Dừng monitor bằng `Ctrl+C` trước khi chạy vì thao tác dùng shared lock; không sửa outbox khi monitor còn hoạt động.

```sh
python main.py --outbox-quarantine 123 --confirm --reason "Lý do cách ly"
python main.py --outbox-release 123 --confirm --reason "Lý do gỡ cách ly"
```

`ID` phải là ID dương của sự kiện chưa gửi trong phạm vi `BANK_KEY`; `--confirm` và `--reason` không rỗng là bắt buộc, `--reason` tối đa 1000 ký tự. Thao tác ghi audit. Cách ly đưa sự kiện ra khỏi gửi thông thường. Gỡ cách ly chỉ đưa sự kiện về trạng thái chờ cho chu kỳ monitor bình thường kế tiếp; cả hai lệnh đều không gửi ngay.

Không có tự động cách ly. Lỗi của sự kiện chưa cách ly vẫn chặn lượt gửi hiện tại và không tự cho các sự kiện sau chạy tiếp. Cách ly bản gốc vẫn chặn các bản điều chỉnh của cùng nhóm tuần; sự kiện hoặc nhóm không liên quan vẫn có thể đủ điều kiện gửi.

Các số `123` trong ví dụ chỉ minh họa, không phải lệnh cần chạy; lấy ID thực tế từ `--outbox-list`. Sự kiện đã gửi không thể cách ly hoặc gỡ cách ly.

### Đối soát lịch sử đã xác nhận (`--reconcile-history`)

Chỉ chạy sau khi đã đối chiếu thực tế với sao kê bên ngoài:

```sh
python main.py --reconcile-history --confirm --reason "Đã đối chiếu sao kê bên ngoài"
```

Đây là thao tác ghi; dừng monitor bằng `Ctrl+C` trước khi chạy. Lệnh không gọi API, không tải giao dịch thiếu và không thay đổi bằng chứng giao dịch hiện có. Lệnh lưu audit với thời điểm, lý do và trạng thái cờ trước đối soát, sau đó xóa `gap_suspected` và `history_unknown` cho phạm vi `BANK_KEY`. Cửa sổ API sau đó có dấu hiệu thiếu lịch sử có thể bật lại các cờ này. Cần có baseline lịch sử không rỗng và hợp lệ; `--confirm` bắt buộc, `--reason` không rỗng và tối đa 1000 ký tự.

## Chạy một tiến trình

Ứng dụng giữ khóa hệ điều hành theo đường dẫn DB để ngăn hai tiến trình theo dõi cùng lúc. Tệp `budget_state.sqlite3.lock` có thể còn lại sau khi ứng dụng dừng; tệp còn đó không chứng minh có tiến trình đang giữ khóa. Không xóa tệp khóa. Trước khi nâng cấp lên phiên bản dùng khóa, hãy dừng tiến trình cũ chưa có khóa để tránh chạy đồng thời.

`python main.py --self-check` chỉ dùng SQLite tạm, không gọi mạng hoặc lấy khóa ứng dụng; lệnh này có thể chạy khi tiến trình theo dõi đang hoạt động.

## Sao lưu và khôi phục

Xem cú pháp hiện có:

```sh
python -m storage.db_backup --help
python -m storage.db_backup backup --help
python -m storage.db_backup restore --help
```

Tạo hoặc làm mới snapshot hôm nay, ngày theo UTC+07:00 như monitor dù máy đặt múi giờ khác (mặc định lấy `budget_state.sqlite3`, lưu trong `backups/`):

```sh
python -m storage.db_backup backup
```

Snapshot ngày hợp lệ được thay bằng bản sao mới đã kiểm tra; nếu không thay được (ví dụ tệp đang bị tiến trình khác mở trên Windows) thì lệnh thử lại trong 1 giây, sau đó giữ nguyên bản cũ, xóa tệp tạm và báo lỗi; tệp rỗng hoặc thiếu header SQLite bị từ chối, không được tính là snapshot hợp lệ. Nếu snapshot ngày hỏng được xác nhận, lệnh tạo bản thay thế vào tệp tạm, kiểm tra `integrity_check` trước khi xuất bản, chuyển tệp hỏng sang `.budget-YYYY-MM-DD.sqlite3.corrupt-<random>.sqlite3` trong cùng thư mục rồi xuất bản bản thay thế. Lỗi truy cập hoặc khóa không bị coi là hỏng và không bị cách ly. Nếu xuất bản thất bại, các tệp tạm và artifact cách ly đã tạo được giữ lại. Sau mỗi lần sao lưu thành công, tệp tạm `.budget-<ngày>.sqlite3.tmp-*` bị xóa khi snapshot của cùng ngày đã hợp lệ và tệp tạm thuộc hôm nay hoặc cũ hơn 1 giờ. Tệp tạm và tệp cách ly `.corrupt-*` của ngày cách hôm nay hơn 7 ngày luôn bị xóa. Lệnh `backup` chạy tay luôn sao lưu, không áp dụng quy tắc bỏ qua của monitor.

Khôi phục snapshot đã qua kiểm tra toàn vẹn sang **tệp mới chưa tồn tại**. Đường dẫn đích cũng không được có `-wal`, `-shm` hoặc `-journal`; lệnh từ chối và không xóa các tệp này. Không để tiến trình khác mở hoặc ghi đường dẫn đích trong lúc khôi phục; nếu thư mục dùng chung, chọn thư mục riêng:

```sh
python -m storage.db_backup restore backups/budget-YYYY-MM-DD.sqlite3 budget_state-restored.sqlite3
```

Dừng ứng dụng trước khi tự đưa DB khôi phục vào sử dụng; giữ bản hiện tại an toàn trước khi thay thế `budget_state.sqlite3`.
