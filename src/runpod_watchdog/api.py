"""Runpod REST API v2 client.

Four jobs and no more: read one pod, read its logs, stop it, terminate it. This module is
transport. It has no retries, no watch loop, and no opinion about what "healthy" means — those
belong to later cycles.

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


class AuthError(RunpodError):
    """HTTP 401. Runpod rejected the key."""


class ForbiddenError(RunpodError):
    """HTTP 403. The key is valid but lacks access to this pod or action."""


class PodNotFoundError(RunpodError):
    """HTTP 404. No pod with that id."""


class ConflictError(RunpodError):
    """HTTP 409. The action is not valid for the pod's current status."""


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

    Not necessarily a fault — a quiet pod is quiet. `read_pod_logs` treats it as the end of a
    batch rather than as an error.
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


def _raise_for_status(response: requests.Response, pod_id: str, key: str) -> None:
    status = response.status_code
    if status < 400:
        return

    detail = _detail(response, key)
    said = f" Runpod said: {detail}" if detail else ""

    if status == 401:
        raise AuthError(
            f"Runpod rejected the API key. Check the {API_KEY_ENV} environment variable.{said}"
        )
    if status == 403:
        raise ForbiddenError(
            f"That API key is not allowed to do this to pod {pod_id!r}.{said}"
        )
    if status == 404:
        raise PodNotFoundError(f"Runpod has no pod with id {pod_id!r}.{said}")
    if status == 409:
        raise ConflictError(
            f"Runpod refused that action for pod {pod_id!r} in its current status.{said}"
        )
    if status == 429:
        raise RateLimitedError(f"Runpod is rate limiting this API key.{said}")
    if 500 <= status < 600:
        raise ServerError(f"Runpod had a server error (HTTP {status}).{said}")
    raise UnexpectedStatusError(
        f"Runpod returned an unexpected HTTP {status} for pod {pod_id!r}.{said}"
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
        pod_id: str,
        json_body: dict | None = None,
        params: dict | None = None,
        extra_headers: dict[str, str] | None = None,
        stream: bool = False,
    ) -> requests.Response:
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
            _raise_for_status(response, pod_id, key)
        except RunpodError:
            response.close()
            raise
        return response

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
        after it. If the pod says nothing at all, this returns when the stream goes idle — so the
        real worst case is the read timeout, not `seconds`. That is a property of a pushed stream:
        there is nothing to return early from while the connection is simply quiet.
        """
        deadline = time.monotonic() + seconds
        events: list[LogEvent] = []
        stream = self.stream_pod_logs(pod_id, **kwargs)  # type: ignore[arg-type]
        try:
            for event in stream:
                events.append(event)
                if time.monotonic() >= deadline:
                    break
        except LogStreamIdle:
            pass  # A quiet pod is not a broken pod. What arrived is the batch.
        finally:
            stream.close()
        return events
