# 0001 — Talk to Runpod's REST API v2 directly, with plain HTTP

Status: Accepted

## Context

This tool needs six things from Runpod: create a pod, get one pod, list pods, read a pod's logs,
stop a pod, and terminate a pod. There is more than one way to ask Runpod for those, and the choice
is not obvious from the outside, because Runpod currently runs four separate API surfaces at once:

- **REST v2** — the current surface, marked beta. This is where Runpod's own documentation points.
  Its machine-readable description is an OpenAPI 3.1 specification (OpenAPI is a standard file
  format that lists every URL an API accepts), covering 29 paths and 44 operations, and it can be
  fetched without an API key. https://docs.runpod.io/api-reference/
- **REST v1** — deprecated.
- **GraphQL** — the legacy surface. GraphQL is a different style of API where the client sends a
  query describing exactly what it wants, instead of calling one URL per operation.
- **Serverless** — a separate product surface, not pods, not relevant here.

There is also an official Python package, `runpod-python`, which looks like the obvious choice for a
Python tool: https://github.com/runpod/runpod-python

## Decision

Call Runpod's REST v2 endpoints directly over plain HTTP from Python, using `requests`. Do not use
`runpod-python`.

The reason is not a complaint about the package. It is that `runpod-python`'s API layer — the part
that would create, inspect, stop, and terminate pods — rides the legacy GraphQL surface. Depending
on it would mean this tool is built on the surface Runpod is moving away from, while the six
operations it needs all already exist on the current one. Building on the current surface is
deliberate, and it is the point: the intended path for this project is community tool → adopted by
Runpod, following the precedent of
[Runpod-Idle-Pod-Monitor](https://github.com/runpod/Runpod-Idle-Pod-Monitor), a community tool
Runpod moved into its own GitHub organization. A tool built on the legacy surface is harder to
adopt, not easier.

`requests` is therefore the only runtime dependency. Everything else comes from the Python standard
library.

## Consequences

- One runtime dependency, so a clone installs fast and the supply chain stays small.
- The HTTP details — base URL, authentication header, retries, error shapes — are this project's
  code to write and this project's code to test. A later cycle owns that, with its own ADR.
- REST v2 is marked beta. If it changes, this tool changes with it. That is the accepted cost of
  building on the current surface instead of the legacy one, and it is preferable to being pinned to
  a surface that is already legacy on the day the tool ships.
- The v2 specification is version-pinned by hand rather than fetched at runtime: a snapshot dated
  2026-07-30 is the reference for what the endpoints look like. The tool never downloads a spec.
- Authentication uses the API key from the `RUNPOD_API_KEY` environment variable and nowhere else.
  It is never written to a file, never printed, and never committed.
