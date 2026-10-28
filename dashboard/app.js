"use strict";

if (typeof window === "undefined" && process.argv.includes("--self-check")) {
  require("../tests/test_dashboard_helpers.cjs")();
  console.log("Dashboard frontend self-check passed.");
  process.exit(0);
}

const { normalizeLabelText, amount, monthlyPlan, percentOfDay, validTransactionMetadata, validDayOrder, validBudgetBasis, parseTimestamp, ageSeconds, signedMoney, hasControlChars } = window.DashboardHelpers;

const themeMedia = window.matchMedia("(prefers-color-scheme: dark)");
let savedTheme = null;
try {
  const stored = localStorage.getItem("budget-dashboard-theme");
  if (stored === "light" || stored === "dark") savedTheme = stored;
} catch {}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="theme-color"]').content = theme === "dark" ? "#121212" : "#f2efe6";
  const button = document.getElementById("theme-toggle");
  if (button) {
    button.setAttribute("aria-pressed", String(theme === "dark"));
    button.setAttribute("aria-label", `Giao diện tối; hiện tại ${theme === "dark" ? "Tối" : "Sáng"}`);
    button.title = theme === "dark" ? "Chuyển sang giao diện sáng" : "Chuyển sang giao diện tối";
    document.getElementById("theme-label").textContent = theme === "dark" ? "Tối" : "Sáng";
  }
}

applyTheme(savedTheme || (themeMedia.matches ? "dark" : "light"));
themeMedia.addEventListener("change", () => {
  if (savedTheme === null) applyTheme(themeMedia.matches ? "dark" : "light");
});

// Apply theme before paint; dashboard data waits until markup exists.
document.addEventListener("DOMContentLoaded", () => {
  const $ = (id) => document.getElementById(id);
  applyTheme(document.documentElement.dataset.theme);
  $("theme-toggle").addEventListener("click", () => {
    savedTheme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    applyTheme(savedTheme);
    try { localStorage.setItem("budget-dashboard-theme", savedTheme); } catch {}
  });
  const moneyFormat = new Intl.NumberFormat("vi-VN", { style: "currency", currency: "VND", maximumFractionDigits: 0 });
  const integerFormat = new Intl.NumberFormat("vi-VN");
  const timeFormat = new Intl.DateTimeFormat("vi-VN", { timeZone: "Asia/Ho_Chi_Minh", day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
  const weekNames = ["Chủ nhật", "Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy"];
  const stepNames = {
    bank_api: "Bank API", validation: "Xác thực dữ liệu", financial_save: "Lưu tài chính",
    weekly_report: "Báo cáo tuần", outbox: "Hàng đợi Bark", backup: "Sao lưu"
  };
  const outcomeNames = {
    healthy: "Thành công", failed: "Thất bại", running: "Đang xử lý", skipped: "Chưa chạy",
    unknown: "Chưa rõ", absent: "Chưa có"
  };
  const freshnessNames = { fresh: "Mới", stale: "Đã cũ", unknown: "Chưa rõ độ mới" };
  const errorNames = {
    bank_api: "bank_api", request: "request", validation: "validation", database: "database",
    unexpected: "unexpected", report: "report", delivery: "delivery", backup: "backup"
  };
  const errors = { calendar: null, day: null, today: null, comparison: null };
  const vnParts = Object.fromEntries(new Intl.DateTimeFormat("en", {
    timeZone: "Asia/Ho_Chi_Minh", year: "numeric", month: "2-digit", day: "2-digit"
  }).formatToParts(new Date()).filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
  let today = `${vnParts.year.padStart(4, "0")}-${vnParts.month}-${vnParts.day}`;
  let requestedMonth = today.slice(0, 7);
  let selectedDate = today;
  let calendar = null;
  let day = null;
  let daySort = "latest";
  let transactions = [];
  let calendarRequest = null;
  let dayRequest = null;
  let statusRequest = null;
  let calendarVersion = 0;
  let dayVersion = 0;
  let daySelectionVersion = 0;
  let statusVersion = 0;
  let todaySnapshot = null;
  let todayRequest = null;
  let todayVersion = 0;
  let comparisonRequest = null;
  let comparisonVersion = 0;
  let categoriesRequest = null;
  let categoriesVersion = 0;
  let writeKey = null;
  let writeEnabled = false;
  let labelTarget = null;
  let labelSaving = false;
  let incomeShiftSaving = false;
  const comparisonCache = new Map();
  const COMPARISON_CACHE_MS = 600000;
  let pollTimer = null;
  let lastReadAt = null;
  let lastBankSync = null;
  let statusFailed = false;
  let openedDate = null;
  let refreshVersion = 0;

  function element(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = text;
    return result;
  }

  // Put rows into list in order and drop the rest; moveBefore keeps focus where the browser has it.
  function syncChildren(list, rows) {
    let cursor = list.firstElementChild;
    for (const row of rows) {
      if (row === cursor) cursor = cursor.nextElementSibling;
      else if (row.parentNode === list && list.moveBefore) list.moveBefore(row, cursor);
      else list.insertBefore(row, cursor);
    }
    while (cursor) {
      const next = cursor.nextElementSibling;
      cursor.remove();
      cursor = next;
    }
  }

  function money(value, fallback = "Chưa rõ") {
    const parsed = amount(value);
    return parsed === null ? fallback : moneyFormat.format(parsed);
  }

  function count(value) {
    return Number.isSafeInteger(value) && value >= 0 ? integerFormat.format(value) : "Chưa rõ";
  }

  const MINUS = "\u2212";
  const NOT_TRACKED = "Chưa theo dõi tiền vào";

  // OUT uses MINUS and IN uses "+". Zero has no direction, so it stays plain money.
  function signed(value, sign, fallback = "Chưa rõ") {
    const parsed = amount(value);
    if (parsed === null || parsed < 0n) return fallback;
    return parsed === 0n ? moneyFormat.format(parsed) : signedMoney(value, sign);
  }

  function showSigned(id, value, sign, fallback) {
    const text = signed(value, sign, null);
    $(id).textContent = text ?? fallback;
    $(id).classList.toggle(sign === "+" ? "money-in" : "money-out", text !== null);
  }

  function civilDate(value) {
    const result = new Date(0);
    const [year, month, date] = value.split("-").map(Number);
    result.setUTCFullYear(year, month - 1, date || 1);
    result.setUTCHours(0, 0, 0, 0);
    return result;
  }

  function dateLabel(value, withYear = false) {
    const [year, month, date] = value.split("-");
    return `${date}/${month}${withYear ? `/${year}` : ""}`;
  }

  function validMonth(value) {
    return typeof value === "string" && /^\d{4}-(0[1-9]|1[0-2])$/.test(value) && value >= "0001-01" && value <= "9998-12";
  }

  function validDate(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value) || !validMonth(value.slice(0, 7))) return false;
    const date = civilDate(value);
    return date.getUTCFullYear() === Number(value.slice(0, 4)) && date.getUTCMonth() + 1 === Number(value.slice(5, 7)) && date.getUTCDate() === Number(value.slice(8));
  }

  function timestamp(value) {
    if (value === null || value === undefined) return "Chưa có";
    const parsed = parseTimestamp(value);
    return parsed === null ? "Chưa rõ" : timeFormat.format(new Date(parsed));
  }

  function outcome(value) {
    return Object.hasOwn(outcomeNames, value) ? value : "unknown";
  }

  function freshness(value) {
    return Object.hasOwn(freshnessNames, value) ? value : "unknown";
  }

  function setError(area, value) {
    errors[area] = value;
    const labels = { calendar: "Lịch", day: "Khoản chi ngày", today: "Hôm nay", comparison: "So sánh" };
    const messages = Object.entries(errors).filter(([, message]) => message).map(([key, message]) => `${labels[key]}: ${message}`);
    $("error-banner").hidden = messages.length === 0;
    $("error-message").textContent = messages.join(" ");
  }

  async function readApi(path, controller) {
    let timedOut = false;
    const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 12000);
    try {
      const response = await fetch(path, {
        method: "GET", headers: { "X-Budget-Dashboard": "1" }, credentials: "same-origin", cache: "no-store", signal: controller.signal
      });
      let data;
      try {
        data = await response.json();
      } catch (error) {
        if (error.name === "AbortError") throw error;
        throw new Error("Máy chủ trả dữ liệu không hợp lệ. Sổ đang hiển thị chưa được cập nhật.");
      }
      if (!response.ok) throw new Error(typeof data?.error === "string" ? data.error : "Không đọc được dữ liệu. Sổ đang hiển thị chưa được cập nhật.");
      return data;
    } catch (error) {
      if (timedOut) throw new Error("Đọc dashboard quá thời gian. Giữ lần đọc trước và thử cập nhật lại.");
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  async function writeApi(path, body) {
    if (!writeKey) throw new Error("Chưa mở khóa chỉnh sửa.");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(path, {
        method: "POST", credentials: "same-origin", cache: "no-store", signal: controller.signal,
        headers: { "X-Budget-Dashboard": "1", "Content-Type": "application/json", "X-Budget-Write-Key": writeKey },
        body: JSON.stringify(body)
      });
      let data = null;
      try { data = await response.json(); } catch (error) { if (error.name === "AbortError") throw error; }
      if (response.status === 403) { writeKey = null; updateEditState(); }
      if (!response.ok) throw new Error(typeof data?.error === "string" ? data.error : "Không ghi được. Thử lại sau.");
      if (data?.ok !== true || typeof data.changed !== "boolean") throw new Error("Chưa xác nhận được kết quả ghi. Tải lại trang trước khi thử lại.");
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Ghi quá thời gian. Tải lại trang trước khi thử lại.");
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  function readableError(error) {
    return error instanceof TypeError ? "Không kết nối được dashboard cục bộ. Kiểm tra tiến trình đang chạy rồi cập nhật lại." : error.message;
  }

  function renderSummary(data) {
    $("month-insights-title").textContent = `Tổng hợp tháng ${Number(data.month.slice(5))}/${data.month.slice(0, 4)}`;
    const plan = monthlyPlan(data.days, data.summary.spent, data.summary.elapsed_days);
    const format = (value) => value === null ? "Chưa xác định" : moneyFormat.format(value);
    showSigned("month-spent", data.summary.spent, MINUS, "Chưa có sổ ghi");
    $("month-transactions").textContent = data.summary.transactions === null ? "Chưa có dữ liệu khoản chi" : `${count(data.summary.transactions)} khoản chi`;
    $("month-transactions").hidden = false;
    $("month-budget").textContent = format(plan.budget);
    const closedBasis = data.summary.budget_basis === "actual";
    $("month-budget-label").textContent = closedBasis ? "Ngân sách tháng (đã chốt theo tiền vào)" : "Ngân sách tháng (dự kiến)";
    $("month-budget").title = closedBasis ? "Tổng ngân sách đã chốt của mọi ngày trong tháng: tiền vào của tháng chia đều theo số ngày của tháng." : "Tổng ngân sách dự kiến của mọi ngày trong tháng: mức BUDGET_PERMONTH chia đều theo số ngày của tháng.";
    $("month-budget-note").textContent = plan.unknownDays > 0 ? `Thiếu ngân sách ${count(plan.unknownDays)} ngày.` : "";
    $("month-budget-note").hidden = plan.unknownDays === 0;
    $("month-budget-remaining").textContent = format(plan.remaining);
    $("month-budget-remaining").classList.toggle("is-over", plan.remaining !== null && plan.remaining < 0n);
    $("month-budget-remaining").title = "Ngân sách cả tháng trừ mọi khoản chi đã ghi trong tháng; giữ số âm khi vượt.";
    $("month-daily-available").textContent = plan.daysLeft === 0 ? "—" : format(plan.daily);
    $("month-daily-available").title = "Số còn lại chia số ngày còn lại, không tính hôm nay; làm tròn xuống và không cấp thêm ngân sách khi đã vượt.";
    $("month-daily-note").textContent = plan.daysLeft === 0 ? data.month === data.today.slice(0, 7) ? "Hôm nay là ngày cuối." : "Tháng đã hết." : plan.remaining !== null && plan.remaining <= 0n ? "Đã hết ngân sách." : "";
    $("month-daily-note").hidden = $("month-daily-note").textContent === "";
    const highest = data.summary.highest_day;
    $("month-highest").textContent = highest ? money(highest.spent) : data.ledger_status === "absent" ? "Chưa xác định" : "Chưa ghi khoản chi";
    $("month-highest").title = highest ? `Ngày ${dateLabel(highest.date, true)}` : "Trong phần tháng đã đến.";
    $("month-highest-note").textContent = highest ? `Ngày ${dateLabel(highest.date)}` : "";
    $("month-highest-note").hidden = !highest;
    renderIncomeFigures(data);
  }

  function renderIncomeFigures(data) {
    const summary = data.summary;
    const start = summary.budget_start_month ?? null;
    const tracked = start !== null && data.month >= start;
    const put = (id, text) => {
      $(id).textContent = text ?? "Chưa xác định";
      $(id).classList.toggle("is-pending", text === null);
      $(id).classList.remove("money-in");
    };
    if (!tracked) {
      put("month-income", NOT_TRACKED);
      $("month-income").classList.add("is-pending");
    } else {
      put("month-income", signed(summary.income, "+", null));
      $("month-income").classList.toggle("money-in", amount(summary.income) !== null);
    }
    $("month-income").title = "Tổng tiền vào có ngày thuộc tháng này, gồm cả khoản đã chuyển sang tháng sau.";
    const coverage = summary.budget_coverage_from ?? null;
    const late = tracked && data.month === start && coverage !== null && coverage > `${data.month}-01`;
    const lateText = !late ? "" : summary.budget_basis === "actual" ? `Ngân sách đã chốt có thể thiếu tiền vào trước ${dateLabel(coverage, true)}.` : `IN được ghi từ ngày ${dateLabel(coverage, true)}; tiền vào trước đó có thể thiếu.`;
    $("month-actual-note").textContent = lateText;
    $("month-actual-note").hidden = !late;
  }

  function updateReadAge() {
    const label = $("last-read");
    const relative = (seconds) => seconds === null ? "chưa rõ mốc" : seconds < 60 ? "vừa xong" : seconds < 3600 ? `${count(Math.floor(seconds / 60))} phút trước` : seconds < 86400 ? `${count(Math.floor(seconds / 3600))} giờ trước` : `${count(Math.floor(seconds / 86400))} ngày trước`;
    const seconds = ageSeconds(lastBankSync);
    label.classList.toggle("is-stale", statusFailed);
    label.textContent = seconds !== null ? `Đồng bộ ngân hàng ${relative(seconds)}` : lastBankSync === null || lastBankSync === undefined ? "Chưa có mốc đồng bộ ngân hàng" : "Chưa rõ mốc đồng bộ ngân hàng";
    const localStamp = lastReadAt === null ? null : new Date(lastReadAt).toISOString();
    label.title = `Đồng bộ ngân hàng: ${timestamp(lastBankSync)}; đọc SQLite: ${timestamp(localStamp)} (${relative(ageSeconds(localStamp))}).${errors.calendar ? " Lần đọc SQLite chưa cập nhật." : ""}${statusFailed ? " Chưa cập nhật trạng thái đồng bộ ngân hàng." : ""}`;
  }

  function freshestToday() {
    return todaySnapshot?.date > today ? todaySnapshot.date : today;
  }

  function updateDayVisibility() {
    const visible = selectedDate !== today || openedDate === selectedDate;
    if (labelTarget && (!visible || labelTarget.date !== selectedDate)) closeLabelEditor(false);
    $("day-detail").hidden = !visible;
    document.querySelector(".calendar-layout").classList.toggle("has-day", visible);
    for (const button of $("calendar-days").querySelectorAll("button")) button.setAttribute("aria-pressed", String(visible && button.dataset.date === selectedDate));
    $("today-transactions").disabled = !calendar && !todaySnapshot;
    $("today-transactions").setAttribute("aria-expanded", String(visible && selectedDate === freshestToday()));
  }

  function validCalendarData(data, month) {
    if (data?.month !== month || !validDate(data.today) || !data.summary || !Array.isArray(data.days)) return false;
    const last = civilDate(`${month}-01`);
    last.setUTCMonth(last.getUTCMonth() + 1, 0);
    return data.days.length === last.getUTCDate() && data.days.every((record, index) =>
      record.date === `${month}-${String(index + 1).padStart(2, "0")}` &&
      (record.spent === null || amount(record.spent) !== null && amount(record.spent) >= 0n) &&
      (record.budget === null || amount(record.budget) !== null && amount(record.budget) >= 0n) &&
      validIncomeDay(record)) && validIncomeSummary(data.summary);
  }

  // New income fields may be absent or null (unknown); a present value must be a valid integer string.
  function optionalAmount(value) {
    if (value === null || value === undefined) return true;
    const parsed = amount(value);
    return parsed !== null && parsed >= 0n;
  }

  function validIncomeDay(record) {
    return optionalAmount(record.income);
  }

  function validIncomeSummary(summary) {
    return validBudgetBasis(summary.budget_basis) && optionalAmount(summary.income) &&
      (summary.budget_start_month == null || validMonth(summary.budget_start_month)) &&
      (summary.budget_coverage_from == null || validDate(summary.budget_coverage_from));
  }

  async function loadToday() {
    todayRequest?.abort();
    todayRequest = new AbortController();
    const version = ++todayVersion;
    $("today-card").setAttribute("aria-busy", "true");
    try {
      const data = await readApi("/api/today", todayRequest);
      if (version !== todayVersion) return;
      if (!validDate(data?.date) || !["available", "absent"].includes(data.ledger_status) || ["spent", "budget", "remaining"].some((key) => data[key] !== null && amount(data[key]) === null) || !optionalAmount(data.income) || !validBudgetBasis(data.budget_basis)) throw new Error("Dữ liệu hôm nay không hợp lệ.");
      todaySnapshot = data;
      updateDayVisibility();
      $("today-date").textContent = dateLabel(data.date, true);
      showSigned("today-spent", data.spent, MINUS, "Chưa có sổ ghi");
      showSigned("today-income", data.income, "+", data.ledger_status === "absent" ? "Chưa có sổ ghi" : "Chưa rõ");
      $("today-budget").textContent = money(data.budget, "Chưa có mốc");
      $("today-remaining").textContent = money(data.remaining, "Chưa xác định");
      $("today-remaining").classList.toggle("is-over", amount(data.remaining) !== null && amount(data.remaining) < 0n);
      $("today-message").textContent = data.ledger_status === "absent" ? "Chưa có dữ liệu khoản chi." : data.budget === null ? "Hôm nay chưa có ngân sách; chưa tính số còn lại." : "";
      $("today-message").hidden = $("today-message").textContent === "";
      $("today-card").classList.remove("is-stale");
      setError("today", null);
      return true;
    } catch (error) {
      if (error.name === "AbortError" || version !== todayVersion) return;
      $("today-card").classList.add("is-stale");
      $("today-message").hidden = false;
      $("today-message").textContent = todaySnapshot ? "Giữ lần đọc trước, chưa cập nhật hôm nay. Kiểm tra ngày hiển thị." : "Chưa đọc được hôm nay; không thay bằng số 0.";
      setError("today", readableError(error));
    } finally {
      if (version === todayVersion) $("today-card").setAttribute("aria-busy", "false");
    }
  }

  function clearComparison(message) {
    $("comparison-difference").textContent = "—";
    $("comparison-percent").textContent = "Chưa có mốc so sánh.";
    $("comparison-range").textContent = message;
    $("comparison-message").textContent = "";
    $("comparison-message").hidden = true;
    $("comparison").classList.remove("is-over", "is-lower", "is-stale");
    setError("comparison", null);
  }

  function sumField(records, key) {
    let total = 0n;
    for (const record of records) {
      const value = amount(record[key]);
      if (value === null) return null;
      total += value;
    }
    return total;
  }

  async function loadComparison(data, force = false) {
    comparisonRequest?.abort();
    const version = ++comparisonVersion;
    const monthVersion = calendarVersion;
    if (data.month === "0001-01" || data.month > data.today.slice(0, 7)) {
      clearComparison(data.month === "0001-01" ? "Không có tháng trước trong phạm vi lịch." : "Tháng chưa bắt đầu; chưa so sánh nhịp chi.");
      $("comparison").setAttribute("aria-busy", "false");
      return true;
    }
    const previousDate = civilDate(`${data.month}-01`);
    previousDate.setUTCMonth(previousDate.getUTCMonth() - 1);
    const previousMonth = `${String(previousDate.getUTCFullYear()).padStart(4, "0")}-${String(previousDate.getUTCMonth() + 1).padStart(2, "0")}`;
    const cached = comparisonCache.get(previousMonth);
    // A past month changes only when a bank sync commits; the time limit covers a missed sync stamp.
    let previous = !force && cached && cached.sync === lastBankSync && Date.now() - cached.at < COMPARISON_CACHE_MS ? cached.data : null;
    comparisonRequest = new AbortController();
    $("comparison").setAttribute("aria-busy", "true");
    $("comparison-message").textContent = "Đang đọc tháng trước…";
    try {
      if (!previous) {
        previous = await readApi(`/api/calendar?${new URLSearchParams({ month: previousMonth })}`, comparisonRequest);
        if (version !== comparisonVersion || monthVersion !== calendarVersion || calendar?.month !== data.month) return;
        if (!validCalendarData(previous, previousMonth)) throw new Error("Dữ liệu tháng trước không hợp lệ.");
        if (previous.ledger_status === "available") {
          if (comparisonCache.size >= 12) comparisonCache.clear();
          comparisonCache.set(previousMonth, { sync: lastBankSync, at: Date.now(), data: previous });
        } else comparisonCache.delete(previousMonth);
      }
      const currentMonth = data.month === data.today.slice(0, 7);
      const days = currentMonth ? Math.min(data.summary.elapsed_days, previous.days.length) : null;
      const currentRecords = days === null ? data.days : data.days.slice(0, days);
      const previousRecords = days === null ? previous.days : previous.days.slice(0, days);
      const currentSpent = data.ledger_status === "available" ? sumField(currentRecords, "spent") : null;
      const previousSpent = previous.ledger_status === "available" ? sumField(previousRecords, "spent") : null;
      $("comparison-range").textContent = days === null ? `Toàn tháng ${Number(data.month.slice(5))}/${data.month.slice(0, 4)} so với ${Number(previousMonth.slice(5))}/${previousMonth.slice(0, 4)}` : `Ngày 01–${String(days).padStart(2, "0")} của ${Number(data.month.slice(5))}/${data.month.slice(0, 4)} và ${Number(previousMonth.slice(5))}/${previousMonth.slice(0, 4)} · cùng ${days} ngày`;
      if (currentSpent === null || previousSpent === null) {
        $("comparison-difference").textContent = "Chưa xác định";
        $("comparison-percent").textContent = "Một kỳ chưa có sổ ghi; không coi là chi 0.";
        $("comparison").classList.remove("is-over", "is-lower");
      } else {
        const difference = currentSpent - previousSpent;
        $("comparison-difference").textContent = `${difference > 0n ? "+" : ""}${moneyFormat.format(difference)}`;
        $("comparison").classList.toggle("is-over", difference > 0n);
        $("comparison").classList.toggle("is-lower", difference < 0n);
        const absolute = difference < 0n ? -difference : difference;
        const percent = previousSpent > 0n ? (absolute * 1000n + previousSpent / 2n) / previousSpent : null;
        $("comparison-percent").textContent = percent === null ? "Không tính % khi mốc chi tháng trước bằng 0." : `${difference < 0n ? "−" : difference > 0n ? "+" : ""}${integerFormat.format(percent / 10n)},${percent % 10n}% so với phần chi tháng trước`;
      }
      $("comparison-message").textContent = "";
      $("comparison-message").hidden = true;
      $("comparison").classList.remove("is-stale");
      setError("comparison", null);
      return true;
    } catch (error) {
      if (error.name === "AbortError" || version !== comparisonVersion || monthVersion !== calendarVersion) return;
      $("comparison").classList.add("is-stale");
      $("comparison-message").textContent = "Chưa cập nhật so sánh. Số cũ, nếu còn, chưa được đọc lại.";
      $("comparison-message").hidden = false;
      setError("comparison", readableError(error));
    } finally {
      if (version === comparisonVersion) $("comparison").setAttribute("aria-busy", "false");
    }
  }

  function svgElement(tag, attributes = {}, text) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderCumulative(data) {
    const records = data.days.filter((record) => record.date <= data.today);
    let spent = 0n;
    let budget = 0n;
    let knownSpent = true;
    let knownBudget = true;
    const points = records.map((record) => {
      const expense = amount(record.spent);
      const allowance = amount(record.budget);
      knownSpent = knownSpent && expense !== null;
      knownBudget = knownBudget && allowance !== null;
      if (knownSpent) spent += expense;
      if (knownBudget) budget += allowance;
      return { date: record.date, spent: knownSpent ? spent : null, budget: knownBudget ? budget : null };
    });
    const rows = points.map((point) => {
      const row = element("tr");
      const label = element("th", "", dateLabel(point.date));
      label.scope = "row";
      row.append(label, element("td", "", point.spent === null ? "Chưa xác định" : moneyFormat.format(point.spent)), element("td", "", point.budget === null ? "Chưa xác định" : moneyFormat.format(point.budget)));
      return row;
    });
    $("cumulative-table").replaceChildren(...rows);
    $("chart-details").hidden = points.length === 0;
    $("cumulative-chart").replaceChildren();
    const last = points[points.length - 1];
    $("chart-total-date").textContent = last ? `${dateLabel(points[0].date)}–${dateLabel(last.date)}` : "Chưa có ngày đã đến";
    $("cumulative").title = last ? `Tích lũy từ đầu tháng đến ${dateLabel(last.date, true)}, tính cả ngày này.` : "Tháng chưa bắt đầu.";
    $("chart-message").hidden = false;
    if (points.length === 0) {
      $("chart-message").textContent = "Tháng chưa bắt đầu; không vẽ chi hoặc ngân sách tích lũy tương lai.";
      return;
    }
    $("chart-message").textContent = !knownBudget ? "Thiếu ngân sách; không nối phần chưa rõ." : "";
    if (!knownSpent) $("chart-message").textContent += " Chưa có đủ khoản chi để vẽ tích lũy.";
    $("chart-message").hidden = $("chart-message").textContent === "";
    const values = points.flatMap((point) => [point.spent, point.budget]).filter((value) => value !== null);
    if (values.length === 0) return;
    const maximum = values.reduce((highest, value) => value > highest ? value : highest, 1n);
    const x = (index) => points.length === 1 ? 360 : 16 + index * 688 / (points.length - 1);
    const y = (value) => 166 - Number(value * 10000n / maximum) * 0.014;
    const svg = svgElement("svg", { viewBox: "0 0 720 194", role: "img", "aria-labelledby": "chart-title chart-description" });
    svg.append(svgElement("title", { id: "chart-title" }, `Chi và ngân sách tích lũy tháng ${data.month}`), svgElement("desc", { id: "chart-description" }, "Chi là đường liền với điểm tròn; ngân sách là đường đứt với điểm vuông. Chỉ vẽ ngày đã đến. Bảng bên dưới cung cấp số tiền chính xác."));
    svg.append(svgElement("line", { x1: 16, y1: 166, x2: 704, y2: 166, class: "chart-axis" }));
    for (const key of ["spent", "budget"]) {
      const known = points.map((point, index) => ({ point, index })).filter(({ point }) => point[key] !== null);
      if (known.length === 0) continue;
      svg.append(svgElement("polyline", { class: `chart-line chart-${key}`, points: known.map(({ point, index }) => `${x(index)},${y(point[key])}`).join(" ") }));
      for (const { point, index } of known) {
        const marker = key === "spent" ? svgElement("circle", { cx: x(index), cy: y(point[key]), r: 2.5, class: "chart-marker chart-spent" }) : svgElement("rect", { x: x(index) - 2.5, y: y(point[key]) - 2.5, width: 5, height: 5, class: "chart-marker chart-budget" });
        marker.append(svgElement("title", {}, `${dateLabel(point.date)} · ${key === "spent" ? "Chi" : "Ngân sách"}: ${moneyFormat.format(point[key])}`));
        svg.append(marker);
      }
    }
    $("cumulative-chart").append(svg);
  }

  function animateDaySelection() {
    const heading = $("day-title").parentElement;
    for (const animation of heading.getAnimations()) animation.cancel();
    if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      heading.animate([{ backgroundColor: "var(--forest-soft)" }, { backgroundColor: "transparent" }], { duration: 150, easing: "cubic-bezier(0.16, 1, 0.3, 1)" });
    }
  }

  function selectDay(date) {
    if (!validDate(date)) return;
    ++daySelectionVersion;
    selectedDate = date;
    openedDate = date;
    updateDayVisibility();
    loadDay(date);
    animateDaySelection();
    if (window.matchMedia("(max-width: 1050px)").matches) {
      $("day-title").focus({ preventScroll: true });
      $("day-title").scrollIntoView({ block: "start" });
    }
    if (requestedMonth !== date.slice(0, 7)) loadCalendar(date.slice(0, 7), date);
  }

  function renderCalendar(data) {
    const list = $("calendar-days");
    const focusDate = list.contains(document.activeElement) ? document.activeElement.dataset.date : null;
    const footers = new Map(Array.from(list.querySelectorAll(".calendar-week-total"), (footer) => [`${footer.dataset.weekStart}/${footer.dataset.weekEnd}`, footer]));
    const [year, month] = data.month.split("-");
    $("calendar-title").textContent = `Tháng ${Number(month)}`;
    $("calendar-year").textContent = year;
    $("month-input").value = data.month;
    $("previous-month").disabled = data.month === "0001-01";
    $("next-month").disabled = data.month === "9998-12";
    const cells = [];
    const firstWeekday = (civilDate(`${data.month}-01`).getUTCDay() + 6) % 7;
    for (let index = 0; index < firstWeekday; index += 1) {
      const blank = element("div", "calendar-blank");
      blank.setAttribute("aria-hidden", "true");
      cells.push(blank);
    }
    for (const record of data.days) {
      const button = element("button", "calendar-day");
      button.type = "button";
      button.dataset.date = record.date;
      button.setAttribute("aria-pressed", String(record.date === selectedDate));
      const spent = amount(record.spent);
      const budget = amount(record.budget);
      const over = spent !== null && budget !== null && spent > budget;
      const isFuture = record.date > today;
      const weekday = civilDate(record.date).getUTCDay();
      button.classList.toggle("is-over", over);
      button.classList.toggle("is-today", record.date === today);
      button.classList.toggle("is-weekend", weekday === 0 || weekday === 6);
      button.classList.toggle("is-future", isFuture);
      if (record.date === today) button.setAttribute("aria-current", "date");
      const empty = record.transactions === 0 && spent === 0n;
      const income = amount(record.income);
      const hasIncome = income !== null && income > 0n;
      const labels = [`${weekNames[weekday]}, ${dateLabel(record.date, true)}`, empty ? "Chưa có khoản chi" : `chi ${money(record.spent)}`];
      if (hasIncome) labels.push(`tiền vào ${money(record.income)}`);
      labels.push(`ngân sách ${money(record.budget)}`);
      if (record.transactions > 0) labels.push(`${count(record.transactions)} khoản chi`);
      if (over) labels.push("vượt ngân sách đã biết");
      button.setAttribute("aria-label", labels.join("; "));
      button.title = labels.join(" · ");
      button.append(element("span", "day-number", String(Number(record.date.slice(8)))));
      button.append(element("span", "day-amount", spent === null || empty ? "—" : signed(record.spent, MINUS)));
      if (hasIncome) button.append(element("span", "day-amount-in", signed(record.income, "+")));
      if (over) button.append(element("span", "day-over", "Vượt"));
      if (record.transactions > 0) button.append(element("span", "day-captured", `${count(record.transactions)} khoản chi`));
      if (record.transactions > 0 && budget !== null) button.append(element("span", "day-marker"));
      cells.push(button);
    }
    const trailing = (7 - cells.length % 7) % 7;
    for (let index = 0; index < trailing; index += 1) {
      const blank = element("div", "calendar-blank");
      blank.setAttribute("aria-hidden", "true");
      cells.push(blank);
    }
    const nodes = [];
    for (let offset = 0; offset < cells.length; offset += 7) {
      const row = cells.slice(offset, offset + 7);
      row[6].classList.add("calendar-row-end");
      const dates = row.filter((cell) => cell.dataset.date).map((cell) => cell.dataset.date);
      const records = data.days.filter((record) => dates.includes(record.date));
      const total = sumField(records, "spent");
      const incomeTotal = sumField(records, "income");
      const footer = footers.get(`${dates[0]}/${dates[dates.length - 1]}`) || element("div", "calendar-week-total");
      footer.dataset.weekStart = dates[0];
      footer.dataset.weekEnd = dates[dates.length - 1];
      if (!footer.children.length) footer.append(element("span"), element("div", "week-amounts"));
      const label = `Tuần ${dateLabel(dates[0])}–${dateLabel(dates[dates.length - 1])}${records.length < 7 ? " · phần trong tháng" : ""}`;
      if (footer.firstElementChild.textContent !== label) footer.firstElementChild.textContent = label;
      const amounts = footer.lastElementChild;
      if (!amounts.firstElementChild) amounts.append(element("strong"));
      const outNode = amounts.firstElementChild;
      const value = total === null ? "Chưa có sổ ghi" : signed(String(total), MINUS);
      if (outNode.textContent !== value) outNode.textContent = value;
      outNode.classList.toggle("money-out", total !== null);
      // The IN amount appears only when the week has income; the OUT node stays the same node.
      let inNode = amounts.children[1] || null;
      if (incomeTotal !== null && incomeTotal > 0n) {
        if (!inNode) { inNode = element("strong", "money-in"); amounts.append(inNode); }
        const incomeValue = signed(String(incomeTotal), "+");
        if (inNode.textContent !== incomeValue) inNode.textContent = incomeValue;
      } else inNode?.remove();
      nodes.push(...row, footer);
    }
    // Keep retained week rows attached so native money focus and scroll survive refresh.
    const retained = new Set(nodes);
    for (const node of Array.from(list.children)) if (!retained.has(node)) node.remove();
    let cursor = list.firstElementChild;
    for (const node of nodes) {
      if (node === cursor) cursor = cursor.nextElementSibling;
      else list.insertBefore(node, cursor);
    }
    if (focusDate) {
      const focused = Array.from($("calendar-days").querySelectorAll("button")).find((button) => button.dataset.date === focusDate);
      focused?.focus({ preventScroll: true });
    }
  }

  async function loadCalendar(month, preferredDate = null, force = false) {
    if (!validMonth(month)) return;
    const selectionVersion = daySelectionVersion;
    comparisonRequest?.abort();
    ++comparisonVersion;
    if (calendar?.month !== month) clearComparison("Đang chờ tháng đang xem.");
    $("comparison").setAttribute("aria-busy", "true");
    requestedMonth = month;
    $("month-input").value = month;
    calendarRequest?.abort();
    calendarRequest = new AbortController();
    const version = ++calendarVersion;
    $("calendar-days").setAttribute("aria-busy", "true");
    $("calendar-message").hidden = false;
    $("calendar-message").textContent = `Đang đọc tháng ${Number(month.slice(5))}/${month.slice(0, 4)}…`;
    try {
      const data = await readApi(`/api/calendar?${new URLSearchParams({ month })}`, calendarRequest);
      if (version !== calendarVersion) return;
      if (!validCalendarData(data, month)) throw new Error("Dữ liệu lịch không hợp lệ; chưa thay sổ đang hiển thị.");
      const followToday = openedDate === null && selectionVersion === daySelectionVersion && (selectedDate === today || preferredDate === today) && month === today.slice(0, 7);
      if (followToday && data.today.slice(0, 7) !== month) {
        loadCalendar(data.today.slice(0, 7), data.today, force);
        return;
      }
      calendar = data;
      today = data.today;
      if (followToday) selectedDate = today;
      else if (preferredDate && selectionVersion === daySelectionVersion && preferredDate.startsWith(`${month}-`)) selectedDate = preferredDate;
      if (!selectedDate.startsWith(`${month}-`)) selectedDate = today.startsWith(`${month}-`) ? today : `${month}-01`;
      renderSummary(data);
      renderCalendar(data);
      updateDayVisibility();
      renderCumulative(data);
      const comparison = loadComparison(data, force);
      loadCategories(month);
      $("month-insights").classList.remove("is-stale");
      $("cumulative").classList.remove("is-stale");
      $("calendar").classList.remove("is-stale");
      $("calendar-message").textContent = data.ledger_status === "absent" ? "Chưa có dữ liệu khoản chi." : "";
      $("calendar-message").hidden = data.ledger_status === "available";
      lastReadAt = parseTimestamp(data.generated_at);
      setError("calendar", null);
      updateReadAge();
      return { at: lastReadAt, reads: Promise.all([comparison, loadDay(selectedDate, 0, true)]) };
    } catch (error) {
      if (error.name === "AbortError" || version !== calendarVersion) return;
      if (calendar) {
        requestedMonth = calendar.month;
        $("month-input").value = calendar.month;
        if (!selectedDate.startsWith(`${calendar.month}-`)) {
          selectedDate = day?.date.startsWith(`${calendar.month}-`) ? day.date : `${calendar.month}-01`;
          for (const button of $("calendar-days").querySelectorAll("button")) button.setAttribute("aria-pressed", String(button.dataset.date === selectedDate));
        }
      }
      $("calendar").classList.add("is-stale");
      $("month-insights").classList.add("is-stale");
      $("cumulative").classList.add("is-stale");
      $("comparison").classList.add("is-stale");
      $("comparison").setAttribute("aria-busy", "false");
      $("comparison-message").textContent = "Lịch chưa cập nhật; so sánh này chưa được đọc lại.";
      $("comparison-message").hidden = false;
      $("calendar-message").textContent = calendar ? "Giữ lần đọc trước. Lịch này chưa được cập nhật." : "Chưa đọc được lịch; không có dữ liệu thay thế.";
      setError("calendar", readableError(error));
      updateDayVisibility();
      updateReadAge();
    } finally {
      if (version === calendarVersion) $("calendar-days").setAttribute("aria-busy", "false");
    }
  }

  async function loadCategories(month) {
    categoriesRequest?.abort();
    categoriesRequest = new AbortController();
    const version = ++categoriesVersion;
    $("categories").setAttribute("aria-busy", "true");
    $("categories").classList.add("is-stale");
    $("categories-message").hidden = false;
    $("categories-message").textContent = "Đang đọc chi theo phân loại…";
    try {
      const data = await readApi(`/api/categories?${new URLSearchParams({ month })}`, categoriesRequest);
      if (version !== categoriesVersion || month !== requestedMonth) return;
      if (data?.month !== month || !["available", "absent"].includes(data.ledger_status) || !Array.isArray(data.categories) ||
          (data.ledger_status === "available" ? amount(data.spent) === null || amount(data.spent) < 0n : data.spent !== null || data.categories.length !== 0) ||
          data.categories.some((row) => !row || typeof row.category !== "string" || !Number.isSafeInteger(row.count) || row.count < 0 || amount(row.spent) === null || amount(row.spent) < 0n)) throw new Error("Invalid category data");
      const rows = data.categories.map((record) => {
        const row = element("tr");
        const heading = element("th", null, record.category);
        heading.scope = "row";
        const share = percentOfDay(record.spent, data.spent);
        row.append(heading, element("td", null, count(record.count)), element("td", null, money(record.spent)),
          element("td", null, share === null ? "—" : `${integerFormat.format(share / 10n)},${share % 10n}%`));
        return row;
      });
      $("categories-body").replaceChildren(...rows);
      $("categories-table").hidden = rows.length === 0;
      $("categories-message").textContent = data.ledger_status === "absent" ? "Chưa có dữ liệu khoản chi." : rows.length === 0 ? "Chưa có khoản chi trong tháng này." : "";
      $("categories-message").hidden = rows.length > 0;
      $("categories").classList.remove("is-stale");
    } catch (error) {
      if (error.name === "AbortError" || version !== categoriesVersion || month !== requestedMonth) return;
      $("categories-message").textContent = "Chưa tải được chi theo phân loại.";
      $("categories-message").hidden = false;
    } finally {
      if (version === categoriesVersion) $("categories").setAttribute("aria-busy", "false");
    }
  }

  function setDaySort(sort) {
    daySort = sort;
    $("sort-latest").setAttribute("aria-pressed", String(sort === "latest"));
    $("sort-amount").setAttribute("aria-pressed", String(sort === "amount"));
    $("day-order-note").hidden = day?.sort !== "latest" || day.timed_transactions !== 0 || day.total === 0;
  }

  function renderDay(data) {
    setDaySort(data.sort);
    $("day-time-note").hidden = !(data.timed_transactions > 0 && data.timed_transactions < data.total);
    $("day-title").textContent = `Ngày ${dateLabel(data.date)}`;
    $("day-weekday").textContent = `${weekNames[civilDate(data.date).getUTCDay()]} · ${data.date.slice(0, 4)}`;
    showSigned("day-spent", data.spent, MINUS, "Chưa có sổ ghi");
    const incomeDetail = data.income ?? null;
    if (incomeDetail === null) {
      $("day-income-total").textContent = NOT_TRACKED;
      $("day-income-total").classList.remove("money-in");
    } else showSigned("day-income-total", incomeDetail.total, "+", "Chưa rõ");
    $("day-budget-label").textContent = data.budget_basis === "actual" ? "Ngân sách ngày (đã chốt)" : "Ngân sách dự kiến ngày";
    $("day-budget").textContent = money(data.budget, "Chưa có mốc");
    const budget = amount(data.budget);
    $("day-budget-note").textContent = budget === null ? "Chưa có ngân sách ngày này." : "";
    $("day-budget-note").hidden = budget !== null;
    $("transaction-count").textContent = data.ledger_status === "absent" ? "Chưa có dữ liệu khoản chi" : `${count(data.total)} khoản chi`;
    $("day-message").textContent = data.ledger_status === "absent" ? "Chưa có dữ liệu khoản chi." : data.total === 0 ? "Chưa có khoản chi trong ngày này." : transactions.length < data.total ? `Đang hiển thị ${count(transactions.length)} / ${count(data.total)} khoản chi.` : "";
    $("day-message").hidden = $("day-message").textContent === "";
    const list = $("transactions");
    const focused = list.contains(document.activeElement) ? document.activeElement : null;
    const focusScroll = focused?.scrollLeft;
    const existing = new Map(Array.from(list.children, (row) => [row.dataset.id, row]));
    const rows = transactions.map((record) => {
      const row = existing.get(record.id) || element("li", "transaction");
      row.dataset.id = record.id;
      const value = signed(record.amount, MINUS);
      const moneyNode = row.querySelector(".transaction-amount") || element("span", "transaction-amount money-out");
      if (moneyNode.textContent !== value) moneyNode.textContent = value;
      if (!moneyNode.parentNode) row.append(moneyNode);
      row.querySelector(".transaction-description")?.remove();
      const share = percentOfDay(record.amount, data.spent);
      const label = share === null ? "—" : `${integerFormat.format(share / 10n)},${share % 10n}% tổng ngày`;
      const ratio = row.querySelector(".transaction-share") || element("span", "transaction-share");
      if (ratio.textContent !== label) ratio.textContent = label;
      ratio.title = share === null ? "Không tính tỷ trọng khi tổng ngày bằng 0 hoặc chưa biết." : `Tỷ trọng trong toàn bộ ${money(data.spent)} của ngày, không chỉ trang đã tải.`;
      const description = element("div", "transaction-description");
      if (record.time) description.append(element("span", "transaction-time", record.time));
      if (record.note) description.append(element("span", "transaction-note", record.note));
      if (record.label) description.append(element("span", "transaction-label", record.label));
      else if (typeof record.category === "string") description.append(element("span", record.category_source === "keyword" ? "transaction-category" : "transaction-category is-none", record.category));
      row.insertBefore(description, row.firstElementChild);
      if (!ratio.parentNode) row.insertBefore(ratio, moneyNode);
      let edit = row.querySelector(".transaction-edit");
      if (writeKey) {
        edit ||= element("button", "transaction-edit button button-outline");
        edit.textContent = record.label ? "Sửa nhãn" : "Gán nhãn";
        edit.type = "button";
        edit.onclick = () => openLabelEditor(record, edit);
        if (!edit.parentNode) row.append(edit);
      } else edit?.remove();
      return row;
    });
    syncChildren(list, rows);
    if (focused && document.activeElement === document.body) (focused.isConnected ? focused : $("day-title")).focus({ preventScroll: true });
    if (focused && document.activeElement === focused) focused.scrollLeft = focusScroll;
    $("load-more").hidden = data.next_offset === null || data.ledger_status === "absent";
    $("load-more").disabled = false;
    renderDayIncome(incomeDetail);
  }

  function renderDayIncome(detail) {
    const list = $("day-income-list");
    const message = $("day-income-message");
    if (detail === null) {
      $("day-income-count").textContent = "—";
      message.textContent = NOT_TRACKED;
      message.hidden = false;
      list.replaceChildren();
      return;
    }
    $("day-income-count").textContent = `${count(detail.count)} khoản tiền vào`;
    message.textContent = detail.count === 0 ? "Chưa có tiền vào trong ngày này." : detail.transactions.length < detail.count ? `Đang hiển thị ${count(detail.transactions.length)} / ${count(detail.count)} khoản tiền vào mới nhất.` : "";
    message.hidden = message.textContent === "";
    const existing = new Map(Array.from(list.children, (row) => [row.dataset.id, row]));
    const rows = detail.transactions.map((record) => {
      const row = existing.get(record.id) || element("li", "transaction");
      row.dataset.id = record.id;
      const value = signed(record.amount, "+");
      const moneyNode = row.querySelector(".transaction-amount") || element("span", "transaction-amount money-in");
      if (moneyNode.textContent !== value) moneyNode.textContent = value;
      if (!moneyNode.parentNode) row.append(moneyNode);
      row.querySelector(".transaction-description")?.remove();
      const description = element("div", "transaction-description");
      if (record.time) description.append(element("span", "transaction-time", record.time));
      if (record.note) description.append(element("span", "transaction-note", record.note));
      if (record.next_month && validMonth(record.budget_month)) description.append(element("span", "transaction-next", `Tính cho tháng ${record.budget_month.slice(5)}/${record.budget_month.slice(0, 4)}`));
      row.insertBefore(description, moneyNode);
      let shift = row.querySelector(".income-shift");
      if (writeKey) {
        shift ||= element("button", "income-shift button button-outline", "Tính cho tháng sau");
        shift.type = "button";
        shift.setAttribute("aria-pressed", String(record.next_month));
        shift.disabled = incomeShiftSaving;
        shift.onclick = () => toggleIncomeShift(record, shift);
        if (!shift.parentNode) row.append(shift);
      } else shift?.remove();
      return row;
    });
    syncChildren(list, rows);
  }

  function updateEditState() {
    $("edit-unlock").hidden = !writeEnabled || writeKey !== null;
    $("edit-lock").hidden = writeKey === null;
    if (writeKey === null) closeLabelEditor(false);
    if (day) renderDay(day);
  }

  function openLabelEditor(record, button) {
    labelTarget = { id: record.id, date: selectedDate, button };
    $("label-input").value = record.label || "";
    $("label-editor-title").textContent = `Gán nhãn cho khoản ${money(record.amount)}`;
    $("label-editor").hidden = false;
    $("label-input").focus();
  }

  function closeLabelEditor(restoreFocus = true) {
    const target = labelTarget;
    labelTarget = null;
    $("label-editor").hidden = true;
    if (restoreFocus && target?.button?.isConnected) target.button.focus();
  }

  async function saveLabel(clear) {
    if (!labelTarget || labelSaving) return;
    const target = labelTarget;
    let label = null;
    if (!clear) {
      try { label = normalizeLabelText($("label-input").value, 40); } catch (error) { $("edit-message").textContent = error.message; return; }
    }
    labelSaving = true;
    const buttons = $("label-editor").querySelectorAll('button[type="submit"], #label-clear');
    for (const button of buttons) button.disabled = true;
    try {
      await writeApi("/api/labels", { transaction_id: target.id, label });
      if (labelTarget === target) {
        $("edit-message").textContent = label === null ? "Đã xóa nhãn." : "Đã lưu nhãn.";
        closeLabelEditor();
      }
      await loadDay(selectedDate, 0, true);
      loadLabelOptions();
    } catch (error) {
      $("edit-message").textContent = error.message;
    } finally {
      labelSaving = false;
      for (const button of buttons) button.disabled = false;
    }
  }

  async function toggleIncomeShift(record, button) {
    if (incomeShiftSaving) return;
    incomeShiftSaving = true;
    const next = !record.next_month;
    const refocus = document.activeElement === button;
    for (const control of $("day-income-list").querySelectorAll(".income-shift")) control.disabled = true;
    try {
      await writeApi("/api/income-shift", { transaction_id: record.id, next_month: next });
      $("edit-message").textContent = next ? "Đã tính khoản tiền vào cho tháng sau." : "Đã bỏ tính khoản tiền vào cho tháng sau.";
      // The shift changes the actual budget of two months, so reload the month, the open day and the today card.
      const [snapshot] = await Promise.all([loadCalendar(requestedMonth, selectedDate, true), loadToday()]);
      if (snapshot) await snapshot.reads;
      else await loadDay(selectedDate, 0, true);
    } catch (error) {
      $("edit-message").textContent = error.message;
    } finally {
      incomeShiftSaving = false;
      const list = $("day-income-list");
      for (const control of list.querySelectorAll(".income-shift")) control.disabled = false;
      if (refocus) Array.from(list.children).find((row) => row.dataset.id === record.id)?.querySelector(".income-shift")?.focus({ preventScroll: true });
    }
  }

  async function loadSession() {
    try {
      const data = await readApi("/api/session", new AbortController());
      writeEnabled = data?.write_enabled === true;
    } catch {
      writeEnabled = false;
    }
    updateEditState();
  }

  async function loadLabelOptions() {
    try {
      const data = await readApi("/api/labels", new AbortController());
      if (!Array.isArray(data?.labels)) return;
      $("label-options").replaceChildren(...data.labels.filter((entry) => typeof entry?.label === "string").map((entry) => {
        const option = document.createElement("option");
        option.value = entry.label;
        return option;
      }));
    } catch {}
  }

  async function loadDay(date, offset = 0, preserve = false) {
    const sort = daySort;
    const keepCount = preserve && day?.date === date && day.sort === sort ? Math.max(50, transactions.length) : 50;
    const focusMore = document.activeElement === $("load-more");
    if (offset > 0 && (!day || day.date !== date || day.sort !== sort || transactions.length !== offset)) return loadDay(date);
    dayRequest?.abort();
    dayRequest = new AbortController();
    const version = ++dayVersion;
    $("day-detail").setAttribute("aria-busy", "true");
    $("load-more").disabled = true;
    if (!day || day.date !== date) {
      day = null;
      transactions = [];
      $("day-title").textContent = `Ngày ${dateLabel(date)}`;
      $("day-weekday").textContent = `${weekNames[civilDate(date).getUTCDay()]} · ${date.slice(0, 4)}`;
      for (const id of ["day-spent", "day-income-total", "day-budget", "transaction-count"]) {
        $(id).textContent = "—";
        $(id).classList.remove("money-out", "money-in");
      }
      $("day-budget-label").textContent = "Ngân sách dự kiến ngày";
      $("transactions").replaceChildren();
      $("day-income-list").replaceChildren();
      $("day-income-count").textContent = "—";
      $("day-income-message").hidden = true;
      $("load-more").hidden = true;
      $("day-time-note").hidden = true;
      $("day-order-note").hidden = true;
      $("day-budget-note").textContent = "Đang đọc ngân sách…";
      $("day-budget-note").hidden = false;
    }
    $("day-message").hidden = false;
    $("day-message").textContent = offset > 0 ? "Đang tải thêm khoản chi…" : day && day.sort !== sort ? "Đang đổi thứ tự khoản chi; danh sách bên dưới giữ thứ tự cũ." : "Đang tải khoản chi…";
    try {
      let data = offset > 0 ? day : null;
      let pageOffset = offset;
      const records = [];
      const ids = new Set(offset > 0 ? transactions.map((record) => record.id) : []);
      do {
        const page = await readApi(`/api/day?${new URLSearchParams({ date, offset: String(pageOffset), sort })}`, dayRequest);
        if (version !== dayVersion || date !== selectedDate || sort !== daySort) return;
        if (page?.date !== date || !Array.isArray(page.transactions) || page.transactions.length > 50 || !Number.isSafeInteger(page.total) || page.total < 0 || !validDayOrder(page, sort) || page.transactions.some((record) => !record || typeof record.id !== "string" || amount(record.amount) === null || amount(record.amount) < 0n || !validTransactionMetadata(record)) || (page.ledger_status === "available" ? amount(page.spent) === null || amount(page.spent) < 0n : page.spent !== null || page.total !== 0) || (page.budget !== null && (amount(page.budget) === null || amount(page.budget) < 0n)) || !validDayIncome(page)) throw new Error("Dữ liệu khoản chi không hợp lệ; chưa thay phần đã đọc.");
        const end = pageOffset + page.transactions.length;
        if (end > page.total || page.next_offset !== (end < page.total ? end : null) || (page.next_offset !== null && end <= pageOffset)) throw new Error("Phân trang khoản chi không hợp lệ; giữ danh sách đã đọc.");
        // ponytail: compare snapshot fields; add a ledger revision if metadata can change without totals changing.
        if (data && ["ledger_status", "total", "timed_transactions", "spent", "budget"].some((key) => page[key] !== data[key])) throw new Error("Khoản chi đã thay đổi trong lúc tải. Giữ danh sách cũ; bấm Cập nhật để đọc lại.");
        for (const record of page.transactions) {
          if (ids.has(record.id)) throw new Error("Khoản chi bị trùng giữa các trang; giữ danh sách đã đọc.");
          ids.add(record.id);
        }
        records.push(...page.transactions);
        data = page;
        pageOffset = page.next_offset;
      } while (offset === 0 && pageOffset !== null && records.length < keepCount);
      transactions = offset === 0 ? records : [...transactions, ...records];
      day = data;
      renderDay(data);
      if (offset > 0) {
        $("day-message").textContent = `Đã tải thêm ${count(records.length)} khoản chi; đang hiển thị ${count(transactions.length)} / ${count(data.total)} khoản chi.`;
        $("day-message").hidden = false;
      }
      if (focusMore && document.activeElement === document.body && $("load-more").hidden) {
        const target = offset > 0 ? $("transactions").children[offset] : $("day-title");
        if (target) {
          target.tabIndex = -1;
          target.focus({ preventScroll: offset === 0 });
        }
      }
      $("day-detail").classList.remove("is-stale");
      setError("day", null);
      return true;
    } catch (error) {
      if (error.name === "AbortError" || version !== dayVersion) return;
      if (day) setDaySort(day.sort);
      $("day-detail").classList.add("is-stale");
      $("day-message").textContent = day ? "Giữ phần đã đọc. Danh sách này chưa được cập nhật." : "Chưa tải được khoản chi ngày này.";
      setError("day", readableError(error));
    } finally {
      if (version === dayVersion) {
        $("day-detail").setAttribute("aria-busy", "false");
        $("load-more").disabled = false;
        if (focusMore && document.activeElement === document.body && !$("load-more").hidden) $("load-more").focus({ preventScroll: true });
      }
    }
  }

  // Income fields may be absent or null (unknown). Present values get the same strict checks as OUT rows.
  function validDayIncome(page) {
    const detail = page.income;
    if (!validBudgetBasis(page.budget_basis)) return false;
    if (detail === null || detail === undefined) return true;
    if (typeof detail !== "object" || Array.isArray(detail) || amount(detail.total) === null || amount(detail.total) < 0n || !Number.isSafeInteger(detail.count) || detail.count < 0 ||
        !Array.isArray(detail.transactions) || detail.transactions.length > 50 || detail.transactions.length > detail.count) return false;
    const ids = new Set();
    return detail.transactions.every((record) => {
      if (!record || typeof record.id !== "string" || record.id === "" || ids.has(record.id)) return false;
      ids.add(record.id);
      return amount(record.amount) !== null && amount(record.amount) >= 0n && validTransactionMetadata(record) && typeof record.next_month === "boolean" &&
        (record.budget_month === null || typeof record.budget_month === "string" && /^\d{4}-(0[1-9]|1[0-2])$/.test(record.budget_month));
    });
  }

  function renderStatus(data) {
    const sync = data.sync || {};
    const result = outcome(sync.status);
    const age = freshness(sync.freshness);
    $("sync-badge").className = `sync-badge state-${age === "stale" ? "stale" : result}`;
    $("sync-badge").textContent = `Đồng bộ: ${age === "stale" ? "Đã cũ" : outcomeNames[result]}`;
    $("sync-badge").title = `Đồng bộ ngân hàng gần nhất: ${timestamp(sync.last_success_at)}`;
    $("sync-outcome").textContent = `${outcomeNames[result]} · ${freshnessNames[age]}`;
    $("sync-attempt").textContent = timestamp(sync.last_attempt_at);
    $("sync-success").textContent = timestamp(sync.last_success_at);
    $("sync-failures").textContent = count(sync.consecutive_failures);
    $("sync-error").textContent = sync.last_error_code === null ? "Không có" : Object.hasOwn(errorNames, sync.last_error_code) ? errorNames[sync.last_error_code] : "Chưa rõ";
    const outbox = data.outbox || {};
    $("outbox-pending").textContent = count(outbox.pending);
    $("outbox-delivered").textContent = count(outbox.delivered);
    $("outbox-quarantined").textContent = count(outbox.quarantined);
    const backup = data.backup || {};
    const backups = { verified: "SQLite toàn vẹn", invalid: "Snapshot không hợp lệ", error: "Chưa xác minh được", absent: "Chưa có snapshot" };
    $("backup-state").textContent = Object.hasOwn(backups, backup.status) ? backups[backup.status] : "Chưa rõ";
    $("backup-date").textContent = validDate(backup.date) ? `Ngày snapshot · ${dateLabel(backup.date, true)}` : "Chưa có ngày snapshot hợp lệ";
    const history = data.history || {};
    const gap = history.gap_suspected;
    const unknown = history.history_unknown;
    $("history-notice").hidden = gap !== true && unknown !== true;
    $("history-description").textContent = gap === true ? "Có dấu hiệu khoảng trống trong lịch sử." : unknown === true ? "Lịch sử chưa được xác minh đầy đủ." : "";
    const steps = Object.entries(stepNames).map(([key, label]) => {
      const record = data.steps?.[key] || {};
      const status = outcome(record.status);
      const freshnessValue = freshness(record.freshness);
      const item = element("div", "step");
      item.append(element("span", "step-name", label));
      item.append(element("span", `step-result state-${status}`, outcomeNames[status]));
      item.append(element("span", "step-detail", `${freshnessNames[freshnessValue]} · ${count(record.consecutive_failures)} lỗi liên tiếp`));
      item.append(element("span", "step-detail", `Thành công: ${timestamp(record.last_success_at)}`));
      if (record.last_error_code !== null && record.last_error_code !== undefined) item.append(element("span", "step-detail", `Mã lỗi: ${Object.hasOwn(errorNames, record.last_error_code) ? record.last_error_code : "Chưa rõ"}`));
      return item;
    });
    $("step-statuses").replaceChildren(...steps);
    $("status-message").textContent = "";
  }

  async function loadStatus() {
    statusRequest?.abort();
    statusRequest = new AbortController();
    const version = ++statusVersion;
    $("status-message").textContent = "Đang đọc trạng thái…";
    try {
      const data = await readApi("/api/status", statusRequest);
      if (version !== statusVersion) return;
      if (!data || !data.database || !data.sync || !data.outbox || !data.history || !data.backup) throw new Error("Dữ liệu trạng thái không hợp lệ.");
      renderStatus(data);
      lastBankSync = data.sync.last_success_at;
      statusFailed = false;
      $("operations").classList.remove("is-stale");
      updateReadAge();
      return true;
    } catch (error) {
      if (error.name === "AbortError" || version !== statusVersion) return;
      $("operations").classList.add("is-stale");
      $("sync-badge").className = "sync-badge state-stale";
      $("sync-badge").textContent = "Đồng bộ: Chưa cập nhật";
      $("status-message").textContent = `Chưa cập nhật trạng thái. ${readableError(error)}`;
      statusFailed = true;
      updateReadAge();
    }
  }

  let searchRequest = null;
  let searchVersion = 0;
  let searchFilters = null;
  let searchSnapshot = null;
  let searchRecords = [];
  const filterKeys = ["q", "start", "end", "min", "max", "label", "category"];

  function submittedFilters() {
    const filters = Object.fromEntries([...filterKeys, "sort"].map((key) => [key, $("search-" + key).value.trim()]));
    for (const key of [...filterKeys, "sort"]) $("search-" + key).setCustomValidity("");
    if (filters.q.length > 200 || hasControlChars(filters.q)) $("search-q").setCustomValidity("Ghi chú tối đa 200 ký tự, không có ký tự điều khiển.");
    for (const key of ["start", "end"]) if (filters[key] && !validDate(filters[key])) $("search-" + key).setCustomValidity("Chọn ngày hợp lệ trong phạm vi 0001–9998.");
    if (filters.start && filters.end && filters.start > filters.end) $("search-end").setCustomValidity("Đến ngày phải bằng hoặc sau Từ ngày.");
    for (const key of ["min", "max"]) {
      if (!filters[key]) continue;
      if (!/^[0-9]{1,19}$/.test(filters[key]) || BigInt(filters[key]) > 9223372036854775807n) $("search-" + key).setCustomValidity("Nhập VND nguyên từ 0 đến 9223372036854775807, không dấu phân cách.");
      else filters[key] = BigInt(filters[key]).toString();
    }
    if (filters.min && filters.max && /^[0-9]+$/.test(filters.min) && /^[0-9]+$/.test(filters.max) && BigInt(filters.min) > BigInt(filters.max)) $("search-max").setCustomValidity("Tối đa phải bằng hoặc lớn hơn Tối thiểu.");
    if (!["latest", "amount"].includes(filters.sort)) $("search-sort").setCustomValidity("Chọn thứ tự hợp lệ.");
    return $("search-form").reportValidity() ? filters : null;
  }

  function renderSearch() {
    const data = searchSnapshot;
    const parts = [searchFilters.q ? `Ghi chú: ${searchFilters.q}` : "Mọi ghi chú", searchFilters.start ? `Từ ${dateLabel(searchFilters.start, true)}` : "Không giới hạn ngày bắt đầu", searchFilters.end ? `Đến ${dateLabel(searchFilters.end, true)}` : "Không giới hạn ngày kết thúc"];
    if (searchFilters.min) parts.push(`Tối thiểu ${money(searchFilters.min)}`);
    if (searchFilters.max) parts.push(`Tối đa ${money(searchFilters.max)}`);
    if (searchFilters.label) parts.push(`Nhãn: ${searchFilters.label}`);
    if (searchFilters.category) parts.push(`Phân loại: ${searchFilters.category}`);
    parts.push(searchFilters.sort === "latest" ? "Mới nhất" : "Lớn nhất");
    const state = data.ledger_status === "absent" ? "Chưa có sổ ghi; không coi là chi 0." : data.total === 0 ? "Không có khoản khớp. Nới bộ lọc hoặc Xóa lọc để thử lại." : `Đang hiển thị ${count(searchRecords.length)} / ${count(data.total)} khoản khớp.`;
    $("search-summary").textContent = parts.join(" · ");
    $("search-coverage").textContent = state;
    $("search-count").textContent = data.ledger_status === "absent" ? "Chưa có sổ" : count(data.total);
    $("search-total").textContent = money(data.spent, "Chưa có sổ");
    const list = $("search-results");
    const focused = list.contains(document.activeElement) ? document.activeElement : null;
    const scroll = focused?.scrollLeft;
    const existing = new Map(Array.from(list.children, (row) => [row.dataset.key, row]));
    const rows = searchRecords.map((record) => {
      const key = `${record.date}/${record.id}`;
      const row = existing.get(key) || element("li", "transaction");
      row.dataset.key = key;
      row.dataset.id = record.id;
      row.dataset.date = record.date;
      const value = money(record.amount);
      const moneyNode = row.querySelector(".transaction-amount") || element("span", "transaction-amount");
      if (moneyNode.textContent !== value) moneyNode.textContent = value;
      if (!moneyNode.parentNode) row.append(moneyNode);
      row.querySelector(".transaction-description")?.remove();
      const description = element("div", "transaction-description");
      description.append(element("span", "transaction-time", `${dateLabel(record.date, true)} · ${record.time || "Chưa có giờ"}`));
      if (record.note) description.append(element("span", "transaction-note", record.note));
      if (record.label) description.append(element("span", "transaction-label", record.label));
      else if (typeof record.category === "string") description.append(element("span", record.category_source === "keyword" ? "transaction-category" : "transaction-category is-none", record.category));
      row.insertBefore(description, moneyNode);
      return row;
    });
    syncChildren(list, rows);
    if (focused && document.activeElement === document.body) (focused.isConnected ? focused : $("search-results-title")).focus({ preventScroll: true });
    if (focused && document.activeElement === focused) focused.scrollLeft = scroll;
    $("search-more").hidden = data.next_offset === null;
  }

  async function loadSearch(filters, offset = 0, preserve = false, silent = false) {
    const keep = preserve ? Math.max(50, searchRecords.length) : 50;
    const focusMore = document.activeElement === $("search-more");
    if (offset > 0 && (!searchSnapshot || offset !== searchRecords.length)) return;
    searchRequest?.abort();
    searchRequest = new AbortController();
    const version = ++searchVersion;
    $("search-results-panel").setAttribute("aria-busy", "true");
    $("search-more").disabled = true;
    if (!silent) $("search-message").textContent = offset > 0 ? "Đang tải thêm kết quả…" : "Đang tìm trong sổ đã ghi nhận…";
    try {
      let data = offset > 0 ? searchSnapshot : null;
      let pageOffset = offset;
      const records = [];
      const ids = new Set(offset > 0 ? searchRecords.map((record) => `${record.date}/${record.id}`) : []);
      do {
        const page = await readApi(`/api/transactions?${new URLSearchParams({ ...filters, offset: String(pageOffset) })}`, searchRequest);
        if (version !== searchVersion) return;
        if (!page || !["available", "absent"].includes(page.ledger_status) || page.sort !== filters.sort || !page.filters || filterKeys.some((key) => page.filters[key] !== filters[key]) || !/^[0-9a-f]{64}$/.test(page.revision) || !Number.isSafeInteger(page.total) || page.total < 0 || !Array.isArray(page.transactions) || page.transactions.length > 50 || (page.ledger_status === "available" ? amount(page.spent) === null || amount(page.spent) < 0n : page.spent !== null || page.total !== 0) || page.transactions.some((record) => !record || typeof record.id !== "string" || !record.id || !validDate(record.date) || amount(record.amount) === null || amount(record.amount) < 0n || !validTransactionMetadata(record))) throw new Error("Kết quả lọc không hợp lệ; giữ lần đọc trước.");
        const end = pageOffset + page.transactions.length;
        if (end > page.total || page.next_offset !== (end < page.total ? end : null) || (page.next_offset !== null && end <= pageOffset)) throw new Error("Phân trang kết quả không hợp lệ; giữ lần đọc trước.");
        if (data && ["revision", "ledger_status", "total", "spent"].some((key) => page[key] !== data[key])) throw new Error("Sổ đã đổi trong lúc tải. Bấm Tìm khoản chi để đọc lại; giữ kết quả trước.");
        for (const record of page.transactions) {
          const key = `${record.date}/${record.id}`;
          if (ids.has(key)) throw new Error("Kết quả trùng giữa các trang; giữ lần đọc trước.");
          ids.add(key);
        }
        records.push(...page.transactions);
        data = page;
        pageOffset = page.next_offset;
      } while (offset === 0 && pageOffset !== null && records.length < keep);
      const nextRecords = offset === 0 ? records : [...searchRecords, ...records];
      const visibleTotal = nextRecords.reduce((sum, record) => sum + amount(record.amount), 0n);
      if (data.ledger_status === "available" && (visibleTotal > amount(data.spent) || nextRecords.length === data.total && visibleTotal !== amount(data.spent))) throw new Error("Tổng kết quả không khớp; giữ lần đọc trước.");
      searchFilters = { ...filters };
      searchRecords = nextRecords;
      searchSnapshot = data;
      $("search-results-panel").hidden = false;
      renderSearch();
      $("transaction-search").classList.remove("is-stale");
      $("search-error").hidden = true;
      if (!silent) $("search-message").textContent = "Đã đọc kết quả lọc.";
      if (focusMore && document.activeElement === document.body && $("search-more").hidden) {
        const target = offset > 0 ? $("search-results").children[offset] : $("search-results-title");
        if (target) { target.tabIndex = -1; target.focus({ preventScroll: offset === 0 }); }
      }
      return true;
    } catch (error) {
      if (error.name === "AbortError" || version !== searchVersion) return;
      $("transaction-search").classList.add("is-stale");
      $("search-error").textContent = `${readableError(error)}${searchSnapshot ? " Kết quả và bộ lọc bên dưới là lần đọc trước." : " Chưa có kết quả; không thay bằng số 0."}`;
      $("search-error").hidden = false;
      if (!silent) $("search-message").textContent = "Chưa cập nhật kết quả lọc.";
    } finally {
      if (version === searchVersion) {
        $("search-results-panel").setAttribute("aria-busy", "false");
        $("search-more").disabled = false;
        if (focusMore && document.activeElement === document.body && !$("search-more").hidden) $("search-more").focus({ preventScroll: true });
      }
    }
  }

  $("search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const filters = submittedFilters();
    if (filters) loadSearch(filters);
  });
  $("search-form").addEventListener("input", (event) => {
    for (const key of [...filterKeys, "sort"]) $("search-" + key).setCustomValidity("");
  });
  $("search-clear").addEventListener("click", () => {
    $("search-form").reset();
    for (const key of [...filterKeys, "sort"]) $("search-" + key).setCustomValidity("");
    loadSearch(submittedFilters());
  });
  $("search-more").addEventListener("click", () => {
    if (searchSnapshot?.next_offset !== null && searchFilters) loadSearch(searchFilters, searchSnapshot.next_offset);
  });

  function changeMonth(delta) {
    const [year, month] = requestedMonth.split("-").map(Number);
    const index = year * 12 + month - 1 + delta;
    const next = `${String(Math.floor(index / 12)).padStart(4, "0")}-${String(index % 12 + 1).padStart(2, "0")}`;
    if (validMonth(next)) loadCalendar(next);
  }

  async function refresh(manual = false) {
    const version = manual ? ++refreshVersion : refreshVersion;
    if (manual) {
      $("refresh-feedback").textContent = "Đang đọc lại sổ…";
      $("refresh").setAttribute("aria-busy", "true");
    }
    try {
      const searchRead = searchSnapshot && $("transaction-search").open ? loadSearch(searchFilters, 0, true, !manual) : Promise.resolve(true);
      const reads = Promise.all([loadCalendar(requestedMonth, null, manual), loadStatus(), loadToday(), searchRead]);
      const monthVersion = calendarVersion;
      if (!manual) return reads;
      const [snapshot, statusRead, todayRead, searchDone] = await reads;
      const details = await snapshot?.reads;
      if (version !== refreshVersion) return;
      if (monthVersion !== calendarVersion) {
        $("refresh-feedback").textContent = "";
      } else if (!snapshot || !statusRead || !todayRead || !searchDone || !details?.every(Boolean)) {
        $("refresh-feedback").textContent = "Chưa đọc lại đủ dữ liệu. Bấm Cập nhật để thử lại.";
      } else if (snapshot.at === null || ageSeconds(new Date(snapshot.at).toISOString()) === null) {
        $("refresh-feedback").textContent = "Chưa rõ thời điểm đọc; chưa xác nhận lần cập nhật này.";
      } else {
        $("refresh-feedback").textContent = `Đã đọc lại sổ lúc ${timeFormat.format(new Date(snapshot.at))}. Chỉ đọc dữ liệu, không chạy đồng bộ ngân hàng.`;
      }
    } finally {
      if (manual && version === refreshVersion) $("refresh").setAttribute("aria-busy", "false");
    }
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    if (document.hidden) return;
    pollTimer = setTimeout(() => {
      updateReadAge();
      if (!$("calendar-days").matches('[aria-busy="true"]') && !$("day-detail").matches('[aria-busy="true"]')) refresh();
      schedulePoll();
    }, 60000);
  }

  $("previous-month").addEventListener("click", () => changeMonth(-1));
  $("next-month").addEventListener("click", () => changeMonth(1));
  $("month-input").addEventListener("change", (event) => {
    if (validMonth(event.target.value)) loadCalendar(event.target.value);
    else event.target.value = requestedMonth;
  });
  $("today").addEventListener("click", () => {
    const date = freshestToday();
    ++daySelectionVersion;
    openedDate = null;
    loadCalendar(date.slice(0, 7), date);
  });
  $("today-transactions").addEventListener("click", async () => {
    const focusTarget = document.activeElement;
    const date = freshestToday();
    ++daySelectionVersion;
    openedDate = date;
    const pending = loadCalendar(date.slice(0, 7), date);
    const version = calendarVersion;
    await pending;
    if (document.activeElement === focusTarget && version === calendarVersion && openedDate === date && selectedDate === date && calendar?.month === date.slice(0, 7) && !errors.calendar && !$("day-detail").hidden) {
      animateDaySelection();
      $("day-title").focus({ preventScroll: true });
      $("day-title").scrollIntoView({ block: "start" });
    }
  });
  $("refresh").addEventListener("click", () => refresh(true));
  $("calendar-days").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-date]");
    if (button) selectDay(button.dataset.date);
  });
  for (const sort of ["latest", "amount"]) $("sort-" + sort).addEventListener("click", () => {
    if (sort === daySort) return;
    setDaySort(sort);
    loadDay(selectedDate);
  });
  $("load-more").addEventListener("click", () => {
    if (day && day.date === selectedDate && day.sort === daySort && Number.isSafeInteger(day.next_offset) && day.next_offset >= 0) loadDay(selectedDate, day.next_offset);
  });
  document.addEventListener("visibilitychange", () => {
    updateReadAge();
    if (!document.hidden) refresh();
    schedulePoll();
  });
  window.addEventListener("pagehide", () => {
    clearTimeout(pollTimer);
    calendarRequest?.abort();
    dayRequest?.abort();
    statusRequest?.abort();
    todayRequest?.abort();
    comparisonRequest?.abort();
    categoriesRequest?.abort();
    searchRequest?.abort();
  });
  $("edit-unlock").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("edit-key");
    writeKey = input.value || null;
    input.value = "";
    $("edit-message").textContent = writeKey ? "Đã mở khóa chỉnh sửa." : "";
    updateEditState();
    if (writeKey) loadLabelOptions();
  });
  $("edit-lock").addEventListener("click", () => {
    writeKey = null;
    $("edit-message").textContent = "Đã khóa chỉnh sửa.";
    updateEditState();
  });
  $("label-editor").addEventListener("submit", (event) => { event.preventDefault(); saveLabel(false); });
  $("label-clear").addEventListener("click", () => saveLabel(true));
  $("label-cancel").addEventListener("click", () => closeLabelEditor());

  loadSession();
  refresh();
  schedulePoll();
}, { once: true });
