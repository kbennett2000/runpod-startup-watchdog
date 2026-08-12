# 0005 — What the live proving run found

Status: Accepted

## Context

Cycles 1 to 3 were built entirely against a mocked API and Runpod's OpenAPI document. ADR-0004
ended with a list of things only a live run could settle, and named the most important one:

> **The `--port` signal is the risky one, and this is the thing to check first on the live proving
> run.** [...] runpodctl's `pod.go` says the REST read shape leaves `runtime` null *even on a
> running pod*, "verified against prod", which is why their pod wait uses GraphQL instead. If that
> is still true, a `--port`-only run stops every pod at its deadline no matter how healthy it is.

On **2026-08-12**, between 16:49 and 16:59 UTC, that run happened. Two pods, both on `cpu3c` with
2 vCPU — the cheapest flavor Runpod offers at 0.03 US dollars per vCPU per hour of Runpod account
spend, so 0.06 per hour per pod. Both terminated. Total elapsed pod time under fifteen minutes.

The transcripts are the evidence and they are committed, not summarised:

| Transcript | What it covers |
| --- | --- |
| [demo/00-baseline-and-catalog.txt](../../demo/00-baseline-and-catalog.txt) | The account before anything was created; how the instance was chosen |
| [demo/01-port-field-question.txt](../../demo/01-port-field-question.txt) | The `--port` question, and the raw pod response |
| [demo/02-proving-run-a-broken-pod.txt](../../demo/02-proving-run-a-broken-pod.txt) | Run A: a crash-looping pod, stopped, exit 4 |
| [demo/03-proving-run-b-healthy-pod.txt](../../demo/03-proving-run-b-healthy-pod.txt) | Run B: a healthy pod, exit 0, untouched |
| [demo/04-proving-run-c-dry-run.txt](../../demo/04-proving-run-c-dry-run.txt) | Run C: `--dry-run`, exit 3, nothing changed |
| [demo/05-account-after.txt](../../demo/05-account-after.txt) | Nothing left running or stopped |

## Findings

### 1. The public port address IS published on REST v2. `--port` works.

A `RUNNING` pod read over `GET /v2/pods/{id}` returns `runtime.ports` populated, and the entry for
the requested port carries a real, publicly routable address:

```json
"runtime": {
  "cpu": {"util": 0},
  "memory": {"util": 0},
  "ports": [
    {"ip": "100.65.23.30",  "private": 19123, "public": 60223, "type": "http"},
    {"ip": "38.80.152.147", "private": 80,    "public": 39998, "type": "tcp"}
  ],
  "uptime": 390
}
```

That is exactly the shape `watch.py`'s `public_mapping()` already reads — match on `private`, require
`ip` and `public` to be non-null. The watchdog probed `38.80.152.147:39998`, nginx answered, and the
`--port` signal fired (demo/03).

**So the concern ADR-0004 recorded from runpodctl's source does not hold on this surface on this
date.** ADR-0004's consequence section is answered, not overturned: everything it says about *why*
a real TCP connection is required still stands, and its warning that `runtime` is null early is also
confirmed below.

Three qualifications, all of which matter:

- **`null` early is normal.** `runtime.ports` was still `null` in the response to the create call at
  16:50:14 and populated by the first poll at 16:50:38. The watchdog already reports that as "no
  public mapping published yet" rather than as a fault, which is correct.
- **Order is not stable.** The two entries come back in different orders on different reads (compare
  the 16:55:52 and 16:56:08 lines in demo/01). Nothing may depend on position. `public_mapping()`
  matches on the `private` field, so it is unaffected — but this is now a rule with a receipt.
- **Not every entry is reachable.** The `type: "http"` entry has ip `100.65.23.30`, inside
  `100.64.0.0/10` — the range reserved for carrier-grade NAT, so it is not routable from outside.
  Only the `tcp` entry carries a public address. A port exposed as `N/http` may therefore get a
  mapping the TCP probe can never reach. **Expose a port as `N/tcp` if you intend to watch it with
  `--port`.** This is now in the README.

### 2. A pod reports `RUNNING` while its image is still downloading. Observed directly.

This is the premise of the entire project, and it turned out to be stronger than CLAUDE.md's
version of it. Three timestamps from demo/01, all from the same pod:

| Time (UTC) | What |
| --- | --- |
| 16:50:14 | `createPod` returns, `status: "RUNNING"` |
| 16:50:27 | nginx prints `start worker processes` — the server actually starts |
| 16:50:38 | `runtime.ports` first appears |

The pod's own log across that window shows the image still being fetched: `Extracting`,
`Pull complete`, `Status: Downloaded newer image for nginx:alpine`. The API called the pod `RUNNING`
thirteen seconds before the software inside it began serving, and twenty-four seconds before there
was an address to connect to.

It is also stronger than the OpenAPI document's own description of `createPod`, which says: "the pod
starts in `PROVISIONING`, transitions through `STARTING`, and reaches `RUNNING` once its container is
healthy." Across every read taken in this session, **neither pod was ever observed in
`PROVISIONING` or `STARTING`.** Both were `RUNNING` in the create response itself.

The practical consequence for anyone using this tool: **status is worth nothing as a readiness
signal, and `--max-minutes` has to be generous enough to cover an image pull.** A cold pull of a
20 MB image took about thirteen seconds here; a multi-gigabyte machine-learning image will take
minutes, and for all of them the pod says `RUNNING` the whole time.

### 3. A bug in the log reader that only a live stream could expose

`read_pod_logs` was throwing away every log line it had collected.

The mechanism: `stream_pod_logs` caught `requests.exceptions.ReadTimeout` to detect a quiet stream
and raise `LogStreamIdle`, which `read_pod_logs` handled as "end of batch". But requests does not
raise `ReadTimeout` for a timeout that happens *partway through* a streamed body. `iter_content`
catches urllib3's `ReadTimeoutError` and re-raises it as `requests.exceptions.ConnectionError`:

```python
except ReadTimeoutError as e:
    raise ConnectionError(e)
```

So the idle case arrived as `NetworkError`, `read_pod_logs` did not catch it, and the exception
propagated out — taking the collected events with it. `watch.py`'s `_drain_logs` then caught the
`RunpodError` and returned an empty list.

**Every log read ends by going quiet.** So this was not an edge case: the watchdog would have seen
no log lines at all, and `--success-phrase` and `--failure-phrase` — the two signals ADR-0004 calls
the more dependable ones — would never have fired. Every phrase-based run would have ended in a
timeout. The mocked tests all passed, because a mock stream ends cleanly instead of going quiet.

**The fix:** `read_pod_logs` keeps what it collected and returns it, whatever ended the stream. It
no longer tries to tell "quiet" from "dropped", because requests reports them identically and the
right answer to both is the same — keep what arrived, and let the next poll resume from the last
event id. A failure that happens *before* the stream opens (no such pod, rejected key) is still
raised, because that is not an interrupted batch.

Telling the two apart would mean importing urllib3's exception classes directly. CLAUDE.md fixes the
runtime dependencies at requests only, and the distinction buys nothing, so it is not done.

`tests/test_api.py::test_read_pod_logs_keeps_the_lines_it_already_collected` is the regression test,
and it fails against the old code.

### 4. `cost` does not go to zero on a stopped pod

The document describes the field as "Current cost in USD per hour (0.0 when `EXITED` or
`TERMINATED`)". Live, the crash-test pod read `EXITED` with `cost: 0.06` (demo/02).

Nothing in this tool reads `cost` to decide anything, and after this finding nothing should:
**`cost` cannot be used to tell whether a pod is still billing.** It is printed by
`runpod-watchdog-pod` as reported, labelled as Runpod account spend, and that is all.

### 5. A stopped pod's container log is gone

After the watchdog stopped the crash-test pod, re-reading its log returned only `system` events —
the container's own lines, including the failure phrase the verdict was based on, were no longer in
the tail. The last system line is `remove container`.

So **the watchdog's own printed output is the only record of what it saw.** A later reader cannot
re-derive the verdict from the log. That is an argument for the status line the tool already prints
on every poll, and against ever making it quieter.

### 6. Small documentation drift, noted and not acted on

- `GET /v2/catalog/cpus` returns flavor ids like `cpu3c` and `cpu5c`. The document's *example
  response* for that operation shows `cpu3c-2-4`, an id format the live catalog does not return.
  The example on `BaseCpuConfig` (`cpu5c`) does match. Use the catalog, not the example.
- A CPU pod's `cpu` object comes back as `{"id", "memory", "vcpuCount"}` — `memory` is derived by
  the API from the flavor's RAM multiplier, exactly as `CreatePodRequest` describes.

## Consequences

- **`--port` stays.** It is a working signal on REST v2, with the qualification that the port must
  be exposed as `tcp`. The README says so now.
- **The log fix is the load-bearing change of this cycle.** Cycles 1 to 3 shipped a watchdog whose
  two log signals could not fire. Mocked tests cannot find that class of bug, because a mock stream
  ends and a real one goes quiet. That is the argument for the proving run existing at all.
- **`--max-minutes` guidance belongs in the README**, because status is useless as a readiness
  signal and an image pull is invisible from outside.
- Nothing here changes a settled decision from CLAUDE.md. No new flags, no new knobs. The poll
  interval, the failure count, and the probe timeout were all adequate at the sizes ADR-0004 fixed.
- The spec snapshot behind ADR-0003 was re-fetched on 2026-08-12 and is unchanged in shape: OpenAPI
  3.1.0, `info.version` 2.0.0, 29 paths, 44 operations. The drift found is in its examples and two
  field descriptions, not in its routes.
