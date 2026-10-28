"use strict";

(() => {
const LABEL_CONTROL = /[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\ud800-\udfff]/u;
function normalizeLabelText(value, limit) {
  const text = String(value ?? "").normalize("NFC");
  if (LABEL_CONTROL.test(text)) throw new Error("Nhãn không được chứa ký tự điều khiển.");
  const cleaned = text.split(/\s+/).filter(Boolean).join(" ");
  if (cleaned === "") return null;
  if ([...cleaned].length > limit) throw new Error(`Nhãn tối đa ${limit} ký tự.`);
  return cleaned;
}

function hasControlChars(text) {
  return LABEL_CONTROL.test(text);
}

function amount(value) {
  if (typeof value !== "string" || value.length > 80 || !/^-?\d+$/.test(value)) return null;
  return BigInt(value);
}

const MONEY_FORMAT = new Intl.NumberFormat("vi-VN", { style: "currency", currency: "VND", maximumFractionDigits: 0 });
const SIGNS = new Set(["+", String.fromCharCode(0x2212)]);

// Prefix a non-negative amount with "+" or U+2212. Negative or invalid input gives null.
function signedMoney(value, sign) {
  const parsed = amount(value);
  return parsed === null || parsed < 0n || !SIGNS.has(sign) ? null : sign + MONEY_FORMAT.format(parsed);
}

function monthlyPlan(days, spent, elapsed) {
  const budgets = days.map((record) => amount(record.budget));
  const unknownDays = budgets.filter((value) => value === null || value < 0n).length;
  const budget = unknownDays ? null : budgets.reduce((total, value) => total + value, 0n);
  const used = amount(spent);
  const remaining = budget !== null && used !== null && used >= 0n ? budget - used : null;
  const daysLeft = Number.isSafeInteger(elapsed) && elapsed >= 0 && elapsed <= days.length ? days.length - elapsed : null;
  const daily = remaining !== null && daysLeft > 0 ? remaining > 0n ? remaining / BigInt(daysLeft) : 0n : null;
  return { budget, remaining, daily, daysLeft, unknownDays };
}

function percentOfDay(value, total) {
  const expense = amount(value);
  const spent = amount(total);
  return expense !== null && expense >= 0n && spent !== null && spent > 0n ? (expense * 1000n + spent / 2n) / spent : null;
}

function validTransactionMetadata(record) {
  return (record.time == null || typeof record.time === "string" && (record.time.length === 5 || record.time.length === 8) && /^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$/.test(record.time)) &&
    (record.note == null || typeof record.note === "string" && [...record.note].length <= 500 && !hasControlChars(record.note));
}

function validDayOrder(data, sort) {
  return data?.sort === sort && ["latest", "amount"].includes(sort) &&
    (data.ledger_status === "absent" ? data.timed_transactions === null :
      data.ledger_status === "available" && Number.isSafeInteger(data.timed_transactions) && data.timed_transactions >= 0 && data.timed_transactions <= data.total);
}

// A missing budget_basis means forecast, so older payloads keep working. Any other unknown value is rejected.
function validBudgetBasis(value) {
  return value === undefined || value === "forecast" || value === "actual";
}

function parseTimestamp(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T/.test(value) || !/(Z|[+-]\d{2}:\d{2})$/i.test(value)) return null;
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed) || new Date(value.slice(0, 10)).toISOString().slice(0, 10) !== value.slice(0, 10)) return null;
  return parsed;
}

function ageSeconds(value, now = Date.now()) {
  const parsed = parseTimestamp(value);
  return parsed === null || parsed > now + 5000 ? null : Math.max(0, Math.floor((now - parsed) / 1000));
}

const helpers = { normalizeLabelText, amount, monthlyPlan, percentOfDay, validTransactionMetadata, validDayOrder, validBudgetBasis, parseTimestamp, ageSeconds, signedMoney, hasControlChars };
if (typeof module !== "undefined" && module.exports) module.exports = helpers;
else window.DashboardHelpers = helpers;
})();
