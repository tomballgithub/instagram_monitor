"""Tests for --open-browser, which opens the window of the browser the follower list source uses."""

import sys
import types

import pytest

import instagram_monitor as im


# Stands in for one browser window that a person closes after it has been polled a given number of times
class FakePage:
    def __init__(self, load_error=None, closes_after_waits=1):
        self.load_error = load_error
        self.closes_after_waits = closes_after_waits
        self.waits = 0
        self.visited = []

    # Records the address, or fails the way an unreachable proxy does
    def goto(self, address, **kwargs):
        self.visited.append(address)
        if self.load_error:
            raise self.load_error

    # Reports whether the person has closed the window yet
    def is_closed(self):
        return self.waits >= self.closes_after_waits

    # Counts one poll of the open window
    def wait_for_timeout(self, milliseconds):
        self.waits += 1


# Stands in for the persistent browser context a launch returns
class FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False

    # Records that the browser was shut down
    def close(self):
        self.closed = True


# Installs a fake Playwright driver and returns what it was asked to launch
def _install_playwright(monkeypatch, page, launch_error=None):
    launches = []
    context = FakeContext(page)

    class Chromium:
        # Records the profile and options, or fails the way a profile that is already in use does
        def launch_persistent_context(self, user_data_dir, **options):
            launches.append((user_data_dir, options))
            if launch_error:
                raise launch_error
            return context

    class Driver:
        chromium = Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: Driver()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return launches, context


@pytest.fixture(autouse=True)
# Points the profile at a throwaway folder and makes the optional package look installed
def browser_settings(im_module, monkeypatch, tmp_path):
    monkeypatch.setattr(im_module, "playwright_available", lambda: True)
    monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_PROFILE_DIR", str(tmp_path / "profiles"))
    monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_HEADLESS", True)
    monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_CHANNEL", "chromium")
    monkeypatch.setattr(im_module, "SESSION_USERNAME", "")
    monkeypatch.setattr(im_module, "PROXY_ENABLED", False)


class TestOpenBrowserWindow:
    # The window opens on the profile the scans use, visible even when scans are hidden, and shows Instagram's home page
    def test_it_opens_the_scan_profile_in_a_visible_window(self, im_module, monkeypatch, tmp_path, capsys):
        page = FakePage()
        launches, context = _install_playwright(monkeypatch, page)

        assert im_module.open_browser_window() == 0

        profile, options = launches[0]
        assert profile == im_module.browser_profile_dir()
        assert profile.endswith("_anonymous_")
        assert options["headless"] is False and options["channel"] == "chromium"
        assert page.visited == ["https://www.instagram.com/"]
        assert context.closed is True
        assert "Close the browser window to finish" in capsys.readouterr().out

    # A target opens its own profile page
    def test_a_target_opens_its_profile(self, im_module, monkeypatch):
        page = FakePage()
        _install_playwright(monkeypatch, page)

        assert im_module.open_browser_window("Target.Name") == 0

        assert page.visited == ["https://www.instagram.com/target.name/"]

    # The window uses the same proxy and user agent as a scan, so Instagram sees the same client
    def test_it_uses_the_scan_proxy_and_user_agent(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "USER_AGENT", "Agent/1.0")
        monkeypatch.setattr(im_module, "browser_proxy_settings", lambda: {"server": "http://proxy.example:8080"})
        launches, _context = _install_playwright(monkeypatch, FakePage())

        im_module.open_browser_window()

        assert launches[0][1]["user_agent"] == "Agent/1.0"
        assert launches[0][1]["proxy"] == {"server": "http://proxy.example:8080"}

    # A session account has its own profile, apart from the anonymous one
    def test_a_session_account_has_its_own_profile(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "SESSION_USERNAME", "some.account")
        monkeypatch.setattr(im_module, "SKIP_SESSION", False)
        launches, _context = _install_playwright(monkeypatch, FakePage())

        im_module.open_browser_window()

        assert launches[0][0].endswith("some.account")

    # A profile that cannot be freed is reported before any launch is tried, with the reason and what to do about it
    def test_a_profile_that_cannot_be_freed_is_reported_without_a_launch(self, im_module, monkeypatch, capsys):
        launches, context = _install_playwright(monkeypatch, FakePage())
        monkeypatch.setattr(im_module, "release_browser_profile", lambda profile_dir, stop_event=None: "a scan, or another run of this tool, is using it right now")

        assert im_module.open_browser_window() == 1

        output = capsys.readouterr().out
        assert "is open in a browser window and could not be freed (a scan, or another run of this tool, is using it right now)" in output
        assert launches == [] and context.closed is False

    # A browser opened between the check and the launch fails the launch, and gets the same answer
    def test_a_profile_opened_during_the_launch_gets_the_same_answer(self, im_module, monkeypatch, capsys):
        launches, _context = _install_playwright(monkeypatch, FakePage(), launch_error=RuntimeError("Target page, context or browser has been closed"))
        monkeypatch.setattr(im_module, "release_browser_profile", lambda profile_dir, stop_event=None: None)
        monkeypatch.setattr(im_module, "browser_profile_in_use", lambda profile_dir: True)

        assert im_module.open_browser_window() == 1

        assert len(launches) == 1
        assert "it was opened while the browser was starting" in capsys.readouterr().out

    # A launch that fails with the profile free is reported as the browser failing to start
    def test_a_launch_failure_with_a_free_profile_is_reported(self, im_module, monkeypatch, capsys):
        _launches, _context = _install_playwright(monkeypatch, FakePage(), launch_error=RuntimeError("Executable doesn't exist"))

        assert im_module.open_browser_window() == 1

        output = capsys.readouterr().out
        assert "The browser could not start: RuntimeError: Executable" in output and "playwright install chromium" in output

    # A page that does not load leaves the window open, since a broken proxy or a sign-in is fixed there
    def test_a_page_that_does_not_load_keeps_the_window(self, im_module, monkeypatch, capsys):
        page = FakePage(load_error=RuntimeError("net::ERR_PROXY_CONNECTION_FAILED"), closes_after_waits=2)
        _launches, context = _install_playwright(monkeypatch, page)

        assert im_module.open_browser_window() == 0

        assert "did not load" in capsys.readouterr().out
        assert page.waits == 2 and context.closed is True

    # Without the optional package the command says how to install it
    def test_a_missing_package_is_reported(self, im_module, monkeypatch, capsys):
        monkeypatch.setattr(im_module, "playwright_available", lambda: False)

        assert im_module.open_browser_window() == 1

        assert "pip install playwright" in capsys.readouterr().out

    # A stop request ends the wait, so shutting the monitor down never waits on a window
    def test_a_stop_request_ends_the_wait(self, im_module, monkeypatch):
        import threading

        stop = threading.Event()
        stop.set()
        page = FakePage(closes_after_waits=100)
        _launches, context = _install_playwright(monkeypatch, page)

        assert im_module.open_browser_window(stop_event=stop) == 0

        assert page.waits == 0 and context.closed is True


class TestBrowserProfileInUse:
    # A profile folder that was never opened is free
    def test_an_empty_profile_is_free(self, im_module, tmp_path):
        assert im_module.browser_profile_in_use(str(tmp_path)) is False

    # On Windows a lockfile that can still be opened for writing is left over from a browser that has exited
    @pytest.mark.skipif(sys.platform != "win32", reason="Chrome keeps its lockfile open on Windows only")
    def test_a_stale_lockfile_is_free(self, im_module, tmp_path):
        (tmp_path / "lockfile").write_bytes(b"")

        assert im_module.browser_profile_in_use(str(tmp_path)) is False

    # On Windows a lockfile that cannot be opened is held by a running browser
    @pytest.mark.skipif(sys.platform != "win32", reason="Chrome keeps its lockfile open on Windows only")
    def test_a_held_lockfile_is_in_use(self, im_module, tmp_path):
        import ctypes
        from ctypes import wintypes

        lockfile = tmp_path / "lockfile"
        lockfile.write_bytes(b"")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        # Opened for reading and writing with no sharing, the way Chrome holds its lockfile while it runs
        handle = kernel32.CreateFileW(str(lockfile), 0xC0000000, 0, None, 3, 0x80, None)
        assert handle not in (None, wintypes.HANDLE(-1).value)
        try:
            assert im_module.browser_profile_in_use(str(tmp_path)) is True
        finally:
            kernel32.CloseHandle(handle)
        assert im_module.browser_profile_in_use(str(tmp_path)) is False

    # Elsewhere the SingletonLock link names the owner, which counts only while that process is alive
    @pytest.mark.skipif(sys.platform == "win32", reason="the link is a Unix feature")
    def test_a_singleton_lock_counts_while_its_process_lives(self, im_module, tmp_path):
        import os

        link = tmp_path / "SingletonLock"
        os.symlink(f"host-{os.getpid()}", link)
        assert im_module.browser_profile_in_use(str(tmp_path)) is True
        link.unlink()
        os.symlink("host-999999999", link)
        assert im_module.browser_profile_in_use(str(tmp_path)) is False


class TestReleaseBrowserProfile:
    PROFILE = "C:\\Python Dev\\monitor\\profiles\\_anonymous_"
    HAND = f'"C:\\Chrome\\chrome.exe" --user-data-dir="{PROFILE}" about:blank'
    HELPER = f'"C:\\Chrome\\chrome.exe" --type=renderer --user-data-dir="{PROFILE}"'
    DRIVEN = f'"C:\\Chrome\\chrome.exe" --enable-automation --user-data-dir={PROFILE} --remote-debugging-pipe about:blank'

    # Stands in for the machine: the processes it lists, a clock that only moves when the code sleeps, and a profile that stays
    # in use until a close request of the kind the test allows. Returns what happened, in order
    @staticmethod
    def _machine(im_module, monkeypatch, table, frees_on=("graceful", "force")):
        state = {"in_use": True, "now": 0.0, "closed": [], "closed_at": [], "notices": []}

        def close(pid, force=False):
            state["closed"].append((pid, force))
            state["closed_at"].append(state["now"])
            if ("force" if force else "graceful") in frees_on:
                state["in_use"] = False

        def sleep(seconds):
            state["now"] += seconds

        monkeypatch.setattr(im_module, "browser_profile_in_use", lambda profile_dir: state["in_use"])
        monkeypatch.setattr(im_module, "process_table", lambda: table)
        monkeypatch.setattr(im_module, "close_browser_process", close)
        monkeypatch.setattr(im_module, "print_above_progress", state["notices"].append)
        monkeypatch.setattr(im_module.time, "sleep", sleep)
        monkeypatch.setattr(im_module.time, "monotonic", lambda: state["now"])
        return state

    # A free profile needs no process list at all
    def test_a_free_profile_is_left_alone(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "browser_profile_in_use", lambda profile_dir: False)
        monkeypatch.setattr(im_module, "process_table", lambda: pytest.fail("the processes were listed"))

        assert im_module.release_browser_profile(self.PROFILE) is None

    # A window opened by hand is announced, left alone for the whole delay, and only then closed through its main process
    def test_a_window_opened_by_hand_is_announced_and_closed_after_the_delay(self, im_module, monkeypatch):
        state = self._machine(im_module, monkeypatch, {10: (1, "chrome.exe", self.HAND), 11: (10, "chrome.exe", self.HELPER)})

        assert im_module.release_browser_profile(self.PROFILE) is None

        assert state["closed"] == [(10, False)]
        assert state["closed_at"][0] >= im_module.BROWSER_PROFILE_CLOSE_DELAY_SECONDS == 15
        assert len(state["notices"]) == 1 and "in 15 seconds" in state["notices"][0] and "Ctrl+C" in state["notices"][0]

    # A window that goes away during the delay, closed by the person, is not closed again
    def test_a_window_closed_during_the_delay_is_left_alone(self, im_module, monkeypatch):
        state = self._machine(im_module, monkeypatch, {10: (1, "chrome.exe", self.HAND)})
        original_sleep = im_module.time.sleep

        def person_closes_it(seconds):
            original_sleep(seconds)
            if state["now"] >= 5:
                state["in_use"] = False

        monkeypatch.setattr(im_module.time, "sleep", person_closes_it)

        assert im_module.release_browser_profile(self.PROFILE) is None
        assert state["closed"] == []

    # Aborting during the delay closes nothing and says so
    def test_an_abort_closes_nothing(self, im_module, monkeypatch):
        import threading

        state = self._machine(im_module, monkeypatch, {10: (1, "chrome.exe", self.HAND)})
        stop = threading.Event()
        original_sleep = im_module.time.sleep

        def person_aborts(seconds):
            original_sleep(seconds)
            if state["now"] >= 4:
                stop.set()

        monkeypatch.setattr(im_module.time, "sleep", person_aborts)

        assert "aborted" in im_module.release_browser_profile(self.PROFILE, stop)
        assert state["closed"] == []

    # A window that does not close when asked is ended by force, and the profile is then free
    def test_a_window_that_stays_open_is_closed_by_force(self, im_module, monkeypatch):
        state = self._machine(im_module, monkeypatch, {10: (1, "chrome.exe", self.HAND)}, frees_on=("force",))

        assert im_module.release_browser_profile(self.PROFILE) is None
        assert state["closed"] == [(10, False), (10, True)]

    # A window that cannot be closed even by force is reported
    def test_a_window_that_cannot_be_closed_is_reported(self, im_module, monkeypatch):
        state = self._machine(im_module, monkeypatch, {10: (1, "chrome.exe", self.HAND)}, frees_on=())

        assert "could not be closed" in im_module.release_browser_profile(self.PROFILE)
        assert state["closed"] == [(10, False), (10, True)]

    # A browser a live run is driving is never closed, however long it is waited on, and the reason says so
    def test_a_running_scan_is_never_closed(self, im_module, monkeypatch):
        state = self._machine(im_module, monkeypatch, {
            20: (21, "chrome.exe", self.DRIVEN),
            21: (22, "node.exe", "node playwright driver"),
            22: (1, "python.exe", "python instagram_monitor.py target"),
        })

        reason = im_module.release_browser_profile(self.PROFILE)

        assert "scan" in reason and state["closed"] == [] and state["notices"] == []

    # A browser started through the installed command is a live run too, whatever the program is called
    def test_a_run_started_by_the_installed_command_is_alive(self, im_module, monkeypatch):
        state = self._machine(im_module, monkeypatch, {
            20: (22, "chrome.exe", self.DRIVEN),
            22: (1, "monitor.exe", "C:\\Scripts\\instagram_monitor.exe target"),
        })

        assert "scan" in im_module.release_browser_profile(self.PROFILE)
        assert state["closed"] == []

    # A browser an earlier run left behind when it died has the automation flags but no live run above it, so it is closed
    @pytest.mark.parametrize("table", [
        {20: (999, "chrome.exe", DRIVEN)},
        {20: (21, "chrome.exe", DRIVEN), 21: (999, "node.exe", "node playwright driver")},
    ])
    def test_a_window_left_by_a_dead_run_is_closed(self, im_module, monkeypatch, table):
        state = self._machine(im_module, monkeypatch, table)

        assert im_module.release_browser_profile(self.PROFILE) is None
        assert state["closed"] == [(20, False)]

    # A live scan next to a window opened by hand leaves both alone, since the scan has to be left running
    def test_a_scan_next_to_a_window_closes_neither(self, im_module, monkeypatch):
        state = self._machine(im_module, monkeypatch, {
            10: (1, "chrome.exe", self.HAND),
            20: (22, "chrome.exe", self.DRIVEN),
            22: (1, "python.exe", "python instagram_monitor.py"),
        })

        assert im_module.release_browser_profile(self.PROFILE) is not None
        assert state["closed"] == []

    # Another profile whose name only begins the same is not this one
    def test_only_this_profile_is_matched(self, im_module, monkeypatch):
        state = self._machine(im_module, monkeypatch, {30: (1, "chrome.exe", f'chrome.exe --user-data-dir="{self.PROFILE}2"')})

        assert "could not be found" in im_module.release_browser_profile(self.PROFILE)
        assert state["closed"] == []

    # A profile held by a browser that cannot be found is reported rather than guessed at
    def test_an_unfindable_owner_is_reported(self, im_module, monkeypatch):
        self._machine(im_module, monkeypatch, {})

        assert "could not be found" in im_module.release_browser_profile(self.PROFILE)

    # A scan that cannot free the profile stops with the reason, instead of the launch failing without one
    def test_a_scan_stops_with_the_reason(self, im_module, monkeypatch, tmp_path):
        from types import SimpleNamespace

        monkeypatch.setattr(im_module, "release_browser_profile", lambda profile_dir, stop_event=None: "its window could not be closed")
        bot = SimpleNamespace(context=SimpleNamespace(is_logged_in=False))

        with pytest.raises(im_module.BrowserFollowListError, match="could not be freed \\(its window could not be closed\\)"):
            next(im_module.browser_follow_list_batches(bot, SimpleNamespace(username="target"), "followers"))


class TestProcessAncestry:
    # A browser is driven by a live run when a Python process is anywhere above it
    def test_a_python_parent_is_a_live_run(self, im_module):
        table = {5: (6, "chrome.exe", "chrome"), 6: (7, "node.exe", "node"), 7: (1, "python3", "python3 x.py")}

        assert im_module.started_by_a_live_run(5, table) is True

    # A parent that no longer exists ends the chain without one
    def test_a_missing_parent_is_not(self, im_module):
        assert im_module.started_by_a_live_run(5, {5: (999, "chrome.exe", "chrome")}) is False

    # A chain that loops cannot hang the check
    def test_a_loop_ends(self, im_module):
        assert im_module.started_by_a_live_run(5, {5: (6, "a", "a"), 6: (5, "b", "b")}) is False

    # The real process table lists this very process with a Python name, on whichever system the tests run
    def test_the_real_table_lists_this_process(self, im_module):
        import os

        table = im_module.process_table()

        assert os.getpid() in table and "python" in table[os.getpid()][1].lower()


class TestOpenBrowserCommand:
    # Runs the real command line with the window stubbed, returning the exit code and what the window was asked to open
    @staticmethod
    def _run(monkeypatch, capsys, *argv, result=0):
        asked = []
        monkeypatch.setattr(im, "open_browser_window", lambda target=None, stop_event=None: asked.append(target) or result)
        monkeypatch.setattr(im, "check_internet", lambda *args, **kwargs: True, raising=False)
        monkeypatch.setattr(im, "clear_screen", lambda *args, **kwargs: None, raising=False)
        monkeypatch.setattr(im.sys, "argv", ["instagram_monitor.py", "--config-file", "none", "--env-file", "none", "--no-color", *argv])
        capsys.readouterr()
        with pytest.raises(SystemExit) as exit_call:
            im.run_main()
        return exit_call.value.code, asked, capsys.readouterr().out

    # The option opens the window with no target and exits with the window's own code
    def test_the_option_opens_the_window(self, monkeypatch, capsys):
        code, asked, _output = self._run(monkeypatch, capsys, "--open-browser")

        assert (code, asked) == (0, [None])

    # A target on the command line, positional or from --targets, is the profile that opens
    @pytest.mark.parametrize("argv", [("--open-browser", "target.user"), ("--open-browser", "--targets", "target.user,other")])
    def test_a_target_is_passed_on(self, monkeypatch, capsys, argv):
        _code, asked, _output = self._run(monkeypatch, capsys, *argv)

        assert asked == ["target.user"]

    # A failed start is the command's exit code, so a script can tell
    def test_a_failure_is_the_exit_code(self, monkeypatch, capsys):
        code, _asked, _output = self._run(monkeypatch, capsys, "--open-browser", result=1)

        assert code == 1

    # It is one of the standalone actions, so it cannot be mixed with another
    def test_it_cannot_be_combined_with_another_action(self, monkeypatch, capsys):
        code, asked, _output = self._run(monkeypatch, capsys, "--open-browser", "--exposure")

        assert code == 2 and asked == []

    # It leaves the screen alone like the other one-shot commands
    def test_it_keeps_the_terminal_history(self, monkeypatch):
        monkeypatch.setattr(im.sys, "argv", ["instagram_monitor.py", "--open-browser"])

        assert im.keep_terminal_history() is True
