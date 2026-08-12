"""Settings resolution: reading the TOML file, layering flags over it, and validation.

No network, no environment variables, no real Runpod anything.
"""

from __future__ import annotations

import textwrap

import pytest

from runpod_watchdog import config
from runpod_watchdog.config import ConfigError


def write_config(tmp_path, text: str):
    path = tmp_path / "watchdog.toml"
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


def flags(**overrides):
    """A flag dictionary shaped the way argparse hands one over: every setting present, and
    every flag the user did not type set to None."""
    values = {name: None for name in config.SETTING_NAMES}
    values.update(overrides)
    return values


def errors_from(flag_values, config_path=None) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        config.resolve(flag_values, config_path)
    return caught.value.errors


# --- the two sources on their own -------------------------------------------------------------


def test_flags_alone_resolve():
    settings = config.resolve(
        flags(pod_id="abc123", max_minutes=10.0, port=8888, terminate=True)
    )

    assert settings.pod_id == "abc123"
    assert settings.max_minutes == 10.0
    assert settings.port == 8888
    assert settings.terminate is True


def test_config_file_alone_resolves(tmp_path):
    path = write_config(
        tmp_path,
        """
        pod_id = "from-file"
        max_minutes = 12
        port = 8888
        success_phrase = "Uvicorn running"
        failure_phrase = "CUDA error"
        retry = true
        terminate = true
        dry_run = true
        """,
    )

    settings = config.resolve(flags(), path)

    assert settings.pod_id == "from-file"
    assert settings.max_minutes == 12.0
    assert settings.port == 8888
    assert settings.success_phrase == "Uvicorn running"
    assert settings.failure_phrase == "CUDA error"
    assert settings.retry is True
    assert settings.terminate is True
    assert settings.dry_run is True


def test_defaults_apply_when_neither_source_sets_an_optional_setting():
    settings = config.resolve(flags(pod_id="abc123", max_minutes=10.0, port=8888))

    assert settings.success_phrase is None
    assert settings.failure_phrase is None
    assert settings.retry is False
    assert settings.terminate is False
    assert settings.dry_run is False


def test_no_config_file_is_fine():
    assert config.resolve(flags(pod_id="abc123", max_minutes=10.0, port=8888), None)


# --- precedence -------------------------------------------------------------------------------


def test_flag_beats_file_for_a_string(tmp_path):
    path = write_config(
        tmp_path,
        """
        pod_id = "from-file"
        max_minutes = 10
        port = 8888
        """,
    )

    settings = config.resolve(flags(pod_id="from-flag"), path)

    assert settings.pod_id == "from-flag"


def test_flag_beats_file_for_a_number(tmp_path):
    path = write_config(
        tmp_path,
        """
        pod_id = "abc123"
        max_minutes = 10
        port = 8888
        """,
    )

    settings = config.resolve(flags(max_minutes=2.5), path)

    assert settings.max_minutes == 2.5


@pytest.mark.parametrize(
    ("name", "toml_line", "expected"),
    [
        ("pod_id", 'pod_id = "from-file"', "from-file"),
        ("max_minutes", "max_minutes = 7", 7.0),
        ("port", "port = 9999", 9999),
        ("success_phrase", 'success_phrase = "ready"', "ready"),
        ("failure_phrase", 'failure_phrase = "boom"', "boom"),
        ("retry", "retry = true", True),
        ("terminate", "terminate = true", True),
        ("dry_run", "dry_run = true", True),
    ],
)
def test_file_value_survives_when_the_flag_is_absent(tmp_path, name, toml_line, expected):
    """The regression guard for the whole cycle, run once per setting.

    Every flag defaults to None rather than to False or 0, so "flag not typed" stays different
    from "flag set to false". If a default ever changes to a real value, this test fails for that
    setting instead of the tool silently overwriting the config file on every run.
    """
    # The three settings a valid file always needs, minus whichever one this run is testing, so
    # the line under test is the only place that key appears. TOML rejects a duplicate key.
    base = {
        "pod_id": 'pod_id = "abc123"',
        "max_minutes": "max_minutes = 10",
        "port": "port = 8888",
    }
    lines = [line for key, line in base.items() if key != name] + [toml_line]
    path = write_config(tmp_path, "\n".join(lines) + "\n")

    settings = config.resolve(flags(), path)

    assert getattr(settings, name) == expected


def test_switch_turned_off_on_the_command_line_beats_a_true_in_the_file(tmp_path):
    path = write_config(
        tmp_path,
        """
        pod_id = "abc123"
        max_minutes = 10
        port = 8888
        terminate = true
        """,
    )

    settings = config.resolve(flags(terminate=False), path)

    assert settings.terminate is False


def test_switch_turned_on_on_the_command_line_beats_a_false_in_the_file(tmp_path):
    path = write_config(
        tmp_path,
        """
        pod_id = "abc123"
        max_minutes = 10
        port = 8888
        terminate = false
        """,
    )

    settings = config.resolve(flags(terminate=True), path)

    assert settings.terminate is True


# --- validation -------------------------------------------------------------------------------


def test_pod_id_is_required():
    assert errors_from(flags(max_minutes=10.0, port=8888)) == [
        "pod-id is required: pass --pod-id or set pod_id in the config file"
    ]


def test_max_minutes_is_required():
    assert errors_from(flags(pod_id="abc123", port=8888)) == [
        "max-minutes is required: pass --max-minutes or set max_minutes in the config file"
    ]


def test_at_least_one_health_signal_is_required():
    assert errors_from(flags(pod_id="abc123", max_minutes=10.0)) == [
        "at least one health signal is required: pass --port, --success-phrase, or "
        "--failure-phrase (or set port, success_phrase, or failure_phrase in the config file)"
    ]


@pytest.mark.parametrize(
    "signal",
    [{"port": 8888}, {"success_phrase": "ready"}, {"failure_phrase": "boom"}],
)
def test_any_one_health_signal_satisfies_the_rule(signal):
    assert config.resolve(flags(pod_id="abc123", max_minutes=10.0, **signal))


def test_every_violation_is_reported_at_once():
    assert errors_from(flags()) == [
        "pod-id is required: pass --pod-id or set pod_id in the config file",
        "max-minutes is required: pass --max-minutes or set max_minutes in the config file",
        "at least one health signal is required: pass --port, --success-phrase, or "
        "--failure-phrase (or set port, success_phrase, or failure_phrase in the config file)",
    ]


@pytest.mark.parametrize("value", [0.0, -1.0])
def test_max_minutes_must_be_greater_than_zero(value):
    errors = errors_from(flags(pod_id="abc123", max_minutes=value, port=8888))

    assert errors == [f"max-minutes must be greater than zero, got {value}"]


@pytest.mark.parametrize("value", [0, 65536, -1])
def test_port_must_be_a_real_port_number(value):
    errors = errors_from(flags(pod_id="abc123", max_minutes=10.0, port=value))

    assert errors == [f"port must be between 1 and 65535, got {value}"]


@pytest.mark.parametrize("value", [1, 65535])
def test_port_edges_are_accepted(value):
    assert config.resolve(flags(pod_id="abc123", max_minutes=10.0, port=value)).port == value


def test_pod_id_cannot_be_blank():
    errors = errors_from(flags(pod_id="   ", max_minutes=10.0, port=8888))

    assert errors == ["pod-id cannot be empty"]


def test_pod_id_is_stripped():
    settings = config.resolve(flags(pod_id="  abc123  ", max_minutes=10.0, port=8888))

    assert settings.pod_id == "abc123"


@pytest.mark.parametrize("name", ["success_phrase", "failure_phrase"])
@pytest.mark.parametrize("value", ["", "   "])
def test_a_phrase_cannot_be_blank(name, value):
    """A blank phrase would match every log line, so the pod would look healthy on its first line
    of output and never be stopped. That is the failure this tool exists to prevent, so it is an
    error rather than a warning."""
    errors = errors_from(flags(pod_id="abc123", max_minutes=10.0, **{name: value}))

    assert errors == [f"{name.replace('_', '-')} cannot be empty"]


def test_whitespace_inside_a_phrase_is_kept():
    settings = config.resolve(
        flags(pod_id="abc123", max_minutes=10.0, success_phrase="  ready  ")
    )

    assert settings.success_phrase == "  ready  "


# --- config file shape ------------------------------------------------------------------------


def test_missing_config_file_is_an_error(tmp_path):
    missing = tmp_path / "nope.toml"

    errors = errors_from(flags(), missing)

    assert errors == [f"config file not found: {missing}"]


def test_invalid_toml_is_an_error(tmp_path):
    path = write_config(tmp_path, "pod_id = \n")

    errors = errors_from(flags(), path)

    assert len(errors) == 1
    assert errors[0].startswith(f"config file is not valid TOML: {path}: ")


def test_an_unknown_key_names_the_offending_key(tmp_path):
    """A typo such as `max_minute` that was silently ignored would leave the run with no time
    limit at all, so unknown keys fail loudly."""
    path = write_config(
        tmp_path,
        """
        pod_id = "abc123"
        max_minute = 10
        port = 8888
        """,
    )

    errors = errors_from(flags(), path)

    assert len(errors) == 1
    assert errors[0].startswith(f"{path}: 'max_minute' is not a setting. The settings are: ")


def test_several_unknown_keys_are_all_reported(tmp_path):
    path = write_config(
        tmp_path,
        """
        pod_id = "abc123"
        max_minute = 10
        sucess_phrase = "ready"
        """,
    )

    errors = errors_from(flags(), path)

    assert len(errors) == 2
    assert "'max_minute' is not a setting" in errors[0]
    assert "'sucess_phrase' is not a setting" in errors[1]


@pytest.mark.parametrize(
    ("toml_line", "message"),
    [
        ('max_minutes = "ten"', "'max_minutes' must be a number, got 'ten'"),
        ("port = true", "'port' must be a whole number, got True"),
        ("port = 88.5", "'port' must be a whole number, got 88.5"),
        ("pod_id = 5", "'pod_id' must be a string, got 5"),
        ('retry = "yes"', "'retry' must be true or false, got 'yes'"),
        ("success_phrase = 3", "'success_phrase' must be a string, got 3"),
        ("max_minutes = true", "'max_minutes' must be a number, got True"),
    ],
)
def test_a_wrong_type_in_the_file_is_an_error(tmp_path, toml_line, message):
    """`port = true` is the sharp one: in Python `isinstance(True, int)` is True, so a naive
    integer check would have accepted it as port 1."""
    path = write_config(tmp_path, toml_line + "\n")

    errors = errors_from(flags(), path)

    assert errors == [f"{path}: {message}"]


def test_file_shape_errors_are_reported_before_missing_settings(tmp_path):
    """A malformed file cannot be merged, so its own problems come first and alone. Reporting
    'pod-id is required' next to 'that key is a typo' would send the user the wrong way."""
    path = write_config(tmp_path, "pod_ide = \"abc123\"\n")

    errors = errors_from(flags(), path)

    assert len(errors) == 1
    assert "'pod_ide' is not a setting" in errors[0]


def test_an_empty_config_file_is_valid_on_its_own(tmp_path):
    path = write_config(tmp_path, "")

    settings = config.resolve(flags(pod_id="abc123", max_minutes=10.0, port=8888), path)

    assert settings.pod_id == "abc123"


# --- shape of the module ----------------------------------------------------------------------


def test_settings_are_frozen():
    settings = config.resolve(flags(pod_id="abc123", max_minutes=10.0, port=8888))

    with pytest.raises(Exception):
        settings.pod_id = "changed"


def test_every_setting_name_has_a_default_or_is_required():
    """Guard against a setting being added to SETTING_NAMES and then falling through resolve."""
    required = {"pod_id", "max_minutes"}

    assert set(config.DEFAULTS) | required == set(config.SETTING_NAMES)
