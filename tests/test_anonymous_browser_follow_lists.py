"""Offline tests for reading follower and following lists in anonymous mode through the browser source.

No surface lists followers without a session, but the browser source reads the dialog the way a person does, so
it needs a signed-in browser rather than a session. These cover how an anonymous run reaches that source, how the
browser's own sign-in is checked and waited for, and the startup, doctor and loop behavior around it.
"""

import io
import json
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from tqdm import tqdm

CHROME_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
SESSION_COOKIE = {"name": "sessionid", "value": "abc123", "domain": ".instagram.com"}
LOGIN_URL = "https://www.instagram.com/accounts/login/"


# Builds the bot stub an anonymous run holds, which has a context and no session behind it
def anonymous_bot():
    return SimpleNamespace(context=SimpleNamespace(is_logged_in=False, _session=SimpleNamespace(), iphone_headers={}), load_session_from_file=lambda *args: None)


# Builds a target profile stub carrying the counts the shortfall guard compares against
def target_profile(followers=0, followees=0):
    return SimpleNamespace(username="target.user", userid=4242, followers=followers, followees=followees)


class FakeBrowserContext:
    # Takes the cookie lists successive reads return, the last one repeating once they run out
    def __init__(self, reads):
        self.reads = list(reads)
        self.urls = []

    # Records the address asked about and returns the next cookie list
    def cookies(self, url):
        self.urls.append(url)
        return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]


class FakeLoginPage:
    # Moves a fake clock forward by each wait, so a deadline can pass without the test sleeping
    def __init__(self, clock, closes_on_wait=False):
        self.clock = clock
        self.closes_on_wait = closes_on_wait
        self.visited = []
        self.waits = []

    # Accepts the event handlers the scan registers, which this stand-in never fires
    def on(self, event, handler):
        return None

    # Records the page the browser was sent to
    def goto(self, url, **kwargs):
        self.visited.append(url)

    # Records the pause, advances the clock by it and optionally fails like a window the person closed
    def wait_for_timeout(self, milliseconds):
        if self.closes_on_wait:
            raise RuntimeError("Target page, context or browser has been closed")
        self.waits.append(milliseconds)
        self.clock[0] += milliseconds / 1000


@pytest.fixture
# Replaces the monotonic clock with the real one plus an offset a test page moves forward
def fake_clock(im_module, monkeypatch):
    offset = [0.0]
    real = time.monotonic
    monkeypatch.setattr(im_module.time, "monotonic", lambda: real() + offset[0])
    return offset


@pytest.fixture
# Gives every sign-in test a windowed browser that waits a short time and reports progress to a mock
def windowed_browser(im_module, monkeypatch):
    monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_HEADLESS", False)
    monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_LOGIN_WAIT", 5)
    monkeypatch.setattr(im_module, "log_activity", lambda *args, **kwargs: None)
    progress = Mock()
    monkeypatch.setattr(im_module, "browser_follow_list_progress", progress)
    return progress


class TestSourceSelection:
    # The browser source needs no session, so it is the one source an anonymous run reaches
    def test_an_anonymous_run_reads_lists_in_the_browser(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_SOURCE", "browser")
        monkeypatch.setattr(im_module, "browser_follow_list_batches", lambda *args, **kwargs: iter([["a", "b"]]))

        names = [entry.username for entry in im_module.follow_list_generator(anonymous_bot(), target_profile(followers=2), "followers")]

        assert names == ["a", "b"]

    # Every other source keeps the path it always had for an anonymous run
    @pytest.mark.parametrize("source", ["auto", "rest", "graphql"])
    def test_other_sources_still_take_the_graphql_path_without_a_session(self, im_module, monkeypatch, source):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_SOURCE", source)
        monkeypatch.setattr(im_module, "iter_graphql_follow_list", lambda *args, **kwargs: iter(["graphql"]))
        monkeypatch.setattr(im_module, "browser_follow_list_batches", Mock(side_effect=AssertionError("a browser was started")))

        assert list(im_module.follow_list_generator(anonymous_bot(), target_profile(), "followers")) == ["graphql"]

    # A session run always reads lists, and an anonymous run only through the browser source
    @pytest.mark.parametrize("source, skip_session, expected", [
        ("browser", True, True), ("auto", True, False), ("rest", True, False), ("graphql", True, False),
        ("browser", False, True), ("auto", False, True), ("rest", False, True), ("graphql", False, True),
    ])
    def test_which_runs_read_lists(self, im_module, monkeypatch, source, skip_session, expected):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_SOURCE", source)

        assert im_module.follow_lists_available(skip_session) is expected

    # An unrecognised source falls back to auto, which an anonymous run cannot use for lists
    def test_an_unknown_source_does_not_enable_lists_without_a_session(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_SOURCE", "brwoser")

        assert im_module.follow_lists_available(True) is False

    # Only the browser source with no session behind it counts as an anonymous browser run
    @pytest.mark.parametrize("source, session_username, skip_session, expected", [
        ("browser", "", True, True), ("browser", "someone", True, True), ("browser", "", False, True),
        ("browser", "someone", False, False), ("auto", "", True, False), ("graphql", "", True, False),
    ])
    def test_what_counts_as_an_anonymous_browser_run(self, im_module, monkeypatch, source, session_username, skip_session, expected):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_SOURCE", source)
        monkeypatch.setattr(im_module, "SESSION_USERNAME", session_username)
        monkeypatch.setattr(im_module, "SKIP_SESSION", skip_session)

        assert im_module.anonymous_browser_run() is expected


class TestSignedInCheck:
    # Only a session cookie with a value shows the browser is signed in, whatever else Instagram has stored
    @pytest.mark.parametrize("cookies, expected", [
        ([], False),
        ([{"name": "csrftoken", "value": "x"}, {"name": "mid", "value": "y"}], False),
        ([{"name": "sessionid", "value": ""}], False),
        ([SESSION_COOKIE], True),
        ([{"name": "csrftoken", "value": "x"}, SESSION_COOKIE], True),
    ])
    def test_the_session_cookie_decides(self, im_module, cookies, expected):
        context = FakeBrowserContext([cookies])

        assert im_module.browser_signed_in(context) is expected
        assert context.urls == ["https://www.instagram.com"]

    # A browser that cannot answer is treated as signed out rather than raising out of the check
    def test_an_unreadable_cookie_store_reads_as_signed_out(self, im_module):
        context = Mock()
        context.cookies.side_effect = RuntimeError("Target closed")

        assert im_module.browser_signed_in(context) is False


class TestEnsureSignedIn:
    # A browser that already holds a sign-in goes straight to the profile
    def test_a_signed_in_browser_is_left_alone(self, im_module, windowed_browser, fake_clock):
        page = FakeLoginPage(fake_clock)

        assert im_module.ensure_browser_signed_in(FakeBrowserContext([[SESSION_COOKIE]]), page, target_profile()) is True
        assert page.visited == [] and page.waits == []

    # A hidden window cannot be signed in to, so the run stops before opening any page
    def test_a_hidden_browser_stops_with_the_fix(self, im_module, windowed_browser, fake_clock, monkeypatch):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_HEADLESS", True)
        page = FakeLoginPage(fake_clock)

        with pytest.raises(im_module.BrowserFollowListError) as raised:
            im_module.ensure_browser_signed_in(FakeBrowserContext([[]]), page, target_profile())

        assert "FOLLOW_LIST_BROWSER_HEADLESS = False" in str(raised.value)
        assert page.visited == []

    # A wait of zero asks for an error instead of a pause
    def test_no_wait_stops_with_the_fix(self, im_module, windowed_browser, fake_clock, monkeypatch):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_LOGIN_WAIT", 0)
        page = FakeLoginPage(fake_clock)

        with pytest.raises(im_module.BrowserFollowListError) as raised:
            im_module.ensure_browser_signed_in(FakeBrowserContext([[]]), page, target_profile())

        assert "FOLLOW_LIST_BROWSER_LOGIN_WAIT" in str(raised.value)
        assert page.visited == []

    # A visible window is sent to the sign-in page and the run carries on the moment the sign-in appears
    def test_a_person_can_sign_in_while_the_run_waits(self, im_module, windowed_browser, fake_clock, capsys):
        page = FakeLoginPage(fake_clock)
        context = FakeBrowserContext([[], [], [SESSION_COOKIE]])

        assert im_module.ensure_browser_signed_in(context, page, target_profile()) is True

        assert page.visited == [LOGIN_URL]
        assert page.waits == [im_module.BROWSER_SIGN_IN_POLL_MS, im_module.BROWSER_SIGN_IN_SETTLE_MS]
        windowed_browser.assert_called_once_with("Waiting for sign-in")
        assert "Sign in in the browser window, waiting up to 5 seconds" in capsys.readouterr().out

    # The instruction reaches the screen even while the follower progress bar owns the terminal, which mutes plain prints
    def test_the_notice_is_shown_above_the_progress_bar(self, im_module, windowed_browser, fake_clock, monkeypatch, capsys):
        screen = io.StringIO()
        with tqdm(total=3, file=screen, bar_format="{n_fmt}/{total_fmt}") as bar:
            monkeypatch.setattr(im_module._thread_local, "pbar", bar, raising=False)

            im_module.ensure_browser_signed_in(FakeBrowserContext([[], [SESSION_COOKIE]]), FakeLoginPage(fake_clock), target_profile())

        assert "Sign in in the browser window, waiting up to 5 seconds" in screen.getvalue()
        assert "Sign in in the browser window" in capsys.readouterr().out

    # Running out of time without a sign-in stops the run with the step that fixes it
    def test_a_sign_in_that_never_comes_stops_the_run(self, im_module, windowed_browser, fake_clock):
        page = FakeLoginPage(fake_clock)

        with pytest.raises(im_module.BrowserFollowListError) as raised:
            im_module.ensure_browser_signed_in(FakeBrowserContext([[]]), page, target_profile())

        assert "No sign-in was completed within 5 seconds" in str(raised.value)
        assert "FOLLOW_LIST_BROWSER_LOGIN_WAIT" in str(raised.value)
        assert sum(page.waits) >= 5000

    # A stop request ends the wait quietly, since the caller is shutting the run down anyway
    def test_a_stop_request_ends_the_wait_without_an_error(self, im_module, windowed_browser, fake_clock):
        page = FakeLoginPage(fake_clock)
        stop = threading.Event()
        stop.set()

        assert im_module.ensure_browser_signed_in(FakeBrowserContext([[]]), page, target_profile(), stop_event=stop) is False
        assert page.waits == []

    # Closing the window during the wait is reported as the missing sign-in it is
    def test_a_closed_window_is_reported(self, im_module, windowed_browser, fake_clock):
        page = FakeLoginPage(fake_clock, closes_on_wait=True)

        with pytest.raises(im_module.BrowserFollowListError, match="window was closed"):
            im_module.ensure_browser_signed_in(FakeBrowserContext([[]]), page, target_profile())

    # Every way of being signed out carries the opening phrase the advice rule recognises, and the advice names the fix
    def test_every_signed_out_message_gets_the_sign_in_advice(self, im_module, windowed_browser, fake_clock, monkeypatch):
        messages = []
        for headless, wait in ((True, 5), (False, 0), (False, 5)):
            monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_HEADLESS", headless)
            monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_LOGIN_WAIT", wait)
            with pytest.raises(im_module.BrowserFollowListError) as raised:
                im_module.ensure_browser_signed_in(FakeBrowserContext([[]]), FakeLoginPage(fake_clock), target_profile())
            messages.append(str(raised.value))
        messages.append(str(pytest.raises(im_module.BrowserFollowListError, im_module.ensure_browser_signed_in, FakeBrowserContext([[]]), FakeLoginPage(fake_clock, closes_on_wait=True), target_profile()).value))

        for message in messages:
            advice = im_module.classify_recovery_error(f"BrowserFollowListError: {message}", is_logged_in=False)
            assert advice.code == "instagram.browser_signin"
            assert "FOLLOW_LIST_BROWSER_HEADLESS" in advice.fix and advice.retryable is False
            assert im_module.BROWSER_FOLLOW_LIST_GUIDE_URL in advice.fix


class TestBrowserLifecycle:
    # Builds a mocked Playwright driver whose persistent context holds the given cookies and one profile page
    @staticmethod
    def driver_with(monkeypatch, tmp_path, im_module, cookies, page=None):
        driver = MagicMock()
        driver.__enter__.return_value = driver
        context = driver.chromium.launch_persistent_context.return_value
        page = page or Mock(url="https://www.instagram.com/target.user/")
        context.pages = [page]
        context.cookies.return_value = cookies
        monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=lambda: driver))
        monkeypatch.setattr(im_module, "playwright_available", lambda: True)
        monkeypatch.setattr(im_module, "browser_profile_dir", lambda: str(tmp_path / "browser-profile"))
        monkeypatch.setattr(im_module, "harvest_follow_list_dialog", lambda *args, **kwargs: iter([["a", "b"]]))
        monkeypatch.setattr(im_module, "browser_follow_list_progress", Mock())
        monkeypatch.setattr(im_module, "log_activity", lambda *args, **kwargs: None)
        return context, page

    # An anonymous run with a signed-in profile reads the list and hands the browser no cookies
    def test_an_anonymous_scan_uses_the_browsers_own_sign_in(self, im_module, monkeypatch, tmp_path):
        context, page = self.driver_with(monkeypatch, tmp_path, im_module, [SESSION_COOKIE])
        monkeypatch.setattr(im_module, "browser_session_cookies", Mock(side_effect=AssertionError("cookies were taken from an anonymous session")))

        batches = list(im_module.browser_follow_list_batches(anonymous_bot(), target_profile(), "followers"))

        assert batches == [["a", "b"]]
        context.add_cookies.assert_not_called()
        page.goto.assert_called_once_with("https://www.instagram.com/target.user/", wait_until="domcontentloaded")
        context.close.assert_called_once()

    # Without a sign-in the run stops before it opens the profile, and still closes the browser
    def test_an_anonymous_scan_without_a_sign_in_stops_before_the_profile(self, im_module, monkeypatch, tmp_path):
        context, page = self.driver_with(monkeypatch, tmp_path, im_module, [])
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_HEADLESS", True)

        with pytest.raises(im_module.BrowserFollowListError, match="browser is not signed in"):
            list(im_module.browser_follow_list_batches(anonymous_bot(), target_profile(), "followers"))

        page.goto.assert_not_called()
        context.close.assert_called_once()

    # A stop request during the sign-in wait ends the scan without reading anything
    def test_a_stop_during_the_wait_ends_the_scan(self, im_module, monkeypatch, tmp_path, fake_clock):
        context, page = self.driver_with(monkeypatch, tmp_path, im_module, [])
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_HEADLESS", False)
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_LOGIN_WAIT", 5)
        stop = threading.Event()
        stop.set()

        assert list(im_module.browser_follow_list_batches(anonymous_bot(), target_profile(), "followers", stop_event=stop)) == []

        assert [call.args[0] for call in page.goto.call_args_list] == [LOGIN_URL]
        context.close.assert_called_once()

    # A session run keeps handing its cookies over and never waits for a sign-in
    def test_a_session_scan_still_hands_over_its_cookies(self, im_module, monkeypatch, tmp_path):
        context, page = self.driver_with(monkeypatch, tmp_path, im_module, [])
        monkeypatch.setattr(im_module, "browser_session_cookies", lambda bot: [SESSION_COOKIE])
        bot = SimpleNamespace(context=SimpleNamespace(is_logged_in=True, _session=SimpleNamespace()))

        assert list(im_module.browser_follow_list_batches(bot, target_profile(), "followers")) == [["a", "b"]]

        context.add_cookies.assert_called_once_with([SESSION_COOKIE])
        page.goto.assert_called_once_with("https://www.instagram.com/target.user/", wait_until="domcontentloaded")

    # An anonymous run keeps its browser profile apart from every session account's
    def test_the_anonymous_profile_has_its_own_directory(self, im_module, monkeypatch, tmp_path):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_PROFILE_DIR", str(tmp_path))
        monkeypatch.setattr(im_module, "SESSION_USERNAME", "")
        monkeypatch.setattr(im_module, "SKIP_SESSION", True)
        anonymous = im_module.browser_profile_dir()
        monkeypatch.setattr(im_module, "SESSION_USERNAME", "someone")
        monkeypatch.setattr(im_module, "SKIP_SESSION", False)

        assert anonymous != im_module.browser_profile_dir()
        assert anonymous.startswith(str(tmp_path))


class TestProfileSignInProbe:
    # Writes a Chromium cookie database holding the given (name, expiry in Chromium microseconds) rows
    @staticmethod
    def write_cookies(path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, expires_utc INTEGER)")
            connection.executemany("INSERT INTO cookies VALUES ('.instagram.com', ?, '', x'00', ?)", rows)
            connection.commit()
        finally:
            connection.close()

    # Returns a Chromium expiry timestamp the given number of seconds from now
    @staticmethod
    def chromium_expiry(seconds_from_now):
        return int((time.time() + seconds_from_now + 11644473600) * 1_000_000)

    # A profile that was never opened has no sign-in
    def test_a_missing_profile_is_signed_out(self, im_module, monkeypatch, tmp_path):
        monkeypatch.setattr(im_module, "browser_profile_dir", lambda: str(tmp_path / "never-created"))

        assert im_module.anonymous_browser_profile_signed_in() is False

    # A profile directory without a cookie database is signed out as well
    def test_a_profile_without_cookies_is_signed_out(self, im_module, monkeypatch, tmp_path):
        monkeypatch.setattr(im_module, "browser_profile_dir", lambda: str(tmp_path))

        assert im_module.anonymous_browser_profile_signed_in() is False

    # A current session cookie in either location Chromium keeps its database reads as signed in
    @pytest.mark.parametrize("location", [("Default", "Network", "Cookies"), ("Default", "Cookies")])
    def test_a_current_session_cookie_reads_as_signed_in(self, im_module, monkeypatch, tmp_path, location):
        self.write_cookies(tmp_path.joinpath(*location), [("sessionid", self.chromium_expiry(86400))])
        monkeypatch.setattr(im_module, "browser_profile_dir", lambda: str(tmp_path))

        assert im_module.anonymous_browser_profile_signed_in() is True

    # An expired session, or cookies that are not the session, read as signed out
    @pytest.mark.parametrize("name, seconds_from_now", [("sessionid", -3600), ("csrftoken", 86400)])
    def test_an_expired_or_unrelated_cookie_reads_as_signed_out(self, im_module, monkeypatch, tmp_path, name, seconds_from_now):
        self.write_cookies(tmp_path / "Default" / "Network" / "Cookies", [(name, self.chromium_expiry(seconds_from_now))])
        monkeypatch.setattr(im_module, "browser_profile_dir", lambda: str(tmp_path))

        assert im_module.anonymous_browser_profile_signed_in() is False

    # A database that cannot be read says so instead of claiming either answer
    def test_an_unreadable_database_is_unknown(self, im_module, monkeypatch, tmp_path):
        cookie_file = tmp_path / "Default" / "Network" / "Cookies"
        cookie_file.parent.mkdir(parents=True)
        cookie_file.write_bytes(b"this is not an sqlite database" * 100)
        monkeypatch.setattr(im_module, "browser_profile_dir", lambda: str(tmp_path))

        assert im_module.anonymous_browser_profile_signed_in() is None


class TestDoctor:
    # Points the doctor at an anonymous browser run with the given window and wait settings
    @staticmethod
    def configure(im_module, monkeypatch, headless=False, wait=300, signed_in=False):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_SOURCE", "browser")
        monkeypatch.setattr(im_module, "SESSION_USERNAME", "")
        monkeypatch.setattr(im_module, "SKIP_SESSION", True)
        monkeypatch.setattr(im_module, "SKIP_FOLLOWERS", False)
        monkeypatch.setattr(im_module, "SKIP_FOLLOWINGS", False)
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_HEADLESS", headless)
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_LOGIN_WAIT", wait)
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_CHANNEL", "chromium")
        monkeypatch.setattr(im_module, "HTTP_BACKEND", "curl_cffi")
        monkeypatch.setattr(im_module, "_CURL_CFFI_AVAILABLE", True)
        monkeypatch.setattr(im_module, "CURL_CFFI_IMPERSONATE", "auto")
        monkeypatch.setattr(im_module, "USER_AGENT", CHROME_AGENT)
        monkeypatch.setattr(im_module, "browser_follow_list_readiness", lambda: (True, "Channel: chromium", ""))
        monkeypatch.setattr(im_module, "anonymous_browser_profile_signed_in", lambda: signed_in)

    # Returns the sign-in row the configuration checks produced, or None
    @staticmethod
    def sign_in_row(im_module):
        rows = [check for check in im_module.doctor_check_configuration(["target.user"]) if "sign-in" in check.label or "signed in" in check.label]
        return rows[0] if rows else None

    # A profile that holds a sign-in passes
    def test_a_signed_in_profile_passes(self, im_module, monkeypatch):
        self.configure(im_module, monkeypatch, signed_in=True)

        assert self.sign_in_row(im_module).status == "PASS"

    # A windowed run that is not signed in yet is a warning, since the first run can sign in
    @pytest.mark.parametrize("signed_in", [False, None])
    def test_a_windowed_run_without_a_sign_in_warns(self, im_module, monkeypatch, signed_in):
        self.configure(im_module, monkeypatch, signed_in=signed_in)

        row = self.sign_in_row(im_module)

        assert row.status == "WARN"
        assert "300 seconds" in row.advice.fix

    # A run that cannot sign in fails, since it would stop on its first list
    @pytest.mark.parametrize("headless, wait", [(True, 300), (False, 0)])
    def test_a_run_that_cannot_sign_in_fails(self, im_module, monkeypatch, headless, wait):
        self.configure(im_module, monkeypatch, headless=headless, wait=wait)

        row = self.sign_in_row(im_module)

        assert row.status == "FAIL"
        assert "FOLLOW_LIST_BROWSER_HEADLESS" in row.advice.fix and "FOLLOW_LIST_BROWSER_LOGIN_WAIT" in row.advice.fix

    # Only an anonymous browser run that reads names depends on the browser's own sign-in
    @pytest.mark.parametrize("change", [
        {"SESSION_USERNAME": "someone", "SKIP_SESSION": False},
        {"SKIP_FOLLOWERS": True, "SKIP_FOLLOWINGS": True},
        {"FOLLOW_LIST_SOURCE": "graphql"},
    ])
    def test_other_runs_have_no_sign_in_row(self, im_module, monkeypatch, change):
        self.configure(im_module, monkeypatch)
        for name, value in change.items():
            monkeypatch.setattr(im_module, name, value)

        assert self.sign_in_row(im_module) is None


# Runs the real startup path far enough to print the summary and returns what the terminal was shown
def startup_output(im_module, monkeypatch, capsys, tmp_path, *extra_args):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(im_module.sys, "argv", ["instagram_monitor.py", "target.user", "--env-file", "none", "--no-color", "--disable-logging", "--verbose", *extra_args])
    monkeypatch.setattr(im_module, "CLI_CONFIG_PATH", None)
    monkeypatch.setattr(im_module, "DASHBOARD_ENABLED", False)
    monkeypatch.setattr(im_module, "WEB_DASHBOARD_ENABLED", False)
    monkeypatch.setattr(im_module, "HTTP_BACKEND", "curl_cffi")
    monkeypatch.setattr(im_module, "_CURL_CFFI_AVAILABLE", True)
    monkeypatch.setattr(im_module, "find_config_file", lambda path=None: None)
    monkeypatch.setattr(im_module, "clear_screen", lambda *args, **kwargs: None)
    monkeypatch.setattr(im_module, "check_internet", lambda: True)
    monkeypatch.setattr(im_module, "start_dashboard_input_handler", Mock(side_effect=SystemExit(0)))
    with pytest.raises(SystemExit):
        im_module.run_main()
    return capsys.readouterr().out


class TestStartup:
    @pytest.fixture(autouse=True)
    # Restores every module setting, since these tests run the real startup path and it rewrites them in place
    def restored_settings(self, im_module):
        snapshot = {name: value for name, value in vars(im_module).items() if name.isupper()}
        yield
        for name, value in snapshot.items():
            setattr(im_module, name, value)

    # An anonymous run keeps both lists when the browser source is configured, and says where the sign-in lives
    def test_an_anonymous_browser_run_keeps_the_lists(self, im_module, monkeypatch, capsys, tmp_path):
        output = startup_output(im_module, monkeypatch, capsys, tmp_path, "--follow-list-source", "browser")

        assert (im_module.SKIP_FOLLOWERS, im_module.SKIP_FOLLOWINGS) == (False, False)
        assert "No login (follow lists via browser)" in output
        assert "* Skip fetching followers:" in output and "False" in output.split("* Skip fetching followers:")[1].splitlines()[0]
        assert "* Browser sign-in:" in output and "kept in" in output

    # Follower churn detection re-reads the lists on every check, which stays off without a session
    def test_churn_detection_stays_off_without_a_session(self, im_module, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(im_module, "FOLLOWERS_CHURN_DETECTION", True)

        output = startup_output(im_module, monkeypatch, capsys, tmp_path, "--follow-list-source", "browser")

        assert im_module.FOLLOWERS_CHURN_DETECTION is False
        assert "autodisabled due to SKIP_SESSION" in output

    # The skip switches still apply to an anonymous browser run
    def test_the_skip_switches_still_apply(self, im_module, monkeypatch, capsys, tmp_path):
        startup_output(im_module, monkeypatch, capsys, tmp_path, "--follow-list-source", "browser", "--skip-followers")

        assert (im_module.SKIP_FOLLOWERS, im_module.SKIP_FOLLOWINGS) == (True, False)

    # Without the browser source an anonymous run skips the lists exactly as before
    @pytest.mark.parametrize("source", ["auto", "rest", "graphql"])
    def test_other_sources_keep_skipping_the_lists(self, im_module, monkeypatch, capsys, tmp_path, source):
        output = startup_output(im_module, monkeypatch, capsys, tmp_path, "--follow-list-source", source)

        assert (im_module.SKIP_FOLLOWERS, im_module.SKIP_FOLLOWINGS) == (True, True)
        assert "No login (limited data)" in output
        assert "Browser sign-in" not in output


    # The breaker stays armed by default, since turning it off is a choice the user makes
    def test_the_breaker_stays_armed_by_default(self, im_module, monkeypatch, capsys, tmp_path):
        output = startup_output(im_module, monkeypatch, capsys, tmp_path, "--follow-list-source", "browser")

        assert im_module.BROWSER_MODE_CIRCUIT_BREAKER_DISABLE is False
        assert im_module.CIRCUIT_BREAKER is True
        breaker_line = next(line for line in output.splitlines() if "Account circuit breaker:" in line)
        assert "Armed" in breaker_line

    # With the option on, an anonymous browser run turns the breaker off and the summary says which setting did it
    def test_the_option_turns_the_breaker_off_for_an_anonymous_browser_run(self, im_module, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(im_module, "BROWSER_MODE_CIRCUIT_BREAKER_DISABLE", True)

        output = startup_output(im_module, monkeypatch, capsys, tmp_path, "--follow-list-source", "browser")

        assert im_module.CIRCUIT_BREAKER is False
        assert "Disabled (BROWSER_MODE_CIRCUIT_BREAKER_DISABLE)" in output

    # A challenge or suspension page then cannot stop anything, while by default it does, which is why the option exists
    def test_a_warning_page_trips_the_breaker_only_without_the_option(self, im_module, monkeypatch, capsys, tmp_path):
        startup_output(im_module, monkeypatch, capsys, tmp_path, "--follow-list-source", "browser")
        im_module.note_instagram_failure("BrowserFollowListError: Instagram answered with a challenge page", "target.user", anonymous_bot())
        assert im_module.circuit_breaker_state() is not None

    # With the option on the same failures are recorded without tripping the breaker
    def test_a_warning_page_cannot_trip_the_breaker_with_the_option(self, im_module, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(im_module, "BROWSER_MODE_CIRCUIT_BREAKER_DISABLE", True)
        startup_output(im_module, monkeypatch, capsys, tmp_path, "--follow-list-source", "browser")

        im_module.note_instagram_failure("BrowserFollowListError: Instagram answered with a suspended account page", "target.user", anonymous_bot())
        im_module.note_instagram_failure("BrowserFollowListError: Instagram answered with a challenge page", "target.user", anonymous_bot())

        assert im_module.circuit_breaker_state() is None

    # The option is about the browser source only, so another anonymous source keeps the breaker even with it on
    @pytest.mark.parametrize("source", ["auto", "rest", "graphql"])
    def test_other_sources_keep_the_breaker_with_the_option_on(self, im_module, monkeypatch, capsys, tmp_path, source):
        monkeypatch.setattr(im_module, "BROWSER_MODE_CIRCUIT_BREAKER_DISABLE", True)

        output = startup_output(im_module, monkeypatch, capsys, tmp_path, "--follow-list-source", source)

        assert im_module.CIRCUIT_BREAKER is True
        assert "BROWSER_MODE_CIRCUIT_BREAKER_DISABLE" not in output

    # A breaker the user already turned off is reported as plain disabled, not as something the option did
    def test_a_breaker_the_user_disabled_is_not_blamed_on_the_option(self, im_module, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(im_module, "CIRCUIT_BREAKER", False)
        monkeypatch.setattr(im_module, "BROWSER_MODE_CIRCUIT_BREAKER_DISABLE", True)

        output = startup_output(im_module, monkeypatch, capsys, tmp_path, "--follow-list-source", "browser")

        assert "Disabled (BROWSER_MODE_CIRCUIT_BREAKER_DISABLE)" not in output
        assert im_module.CIRCUIT_BREAKER_AUTODISABLED_REASON == ""

    # A session run keeps its breaker whatever the option says, since there the breaker protects a real account
    def test_a_session_run_keeps_the_breaker_with_the_option_on(self, im_module, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(im_module, "BROWSER_MODE_CIRCUIT_BREAKER_DISABLE", True)

        output = startup_output(im_module, monkeypatch, capsys, tmp_path, "--follow-list-source", "browser", "--session-username", "someone")

        assert im_module.CIRCUIT_BREAKER is True
        assert "BROWSER_MODE_CIRCUIT_BREAKER_DISABLE" not in output

    # The daily identity budget is a limit on the same account, so the option turns it off with the breaker
    def test_the_option_turns_the_budget_off_too(self, im_module, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(im_module, "BROWSER_MODE_CIRCUIT_BREAKER_DISABLE", True)

        output = startup_output(im_module, monkeypatch, capsys, tmp_path, "--follow-list-source", "browser", "--identity-budget", "5000")

        assert im_module.IDENTITY_BUDGET_PER_DAY == 0
        budget_line = next(line for line in output.splitlines() if "Identity budget:" in line)
        assert "Disabled (BROWSER_MODE_CIRCUIT_BREAKER_DISABLE, counted but not capped)" in budget_line

    # With the budget off a scan is never refused for lack of it, however long the list and however much was read today
    def test_a_long_scan_is_not_refused_with_the_option_on(self, im_module, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(im_module, "BROWSER_MODE_CIRCUIT_BREAKER_DISABLE", True)
        startup_output(im_module, monkeypatch, capsys, tmp_path, "--follow-list-source", "browser", "--identity-budget", "5000")
        im_module.record_identities_returned(4900)

        assert im_module.name_fetch_refusal(4800) is None

    # Without the option the budget keeps refusing a scan it cannot cover, which is the behavior the option exists to lift
    def test_the_budget_still_refuses_without_the_option(self, im_module, monkeypatch, capsys, tmp_path):
        startup_output(im_module, monkeypatch, capsys, tmp_path, "--follow-list-source", "browser", "--identity-budget", "5000")
        im_module.record_identities_returned(1365)

        refusal = im_module.name_fetch_refusal(4800)

        assert im_module.IDENTITY_BUDGET_PER_DAY == 5000
        assert "only 3635 of the daily identity budget of 5000 is left" in refusal.screen

    # The budget is left alone for another source and for a session run, even with the option on
    @pytest.mark.parametrize("extra", [("--follow-list-source", "graphql"), ("--follow-list-source", "browser", "--session-username", "someone")])
    def test_the_budget_is_kept_where_the_option_does_not_apply(self, im_module, monkeypatch, capsys, tmp_path, extra):
        monkeypatch.setattr(im_module, "BROWSER_MODE_CIRCUIT_BREAKER_DISABLE", True)

        output = startup_output(im_module, monkeypatch, capsys, tmp_path, *extra, "--identity-budget", "5000")

        assert im_module.IDENTITY_BUDGET_PER_DAY == 5000
        assert "Disabled (BROWSER_MODE_CIRCUIT_BREAKER_DISABLE, counted but not capped)" not in output

    # The option is a plain True or False, and the shipped template turns it off
    def test_the_option_is_a_boolean_that_ships_off(self, im_module, monkeypatch):
        assert im_module.parse_config_content(im_module.CONFIG_BLOCK, "<built-in>")["BROWSER_MODE_CIRCUIT_BREAKER_DISABLE"] is False

        monkeypatch.setattr(im_module, "BROWSER_MODE_CIRCUIT_BREAKER_DISABLE", "yes")

        assert any("BROWSER_MODE_CIRCUIT_BREAKER_DISABLE" in problem for problem in im_module.runtime_boolean_errors())

    # A browser kept open on error is named in the summary so a run that waits on a window is never a surprise
    def test_the_summary_names_a_kept_open_browser(self, im_module, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_KEEP_OPEN_ON_ERROR", True)
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_HEADLESS", False)

        output = startup_output(im_module, monkeypatch, capsys, tmp_path, "--follow-list-source", "browser")

        assert "* Browser kept open on error:" in output and "until its window is closed" in output


class TestMonitoringPass:
    @pytest.fixture
    # Runs the real monitoring pass against a fake Instagram with no session, writing under tmp_path and never waiting
    def anonymous_pass(self, im_module, monkeypatch, tmp_path):
        settings = {
            "OUTPUT_DIR": str(tmp_path), "LOCAL_TIMEZONE": "UTC", "SESSION_USERNAME": "", "SESSION_PASSWORD": "", "SKIP_SESSION": True,
            "CIRCUIT_BREAKER": False, "IDENTITY_BUDGET_PER_DAY": 0, "SKIP_FOLLOWERS": False, "SKIP_FOLLOWINGS": True,
            "SKIP_GETTING_STORY_DETAILS": True, "SKIP_GETTING_POSTS_DETAILS": True, "GET_MORE_POST_DETAILS": False,
            "DETECT_CHANGED_PROFILE_PIC": False, "CHECK_POSTS_IN_HOURS_RANGE": False, "WEB_DASHBOARD_ENABLED": False,
            "DASHBOARD_ENABLED": False, "RICH_AVAILABLE": False, "PROXY_ENABLED": False, "DISABLE_LOGGING": True,
            "COLORED_OUTPUT": False, "ERROR_NOTIFICATION": False, "WEBHOOK_ENABLED": False, "BE_HUMAN": False,
            "NEXT_OPERATION_DELAY": 0, "FOLLOWERS_CHURN_DETECTION": False, "ADVANCED_FOLLOWER_FETCH": False,
            "INSTA_CHECK_INTERVAL": 1, "RANDOM_SLEEP_DIFF_LOW": 0, "RANDOM_SLEEP_DIFF_HIGH": 0,
        }
        for name, value in settings.items():
            monkeypatch.setattr(im_module, name, value, raising=False)
        for name in ("log_activity", "update_ui_data", "update_check_times", "print_cur_ts", "ensure_instagram_session_wrapped", "refresh_proxy_if_needed"):
            monkeypatch.setattr(im_module, name, lambda *args, **kwargs: None, raising=False)
        monkeypatch.setattr(im_module.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(im_module, "get_total_reels_count", lambda user, bot, skip_session=False, posts_count=None: 0)
        monkeypatch.setattr(im_module, "get_dashboard_config_data", lambda *args, **kwargs: {"ready": True})
        profile = SimpleNamespace(username="target", userid=123, followers=3, followees=0, biography="", is_private=False, followed_by_viewer=False, mediacount=0, has_public_story=False, profile_pic_url_no_iphone="")
        monkeypatch.setattr(im_module, "instaloader_client", lambda **kwargs: anonymous_bot(), raising=False)
        monkeypatch.setattr(im_module, "profile_from_username_resilient", lambda _bot, username: profile, raising=False)
        monkeypatch.setattr(im_module, "interruptible_sleep", lambda *args, **kwargs: True, raising=False)
        return monkeypatch

    # Drives one pass with the given skip switches and returns the baseline file it left behind, if any
    def run_pass(self, im_module, tmp_path, skip_followers=False):
        im_module.instagram_monitor_user("target", str(tmp_path / "events.csv"), True, skip_followers, True, True, True, False)
        baseline = tmp_path / "target" / "json" / "instagram_target_followers.json"
        return json.loads(baseline.read_text(encoding="utf-8")) if baseline.exists() else None

    # The loop itself reads and saves the followers of an anonymous run through the browser
    def test_the_loop_saves_followers_read_in_the_browser(self, im_module, anonymous_pass, tmp_path):
        anonymous_pass.setattr(im_module, "FOLLOW_LIST_SOURCE", "browser")
        asked = []

        # Stands in for the browser, recording what the loop asked it to read
        def batches(bot, profile, kind, stop_event=None):
            asked.append((bot.context.is_logged_in, profile.username, kind))
            yield ["alpha", "beta", "gamma"]

        anonymous_pass.setattr(im_module, "browser_follow_list_batches", batches)

        saved = self.run_pass(im_module, tmp_path)

        assert asked == [(False, "target", "followers")]
        assert saved == [3, ["alpha", "beta", "gamma"]]

    # The skip switch still keeps the loop from reading followers
    def test_the_loop_honors_the_skip_switch(self, im_module, anonymous_pass, tmp_path):
        anonymous_pass.setattr(im_module, "FOLLOW_LIST_SOURCE", "browser")
        anonymous_pass.setattr(im_module, "browser_follow_list_batches", Mock(side_effect=AssertionError("followers were read")))

        assert self.run_pass(im_module, tmp_path, skip_followers=True) is None

    # Without the browser source the loop reads nothing in anonymous mode, however the switches are set
    @pytest.mark.parametrize("source", ["auto", "rest", "graphql"])
    def test_the_loop_reads_nothing_without_the_browser_source(self, im_module, anonymous_pass, tmp_path, source):
        anonymous_pass.setattr(im_module, "FOLLOW_LIST_SOURCE", source)
        anonymous_pass.setattr(im_module, "browser_follow_list_batches", Mock(side_effect=AssertionError("a browser was started")))
        anonymous_pass.setattr(im_module, "iter_graphql_follow_list", Mock(side_effect=AssertionError("a list was read")))

        assert self.run_pass(im_module, tmp_path) is None

    # A browser that is not signed in ends the run with the sign-in advice rather than a bare traceback
    def test_the_loop_reports_a_missing_sign_in_with_advice(self, im_module, anonymous_pass, tmp_path, capsys):
        anonymous_pass.setattr(im_module, "FOLLOW_LIST_SOURCE", "browser")

        # Fails the way a browser with no sign-in does
        def batches(bot, profile, kind, stop_event=None):
            raise im_module.BrowserFollowListError(im_module.browser_signed_out_message("Set FOLLOW_LIST_BROWSER_HEADLESS = False, run again and sign in in the window that opens"))
            yield []

        anonymous_pass.setattr(im_module, "browser_follow_list_batches", batches)

        with pytest.raises(SystemExit):
            self.run_pass(im_module, tmp_path)

        output = capsys.readouterr().out
        assert "The browser is not signed in to Instagram" in output
        assert "FOLLOW_LIST_BROWSER_HEADLESS" in output


    # Replaces the target with a private one the anonymous lookup cannot view, which reports followings but no followers
    @staticmethod
    def private_target(im_module, anonymous_pass):
        profile = SimpleNamespace(username="target", userid=123, followers=0, followees=296, biography="", is_private=True, followed_by_viewer=False, mediacount=0, has_public_story=False, profile_pic_url_no_iphone="")
        anonymous_pass.setattr(im_module, "profile_from_username_resilient", lambda _bot, username: profile)
        anonymous_pass.setattr(im_module, "FOLLOW_LIST_SOURCE", "browser")

    # Runs one pass reading followings only, and returns the followings baseline it left behind, if any
    @staticmethod
    def run_followings(im_module, tmp_path):
        im_module.instagram_monitor_user("target", str(tmp_path / "events.csv"), True, True, False, True, True, False)
        baseline = tmp_path / "target" / "json" / "instagram_target_followings.json"
        return json.loads(baseline.read_text(encoding="utf-8")) if baseline.exists() else None

    # A private target is no longer skipped on the anonymous lookup's say, so the browser reads its followings
    def test_a_private_target_is_attempted(self, im_module, anonymous_pass, tmp_path):
        self.private_target(im_module, anonymous_pass)
        asked = []
        names = [f"user{index:03d}" for index in range(296)]

        # Stands in for a browser whose account follows the private target
        def batches(bot, profile, kind, stop_event=None):
            asked.append(kind)
            yield names

        anonymous_pass.setattr(im_module, "browser_follow_list_batches", batches)

        assert self.run_followings(im_module, tmp_path) == [296, names]
        assert asked == ["following"]

    # A private target the browser's account does not follow skips the list with a note and does not end the run
    def test_an_unfollowed_private_target_is_skipped_without_failing(self, im_module, anonymous_pass, tmp_path, capsys):
        self.private_target(im_module, anonymous_pass)

        # Stands in for a browser whose account is not allowed to see the target's lists
        def batches(bot, profile, kind, stop_event=None):
            raise im_module.BrowserFollowListPrivateError(f"{profile.username} is private and the browser's own account does not follow it, so Instagram does not show it the {kind}")
            yield []

        anonymous_pass.setattr(im_module, "browser_follow_list_batches", batches)

        assert self.run_followings(im_module, tmp_path) is None

        output = capsys.readouterr().out
        assert "target is private and the browser's own account does not follow it" in output
        assert "Its following are skipped" in output

    # Without the browser source a private target stays skipped, exactly as before
    @pytest.mark.parametrize("source", ["auto", "graphql"])
    def test_other_sources_still_skip_a_private_target(self, im_module, anonymous_pass, tmp_path, source):
        self.private_target(im_module, anonymous_pass)
        anonymous_pass.setattr(im_module, "FOLLOW_LIST_SOURCE", source)
        anonymous_pass.setattr(im_module, "browser_follow_list_batches", Mock(side_effect=AssertionError("a browser was started")))

        assert self.run_followings(im_module, tmp_path) is None


    # A fetch the budget refuses starts no progress bar and no browser, and the reason is on screen
    def test_a_refused_fetch_shows_no_progress_bar(self, im_module, anonymous_pass, tmp_path, capsys):
        anonymous_pass.setattr(im_module, "FOLLOW_LIST_SOURCE", "browser")
        anonymous_pass.setattr(im_module, "IDENTITY_BUDGET_PER_DAY", 5000)
        anonymous_pass.setattr(im_module, "identity_budget_exhausted", lambda: False)
        anonymous_pass.setattr(im_module, "identity_budget_remaining", lambda: 2)
        bar = Mock()
        anonymous_pass.setattr(im_module, "setup_pbar", bar)
        anonymous_pass.setattr(im_module, "browser_follow_list_batches", Mock(side_effect=AssertionError("a browser was started")))

        assert self.run_pass(im_module, tmp_path) is None

        bar.assert_not_called()
        assert "Skipping name fetch: 3 names are needed but only 2 of the daily identity budget of 5000 is left" in capsys.readouterr().out


class TestExpiredSignIn:
    # Builds a page stub standing on the given address
    @staticmethod
    def page_at(url):
        return Mock(url=url)

    # An anonymous run sent to the login page learns its own sign-in is gone, not that a session needs importing
    def test_a_login_page_means_the_browsers_sign_in_expired(self, im_module):
        with pytest.raises(im_module.BrowserFollowListError) as raised:
            im_module.guard_browser_page_state(self.page_at("https://www.instagram.com/accounts/login/?next=/target.user/"), anonymous=True)

        message = str(raised.value)
        assert "browser is not signed in" in message and "expired or was removed" in message
        assert "re-import" not in message
        assert im_module.classify_recovery_error(f"BrowserFollowListError: {message}").code == "instagram.browser_signin"

    # A session run keeps the message that tells it to refresh its session
    def test_a_session_run_keeps_the_session_advice(self, im_module):
        with pytest.raises(im_module.BrowserFollowListError, match="re-import the session"):
            im_module.guard_browser_page_state(self.page_at("https://www.instagram.com/accounts/login/"))

    # Other interruptions read the same for an anonymous run, since they are about the account and not the sign-in
    @pytest.mark.parametrize("path, described", [("challenge/", "a challenge page"), ("accounts/suspended/", "a suspended account page")])
    def test_other_interruptions_are_unchanged(self, im_module, path, described):
        with pytest.raises(im_module.BrowserFollowListError, match=described):
            im_module.guard_browser_page_state(self.page_at(f"https://www.instagram.com/{path}"), anonymous=True)

    # A dialog that fails to open on the login page reports the sign-in, because that is what actually failed
    @pytest.mark.parametrize("click_fails", [False, True])
    def test_a_dialog_that_cannot_open_names_the_missing_sign_in(self, im_module, click_fails):
        page = self.page_at("https://www.instagram.com/accounts/login/")
        if click_fails:
            page.locator.return_value.or_.return_value.first.click.side_effect = RuntimeError("selector failed")
        else:
            page.wait_for_selector.side_effect = RuntimeError("selector failed")

        with pytest.raises(im_module.BrowserFollowListError, match="browser is not signed in"):
            im_module.open_browser_follow_list_dialog(page, "target.user", "followers", anonymous=True)

    # A missing sign-in is something a person fixes in the window, so it never reads as an account-level failure
    def test_a_missing_sign_in_is_not_an_account_level_failure(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_HEADLESS", True)
        message = im_module.format_error_message(im_module.BrowserFollowListError(im_module.browser_signed_out_message("Sign in.")))

        assert im_module.is_account_level_failure(im_module.classify_failure_class(message)) is False


class TestSettingAndMessages:
    # The wait is a number of seconds, so a negative or non-numeric value is rejected at startup like the other timings
    @pytest.mark.parametrize("value", [-1, "soon", None, float("nan")])
    def test_a_bad_wait_is_rejected(self, im_module, monkeypatch, value):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_LOGIN_WAIT", value)

        assert "FOLLOW_LIST_BROWSER_LOGIN_WAIT" in im_module.runtime_configuration_problems()

    # Zero is a valid wait, since it is how a run asks to stop instead of waiting
    @pytest.mark.parametrize("value", [0, 300, 12.5])
    def test_a_usable_wait_is_accepted(self, im_module, monkeypatch, value):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_LOGIN_WAIT", value)

        assert "FOLLOW_LIST_BROWSER_LOGIN_WAIT" not in im_module.runtime_configuration_problems()

    # Without a progress bar the message is an ordinary print
    def test_a_message_without_a_progress_bar_is_printed_once(self, im_module, monkeypatch, capsys):
        monkeypatch.setattr(im_module._thread_local, "pbar", None, raising=False)

        im_module.print_above_progress("* hello")

        assert capsys.readouterr().out == "* hello\n"


SUSPENDED_URL = "https://www.instagram.com/accounts/suspended/"
CHALLENGE_URL = "https://www.instagram.com/challenge/"
PROFILE_URL = "https://www.instagram.com/target.user/"


class ScriptedBrowserPage(FakeLoginPage):
    # Lands on the next scripted address after each goto, and a person clears an interruption after enough waiting
    def __init__(self, clock, landings, clears_after_ms=None):
        super().__init__(clock)
        self.url = ""
        self.landings = list(landings)
        self.clears_after_ms = clears_after_ms
        self.waited = 0

    # Records the visit and lands on the next scripted address, or on the one asked for when the script runs out
    def goto(self, url, **kwargs):
        super().goto(url)
        self.url = self.landings.pop(0) if self.landings else url

    # Waits like the fake clock page, and moves off an interruption once the person has had long enough
    def wait_for_timeout(self, milliseconds):
        super().wait_for_timeout(milliseconds)
        self.waited += milliseconds
        if self.clears_after_ms is not None and self.waited >= self.clears_after_ms and any(marker in self.url for marker in ("/challenge", "/accounts/suspended", "/accounts/disabled", "/accounts/login")):
            self.url = "https://www.instagram.com/"


class TestClearingAWarning:
    # Runs one anonymous scan on a scripted page and returns the batches it yielded
    def scan(self, im_module, monkeypatch, tmp_path, page, stop_event=None):
        self.context, _ = TestBrowserLifecycle.driver_with(monkeypatch, tmp_path, im_module, [SESSION_COOKIE], page=page)
        monkeypatch.setattr(im_module, "log_activity", lambda *args, **kwargs: None)
        monkeypatch.setattr(im_module, "open_browser_follow_list_dialog", Mock())
        return list(im_module.browser_follow_list_batches(anonymous_bot(), target_profile(), "followers", stop_event=stop_event))

    # Reads which interruption page the scan is standing on
    def test_the_interruption_page_is_named(self, im_module):
        assert im_module.browser_interruption(Mock(url=SUSPENDED_URL))[1] == "a suspended account page"
        assert im_module.browser_interruption(Mock(url=PROFILE_URL)) is None

    # A person clearing the notice in the visible window lets the scan open the profile again and carry on
    def test_a_cleared_page_lets_the_scan_continue(self, im_module, windowed_browser, fake_clock, monkeypatch, tmp_path, capsys):
        page = ScriptedBrowserPage(fake_clock, [SUSPENDED_URL, PROFILE_URL], clears_after_ms=2000)

        assert self.scan(im_module, monkeypatch, tmp_path, page) == [["a", "b"]]

        assert page.visited == [PROFILE_URL, PROFILE_URL]
        assert "Clear it in the browser window, waiting up to 5 seconds" in capsys.readouterr().out
        self.context.close.assert_called_once()

    # A page nobody clears ends the scan with a message that names the fix for an anonymous run
    def test_an_uncleared_page_stops_the_scan(self, im_module, windowed_browser, fake_clock, monkeypatch, tmp_path):
        page = ScriptedBrowserPage(fake_clock, [SUSPENDED_URL])

        with pytest.raises(im_module.BrowserFollowListError, match="suspended account page") as raised:
            self.scan(im_module, monkeypatch, tmp_path, page)

        assert "FOLLOW_LIST_BROWSER_HEADLESS = False" in str(raised.value)
        assert sum(page.waits) >= 5000
        self.context.close.assert_called_once()

    # Clearing the page only to meet another one is reported, not waited on again
    def test_a_second_interruption_is_reported(self, im_module, windowed_browser, fake_clock, monkeypatch, tmp_path):
        page = ScriptedBrowserPage(fake_clock, [SUSPENDED_URL, CHALLENGE_URL], clears_after_ms=2000)

        with pytest.raises(im_module.BrowserFollowListError, match="challenge page"):
            self.scan(im_module, monkeypatch, tmp_path, page)

        assert len(page.visited) == 2

    # A hidden window or a wait of zero cannot be clicked, so the scan stops at once without holding anything open
    @pytest.mark.parametrize("headless, wait", [(True, 5), (False, 0)])
    def test_no_window_means_no_wait(self, im_module, windowed_browser, fake_clock, monkeypatch, tmp_path, headless, wait):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_HEADLESS", headless)
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_LOGIN_WAIT", wait)
        page = ScriptedBrowserPage(fake_clock, [SUSPENDED_URL], clears_after_ms=1000)

        with pytest.raises(im_module.BrowserFollowListError, match="suspended account page"):
            self.scan(im_module, monkeypatch, tmp_path, page)

        assert page.waits == []

    # A session run keeps the guidance it always had, since clearing a page in this window would not refresh its session
    def test_a_session_run_never_waits_for_a_person(self, im_module, windowed_browser, fake_clock, monkeypatch, tmp_path):
        page = ScriptedBrowserPage(fake_clock, [SUSPENDED_URL], clears_after_ms=1000)
        TestBrowserLifecycle.driver_with(monkeypatch, tmp_path, im_module, [], page=page)
        monkeypatch.setattr(im_module, "browser_session_cookies", lambda bot: [])
        bot = SimpleNamespace(context=SimpleNamespace(is_logged_in=True, _session=SimpleNamespace()))

        with pytest.raises(im_module.BrowserFollowListError, match="re-import the session"):
            list(im_module.browser_follow_list_batches(bot, target_profile(), "followers"))

        assert page.waits == []

    # A stop request while waiting ends the scan quietly
    def test_a_stop_request_ends_the_wait(self, im_module, windowed_browser, fake_clock, monkeypatch, tmp_path):
        page = ScriptedBrowserPage(fake_clock, [SUSPENDED_URL])
        stop = threading.Event()
        stop.set()

        assert self.scan(im_module, monkeypatch, tmp_path, page, stop_event=stop) == []

        assert page.waits == []
        self.context.close.assert_called_once()

    # Closing the window while Instagram's page is up is reported as what it is
    def test_a_closed_window_is_reported_as_uncleared(self, im_module, windowed_browser, fake_clock, monkeypatch, tmp_path):
        page = ScriptedBrowserPage(fake_clock, [SUSPENDED_URL])
        page.closes_on_wait = True

        with pytest.raises(im_module.BrowserFollowListError, match="window was closed before it was cleared"):
            self.scan(im_module, monkeypatch, tmp_path, page)


class FakeOpenPage:
    # Reports itself open until it has been waited on the given number of times, like a window a person closes
    def __init__(self, closes_after_waits=None, wait_raises=False):
        self.closes_after_waits = closes_after_waits
        self.wait_raises = wait_raises
        self.waits = 0

    # Reports whether the person has closed the window yet
    def is_closed(self):
        return self.closes_after_waits is not None and self.waits >= self.closes_after_waits

    # Counts the poll, or fails the way a browser that exited under the wait does
    def wait_for_timeout(self, milliseconds):
        if self.wait_raises:
            raise RuntimeError("Target page, context or browser has been closed")
        self.waits += 1


class TestKeepOpen:
    # A browser with no open window left is not held at all
    def test_no_open_window_returns_at_once(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "print_above_progress", lambda message: None)

        im_module.hold_browser_open(SimpleNamespace(pages=[FakeOpenPage(closes_after_waits=0)]))
        im_module.hold_browser_open(SimpleNamespace(pages=[]))

    # An open window is held until the person closes it, and the wait is announced
    def test_an_open_window_is_held_until_it_is_closed(self, im_module, capsys):
        page = FakeOpenPage(closes_after_waits=3)

        im_module.hold_browser_open(SimpleNamespace(pages=[page]))

        assert page.waits == 3
        assert "FOLLOW_LIST_BROWSER_KEEP_OPEN_ON_ERROR is on" in capsys.readouterr().out

    # A browser that exits under the wait ends the hold instead of raising into the error that is already propagating
    def test_a_browser_that_exits_ends_the_hold(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "print_above_progress", lambda message: None)

        im_module.hold_browser_open(SimpleNamespace(pages=[FakeOpenPage(wait_raises=True)]))

    # A stop request ends the hold, so shutting the monitor down never waits on a window
    def test_a_stop_request_ends_the_hold(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "print_above_progress", lambda message: None)
        stop = threading.Event()
        stop.set()
        page = FakeOpenPage()

        im_module.hold_browser_open(SimpleNamespace(pages=[page]), stop_event=stop)

        assert page.waits == 0

    # Runs one scan with the hold stubbed, recording whether it ran and whether it ran before the browser closed
    @staticmethod
    def scan(im_module, monkeypatch, tmp_path, keep_open, headless, fail=False):
        order = []
        context, page = TestBrowserLifecycle.driver_with(monkeypatch, tmp_path, im_module, [SESSION_COOKIE])
        context.close.side_effect = lambda: order.append("close")
        if fail:
            page.goto.side_effect = RuntimeError("the profile page failed to load")
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_KEEP_OPEN_ON_ERROR", keep_open)
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_HEADLESS", headless)
        monkeypatch.setattr(im_module, "hold_browser_open", lambda browser_context, stop_event=None: order.append("hold"))
        monkeypatch.setattr(im_module, "open_browser_follow_list_dialog", Mock())
        try:
            list(im_module.browser_follow_list_batches(anonymous_bot(), target_profile(), "followers"))
        except RuntimeError:
            order.append("raised")
        return order

    # A scan that worked closes the browser and carries on, whatever the setting says
    def test_a_finished_scan_is_closed(self, im_module, monkeypatch, tmp_path):
        assert self.scan(im_module, monkeypatch, tmp_path, keep_open=True, headless=False) == ["close"]

    # A scan that failed is held too, which is the case the setting exists for, and its error still propagates
    def test_a_failed_scan_is_held_open_and_still_fails(self, im_module, monkeypatch, tmp_path):
        assert self.scan(im_module, monkeypatch, tmp_path, keep_open=True, headless=False, fail=True) == ["hold", "close", "raised"]

    # A hidden browser has nothing to look at, so it is closed as usual even after a failure
    def test_a_hidden_browser_is_not_held(self, im_module, monkeypatch, tmp_path):
        assert self.scan(im_module, monkeypatch, tmp_path, keep_open=True, headless=True, fail=True) == ["close", "raised"]

    # Left at its default the setting changes nothing
    def test_the_default_closes_the_browser(self, im_module, monkeypatch, tmp_path):
        assert im_module.FOLLOW_LIST_BROWSER_KEEP_OPEN_ON_ERROR is False
        assert self.scan(im_module, monkeypatch, tmp_path, keep_open=False, headless=False, fail=True) == ["close", "raised"]


class TestPrivateTargets:
    # The lists are viewable on the profile's own say, and an anonymous browser run also attempts a private target
    @pytest.mark.parametrize("source, skip_session, can_view, expected", [
        ("browser", True, False, True), ("browser", True, True, True),
        ("auto", True, False, False), ("graphql", True, False, False), ("rest", True, False, False),
        ("browser", False, False, False), ("auto", False, False, False), ("auto", False, True, True), ("browser", False, True, True),
    ])
    def test_which_targets_may_be_read(self, im_module, monkeypatch, source, skip_session, can_view, expected):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_SOURCE", source)

        assert im_module.follow_lists_viewable(can_view, skip_session) is expected

    # Builds a page stub whose visible text is the given string
    @staticmethod
    def page_saying(text):
        return Mock(evaluate=Mock(return_value=text))

    # A private profile page that says so stops the scan with a specific error
    @pytest.mark.parametrize("text", ["This account is private\nFollow to see their photos and videos.", "this Account is Private", "This profile is private"])
    def test_the_private_banner_is_recognised(self, im_module, text):
        profile = SimpleNamespace(username="target.user", is_private=True)

        with pytest.raises(im_module.BrowserFollowListPrivateError, match="target.user is private and the browser's own account does not follow it"):
            im_module.guard_private_profile(self.page_saying(text), profile, "followers")

    # A private profile the browser's account follows shows no such banner, so the scan goes on after looking for it
    def test_a_followed_private_profile_passes(self, im_module):
        page = self.page_saying("target.user\n12 posts\n135 followers\n447 following")

        im_module.guard_private_profile(page, SimpleNamespace(username="target.user", is_private=True), "followers")

        assert page.evaluate.call_count == im_module.BROWSER_PRIVATE_CHECK_ATTEMPTS

    # The profile renders after the page opens, so a banner that appears on a later read is still found
    def test_a_banner_that_renders_late_is_found(self, im_module):
        page = Mock()
        page.evaluate.side_effect = ["", "target.user", "target.user\nThis profile is private"]

        with pytest.raises(im_module.BrowserFollowListPrivateError):
            im_module.guard_private_profile(page, SimpleNamespace(username="target.user", is_private=True), "following")

        assert page.wait_for_timeout.call_count == 2

    # A public profile is not even looked at, so ordinary scans make no extra call
    def test_a_public_profile_is_not_inspected(self, im_module):
        page = self.page_saying("This account is private")

        im_module.guard_private_profile(page, SimpleNamespace(username="target.user", is_private=False), "followers")
        im_module.guard_private_profile(page, SimpleNamespace(username="target.user"), "followers")

        page.evaluate.assert_not_called()

    # A page that cannot be read leaves the decision to the steps that follow
    def test_an_unreadable_page_does_not_raise(self, im_module):
        page = Mock()
        page.evaluate.side_effect = RuntimeError("Target closed")

        im_module.guard_private_profile(page, SimpleNamespace(username="target.user", is_private=True), "followers")

    # The scan stops on the private banner before it tries to open the list, and still closes the browser
    def test_the_scan_stops_before_opening_the_list(self, im_module, monkeypatch, tmp_path):
        context, page = TestBrowserLifecycle.driver_with(monkeypatch, tmp_path, im_module, [SESSION_COOKIE])
        page.evaluate.return_value = "This account is private"
        opened = Mock()
        monkeypatch.setattr(im_module, "open_browser_follow_list_dialog", opened)
        profile = SimpleNamespace(username="target.user", userid=1, followers=0, followees=296, is_private=True)

        with pytest.raises(im_module.BrowserFollowListPrivateError):
            list(im_module.browser_follow_list_batches(anonymous_bot(), profile, "following"))

        opened.assert_not_called()
        context.close.assert_called_once()

    # The iterator ends quietly on that error, with no shortfall error even though 296 followings were expected
    def test_the_iterator_skips_the_list_without_a_shortfall_error(self, im_module, monkeypatch, capsys):
        # Fails the way a browser whose account cannot see the lists does
        def batches(bot, profile, kind, stop_event=None):
            raise im_module.BrowserFollowListPrivateError("target.user is private and the browser's own account does not follow it, so Instagram does not show it the following")
            yield []

        monkeypatch.setattr(im_module, "browser_follow_list_batches", batches)
        monkeypatch.setattr(im_module, "log_activity", lambda *args, **kwargs: None)

        names = list(im_module.iter_browser_follow_list(anonymous_bot(), target_profile(followees=296), "following"))

        assert names == []
        assert "Its following are skipped" in capsys.readouterr().out


class TestSkipReasonsReachTheScreen:
    # Runs a name fetch that the daily budget refuses, with a real progress bar owning the terminal, and returns what the bar showed
    @staticmethod
    def refused_fetch(im_module, monkeypatch, remaining, exhausted):
        monkeypatch.setattr(im_module, "circuit_breaker_state", lambda: None)
        monkeypatch.setattr(im_module, "_account_breaker_memory_state", lambda: None)
        monkeypatch.setattr(im_module, "verify_exposure_ledger_writable", lambda: None)
        monkeypatch.setattr(im_module, "identity_budget_exhausted", lambda: exhausted)
        monkeypatch.setattr(im_module, "identity_budget_remaining", lambda: remaining)
        monkeypatch.setattr(im_module, "IDENTITY_BUDGET_PER_DAY", 5000)
        monkeypatch.setattr(im_module, "log_activity", lambda *args, **kwargs: None)
        screen = io.StringIO()
        with tqdm(total=4800, file=screen, bar_format="{n_fmt}/{total_fmt}") as bar:
            monkeypatch.setattr(im_module._thread_local, "pbar", bar, raising=False)
            result = im_module.fetch_usernames_paginated(anonymous_bot(), lambda: iter([]), 0, 0, 0, False, 4800, "target")
        return result, screen.getvalue()

    # A scan too big for what is left of the budget says so on the screen, not only in the log, instead of looking like a hang
    def test_a_budget_that_cannot_cover_the_scan_is_explained_on_screen(self, im_module, monkeypatch):
        result, screen = self.refused_fetch(im_module, monkeypatch, remaining=3635, exhausted=False)

        assert result.complete is False
        assert "4800 names are needed but only 3635 of the daily identity budget of 5000 is left" in screen

    # A spent budget is explained the same way
    def test_a_spent_budget_is_explained_on_screen(self, im_module, monkeypatch):
        result, screen = self.refused_fetch(im_module, monkeypatch, remaining=0, exhausted=True)

        assert result.complete is False
        assert "daily identity budget of 5000 is spent" in screen


class TestNameFetchRefusal:
    # Points every check the refusal reads at a healthy state, for a test to spoil one at a time
    @staticmethod
    def healthy(im_module, monkeypatch, remaining=None, exhausted=False):
        monkeypatch.setattr(im_module, "circuit_breaker_state", lambda: None)
        monkeypatch.setattr(im_module, "_account_breaker_memory_state", lambda: None)
        monkeypatch.setattr(im_module, "verify_exposure_ledger_writable", lambda: None)
        monkeypatch.setattr(im_module, "identity_budget_exhausted", lambda: exhausted)
        monkeypatch.setattr(im_module, "identity_budget_remaining", lambda: remaining)
        monkeypatch.setattr(im_module, "IDENTITY_BUDGET_PER_DAY", 5000)

    # With nothing wrong, or with no budget set, or with an unknown size, the fetch may start
    @pytest.mark.parametrize("remaining, limit", [(None, 4800), (5000, 4800), (4800, 4800), (10, 0)])
    def test_a_healthy_state_allows_the_fetch(self, im_module, monkeypatch, remaining, limit):
        self.healthy(im_module, monkeypatch, remaining=remaining)

        assert im_module.name_fetch_refusal(limit) is None

    # A tripped breaker is the first reason, and is shown the way it always was
    def test_a_tripped_breaker_refuses(self, im_module, monkeypatch):
        self.healthy(im_module, monkeypatch)
        monkeypatch.setattr(im_module, "circuit_breaker_state", lambda: {"failure_class": "challenge"})

        refusal = im_module.name_fetch_refusal(100)

        assert "circuit breaker tripped" in refusal.screen and "(challenge)" in refusal.screen and refusal.wrapped is True

    # An unavailable ledger refuses before any budget is read
    def test_an_unavailable_ledger_refuses(self, im_module, monkeypatch):
        self.healthy(im_module, monkeypatch)
        monkeypatch.setattr(im_module, "_account_breaker_memory_state", lambda: {"failure_class": "ledger_unavailable"})

        assert "account safety ledger is unavailable" in im_module.name_fetch_refusal(100).screen

    # A ledger that cannot be read carries its error, so reporting it can mark the account unsafe
    def test_an_unreadable_ledger_refuses_with_its_error(self, im_module, monkeypatch):
        self.healthy(im_module, monkeypatch)
        error = im_module.ExposureLedgerError("cannot read")
        monkeypatch.setattr(im_module, "verify_exposure_ledger_writable", Mock(side_effect=error))

        refusal = im_module.name_fetch_refusal(100)

        assert "cannot be read and saved" in refusal.screen and refusal.ledger_error is error

    # A spent budget and a budget too small for the scan each refuse, with their own wording
    def test_budget_refusals(self, im_module, monkeypatch):
        self.healthy(im_module, monkeypatch, remaining=0, exhausted=True)
        assert "daily identity budget of 5000 is spent" in im_module.name_fetch_refusal(100).screen

        self.healthy(im_module, monkeypatch, remaining=3635)
        refusal = im_module.name_fetch_refusal(4800)

        assert "4800 names are needed but only 3635 of the daily identity budget of 5000 is left" in refusal.screen
        assert refusal.screen == f"* {refusal.log}"

    # The bar is started only for a fetch that will run
    @pytest.mark.parametrize("remaining, started", [(3635, False), (5000, True)])
    def test_the_bar_follows_the_refusal(self, im_module, monkeypatch, remaining, started):
        self.healthy(im_module, monkeypatch, remaining=remaining)
        bar = Mock()
        monkeypatch.setattr(im_module, "setup_pbar", bar)

        im_module.setup_name_fetch_pbar(4800, "* Downloading Followings")

        assert bar.called is started

    # Reporting marks an unreadable ledger, and writes the breaker's line above a bar and into the log
    def test_reporting_marks_the_ledger_and_reaches_the_bar(self, im_module, monkeypatch, capsys):
        marked = []
        logged = []
        monkeypatch.setattr(im_module, "_mark_account_safety_unavailable", marked.append)
        monkeypatch.setattr(im_module, "log_activity", lambda message, **kwargs: logged.append(message))
        error = im_module.ExposureLedgerError("cannot read")
        screen = io.StringIO()
        with tqdm(total=3, file=screen, bar_format="{n_fmt}/{total_fmt}") as bar:
            monkeypatch.setattr(im_module._thread_local, "pbar", bar, raising=False)
            im_module.report_name_fetch_refusal(im_module.NameFetchRefusal("* breaker line", "breaker line", wrapped=True), "target")
            im_module.report_name_fetch_refusal(im_module.NameFetchRefusal("* ledger line", "ledger line", ledger_error=error), "target")

        assert marked == [error]
        assert "* breaker line" in screen.getvalue() and "* ledger line" in screen.getvalue()
        assert logged == ["breaker line", "ledger line"]
