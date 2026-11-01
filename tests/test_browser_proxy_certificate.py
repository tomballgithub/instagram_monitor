"""Offline tests for the stop on a proxy CA the browser does not trust.

Playwright has no CA-file option, so on Windows Chromium trusts the current user's Trusted Root store and never reads
PROXY_CERT_PATH, which only the HTTP clients use. A run that sends the browser through a proxy stops at startup, in red,
when the CA in that file is not in the Windows store, and doctor reports the same condition as a failure. With the CA in
the store the run goes on and doctor says the browser trusts it.
"""

import hashlib
import ssl

import pytest

# A public root certificate, only ever parsed for its thumbprint
PEM = """-----BEGIN CERTIFICATE-----
MIIBtjCCAVugAwIBAgITBmyf1XSXNmY/Owua2eiedgPySjAKBggqhkjOPQQDAjA5
MQswCQYDVQQGEwJVUzEPMA0GA1UEChMGQW1hem9uMRkwFwYDVQQDExBBbWF6b24g
Um9vdCBDQSAzMB4XDTE1MDUyNjAwMDAwMFoXDTQwMDUyNjAwMDAwMFowOTELMAkG
A1UEBhMCVVMxDzANBgNVBAoTBkFtYXpvbjEZMBcGA1UEAxMQQW1hem9uIFJvb3Qg
Q0EgMzBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABCmXp8ZBf8ANm+gBG1bG8lKl
ui2yEujSLtf6ycXYqm0fc4E7O5hrOXwzpcVOho6AF2hiRVd9RFgdszflZwjrZt6j
QjBAMA8GA1UdEwEB/wQFMAMBAf8wDgYDVR0PAQH/BAQDAgGGMB0GA1UdDgQWBBSr
ttvXBp43rDCGB5Fwx5zEGbF4wDAKBggqhkjOPQQDAgNJADBGAiEA4IWSoxe3jfkr
BqWTrBqYaGFy+uGh0PsceGCmQ5nFuMQCIQCcAu/xlJyzlvnrxir4tiz+OpAUFteM
YyRIHN8wfdVoOw==
-----END CERTIFICATE-----
"""


@pytest.fixture
# Writes the certificate to a file and returns its path and thumbprint
def certificate(tmp_path):
    path = tmp_path / "proxy-ca.pem"
    path.write_text(PEM)
    return str(path), hashlib.sha1(ssl.PEM_cert_to_DER_cert(PEM)).hexdigest().upper()


@pytest.fixture
# Configures a Windows run that sends the browser through a proxy with a CA file, whose Windows store lacks that CA
def browser_proxy(im_module, monkeypatch, certificate):
    monkeypatch.setattr(im_module, "PROXY_ENABLED", True)
    monkeypatch.setattr(im_module, "PROXY_URL", "http://proxy.example:8080")
    monkeypatch.setattr(im_module, "PROXY_CERT_PATH", certificate[0])
    monkeypatch.setattr(im_module, "FOLLOW_LIST_SOURCE", "browser")
    monkeypatch.setattr(im_module, "system", lambda: "Windows")
    monkeypatch.setattr(im_module, "PROXY_STARTUP_ERRORS", [], raising=False)
    monkeypatch.setattr(im_module, "windows_trusted_root_thumbprints", lambda: set())
    return certificate


class TestCondition:
    # Verifies the stop needs all five conditions, so any one missing leaves the run alone
    @pytest.mark.parametrize("name,value", [("PROXY_ENABLED", False), ("PROXY_CERT_PATH", ""), ("FOLLOW_LIST_SOURCE", "rest"), ("FOLLOW_LIST_SOURCE", "auto")])
    def test_each_missing_condition_clears_it(self, im_module, monkeypatch, browser_proxy, name, value):
        assert im_module.browser_proxy_certificate_missing() is True

        monkeypatch.setattr(im_module, name, value)

        assert im_module.browser_proxy_certificate_missing() is False
        assert all(detail != im_module.BROWSER_PROXY_CERT_DETAIL for detail, _fix in im_module.proxy_configuration_problems())

    # Verifies another operating system is left alone, since only Windows reads the trust store this describes
    @pytest.mark.parametrize("platform_name", ["Linux", "Darwin"])
    def test_other_systems_are_not_stopped(self, im_module, monkeypatch, browser_proxy, platform_name):
        monkeypatch.setattr(im_module, "system", lambda: platform_name)

        assert im_module.browser_proxy_certificate_missing() is False

    # Verifies a CA the Windows store holds clears the condition, which is the case the run must go on in
    def test_a_trusted_ca_clears_it(self, im_module, monkeypatch, browser_proxy):
        monkeypatch.setattr(im_module, "windows_trusted_root_thumbprints", lambda: {browser_proxy[1]})

        assert im_module.browser_proxy_certificate_applies() is True
        assert im_module.browser_proxy_certificate_missing() is False
        assert all(detail != im_module.BROWSER_PROXY_CERT_DETAIL for detail, _fix in im_module.proxy_configuration_problems())

    # Verifies a bundle is missing when any one certificate in it is absent from the store
    def test_one_absent_certificate_in_a_bundle_is_missing(self, im_module, monkeypatch, browser_proxy, tmp_path):
        bundle = tmp_path / "bundle.pem"
        bundle.write_text(PEM + PEM.replace("MIIBtjCC", "MIIBtjCD", 1))
        monkeypatch.setattr(im_module, "PROXY_CERT_PATH", str(bundle))
        monkeypatch.setattr(im_module, "windows_trusted_root_thumbprints", lambda: {browser_proxy[1]})
        monkeypatch.setattr(im_module, "certificate_file_thumbprints", lambda path: [browser_proxy[1], "F" * 40])

        assert im_module.browser_proxy_certificate_missing() is True

    # Verifies a store or file that cannot be read does not stop the run, since it cannot be said that the CA is missing
    @pytest.mark.parametrize("error", [AttributeError("no enum_certificates"), OSError("denied"), ValueError("not a certificate")])
    def test_an_unreadable_store_does_not_stop_the_run(self, im_module, monkeypatch, browser_proxy, error):
        # Raises what the Windows-only call does elsewhere or on a bad file
        def unavailable():
            raise error

        monkeypatch.setattr(im_module, "windows_trusted_root_thumbprints", unavailable)

        assert im_module.browser_proxy_certificate_missing() is False
        assert im_module.proxy_configuration_problems() == []

    # Verifies the problem is reported with the action that clears it, which is the import and not unassigning the setting
    def test_the_problem_names_the_fix(self, im_module, browser_proxy):
        problems = dict(im_module.proxy_configuration_problems())

        fix = problems[im_module.BROWSER_PROXY_CERT_DETAIL]
        assert f'certutil -user -addstore Root "{browser_proxy[0]}"' in fix
        assert "Keep PROXY_CERT_PATH set" in fix


class TestStartupStop:
    @pytest.fixture(autouse=True)
    # Gives startup its own copies of what it records, so the proxy and secret settings it sets reach no other test
    def isolated_startup(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "SECRET_SOURCES", {})
        monkeypatch.setattr(im_module, "PROXY_STARTUP_ERRORS", [], raising=False)
        for name in ("PROXY_ENABLED", "PROXY_URL", "PROXY_CERT_PATH", "FOLLOW_LIST_SOURCE"):
            monkeypatch.setattr(im_module, name, getattr(im_module, name))

    # Runs startup with the command line and returns what it printed and its exit code
    @staticmethod
    def start(im_module, monkeypatch, capsys, *arguments):
        monkeypatch.setattr(im_module.sys, "argv", ["instagram_monitor.py", "someuser", "--config-file", "none", "--env-file", "none", *arguments])
        monkeypatch.setattr(im_module, "clear_screen", lambda *args, **kwargs: None)
        with pytest.raises(SystemExit) as stop:
            im_module.run_main()
        return capsys.readouterr().out, stop.value.code

    # The command line that turns on the browser source and a proxy with the certificate
    @staticmethod
    def browser_arguments(certificate, source="browser"):
        return ["--follow-list-source", source, "--enable-proxy", "--proxy-url", "http://proxy.example:8080", "--proxy-cert", certificate[0], "--no-color"]

    # Verifies a run whose store lacks the CA stops, says why, and gives the import
    def test_the_run_stops_with_the_reason(self, im_module, monkeypatch, capsys, certificate):
        monkeypatch.setattr(im_module, "system", lambda: "Windows")
        monkeypatch.setattr(im_module, "windows_trusted_root_thumbprints", lambda: set())

        output, code = self.start(im_module, monkeypatch, capsys, *self.browser_arguments(certificate))

        assert code == 1
        # The box wraps its text, so each phrase is checked on its own
        assert "The proxy CA in PROXY_CERT_PATH is not in the Windows Trusted Root" in output
        assert f'certutil -user -addstore Root "{certificate[0]}"' in output
        assert "browser-proxy-certificates" in output
        assert "Keep PROXY_CERT_PATH set" in output

    # Verifies the stop is in red when colour output is on, the way the cookie warning is
    def test_the_stop_is_red_with_colour_on(self, im_module, monkeypatch, capsys, browser_proxy):
        monkeypatch.setattr(im_module, "COLOR_ENABLED", True)

        im_module.print_browser_proxy_certificate_error()

        output = capsys.readouterr().out
        red = f"\033[{im_module._STYLE_CODES['red']}m"
        assert output.count(red) == len([line for line in output.splitlines() if line.strip()])
        assert "*" * 69 in output

    # Verifies the box has no colour codes when colour output is off
    def test_the_stop_is_plain_with_colour_off(self, im_module, monkeypatch, capsys, browser_proxy):
        monkeypatch.setattr(im_module, "COLOR_ENABLED", False)

        im_module.print_browser_proxy_certificate_error()

        assert "\033[" not in capsys.readouterr().out

    # Verifies another source is not stopped by the same settings, since the proxy check has nothing to say about it
    def test_another_source_is_not_stopped(self, im_module, monkeypatch, certificate):
        monkeypatch.setattr(im_module, "system", lambda: "Windows")
        monkeypatch.setattr(im_module, "PROXY_ENABLED", True)
        monkeypatch.setattr(im_module, "PROXY_URL", "http://proxy.example:8080")
        monkeypatch.setattr(im_module, "PROXY_CERT_PATH", certificate[0])
        monkeypatch.setattr(im_module, "FOLLOW_LIST_SOURCE", "rest")
        monkeypatch.setattr(im_module, "windows_trusted_root_thumbprints", lambda: set())

        assert im_module.proxy_configuration_problems() == []

    # Verifies the commands that exist to explain a configuration still run, and record the problem for the report
    def test_doctor_is_not_stopped(self, im_module, monkeypatch, capsys, certificate):
        seen = {}
        monkeypatch.setattr(im_module, "system", lambda: "Windows")
        monkeypatch.setattr(im_module, "windows_trusted_root_thumbprints", lambda: set())
        monkeypatch.setattr(im_module, "run_doctor", lambda *args, **kwargs: seen.setdefault("reached", True) and 0)

        self.start(im_module, monkeypatch, capsys, "--doctor", *self.browser_arguments(certificate))

        assert seen.get("reached") is True
        assert any(detail == im_module.BROWSER_PROXY_CERT_DETAIL for detail, _fix in im_module.PROXY_STARTUP_ERRORS)


class TestDoctor:
    # Verifies doctor fails the condition, with the import as the fix
    def test_a_missing_ca_is_a_failure_with_the_import(self, im_module, browser_proxy):
        rows = im_module.doctor_proxy_checks()

        assert [row.status for row in rows] == ["FAIL"]
        assert rows[0].label == im_module.BROWSER_PROXY_CERT_DETAIL
        assert f'certutil -user -addstore Root "{browser_proxy[0]}"' in rows[0].advice.fix
        assert "browser-proxy-certificates" in rows[0].advice.fix

    # Verifies doctor says when the CA is already loaded into the Windows store
    def test_a_loaded_ca_passes_the_trust_row(self, im_module, monkeypatch, browser_proxy):
        monkeypatch.setattr(im_module, "windows_trusted_root_thumbprints", lambda: {browser_proxy[1]})

        rows = im_module.doctor_proxy_checks()

        assert [row.status for row in rows] == ["PASS", "PASS"]
        assert "go through a proxy" in rows[0].label
        assert rows[1].label == "The browser trusts the proxy CA" and browser_proxy[1] in rows[1].detail

    # Verifies a store that cannot be read is skipped, not reported as missing
    def test_an_unreadable_store_is_skipped(self, im_module, monkeypatch, browser_proxy):
        # Raises what the Windows-only call does elsewhere
        def unavailable():
            raise AttributeError("no enum_certificates")

        monkeypatch.setattr(im_module, "windows_trusted_root_thumbprints", unavailable)

        rows = im_module.doctor_proxy_checks()

        assert [row.status for row in rows] == ["PASS", "SKIP"]

    # Verifies doctor still names the failure after startup switched the proxy off, which it does for the reporting commands
    def test_the_failure_survives_a_recorded_startup_error(self, im_module, monkeypatch, browser_proxy):
        monkeypatch.setattr(im_module, "PROXY_STARTUP_ERRORS", [(im_module.BROWSER_PROXY_CERT_DETAIL, im_module.browser_proxy_certificate_fix())])
        monkeypatch.setattr(im_module, "PROXY_ENABLED", False)

        rows = im_module.doctor_proxy_checks()

        assert [row.status for row in rows] == ["FAIL"] and rows[0].label == im_module.BROWSER_PROXY_CERT_DETAIL

    # Verifies no trust row appears for a run the condition does not describe
    def test_no_trust_row_without_the_condition(self, im_module, monkeypatch, browser_proxy):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_SOURCE", "rest")

        assert [row.status for row in im_module.doctor_proxy_checks()] == ["PASS"]


class TestCertificateFiles:
    # Verifies a PEM file and the same certificate as DER give one thumbprint, and a bundle gives one per certificate
    def test_thumbprints_of_pem_der_and_bundles(self, im_module, tmp_path, certificate):
        der = tmp_path / "proxy-ca.cer"
        der.write_bytes(ssl.PEM_cert_to_DER_cert(PEM))
        bundle = tmp_path / "bundle.pem"
        bundle.write_text(PEM + PEM)

        assert im_module.certificate_file_thumbprints(certificate[0]) == [certificate[1]]
        assert im_module.certificate_file_thumbprints(str(der)) == [certificate[1]]
        assert im_module.certificate_file_thumbprints(str(bundle)) == [certificate[1], certificate[1]]
