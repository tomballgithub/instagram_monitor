"""Browser-level dashboard tests using a real headless Chromium instance."""

import http.server
import json
import re
import shutil
import socketserver
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from werkzeug.serving import make_server


playwright_sync = pytest.importorskip("playwright.sync_api")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


# Launches headless Chromium, skipping only when the Playwright package is present without its browser download
def launch_chromium(playwright):
    try:
        return playwright.chromium.launch(headless=True)
    except playwright_sync.Error as error:
        # Any other launch failure is a real problem and must fail rather than silently skip
        if "executable doesn't exist" not in str(error).casefold():
            raise
        pytest.skip("Chromium is not installed, run 'python -m playwright install chromium'")


@pytest.fixture
# Serves a local HTTP response through an HTTPS proxy with an untrusted certificate
def untrusted_https_proxy(tmp_path):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("OpenSSL is needed to create the local proxy certificate")
    certificate = tmp_path / "proxy.crt"
    key = tmp_path / "proxy.key"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(certificate), "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True, timeout=30)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, key)
    requests = []

    class ProxyHandler(socketserver.BaseRequestHandler):
        # Replies locally without connecting to the requested destination
        def handle(self):
            self.request.settimeout(5)
            try:
                with tls.wrap_socket(self.request, server_side=True) as connection:
                    request = b""
                    while b"\r\n\r\n" not in request:
                        chunk = connection.recv(4096)
                        if not chunk:
                            return
                        request += chunk
                    requests.append(request.split(b"\r\n", 1)[0])
                    body = b"<title>Local proxy response</title>"
                    connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            except OSError:
                # Certificate rejection and idle browser connections can close before an HTTP request
                return

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), ProxyHandler) as server:
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"https://127.0.0.1:{server.server_address[1]}", certificate, requests
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.e2e
@pytest.mark.parametrize("verify", [True, False])
# Exercises Chromium's HTTPS proxy handshake through the browser provider's launch options
def test_browser_source_https_proxy_verification(im_module, monkeypatch, tmp_path, untrusted_https_proxy, verify):
    proxy_url, certificate, requests = untrusted_https_proxy
    monkeypatch.setattr(im_module, "PROXY_ENABLED", True)
    monkeypatch.setattr(im_module, "PROXY_URL", proxy_url)
    monkeypatch.setattr(im_module, "PROXY_CERT_PATH", str(certificate))
    monkeypatch.setattr(im_module, "VERIFY_SSL", verify)
    monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_HEADLESS", True)
    with playwright_sync.sync_playwright() as driver:
        try:
            context = driver.chromium.launch_persistent_context(str(tmp_path / "browser-profile"), **im_module.browser_launch_options())
        except playwright_sync.Error as error:
            if "executable doesn't exist" not in str(error).casefold():
                raise
            pytest.skip("Chromium is not installed")
        try:
            page = context.pages[0]
            url = "http://browser-tls.invalid/"
            if verify:
                with pytest.raises(playwright_sync.Error, match="ERR_PROXY_CERTIFICATE_INVALID"):
                    page.goto(url, timeout=10000)
            else:
                page.goto(url, wait_until="domcontentloaded", timeout=10000)
                assert page.title() == "Local proxy response"
                assert b"GET http://browser-tls.invalid/ HTTP/1.1" in requests
        finally:
            context.close()


# Runs the real dashboard application on an ephemeral loopback port
@pytest.fixture
def dashboard_server(im_module, monkeypatch) -> Iterator[str]:
    # Targets added through the running app sync into the module-global list, so isolate it from later tests
    monkeypatch.setattr(im_module, "TARGET_USERNAMES", [])
    monkeypatch.setattr(im_module, "WEB_DASHBOARD_TEMPLATE_DIR", str(PROJECT_ROOT / "templates"))
    monkeypatch.setattr(im_module, "SESSION_USERNAME", "")
    monkeypatch.setattr(im_module, "SKIP_SESSION", True)
    monkeypatch.setattr(im_module, "INSTA_CHECK_INTERVAL", 5400)
    monkeypatch.setattr(im_module, "WEB_DASHBOARD_DATA", {"session": {"username": None, "active": False}, "targets": {"target.user": {"status": "Waiting", "posts": 7, "followers": 11, "followings": 5}}, "activities": [], "check_count": 2, "last_check": "Never", "next_check": "Pending", "is_monitoring": False})
    monkeypatch.setattr(im_module, "log_activity", lambda *args, **kwargs: None)
    app = im_module.create_web_dashboard_app()
    assert app is not None
    server = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()
    thread.join(timeout=5)


# Verifies rendered dashboard data, navigation and target creation in Chromium
@pytest.mark.e2e
def test_dashboard_user_flow_in_chromium(dashboard_server):
    server_url = dashboard_server
    page_errors = []
    with playwright_sync.sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        page = browser.new_page()
        page.set_default_timeout(5000)
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        response = page.goto(server_url, wait_until="domcontentloaded")
        assert response is not None
        assert response.ok
        playwright_sync.expect(page).to_have_title("Instagram Monitor - Control Panel")
        playwright_sync.expect(page.locator("#active-targets")).to_have_text("1")
        playwright_sync.expect(page.locator("#dashboard-targets-simple-body")).to_contain_text("target.user")
        page.locator('[data-page="targets"]').click()
        playwright_sync.expect(page.locator("#page-targets")).to_have_class(re.compile(r"\bactive\b"))
        page.locator("#page-targets").get_by_role("button", name="Add Target").click()
        page.locator("#modal-add-target .toggle-slider").click()
        assert not page.locator("#start-immediately").is_checked()
        page.locator("#new-target-username").fill("Added.User")
        page.locator("#modal-add-target").get_by_role("button", name="Add Target").click()
        playwright_sync.expect(page.locator("#toast-message")).to_contain_text("Added target: added.user")
        playwright_sync.expect(page.locator("#targets-list")).to_contain_text("added.user")
        assert page_errors == []
        browser.close()


# Opens synthetic story data with every nonlocal browser request intercepted
@pytest.fixture
def story_dashboard_page(dashboard_server, im_module, monkeypatch):
    monkeypatch.setattr(im_module, "DASHBOARD_MODE", "user")
    external_requests = []

    # Serves external destinations locally so even a failed warning cannot contact Instagram
    def route_request(route):
        if urlsplit(route.request.url).netloc == urlsplit(dashboard_server).netloc:
            route.continue_()
        else:
            external_requests.append(route.request.url)
            route.fulfill(status=200, content_type="text/html", body="Offline destination")

    with playwright_sync.sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        context = browser.new_context(service_workers="block")
        context.route("**/*", route_request)
        page = context.new_page()
        page.set_default_timeout(3000)
        yield page, context, external_requests, dashboard_server
        browser.close()


# Requires confirmation on every story surface for both current and older records
@pytest.mark.e2e
@pytest.mark.parametrize("surface", ["fetched", "legacy", "dashboard", "config", "activity"])
@pytest.mark.parametrize("story_flag", [True, None, False])
def test_story_warning_guards_navigation_in_chromium(story_dashboard_page, im_module, surface, story_flag):
    page, context, external_requests, server_url = story_dashboard_page
    story_url = "https://www.instagram.com/stories/target.user/123/"
    update = {"type": "Image", "user": "target.user", "post_url": story_url}
    details = {"url": story_url}
    if story_flag is not None:
        update["is_story"] = story_flag
        details["is_story"] = story_flag
    if surface == "legacy":
        im_module.WEB_DASHBOARD_DATA["targets"]["target.user"]["last_story"] = update
    else:
        im_module.WEB_DASHBOARD_DATA["targets"]["target.user"]["fetched_updates"] = [update]
    im_module.WEB_DASHBOARD_DATA["activities"] = [{"time": "12:00", "message": "New story item", "level": "update", "details": details}]
    page.goto(server_url, wait_until="domcontentloaded")
    if surface in ("fetched", "legacy"):
        control = page.locator("#last-fetched-container").get_by_text(re.compile(r"^View (Story|Post)$"))
        playwright_sync.expect(control).to_have_text("View Story")
    else:
        if surface == "config":
            page.locator("#mode-config-btn").click()
        elif surface == "activity":
            page.locator('[data-page="activity"]').click()
        feed_id = {"dashboard": "dashboard-activity", "config": "dashboard-activity-config", "activity": "activity-feed"}[surface]
        control = page.locator(f"#{feed_id}").get_by_text("View", exact=True)
    modal = page.locator("#modal-anonymity")
    for activation in ("click", "middle", "modified", "keyboard"):
        if activation == "middle":
            control.click(button="middle")
        elif activation == "modified":
            control.click(modifiers=["ControlOrMeta"])
        elif activation == "keyboard":
            control.press("Enter")
        else:
            control.click()
        playwright_sync.expect(modal).to_be_visible()
        playwright_sync.expect(modal).to_contain_text("Warning")
        playwright_sync.expect(page.locator("#anonymity-confirm-link")).to_have_attribute("href", story_url)
        assert external_requests == []
        assert len(context.pages) == 1
        modal.get_by_role("button", name="Cancel", exact=True).click()
        playwright_sync.expect(modal).to_be_hidden()

    control.click()
    page.keyboard.press("Escape")
    playwright_sync.expect(modal).to_be_hidden()
    control.click()
    modal.click(position={"x": 5, "y": 5})
    playwright_sync.expect(modal).to_be_hidden()
    assert control.get_attribute("href") is None
    control.click()
    with page.expect_popup() as popup_info:
        modal.get_by_role("link", name="I Understand, Proceed").click()
    popup_info.value.wait_for_load_state()
    assert external_requests == [story_url]
    playwright_sync.expect(modal).to_be_hidden()


# Leaves post links and downloaded images accessible without a story warning
@pytest.mark.e2e
def test_posts_and_local_media_do_not_require_story_confirmation(story_dashboard_page, im_module):
    page, context, external_requests, server_url = story_dashboard_page
    post_url = "https://www.instagram.com/p/offline/"
    im_module.WEB_DASHBOARD_DATA["targets"]["target.user"]["fetched_updates"] = [
        {"type": "Post", "post_url": post_url, "url": "/media/post.jpg", "video_url": None, "is_story": False},
        {"type": "Story Video", "post_url": "https://www.instagram.com/stories/target.user/", "video_url": "/media/story.mp4", "is_story": True},
    ]
    page.add_init_script("HTMLMediaElement.prototype.play = () => Promise.resolve()")
    page.goto(server_url, wait_until="domcontentloaded")
    modal = page.locator("#modal-anonymity")
    with page.expect_popup() as post_popup:
        page.get_by_role("link", name="View Post", exact=True).click()
    post_popup.value.wait_for_load_state()
    assert external_requests == [post_url]
    playwright_sync.expect(modal).to_be_hidden()
    with page.expect_popup() as media_popup:
        page.get_by_role("link", name="View Media", exact=True).click()
    media_popup.value.wait_for_load_state()
    assert media_popup.value.url == server_url + "media/post.jpg"
    page.get_by_role("link", name="Play Video", exact=True).click()
    playwright_sync.expect(page.locator("#modal-video-player")).to_be_visible()
    playwright_sync.expect(page.locator("#local-video-element")).to_have_attribute("src", server_url + "media/story.mp4")
    playwright_sync.expect(modal).to_be_hidden()
    assert external_requests == [post_url]


# Verifies the connection card is filled from the settings endpoint and saves what the user picks
@pytest.mark.e2e
def test_connection_settings_save_from_chromium(dashboard_server, im_module, monkeypatch):
    monkeypatch.setattr(im_module, "HTTP_BACKEND", "requests")
    monkeypatch.setattr(im_module, "CURL_CFFI_IMPERSONATE", "auto")
    monkeypatch.setattr(im_module, "FOLLOW_LIST_SOURCE", "auto")
    monkeypatch.setattr(im_module, "_CURL_CFFI_AVAILABLE", True)
    monkeypatch.setattr(im_module, "curl_cffi_supported_impersonate_targets", lambda: {"chrome", "firefox"})
    monkeypatch.setattr(im_module, "print_cur_ts", lambda *args, **kwargs: None)
    page_errors = []
    with playwright_sync.sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        page = browser.new_page()
        page.set_default_timeout(5000)
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(dashboard_server, wait_until="domcontentloaded")
        page.locator('[data-page="settings"]').click()

        playwright_sync.expect(page.locator("#http-backend")).to_have_value("requests")
        # Only curl_cffi acts on an impersonation target, so the stock transport hides that question
        playwright_sync.expect(page.locator("#impersonate-group")).to_be_hidden()
        page.locator("#http-backend").select_option("curl_cffi")
        playwright_sync.expect(page.locator("#impersonate-group")).to_be_visible()
        assert page.locator("#impersonate option").all_text_contents() == ["Auto (match the user agent)", "chrome", "firefox"]
        page.locator("#impersonate").select_option("firefox")
        page.locator("#page-settings").get_by_role("button", name="Save Settings").click()
        playwright_sync.expect(page.locator("#toast-message")).to_contain_text("Settings saved")

        # The browser source drives a Chromium build, so it only saves alongside a Chromium target
        page.locator("#impersonate").select_option("chrome")
        page.locator("#follow-list-source").select_option("browser")
        playwright_sync.expect(page.locator("#connection-note")).to_contain_text("experimental")
        page.locator("#page-settings").get_by_role("button", name="Save Settings").click()
        playwright_sync.expect(page.locator("#toast-message")).to_contain_text("Settings saved")

        assert im_module.HTTP_BACKEND == "curl_cffi"
        assert im_module.CURL_CFFI_IMPERSONATE == "chrome"
        assert im_module.FOLLOW_LIST_SOURCE == "browser"
        assert page_errors == []
        browser.close()


# Verifies the dashboard cannot put a running session into the split identity monitoring refuses to start in
@pytest.mark.e2e
def test_a_split_identity_cannot_be_saved_in_chromium(dashboard_server, im_module, monkeypatch):
    monkeypatch.setattr(im_module, "HTTP_BACKEND", "curl_cffi")
    monkeypatch.setattr(im_module, "CURL_CFFI_IMPERSONATE", "auto")
    monkeypatch.setattr(im_module, "FOLLOW_LIST_SOURCE", "auto")
    monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_CHANNEL", "chromium")
    monkeypatch.setattr(im_module, "_CURL_CFFI_AVAILABLE", True)
    monkeypatch.setattr(im_module, "curl_cffi_supported_impersonate_targets", lambda: {"chrome", "firefox"})
    monkeypatch.setattr(im_module, "print_cur_ts", lambda *args, **kwargs: None)
    with playwright_sync.sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        page = browser.new_page()
        page.set_default_timeout(5000)
        page.goto(dashboard_server, wait_until="domcontentloaded")
        page.locator('[data-page="settings"]').click()

        page.locator("#impersonate").select_option("firefox")
        page.locator("#follow-list-source").select_option("browser")
        page.locator("#page-settings").get_by_role("button", name="Save Settings").click()

        playwright_sync.expect(page.locator("#toast-message")).to_contain_text("two different clients")
        assert im_module.FOLLOW_LIST_SOURCE == "auto"
        assert im_module.CURL_CFFI_IMPERSONATE == "auto"
        browser.close()


# Verifies a machine without curl_cffi cannot pick it, rather than saving a transport every request would fail on
@pytest.mark.e2e
def test_an_uninstalled_transport_cannot_be_selected_in_chromium(dashboard_server, im_module, monkeypatch):
    monkeypatch.setattr(im_module, "HTTP_BACKEND", "requests")
    monkeypatch.setattr(im_module, "_CURL_CFFI_AVAILABLE", False)
    with playwright_sync.sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        page = browser.new_page()
        page.set_default_timeout(5000)
        page.goto(dashboard_server, wait_until="domcontentloaded")
        page.locator('[data-page="settings"]').click()

        option = page.locator('#http-backend option[value="curl_cffi"]')
        playwright_sync.expect(option).to_be_disabled()
        playwright_sync.expect(option).to_contain_text("not installed")
        playwright_sync.expect(page.locator("#http-backend")).to_have_value("requests")
        browser.close()


# Runs the dashboard seeded with a hostile Instagram-supplied media item
@pytest.fixture
def hostile_media_server(im_module, monkeypatch) -> Iterator[str]:
    # Targets added through the running app sync into the module-global list, so isolate it from later tests
    monkeypatch.setattr(im_module, "TARGET_USERNAMES", [])
    monkeypatch.setattr(im_module, "WEB_DASHBOARD_TEMPLATE_DIR", str(PROJECT_ROOT / "templates"))
    monkeypatch.setattr(im_module, "SESSION_USERNAME", "")
    monkeypatch.setattr(im_module, "SKIP_SESSION", True)
    monkeypatch.setattr(im_module, "INSTA_CHECK_INTERVAL", 5400)
    hostile_update = {
        "type": "Post",
        "caption": "holiday",
        "timestamp": "01 Jan 26 10:00",
        "user": "target.user",
        "is_story": False,
        "url": 'https://cdn.example/a.jpg" onerror="window.__xss_img=1',
        "post_url": 'https://www.instagram.com/p/x/" onmouseover="window.__xss_href=1" data-x="',
        "video_url": "javascript:window.__xss_scheme=1",
    }
    monkeypatch.setattr(im_module, "WEB_DASHBOARD_DATA", {"session": {"username": None, "active": False}, "targets": {"target.user": {"status": "Waiting", "fetched_updates": [hostile_update]}}, "activities": [], "check_count": 1, "last_check": "Never", "next_check": "Pending", "is_monitoring": False})
    monkeypatch.setattr(im_module, "log_activity", lambda *args, **kwargs: None)
    app = im_module.create_web_dashboard_app()
    assert app is not None
    server = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()
    thread.join(timeout=5)


# Verifies Instagram-supplied media URLs cannot break out of their attribute or smuggle a javascript scheme
@pytest.mark.e2e
def test_hostile_media_urls_do_not_execute_in_chromium(hostile_media_server):
    with playwright_sync.sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        page = browser.new_page()
        page.set_default_timeout(5000)
        page.goto(hostile_media_server, wait_until="domcontentloaded")
        page.wait_for_selector(".fetched-history-item")

        assert page.evaluate("() => [window.__xss_img, window.__xss_href, window.__xss_scheme]") == [None, None, None]
        assert page.locator("[onerror]").count() == 0
        assert page.locator("[onmouseover]").count() == 0
        assert page.evaluate("() => Array.from(document.querySelectorAll('.fetched-history-item a')).every(a => a.protocol === 'http:' || a.protocol === 'https:')")

        browser.close()


# Mimics the structure of Instagram's follower dialog: a scrollable box that appends more entries as it scrolls
FOLLOW_DIALOG_PAGE = """
<html><body>
<div role="dialog">
  <div id="scroller" style="height:300px;overflow-y:auto">
    <div id="items"></div>
  </div>
</div>
<script>
  const items = document.getElementById('items');
  let next = 0;
  function add(count) {
    for (let i = 0; i < count; i++) {
      const link = document.createElement('a');
      link.setAttribute('href', '/user' + next + '/');
      link.textContent = 'user' + next;
      link.style.display = 'block';
      link.style.height = '60px';
      items.appendChild(link);
      next++;
    }
  }
  add(10);
  document.getElementById('scroller').addEventListener('scroll', () => { if (next < 30) add(10); });
  const dialog = document.querySelector('div[role="dialog"]');
  for (const href of ['/p/ABC123/', '/explore/tags/travel/', '/user0/followers/', '/', '/reels/audio/1/']) {
    const decoy = document.createElement('a');
    decoy.setAttribute('href', href);
    dialog.appendChild(decoy);
  }
</script>
</body></html>
"""


# Verifies the follower dialog scripts read real rendered markup, since those selectors are the fragile part
@pytest.mark.e2e
def test_follow_list_dialog_is_harvested_in_chromium(im_module):
    with playwright_sync.sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        page = browser.new_page()
        page.set_default_timeout(5000)
        page.set_content(FOLLOW_DIALOG_PAGE)

        # Chromium dispatches the scroll event that appends the next page asynchronously, so the delay has to outlast a loaded CI runner or the harvest stalls out early
        batches = list(im_module.harvest_follow_list_dialog(page, 0.5))
        harvested = [name for batch in batches for name in batch]

        browser.close()

    # Every rendered profile link is read, scrolling loads the rest, and post, tag and sub-page links are ignored
    assert harvested == [f"user{index}" for index in range(30)]
    assert len(batches) > 1


# Keeps suggested accounts out of a follow list even when both sections contain profile links
@pytest.mark.e2e
def test_follow_list_suggestions_are_excluded_in_chromium(im_module):
    with playwright_sync.sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        page = browser.new_page()
        page.set_content('<div role="dialog"><div><a href="/actual.user/">actual.user</a><a href="/actual.user/"><img alt="profile"></a></div><div><h4>Suggested for you</h4></div><div><a href="/suggested.user/">suggested.user</a></div><a href="/explore/people/">See All Suggestions</a></div>')

        names = [name for batch in im_module.harvest_follow_list_dialog(page, 0) for name in batch]

        assert names == ["actual.user"]
        browser.close()


# Opens count links from both profile layouts and waits for asynchronously rendered names
@pytest.mark.e2e
@pytest.mark.parametrize("kind", ["followers", "following"])
@pytest.mark.parametrize("legacy", [False, True])
def test_follow_list_count_links_open_in_chromium(im_module, kind, legacy):
    with playwright_sync.sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        page = browser.new_page()
        page.set_default_timeout(1000)
        href = f"/target.user/{kind}/" if legacy else "#"
        # The direct URL also covers accounts whose display language differs from the browser locale
        label = "localized count" if legacy else f"6 {kind}"
        page.set_content(f'<button onclick="window.wrongControl = true">Following</button><a href="#">About followers</a><a href="#">6 {"following" if kind == "followers" else "followers"}</a><a id="count" href="{href}" role="link"><span>{label}</span></a>')
        page.locator("#count").evaluate("""link => link.addEventListener('click', event => {
          event.preventDefault();
          const dialog = document.createElement('div');
          dialog.setAttribute('role', 'dialog');
          document.body.appendChild(dialog);
          setTimeout(() => { dialog.innerHTML = '<a href="/first.user/">first.user</a><a href="/second.user/">second.user</a>'; }, 100);
        })""")

        im_module.open_browser_follow_list_dialog(page, "target.user", kind)
        names = [name for batch in im_module.harvest_follow_list_dialog(page, 0) for name in batch]

        assert names == ["first.user", "second.user"]
        assert page.evaluate("window.wrongControl") is None
        browser.close()


# Verifies the dialog scripts report a page with no follower dialog instead of harvesting the rest of it
@pytest.mark.e2e
def test_a_page_without_a_dialog_is_reported_in_chromium(im_module):
    with playwright_sync.sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        page = browser.new_page()
        page.set_default_timeout(5000)
        page.set_content('<html><body><a href="/someone/">someone</a></body></html>')

        with pytest.raises(im_module.BrowserFollowListError):
            list(im_module.harvest_follow_list_dialog(page, 0))

        browser.close()


# Verifies the dashboard profile picker marks the profiles holding an Instagram session and preselects the only one
@pytest.mark.e2e
def test_browser_import_marks_signed_in_profiles_in_chromium(dashboard_server, im_module, monkeypatch):
    signed_in = "/u/b/cookies.sqlite"
    monkeypatch.setattr(im_module, "list_firefox_profiles", lambda: [{"dir": "a.work", "name": "work", "path": "/u/a/cookies.sqlite", "install": ""}, {"dir": "b.default", "name": "default-release", "path": signed_in, "install": ""}, {"dir": "c.default", "name": "default-release", "path": "/u/c/cookies.sqlite", "install": "Snap"}])
    monkeypatch.setattr(im_module, "cookie_file_has_instagram_session", lambda cookie_file, firefox=False: cookie_file == signed_in)
    page_errors = []
    with playwright_sync.sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        page = browser.new_page()
        page.set_default_timeout(5000)
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(dashboard_server, wait_until="domcontentloaded")
        page.locator('[data-page="session"]').click()
        page.locator("#browser-import-btn").click()

        playwright_sync.expect(page.locator("#browser-profiles-container")).to_be_visible()
        assert page.locator("#browser-profile-select option").all_text_contents() == ["work", "* default-release", "default-release (Snap)"]
        # The two installs share a friendly name, so the packaging has to reach the label the user reads
        playwright_sync.expect(page.locator("#browser-profile-select")).to_have_value(signed_in)
        playwright_sync.expect(page.locator("#toast-message")).to_contain_text("* marks the 1 signed in to Instagram")

        assert page_errors == []
        browser.close()


# The only session value the fake Instagram below accepts, so a stale cookie can be told from a good one
FAKE_SESSION = "valid-session"

# The profile page the fake Instagram serves: the follower link opens a dialog for a good session and the login page otherwise
FAKE_PROFILE_PAGE = """<!doctype html><html><body><h1>target.user</h1>
<a href="/target.user/followers/" id="followers">3 followers</a>
<script>
document.getElementById('followers').addEventListener('click', event => {
  event.preventDefault();
  if (!document.cookie.includes('sessionid=%s')) { location.href = '/accounts/login/?next=/target.user/'; return; }
  const dialog = document.createElement('div');
  dialog.setAttribute('role', 'dialog');
  const box = document.createElement('div');
  box.style.cssText = 'height:200px;overflow:auto';
  for (const name of ['alpha', 'beta', 'gamma']) {
    const row = document.createElement('div');
    row.style.height = '30px';
    row.innerHTML = '<a href="/' + name + '/">' + name + '</a>';
    box.appendChild(row);
  }
  dialog.appendChild(box);
  document.body.appendChild(dialog);
});
</script></body></html>""" % FAKE_SESSION


# The follower count and names behind the paged fake list below
FAKE_LIST_NAMES = [f"name{index:03d}" for index in range(60)]

# A profile whose follower dialog loads its list from the friendships endpoint 12 at a time, the way Instagram's does,
# and shows each account once however often the endpoint sends it
FAKE_PAGED_PAGE = """<!doctype html><html><body><h1>paged.user</h1>
<a href="/paged.user/followers/" id="followers">60 followers</a>
<script>
document.getElementById('followers').addEventListener('click', event => {
  event.preventDefault();
  const dialog = document.createElement('div');
  dialog.setAttribute('role', 'dialog');
  const box = document.createElement('div');
  box.style.cssText = 'height:200px;overflow:auto';
  dialog.appendChild(box);
  document.body.appendChild(dialog);
  const shown = new Set();
  let next = null, loading = false, done = false;
  async function load() {
    if (loading || done) return;
    loading = true;
    const response = await fetch('/api/v1/friendships/1/followers/?count=12' + (next ? '&max_id=' + next : ''), {credentials: 'include'});
    const body = await response.json();
    loading = false;
    for (const user of body.users) {
      if (shown.has(user.username)) continue;
      shown.add(user.username);
      const row = document.createElement('div');
      row.style.height = '30px';
      row.innerHTML = '<a href="/' + user.username + '/">' + user.username + '</a>';
      box.appendChild(row);
    }
    next = body.next_max_id;
    if (!next) done = true;
  }
  box.addEventListener('scroll', () => { if (box.scrollTop + box.clientHeight >= box.scrollHeight - 40) load(); });
  load();
});
</script></body></html>"""


@pytest.fixture
# Serves a fake Instagram over HTTPS on loopback, recording every request path and whether it carried a session cookie
def fake_instagram(im_module, monkeypatch, tmp_path):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("OpenSSL is needed to create the local site certificate")
    certificate = tmp_path / "site.crt"
    key = tmp_path / "site.key"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(certificate), "-days", "1", "-subj", "/CN=www.instagram.com"], check=True, capture_output=True, timeout=60)
    class Requests(list):
        # Seconds the site pauses before it first answers the page at a given offset, which a test sets
        delays = {}

    requests = Requests()
    served = {}

    class SiteHandler(http.server.BaseHTTPRequestHandler):
        # Serves the profile pages, the friendships endpoint and a bare login page, and records what the browser asked for
        def do_GET(self):
            requests.append((self.path, "sessionid=" in (self.headers.get("Cookie") or "")))
            if self.path.startswith("/api/v1/friendships/"):
                self.send_list_page()
                return
            body = "<!doctype html><title>Log in</title><p>Log in</p>"
            if self.path.startswith("/target.user/"):
                body = FAKE_PROFILE_PAGE
            elif self.path.startswith("/paged.user/"):
                body = FAKE_PAGED_PAGE
            self.send_payload(body.encode(), "text/html; charset=utf-8")

        # Answers one list request, putting accounts from the first page into the last slots of a later page the first
        # time it is asked for, the way Instagram does. Asking for the same page again returns it complete
        def send_list_page(self):
            query = parse_qs(urlsplit(self.path).query)
            count = int(query.get("count", ["12"])[0])
            offset = int(query.get("max_id", ["0"])[0])
            names = FAKE_LIST_NAMES[offset:offset + count]
            served[(offset, count)] = served.get((offset, count), 0) + 1
            if served[(offset, count)] == 1:
                time.sleep(requests.delays.get(offset, 0))
            if offset and served[(offset, count)] == 1:
                names = names[:-2] + FAKE_LIST_NAMES[:2]
            following = offset + count
            body = {"users": [{"username": name} for name in names], "next_max_id": str(following) if following < len(FAKE_LIST_NAMES) else None, "status": "ok"}
            self.send_payload(json.dumps(body).encode(), "application/json")

        # Sends one 200 response with the given body
        def send_payload(self, payload, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        # Keeps the test output free of the request log
        def log_message(self, *args):
            return

    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, key)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SiteHandler)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    original_options = im_module.browser_launch_options

    # Sends www.instagram.com to the local server and accepts its self-signed certificate
    def local_options():
        options = original_options()
        options["args"] = [*options["args"], f"--host-resolver-rules=MAP www.instagram.com 127.0.0.1:{server.server_port}", "--ignore-certificate-errors"]
        options["ignore_https_errors"] = True
        return options

    monkeypatch.setattr(im_module, "browser_launch_options", local_options)
    monkeypatch.setattr(im_module, "SESSION_USERNAME", "")
    monkeypatch.setattr(im_module, "SKIP_SESSION", True)
    monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_HEADLESS", True)
    monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_SCROLL_DELAY", 0)
    monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_PROFILE_DIR", str(tmp_path / "profiles"))
    monkeypatch.setattr(im_module, "browser_follow_list_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(im_module, "log_activity", lambda *args, **kwargs: None)
    try:
        yield requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# Signs the anonymous browser profile in the way a person would, by leaving a session cookie in it for the next launch
# Instagram's own session cookie carries an expiry, which is what makes Chromium keep it once the browser closes
def sign_in_anonymous_profile(im_module, session_value):
    with playwright_sync.sync_playwright() as driver:
        try:
            context = driver.chromium.launch_persistent_context(im_module.browser_profile_dir(), **im_module.browser_launch_options())
        except playwright_sync.Error as error:
            if "executable doesn't exist" not in str(error).casefold():
                raise
            pytest.skip("Chromium is not installed")
        try:
            context.add_cookies([{"name": "sessionid", "value": session_value, "domain": ".instagram.com", "path": "/", "secure": True, "sameSite": "Lax", "expires": time.time() + 86400}])
        finally:
            context.close()


# Returns the stand-in for an anonymous session, which has a context and no logged-in state
def anonymous_session_bot():
    return SimpleNamespace(context=SimpleNamespace(is_logged_in=False, _session=SimpleNamespace()))


# Verifies an anonymous run reads a list with the sign-in the browser profile kept from an earlier launch
@pytest.mark.e2e
def test_anonymous_browser_scan_reads_a_list_with_its_own_sign_in(im_module, fake_instagram):
    sign_in_anonymous_profile(im_module, FAKE_SESSION)
    profile = SimpleNamespace(username="target.user", userid=1, followers=3, followees=0)

    names = [entry.username for entry in im_module.iter_browser_follow_list(anonymous_session_bot(), profile, "followers")]

    assert names == ["alpha", "beta", "gamma"]
    # The profile page was fetched with the persisted cookie, which nothing in the run handed over
    assert ("/target.user/", True) in fake_instagram


# Verifies a profile with no sign-in stops before the browser opens a single page, instead of browsing anonymously
@pytest.mark.e2e
def test_anonymous_browser_scan_without_a_sign_in_never_opens_the_profile(im_module, fake_instagram):
    profile = SimpleNamespace(username="target.user", userid=1, followers=3, followees=0)

    try:
        with pytest.raises(im_module.BrowserFollowListError, match="browser is not signed in"):
            list(im_module.iter_browser_follow_list(anonymous_session_bot(), profile, "followers"))
    except playwright_sync.Error as error:
        if "executable doesn't exist" not in str(error).casefold():
            raise
        pytest.skip("Chromium is not installed")

    assert fake_instagram == []


# Verifies a sign-in the site no longer accepts is reported as expired, since the follower link leads to the login page
@pytest.mark.e2e
def test_anonymous_browser_scan_reports_a_sign_in_the_site_rejected(im_module, fake_instagram):
    sign_in_anonymous_profile(im_module, "stale-session")
    profile = SimpleNamespace(username="target.user", userid=1, followers=3, followees=0)

    with pytest.raises(im_module.BrowserFollowListError, match="expired or was removed"):
        list(im_module.iter_browser_follow_list(anonymous_session_bot(), profile, "followers"))


# Returns the friendships requests the fake Instagram saw, as the paths the browser asked for
def list_requests(requests):
    return [path for path, _cookie in requests if path.startswith("/api/v1/friendships/")]


# Verifies the page size is raised and pages that repeated accounts are asked for again, so the whole list arrives
@pytest.mark.e2e
def test_anonymous_browser_scan_recovers_accounts_a_page_lost_to_repeats(im_module, fake_instagram, monkeypatch):
    monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_SCROLL_DELAY", 0.3)
    monkeypatch.setattr(im_module, "BROWSER_REFETCH_DELAY_MS", 20)
    sign_in_anonymous_profile(im_module, FAKE_SESSION)
    profile = SimpleNamespace(username="paged.user", userid=1, followers=60, followees=0)

    names = [entry.username for entry in im_module.iter_browser_follow_list(anonymous_session_bot(), profile, "followers")]

    assert sorted(names) == FAKE_LIST_NAMES
    asked = list_requests(fake_instagram)
    # The dialog asks for 12, and every request that reached the site asked for 25
    assert asked and all("count=25" in path for path in asked)
    # Pages 2 and 3 repeated accounts the first time, so each was asked for again at the same cursor
    assert len(asked) > len(set(asked))


# Verifies the same list comes back short when both settings are off, which is the problem they exist to solve
@pytest.mark.e2e
def test_anonymous_browser_scan_with_both_settings_off_reads_only_the_rows(im_module, fake_instagram, monkeypatch):
    monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_SCROLL_DELAY", 0.3)
    monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_PAGE_SIZE", 0)
    monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_REFETCH_TRIES", 0)
    sign_in_anonymous_profile(im_module, FAKE_SESSION)
    profile = SimpleNamespace(username="paged.user", userid=1, followers=60, followees=0)

    names = [entry.username for entry in im_module.iter_browser_follow_list(anonymous_session_bot(), profile, "followers")]

    assert 40 < len(names) < 60
    asked = list_requests(fake_instagram)
    assert asked and all("count=12" in path for path in asked)
    assert len(asked) == len(set(asked))


# Verifies a page that arrives long after the scan's few quiet scrolls is still read, instead of ending the list there
@pytest.mark.e2e
def test_anonymous_browser_scan_waits_for_a_slow_page(im_module, fake_instagram, monkeypatch):
    monkeypatch.setattr(im_module, "FOLLOW_LIST_BROWSER_SCROLL_DELAY", 0.3)
    monkeypatch.setattr(im_module, "BROWSER_REFETCH_DELAY_MS", 20)
    monkeypatch.setattr(im_module, "BROWSER_NO_END_PATIENCE_SECONDS", 30)
    # Longer than three quiet scrolls at 0.3 seconds each, which is how long the scan used to wait
    fake_instagram.delays[25] = 4.0
    sign_in_anonymous_profile(im_module, FAKE_SESSION)
    profile = SimpleNamespace(username="paged.user", userid=1, followers=60, followees=0)

    names = [entry.username for entry in im_module.iter_browser_follow_list(anonymous_session_bot(), profile, "followers")]

    assert sorted(names) == FAKE_LIST_NAMES
