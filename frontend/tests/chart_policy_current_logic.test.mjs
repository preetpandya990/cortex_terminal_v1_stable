import test from "node:test";
import assert from "node:assert/strict";

const INTRADAY_RANGE_MAX_DAYS = 10;

function ymdToUtcDate(ymd) {
  const [year, month, day] = ymd.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day));
}

function diffDaysInclusive(fromDate, toDate) {
  const start = ymdToUtcDate(fromDate);
  const end = ymdToUtcDate(toDate);
  const diffMs = end.getTime() - start.getTime();
  return Math.floor(diffMs / 86400000) + 1;
}

function includesToday(fromDate, toDate, today = "2026-02-12") {
  return fromDate <= today && toDate >= today;
}

// Mirrors expected fixed frontend logic for source routing.
function shouldUseIntradaySource(rangeDays, unit, fromDate, toDate, today = "2026-02-12") {
  return (
    rangeDays <= INTRADAY_RANGE_MAX_DAYS &&
    (unit === "minutes" || unit === "hours") &&
    includesToday(fromDate, toDate, today)
  );
}

// Mirrors expected fixed chart-unit pass-through behavior.
function mapChartUnit(unit) {
  return unit;
}

function includesTodayOrYesterdayCurrent(fromDate, toDate, today = "2026-02-12") {
  const yesterdayDate = ymdToUtcDate(today);
  yesterdayDate.setUTCDate(yesterdayDate.getUTCDate() - 1);
  const yesterday = yesterdayDate.toISOString().slice(0, 10);
  return fromDate <= today && toDate >= yesterday;
}

test("policy: recent minute range including today should use intraday", () => {
  const fromDate = "2026-02-09";
  const toDate = "2026-02-12";
  const rangeDays = diffDaysInclusive(fromDate, toDate);
  assert.equal(shouldUseIntradaySource(rangeDays, "minutes", fromDate, toDate), true);
});

test("policy: past 5-day minute range should use historical (expected)", () => {
  const fromDate = "2025-01-10";
  const toDate = "2025-01-14";
  const rangeDays = diffDaysInclusive(fromDate, toDate);

  assert.equal(shouldUseIntradaySource(rangeDays, "minutes", fromDate, toDate), false);
});

test("policy: short daily range should use historical (expected)", () => {
  const fromDate = "2026-02-08";
  const toDate = "2026-02-12";
  const rangeDays = diffDaysInclusive(fromDate, toDate);

  assert.equal(shouldUseIntradaySource(rangeDays, "days", fromDate, toDate), false);
});

test("policy: live merge enablement should be true if range includes yesterday", () => {
  const actual = includesTodayOrYesterdayCurrent("2026-02-10", "2026-02-11", "2026-02-12");
  assert.equal(actual, true);
});

test("policy: live merge enablement should be false for old custom range", () => {
  const actual = includesTodayOrYesterdayCurrent("2025-01-10", "2025-01-14", "2026-02-12");
  assert.equal(actual, false);
});

test("policy: weekly/monthly chart unit should be preserved (expected)", () => {
  assert.equal(mapChartUnit("weeks"), "weeks");
  assert.equal(mapChartUnit("months"), "months");
});
