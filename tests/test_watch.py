"""The watch loop: verdicts, precedence, actions, and exit codes.

No network and no real sleeping. The clock is a counter that only moves when the loop asks to
sleep, the Runpod client is a fake, and the port probe is a function that returns whatever the test
wants. So a ten-minute watch runs instantly and nothing touches the wire.

The one exception is `test_tcp_probe_*`, which opens a loopback socket to prove the probe helper
actually probes. That is a connection to this machine, not to Runpod.
"""

from __future__ import annotations

import io
import socket

import pytest

from runpod_watchdog import watch
from runpod_watchdog.api import (
    AuthError,
    ConflictError,
    LogEvent,
    MissingApiKeyError,
    NetworkError,
    PodNotFoundError,
    RateLimitedError,
    ServerError,
)
from runpod_watchdog.config import Settings
from runpod_watchdog.watch import (
    EXIT_FAILURE_SIGNAL,
    EXIT_HEALTHY,
    EXIT_TIMEOUT,
    EXIT_TOOL_ERROR,
    VERDICT_ERROR,
    VERDICT_FAILURE,
    VERDICT_HEALTHY,
    VERDICT_TIMEOUT,
)

POD = "abc123"
IP = "45.23.12.1"
PUBLIC = 43210


# --- fakes ------------------------------------------------------------------------------------


class FakeClock:
    """Time only moves when the loop sleeps, so a ten-minute window costs nothing."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeClient:
    """A stand-in for RunpodClient that records what it was asked to do.

    `pods` is consumed one entry per read, and the last entry repeats forever so a loop of unknown
    length does not run off the end. An entry that is an exception is raised instead of returned.
    `pod_factory` replaces the queue with a function of the client's own state, for tests where what
    the pod looks like depends on what has already been done to it.
    """

    def __init__(self, pods=None, logs=None, pod_factory=None, on_stop=None, on_start=None):
        self.pods = list(pods or [])
        self.logs = list(logs or [])
        self.pod_factory = pod_factory
        self.on_stop = on_stop
        self.on_start = on_start
        self.calls: list[tuple] = []
        self.stopped = False
        self.started = False
        self.terminated = False

    def _names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def get_pod(self, pod_id):
        self.calls.append(("get_pod", pod_id))
        if self.pod_factory is not None:
            return self.pod_factory(self)
        item = self.pods[0] if len(self.pods) == 1 else self.pods.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def read_pod_logs(self, pod_id, *, seconds, **kwargs):
        self.calls.append(("read_pod_logs", pod_id, kwargs))
        if not self.logs:
            return []
        item = self.logs[0] if len(self.logs) == 1 else self.logs.pop(0)
        if isinstance(item, Exception):
            raise item
        return list(item)

    def stop_pod(self, pod_id):
        self.calls.append(("stop_pod", pod_id))
        if self.on_stop is not None:
            raise self.on_stop
        self.stopped = True
        return {"id": pod_id, "status": "EXITED"}

    def start_pod(self, pod_id):
        self.calls.append(("start_pod", pod_id))
        if self.on_start is not None:
            raise self.on_start
        self.started = True
        return {"id": pod_id, "status": "STARTING"}

    def terminate_pod(self, pod_id):
        self.calls.append(("terminate_pod", pod_id))
        self.terminated = True
        return None


def pod(status="STARTING", ports=("8888/http", "22/tcp"), mapping=None, actions=None):
    """One pod as the API reports it. `mapping` is (private, public, ip) once one is published."""
    body = {
        "id": POD,
        "status": status,
        "ports": list(ports),
        "actions": list(actions) if actions is not None else ["stop", "terminate"],
        # Documented as "Null when the pod is not RUNNING", which is most of a startup.
        "runtime": None,
    }
    if mapping is not None:
        private, public, ip = mapping
        body["runtime"] = {
            "ports": [{"private": private, "public": public, "ip": ip, "type": "http"}]
        }
    return body


def mapped(status="RUNNING", port=8888):
    return pod(status=status, mapping=(port, PUBLIC, IP))


def line(text, event_id="e1"):
    return LogEvent(ts="2026-08-12T00:00:00Z", source="container", line=text, event_id=event_id)


def settings(**overrides) -> Settings:
    base = dict(
        pod_id=POD,
        max_minutes=0.5,
        port=None,
        success_phrase=None,
        failure_phrase=None,
        retry=False,
        terminate=False,
        dry_run=False,
    )
    base.update(overrides)
    return Settings(**base)


def answering(host, port, timeout):
    return None


def refusing(host, port, timeout):
    return "connection refused"


def run(config: Settings, client: FakeClient, probe=answering):
    clock = FakeClock()
    out = io.StringIO()
    outcome = watch.watch(
        config, client, now=clock.now, sleep=clock.sleep, probe=probe, out=out
    )
    return outcome, out.getvalue()


# --- reading a pod ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ports", "expected"),
    [
        (["8888/http", "22/tcp"], [8888, 22]),
        (["8888"], [8888]),
        ([], []),
        (["nonsense", "22/tcp"], [22]),
        ([None, 8888, "22/tcp"], [22]),
    ],
)
def test_declared_ports_reads_what_it_can_and_skips_the_rest(ports, expected):
    assert watch.declared_ports({"ports": ports}) == expected


def test_declared_ports_survives_a_pod_with_no_ports_field():
    assert watch.declared_ports({}) == []


def test_public_mapping_is_none_while_runtime_is_null():
    """The normal state for most of a startup, and it must never read as a fault."""
    assert watch.public_mapping(pod(status="STARTING"), 8888) is None


def test_public_mapping_finds_the_matching_private_port():
    assert watch.public_mapping(mapped(), 8888) == (IP, PUBLIC)


def test_public_mapping_ignores_a_different_private_port():
    assert watch.public_mapping(mapped(port=22), 8888) is None


@pytest.mark.parametrize(
    "entry",
    [
        {"private": 8888, "public": None, "ip": IP},
        {"private": 8888, "public": PUBLIC, "ip": None},
        # A port that is mapped but not publicly routable looks like this. It is not an address.
        {"private": 8888, "public": True, "ip": IP},
    ],
)
def test_public_mapping_needs_both_a_public_port_and_an_ip(entry):
    body = pod(status="RUNNING")
    body["runtime"] = {"ports": [entry]}
    assert watch.public_mapping(body, 8888) is None


def test_tcp_probe_returns_none_when_the_port_answers():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()
        assert watch.tcp_probe(host, port, 2.0) is None


def test_tcp_probe_returns_a_reason_when_nothing_is_listening():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()
    # The socket is closed now, so nothing is listening on that port.
    reason = watch.tcp_probe(host, port, 2.0)
    assert isinstance(reason, str) and reason


# --- success ------------------------------------------------------------------------------------


def test_the_port_answering_is_healthy():
    client = FakeClient(pods=[mapped()])

    outcome, out = run(settings(port=8888), client, probe=answering)

    assert outcome.verdict == VERDICT_HEALTHY
    assert outcome.exit_code == EXIT_HEALTHY
    assert "stop_pod" not in client._names()
    assert "answered" in out


def test_the_success_phrase_is_healthy():
    client = FakeClient(pods=[pod()], logs=[[line("uvicorn running on 0.0.0.0:8888")]])

    outcome, _ = run(settings(success_phrase="uvicorn running"), client)

    assert outcome.verdict == VERDICT_HEALTHY
    assert "stop_pod" not in client._names()


def test_a_pod_that_never_answers_is_not_called_healthy():
    client = FakeClient(pods=[mapped()])

    outcome, _ = run(settings(port=8888), client, probe=refusing)

    assert outcome.verdict == VERDICT_TIMEOUT


def test_logs_are_not_read_at_all_when_no_phrase_is_set():
    """A port-only run has no reason to open the log stream."""
    client = FakeClient(pods=[mapped()])

    run(settings(port=8888), client)

    assert "read_pod_logs" not in client._names()


# --- every success signal must fire ---------------------------------------------------------


def test_one_of_two_success_signals_is_not_enough():
    """The port answers the whole time; the phrase never arrives. That is not healthy."""
    client = FakeClient(pods=[mapped()], logs=[[line("still loading")]])

    outcome, _ = run(settings(port=8888, success_phrase="ready to serve"), client)

    assert outcome.verdict == VERDICT_TIMEOUT
    assert outcome.exit_code == EXIT_TIMEOUT


def test_two_success_signals_that_fire_on_different_polls_still_agree():
    """A satisfied signal stays satisfied, so two signals do not have to land on the same poll."""
    client = FakeClient(
        pods=[mapped()],
        logs=[[], [], [line("ready to serve")]],
    )

    outcome, _ = run(settings(port=8888, success_phrase="ready to serve"), client)

    assert outcome.verdict == VERDICT_HEALTHY


def test_the_port_is_probed_once_and_then_remembered():
    probes: list[tuple] = []

    def counting_probe(host, port, timeout):
        probes.append((host, port))
        return None

    client = FakeClient(pods=[mapped()], logs=[[], [], [line("ready")]])

    run(settings(port=8888, success_phrase="ready"), client, probe=counting_probe)

    assert probes == [(IP, PUBLIC)]


# --- failure --------------------------------------------------------------------------------


def test_the_failure_phrase_twice_is_a_failure():
    client = FakeClient(
        pods=[pod()],
        logs=[[line("CUDA out of memory")], [line("CUDA out of memory")]],
    )

    outcome, _ = run(settings(failure_phrase="CUDA out of memory"), client)

    assert outcome.verdict == VERDICT_FAILURE
    assert outcome.exit_code == EXIT_FAILURE_SIGNAL
    assert outcome.action == "stopped"
    assert "stop_pod" in client._names()


def test_the_failure_phrase_once_is_not_a_verdict():
    """A single line can be an error a program recovers from. Only a repeat is a crash loop."""
    client = FakeClient(pods=[pod()], logs=[[line("CUDA out of memory")], []])

    outcome, _ = run(settings(failure_phrase="CUDA out of memory"), client)

    assert outcome.verdict == VERDICT_TIMEOUT


def test_one_line_holding_the_phrase_twice_still_counts_once():
    """What is being counted is a repeat of the message, and a crash loop prints a new line."""
    client = FakeClient(pods=[pod()], logs=[[line("oom oom")], []])

    outcome, _ = run(settings(failure_phrase="oom"), client)

    assert outcome.verdict == VERDICT_TIMEOUT


def test_a_pod_that_exits_on_its_own_is_a_failure():
    client = FakeClient(pods=[pod(status="EXITED")])

    outcome, _ = run(settings(port=8888), client)

    assert outcome.verdict == VERDICT_FAILURE
    assert "EXITED" in outcome.detail


@pytest.mark.parametrize("status", ["EXITED", "ERROR", "TERMINATED"])
def test_every_terminal_status_is_a_failure(status):
    client = FakeClient(pods=[pod(status=status)])

    outcome, _ = run(settings(port=8888), client)

    assert outcome.verdict == VERDICT_FAILURE


# --- timeout --------------------------------------------------------------------------------


def test_a_pod_that_never_becomes_healthy_times_out():
    client = FakeClient(pods=[pod(status="STARTING")])

    outcome, out = run(settings(port=8888), client)

    assert outcome.verdict == VERDICT_TIMEOUT
    assert outcome.exit_code == EXIT_TIMEOUT
    assert outcome.action == "stopped"
    assert "no public mapping published yet" in out


def test_the_timeout_message_carries_the_last_known_state():
    """runpodctl's rule: never report a bare timeout, always say what you last saw."""
    client = FakeClient(pods=[pod(status="PROVISIONING")])

    outcome, _ = run(settings(port=8888), client)

    assert "last known state" in outcome.detail
    assert "PROVISIONING" in outcome.detail


def test_a_status_line_is_printed_for_every_poll():
    client = FakeClient(pods=[pod(status="STARTING")])

    _, out = run(settings(port=8888, max_minutes=0.5), client)

    # 30 seconds of window at a 10 second interval: polls at 0, 10, 20 and 30.
    assert len([n for n in out.splitlines() if n.startswith("[")]) == 4


# --- precedence ------------------------------------------------------------------------------


def test_the_failure_phrase_beats_the_port_answering():
    """An explicit failure signal beats an inferred success. ADR-0004."""
    client = FakeClient(
        pods=[mapped()],
        logs=[[line("CUDA out of memory"), line("CUDA out of memory")]],
    )

    outcome, _ = run(
        settings(port=8888, failure_phrase="CUDA out of memory"), client, probe=answering
    )

    assert outcome.verdict == VERDICT_FAILURE
    assert outcome.exit_code == EXIT_FAILURE_SIGNAL


def test_the_failure_phrase_beats_the_success_phrase_in_the_same_batch():
    client = FakeClient(
        pods=[pod()],
        logs=[[line("ready to serve"), line("fatal error"), line("fatal error")]],
    )

    outcome, _ = run(
        settings(success_phrase="ready to serve", failure_phrase="fatal error"), client
    )

    assert outcome.verdict == VERDICT_FAILURE


def test_a_dead_pod_beats_a_port_that_still_answers():
    """A stopped pod keeps reporting its old port mapping for a while, so status is checked first."""
    client = FakeClient(pods=[mapped(status="EXITED")])

    outcome, _ = run(settings(port=8888), client, probe=answering)

    assert outcome.verdict == VERDICT_FAILURE
    assert "EXITED" in outcome.detail


def test_success_on_the_last_poll_beats_the_clock():
    """A pod that became healthy inside its window is healthy, even at the very edge of it."""
    client = FakeClient(pods=[pod(), pod(), pod(), mapped()])

    outcome, _ = run(settings(port=8888, max_minutes=0.5), client, probe=answering)

    assert outcome.verdict == VERDICT_HEALTHY
    assert "stop_pod" not in client._names()


# --- terminate and dry run -------------------------------------------------------------------


def test_terminate_deletes_the_pod_instead_of_stopping_it():
    client = FakeClient(pods=[pod(status="STARTING")])

    outcome, _ = run(settings(port=8888, terminate=True), client)

    assert outcome.action == "terminated"
    assert "terminate_pod" in client._names()
    assert "stop_pod" not in client._names()


def test_a_dry_run_changes_nothing():
    client = FakeClient(pods=[pod(status="STARTING")])

    outcome, out = run(settings(port=8888, dry_run=True), client)

    assert client._names() == ["get_pod"] * 4
    assert outcome.action == "would stop"
    assert "would stop" in out


def test_a_dry_run_returns_the_same_exit_code_as_the_real_run_would():
    """Otherwise a dry run is useless for testing the script around it."""
    logs = [[line("fatal"), line("fatal")]]

    wet = FakeClient(pods=[pod()], logs=list(logs))
    dry = FakeClient(pods=[pod()], logs=list(logs))

    real, _ = run(settings(failure_phrase="fatal"), wet)
    pretend, _ = run(settings(failure_phrase="fatal", dry_run=True), dry)

    assert real.exit_code == pretend.exit_code == EXIT_FAILURE_SIGNAL
    assert "stop_pod" in wet._names()
    assert "stop_pod" not in dry._names()


def test_a_dry_run_says_what_it_would_terminate():
    client = FakeClient(pods=[pod(status="STARTING")])

    outcome, out = run(settings(port=8888, terminate=True, dry_run=True), client)

    assert outcome.action == "would terminate"
    assert "terminate_pod" not in client._names()


# --- retry ------------------------------------------------------------------------------------


def retry_pods(client: FakeClient):
    """A pod that fails its first window, then comes up healthy after being restarted."""
    if client.started:
        return mapped()
    if client.stopped:
        return pod(status="EXITED", actions=["start", "terminate"])
    return pod(status="STARTING")


def test_retry_stops_the_pod_starts_it_again_and_watches_a_second_window():
    client = FakeClient(pod_factory=retry_pods)

    outcome, out = run(settings(port=8888, retry=True), client, probe=answering)

    names = client._names()
    assert names.index("stop_pod") < names.index("start_pod")
    assert outcome.verdict == VERDICT_HEALTHY
    assert outcome.exit_code == EXIT_HEALTHY
    assert "Restarting pod" in out


def never_healthy(client: FakeClient):
    """A pod that restarts cleanly but never comes up healthy, so both windows run out."""
    if client.started:
        return pod(status="STARTING")
    if client.stopped:
        return pod(status="EXITED", actions=["start", "terminate"])
    return pod(status="STARTING")


def test_retry_gives_up_after_the_second_window_and_acts():
    client = FakeClient(pod_factory=never_healthy)

    outcome, _ = run(settings(port=8888, retry=True), client, probe=refusing)

    assert outcome.verdict == VERDICT_TIMEOUT
    assert outcome.action == "stopped"
    # Stopped once for the restart, once for the final verdict; started once in between.
    assert client._names().count("stop_pod") == 2
    assert client._names().count("start_pod") == 1


def test_retry_happens_only_once():
    client = FakeClient(pod_factory=never_healthy)

    run(settings(port=8888, retry=True), client, probe=refusing)

    assert client._names().count("start_pod") == 1


def test_a_pod_that_never_becomes_startable_is_a_tool_error():
    """The retry waits for the pod's own `actions` list to offer `start`, and gives up if it never
    does rather than hanging."""
    client = FakeClient(pods=[pod(status="STARTING", actions=["stop", "terminate"])])

    outcome, _ = run(settings(port=8888, retry=True), client, probe=refusing)

    assert outcome.verdict == VERDICT_ERROR
    assert outcome.exit_code == EXIT_TOOL_ERROR
    assert "did not become startable" in outcome.detail


def test_a_dry_run_does_not_restart_anything():
    client = FakeClient(pods=[pod(status="STARTING")])

    outcome, out = run(settings(port=8888, retry=True, dry_run=True), client)

    assert "start_pod" not in client._names()
    assert "stop_pod" not in client._names()
    assert "would restart" in out
    assert outcome.exit_code == EXIT_TIMEOUT


def test_a_restart_that_cannot_start_the_pod_is_a_tool_error():
    client = FakeClient(
        pod_factory=lambda c: pod(status="EXITED", actions=["start", "terminate"])
        if c.stopped
        else pod(status="STARTING"),
        on_start=ServerError("Runpod had a server error (HTTP 500)."),
    )

    outcome, _ = run(settings(port=8888, retry=True), client)

    assert outcome.verdict == VERDICT_ERROR
    assert outcome.exit_code == EXIT_TOOL_ERROR
    assert "could not start" in outcome.detail


def test_a_retry_does_not_count_the_first_window_log_lines_again():
    """Resuming the log stream is what makes the second window judge the second attempt."""
    client = FakeClient(
        pod_factory=retry_pods,
        logs=[[line("fatal", event_id="e1")], []],
    )

    outcome, _ = run(settings(port=8888, retry=True, failure_phrase="fatal"), client)

    reads = [call[2] for call in client.calls if call[0] == "read_pod_logs"]
    assert reads[0] == {"tail": watch.LOG_BACKFILL}
    assert reads[1] == {"last_event_id": "e1"}
    assert outcome.verdict == VERDICT_HEALTHY


# --- the undeclared port ----------------------------------------------------------------------


def test_an_undeclared_port_warns_and_lists_what_the_pod_does_publish():
    """It keeps watching, because the failure signal can still reach a verdict of its own."""
    client = FakeClient(
        pods=[pod(ports=["22/tcp", "5000/http"])],
        logs=[[line("fatal"), line("fatal")]],
    )

    outcome, out = run(settings(port=8888, failure_phrase="fatal"), client)

    assert "warning:" in out
    assert "does not publish port 8888" in out
    assert "22/tcp, 5000/http" in out
    assert outcome.verdict == VERDICT_FAILURE
    assert outcome.exit_code == EXIT_FAILURE_SIGNAL


def test_the_warning_says_a_healthy_verdict_is_now_out_of_reach():
    """Success signals are ANDed, so a port that can never answer settles the whole run."""
    client = FakeClient(pods=[pod(ports=["22/tcp"])], logs=[[line("ready")]])

    outcome, out = run(settings(port=8888, success_phrase="ready"), client)

    assert "can only end in a stop" in out
    assert outcome.verdict == VERDICT_TIMEOUT


def test_an_undeclared_port_that_is_the_only_signal_refuses_to_watch():
    client = FakeClient(pods=[pod(ports=["22/tcp", "5000/http"])])

    outcome, _ = run(settings(port=8888), client)

    assert outcome.verdict == VERDICT_ERROR
    assert outcome.exit_code == EXIT_TOOL_ERROR
    assert "does not publish port 8888" in outcome.detail
    assert "22/tcp, 5000/http" in outcome.detail
    assert "stop_pod" not in client._names()
    assert "terminate_pod" not in client._names()


def test_a_declared_port_says_nothing():
    client = FakeClient(pods=[mapped()])

    _, out = run(settings(port=8888), client)

    assert "warning:" not in out


def test_a_pod_that_publishes_no_port_list_is_not_second_guessed():
    body = pod()
    del body["ports"]
    client = FakeClient(pods=[body])

    outcome, out = run(settings(port=8888), client)

    assert "warning:" not in out
    assert outcome.verdict == VERDICT_TIMEOUT


def test_the_warning_is_printed_once_not_once_per_poll():
    client = FakeClient(pods=[pod(ports=["22/tcp"])], logs=[[]])

    _, out = run(settings(port=8888, success_phrase="never"), client)

    assert out.count("warning:") == 1


# --- API trouble ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        RateLimitedError("Runpod is rate limiting this API key."),
        ServerError("Runpod had a server error (HTTP 503)."),
        NetworkError("Could not reach Runpod."),
    ],
)
def test_a_survivable_error_does_not_end_the_watch(error):
    """The pod exists and is billing, so giving up early is the expensive answer."""
    client = FakeClient(pods=[error, mapped()])

    outcome, _ = run(settings(port=8888), client, probe=answering)

    assert outcome.verdict == VERDICT_HEALTHY


@pytest.mark.parametrize(
    "error",
    [
        MissingApiKeyError("No Runpod API key. Set the RUNPOD_API_KEY environment variable."),
        AuthError("Runpod rejected the API key."),
    ],
)
def test_an_unfixable_error_ends_the_watch_immediately(error):
    client = FakeClient(pods=[error, mapped()])

    outcome, _ = run(settings(port=8888), client)

    assert outcome.verdict == VERDICT_ERROR
    assert outcome.exit_code == EXIT_TOOL_ERROR
    assert client._names() == ["get_pod"]


def test_a_missing_key_names_the_environment_variable():
    client = FakeClient(
        pods=[MissingApiKeyError("No Runpod API key. Set the RUNPOD_API_KEY environment variable.")]
    )

    outcome, _ = run(settings(port=8888), client)

    assert "RUNPOD_API_KEY" in outcome.detail


def test_one_missing_read_is_survived_but_two_in_a_row_is_not():
    survived = FakeClient(pods=[PodNotFoundError("no pod"), mapped()])
    gone = FakeClient(pods=[PodNotFoundError("no pod"), PodNotFoundError("no pod")])

    healthy, _ = run(settings(port=8888), survived, probe=answering)
    missing, _ = run(settings(port=8888), gone)

    assert healthy.verdict == VERDICT_HEALTHY
    assert missing.verdict == VERDICT_ERROR
    assert missing.exit_code == EXIT_TOOL_ERROR


def test_a_log_read_that_fails_costs_one_poll_not_the_run():
    client = FakeClient(
        pods=[pod()],
        logs=[ServerError("Runpod had a server error (HTTP 500)."), [line("ready")]],
    )

    outcome, _ = run(settings(success_phrase="ready"), client)

    assert outcome.verdict == VERDICT_HEALTHY


def test_a_stop_that_fails_says_the_pod_may_still_be_billing():
    client = FakeClient(
        pods=[pod(status="STARTING")],
        on_stop=ServerError("Runpod had a server error (HTTP 500)."),
    )

    outcome, _ = run(settings(port=8888), client)

    assert outcome.verdict == VERDICT_ERROR
    assert outcome.exit_code == EXIT_TOOL_ERROR
    assert "may still be running and billing" in outcome.detail


def test_a_pod_that_stopped_itself_between_reads_is_not_an_error():
    client = FakeClient(
        pods=[pod(status="STARTING")],
        on_stop=ConflictError("Runpod refused that action for its current status."),
    )

    outcome, out = run(settings(port=8888), client)

    assert outcome.verdict == VERDICT_TIMEOUT
    assert outcome.action == "already stopped"
    assert outcome.exit_code == EXIT_TIMEOUT
    assert "already stopped" in out


# --- exit codes -------------------------------------------------------------------------------


def test_every_documented_exit_code_is_reachable():
    """The exit codes are the tool's real output for anything scripting around it."""
    healthy, _ = run(settings(port=8888), FakeClient(pods=[mapped()]), probe=answering)
    timeout, _ = run(settings(port=8888), FakeClient(pods=[pod(status="STARTING")]))
    failure, _ = run(
        settings(failure_phrase="boom"),
        FakeClient(pods=[pod()], logs=[[line("boom"), line("boom")]]),
    )
    tool, _ = run(
        settings(port=8888), FakeClient(pods=[AuthError("Runpod rejected the API key.")])
    )

    assert (healthy.exit_code, timeout.exit_code, failure.exit_code, tool.exit_code) == (
        EXIT_HEALTHY,
        EXIT_TIMEOUT,
        EXIT_FAILURE_SIGNAL,
        EXIT_TOOL_ERROR,
    )
    assert len({healthy.exit_code, timeout.exit_code, failure.exit_code, tool.exit_code}) == 4
