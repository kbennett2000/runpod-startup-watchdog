"""The command line: flag parsing, exit codes, the settings report, and the hand-off to the loop.

No network. Every test here runs with the watch loop stubbed out, and
`test_importing_the_package_touches_no_network` is the guard that keeps imports clean.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from runpod_watchdog import __version__, cli
from runpod_watchdog.watch import VERDICT_HEALTHY, Outcome


def write_config(tmp_path, text: str):
    path = tmp_path / "watchdog.toml"
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


VALID = ["--pod-id", "abc123", "--max-minutes", "10", "--port", "8888"]

HEALTHY = Outcome(VERDICT_HEALTHY, "every success signal fired", "none", 0)


class StubClient:
    """Stands in for RunpodClient. It cannot make a request, which is the point."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_real_watching(monkeypatch):
    """Nothing in this file may build a real client or run a real watch.

    These tests are about settings, reports and exit codes. The loop itself is covered by
    tests/test_watch.py against a fake client, so here it is replaced wholesale — which also means
    no test in this file can reach the network even if the settings are valid.
    """
    watched: list[tuple] = []
    monkeypatch.setattr(cli, "RunpodClient", StubClient)
    monkeypatch.setattr(
        cli,
        "watch",
        lambda settings, client, **kwargs: watched.append((settings, client)) or HEALTHY,
    )
    return watched


def row(out: str, name: str) -> str:
    """The value column of one row of the settings report, looked up by name rather than by
    counting spaces, so widening a label does not break every assertion."""
    for line in out.splitlines():
        stripped = line.strip()
        if stripped == name or stripped.startswith(name + " "):
            return stripped[len(name) :].strip()
    raise AssertionError(f"no row named {name!r} in:\n{out}")


# --- exit codes -------------------------------------------------------------------------------


def test_version_prints_the_version_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["--version"])

    assert caught.value.code == 0
    assert capsys.readouterr().out.strip() == __version__


def test_a_valid_run_exits_zero(capsys):
    assert cli.main(VALID) == 0
    assert "Settings for this run:" in capsys.readouterr().out


def test_a_settings_problem_exits_two_and_writes_to_stderr(capsys):
    assert cli.main([]) == cli.EXIT_CONFIG_ERROR

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "error: pod-id is required: pass --pod-id or set pod_id in the config file",
        "error: max-minutes is required: pass --max-minutes or set max_minutes in the config file",
        "error: at least one health signal is required: pass --port, --success-phrase, or "
        "--failure-phrase (or set port, success_phrase, or failure_phrase in the config file)",
    ]


@pytest.mark.parametrize("argv", [["--max-minutes", "soon"], ["--port", "http"]])
def test_a_value_that_is_not_a_number_is_rejected_by_the_parser(argv):
    with pytest.raises(SystemExit) as caught:
        cli.main(argv)

    assert caught.value.code == 2


# --- flags over file, end to end --------------------------------------------------------------


def test_a_flag_beats_the_config_file(tmp_path, capsys):
    path = write_config(
        tmp_path,
        """
        pod_id = "from-file"
        max_minutes = 10
        port = 8888
        """,
    )

    assert cli.main(["--config", str(path), "--pod-id", "from-flag"]) == 0

    out = capsys.readouterr().out
    assert row(out, "pod-id") == "from-flag"
    assert "from-file" not in out


def test_the_config_file_is_used_when_no_flag_is_typed(tmp_path, capsys):
    path = write_config(
        tmp_path,
        """
        pod_id = "from-file"
        max_minutes = 10
        port = 8888
        terminate = true
        """,
    )

    assert cli.main(["--config", str(path)]) == 0

    out = capsys.readouterr().out
    assert row(out, "pod-id") == "from-file"
    assert row(out, "terminate") == "yes"
    assert row(out, "config file") == str(path)


def test_the_off_switch_beats_a_true_in_the_config_file(tmp_path, capsys):
    path = write_config(
        tmp_path,
        """
        pod_id = "abc123"
        max_minutes = 10
        port = 8888
        terminate = true
        """,
    )

    assert cli.main(["--config", str(path), "--no-terminate"]) == 0

    assert row(capsys.readouterr().out, "terminate") == "no"


def test_the_off_switches_exist_in_help(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])

    out = capsys.readouterr().out
    for switch in ("--no-retry", "--no-terminate", "--no-dry-run"):
        assert switch in out


# --- the settings report ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"), [(10.0, "10"), (0.5, "0.5"), (1.0, "1"), (2.25, "2.25")]
)
def test_minutes_print_without_a_pointless_decimal(value, expected):
    assert cli.format_minutes(value) == expected


def test_the_report_names_every_setting(capsys):
    cli.main(VALID + ["--success-phrase", "ready", "--dry-run"])

    out = capsys.readouterr().out
    for name in (
        "pod-id",
        "max-minutes",
        "port",
        "success-phrase",
        "failure-phrase",
        "retry",
        "terminate",
        "dry-run",
        "config file",
    ):
        assert name in out


def test_unset_optional_settings_read_as_not_set(capsys):
    cli.main(VALID)

    out = capsys.readouterr().out
    assert row(out, "success-phrase") == "(not set)"
    assert row(out, "failure-phrase") == "(not set)"
    assert row(out, "config file") == "(none)"


def test_a_phrase_is_quoted_so_stray_spaces_are_visible(capsys):
    cli.main(["--pod-id", "abc123", "--max-minutes", "10", "--success-phrase", " ready "])

    assert row(capsys.readouterr().out, "success-phrase") == '" ready "'


# --- the hand-off to the watch loop -----------------------------------------------------------


def test_the_resolved_settings_are_handed_to_the_loop(no_real_watching, capsys):
    cli.main(VALID + ["--success-phrase", "ready", "--terminate"])

    assert len(no_real_watching) == 1
    settings, _ = no_real_watching[0]
    assert (settings.pod_id, settings.port, settings.success_phrase) == ("abc123", 8888, "ready")
    assert settings.terminate is True
    assert "Watching pod abc123. Time limit 10 minutes." in capsys.readouterr().out


def test_the_client_gets_the_shorter_log_timeout(no_real_watching):
    cli.main(VALID)

    _, client = no_real_watching[0]
    assert client.kwargs["log_timeout"] == cli.WATCH_LOG_TIMEOUT


def test_the_client_is_closed_even_when_the_loop_raises(monkeypatch):
    closed: list[StubClient] = []

    def remember(**kwargs):
        client = StubClient(**kwargs)
        closed.append(client)
        return client

    monkeypatch.setattr(cli, "RunpodClient", remember)
    monkeypatch.setattr(cli, "watch", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        cli.main(VALID)

    assert closed[0].closed is True


def test_the_loops_exit_code_is_the_tools_exit_code(monkeypatch):
    monkeypatch.setattr(
        cli, "watch", lambda *a, **k: Outcome("timeout", "ran out of time", "stopped", 3)
    )

    assert cli.main(VALID) == 3


def test_a_verdict_prints_what_happened(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "watch", lambda *a, **k: Outcome("failure", "the phrase repeated", "stopped", 4)
    )

    assert cli.main(VALID) == 4
    assert "Result: stopped, because the phrase repeated." in capsys.readouterr().out


def test_a_tool_error_goes_to_stderr(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "watch",
        lambda *a, **k: Outcome(
            "error", "No Runpod API key. Set the RUNPOD_API_KEY environment variable.", "none", 5
        ),
    )

    assert cli.main(VALID) == 5

    captured = capsys.readouterr()
    assert captured.err.strip() == (
        "error: No Runpod API key. Set the RUNPOD_API_KEY environment variable."
    )
    assert "Result:" not in captured.out


def test_a_healthy_run_says_nothing_was_stopped(capsys):
    assert cli.main(VALID) == 0
    assert "Nothing was stopped" in capsys.readouterr().out


# --- no network -------------------------------------------------------------------------------


def test_importing_the_package_touches_no_network():
    """CLAUDE.md forbids live API calls until the final proving run.

    Cycle 1 proved this by checking that no HTTP library was imported at all. Cycle 2 adds one on
    purpose, so that check would now be false. This proves the stronger thing instead: importing
    every module in the package opens no connection. The probe makes connecting and name lookup
    raise, so any import that tried to reach the network would fail the import.

    The connect methods are replaced rather than the socket class itself: `ssl` subclasses
    `socket.socket` at its own import, so replacing the class breaks importing `ssl` and the probe
    would fail for a reason that has nothing to do with this package.
    """
    probe = textwrap.dedent(
        """
        import socket

        def refuse(*args, **kwargs):
            raise AssertionError("import-time network access")

        socket.socket.connect = refuse
        socket.socket.connect_ex = refuse
        socket.create_connection = refuse
        socket.getaddrinfo = refuse

        import runpod_watchdog
        import runpod_watchdog.api
        import runpod_watchdog.cli
        import runpod_watchdog.config
        import runpod_watchdog.watch

        print("no network at import")
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "no network at import"
