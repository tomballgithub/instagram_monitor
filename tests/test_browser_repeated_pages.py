"""Offline tests for asking again for follower list pages that repeat accounts, and for the page size the browser requests.

Instagram fills part of many pages with accounts it sent earlier in the same list, in the slots where other accounts
belong, so a list read once comes back short. These cover the watcher that notices such pages and asks for them again,
the rewrite that raises the page size, and how both are wired into the browser scan.
"""

import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

FOLLOWERS_URL = "https://www.instagram.com/api/v1/friendships/4242/followers/"
CURSOR_URL = FOLLOWERS_URL + "?count=25&max_id=25"


class FakeResponse:
    # Stands in for a list response with the usernames it carries, or for one whose body is not JSON
    def __init__(self, url, names=None, body="unset"):
        self.url = url
        self.body = {"users": [{"username": name} for name in names]} if body == "unset" else body

    # Returns the body like Playwright does, or fails when it was not JSON
    def json(self):
        if self.body is None:
            raise ValueError("not json")
        return self.body


class FakeRefetchPage:
    # Answers the in-page request with the scripted results in order, and records every pause and path asked for
    def __init__(self, results, on_request=None):
        self.results = list(results)
        self.paths = []
        self.waits = []
        self.on_request = on_request

    # Pauses are recorded instead of slept
    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)

    # Returns the next scripted result, or raises when the script holds an exception
    def evaluate(self, script, args=None):
        self.paths.append(args[0])
        if self.on_request:
            self.on_request()
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


# Builds the answer the in-page request returns for a page of accounts
def page_of(*names):
    return {"status": 200, "retryAfter": None, "users": list(names)}


@pytest.fixture
# Makes asking again instant and silent, and keeps the activity log out of the way
def quick_watcher(im_module, monkeypatch):
    monkeypatch.setattr(im_module, "BROWSER_REFETCH_DELAY_MS", 10)
    monkeypatch.setattr(im_module, "log_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(im_module, "print_above_progress", lambda message: None)


class TestWatcher:
    # Names from pages that repeat nothing are reported once, in the order they arrived, with no request made
    def test_clean_pages_are_read_without_asking_again(self, im_module, quick_watcher):
        watcher = im_module.BrowserFollowListWatcher("followers", 3)
        page = FakeRefetchPage([])
        watcher.on_response(FakeResponse(FOLLOWERS_URL + "?count=25", ["a", "b"]))
        watcher.on_response(FakeResponse(CURSOR_URL, ["c", "d"]))

        assert watcher.collect(page, set()) == ["a", "b", "c", "d"]
        assert page.paths == [] and watcher.requests == 0
        assert watcher.collect(page, set()) == []

    # A page that carries a name seen before is asked for again at the same address, and the new names are kept
    def test_a_repeating_page_is_asked_for_again(self, im_module, quick_watcher):
        watcher = im_module.BrowserFollowListWatcher("followers", 1)
        page = FakeRefetchPage([page_of("c", "x", "y")])
        watcher.on_response(FakeResponse(FOLLOWERS_URL + "?count=25", ["a", "b"]))
        watcher.on_response(FakeResponse(CURSOR_URL, ["c", "a"]))

        assert watcher.collect(page, set()) == ["a", "b", "c", "x", "y"]
        assert page.paths == ["/api/v1/friendships/4242/followers/?count=25&max_id=25"]
        assert (watcher.pages, watcher.requests, watcher.recovered) == (1, 1, 2)

    # Every configured try is made, since each one returns a different mix and any of them may fill a slot
    def test_every_configured_try_is_made(self, im_module, quick_watcher):
        watcher = im_module.BrowserFollowListWatcher("followers", 3)
        page = FakeRefetchPage([page_of("x"), page_of("x"), page_of("x", "y")])
        watcher.on_response(FakeResponse(FOLLOWERS_URL + "?count=25", ["a", "a"]))

        assert watcher.collect(page, set()) == ["a", "x", "y"]
        assert watcher.requests == 3 and watcher.recovered == 2
        assert all(wait >= 10 for wait in page.waits) and len(page.waits) == 3

    # A page of accounts nobody has seen is never asked for again
    def test_new_accounts_alone_do_not_trigger_a_request(self, im_module, quick_watcher):
        watcher = im_module.BrowserFollowListWatcher("followers", 3)
        page = FakeRefetchPage([])
        watcher.on_response(FakeResponse(FOLLOWERS_URL, ["a", "b"]))
        watcher.on_response(FakeResponse(CURSOR_URL, ["c", "d"]))
        watcher.collect(page, set())

        assert watcher.pages == 0

    # With no tries a repeat is read but nothing is asked for
    def test_zero_tries_never_asks_again(self, im_module, quick_watcher):
        watcher = im_module.BrowserFollowListWatcher("followers", 0)
        page = FakeRefetchPage([])
        watcher.on_response(FakeResponse(FOLLOWERS_URL, ["a", "a"]))

        assert watcher.collect(page, set()) == ["a"]
        assert page.paths == []

    # Names the dialog already rendered are not reported again
    def test_names_already_seen_are_not_reported(self, im_module, quick_watcher):
        watcher = im_module.BrowserFollowListWatcher("followers", 1)
        watcher.on_response(FakeResponse(FOLLOWERS_URL, ["a", "b", "c"]))

        assert watcher.collect(FakeRefetchPage([]), {"a", "c"}) == ["b"]

    # The answer to the watcher's own request is not read as a page of the dialog, or it would ask again about itself
    def test_its_own_request_is_not_read_as_a_page(self, im_module, quick_watcher):
        watcher = im_module.BrowserFollowListWatcher("followers", 1)
        watcher.on_response(FakeResponse(CURSOR_URL, ["a", "a"]))
        # The browser reports the response to the in-page request while that request is still running
        page = FakeRefetchPage([page_of("x")], on_request=lambda: watcher.on_response(FakeResponse(CURSOR_URL, ["a", "x"])))

        assert watcher.collect(page, set()) == ["a", "x"]
        assert watcher.pending == [] and watcher.requests == 1

    # Only the list the scan is reading is watched
    @pytest.mark.parametrize("url", [FOLLOWERS_URL.replace("followers", "following"), "https://www.instagram.com/api/v1/users/web_profile_info/", "https://www.instagram.com/target.user/"])
    def test_other_requests_are_ignored(self, im_module, url):
        watcher = im_module.BrowserFollowListWatcher("followers", 1)

        watcher.on_response(FakeResponse(url, ["a"]))

        assert watcher.pending == []

    # A body that is not a list of accounts is skipped without failing the scan
    @pytest.mark.parametrize("body", [None, {"status": "ok"}, {"users": "no"}, []])
    def test_unusable_bodies_are_skipped(self, im_module, quick_watcher, body):
        watcher = im_module.BrowserFollowListWatcher("followers", 1)
        watcher.on_response(FakeResponse(FOLLOWERS_URL, body=body))

        assert watcher.collect(FakeRefetchPage([]), set()) == []

    # Instagram answering with anything but a page of accounts ends all asking again, and the reason is kept
    @pytest.mark.parametrize("result, reason", [
        ({"status": 429, "retryAfter": "120", "users": None}, "HTTP 429, asking for a 120 second wait"),
        ({"status": 400, "retryAfter": None, "users": None}, "HTTP 400"),
        ({"status": 200, "retryAfter": None, "users": None}, "something other than a page of accounts"),
        (None, "something other than a page of accounts"),
        (RuntimeError("Target closed"), "a request failed"),
    ])
    def test_a_refusal_stops_all_further_requests(self, im_module, quick_watcher, result, reason):
        watcher = im_module.BrowserFollowListWatcher("followers", 3)
        page = FakeRefetchPage([result])
        watcher.on_response(FakeResponse(FOLLOWERS_URL, ["a", "a"]))
        watcher.on_response(FakeResponse(CURSOR_URL, ["b", "b"]))

        assert watcher.collect(page, set()) == ["a", "b"]
        assert reason in watcher.stopped
        assert len(page.paths) == 1

    # A scan makes at most the allowed number of extra requests, however many pages repeat
    def test_the_request_limit_is_enforced(self, im_module, quick_watcher, monkeypatch):
        monkeypatch.setattr(im_module, "BROWSER_REFETCH_MAX_REQUESTS", 2)
        watcher = im_module.BrowserFollowListWatcher("followers", 3)
        page = FakeRefetchPage([page_of("x"), page_of("y"), page_of("z")])
        watcher.on_response(FakeResponse(FOLLOWERS_URL, ["a", "a"]))
        watcher.on_response(FakeResponse(CURSOR_URL, ["b", "b"]))

        watcher.collect(page, set())

        assert watcher.requests == 2 and "limit of 2 extra requests" in watcher.stopped

    # What was done is reported once, and a scan that asked for nothing says nothing
    def test_the_report_states_the_outcome(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "BROWSER_REFETCH_DELAY_MS", 10)
        lines = []
        monkeypatch.setattr(im_module, "print_above_progress", lines.append)
        monkeypatch.setattr(im_module, "log_activity", lambda *args, **kwargs: None)
        watcher = im_module.BrowserFollowListWatcher("followers", 1)
        profile = SimpleNamespace(username="target.user")

        watcher.report(profile)
        assert lines == []

        watcher.on_response(FakeResponse(FOLLOWERS_URL, ["a", "a"]))
        watcher.collect(FakeRefetchPage([page_of("x")]), set())
        watcher.report(profile)

        assert lines == ["* Asked again for 1 page(s) that repeated accounts: 1 request(s), 1 account(s) recovered"]


class TestHarvestWithWatcher:
    # Stands in for the dialog, rendering the given names and answering the scroll script
    class Dialog:
        # Takes the rows the dialog shows on each read
        def __init__(self, frames):
            self.frames = list(frames)
            self.refetches = []

        # Answers the names script with the next frame and the scroll script with success
        def evaluate(self, script, args=None):
            if args is not None:
                self.refetches.append(args[0])
                return {"status": 200, "retryAfter": None, "users": ["lost1", "lost2"]}
            if "scrollTop" in script:
                return True
            return self.frames.pop(0) if self.frames else []

        # Pauses are not slept
        def wait_for_timeout(self, milliseconds):
            return None

    # The names the responses carry that the rows never showed are added to what the dialog rendered
    def test_response_names_join_the_rendered_rows(self, im_module, quick_watcher):
        dialog = self.Dialog([["a", "b"], ["a", "b", "c"]])
        watcher = im_module.BrowserFollowListWatcher("followers", 1)
        watcher.on_response(FakeResponse(FOLLOWERS_URL, ["a", "b", "hidden"]))

        batches = list(im_module.harvest_follow_list_dialog(dialog, 0, stall_limit=2, watcher=watcher))

        names = [name for batch in batches for name in batch]
        assert sorted(names) == ["a", "b", "c", "hidden"]
        assert len(names) == len(set(names))

    # A page that repeated accounts is asked for again while the dialog scrolls, and what comes back is yielded
    def test_recovered_names_are_yielded(self, im_module, quick_watcher):
        dialog = self.Dialog([["a", "b"], ["a", "b"]])
        watcher = im_module.BrowserFollowListWatcher("followers", 1)
        watcher.on_response(FakeResponse(FOLLOWERS_URL, ["a", "b", "a"]))

        names = [name for batch in im_module.harvest_follow_list_dialog(dialog, 0, stall_limit=2, watcher=watcher) for name in batch]

        assert sorted(names) == ["a", "b", "lost1", "lost2"]
        assert dialog.refetches == ["/api/v1/friendships/4242/followers/"]

    # A response that arrives while the scan is ending is still read before it returns
    def test_a_late_response_is_not_lost(self, im_module, quick_watcher):
        dialog = self.Dialog([["a"]])
        watcher = im_module.BrowserFollowListWatcher("followers", 1)
        original = dialog.evaluate

        # Delivers one more page as the final scroll is evaluated, the way a response lands after the last look
        def evaluate(script, args=None):
            if "scrollTop" in script and not watcher.pending and not watcher.known:
                return original(script, args)
            if "scrollTop" in script:
                watcher.on_response(FakeResponse(CURSOR_URL, ["late"]))
                return False
            return original(script, args)

        dialog.evaluate = evaluate
        watcher.on_response(FakeResponse(FOLLOWERS_URL, ["a"]))

        names = [name for batch in im_module.harvest_follow_list_dialog(dialog, 0, stall_limit=3, watcher=watcher) for name in batch]

        assert "late" in names

    # Without a watcher the harvest reads only the rows, exactly as before
    def test_no_watcher_changes_nothing(self, im_module):
        dialog = self.Dialog([["a", "b"], ["a", "b"]])

        batches = list(im_module.harvest_follow_list_dialog(dialog, 0, stall_limit=2))

        assert batches == [["a", "b"]]


class FakeRoute:
    # Stands in for one intercepted request
    def __init__(self, url, method="GET"):
        self.request = SimpleNamespace(url=url, method=method)
        self.calls = []

    # Records how the request was continued
    def continue_(self, url=None):
        self.calls.append(url)


class TestPageSize:
    # Installs the rewrite on a fake context and returns the pattern and handler it registered
    @staticmethod
    def installed(im_module, kind="followers", size=25):
        context = Mock()
        im_module.install_browser_page_size(context, kind, size)
        return context

    # A request asking for the dialog's 12 is sent on asking for 25, with every other parameter kept in place
    def test_the_count_is_raised(self, im_module):
        context = self.installed(im_module)
        _pattern, handler = context.route.call_args.args
        route = FakeRoute(FOLLOWERS_URL + "?count=12&max_id=24&search_surface=follow_list_page")

        handler(route)

        assert route.calls == [FOLLOWERS_URL + "?count=25&max_id=24&search_surface=follow_list_page"]

    # A request that names no count is given one
    def test_a_missing_count_is_added(self, im_module):
        _pattern, handler = self.installed(im_module).route.call_args.args
        route = FakeRoute(FOLLOWERS_URL + "?max_id=24")

        handler(route)

        assert route.calls == [FOLLOWERS_URL + "?max_id=24&count=25"]

    # A request already asking for as much, or a different method, is left exactly as it was
    @pytest.mark.parametrize("url, method", [(FOLLOWERS_URL + "?count=25", "GET"), (FOLLOWERS_URL + "?count=50", "GET"), (FOLLOWERS_URL + "?count=12", "POST")])
    def test_other_requests_pass_through(self, im_module, url, method):
        _pattern, handler = self.installed(im_module).route.call_args.args
        route = FakeRoute(url, method)

        handler(route)

        assert route.calls == [None]

    # An address that cannot be read is still sent on rather than left hanging
    def test_an_unreadable_request_still_continues(self, im_module):
        _pattern, handler = self.installed(im_module).route.call_args.args
        route = FakeRoute(FOLLOWERS_URL + "?count=lots")

        handler(route)

        assert route.calls == [None]

    # Only the list being read is touched
    def test_only_the_watched_list_matches(self, im_module):
        pattern, _handler = self.installed(im_module, kind="following").route.call_args.args

        assert pattern.search(FOLLOWERS_URL.replace("followers", "following"))
        assert not pattern.search(FOLLOWERS_URL)
        assert not pattern.search("https://www.instagram.com/api/v1/users/web_profile_info/")

    # A page size of zero leaves the dialog's own requests alone
    def test_zero_installs_nothing(self, im_module):
        context = Mock()

        im_module.install_browser_page_size(context, "followers", 0)

        context.route.assert_not_called()


class TestScanWiring:
    # Builds a mocked Playwright driver for a signed-in session scan, returning the context and page it hands out
    @staticmethod
    def driver(monkeypatch, tmp_path, im_module):
        driver = MagicMock()
        driver.__enter__.return_value = driver
        context = driver.chromium.launch_persistent_context.return_value
        page = Mock(url="https://www.instagram.com/target.user/")
        context.pages = [page]
        monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=lambda: driver))
        monkeypatch.setattr(im_module, "playwright_available", lambda: True)
        monkeypatch.setattr(im_module, "browser_profile_dir", lambda: str(tmp_path / "browser-profile"))
        monkeypatch.setattr(im_module, "browser_session_cookies", lambda bot: [])
        monkeypatch.setattr(im_module, "browser_follow_list_progress", Mock())
        monkeypatch.setattr(im_module, "open_browser_follow_list_dialog", Mock())
        monkeypatch.setattr(im_module, "log_activity", lambda *args, **kwargs: None)
        monkeypatch.setattr(im_module, "harvest_follow_list_dialog", lambda *args, **kwargs: iter([["a"]]))
        return context, page

    # Runs one scan as a logged-in session
    @staticmethod
    def scan(im_module):
        bot = SimpleNamespace(context=SimpleNamespace(is_logged_in=True, _session=SimpleNamespace()))
        return list(im_module.browser_follow_list_batches(bot, SimpleNamespace(username="target.user", userid=1, followers=1, followees=0), "followers"))

    # With the defaults the page size is raised and the list responses are watched before the dialog opens
    def test_the_defaults_enable_both(self, im_module, monkeypatch, tmp_path):
        context, page = self.driver(monkeypatch, tmp_path, im_module)

        assert self.scan(im_module) == [["a"]]

        assert im_module.FOLLOW_LIST_BROWSER_PAGE_SIZE == 25 and im_module.FOLLOW_LIST_BROWSER_REFETCH_TRIES == 3
        assert context.route.call_count == 1
        assert page.on.call_args.args[0] == "response"
        assert isinstance(page.on.call_args.args[1].__self__, im_module.BrowserFollowListWatcher)

    # The harvest is handed the watcher, which is what lets the recovered names reach the caller
    def test_the_harvest_receives_the_watcher(self, im_module, monkeypatch, tmp_path):
        self.driver(monkeypatch, tmp_path, im_module)
        harvest = Mock(return_value=iter([["a"]]))
        monkeypatch.setattr(im_module, "harvest_follow_list_dialog", harvest)

        self.scan(im_module)

        assert isinstance(harvest.call_args.kwargs["watcher"], im_module.BrowserFollowListWatcher)

    # Each setting turns its half off on its own
    @pytest.mark.parametrize("size, tries, routed, watched", [(0, 3, False, True), (25, 0, True, False), (0, 0, False, False)])
    def test_each_setting_works_alone(self, im_module, monkeypatch, tmp_path, size, tries, routed, watched):
        context, page = self.driver(monkeypatch, tmp_path, im_module)
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_PAGE_SIZE", size)
        monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_REFETCH_TRIES", tries)
        harvest = Mock(return_value=iter([["a"]]))
        monkeypatch.setattr(im_module, "harvest_follow_list_dialog", harvest)

        self.scan(im_module)

        assert (context.route.call_count == 1) is routed
        assert (page.on.call_count == 1) is watched
        assert (harvest.call_args.kwargs["watcher"] is not None) is watched


class TestSettings:
    # The shipped template offers both settings with the values the feature was tuned with
    def test_the_template_ships_both_settings(self, im_module):
        parsed = im_module.parse_config_content(im_module.CONFIG_BLOCK, "<built-in>")

        assert parsed["FOLLOW_LIST_BROWSER_PAGE_SIZE"] == 25
        assert parsed["FOLLOW_LIST_BROWSER_REFETCH_TRIES"] == 3

    # Both are whole numbers of zero or more, so anything else is reported at startup like the other counts
    @pytest.mark.parametrize("name", ["FOLLOW_LIST_BROWSER_PAGE_SIZE", "FOLLOW_LIST_BROWSER_REFETCH_TRIES"])
    @pytest.mark.parametrize("value, rejected", [(0, False), (25, False), (3, False), (-1, True), (2.5, True), ("many", True), (True, True), (None, True)])
    def test_a_bad_value_is_rejected(self, im_module, monkeypatch, name, value, rejected):
        monkeypatch.setattr(im_module, name, value)

        assert (name in im_module.runtime_configuration_problems()) is rejected

    # A scan stays out of the way of a stop request while it waits between requests
    def test_the_watcher_needs_no_thread_of_its_own(self, im_module):
        watcher = im_module.BrowserFollowListWatcher("followers", 3)

        assert not any(isinstance(value, threading.Thread) for value in vars(watcher).values())


# Builds a list response that carries a cursor for a next page, or none when it is the last
def paged(url, names, cursor):
    return FakeResponse(url, body={"users": [{"username": name} for name in names], "next_max_id": cursor})


@pytest.fixture
# Replaces the monotonic clock with the real one plus an offset a test moves forward
def clock(im_module, monkeypatch):
    import time as real_time

    offset = [0.0]
    real = real_time.monotonic
    monkeypatch.setattr(im_module.time, "monotonic", lambda: real() + offset[0])
    return offset


class TestEndOfList:
    # A page with a cursor does not end the list, and the last page, which has none, does
    def test_the_last_page_marks_the_end(self, im_module, quick_watcher):
        watcher = im_module.BrowserFollowListWatcher("followers", 0)
        watcher.on_response(paged(FOLLOWERS_URL, ["a"], "25"))
        watcher.collect(FakeRefetchPage([]), set())
        assert watcher.ended is False

        watcher.on_response(paged(CURSOR_URL, ["b"], None))
        watcher.collect(FakeRefetchPage([]), set())

        assert watcher.ended is True

    # Waiting makes sense only after pages have arrived and until the end is marked or the patience runs out
    def test_when_the_watcher_is_waiting_for_more(self, im_module, quick_watcher, clock, monkeypatch):
        monkeypatch.setattr(im_module, "BROWSER_NO_END_PATIENCE_SECONDS", 30)
        watcher = im_module.BrowserFollowListWatcher("followers", 0)
        assert watcher.waiting_for_more() is False

        watcher.on_response(paged(FOLLOWERS_URL, ["a"], "25"))
        assert watcher.waiting_for_more() is True

        clock[0] += 29
        assert watcher.waiting_for_more() is True
        clock[0] += 2
        assert watcher.waiting_for_more() is False

        watcher.on_response(paged(CURSOR_URL, ["b"], "50"))
        assert watcher.waiting_for_more() is True
        watcher.collect(FakeRefetchPage([]), set())
        watcher.on_response(paged(CURSOR_URL, ["c"], None))
        watcher.collect(FakeRefetchPage([]), set())
        assert watcher.waiting_for_more() is False

    # The watcher's own requests are not pages of the dialog, so they neither count nor restart the patience
    def test_its_own_requests_do_not_restart_the_patience(self, im_module, quick_watcher, clock, monkeypatch):
        monkeypatch.setattr(im_module, "BROWSER_NO_END_PATIENCE_SECONDS", 30)
        watcher = im_module.BrowserFollowListWatcher("followers", 1)
        watcher.on_response(paged(CURSOR_URL, ["a", "a"], "50"))
        clock[0] += 40

        # The response to the in-page request arrives while that request runs
        page = FakeRefetchPage([page_of("x")], on_request=lambda: watcher.on_response(paged(CURSOR_URL, ["x"], "50")))
        watcher.collect(page, set())

        assert watcher.responses == 1
        assert watcher.waiting_for_more() is False

    # The report says when reading stopped without the end marker, and stays quiet for a list that ended
    def test_the_report_flags_a_list_that_did_not_end(self, im_module, monkeypatch):
        lines = []
        monkeypatch.setattr(im_module, "print_above_progress", lines.append)
        monkeypatch.setattr(im_module, "log_activity", lambda *args, **kwargs: None)
        profile = SimpleNamespace(username="target.user")
        watcher = im_module.BrowserFollowListWatcher("followers", 0)
        watcher.on_response(paged(FOLLOWERS_URL, ["a"], "25"))

        watcher.report(profile)

        assert len(lines) == 1 and "had not marked the end of the list" in lines[0] and "may be incomplete" in lines[0]

        lines.clear()
        watcher.on_response(paged(CURSOR_URL, ["b"], None))
        watcher.collect(FakeRefetchPage([]), set())
        watcher.report(profile)

        assert lines == []


class TestHarvestPatience:
    # Stands in for a dialog that renders nothing new, moving the clock on every wait and counting the nudges
    class QuietDialog:
        # Takes the clock to move and an optional hook run on a numbered wait
        def __init__(self, clock, on_wait=None):
            self.clock = clock
            self.on_wait = on_wait
            self.waits = 0
            self.reads = 0
            self.nudges = 0

        # Answers the names script with nothing, and the scroll and nudge scripts with success
        def evaluate(self, script, args=None):
            if "Math.max(0, target.scrollTop" in script:
                self.nudges += 1
                return True
            if "scrollTop" in script:
                return True
            self.reads += 1
            return []

        # Moves the clock by the pause and runs the hook
        def wait_for_timeout(self, milliseconds):
            self.waits += 1
            self.clock[0] += milliseconds / 1000
            if self.on_wait:
                self.on_wait(self.waits)

    # With no page having arrived the scan ends after its quiet-scroll limit, exactly as it did before
    def test_without_pages_the_quiet_limit_ends_the_scan(self, im_module, quick_watcher, clock):
        dialog = self.QuietDialog(clock)
        watcher = im_module.BrowserFollowListWatcher("followers", 0)

        list(im_module.harvest_follow_list_dialog(dialog, 1, stall_limit=3, watcher=watcher))

        assert dialog.reads == 3 and dialog.nudges == 0

    # While Instagram has not marked the end of the list the scan waits past the quiet limit, nudging, until the patience runs out
    def test_an_unfinished_list_keeps_the_scan_waiting(self, im_module, quick_watcher, clock, monkeypatch):
        monkeypatch.setattr(im_module, "BROWSER_NO_END_PATIENCE_SECONDS", 10)
        dialog = self.QuietDialog(clock)
        watcher = im_module.BrowserFollowListWatcher("followers", 0)
        watcher.on_response(paged(FOLLOWERS_URL, [], "25"))

        list(im_module.harvest_follow_list_dialog(dialog, 1, stall_limit=3, watcher=watcher))

        assert dialog.reads > 3 and dialog.nudges > 0
        assert 9 <= dialog.waits <= 13

    # A list Instagram has ended stops at the quiet limit with no waiting
    def test_a_finished_list_stops_at_the_quiet_limit(self, im_module, quick_watcher, clock):
        dialog = self.QuietDialog(clock)
        watcher = im_module.BrowserFollowListWatcher("followers", 0)
        watcher.on_response(paged(FOLLOWERS_URL, [], None))

        list(im_module.harvest_follow_list_dialog(dialog, 1, stall_limit=3, watcher=watcher))

        assert dialog.reads == 3 and dialog.nudges == 0

    # A page that arrives long after the quiet limit is still read, which is the case that used to cut a list short
    def test_a_slow_page_is_still_read(self, im_module, quick_watcher, clock, monkeypatch):
        monkeypatch.setattr(im_module, "BROWSER_NO_END_PATIENCE_SECONDS", 60)
        watcher = im_module.BrowserFollowListWatcher("followers", 0)
        watcher.on_response(paged(FOLLOWERS_URL, ["a"], "25"))

        # Delivers the last page twenty waits in, far beyond the three quiet scrolls the scan used to allow
        def deliver(waits):
            if waits == 20:
                watcher.on_response(paged(CURSOR_URL, ["b", "c"], None))

        dialog = self.QuietDialog(clock, on_wait=deliver)

        names = [name for batch in im_module.harvest_follow_list_dialog(dialog, 1, stall_limit=3, watcher=watcher) for name in batch]

        assert sorted(names) == ["a", "b", "c"]
        assert dialog.waits < 30
