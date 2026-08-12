# 0006 — Creating and listing pods lives in a second command, not in the watchdog

Status: Accepted

## Context

ADR-0003 chose four operations and wrote down that the rest of the surface was deliberately unused:
"Creating pods, listing them, and billing are all in the document and all deliberately unused: this
watches one pod that the user already created."

The live proving run needs two of those back. A run that cannot be reproduced is not a receipt, and
reproducing one means creating the same pod again — so the pod that gets watched has to come from
this repository, not from a browser tab. Afterwards the account has to be listed to prove nothing was
left running.

Both operations are read from the same OpenAPI document as the other four: served without
authentication at <https://api.runpod.io/v2/openapi.json>, snapshot dated **2026-07-30**, re-checked
on 2026-08-12 and unchanged in shape — OpenAPI 3.1.0, `info.version` 2.0.0, 29 paths, 44 operations.

## Decision

Add `createPod` and `listPods` to the client, and put them behind a **second command**,
`runpod-watchdog-pod`, with four subcommands: `create`, `show`, `list`, `terminate`.

The watchdog command, `runpod-watchdog`, is unchanged. It still cannot create a pod.

### Why two commands and not more flags

The watchdog's whole job is deciding to stop something. The tool that makes that decision should not
also be the tool that can make the thing — someone reading `runpod-watchdog --help` should not have
to check whether the flag they are about to type spends money. Keeping them apart also keeps the
watchdog honest in the transcripts: when a pod is terminated by `runpod-watchdog-pod terminate`, the
line it prints says so, and the watchdog's own output shows it changed nothing.

`show` and `list` are here for the same reason `create` is: the proving run has to be able to read a
pod back and print the account afterwards, from the repository, without a browser.

### The create body, field by field

`CreatePodRequest` is `ContainerConfig` plus a few pod-only keys, and it sets
`unevaluatedProperties: false` — a key that is not in the schema is rejected outright. The command
sends only these, and leaves out anything the user did not ask for so Runpod's own defaults apply:

| Flag | Body field | Note |
| --- | --- | --- |
| `--name` | `name` | Required by the spec |
| `--image` | `image` | Required by the spec |
| `--cpu`, `--vcpu` | `cpu` | `{"id", "vcpuCount"}` |
| `--gpu`, `--gpu-count` | `gpu` | `{"id", "count"}` |
| `--args` | `args` | The container start command |
| `--port` (repeatable) | `ports` | `port/protocol` strings |
| `--disk` | `disk` | GB, ephemeral |
| `--cloud` | `cloud` | `SECURE` or `COMMUNITY` |
| `--data-center` (repeatable) | `dataCenterIds` | The one flag whose name differs from its field |

Three rules are checked locally before a request is spent, all of them the spec's own:

1. **Exactly one of `gpu` or `cpu`.** The spec says so and enforces it at its handler; checking here
   turns a round trip into a sentence.
2. **`vcpuCount` at least 2 and a power of two.** The spec's words for the field.
3. **A port is `number/protocol`.** The shape the spec gives for `ports` items.

### Two error codes the client did not map before

`createPod` declares `400` and `422`, which nothing among the original four did. They now have their
own classes — `BadRequestError` and `UnprocessableEntityError` — because they say different things:
`400` is Runpod refusing a request it understood (no such CPU flavor), `422` is a body shaped wrong.
Both carry Runpod's own `detail` string, which is where the useful part is.

A `404` on a call that names no pod is **not** a missing pod. `POST /v2/pods` returning `404` means
the route is gone. It raises `UnexpectedStatusError`, so nobody goes looking for the wrong problem.

### JSON output hides environment variable values

`show --json` and `list --json` replace every value in `env` with `***`, keeping the keys. This
command exists to produce transcripts that get committed to a public repository, and environment
variables are where people put secrets. That a variable is set is worth seeing; what is in it is not
worth leaking. The plain (non-JSON) output never prints `env` at all.

The API key itself was never at risk here — it lives only in `RUNPOD_API_KEY`, is read fresh on every
request, and is redacted out of any error body that echoes it (ADR-0003). This is about the pod's
variables, not the account's key.

## Consequences

- The client now speaks six operations. ADR-0003's list of four is amended by this document, not
  overturned: the watchdog still uses exactly those four, and the two new ones are reachable only
  from the other command.
- `runpod-watchdog-pod create --dry-run` prints the exact body a real run would send and sends
  nothing, so the proving run can be rehearsed for free and so can anyone else's.
- Dollar figures are printed as "Runpod account spend" wherever they appear, because a bare number
  next to a pod is ambiguous about whose money it is.
- Terminating from this command prints "This was a manual termination, not the watchdog," which is
  what makes the proving-run transcripts readable as evidence rather than as a claim.
