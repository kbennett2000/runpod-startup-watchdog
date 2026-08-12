"""`runpod-watchdog-pod` — create, show, list, and terminate pods.

This is not the watchdog. The watchdog watches one pod that already exists and never makes one;
this command is the small amount of pod handling a proving run needs, so that a run can be
reproduced from this repository alone: create the pod, read it back, watch it with
`runpod-watchdog`, then prove afterwards that nothing was left running.

Why it is a separate command rather than flags on the watchdog is
docs/adr/0006-pod-lifecycle-is-a-separate-command.md.

Every subcommand that changes anything takes `--dry-run`, which prints exactly what would be sent
and sends nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import __version__
from .api import RunpodClient, RunpodError

# The same codes the watchdog uses, for the same reasons: 2 is argparse's own code for a bad
# command line, and 5 means the tool could not do its job. docs/adr/0004-health-verdicts.md.
EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_TOOL_ERROR = 5

# Environment variable values are hidden in JSON output, keys kept. This command exists to produce
# transcripts that get committed to a public repository, and environment variables are where people
# put secrets. Showing that a variable exists is useful; showing what is in it is a leak.
REDACTED = "***"


class UsageError(Exception):
    """A problem with the arguments, phrased as one plain sentence."""


# --- argument parsing ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runpod-watchdog-pod",
        description=(
            "Create, read, list, and terminate Runpod pods over REST API v2. This is the pod "
            "handling around the watchdog, not the watchdog: it never watches and never decides "
            "whether a pod is healthy."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    create = subcommands.add_parser(
        "create",
        help="Create one pod and print its id.",
        description="Create one pod and print its id. Exactly one of --cpu or --gpu is required.",
    )
    create.add_argument("--name", required=True, help="A name for the pod.")
    create.add_argument("--image", required=True, help="Docker image reference, such as "
                        "nginx:alpine.")
    create.add_argument("--cpu", default=None, metavar="FLAVOR",
                        help="Make a CPU pod on this flavor, as listed by Runpod's CPU catalog "
                             "(for example cpu3c-2-4).")
    create.add_argument("--vcpu", type=int, default=2, metavar="N",
                        help="vCPUs for a CPU pod. Must be at least 2 and a power of two. "
                             "Default: 2.")
    create.add_argument("--gpu", default=None, metavar="TYPE",
                        help='Make a GPU pod on this GPU type (for example "NVIDIA GeForce RTX '
                             '4090").')
    create.add_argument("--gpu-count", type=int, default=1, metavar="N",
                        help="GPUs for a GPU pod. Default: 1.")
    create.add_argument("--args", default=None, metavar="TEXT",
                        help="The container start command, passed to the image's entrypoint. "
                             "Omit to let the image run whatever it normally runs.")
    create.add_argument("--port", action="append", default=None, dest="ports", metavar="PORT/PROTO",
                        help="Expose a port, written as port/protocol such as 80/tcp. Repeatable.")
    create.add_argument("--disk", type=int, default=None, metavar="GB",
                        help="Container disk in GB. Ephemeral: it is wiped when the pod restarts.")
    create.add_argument("--cloud", default=None, choices=["SECURE", "COMMUNITY"],
                        help="Cloud tier. Omit for Runpod's default, SECURE.")
    create.add_argument("--data-center", action="append", default=None, dest="data_centers",
                        metavar="ID",
                        help="Prefer this data center. Repeatable. Omit to let Runpod choose.")
    create.add_argument("--dry-run", action="store_true",
                        help="Print the request body and create nothing.")

    show = subcommands.add_parser("show", help="Read one pod back and print it.")
    show.add_argument("pod_id", help="The pod to read.")
    show.add_argument("--json", action="store_true", dest="as_json",
                      help="Print the whole response as JSON, with environment variable values "
                           "hidden.")

    listing = subcommands.add_parser("list", help="List every pod on the account.")
    listing.add_argument("--json", action="store_true", dest="as_json",
                         help="Print the whole response as JSON, with environment variable values "
                              "hidden.")

    terminate = subcommands.add_parser(
        "terminate",
        help="Delete one pod. Irreversible.",
        description="Delete one pod, along with anything on its disk. This is a manual action by "
                    "whoever runs it; the watchdog is not involved.",
    )
    terminate.add_argument("pod_id", help="The pod to delete.")
    terminate.add_argument("--dry-run", action="store_true",
                           help="Say what would be deleted and delete nothing.")

    return parser


def _instance(args: argparse.Namespace) -> tuple[dict | None, dict | None]:
    """Turn the instance-type flags into the spec's `gpu` or `cpu` object.

    Exactly one, which is the spec's own rule for `CreatePodRequest`. The client checks this too;
    checking here as well is what turns it into a readable message instead of a `ValueError`.
    """
    if (args.cpu is None) == (args.gpu is None):
        raise UsageError("pass exactly one of --cpu or --gpu: a pod is either a CPU pod or a "
                         "GPU pod.")

    if args.gpu is not None:
        if args.gpu_count < 1:
            raise UsageError(f"--gpu-count must be at least 1, got {args.gpu_count}")
        return {"id": args.gpu, "count": args.gpu_count}, None

    # The spec's words: "Must be valid for the selected CPU flavor and must be a power of two."
    # `n & (n - 1)` is zero only for powers of two.
    if args.vcpu < 2 or args.vcpu & (args.vcpu - 1):
        raise UsageError(f"--vcpu must be at least 2 and a power of two, got {args.vcpu}")
    return None, {"id": args.cpu, "vcpuCount": args.vcpu}


def _ports(values: list[str] | None) -> list[str] | None:
    """Check each --port looks like `number/protocol` before spending a round trip on it."""
    if values is None:
        return None
    for value in values:
        number, _, protocol = value.partition("/")
        if not number.isdigit() or not protocol:
            raise UsageError(
                f"--port must be written as port/protocol, such as 80/tcp. Got {value!r}"
            )
    return list(values)


def create_body(args: argparse.Namespace) -> dict[str, Any]:
    """The keyword arguments for `RunpodClient.create_pod`, built from the flags.

    Separated from the call so `--dry-run` can print exactly what a real run would send.
    """
    gpu, cpu = _instance(args)
    return {
        "name": args.name,
        "image": args.image,
        "gpu": gpu,
        "cpu": cpu,
        "args": args.args,
        "ports": _ports(args.ports),
        "disk": args.disk,
        "cloud": args.cloud,
        "data_center_ids": args.data_centers,
    }


# --- printing -----------------------------------------------------------------------------------


def redact(pod: Any) -> Any:
    """A pod with its environment variable values replaced, keys left alone."""
    if not isinstance(pod, dict):
        return pod
    env = pod.get("env")
    if not isinstance(env, dict) or not env:
        return pod
    return {**pod, "env": {name: REDACTED for name in env}}


def _rows(pairs: list[tuple[str, str]]) -> str:
    width = max(len(name) for name, _ in pairs)
    return "\n".join(f"  {name.ljust(width)}  {value}" for name, value in pairs)


def summarise(pod: dict) -> str:
    """The handful of fields worth reading on one pod, as plain lines.

    `cost` is Runpod account spend in US dollars per hour, so it is labelled as such rather than
    left as a bare number.
    """
    # `runtime` is documented "Null when the pod is not RUNNING", and `runtime.ports` inside it is
    # the only place a public address appears. Both are printed raw, because whether they are
    # populated at all is the open question this tool's proving run had to settle. ADR-0005.
    runtime = pod.get("runtime")
    mapped = json.dumps(runtime.get("ports") if isinstance(runtime, dict) else runtime)
    ports = pod.get("ports")
    cost = pod.get("cost")
    return _rows([
        ("id", str(pod.get("id"))),
        ("name", str(pod.get("name"))),
        ("status", str(pod.get("status"))),
        ("image", str(pod.get("image"))),
        ("args", str(pod.get("args") or "(none)")),
        ("exposed ports", ", ".join(ports) if isinstance(ports, list) and ports else "(none)"),
        ("runtime.ports", mapped),
        ("data center", str(pod.get("dataCenterId"))),
        ("cost", "(not reported)" if cost is None
                 else f"{cost} US dollars per hour of Runpod account spend"),
        ("actions", ", ".join(pod.get("actions", [])) or "(none)"),
        ("created", str(pod.get("createdAt"))),
        ("started", str(pod.get("startedAt"))),
    ])


def table(pods: list[dict]) -> str:
    """Every pod on the account, one line each. No environment variables, by construction."""
    if not pods:
        return "No pods on this account."

    header = ("ID", "STATUS", "USD/HR", "NAME", "IMAGE")
    rows = [
        (
            str(pod.get("id", "")),
            str(pod.get("status", "")),
            str(pod.get("cost", "")),
            str(pod.get("name", "")),
            str(pod.get("image", "")),
        )
        for pod in pods
    ]
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
             for row in [header, *rows]]
    total = len(pods)
    lines.append(f"\n{total} pod{'' if total == 1 else 's'} on this account. "
                 "USD/HR is Runpod account spend.")
    return "\n".join(lines)


# --- the subcommands ----------------------------------------------------------------------------


def _create(args: argparse.Namespace, client: RunpodClient) -> int:
    body = create_body(args)
    shown = {name: value for name, value in body.items() if value is not None}
    print("Creating pod with:")
    print(_rows([(name, json.dumps(value)) for name, value in shown.items()]))

    if args.dry_run:
        print("\ndry run: nothing was created.")
        return EXIT_OK

    pod = client.create_pod(**body)
    print(f"\npod id: {pod.get('id')}")
    print(summarise(pod))
    print(
        "\nProvisioning is asynchronous: the pod starts PROVISIONING and is not usable yet. "
        "That gap is what the watchdog watches."
    )
    return EXIT_OK


def _show(args: argparse.Namespace, client: RunpodClient) -> int:
    pod = client.get_pod(args.pod_id)
    print(json.dumps(redact(pod), indent=2, sort_keys=True) if args.as_json else summarise(pod))
    return EXIT_OK


def _list(args: argparse.Namespace, client: RunpodClient) -> int:
    pods = client.list_pods()
    print(json.dumps([redact(pod) for pod in pods], indent=2, sort_keys=True)
          if args.as_json else table(pods))
    return EXIT_OK


def _terminate(args: argparse.Namespace, client: RunpodClient) -> int:
    if args.dry_run:
        print(f"dry run: would terminate pod {args.pod_id}. Nothing was changed.")
        return EXIT_OK
    client.terminate_pod(args.pod_id)
    print(f"terminated pod {args.pod_id}. This was a manual termination, not the watchdog.")
    return EXIT_OK


COMMANDS = {"create": _create, "show": _show, "list": _list, "terminate": _terminate}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    client = RunpodClient()
    try:
        return COMMANDS[args.command](args, client)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except ValueError as exc:
        # The client raises this for an instance type that is neither one thing nor the other.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except RunpodError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR
    finally:
        client.close()
