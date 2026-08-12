"""The command line: flag parsing, exit codes, and the settings report.

No network. `test_the_tool_makes_no_network_machinery_available` is the guard that keeps it that
way for this cycle.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from runpod_watchdog import __version__, cli


def write_config(tmp_path, text: str):
    path = tmp_path / "watchdog.toml"
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


VALID = ["--pod-id", "abc123", "--max-minutes", "10", "--port", "8888"]


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


def test_the_report_says_watching_is_not_built_yet(capsys):
    cli.main(VALID)

    assert "Watching is not implemented yet" in capsys.readouterr().out


# --- no network -------------------------------------------------------------------------------


def test_the_tool_makes_no_network_machinery_available():
    """CLAUDE.md forbids live API calls until the final proving run. This cycle has no HTTP code
    at all, and importing the command line must not pull any in."""
    probe = "import runpod_watchdog.cli, sys; print('requests' in sys.modules)"

    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "False"
