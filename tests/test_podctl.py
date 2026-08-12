"""`runpod-watchdog-pod`: flag parsing, the request body it builds, exit codes, and redaction.

No network. Every test replaces the client wholesale with a recorder, so no test in this file can
reach Runpod even with a key in the environment.
"""

from __future__ import annotations

import json

import pytest

from runpod_watchdog import podctl
from runpod_watchdog.api import BadRequestError, PodNotFoundError

POD = {
    "id": "pod123",
    "name": "watchdog-proving-run",
    "status": "PROVISIONING",
    "image": "nginx:alpine",
    "args": "",
    "ports": ["80/tcp"],
    "env": {"SECRET_TOKEN": "hunter2", "MODEL": "llama-3"},
    "runtime": None,
    "dataCenterId": "US-KS-2",
    "cost": 0.08,
    "actions": ["stop", "terminate"],
    "createdAt": "2026-08-12T12:00:00Z",
    "startedAt": None,
}

CPU = ["--cpu", "cpu3c-2-4"]
NAMED = ["--name", "watchdog-proving-run", "--image", "nginx:alpine"]


class Recorder:
    """Stands in for RunpodClient. It records calls and cannot make a request."""

    def __init__(self, *, pod=None, raises=None, pods=None, **kwargs):
        self.calls: list[tuple] = []
        self.pod = pod if pod is not None else POD
        self.pods = pods if pods is not None else [POD]
        self.raises = raises
        self.closed = False

    def _maybe_raise(self):
        if self.raises is not None:
            raise self.raises

    def create_pod(self, **body):
        self.calls.append(("create_pod", body))
        self._maybe_raise()
        return self.pod

    def get_pod(self, pod_id):
        self.calls.append(("get_pod", pod_id))
        self._maybe_raise()
        return self.pod

    def list_pods(self):
        self.calls.append(("list_pods", None))
        self._maybe_raise()
        return self.pods

    def terminate_pod(self, pod_id):
        self.calls.append(("terminate_pod", pod_id))
        self._maybe_raise()

    def close(self):
        self.closed = True


@pytest.fixture
def client(monkeypatch):
    """One recorder, handed to whatever `main` builds."""
    made: list[Recorder] = []

    def build(**kwargs):
        made.append(Recorder(**kwargs))
        return made[-1]

    monkeypatch.setattr(podctl, "RunpodClient", build)
    return made


def only(client: list[Recorder]) -> Recorder:
    assert len(client) == 1, "expected exactly one client to be built"
    return client[0]


# --- create -------------------------------------------------------------------------------------


def test_create_sends_every_flag_under_the_spec_name(client, capsys):
    code = podctl.main([
        "create", *NAMED, *CPU, "--vcpu", "4",
        "--args", 'sh -c "echo WATCHDOG-CRASH-TEST; exit 1"',
        "--port", "80/tcp", "--port", "22/tcp",
        "--disk", "10", "--cloud", "SECURE", "--data-center", "US-KS-2",
    ])

    assert code == podctl.EXIT_OK
    name, body = only(client).calls[0]
    assert name == "create_pod"
    assert body == {
        "name": "watchdog-proving-run",
        "image": "nginx:alpine",
        "gpu": None,
        "cpu": {"id": "cpu3c-2-4", "vcpuCount": 4},
        "args": 'sh -c "echo WATCHDOG-CRASH-TEST; exit 1"',
        "ports": ["80/tcp", "22/tcp"],
        "disk": 10,
        "cloud": "SECURE",
        "data_center_ids": ["US-KS-2"],
    }
    assert "pod id: pod123" in capsys.readouterr().out


def test_create_leaves_out_what_was_not_asked_for(client):
    podctl.main(["create", *NAMED, *CPU])

    _, body = only(client).calls[0]
    assert body["ports"] is None
    assert body["args"] is None
    assert body["disk"] is None
    assert body["cloud"] is None
    assert body["data_center_ids"] is None


def test_create_builds_a_gpu_instance(client):
    podctl.main(["create", *NAMED, "--gpu", "NVIDIA GeForce RTX 4090", "--gpu-count", "2"])

    _, body = only(client).calls[0]
    assert body["gpu"] == {"id": "NVIDIA GeForce RTX 4090", "count": 2}
    assert body["cpu"] is None


def test_create_dry_run_prints_the_body_and_creates_nothing(client, capsys):
    code = podctl.main(["create", *NAMED, *CPU, "--port", "80/tcp", "--dry-run"])

    assert code == podctl.EXIT_OK
    assert only(client).calls == []
    out = capsys.readouterr().out
    assert "nothing was created" in out
    assert '"80/tcp"' in out


@pytest.mark.parametrize(
    "flags",
    [[], [*CPU, "--gpu", "NVIDIA GeForce RTX 4090"]],
    ids=["neither", "both"],
)
def test_create_needs_exactly_one_instance_type(client, capsys, flags):
    code = podctl.main(["create", *NAMED, *flags])

    assert code == podctl.EXIT_CONFIG_ERROR
    assert only(client).calls == []
    assert "exactly one of --cpu or --gpu" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["1", "3", "0"])
def test_vcpu_must_be_at_least_two_and_a_power_of_two(client, capsys, value):
    """The spec's rule for `vcpuCount`, checked before spending a round trip on a 400."""
    code = podctl.main(["create", *NAMED, *CPU, "--vcpu", value])

    assert code == podctl.EXIT_CONFIG_ERROR
    assert only(client).calls == []
    assert "power of two" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["80", "80/", "http/80", "/tcp"])
def test_a_port_must_be_written_as_port_slash_protocol(client, capsys, value):
    code = podctl.main(["create", *NAMED, *CPU, "--port", value])

    assert code == podctl.EXIT_CONFIG_ERROR
    assert only(client).calls == []
    assert "port/protocol" in capsys.readouterr().err


def test_a_rejected_create_exits_five_and_repeats_what_runpod_said(client, monkeypatch, capsys):
    monkeypatch.setattr(
        podctl, "RunpodClient", lambda **kw: Recorder(raises=BadRequestError("no such flavor"))
    )

    code = podctl.main(["create", *NAMED, *CPU])

    assert code == podctl.EXIT_TOOL_ERROR
    assert "no such flavor" in capsys.readouterr().err


# --- show and list ------------------------------------------------------------------------------


def test_show_reads_the_pod_back(client, capsys):
    code = podctl.main(["show", "pod123"])

    assert code == podctl.EXIT_OK
    assert only(client).calls == [("get_pod", "pod123")]
    out = capsys.readouterr().out
    assert "PROVISIONING" in out
    # The open question of ADR-0005: the raw value is printed rather than tidied away.
    assert "runtime.ports" in out
    assert "null" in out


def test_show_json_hides_environment_variable_values(client, capsys):
    """This command exists to produce transcripts that get committed to a public repository."""
    code = podctl.main(["show", "pod123", "--json"])

    assert code == podctl.EXIT_OK
    printed = json.loads(capsys.readouterr().out)
    assert printed["env"] == {"SECRET_TOKEN": "***", "MODEL": "***"}
    assert "hunter2" not in json.dumps(printed)


def test_list_prints_one_line_per_pod_and_no_environment_variables(client, capsys):
    code = podctl.main(["list"])

    assert code == podctl.EXIT_OK
    assert only(client).calls == [("list_pods", None)]
    out = capsys.readouterr().out
    assert "pod123" in out
    assert "1 pod on this account" in out
    assert "hunter2" not in out


def test_list_says_so_when_the_account_is_empty(client, monkeypatch, capsys):
    monkeypatch.setattr(podctl, "RunpodClient", lambda **kw: Recorder(pods=[]))

    assert podctl.main(["list"]) == podctl.EXIT_OK
    assert "No pods on this account." in capsys.readouterr().out


def test_a_dollar_figure_is_labelled_as_runpod_account_spend(client, capsys):
    podctl.main(["list"])

    assert "Runpod account spend" in capsys.readouterr().out


# --- terminate ----------------------------------------------------------------------------------


def test_terminate_deletes_the_pod_and_says_who_did_it(client, capsys):
    code = podctl.main(["terminate", "pod123"])

    assert code == podctl.EXIT_OK
    assert only(client).calls == [("terminate_pod", "pod123")]
    out = capsys.readouterr().out
    assert "terminated pod pod123" in out
    assert "not the watchdog" in out


def test_terminate_dry_run_deletes_nothing(client, capsys):
    code = podctl.main(["terminate", "pod123", "--dry-run"])

    assert code == podctl.EXIT_OK
    assert only(client).calls == []
    assert "would terminate pod pod123" in capsys.readouterr().out


def test_terminating_a_pod_that_is_gone_exits_five(client, monkeypatch, capsys):
    monkeypatch.setattr(
        podctl, "RunpodClient", lambda **kw: Recorder(raises=PodNotFoundError("no such pod"))
    )

    assert podctl.main(["terminate", "pod123"]) == podctl.EXIT_TOOL_ERROR
    assert "no such pod" in capsys.readouterr().err


# --- housekeeping -------------------------------------------------------------------------------


def test_the_client_is_closed_however_the_run_ends(monkeypatch):
    recorder = Recorder(raises=PodNotFoundError("gone"))
    monkeypatch.setattr(podctl, "RunpodClient", lambda **kw: recorder)

    podctl.main(["terminate", "pod123"])

    assert recorder.closed


def test_no_subcommand_is_a_usage_error(client):
    with pytest.raises(SystemExit) as caught:
        podctl.main([])

    assert caught.value.code == podctl.EXIT_CONFIG_ERROR


def test_redact_leaves_a_pod_without_environment_variables_alone():
    pod = {"id": "x", "env": {}}

    assert podctl.redact(pod) == pod
