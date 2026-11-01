"""Tests for check-interval scheduling and the CHECK_POSTS_IN_HOURS_RANGE window logic."""

from datetime import datetime

import pytest


def _enable_hours(monkeypatch, im_module, min1, max1, min2, max2):
    monkeypatch.setattr(im_module, "CHECK_POSTS_IN_HOURS_RANGE", True)
    monkeypatch.setattr(im_module, "MIN_H1", min1)
    monkeypatch.setattr(im_module, "MAX_H1", max1)
    monkeypatch.setattr(im_module, "MIN_H2", min2)
    monkeypatch.setattr(im_module, "MAX_H2", max2)


class TestHoursToCheck:
    def test_full_day_when_range_disabled(self, im_module):
        # baseline fixture leaves CHECK_POSTS_IN_HOURS_RANGE False
        assert im_module.hours_to_check() == list(range(24))

    def test_two_ranges_union_and_sorted(self, im_module, monkeypatch):
        _enable_hours(monkeypatch, im_module, 0, 4, 11, 23)
        assert im_module.hours_to_check() == [0, 1, 2, 3, 4] + list(range(11, 24))

    def test_overlapping_ranges_deduped(self, im_module, monkeypatch):
        _enable_hours(monkeypatch, im_module, 8, 12, 10, 14)
        assert im_module.hours_to_check() == [8, 9, 10, 11, 12, 13, 14]

    def test_both_ranges_zero_means_empty(self, im_module, monkeypatch):
        _enable_hours(monkeypatch, im_module, 0, 0, 0, 0)
        assert im_module.hours_to_check() == []

    def test_inverted_range_yields_no_hours_from_that_range(self, im_module, monkeypatch):
        # MAX < MIN produces an empty range for that band
        _enable_hours(monkeypatch, im_module, 20, 5, 0, 0)
        assert im_module.hours_to_check() == []


class TestNextAllowedDatetime:
    def test_returns_none_for_empty_allowed(self, im_module):
        assert im_module._next_allowed_datetime_at_or_after(datetime(2024, 1, 1, 8, 0), []) is None

    def test_already_in_window_returns_same(self, im_module):
        planned = datetime(2024, 1, 1, 2, 30)
        assert im_module._next_allowed_datetime_at_or_after(planned, [0, 1, 2, 3]) == planned

    def test_pushes_to_next_window_preserving_offset(self, im_module):
        # 08:30 is disallowed; next allowed hour is 11, keep the 30-min offset
        planned = datetime(2024, 1, 1, 8, 30)
        result = im_module._next_allowed_datetime_at_or_after(planned, list(range(11, 24)))
        assert result == datetime(2024, 1, 1, 11, 30)

    def test_wraps_to_next_day(self, im_module):
        # 22:15 disallowed, only hour 3 allowed -> next day 03:15
        planned = datetime(2024, 1, 1, 22, 15)
        result = im_module._next_allowed_datetime_at_or_after(planned, [3])
        assert result == datetime(2024, 1, 2, 3, 15)


class TestComputeNextCheckWithHoursRange:
    def test_range_disabled_is_plain_sleep(self, im_module):
        now = datetime(2024, 1, 1, 8, 0, 0)
        sleep_s, next_dt = im_module.compute_next_check_with_hours_range(now, 1800)
        assert sleep_s == 1800
        assert next_dt == datetime(2024, 1, 1, 8, 30, 0)

    def test_negative_base_sleep_clamped(self, im_module):
        now = datetime(2024, 1, 1, 8, 0, 0)
        sleep_s, _ = im_module.compute_next_check_with_hours_range(now, -100)
        assert sleep_s == 1

    def test_planned_time_pushed_into_window(self, im_module, monkeypatch):
        _enable_hours(monkeypatch, im_module, 0, 4, 11, 23)
        now = datetime(2024, 1, 1, 8, 0, 0)
        # planned 08:30 -> push to 11:30 -> 3.5h = 12600s
        sleep_s, next_dt = im_module.compute_next_check_with_hours_range(now, 1800)
        assert sleep_s == 12600
        assert next_dt == datetime(2024, 1, 1, 11, 30, 0)

    def test_misconfigured_window_returns_backoff_label(self, im_module, monkeypatch):
        _enable_hours(monkeypatch, im_module, 0, 0, 0, 0)
        now = datetime(2024, 1, 1, 8, 0, 0)
        sleep_s, label = im_module.compute_next_check_with_hours_range(now, 60)
        assert sleep_s == 300
        assert isinstance(label, str) and "No allowed hours" in label


class TestDaysBetween:
    # A check at 22:30 on Monday 1 January 2024 with a one hour interval, as the base the tests below move from
    NOW = datetime(2024, 1, 1, 22, 30, 0)

    @pytest.fixture(autouse=True)
    # Allows every hour, since the setting only works with the hour ranges on, and a test narrows them when it needs to
    def all_hours(self, im_module, monkeypatch):
        _enable_hours(monkeypatch, im_module, 0, 23, 0, 0)

    # Verifies the setting ships disabled, so a run that never sets it keeps the interval alone
    def test_the_template_ships_it_disabled(self, im_module):
        parsed = im_module.parse_config_content(im_module.CONFIG_BLOCK, "<built-in>")

        assert parsed["DAYS_BETWEEN"] == 0

    # Verifies 0 changes nothing, with or without a completed check
    @pytest.mark.parametrize("after_check", [False, True])
    def test_zero_is_disabled(self, im_module, monkeypatch, after_check):
        monkeypatch.setattr(im_module, "DAYS_BETWEEN", 0)

        sleep_s, next_dt = im_module.compute_next_check_with_hours_range(self.NOW, 3600, after_check=after_check)

        assert sleep_s == 3600 and next_dt == datetime(2024, 1, 1, 23, 30, 0)

    # Verifies a retry or a skipped cycle keeps the plain interval, since no check ran to start the wait
    def test_only_a_completed_check_starts_the_wait(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "DAYS_BETWEEN", 3)

        sleep_s, next_dt = im_module.compute_next_check_with_hours_range(self.NOW, 3600)

        assert sleep_s == 3600 and next_dt == datetime(2024, 1, 1, 23, 30, 0)

    # Verifies daily means the next day, not one hour later, and never earlier than the interval
    def test_one_is_daily(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "DAYS_BETWEEN", 1)

        sleep_s, next_dt = im_module.compute_next_check_with_hours_range(self.NOW, 3600, after_check=True)

        # Midnight of Tuesday plus the interval's own minutes and seconds, which are none for exactly one hour
        assert next_dt == datetime(2024, 1, 2, 0, 0, 0)
        assert sleep_s == int((next_dt - self.NOW).total_seconds())

    # Verifies a longer interval than the gap wins, so the setting only ever delays a check
    def test_a_longer_interval_wins(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "DAYS_BETWEEN", 1)

        _sleep_s, next_dt = im_module.compute_next_check_with_hours_range(self.NOW, 23 * 3600 + 45 * 60, after_check=True)

        assert next_dt == datetime(2024, 1, 2, 22, 15, 0)

    # Verifies every other day and longer gaps land on the right calendar day, wherever in the day the check ran
    @pytest.mark.parametrize("days, expected_day", [(2, 3), (3, 4), (7, 8)])
    @pytest.mark.parametrize("hour", [0, 9, 23])
    def test_the_gap_counts_calendar_days(self, im_module, monkeypatch, days, expected_day, hour):
        monkeypatch.setattr(im_module, "DAYS_BETWEEN", days)
        now = datetime(2024, 1, 1, hour, 15, 0)

        _sleep_s, next_dt = im_module.compute_next_check_with_hours_range(now, 3600, after_check=True)

        assert (next_dt.year, next_dt.month, next_dt.day) == (2024, 1, expected_day)
        assert next_dt.hour == 0

    # Verifies the hour ranges still decide the time of day, so the check comes in the first allowed hour of that day
    def test_the_hour_ranges_choose_the_time_of_day(self, im_module, monkeypatch):
        _enable_hours(monkeypatch, im_module, 22, 23, 0, 0)
        monkeypatch.setattr(im_module, "DAYS_BETWEEN", 2)

        _sleep_s, next_dt = im_module.compute_next_check_with_hours_range(self.NOW, 3600 + 17 * 60 + 5, after_check=True)

        assert next_dt.date() == datetime(2024, 1, 3).date()
        assert next_dt.hour == 22
        # The minutes and seconds follow the randomized interval, so checks do not begin on the dot of the hour
        assert (next_dt.minute, next_dt.second) == (17, 5)

    # Verifies the setting is ignored while the hour ranges are off, since it only works with them
    def test_it_is_ignored_without_hour_ranges(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "CHECK_POSTS_IN_HOURS_RANGE", False)
        monkeypatch.setattr(im_module, "DAYS_BETWEEN", 2)

        sleep_s, next_dt = im_module.compute_next_check_with_hours_range(self.NOW, 3600, after_check=True)

        assert sleep_s == 3600 and next_dt == datetime(2024, 1, 1, 23, 30, 0)

    # Verifies the line printed while waiting names the day once the wait ends on another day
    def test_the_resume_line_names_the_day(self, im_module, monkeypatch, capsys):
        monkeypatch.setattr(im_module, "HOURS_VERBOSE", True)
        monkeypatch.setattr(im_module, "DEBUG_MODE", False)
        monkeypatch.setattr(im_module, "DASHBOARD_ENABLED", False)
        monkeypatch.setattr(im_module, "TIME_FORMAT_12H", False)
        monkeypatch.setattr(im_module, "now_local_naive", lambda: self.NOW)

        im_module.sleep_message(3600, "target")
        same_day = capsys.readouterr().out
        im_module.sleep_message(2 * 86400, "target")
        later_day = capsys.readouterr().out

        assert same_day.splitlines()[0].endswith("resumes at ~23:30:00")
        assert later_day.splitlines()[0].endswith("resumes at ~22:30:00 on Wed 03 Jan")

    # Verifies a misconfigured hour range still reports its label rather than a date
    def test_no_allowed_hours_still_reports_the_label(self, im_module, monkeypatch):
        _enable_hours(monkeypatch, im_module, 0, 0, 0, 0)
        monkeypatch.setattr(im_module, "DAYS_BETWEEN", 2)

        sleep_s, label = im_module.compute_next_check_with_hours_range(self.NOW, 3600, after_check=True)

        assert sleep_s == 300 and "No allowed hours" in label

    # Verifies the setting is a whole number of zero or more, reported at startup like the other counts
    @pytest.mark.parametrize("value, rejected", [(0, False), (1, False), (30, False), (-1, True), (1.5, True), ("2", True), (True, True), (None, True)])
    def test_a_bad_value_is_rejected(self, im_module, monkeypatch, value, rejected):
        monkeypatch.setattr(im_module, "DAYS_BETWEEN", value)

        assert ("DAYS_BETWEEN" in im_module.runtime_configuration_problems()) is rejected


class TestProbabilityForCycle:
    def test_full_day_fraction(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "DAILY_HUMAN_HITS", 5)
        # 5 hits/day, sleep = 1/5 of a day -> probability 1.0
        assert im_module.probability_for_cycle(86400 // 5) == pytest.approx(1.0)

    def test_capped_at_one(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "DAILY_HUMAN_HITS", 5)
        assert im_module.probability_for_cycle(86400) == 1.0

    def test_scales_with_window_size(self, im_module, monkeypatch):
        # Restricting allowed hours shrinks the denominator, raising probability
        monkeypatch.setattr(im_module, "DAILY_HUMAN_HITS", 1)
        full = im_module.probability_for_cycle(3600)
        _enable_hours(monkeypatch, im_module, 0, 5, 0, 0)  # 6 allowed hours
        windowed = im_module.probability_for_cycle(3600)
        assert windowed > full

    def test_zero_allowed_hours_returns_zero(self, im_module, monkeypatch):
        _enable_hours(monkeypatch, im_module, 0, 0, 0, 0)
        assert im_module.probability_for_cycle(3600) == 0.0


class TestRandomizeNumber:
    # Standard intervals use number minus diff_low as the lower random bound
    def test_uses_expected_bounds(self, im_module, monkeypatch):
        calls = []
        monkeypatch.setattr(im_module.random, "randint", lambda low, high: calls.append((low, high)) or high)

        assert im_module.randomize_number(5400, 900, 180) == 5580
        assert calls == [(4500, 5580)]

    # Small intervals use number itself as the lower random bound
    def test_small_number_does_not_go_negative(self, im_module, monkeypatch):
        calls = []
        monkeypatch.setattr(im_module.random, "randint", lambda low, high: calls.append((low, high)) or low)

        assert im_module.randomize_number(100, 900, 50) == 100
        assert calls == [(100, 150)]
