"""Offline tests that drive the real monitoring loop to check account safety reaches every failure path.

The unit tests around the classifier cannot see these: each helper was correct on its own while the loop
never called the one that records a failure, so the circuit breaker only ever armed inside the follow list
fetch. These drive instagram_monitor_user itself and assert on the ledger it leaves behind.
"""

import json
from datetime import datetime
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import instagram_monitor as im

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT = PROJECT_ROOT / "local" / "test_artifacts"

# Captured before the autouse ledger-isolation fixture can patch it, so the loop writes a real file under tmp_path
_REAL_EXPOSURE_STATE_PATH = im.exposure_state_path

# Captured before the loop fixture silences it, for the one test that reads the report the loop prints
_REAL_PRINT_CUR_TS = im.print_cur_ts


# Returns an isolated local artifact directory for one loop test
def _artifact_dir() -> Path:
    artifact_dir = ARTIFACT_ROOT / "account_safety_loop" / uuid.uuid4().hex
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


@pytest.fixture
# Runs the real loop against a fake Instagram, with a real ledger under tmp_path and no waiting
def monitored_account(monkeypatch, tmp_path):
    monkeypatch.setattr(im, "exposure_state_path", _REAL_EXPOSURE_STATE_PATH, raising=False)
    im.ACCOUNT_BREAKER_MEMORY.clear()
    settings = {
        "OUTPUT_DIR": str(tmp_path), "LOCAL_TIMEZONE": "UTC", "SESSION_USERNAME": "loop.account",
        "SESSION_PASSWORD": "", "SKIP_SESSION": False, "CIRCUIT_BREAKER": True, "IDENTITY_BUDGET_PER_DAY": 0,
        "SKIP_FOLLOWERS": True, "SKIP_FOLLOWINGS": True, "SKIP_GETTING_STORY_DETAILS": True,
        "SKIP_GETTING_POSTS_DETAILS": True, "GET_MORE_POST_DETAILS": False, "DETECT_CHANGED_PROFILE_PIC": False,
        "CHECK_POSTS_IN_HOURS_RANGE": False, "WEB_DASHBOARD_ENABLED": False, "DASHBOARD_ENABLED": False,
        "RICH_AVAILABLE": False, "PROXY_ENABLED": False, "DISABLE_LOGGING": True, "COLORED_OUTPUT": False,
        "ERROR_NOTIFICATION": False, "WEBHOOK_ENABLED": False, "BE_HUMAN": False, "NEXT_OPERATION_DELAY": 0,
        "FOLLOWERS_CHURN_DETECTION": False, "ADVANCED_FOLLOWER_FETCH": False,
        "INSTA_CHECK_INTERVAL": 1, "RANDOM_SLEEP_DIFF_LOW": 0, "RANDOM_SLEEP_DIFF_HIGH": 0,
    }
    for name, value in settings.items():
        monkeypatch.setattr(im, name, value, raising=False)
    for name in ("log_activity", "update_ui_data", "update_check_times", "print_cur_ts", "ensure_instagram_session_wrapped", "refresh_proxy_if_needed"):
        monkeypatch.setattr(im, name, lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(im.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(im.instaloader.Profile, "own_profile", lambda ctx: SimpleNamespace(username="loop.account"))
    monkeypatch.setattr(im, "get_total_reels_count", lambda user, bot, skip_session=False, posts_count=None: 0)
    monkeypatch.setattr(im, "get_dashboard_config_data", lambda *args, **kwargs: {"ready": True})
    return monkeypatch


# Returns a profile shaped the way the loop reads it, standing in for an instaloader profile
def _profile():
    return SimpleNamespace(username="target", userid=123, followers=0, followees=0, biography="", is_private=False, followed_by_viewer=False, mediacount=0, has_public_story=False, profile_pic_url_no_iphone="")


# Drives the loop through one healthy check and then repeated failures, stopping after `cycles` waits
def _run_loop(monkeypatch, failure, cycles=4):
    lookups = []
    bot = SimpleNamespace(context=SimpleNamespace(_session=SimpleNamespace(), is_logged_in=True, iphone_headers={}), load_session_from_file=lambda *args: None)
    profile = _profile()

    def lookup(_bot, username):
        lookups.append(username)
        if username == im.FLAGGED_PROBE_USERNAME:
            raise failure
        if len(lookups) > 1:
            raise failure
        return profile

    waits = []

    def wait(*args, **kwargs):
        waits.append(1)
        return len(waits) >= cycles

    monkeypatch.setattr(im, "instaloader_client", lambda **kwargs: bot, raising=False)
    monkeypatch.setattr(im, "profile_from_username_resilient", lookup, raising=False)
    monkeypatch.setattr(im, "interruptible_sleep", wait, raising=False)
    im.instagram_monitor_user("target", str(_artifact_dir() / "events.csv"), False, True, True, True, True, False)
    return [name for name in lookups if name == "target"]


class TestTheLoopStopsAnAccountItKeepsFailingAgainst:
    # An expired session reported by an ordinary profile lookup never reached the classifier, so the loop
    # retried a dead session for as long as it was left running
    def test_an_expired_session_arms_the_breaker_and_halts_the_loop(self, monitored_account):
        target_lookups = _run_loop(monitored_account, im.instaloader.exceptions.LoginRequiredException("login_required"))

        breaker = im.circuit_breaker_state()
        assert breaker is not None
        assert breaker["failure_class"] == "auth_expired"
        assert im.exposure_snapshot()["failures"] == {"auth_expired": 1}
        # The healthy check plus the one that failed. The breaker stops the loop before a third
        assert len(target_lookups) == 2

    # A challenge on any endpoint is account level, so the breaker arms before the flagged session ends the run.
    # Without the Web Dashboard there is nothing that can re-import a session, so the tool exits rather than waiting
    def test_a_challenge_arms_the_breaker_before_the_flagged_session_ends_the_run(self, monitored_account):
        with pytest.raises(SystemExit):
            _run_loop(monitored_account, im.instaloader.exceptions.AbortDownloadException("400 checkpoint_required"))

        breaker = im.circuit_breaker_state()
        assert breaker is not None
        assert breaker["failure_class"] == "challenge"
        # One challenge on one request is one entry, whichever path recorded it
        assert im.exposure_snapshot()["failures"] == {"challenge": 1}

    # A transient fault must not stop the account, or a lost connection would end monitoring until cleared by hand
    def test_a_network_fault_keeps_the_loop_running(self, monitored_account):
        target_lookups = _run_loop(monitored_account, ConnectionError("timed out"))

        assert im.circuit_breaker_state() is None
        assert len(target_lookups) > 2
        assert threading.current_thread() is threading.main_thread()


class TestATruncatedListDoesNotReplaceASavedBaseline:
    """A browser dialog that stops rendering returns a short list with nothing to say it stopped early.

    The shortfall tolerance lets a small gap through as a finished list, so a saved baseline of 100 was
    replaced by the 90 that rendered and the ten missing accounts were reported as removals.
    """

    # Drives the real startup pass against a saved baseline and a follow list that comes back short
    @staticmethod
    def _run(monkeypatch, tmp_path, saved, fetched, reported_count):
        baseline = tmp_path / "target" / "json" / "instagram_target_followers.json"
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text(json.dumps([len(saved), saved]), encoding="utf-8")
        monkeypatch.setattr(im, "SKIP_FOLLOWERS", False, raising=False)
        monkeypatch.setattr(im, "SKIP_FOLLOW_CHANGES", False, raising=False)
        removals = []
        additions = []

        # Records what the comparison was asked to report, as removals and as additions
        def compare(user, kind, old, new, csv_file):
            removals.append((kind, sorted(set(old) - set(new))))
            additions.append((kind, sorted(set(new) - set(old))))
            return ("", "", "", "", "", "", "", "")

        monkeypatch.setattr(im, "compare_and_log_follower_changes", compare, raising=False)
        monkeypatch.setattr(im, "LAST_COMPARISON_ADDITIONS", additions, raising=False)

        def fetch(*args, **kwargs):
            result = im.PaginatedUsernameResult(fetched)
            result.complete = True
            return result

        monkeypatch.setattr(im, "fetch_usernames_paginated", fetch, raising=False)
        bot = SimpleNamespace(context=SimpleNamespace(_session=SimpleNamespace(), is_logged_in=True, iphone_headers={}), load_session_from_file=lambda *args: None)
        profile = _profile()
        profile.followers = reported_count
        monkeypatch.setattr(im, "instaloader_client", lambda **kwargs: bot, raising=False)
        monkeypatch.setattr(im, "profile_from_username_resilient", lambda _bot, username: profile, raising=False)
        monkeypatch.setattr(im, "interruptible_sleep", lambda *args, **kwargs: True, raising=False)
        im.instagram_monitor_user("target", str(tmp_path / "events.csv"), False, False, True, True, True, False, skip_follow_changes=False)
        return json.loads(baseline.read_text(encoding="utf-8")), removals

    # One new follower makes the tool refetch. The dialog stalls at 91 of the 101 Instagram reports, a
    # shortfall inside the tolerance, so the list reads as finished and would drop ten saved accounts
    def test_a_render_short_of_the_count_keeps_the_saved_list(self, monitored_account, tmp_path, capsys):
        saved = [f"user{index}" for index in range(100)]
        stored, removals = self._run(monitored_account, tmp_path, saved, saved[:91], 101)

        assert stored == [100, saved]
        assert removals == []
        assert "came back with 91 of the 101 Instagram reports, fewer than the 100 in the saved list" in capsys.readouterr().out

    # With FOLLOW_LIST_SHORT_ADDITIONS on, the same short list still reports the accounts it adds, and removes none
    def test_a_short_list_reports_only_additions_when_asked(self, monitored_account, tmp_path, capsys):
        monitored_account.setattr(im, "FOLLOW_LIST_SHORT_ADDITIONS", True)
        saved = [f"user{index}" for index in range(100)]
        fetched = saved[:91] + ["new_a", "new_b", "new_c"]
        stored, removals = self._run(monitored_account, tmp_path, saved, fetched, 103)

        assert stored == [103, saved + ["new_a", "new_b", "new_c"]], "the saved names the short list lacked stay in the baseline"
        assert removals == [("followers", [])]
        assert im.LAST_COMPARISON_ADDITIONS == [("followers", ["new_a", "new_b", "new_c"])]
        output = capsys.readouterr().out
        assert "so the saved list is kept" not in output
        assert "Using the short followers list only to add names: 3 new, none removed" in output

    # The next short scan finds those names already in the baseline, so nothing is reported twice
    def test_the_same_additions_are_not_reported_again(self, monitored_account, tmp_path):
        monitored_account.setattr(im, "FOLLOW_LIST_SHORT_ADDITIONS", True)
        saved = [f"user{index}" for index in range(100)] + ["new_a"]
        fetched = saved[:90] + ["new_a"]
        stored, removals = self._run(monitored_account, tmp_path, saved, fetched, 103)

        assert stored == [103, saved]
        assert removals == [] or removals == [("followers", [])]
        assert all(names == [] for _kind, names in im.LAST_COMPARISON_ADDITIONS)

    # A list that agrees with Instagram's count is not short, so it still replaces the saved one and reports removals
    def test_a_real_unfollow_still_reports_removals_when_asked(self, monitored_account, tmp_path):
        monitored_account.setattr(im, "FOLLOW_LIST_SHORT_ADDITIONS", True)
        saved = [f"user{index}" for index in range(100)]
        stored, removals = self._run(monitored_account, tmp_path, saved, saved[:80], 80)

        assert stored == [80, saved[:80]]
        assert removals == [("followers", sorted(saved[80:]))]

    # Accounts that really unfollowed move the count too, so the smaller list replaces the saved one
    def test_a_real_unfollow_replaces_the_saved_list(self, monitored_account, tmp_path):
        saved = [f"user{index}" for index in range(100)]
        stored, removals = self._run(monitored_account, tmp_path, saved, saved[:80], 80)

        assert stored == [80, saved[:80]]
        assert removals == [("followers", sorted(saved[80:]))]


class TestFirstCheckWait:
    """The days chosen at startup for the first check are the wait before the first request, after the set-up has run."""

    # Runs the real pass for one target up to its first profile request, returning what happened in order and how long it waited
    @staticmethod
    def _run(monkeypatch, tmp_path, start, pending, now=False):
        events = []
        waited = []

        class FirstRequest(BaseException):
            pass

        bot = SimpleNamespace(context=SimpleNamespace(_session=SimpleNamespace(), is_logged_in=True, iphone_headers={}), load_session_from_file=lambda *args: events.append("session loaded"))
        monkeypatch.setattr(im, "instaloader_client", lambda **kwargs: events.append("client created") or bot, raising=False)

        # Stands in for the first request to Instagram, which ends the run once it is reached
        def first_request(_bot, username):
            events.append("profile requested")
            raise FirstRequest()

        monkeypatch.setattr(im, "profile_from_username_resilient", first_request, raising=False)
        monkeypatch.setattr(im, "now_local_naive", lambda: start)
        monkeypatch.setattr(im, "CHECK_POSTS_IN_HOURS_RANGE", True)
        monkeypatch.setattr(im, "MIN_H1", 22)
        monkeypatch.setattr(im, "MAX_H1", 23)
        monkeypatch.setattr(im, "DAYS_BETWEEN", 2)
        monkeypatch.setattr(im, "FIRST_CHECK_SKIP_DAYS", 1 if pending else 0)
        monkeypatch.setattr(im, "FIRST_CHECK_PENDING", {"target"} if pending else set())
        monkeypatch.setattr(im, "FIRST_CHECK_NOW", {"target"} if now else set())
        stop_event = SimpleNamespace(is_set=lambda: False, wait=lambda seconds: waited.append(seconds))
        with pytest.raises(FirstRequest):
            im.instagram_monitor_user("target", "events.csv", False, True, True, True, True, False, stop_event=stop_event, skip_follow_changes=True)
        return events, sum(waited)

    # The client and the session are set up first, then the pass waits, and only then asks Instagram anything
    def test_the_set_up_runs_before_the_wait(self, monitored_account, tmp_path):
        events, waited = self._run(monitored_account, tmp_path, datetime(2024, 1, 1, 20, 10, 0), pending=True)

        assert events == ["client created", "session loaded", "profile requested"]
        # Tonight's check would be at 22:10, so tomorrow's is 22:10 too, which is 26 hours after 20:10 today
        assert waited == 26 * 3600

    # Without the days, the same start waits only for tonight's window, so the days are what added the wait
    def test_today_waits_only_for_tonights_window(self, monitored_account, tmp_path):
        events, waited = self._run(monitored_account, tmp_path, datetime(2024, 1, 1, 20, 10, 0), pending=False)

        assert events == ["client created", "session loaded", "profile requested"]
        # The existing wait keeps the minutes of the start, so 20:10 waits for 22:10
        assert waited == 2 * 3600

    # Started inside the window with today chosen, the first request is made at once
    def test_inside_the_window_with_today_does_not_wait(self, monitored_account, tmp_path):
        _events, waited = self._run(monitored_account, tmp_path, datetime(2024, 1, 1, 22, 30, 0), pending=False)

        assert waited == 0

    # Started inside the window with tomorrow chosen, it waits a day, to the same time tomorrow
    def test_inside_the_window_with_tomorrow_waits_a_day(self, monitored_account, tmp_path):
        _events, waited = self._run(monitored_account, tmp_path, datetime(2024, 1, 1, 22, 30, 0), pending=True)

        assert waited == 24 * 3600

    # A check asked for right now does not wait for the days or for the window, and the scheduled first check is kept for after it
    def test_a_check_now_does_not_wait_and_keeps_the_scheduled_check(self, monitored_account, tmp_path):
        events, waited = self._run(monitored_account, tmp_path, datetime(2024, 1, 1, 20, 10, 0), pending=True, now=True)

        assert events == ["client created", "session loaded", "profile requested"]
        assert waited == 0
        # Tonight's check would be at 22:10, so the scheduled first check is the same time tomorrow
        assert im.FIRST_CHECK_NEXT["target"] == datetime(2024, 1, 2, 22, 10, 0)

    # With today chosen, the check now is the only one that skips the wait, and tonight's check is the scheduled one
    def test_a_check_now_with_today_keeps_tonights_check(self, monitored_account, tmp_path):
        _events, waited = self._run(monitored_account, tmp_path, datetime(2024, 1, 1, 20, 10, 0), pending=False, now=True)

        assert waited == 0
        assert im.FIRST_CHECK_NEXT["target"] == datetime(2024, 1, 1, 22, 10, 0)

    # After the start-up check made right now, the next check is the scheduled first check and not DAYS_BETWEEN days on
    def test_the_next_check_after_a_check_now_is_the_scheduled_one(self, monitored_account, tmp_path):
        planned = []

        class StartedUp(BaseException):
            pass

        # Records the next check the start-up works out, which is the call that also records the check just made, and ends the pass there
        def record(*args, **kwargs):
            if "last_time" in kwargs:
                planned.append(kwargs["next_time"])
                raise StartedUp()

        profile = SimpleNamespace(username="target", userid=123, followers=0, followees=0, biography="", is_private=False, followed_by_viewer=False, mediacount=0, profile_pic_url_no_iphone="")
        bot = SimpleNamespace(context=SimpleNamespace(_session=SimpleNamespace(), is_logged_in=True, iphone_headers={}), load_session_from_file=lambda *args: None)
        monitored_account.setattr(im, "instaloader_client", lambda **kwargs: bot, raising=False)
        monitored_account.setattr(im, "profile_from_username_resilient", lambda _bot, username: profile, raising=False)
        monitored_account.setattr(im, "update_check_times", record)
        monitored_account.setattr(im, "now_local_naive", lambda: datetime(2024, 1, 1, 20, 10, 0))
        monitored_account.setattr(im, "CHECK_POSTS_IN_HOURS_RANGE", True)
        monitored_account.setattr(im, "MIN_H1", 22)
        monitored_account.setattr(im, "MAX_H1", 23)
        monitored_account.setattr(im, "DAYS_BETWEEN", 2)
        monitored_account.setattr(im, "FIRST_CHECK_SKIP_DAYS", 1)
        monitored_account.setattr(im, "FIRST_CHECK_PENDING", {"target"})
        monitored_account.setattr(im, "FIRST_CHECK_NOW", {"target"})
        monitored_account.setattr(im, "CHECK_STORY_AVAILABILITY", False)
        monitored_account.setattr(im, "INSTA_CHECK_INTERVAL", 60)
        monitored_account.setattr(im, "RANDOM_SLEEP_DIFF_LOW", 0)
        monitored_account.setattr(im, "RANDOM_SLEEP_DIFF_HIGH", 0)

        with pytest.raises(StartedUp):
            im.instagram_monitor_user("target", str(tmp_path / "events.csv"), False, True, True, True, True, False, skip_follow_changes=True)

        # Tonight's check would be at 22:10, so the scheduled first check is 22:10 tomorrow, not the DAYS_BETWEEN wait after this check
        assert planned == [datetime(2024, 1, 2, 22, 10, 0)]
        assert "target" not in im.FIRST_CHECK_NEXT

class TestStoryAvailabilityCheck:
    """The story check is one request before any story detail, so a setting has to be able to switch it off."""

    # Runs the real startup pass for a public target whose story property records that it was read
    @staticmethod
    def _run(monkeypatch, tmp_path, check_enabled):
        reads = []

        class Profile(SimpleNamespace):
            # Records the read, as the request it stands for would be made here
            @property
            def has_public_story(self):
                reads.append(True)
                return False

        profile = Profile(username="target", userid=123, followers=0, followees=0, biography="", is_private=False, followed_by_viewer=False, mediacount=0, profile_pic_url_no_iphone="")
        monkeypatch.setattr(im, "CHECK_STORY_AVAILABILITY", check_enabled)
        monkeypatch.setattr(im, "SKIP_FOLLOWERS", True, raising=False)
        bot = SimpleNamespace(context=SimpleNamespace(_session=SimpleNamespace(), is_logged_in=True, iphone_headers={}), load_session_from_file=lambda *args: None)
        monkeypatch.setattr(im, "instaloader_client", lambda **kwargs: bot, raising=False)
        monkeypatch.setattr(im, "profile_from_username_resilient", lambda _bot, username: profile, raising=False)
        monkeypatch.setattr(im, "interruptible_sleep", lambda *args, **kwargs: True, raising=False)
        im.instagram_monitor_user("target", str(tmp_path / "events.csv"), False, True, True, True, True, False, skip_follow_changes=True)
        return reads

    # The setting ships on, so a run that never sets it asks as before
    def test_the_template_ships_it_on(self):
        assert im.parse_config_content(im.CONFIG_BLOCK, "<built-in>")["CHECK_STORY_AVAILABILITY"] is True

    # With the check on, a signed-in pass asks Instagram about the story
    def test_a_signed_in_pass_asks_by_default(self, monitored_account, tmp_path):
        assert self._run(monitored_account, tmp_path, True) != []

    # With it off, no request is made, which is what keeps a refused story query from stopping the account
    def test_the_setting_skips_the_request(self, monitored_account, tmp_path):
        assert self._run(monitored_account, tmp_path, False) == []


class TestSessionUsernameLookup:
    """The own profile lookup only names the session, so a generic refusal of it must not end a check."""

    GENERIC_REFUSAL = "JSON Query to api/v1/accounts/current_user/: 200 OK - \"fail\" status, message \"We're sorry, but something went wrong. Please try again.\" when accessing https://i.instagram.com/api/v1/accounts/current_user/"

    # Replaces the lookup with one that raises the given error, and returns a client that was loaded for a named session
    @staticmethod
    def _bot(monkeypatch, error, loaded_name="loaded.account"):
        def own_profile(ctx):
            raise error

        monkeypatch.setattr(im.instaloader.Profile, "own_profile", own_profile)
        return SimpleNamespace(context=SimpleNamespace(username=loaded_name, is_logged_in=True))

    # A lookup Instagram answers returns the name it gives and says it was confirmed
    def test_a_confirmed_name_is_returned_as_confirmed(self, monitored_account):
        bot = SimpleNamespace(context=SimpleNamespace(username="loaded.account", is_logged_in=True))
        assert im.load_session_username(bot) == ("loop.account", True)

    # The generic refusal from the report falls back to the name the session was loaded with
    def test_the_generic_refusal_uses_the_loaded_name(self, monitored_account):
        bot = self._bot(monitored_account, im.instaloader.exceptions.ConnectionException(self.GENERIC_REFUSAL))
        assert im.load_session_username(bot) == ("loaded.account", False)

    # A client with no loaded name falls back to the configured session name
    def test_the_configured_name_is_the_second_choice(self, monitored_account):
        bot = self._bot(monitored_account, im.instaloader.exceptions.ConnectionException(self.GENERIC_REFUSAL), loaded_name="")
        assert im.load_session_username(bot) == ("loop.account", False)

    # With no name to fall back to, the refusal is raised rather than invented
    def test_no_name_raises_the_refusal(self, monitored_account):
        bot = self._bot(monitored_account, im.instaloader.exceptions.ConnectionException(self.GENERIC_REFUSAL), loaded_name="")
        monitored_account.setattr(im, "SESSION_USERNAME", "")
        with pytest.raises(im.instaloader.exceptions.ConnectionException):
            im.load_session_username(bot)

    # Rate limits, challenges, flagged sessions and logged-out replies are not hidden, since they decide what the run does next
    @pytest.mark.parametrize("error", [
        im.instaloader.exceptions.ConnectionException("429 Too Many Requests"),
        im.instaloader.exceptions.ConnectionException("checkpoint_required"),
        im.instaloader.exceptions.ConnectionException("400 Bad Request - feedback_required"),
        im.instaloader.exceptions.LoginRequiredException("Login required"),
    ])
    def test_account_level_replies_still_stop_the_check(self, monitored_account, error):
        bot = self._bot(monitored_account, error)
        with pytest.raises(type(error)):
            im.load_session_username(bot)

    # The startup pass carries on after the refusal and says the name was not confirmed
    def test_the_startup_pass_continues(self, monitored_account, tmp_path, capsys):
        monitored_account.setattr(im.instaloader.Profile, "own_profile", lambda ctx: (_ for _ in ()).throw(im.instaloader.exceptions.ConnectionException(self.GENERIC_REFUSAL)))
        TestStoryAvailabilityCheck._run(monitored_account, tmp_path, False)
        output = capsys.readouterr().out
        assert "not confirmed, using loop.account" in output
        assert "Error:" not in output


@pytest.mark.parametrize("kind", ["followers", "followings"])
@pytest.mark.parametrize("challenge", [False, True])
# Records a periodic list failure once when it passes from the paginator to the real monitor
def test_periodic_list_failure_is_recorded_once(monitored_account, kind, challenge):
    monkeypatch = monitored_account
    monkeypatch.setattr(im, "SKIP_" + kind.upper(), False)
    bot = SimpleNamespace(context=SimpleNamespace(_session=SimpleNamespace(), is_logged_in=True, iphone_headers={}), load_session_from_file=lambda *args: None)
    profile = _profile()
    field = "followers" if kind == "followers" else "followees"
    lookups = []
    fetches = []
    waits = []
    failure = im.instaloader.exceptions.AbortDownloadException("400 checkpoint_required") if challenge else im.instaloader.exceptions.TooManyRequestsException("429 Too Many Requests")

    # Changes the reported count after startup so the periodic list path runs
    def lookup(*args):
        lookups.append(1)
        setattr(profile, field, 1 if len(lookups) == 1 else 2)
        return profile

    # Produces the startup baseline then fails the next list request
    def names(*args, **kwargs):
        fetches.append(1)
        if len(fetches) > 1:
            raise failure
        yield SimpleNamespace(username="saved")

    # Ends the probe after the first periodic check if it remains retryable
    def wait(*args, **kwargs):
        waits.append(1)
        return len(waits) >= 2

    monkeypatch.setattr(im, "instaloader_client", lambda **kwargs: bot)
    monkeypatch.setattr(im, "profile_from_username_resilient", lookup)
    monkeypatch.setattr(im, "follow_list_generator", names)
    monkeypatch.setattr(im, "interruptible_sleep", wait)
    args = ("target", "", False, kind != "followers", kind != "followings", True, True, False)
    if challenge:
        with pytest.raises(SystemExit):
            im.instagram_monitor_user(*args)
    else:
        im.instagram_monitor_user(*args)
    assert len(fetches) == 2
    assert im.exposure_snapshot()["failures"] == {"challenge" if challenge else "rate_limit": 1}


# Verifies a first check that fails closes its report with one timestamp footer rather than two
def test_a_failed_first_check_closes_its_report_once(monitored_account, capsys):
    monitored_account.setattr(im, "print_cur_ts", _REAL_PRINT_CUR_TS)
    monitored_account.setattr(im, "instaloader_client", lambda **kwargs: SimpleNamespace(context=SimpleNamespace(is_logged_in=True)))
    throttle = im.instaloader.exceptions.ConnectionException('JSON Query to api/v1/users/web_profile_info/?username=target: 401 Unauthorized - "fail" status, message "Please wait a few minutes before you try again."')

    def lookup(*args):
        raise throttle

    monitored_account.setattr(im, "profile_from_username_resilient", lookup)
    with pytest.raises(SystemExit):
        im.instagram_monitor_user("target", "", False, True, True, True, True, False)

    output = capsys.readouterr().out
    assert "* Error: " in output and "To fix: " in output
    assert output.count("Timestamp:") == 1
