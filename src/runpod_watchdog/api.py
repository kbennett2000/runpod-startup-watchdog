"""Runpod REST API v2 client.

Six jobs and no more: read one pod, read its logs, stop it, start it, terminate it, and — for the
proving run only — create one and list them. This module is transport. It has no retries, no watch
loop, and no opinion about what "healthy" means.

`create_pod` and `list_pods` are here for the `runpod-watchdog-pod` command, not for the watchdog:
the watchdog watches a pod somebody else created and never makes one. See
docs/adr/0006-pod-lifecycle-is-a-separate-command.md.

Every path, method, parameter, and status code here was read out of Runpod's own OpenAPI document
at https://api.runpod.io/v2/openapi.json, snapshot dated 2026-07-30 (OpenAPI 3.1.0, info.version
2.0.0). None of it is recalled. See docs/adr/0003-api-operations.md for which operations were
chosen and what was ambiguous.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import requests

# The spec's `servers` entry is the bare host. Every path key in the document already begins with
# `/v2`, so this must NOT have `/v2` appended — that would produce `/v2/v2/pods`.
BASE_URL = "https://api.runpod.io"

# The only place the API key ever comes from.
API_KEY_ENV = "RUNPOD_API_KEY"

# (connect, read) seconds. Every request carries one; nothing here may block forever.
DEFAULT_TIMEOUT = (10.0, 30.0)

# The log endpoint holds a connection open and pushes events, so its read timeout means "how long
# the stream may say nothing before we treat it as idle", not "how long the whole call may take".
# The document declares no heartbeat interval, so this is the client's choice, not the spec's.
DEFAULT_LOG_TIMEOUT = (10.0, 30.0)


@dataclass(frozen=True)
class LogEvent:
    """One line from a pod's log stream.

    `ts`, `source`, and `line` come from the JSON `data:` payload the spec documents. `event_id`
    is the SSE `id:` line, which the spec says carries the event timestamp so a reconnect can
    resume from it with the `Last-Event-ID` header.
    """

    ts: str
    source: str
    line: str
    event_id: str | None = None


# --- errors -----------------------------------------------------------------------------------
#
# One class per thing that can go wrong, each carrying one plain sentence. They are separate
# classes rather than one error with a status number because the watch loop in a later cycle has
# to tell "stop watching, the pod is gone" apart from "back off and try again".


class RunpodError(Exception):
    """Anything that stopped a Runpod call from succeeding."""


class MissingApiKeyError(RunpodError):
    """No API key in the environment. Raised before anything is sent."""


class BadRequestError(RunpodError):
    """HTTP 400. Runpod understood the request and refused it — a bad image, a flavor that does
    not exist, a port string it will not parse."""


class AuthError(RunpodError):
    """HTTP 401. Runpod rejected the key."""


class ForbiddenError(RunpodError):
    """HTTP 403. The key is valid but lacks access to this pod or action."""


class PodNotFoundError(RunpodError):
    """HTTP 404. No pod with that id."""


class ConflictError(RunpodError):
    """HTTP 409. The action is not valid for the pod's current status."""


class UnprocessableEntityError(RunpodError):
    """HTTP 422. The body is shaped wrong — a missing field, a value outside its range."""


class RateLimitedError(RunpodError):
    """HTTP 429. Too many requests for this key."""


class ServerError(RunpodError):
    """HTTP 5xx. Trouble on Runpod's side."""


class UnexpectedStatusError(RunpodError):
    """A status code the spec does not declare for this operation."""


class NetworkError(RunpodError):
    """The request never completed: DNS, connection, TLS, or a dropped stream."""


class LogStreamIdle(RunpodError):
    """The log stream sent nothing for longer than the read timeout.

    Not necessarily a fault — a quiet pod is quiet.

    Do not rely on this to mean "quiet" and `NetworkError` to mean "broken". The live run found
    that requests reports a mid-stream read timeout as `requests.exceptions.ConnectionError`, not
    as `ReadTimeout`, so a quiet stream arrives here as a `NetworkError` instead. Telling the two
    apart would mean reaching past requests into urllib3's exception classes, and it would buy
    nothing: the answer to both is the same. `read_pod_logs` therefore treats *any* interruption as
    the end of a batch. ADR-0005.
    """


def _api_key() -> str:
    """Read the key from the environment on every call, never at import and never cached.

    CLAUDE.md is strict about this: the key comes from one environment variable, is never written
    to a file, and is never printed.
    """
    key = os.environ.get(API_KEY_ENV, "")
    if not key.strip():
        raise MissingApiKeyError(
            f"No Runpod API key. Set the {API_KEY_ENV} environment variable."
        )
    return key


def _redact(text: str, key: str) -> str:
    """Never repeat the API key back, even if Runpod echoes it into an error body."""
    return text.replace(key, "***") if key and key in text else text


def _detail(response: requests.Response, key: str) -> str:
    """The human-readable half of an error body.

    The spec returns errors as `application/problem+json` matching its `ErrorResponse` schema:
    `title`, `status`, `detail`, and an optional `errors` list.
    """
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    for field in ("detail", "title"):
        value = body.get(field)
        if isinstance(value, str) and value.strip():
            return _redact(value.strip(), key)
    return ""


def _raise_for_status(
    response: requests.Response, subject: str, key: str, *, pod_id: str | None
) -> None:
    """Turn a failed response into one plain sentence.

    `subject` names what the call was about, so the same mapping serves both the pod-scoped calls
    ("pod 'abc123'") and the two that are not ("a new pod", "your pod list"). `pod_id` is None for
    the calls with no pod, which is what makes a 404 on those an unexpected status rather than a
    missing pod — `POST /v2/pods` returning 404 means the route is gone, not that a pod is.
    """
    status = response.status_code
    if status < 400:
        return

    detail = _detail(response, key)
    said = f" Runpod said: {detail}" if detail else ""

    if status == 400:
        raise BadRequestError(f"Runpod rejected the request for {subject}.{said}")
    if status == 401:
        raise AuthError(
            f"Runpod rejected the API key. Check the {API_KEY_ENV} environment variable.{said}"
        )
    if status == 403:
        raise ForbiddenError(f"That API key is not allowed to act on {subject}.{said}")
    if status == 404 and pod_id is not None:
        raise PodNotFoundError(f"Runpod has no pod with id {pod_id!r}.{said}")
    if status == 409:
        raise ConflictError(
            f"Runpod refused that action for {subject} in its current status.{said}"
        )
    if status == 422:
        raise UnprocessableEntityError(f"Runpod could not process the request for {subject}.{said}")
    if status == 429:
        raise RateLimitedError(f"Runpod is rate limiting this API key.{said}")
    if 500 <= status < 600:
        raise ServerError(f"Runpod had a server error (HTTP {status}).{said}")
    raise UnexpectedStatusError(
        f"Runpod returned an unexpected HTTP {status} for {subject}.{said}"
    )


# --- Server-Sent Events -------------------------------------------------------------------------


def _iter_sse_events(lines: Iterable[bytes | str]) -> Iterator[tuple[str | None, str]]:
    """Turn raw stream lines into `(event_id, data)` pairs.

    Server-Sent Events is a line format: `field: value` lines accumulate, a blank line ends one
    event, and a line starting with `:` is a comment used as a keep-alive.
    """
    event_id: str | None = None
    data: list[str] = []

    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw

        if not line:
            if data:
                yield event_id, "\n".join(data)
            event_id, data = None, []
            continue
        if line.startswith(":"):
            continue

        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "id":
            event_id = value
        elif field == "data":
            data.append(value)

    if data:
        yield event_id, "\n".join(data)


def _log_event(event_id: str | None, data: str) -> LogEvent | None:
    """Build a LogEvent from one event's `data:` payload, or None if it is not usable.

    A malformed event is skipped rather than raised. A watchdog that crashed on one bad log line
    would leave the pod it was supposed to be guarding running.
    """
    try:
        payload = json.loads(data)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return LogEvent(
        ts=str(payload.get("ts", "")),
        source=str(payload.get("source", "")),
        line=str(payload.get("line", "")),
        event_id=event_id,
    )


# --- the client -----------------------------------------------------------------------------------


class RunpodClient:
    """Talks to Runpod REST API v2 over plain HTTP.

    The API key is deliberately not held on the instance. It is read from the environment on
    every request, so a client built before the key is set still works, and nothing long-lived
    holds a copy of the secret.
    """

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        log_timeout: tuple[float, float] = DEFAULT_LOG_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.log_timeout = log_timeout
        self.session = session or requests.Session()

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> RunpodClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- one place where every request is built, so the timeout, the auth header, and the error
    # -- mapping cannot drift apart between operations.
    def _request(
        self,
        method: str,
        path: str,
        *,
        pod_id: str | None,
        subject: str | None = None,
        json_body: dict | None = None,
        params: dict | None = None,
        extra_headers: dict[str, str] | None = None,
        stream: bool = False,
    ) -> requests.Response:
        # Every call is about one pod except `createPod` and `listPods`, which pass their own
        # subject and no pod id.
        subject = subject if subject is not None else f"pod {pod_id!r}"
        key = _api_key()
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        if extra_headers:
            headers.update(extra_headers)

        url = f"{self.base_url}{path}"
        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                params=params,
                timeout=self.log_timeout if stream else self.timeout,
                stream=stream,
            )
        except requests.RequestException as exc:
            raise NetworkError(f"Could not reach Runpod at {url}: {exc}") from exc

        try:
            _raise_for_status(response, subject, key, pod_id=pod_id)
        except RunpodError:
            response.close()
            raise
        return response

    def create_pod(
        self,
        *,
        name: str,
        image: str,
        gpu: dict | None = None,
        cpu: dict | None = None,
        args: str | None = None,
        ports: list[str] | None = None,
        disk: int | None = None,
        cloud: str | None = None,
        data_center_ids: list[str] | None = None,
    ) -> dict:
        """POST /v2/pods — the `createPod` operation. Returns the created pod.

        Body fields, all read out of the spec's `CreatePodRequest` (which is `ContainerConfig`
        plus a few pod-only keys):

        - `name`, `image` — the only two the spec marks required.
        - `gpu` / `cpu` — the instance type. The spec says "supply exactly one of `gpu` or `cpu`",
          enforced on Runpod's side, and checked here too so a mistake costs a `ValueError` instead
          of a round trip. `gpu` is `{"id": ..., "count": ...}`; `cpu` is `{"id": ..., "vcpuCount":
          ...}` with the flavor id from `GET /v2/catalog/cpus` and a vCPU count that must be a
          power of two.
        - `args` — the container start command. The spec calls it "Arguments passed to the
          container entrypoint"; it is the field the legacy GraphQL surface named `dockerArgs`.
        - `ports` — exposed ports as `port/protocol` strings, the same shape a pod reports back.
        - `disk` — container disk in GB, ephemeral and wiped on restart.
        - `cloud` — `SECURE` or `COMMUNITY`. Omitted means Runpod's default, `SECURE`.
        - `data_center_ids` — sent as `dataCenterIds`. Omitted lets the scheduler choose.

        Anything left as None is left out of the body entirely rather than sent as null, so
        Runpod's own defaults apply. `CreatePodRequest` sets `unevaluatedProperties: false`, so a
        key that is not in the schema is rejected outright — there is no room to send extras.

        The documented success is 201 with the pod, and the pod comes back `PROVISIONING`: the
        spec says plainly that provisioning is asynchronous and that a caller should poll rather
        than assume the pod is running when this returns. Watching that gap is the whole job of
        this tool.
        """
        if (gpu is None) == (cpu is None):
            raise ValueError(
                "create_pod needs exactly one of gpu or cpu: a pod is either a GPU pod or a CPU pod."
            )

        body: dict[str, object] = {"name": name, "image": image}
        optional: dict[str, object | None] = {
            "gpu": gpu,
            "cpu": cpu,
            "args": args,
            "ports": ports,
            "disk": disk,
            "cloud": cloud,
            "dataCenterIds": data_center_ids,
        }
        body.update({key: value for key, value in optional.items() if value is not None})

        response = self._request(
            "POST", "/v2/pods", pod_id=None, subject="a new pod", json_body=body
        )
        return response.json()

    def list_pods(self) -> list[dict]:
        """GET /v2/pods — the `listPods` operation. Returns every pod on the account.

        The spec wraps the array in a `{"pods": [...]}` envelope; this unwraps it, because the
        envelope carries nothing else — no paging, no total. The operation takes no parameters.

        This exists so a run can be proved clean afterwards: list the account and show that
        nothing the run created is still there.
        """
        response = self._request("GET", "/v2/pods", pod_id=None, subject="your pod list")
        body = response.json()
        pods = body.get("pods") if isinstance(body, dict) else None
        return pods if isinstance(pods, list) else []

    def get_pod(self, pod_id: str) -> dict:
        """GET /v2/pods/{id} — the `getPod` operation. Returns the pod as the API reports it.

        The status field is `status`, one of PROVISIONING, STARTING, RUNNING, EXITED, ERROR, or
        TERMINATED. There is no `desiredStatus` field on this API surface.
        """
        response = self._request("GET", f"/v2/pods/{pod_id}", pod_id=pod_id)
        return response.json()

    def stop_pod(self, pod_id: str) -> dict:
        """POST /v2/pods/{id}/action with {"action": "stop"} — the `podAction` operation.

        Releases compute and keeps the pod's disk, so the pod moves to EXITED. Storage charges
        continue on a stopped pod; `terminate_pod` is the one that ends those too.

        Returns the updated pod. Raises ConflictError on HTTP 409 if stopping is not valid for the
        pod's current status. The spec lists `stop` as permitted from PROVISIONING, STARTING, and
        RUNNING, which covers the whole startup window this tool watches.
        """
        response = self._request(
            "POST",
            f"/v2/pods/{pod_id}/action",
            pod_id=pod_id,
            json_body={"action": "stop"},
        )
        return response.json()

    def start_pod(self, pod_id: str) -> dict:
        """POST /v2/pods/{id}/action with {"action": "start"} — the `podAction` operation.

        Boots a stopped pod back toward RUNNING. The spec permits `start` only from EXITED or
        ERROR, so this raises ConflictError on HTTP 409 if called on a pod that is still running or
        still provisioning.

        This exists for `--retry`. The spec's `restart` action is not usable for that job: it
        requires a RUNNING pod, and a pod that failed to start usually is not one. Stop-then-start
        works from every status this tool encounters. ADR-0004.

        Returns the updated pod.
        """
        response = self._request(
            "POST",
            f"/v2/pods/{pod_id}/action",
            pod_id=pod_id,
            json_body={"action": "start"},
        )
        return response.json()

    def terminate_pod(self, pod_id: str) -> None:
        """DELETE /v2/pods/{id} — the `deletePod` operation. Irreversible.

        The spec offers a second, explicitly equivalent route (`podAction` with
        `{"action": "terminate"}`). This one is used because the spec declares no 409 on it, so
        terminating cannot fail because the pod's status changed underneath us. ADR-0003.

        Returns nothing: the documented success is 204 with no body.
        """
        response = self._request("DELETE", f"/v2/pods/{pod_id}", pod_id=pod_id)
        response.close()

    def stream_pod_logs(
        self,
        pod_id: str,
        *,
        source: str | None = None,
        tail: int | None = None,
        since: str | None = None,
        last_event_id: str | None = None,
    ) -> Iterator[LogEvent]:
        """GET /v2/pods/{id}/logs — the `getPodLogs` operation. Yields events as they arrive.

        This endpoint is a Server-Sent Events stream, not a JSON document: Runpod holds the
        connection open and pushes one event per log line. Breaking out of the loop closes it.

        Parameters, and the precedence the spec documents for them:
        - `source` — "container" or "system". Omit for both.
        - `tail` — historical lines to backfill first. Defaults to 100 when omitted, maximum 5000,
          0 for live-only. Ignored when `since` or `last_event_id` is given.
        - `since` — RFC3339 timestamp to resume from. Ignored when `last_event_id` is given.
        - `last_event_id` — sent as the `Last-Event-ID` header. Beats both of the above.

        Raises LogStreamIdle if the stream says nothing for longer than the read timeout.
        """
        params: dict[str, object] = {}
        if source is not None:
            params["source"] = source
        if tail is not None:
            params["tail"] = tail
        if since is not None:
            params["since"] = since

        headers = {"Accept": "text/event-stream"}
        if last_event_id is not None:
            headers["Last-Event-ID"] = last_event_id

        response = self._request(
            "GET",
            f"/v2/pods/{pod_id}/logs",
            pod_id=pod_id,
            params=params or None,
            extra_headers=headers,
            stream=True,
        )
        try:
            for event_id, data in _iter_sse_events(response.iter_lines()):
                event = _log_event(event_id, data)
                if event is not None:
                    yield event
        except requests.exceptions.ReadTimeout as exc:
            raise LogStreamIdle(
                f"Pod {pod_id!r} sent no log output for {self.log_timeout[1]} seconds."
            ) from exc
        except requests.RequestException as exc:
            raise NetworkError(f"Lost the log stream for pod {pod_id!r}: {exc}") from exc
        finally:
            response.close()

    def read_pod_logs(self, pod_id: str, *, seconds: float, **kwargs: object) -> list[LogEvent]:
        """Collect log events for up to `seconds`, then close the stream.

        The deadline is checked after each event, so collecting stops at the first event at or
        after it. If the pod says nothing at all, this returns when the stream goes quiet — so the
        real worst case is the read timeout, not `seconds`. That is a property of a pushed stream:
        there is nothing to return early from while the connection is simply quiet.

        **Whatever ends the stream, the lines already collected are kept and returned.** Going
        quiet and dropping the connection are not told apart, because requests reports both the
        same way (see `LogStreamIdle`) and because the right response to both is identical: keep
        what arrived, and let the next call resume from the last event id. Throwing the batch away
        was a real bug, found on the live run and fixed here — every log read ends this way, so a
        watchdog that discarded the batch each time would never see a phrase at all. ADR-0005.

        A failure that happens before the stream opens — no such pod, a rejected key — is raised
        rather than swallowed, because it is not an interrupted batch. Those are raised by
        `_request` on the first iteration, and only `NetworkError` is caught here.
        """
        deadline = time.monotonic() + seconds
        events: list[LogEvent] = []
        stream = self.stream_pod_logs(pod_id, **kwargs)  # type: ignore[arg-type]
        try:
            for event in stream:
                events.append(event)
                if time.monotonic() >= deadline:
                    break
        except (LogStreamIdle, NetworkError):
            pass  # A quiet pod is not a broken pod, and a broken stream is not a lost batch.
        finally:
            stream.close()
        return events
