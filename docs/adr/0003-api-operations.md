# 0003 — Which REST v2 operations the watchdog uses, and what the spec left ambiguous

Status: Accepted. Amended by [ADR-0006](0006-pod-lifecycle-is-a-separate-command.md), which adds
`createPod` and `listPods` to the client for the `runpod-watchdog-pod` command. The watchdog itself
still uses exactly the four operations below.

## Context

ADR-0001 settled the surface: plain HTTP against Runpod's REST API v2. This ADR settles which
operations on that surface the tool actually calls, and records the places where the document
offered more than one answer.

Everything below was read out of Runpod's own OpenAPI document, served without authentication at
<https://api.runpod.io/v2/openapi.json>, from a snapshot dated **2026-07-30** — OpenAPI 3.1.0,
`info.version` 2.0.0, 29 paths, 44 operations, every operation carrying an `operationId`. None of it
is recalled and none of it is inferred from the older GraphQL surface.

No copy of that document lives in this repository. It carries no licence grant, so there is no
permission to redistribute it; the tool cites it by URL and pins it by date instead. The tool never
downloads a specification at runtime either — the endpoint shapes are written into the code.

## Decision

Four operations, and no others.

| Job | `operationId` | Method and path | Documented success |
| --- | --- | --- | --- |
| Read one pod | `getPod` | `GET /v2/pods/{id}` | `200` with the pod |
| Read its logs | `getPodLogs` | `GET /v2/pods/{id}/logs` | `200`, `text/event-stream` |
| Stop it | `podAction` | `POST /v2/pods/{id}/action`, body `{"action": "stop"}` | `200` with the updated pod |
| Terminate it | `deletePod` | `DELETE /v2/pods/{id}` | `204`, no body |

Authentication is the document's `bearerAuth` scheme, applied globally by its top-level `security`
block: `Authorization: Bearer <key>`. The key is read from `RUNPOD_API_KEY` on every request, never
at import and never cached on the client, so nothing long-lived holds a copy of the secret.

Every request carries a `(connect, read)` timeout. Nothing in this module may block forever.

### The ambiguities, and how each was resolved

**1. Two routes terminate a pod, and the spec says they are equivalent.**
`DELETE /v2/pods/{id}` and `POST /v2/pods/{id}/action` with `{"action": "terminate"}` do the same
job; the action route's own description says "equivalent to `deletePod`". We use `DELETE`.

The reason is failure behaviour, not taste. The action route declares `409` — "Action not valid for
current pod status" — because which transitions are legal depends on the pod's current status.
`DELETE` declares no `409`. Terminating is the last thing this tool does after deciding a pod is
broken, and a pod that is in the middle of breaking is exactly the pod whose status is changing
underneath us. The route with no status precondition is the right one for the last resort.

A second reason: the action route returns `204` only when the action happens to be `terminate`, and
`200` with a body otherwise. A response type that depends on the request body is a worse thing to
depend on than one that does not.

Stopping has no such choice — `podAction` is the only route that stops a pod.

**2. `desiredStatus` does not exist on this surface.**
CLAUDE.md names `desiredStatus` as the field whose `RUNNING` value is misleading. The string does not
appear anywhere in the v2 document; it belongs to the legacy GraphQL surface. The field here is
`status`, typed by a `PodStatus` enum: `PROVISIONING`, `STARTING`, `RUNNING`, `EXITED`, `ERROR`,
`TERMINATED`.

The warning behind the old name survives intact and is the reason this project exists: `RUNNING`
means the container is up, not that the workload inside it is usable, and the image may still be
downloading. Only the field name changes.

Usefully, the document also states which transitions each status permits: `PROVISIONING` and
`STARTING` both allow `stop` and `terminate`. That is the whole window this tool operates in, so
acting during startup is supported rather than a trick.

**3. The base URL is the bare host, but every path already carries `/v2`.**
`servers` is `https://api.runpod.io` with no path, while every one of the 29 path keys begins
`/v2/`. So the base URL must not have `/v2` appended — doing so produces `/v2/v2/pods`. There is a
test asserting the built URL contains no repeated version segment, because this is the kind of
mistake that is invisible until it 404s.

**4. Logs are a stream, not a document.**
`getPodLogs` returns `text/event-stream` — Server-Sent Events, a connection Runpod holds open while
pushing one event per log line. Each event has an `id:` line and a JSON `data:` payload of
`{"ts", "source", "line"}`.

Its parameters have a documented precedence that is easy to get backwards: `Last-Event-ID` (a
header) beats `since`, and `since` makes `tail` ignored. `tail` defaults to 100 when omitted, so
`tail=0` — live only, no backfill — must be sent rather than treated as "unset", or the caller
silently gets 100 backfilled lines instead of none. There is a test for that specific confusion.

The module exposes both shapes: `stream_pod_logs` yields events as they arrive, and `read_pod_logs`
collects until a deadline and then closes the connection.

**5. The document declares no heartbeat interval for that stream.**
So no safe read timeout can be derived from the spec — it is the client's choice. The consequence is
written into the code: a read timeout partway through a stream raises `LogStreamIdle`, which
`read_pod_logs` treats as the end of a batch rather than as a failure. A quiet pod is not a broken
pod.

This leaves one honest limitation. `read_pod_logs(seconds=…)` checks its deadline after each event,
so if the pod says nothing at all, the call returns when the stream goes idle rather than at the
deadline. The real worst case is the read timeout, not `seconds`. Bounding it tighter would need a
thread or a selector, which is more machinery than this cycle needs.

**6. `Pod` is an `allOf` composition.**
Its required fields are spread across two subschemas, so there is no single flat list of what a pod
response contains. This cycle returns the parsed JSON unchanged and models no `Pod` object. A later
cycle can, once it knows which fields the watch loop actually reads.

### Errors become plain sentences

The document returns errors as `application/problem+json` matching its `ErrorResponse` schema:
`title`, `status`, `detail`, and an optional `errors` list. Each declared status maps to its own
exception class carrying one plain sentence, with `detail` appended when the body has one:

`401` → `AuthError` (names the environment variable) · `403` → `ForbiddenError` ·
`404` → `PodNotFoundError` · `409` → `ConflictError` · `429` → `RateLimitedError` ·
`5xx` → `ServerError` · anything else → `UnexpectedStatusError` · a request that never completed →
`NetworkError` · no key in the environment → `MissingApiKeyError`, raised before anything is sent.

They are separate classes rather than one error carrying a status number because the watch loop in a
later cycle has to tell "stop watching, the pod is gone" apart from "back off and try again". All of
them share a `RunpodError` base so a caller can still catch everything in one place.

The API key is never repeated back. If an error body ever quotes it, it is redacted before the
message is built, and a test asserts the key appears in no message.

## Consequences

- The tool speaks four endpoints. Creating pods, listing them, and billing are all in the document
  and all deliberately unused: this watches one pod that the user already created.
- No retries live here. A `429` or a `5xx` raises, and the cycle that owns the watch loop decides
  what backing off means. Putting retry policy in the transport would hide time from the component
  whose entire job is counting it.
- The command line is not wired to this module yet, so nothing in the shipped tool can make a
  network call this cycle. The test suite runs against a mocked HTTP layer, and a separate test
  proves that importing the package opens no connection.
- The snapshot is dated. Runpod marks v2 beta and serves the document rather than releasing it, so
  it can change without a version bump. When it does, this ADR is the record of what was true on
  2026-07-30 and what to re-check.
