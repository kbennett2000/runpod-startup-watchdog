"""The watch loop: poll one pod, reach a verdict, act on it.

This is the part that decides. It polls the pod's status and its log, checks the user's health
signals against a clock, and produces one of four outcomes: healthy, broken, out of time, or a tool
error. On the middle two it stops the pod, or terminates it when the user asked for that.

Every rule here — what "the port answers" means, what "repeating" means, which signal wins when two
disagree, what `--retry` does, and what each exit code means — is settled in
docs/adr/0004-health-verdicts.md, which also records what was read out of Runpod's own runpodctl to
get here. This module is the implementation of that document; the reasoning lives there.

Nothing in this module sleeps or reads the clock directly. `now`, `sleep`, and `probe` are all
arguments, which is how the tests run a ten-minute watch instantly and with nothing on the wire.
"""

from __future__ import annotations

import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TextIO

from .api import (
    AuthError,
    ConflictError,
    ForbiddenError,
    LogEvent,
    MissingApiKeyError,
    PodNotFoundError,
    RunpodError,
)
from .config import Settings

# How often to poll. runpodctl uses 5 seconds and writes down why it is not 1: "a 10 minute wait at
# 1s is 600 api calls per created pod, and no readiness signal here changes that fast." The same
# reasoning holds at 10.
POLL_SECONDS = 10.0

# How many times the failure phrase must appear before the pod is called broken. Not a flag: see
# ADR-0004. Two is the smallest number that is a repeat at all.
FAILURE_REPEATS = 2

# Seconds to wait for a TCP connection to the pod's port.
PROBE_TIMEOUT = 5.0

# How long each poll spends collecting log events before moving on.
LOG_BATCH_SECONDS = 3.0

# (connect, read) seconds for the client the watch loop uses. The read half is shorter than the
# module default on purpose: ADR-0003 records that a log read returns only when the stream goes
# idle, so a silent pod would otherwise stall a poll for longer than the interval between polls.
WATCH_LOG_TIMEOUT = (10.0, 5.0)

# Log lines to read back on the very first poll, so a phrase printed a moment before the watchdog
# started is not missed. Every later read resumes from where the last one stopped.
LOG_BACKFILL = 100

# A pod missing from one read is an unknown state, not a verdict. runpodctl's rule and its reason:
# every other anomaly is tolerated, so declaring a live pod deleted on a single read would state a
# guess as fact.
MISSES_BEFORE_GONE = 2

# How long `--retry` waits for a stopped pod to become startable again before giving up.
RESTART_WAIT_SECONDS = 120.0

# The statuses a pod does not come back from on its own.
TERMINAL_STATUSES = frozenset({"EXITED", "ERROR", "TERMINATED"})

# Every status the API can report, longest first, so the status column in the printed lines has a
# fixed width: PROVISIONING, STARTING, RUNNING, EXITED, ERROR, TERMINATED.
_STATUS_WIDTH = len("PROVISIONING")

# Exit codes. 0 and 2 are already in use (2 is argparse's own code for a bad command line, so a bad
# flag and a bad config file report the same way); these continue upward from there.
EXIT_HEALTHY = 0
EXIT_TIMEOUT = 3
EXIT_FAILURE_SIGNAL = 4
EXIT_TOOL_ERROR = 5

# Verdict names, used in the Outcome and in what gets printed.
VERDICT_HEALTHY = "healthy"
VERDICT_TIMEOUT = "timeout"
VERDICT_FAILURE = "failure"
VERDICT_ERROR = "error"

# Errors that no amount of polling fixes, so they end the run immediately. Everything else — a 404,
# a 429, a 5xx, a dropped connection — is an unknown state that the loop survives, because the pod
# exists and is billing and giving up early is the expensive answer. ADR-0004.
FATAL_ERRORS = (MissingApiKeyError, AuthError, ForbiddenError)


@dataclass(frozen=True)
class Outcome:
    """What one watchdog run concluded, and what it did about it."""

    verdict: str
    detail: str
    action: str
    exit_code: int


# --- reading a pod ------------------------------------------------------------------------------


def declared_ports(pod: dict[str, Any]) -> list[int]:
    """The port numbers a pod says it exposes.

    The API publishes these as strings shaped `port/protocol` — `"8888/http"`, `"22/tcp"`. Anything
    that does not parse is skipped rather than raised: this feeds a warning, and a warning that
    crashed the watchdog would leave the pod it was guarding running.
    """
    entries = pod.get("ports")
    if not isinstance(entries, list):
        return []

    numbers: list[int] = []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        try:
            numbers.append(int(entry.split("/", 1)[0].strip()))
        except ValueError:
            continue
    return numbers


def declared_ports_text(pod: dict[str, Any]) -> str:
    """The pod's exposed-port list as the user would want to read it back."""
    entries = pod.get("ports")
    if isinstance(entries, list):
        shown = [str(entry) for entry in entries if isinstance(entry, (str, int))]
        if shown:
            return ", ".join(shown)
    return "nothing"


def public_mapping(pod: dict[str, Any], port: int) -> tuple[str, int] | None:
    """The public address Runpod forwards to `port` on this pod, or None if there is not one yet.

    `runtime` is documented "Null when the pod is not RUNNING", and `runtime.ports` is the only
    place a public mapping appears. So None is the normal answer for most of a startup, and callers
    must read it as "not ready yet", never as a fault.
    """
    runtime = pod.get("runtime")
    if not isinstance(runtime, dict):
        return None
    entries = runtime.get("ports")
    if not isinstance(entries, list):
        return None

    for entry in entries:
        if not isinstance(entry, dict) or entry.get("private") != port:
            continue
        ip = entry.get("ip")
        public = entry.get("public")
        # Both are nullable in the schema: a port can be mapped without being publicly routable.
        # `isinstance(True, int)` is True in Python, so booleans are turned away explicitly.
        if isinstance(ip, str) and ip and isinstance(public, int) and not isinstance(public, bool):
            return ip, public
    return None


def tcp_probe(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> str | None:
    """Open a TCP connection and close it. Returns None if it answered, or a short reason if not.

    A field saying the port is mapped is not the same as the port answering. runpodctl verified that
    against production — the API reported port 22 mapped and public while a connection to it was
    still refused, because the image ran no sshd — so this opens a real connection. ADR-0004.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as exc:
        return str(exc).lower() or "connection refused"


def _clock(seconds: float) -> str:
    """Elapsed seconds as `m:ss`, for the status lines."""
    whole = max(0, int(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


# --- the loop -----------------------------------------------------------------------------------


class _Watcher:
    """One watchdog run. Built by `watch`, which is the public way in."""

    def __init__(
        self,
        settings: Settings,
        client: Any,
        *,
        now: Callable[[], float],
        sleep: Callable[[float], None],
        probe: Callable[..., str | None],
        out: TextIO,
    ) -> None:
        self.settings = settings
        self.client = client
        self.now = now
        self.sleep = sleep
        self.probe = probe
        self.out = out

        self.pod_id = settings.pod_id
        self.window_seconds = settings.max_minutes * 60.0
        self.wants_logs = settings.success_phrase is not None or settings.failure_phrase is not None

        # Per-window state, set up by _reset_window.
        self.started = 0.0
        self.deadline = 0.0
        self.failure_hits = 0
        self.phrase_seen = False
        self.port_ok = False
        self.misses = 0
        self.last_note = "nothing observed yet"

        # Whole-run state, deliberately outside the window reset.
        self.retried = False
        self.port_warned = False
        self.last_event_id: str | None = None
        self.first_log_read = True

    # -- output

    def _say(self, line: str) -> None:
        print(line, file=self.out)

    # -- window bookkeeping

    def _reset_window(self) -> None:
        """Start the clock and the evidence over.

        `last_event_id` and `first_log_read` are deliberately not reset. A retry resumes the log
        stream where the previous window stopped, so the crashes that ended attempt one are not
        counted again against attempt two — which would make the retry decide nothing.
        """
        self.started = self.now()
        self.deadline = self.started + self.window_seconds
        self.failure_hits = 0
        self.phrase_seen = False
        self.port_ok = False
        self.misses = 0
        self.last_note = "nothing observed yet"

    # -- one poll's worth of reading

    def _drain_logs(self) -> list[LogEvent]:
        """Collect whatever the log stream has produced since the last poll.

        The first read backfills a little history; every read after it resumes from the last event
        id, so no line is counted twice. If Runpod sends events without ids, later reads ask for
        live-only rather than repeating the backfill.
        """
        if not self.wants_logs:
            return []

        kwargs: dict[str, Any] = {}
        if self.last_event_id is not None:
            kwargs["last_event_id"] = self.last_event_id
        elif self.first_log_read:
            kwargs["tail"] = LOG_BACKFILL
        else:
            kwargs["tail"] = 0

        try:
            events = self.client.read_pod_logs(
                self.pod_id, seconds=LOG_BATCH_SECONDS, **kwargs
            )
        except FATAL_ERRORS:
            raise
        except RunpodError:
            # A log read that failed is one poll with no new lines. The pod read is the signal that
            # matters, and this loop must not end because the log stream hiccupped.
            return []

        self.first_log_read = False
        for event in events:
            if event.event_id:
                self.last_event_id = event.event_id
        return list(events)

    def _count(self, events: list[LogEvent]) -> None:
        """Score new log lines against the user's phrases.

        A line is counted once however many times the phrase occurs inside it: the thing being
        counted is a repeat of the message, and a crash-loop prints its message on a new line each
        time round. Matching is plain, case-sensitive substring, because the settings layer keeps
        phrases exactly as typed — whitespace and case can be part of what makes a match specific.
        """
        for event in events:
            if self.settings.failure_phrase and self.settings.failure_phrase in event.line:
                self.failure_hits += 1
            if self.settings.success_phrase and self.settings.success_phrase in event.line:
                self.phrase_seen = True

    def _check_port(self, pod: dict[str, Any]) -> str:
        """Probe the port if it has not already answered. Returns the note for the status line.

        Once a signal has been satisfied it stays satisfied for the window. The setting reads "the
        pod counts as healthy once this port answers", and latching is also what makes two signals
        combinable: without it they would have to be true on the same poll, which nothing guarantees.
        """
        port = self.settings.port
        if port is None:
            return ""
        if self.port_ok:
            return "answered"

        mapping = public_mapping(pod, port)
        if mapping is None:
            return "no public mapping published yet"

        host, public = mapping
        reason = self.probe(host, public, PROBE_TIMEOUT)
        if reason is None:
            self.port_ok = True
            return f"answered at {host}:{public}"
        return f"mapped to {host}:{public} but not answering: {reason}"

    def _warn_about_undeclared_port(self, pod: dict[str, Any]) -> Outcome | None:
        """Say so when `--port` names a port the pod never exposed.

        The message quotes the pod's own list, so the fix is visible in the same line as the
        complaint. When the port is the only success signal the run cannot end any way except
        stopping a pod the tool was never able to observe, so it refuses instead, having changed
        nothing. ADR-0004.
        """
        port = self.settings.port
        if port is None or self.port_warned:
            return None
        self.port_warned = True

        exposed = declared_ports(pod)
        if not exposed or port in exposed:
            return None

        problem = (
            f"pod {self.pod_id} does not publish port {port}. "
            f"It publishes: {declared_ports_text(pod)}"
        )
        port_is_only_signal = (
            self.settings.success_phrase is None and self.settings.failure_phrase is None
        )
        if port_is_only_signal:
            return Outcome(VERDICT_ERROR, problem, "none", EXIT_TOOL_ERROR)

        # Worth spelling out: success signals are ANDed, so a port that can never answer means a
        # healthy verdict is out of reach for the whole run, whatever the other signals do.
        self._say(
            f"warning: {problem}. That signal cannot fire, so this run can only end in a stop."
        )
        return None

    def _status_line(self, status: str, port_note: str) -> str:
        # Padded to the longest status name so the columns line up while the lines scroll past.
        parts = [f"[{_clock(self.now() - self.started)}] {status.ljust(_STATUS_WIDTH)}"]
        if self.settings.port is not None:
            parts.append(f"port {self.settings.port}: {port_note}")
        if self.settings.success_phrase is not None:
            parts.append(f"success phrase: {'seen' if self.phrase_seen else 'not seen'}")
        if self.settings.failure_phrase is not None:
            parts.append(f"failure phrase: {self.failure_hits} of {FAILURE_REPEATS}")
        return "  |  ".join(parts)

    def _success_signals_met(self) -> bool:
        """True when every success signal the user configured has fired.

        All of them, not any of them. Each signal alone is weak in its own direction — a port can
        answer before the workload is up, a phrase can print before the port binds — so a user who
        set both meant both. Setting one signal is how you ask for one. ADR-0004.
        """
        signals: list[bool] = []
        if self.settings.port is not None:
            signals.append(self.port_ok)
        if self.settings.success_phrase is not None:
            signals.append(self.phrase_seen)
        return bool(signals) and all(signals)

    # -- one watch window, no action taken

    def _window(self) -> Outcome:
        """Poll until something is decided. Returns a verdict; acting on it is the caller's job."""
        while True:
            pod: dict[str, Any] | None = None
            status = "unknown"
            note = ""

            try:
                pod = self.client.get_pod(self.pod_id)
            except FATAL_ERRORS as exc:
                return Outcome(VERDICT_ERROR, str(exc), "none", EXIT_TOOL_ERROR)
            except PodNotFoundError as exc:
                self.misses += 1
                if self.misses >= MISSES_BEFORE_GONE:
                    return Outcome(
                        VERDICT_ERROR,
                        f"Pod {self.pod_id} is not there. {exc}",
                        "none",
                        EXIT_TOOL_ERROR,
                    )
                note = "pod not listed in that read"
            except RunpodError as exc:
                # Rate limits, server errors, and dropped connections are an unknown state, not a
                # verdict. The pod is still billing, so the loop keeps watching and carries the
                # reason into the status line and into the timeout message.
                note = str(exc)

            if pod is not None:
                self.misses = 0
                refusal = self._warn_about_undeclared_port(pod)
                if refusal is not None:
                    return refusal
                status = str(pod.get("status") or "unknown")
                note = self._check_port(pod)

            self._count(self._drain_logs())

            # The line goes out before the verdict, not after it, so every poll reports exactly
            # once and a pod that is healthy on its first read still shows what made it healthy.
            # runpodctl learned the same thing: without the detail on the success line, "neither an
            # operator nor a test can tell which one fired".
            self.last_note = f"status {status}" + (f", {note}" if note else "")
            self._say(self._status_line(status, note))
            stamp = f"[{_clock(self.now() - self.started)}]"

            # The precedence ladder, in the order ADR-0004 fixes. First match wins.
            if self.settings.failure_phrase is not None and self.failure_hits >= FAILURE_REPEATS:
                detail = (
                    f"the failure phrase {self.settings.failure_phrase!r} appeared "
                    f"{self.failure_hits} times"
                )
                self._say(f"{stamp} {detail}")
                return Outcome(VERDICT_FAILURE, detail, "none", EXIT_FAILURE_SIGNAL)

            if status in TERMINAL_STATUSES:
                detail = f"the pod reached {status} on its own"
                self._say(f"{stamp} {detail}")
                return Outcome(VERDICT_FAILURE, detail, "none", EXIT_FAILURE_SIGNAL)

            if self._success_signals_met():
                detail = "every success signal fired"
                self._say(f"{stamp} {detail}")
                return Outcome(VERDICT_HEALTHY, detail, "none", EXIT_HEALTHY)

            if self.now() >= self.deadline:
                minutes = self.settings.max_minutes
                return Outcome(
                    VERDICT_TIMEOUT,
                    f"the pod did not become healthy within {minutes:g} minutes; "
                    f"last known state: {self.last_note}",
                    "none",
                    EXIT_TIMEOUT,
                )

            # Never sleep past the deadline: the tool should act at the limit the user set, not one
            # whole poll after it.
            self.sleep(min(POLL_SECONDS, max(0.0, self.deadline - self.now())))

    # -- restarting for a retry

    def _restart(self) -> str | None:
        """Stop the pod, wait for it to become startable, start it. Returns a problem or None.

        REST v2's `restart` action needs a RUNNING pod, and a pod that failed to start usually is
        not one, so this is stop-then-start. It waits for the pod's own `actions` list to offer
        `start` rather than guessing when a stop has finished. ADR-0004.
        """
        try:
            self.client.stop_pod(self.pod_id)
        except ConflictError:
            pass  # Already stopped. That is where we were trying to get to.
        except RunpodError as exc:
            return f"could not stop pod {self.pod_id} for the retry: {exc}"

        deadline = self.now() + RESTART_WAIT_SECONDS
        note = "no reading yet"
        while True:
            try:
                pod = self.client.get_pod(self.pod_id)
            except FATAL_ERRORS as exc:
                return f"could not read pod {self.pod_id} during the retry: {exc}"
            except RunpodError as exc:
                note = str(exc)
            else:
                actions = pod.get("actions")
                if isinstance(actions, list) and "start" in actions:
                    break
                note = f"status {pod.get('status')}"

            if self.now() >= deadline:
                return (
                    f"pod {self.pod_id} did not become startable within "
                    f"{int(RESTART_WAIT_SECONDS)} seconds ({note})"
                )
            self.sleep(min(POLL_SECONDS, max(0.0, deadline - self.now())))

        try:
            self.client.start_pod(self.pod_id)
        except RunpodError as exc:
            return f"could not start pod {self.pod_id} for the retry: {exc}"
        return None

    # -- acting on a verdict

    def _act(self, outcome: Outcome) -> Outcome:
        """Stop or terminate the pod, and report exactly what happened."""
        verb = "terminate" if self.settings.terminate else "stop"

        if self.settings.dry_run:
            self._say(f"dry run: would {verb} pod {self.pod_id}. Nothing was changed.")
            return Outcome(outcome.verdict, outcome.detail, f"would {verb}", outcome.exit_code)

        try:
            if self.settings.terminate:
                self.client.terminate_pod(self.pod_id)
                action = "terminated"
            else:
                self.client.stop_pod(self.pod_id)
                action = "stopped"
        except ConflictError:
            # The pod got there on its own between the last read and this call.
            action = "already stopped"
        except PodNotFoundError:
            action = "already gone"
        except RunpodError as exc:
            return Outcome(
                VERDICT_ERROR,
                f"could not {verb} pod {self.pod_id}, so it may still be running and billing: "
                f"{exc}",
                "none",
                EXIT_TOOL_ERROR,
            )

        self._say(f"{action} pod {self.pod_id}")
        return Outcome(outcome.verdict, outcome.detail, action, outcome.exit_code)

    # -- the whole run

    def run(self) -> Outcome:
        self._reset_window()
        outcome = self._window()

        if outcome.verdict in (VERDICT_HEALTHY, VERDICT_ERROR):
            return outcome

        if self.settings.retry and not self.retried:
            if self.settings.dry_run:
                self._say(
                    f"dry run: would restart pod {self.pod_id} and watch it for one more "
                    f"{self.settings.max_minutes:g} minute window."
                )
            else:
                self.retried = True
                self._say(f"Restarting pod {self.pod_id} for one more window.")
                problem = self._restart()
                if problem is not None:
                    return Outcome(VERDICT_ERROR, problem, "none", EXIT_TOOL_ERROR)
                self._reset_window()
                outcome = self._window()
                if outcome.verdict in (VERDICT_HEALTHY, VERDICT_ERROR):
                    return outcome

        return self._act(outcome)


def watch(
    settings: Settings,
    client: Any,
    *,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    probe: Callable[..., str | None] = tcp_probe,
    out: TextIO | None = None,
) -> Outcome:
    """Watch one pod through its startup and act on what happens.

    `client` is a `RunpodClient`, or anything with the same four calls. `now`, `sleep`, and `probe`
    are arguments so a test can run a ten-minute watch in no time and with nothing on the wire.
    """
    return _Watcher(
        settings,
        client,
        now=now,
        sleep=sleep,
        probe=probe,
        out=out if out is not None else sys.stdout,
    ).run()
