"""Tests for check-interval scheduling and the CHECK_POSTS_IN_HOURS_RANGE window logic."""

from datetime import datetime, timedelta
from types import SimpleNamespace

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

        # The check the interval would have made tonight at 23:30, moved a day, so it keeps its time of day
        assert next_dt == datetime(2024, 1, 2, 23, 30, 0)
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
        # The time of day is the one the interval alone would have given, an hour and no more after the check
        assert (next_dt.hour, next_dt.minute) == ((hour + 1) % 24, 15)

    # Verifies the check keeps the time the usual schedule gave it, moved by whole days and not placed on the dot of the window
    def test_the_usual_time_is_moved_by_whole_days(self, im_module, monkeypatch):
        _enable_hours(monkeypatch, im_module, 22, 23, 0, 0)
        monkeypatch.setattr(im_module, "DAYS_BETWEEN", 2)

        _sleep_s, next_dt = im_module.compute_next_check_with_hours_range(self.NOW, 3600 + 17 * 60 + 5, after_check=True)

        assert next_dt.date() == datetime(2024, 1, 3).date()
        # Tonight's check would have been at 23:47:05, so the one two days on keeps that time
        assert next_dt.time() == datetime(2024, 1, 1, 23, 47, 5).time()

    # Verifies a usual check that already falls on tomorrow is only moved the days that are still missing
    def test_a_check_already_on_tomorrow_is_moved_only_the_rest(self, im_module, monkeypatch):
        _enable_hours(monkeypatch, im_module, 22, 23, 0, 0)
        monkeypatch.setattr(im_module, "DAYS_BETWEEN", 2)
        late = datetime(2024, 1, 1, 23, 50, 0)

        _sleep_s, next_dt = im_module.compute_next_check_with_hours_range(late, 3600, after_check=True)

        # 00:50 is outside the window, so the usual schedule waits for tomorrow's 22:50, and two days is one more day
        assert next_dt == datetime(2024, 1, 3, 22, 50, 0)

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


class TestFirstCheckDays:
    # Monday 1 January 2024 at 20:10, when the monitor is started
    START = datetime(2024, 1, 1, 20, 10, 0)

    @pytest.fixture(autouse=True)
    # Spaces checks by two days inside a 22:00 to 23:59 window, which is the case that asks
    def two_days(self, im_module, monkeypatch):
        _enable_hours(monkeypatch, im_module, 22, 23, 0, 0)
        monkeypatch.setattr(im_module, "DAYS_BETWEEN", 2)
        monkeypatch.setattr(im_module, "DASHBOARD_ENABLED", False)
        monkeypatch.setattr(im_module, "TIME_FORMAT_12H", False)
        monkeypatch.setattr(im_module, "now_local_naive", lambda: self.START)
        monkeypatch.setattr(im_module, "update_check_times", lambda *args, **kwargs: None)
        monkeypatch.setattr(im_module, "log_activity", lambda *args, **kwargs: None)

    # Gives the prompt a terminal and the answers a test scripts, and records what was asked
    @staticmethod
    def answers(im_module, monkeypatch, *replies):
        asked = []
        scripted = iter(replies)

        # Returns the next scripted reply, or raises the one a test scripted to end the input
        def reader(_input, prompt):
            asked.append(prompt)
            reply = next(scripted)
            if isinstance(reply, BaseException):
                raise reply
            return reply

        monkeypatch.setattr(im_module, "read_interactively", reader)
        monkeypatch.setattr(im_module.sys, "stdin", SimpleNamespace(isatty=lambda: True))
        return asked

    # A bare Enter is today, which is the answer the prompt offers
    def test_enter_means_today(self, im_module, monkeypatch):
        asked = self.answers(im_module, monkeypatch, "")

        assert im_module.ask_first_check_days() == 0
        assert len(asked) == 1 and "0 = today, 1 = tomorrow" in asked[0] and "DAYS_BETWEEN is 2" in asked[0]

    # One is tomorrow, and the answer is kept for the planning that follows
    @pytest.mark.parametrize("reply, expected", [("1", 1), (" 2 ", 2), ("0", 0)])
    def test_the_answer_is_the_number_of_days(self, im_module, monkeypatch, reply, expected):
        self.answers(im_module, monkeypatch, reply)

        assert im_module.ask_first_check_days() == expected
        assert im_module.FIRST_CHECK_SKIP_DAYS == expected

    # Anything that is not a whole number of zero or more is asked again
    def test_a_bad_answer_is_asked_again(self, im_module, monkeypatch, capsys):
        asked = self.answers(im_module, monkeypatch, "tomorrow", "-1", "1.5", "\u00b2", "2")

        assert im_module.ask_first_check_days() == 2
        assert len(asked) == 5
        assert capsys.readouterr().out.count("Please enter a whole number of 0 or more.") == 4

    # Input that ends leaves the first check today instead of stopping startup
    def test_the_end_of_input_means_today(self, im_module, monkeypatch):
        self.answers(im_module, monkeypatch, EOFError())

        assert im_module.ask_first_check_days() == 0

    # Nothing is asked when the schedule has no skipped day, or no hour ranges to place the check in, or nobody to answer
    @pytest.mark.parametrize("setting, value", [("DAYS_BETWEEN", 0), ("DAYS_BETWEEN", 1), ("CHECK_POSTS_IN_HOURS_RANGE", False), ("DASHBOARD_ENABLED", True)])
    def test_nothing_is_asked_without_a_skipped_day(self, im_module, monkeypatch, setting, value):
        asked = self.answers(im_module, monkeypatch, "3")
        monkeypatch.setattr(im_module, setting, value)

        assert im_module.ask_first_check_days() == 0
        assert asked == []

    # A run with no terminal, such as a scheduled task, keeps today
    def test_no_terminal_means_today(self, im_module, monkeypatch):
        asked = self.answers(im_module, monkeypatch, "3")
        monkeypatch.setattr(im_module.sys, "stdin", SimpleNamespace(isatty=lambda: False))

        assert im_module.ask_first_check_days() == 0
        assert asked == []

    # Today leaves a plan alone, and a later day moves it by whole days so it keeps its time of day
    def test_the_plan_moves_to_the_chosen_day(self, im_module, monkeypatch):
        planned = datetime(2024, 1, 1, 22, 10, 30)
        monkeypatch.setattr(im_module, "FIRST_CHECK_SKIP_DAYS", 0)
        assert im_module.apply_first_check_days(self.START, planned) == planned

        monkeypatch.setattr(im_module, "FIRST_CHECK_SKIP_DAYS", 1)
        assert im_module.apply_first_check_days(self.START, planned) == datetime(2024, 1, 2, 22, 10, 30)

        monkeypatch.setattr(im_module, "FIRST_CHECK_SKIP_DAYS", 3)
        assert im_module.apply_first_check_days(self.START, planned) == datetime(2024, 1, 4, 22, 10, 30)

    # The answer is remembered for the targets that were started, so each is delayed once
    def test_the_answer_marks_the_targets_pending(self, im_module, monkeypatch):
        self.answers(im_module, monkeypatch, "1")

        im_module.ask_first_check_days(["a", "b"])

        assert im_module.FIRST_CHECK_PENDING == {"a", "b"}

    # Today leaves nothing pending, so no target waits
    def test_today_marks_nothing_pending(self, im_module, monkeypatch):
        self.answers(im_module, monkeypatch, "0")

        im_module.ask_first_check_days(["a"])

        assert im_module.FIRST_CHECK_PENDING == set()

    # A pending target is given the time its first check would have had today, a day on, and only once
    def test_a_pending_target_is_given_its_time_once(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "FIRST_CHECK_SKIP_DAYS", 1)
        monkeypatch.setattr(im_module, "FIRST_CHECK_PENDING", {"target"})

        # Started at 20:10, tonight's check would be 22:10, so tomorrow's is 22:10 and not the 22:00 the window opens
        assert im_module.take_first_check_time("target", self.START, [22, 23]) == datetime(2024, 1, 2, 22, 10, 0)
        assert im_module.take_first_check_time("target", self.START, [22, 23]) is None

    # Started inside the window, the check that would have been made now is made a day later at the same time
    def test_inside_the_window_it_is_the_same_time_tomorrow(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "FIRST_CHECK_SKIP_DAYS", 1)
        monkeypatch.setattr(im_module, "FIRST_CHECK_PENDING", {"target"})

        assert im_module.take_first_check_time("target", datetime(2024, 1, 1, 22, 40, 12), [22, 23]) == datetime(2024, 1, 2, 22, 40, 12)

    # A target that was never pending, or whose answer was today, is left alone
    def test_other_targets_are_left_alone(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "FIRST_CHECK_SKIP_DAYS", 1)
        monkeypatch.setattr(im_module, "FIRST_CHECK_PENDING", {"target"})
        assert im_module.take_first_check_time("someone.else", self.START, [22, 23]) is None

        monkeypatch.setattr(im_module, "FIRST_CHECK_SKIP_DAYS", 0)
        monkeypatch.setattr(im_module, "FIRST_CHECK_PENDING", {"target"})
        assert im_module.take_first_check_time("target", self.START, [22, 23]) is None


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
