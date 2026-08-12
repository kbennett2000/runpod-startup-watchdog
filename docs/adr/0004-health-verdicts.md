# 0004 — How the watch loop decides healthy, broken, or out of time

Status: Accepted

## Context

ADR-0001 settled the API surface and ADR-0003 settled which four operations the tool calls. Neither
says what the tool should conclude from what it reads. This ADR does, and it is written before the
loop exists so the reasoning is on record rather than reconstructed from the code.

Two documents are cited throughout, both public and both read rather than recalled:

- Runpod's OpenAPI document for REST v2, served at <https://api.runpod.io/v2/openapi.json>, from the
  snapshot dated **2026-07-30** described in ADR-0003.
- Runpod's own command-line tool, <https://github.com/runpod/runpodctl>, at commit
  **`6fc6f8b517916384db779db7de3288b35d773480`** (2026-08-06). CLAUDE.md requires reading how its
  `--wait` flag decides a pod is usable before writing this logic. The relevant code is the package
  `internal/waitfor` (`waitfor.go`, `pod.go`, `probe.go`) and `internal/sshconnect/sshconnect.go`.

No code was copied from runpodctl. What follows is what it does and which parts we adopt.

### What runpodctl's `--wait` does

It polls `GetPods()` every 5 seconds against a 10-minute default budget, reads the pod's status,
finds the public mapping for port 22 in `runtime.ports`, and then **opens a TCP connection to it**
and reads the SSH banner. A status field alone is never enough for it.

The comment on that probe is the single most useful thing in the repository, so it is quoted here in
full rather than paraphrased:

> Why this is not just "is port 22 listed in runtime.ports": that only says the port was
> *allocated*. Verified against prod with a cpu pod running `alpine:3.20 sleep infinity` — the api
> reported privatePort 22 / isIpPublic with a public port within ~25s, while a tcp connect to it was
> refused, because that image runs no sshd. Port allocation is therefore not readiness.

Three of its choices are adopted here:

1. **A real connection, not a field read.** For the reason quoted above.
2. **Transient by default.** Only `400`, `401` and `403` end one of its waits. `404`, `429` and
   `5xx` keep waiting, because — its words — "the resource exists and is billing, so giving up early
   is the expensive answer." A watchdog that quits on one bad response leaves the pod it was
   guarding running.
3. **An injected clock.** Its `Options.Now` and `Options.Sleep` exist so the wait tests run
   instantly. `watch.py` takes the same two seams for the same reason.

Two are deliberately not adopted:

- **The SSH banner check.** runpodctl knows the service behind port 22 speaks SSH. This tool does
  not know what the user's port speaks, so it cannot verify a protocol.
- **The 5-second interval.** This tool polls every 10 seconds, which is what the cycle called for.
  runpodctl's stated reason for not polling faster still applies and then some: "a 10 minute wait at
  1s is 600 api calls per created pod, and no readiness signal here changes that fast."

## Decision

### 1. "The port answers" means a TCP connection succeeds against the pod's public mapping

The tool reads `runtime.ports` from the pod, finds the entry whose `private` equals `--port`,
requires both `public` and `ip` to be non-null, and opens a TCP connection to that address with a
5-second timeout. Connecting and immediately closing is the whole test.

Not the proxy address. Runpod does run an HTTP proxy for pod ports, but it appears nowhere in the v2
document, so there is no public receipt to cite for its address format — and CLAUDE.md requires
every claim to link to a source. Direct TCP is also what CLAUDE.md itself specifies: "a TCP port
that answers." Runpod's own `--wait` carries the same restriction and states it in the flag's help
text: "needs a publicly mapped port 22, so community cloud also needs `--public-ip`."

**The honest weakness:** a connection proving the port *answers* is weaker than proving the workload
is *ready*. Runpod's forwarder can complete a TCP handshake before the service behind it does. This
is why `--success-phrase` is the stronger of the two success signals, and why the README says so.

### 2. "Repeating" means the failure phrase appears twice

`FAILURE_REPEATS = 2`, a fixed constant in the code, not a flag. CLAUDE.md settles the three
settings a user controls and a fourth knob would reopen that decision.

Two is the smallest count that is a repeat at all, which is the property CLAUDE.md asks for: pods
restart automatically after their startup command exits, so a genuinely broken container crash-loops
and prints its fault again. One occurrence can be an error a program recovers from. runpodctl needed
the same judgement for a different question and reached the same number, with the same reasoning:
"One is not enough."

The count is per watch window. `--retry` resets it, because the point of a retry is to judge the
second attempt on its own evidence.

### 3. Precedence: failure, then death, then success, then the clock

Every poll evaluates these in this fixed order and stops at the first that matches.

| Order | Condition | Verdict |
| --- | --- | --- |
| 1 | The failure phrase has appeared twice | `failure` |
| 2 | Pod status is `EXITED`, `ERROR` or `TERMINATED` | `failure` |
| 3 | Every configured success signal is satisfied | `healthy` |
| 4 | The deadline has passed | `timeout` |
| 5 | none of the above | keep polling |

**Failure beats success (1 before 3)** because the failure phrase is the user stating outright what
broken looks like for their own workload, while a port answering is something the tool infers — and
runpodctl verified that a port can answer with nothing behind it. Between an explicit signal and an
inferred one, the explicit signal wins.

**Death beats success (2 before 3)** because a pod's port mapping outlives the pod. runpodctl:
"A stopped or terminated pod keeps reporting its old runtime ports for a while." A dead pod can
therefore still look port-healthy, so status is checked first.

**Success beats the clock (3 before 4)** because a pod that became healthy inside its window is
healthy. The clock only decides cases nothing else decided.

### 4. Several success signals must all be satisfied

With both `--port` and `--success-phrase` set, the pod is healthy only when the port answers **and**
the phrase has appeared. Each signal alone is weak in a different direction — the port can answer
before the workload is up, and a phrase can print before the port binds — so requiring both is the
stronger definition. Anyone who wants either-alone sets one signal, which the settings already allow.

The cost is on record: if one signal can never fire, a healthy pod gets stopped. That is the
trade CLAUDE.md already accepts, since a false stop by an opt-in tool is a settings change.

### 5. `--retry` is stop, then start

REST v2 does have a `restart` action, but its documented rule makes it useless here: `restart`
requires a `RUNNING` pod, and a pod that failed to start usually is not one. The document's own
summary of the rules is that `RUNNING` allows `stop`/`restart`/`terminate`, `EXITED` and `ERROR`
allow `start`/`terminate`, and `PROVISIONING` and `STARTING` allow `stop`/`terminate`.

So there is exactly one sequence that works from every status this tool can encounter: **stop the
pod, wait until `start` appears in the pod's own `actions` list, then start it.** That field is
published on every pod as "Valid state transitions for the current status", so the tool waits for
the pod to say it is startable rather than guessing when it might be.

After the restart the clock and the failure count reset and the pod gets one more full
`--max-minutes` window. Only a second failure triggers the final action. There is exactly one retry;
a watchdog that retried forever would be a way to spend money, not save it.

Under `--dry-run` nothing is restarted, because restarting is an action and a dry run takes none.
The run reports the retry it would have performed and ends at the first verdict.

### 6. Exit codes

| Code | Meaning |
| --- | --- |
| 0 | The pod became healthy |
| 2 | A settings problem — bad flag, bad config file (already in use, and argparse's own code) |
| 3 | Stopped because the time limit ran out |
| 4 | Stopped because the failure signal repeated |
| 5 | Tool error — no API key, Runpod unreachable, the key rejected |

3 and 4 are separate codes because the two say different things to whoever reads the script's exit
status: a timeout may mean the limit was too tight, while a failure signal means the pod told you
what was wrong.

`--dry-run` returns the same code the real run would have returned. A dry run whose exit code did
not match the real one would be useless for testing the script around it.

### 7. An undeclared port warns, and refuses only when it is the only signal

A pod publishes the ports it exposes as strings like `8888/http`, `22/tcp`. If `--port 8888` names a
port the pod never exposed, the tool says so on the first successful read and **quotes the pod's
actual list in the same message**, so the fix is visible where the complaint is:

```
warning: pod abc123 does not publish port 8888. It publishes: 22/tcp, 5000/http
```

If a log phrase is also configured, the run continues — that signal can still fire. If `--port` is
the only success signal, the run cannot end any way except stopping a pod the tool was never able to
observe, so the same text is printed as an error and the run exits 5 having changed nothing.

## Consequences

- **The `--port` signal is the risky one, and this is the thing to check first on the live proving
  run.** The v2 document says `runtime` is "Null when the pod is not RUNNING", so there is no port
  mapping to probe during `PROVISIONING` and `STARTING` — which is most of the window this tool
  watches. That much is expected and is reported as "not ready yet", never as an error. The concern
  is larger: runpodctl's `pod.go` says the REST read shape leaves `runtime` null *even on a running
  pod*, "verified against prod", which is why their pod wait uses GraphQL instead. If that is still
  true, a `--port`-only run stops every pod at its deadline no matter how healthy it is. The
  declared-port check above catches the most common cause early, and every status line states
  plainly whether a mapping has been published yet, but only a live run settles it.
- The loop's timing resolution is one poll. The tool can act up to one poll interval — plus one log
  read — after the deadline, and never before it. Bounding it tighter would mean threads.
- Transient API failures extend a watch rather than ending it, so a pod is never abandoned because
  Runpod returned one `500`. A missing pod has to be missing twice before the tool believes it,
  which is runpodctl's rule and its reason: one short read is an unknown state, not a verdict.
- Nothing here is tunable beyond the three settings CLAUDE.md fixed. The poll interval, the failure
  count and the probe timeout are constants with reasons written above. If a real run shows one of
  them is wrong, changing it is a new ADR, not a new flag.
