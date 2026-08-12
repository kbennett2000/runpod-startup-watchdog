"""Settings for one watchdog run.

Settings arrive from two places: command-line flags, and an optional TOML config file (TOML is a
plain-text settings format, read here with Python's built-in `tomllib`). A flag always beats the
file, and the file beats the built-in default.

This module makes no network calls and reads no environment variables. It merges, it validates, and
it hands back one frozen `Settings` object. See docs/adr/0002-settings-resolution.md for why the
rules are the way they are.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Every setting, in the order it is reported to the user. The name here is the TOML key; the
# matching long flag is the same name with dashes (pod_id -> --pod-id).
SETTING_NAMES: tuple[str, ...] = (
    "pod_id",
    "max_minutes",
    "port",
    "success_phrase",
    "failure_phrase",
    "retry",
    "terminate",
    "dry_run",
)

# At least one of these must be set. Without one, the tool has no way to tell a pod that came up
# healthy from one that is still broken, and a watchdog that cannot tell the difference is worse
# than no watchdog.
HEALTH_SIGNAL_NAMES: tuple[str, ...] = ("port", "success_phrase", "failure_phrase")

# Settings that neither a flag nor the file has to supply. `pod_id` and `max_minutes` are absent
# on purpose: there is no safe default for which pod to watch or how long to wait.
DEFAULTS: dict[str, Any] = {
    "port": None,
    "success_phrase": None,
    "failure_phrase": None,
    "retry": False,
    "terminate": False,
    "dry_run": False,
}

_BOOL_SETTINGS = frozenset({"retry", "terminate", "dry_run"})
_STR_SETTINGS = frozenset({"pod_id", "success_phrase", "failure_phrase"})

_EXPECTED_TYPE_TEXT: dict[str, str] = {
    "pod_id": "a string",
    "max_minutes": "a number",
    "port": "a whole number",
    "success_phrase": "a string",
    "failure_phrase": "a string",
    "retry": "true or false",
    "terminate": "true or false",
    "dry_run": "true or false",
}


@dataclass(frozen=True)
class Settings:
    """One fully resolved, already validated run configuration."""

    pod_id: str
    max_minutes: float
    port: int | None
    success_phrase: str | None
    failure_phrase: str | None
    retry: bool
    terminate: bool
    dry_run: bool


class ConfigError(Exception):
    """One or more problems with the settings.

    `errors` holds one plain sentence per problem so the caller can print every problem at once,
    instead of making the user discover them one run at a time.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def label(name: str) -> str:
    """`pod_id` -> `pod-id`: the name as the user types it, for error and report text."""
    return name.replace("_", "-")


def flag(name: str) -> str:
    """`pod_id` -> `--pod-id`."""
    return "--" + label(name)


def _has_expected_type(name: str, value: Any) -> bool:
    if name in _BOOL_SETTINGS:
        return isinstance(value, bool)
    if name in _STR_SETTINGS:
        return isinstance(value, str)
    # In Python `isinstance(True, int)` is True, so booleans have to be turned away before the
    # number checks or `port = true` would be accepted as port 1.
    if isinstance(value, bool):
        return False
    if name == "port":
        return isinstance(value, int)
    if name == "max_minutes":
        return isinstance(value, (int, float))
    raise AssertionError(f"no type rule for setting {name!r}")


def load_toml_file(path: str | Path) -> dict[str, Any]:
    """Read the config file and check its shape.

    Raises `ConfigError` if the file is missing, is not valid TOML, holds a key that is not a
    setting, or holds a value of the wrong type. Unknown keys are a hard error rather than an
    ignored line: a typo such as `max_minute = 10` that was silently skipped would leave the run
    with no time limit at all, which is the exact failure this tool exists to catch.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError([f"config file not found: {path}"]) from None
    except UnicodeDecodeError:
        raise ConfigError([f"config file is not valid UTF-8 text: {path}"]) from None
    except OSError as exc:
        raise ConfigError([f"config file could not be read: {path}: {exc.strerror}"]) from None

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"config file is not valid TOML: {path}: {exc}"]) from None

    errors: list[str] = []
    known = ", ".join(SETTING_NAMES)
    for key in data:
        if key not in SETTING_NAMES:
            errors.append(f"{path}: '{key}' is not a setting. The settings are: {known}")
    for name in SETTING_NAMES:
        if name in data and not _has_expected_type(name, data[name]):
            errors.append(
                f"{path}: '{name}' must be {_EXPECTED_TYPE_TEXT[name]}, got {data[name]!r}"
            )
    if errors:
        raise ConfigError(errors)
    return dict(data)


def merge(file_values: dict[str, Any], flag_values: dict[str, Any]) -> dict[str, Any]:
    """Layer the two sources: a flag beats the file, the file beats the built-in default.

    A flag the user did not type arrives here as `None`. That sentinel is what makes "flag absent"
    different from "flag set to false" — without it, argparse's `False` default for a switch such
    as `--terminate` would quietly overwrite a `terminate = true` in the config file on every run.
    """
    merged = dict(DEFAULTS)
    for name in SETTING_NAMES:
        if flag_values.get(name) is not None:
            merged[name] = flag_values[name]
        elif name in file_values:
            merged[name] = file_values[name]
    return merged


def validate(values: dict[str, Any]) -> list[str]:
    """Return every problem with the merged settings, one plain sentence each."""
    errors: list[str] = []

    pod_id = values.get("pod_id")
    if pod_id is None:
        errors.append(_required(name="pod_id"))
    elif not pod_id.strip():
        errors.append("pod-id cannot be empty")

    max_minutes = values.get("max_minutes")
    if max_minutes is None:
        errors.append(_required(name="max_minutes"))
    elif max_minutes <= 0:
        errors.append(f"max-minutes must be greater than zero, got {max_minutes}")

    if all(values.get(name) is None for name in HEALTH_SIGNAL_NAMES):
        errors.append(
            "at least one health signal is required: pass --port, --success-phrase, or "
            "--failure-phrase (or set port, success_phrase, or failure_phrase in the config file)"
        )

    port = values.get("port")
    if port is not None and not 1 <= port <= 65535:
        errors.append(f"port must be between 1 and 65535, got {port}")

    for name in ("success_phrase", "failure_phrase"):
        phrase = values.get(name)
        # A blank phrase would match every log line, so the pod would look healthy the moment its
        # first line of output arrived and the watchdog would never stop it.
        if phrase is not None and not phrase.strip():
            errors.append(f"{label(name)} cannot be empty")

    return errors


def _required(name: str) -> str:
    return f"{label(name)} is required: pass {flag(name)} or set {name} in the config file"


def resolve(
    flag_values: dict[str, Any], config_path: str | Path | None = None
) -> Settings:
    """Turn raw flag values and an optional config file path into one validated `Settings`.

    This is the only function the command line needs to call.
    """
    file_values = load_toml_file(config_path) if config_path is not None else {}
    merged = merge(file_values, flag_values)

    errors = validate(merged)
    if errors:
        raise ConfigError(errors)

    return Settings(
        pod_id=merged["pod_id"].strip(),
        # Whole minutes arrive from TOML as an int; store one type so later cycles do not have to
        # care which. Phrases are deliberately NOT stripped — whitespace can be part of the match.
        max_minutes=float(merged["max_minutes"]),
        port=merged["port"],
        success_phrase=merged["success_phrase"],
        failure_phrase=merged["failure_phrase"],
        retry=merged["retry"],
        terminate=merged["terminate"],
        dry_run=merged["dry_run"],
    )
