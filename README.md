# runpod-startup-watchdog

A command-line tool that watches one newly created [Runpod](https://www.runpod.io/) pod while it
starts up. You define what healthy means for that pod — a time limit, a success signal, a failure
signal — and if the pod does not become healthy inside the limit, the tool stops it, which ends the
per-hour compute charge. It is a timer plus text checks. No cleverness.

```
export RUNPOD_API_KEY=your-key-here
runpod-watchdog --pod-id abc123xyz --max-minutes 10 --port 8888
```

That watches pod `abc123xyz` for ten minutes. If TCP port 8888 answers before the ten minutes are
up, the tool exits 0 and leaves the pod alone. If it does not, the tool stops the pod and exits 3.
Add `--dry-run` to get the same verdict and the same exit code while changing nothing.

## Why this exists

A pod that fails to start still bills. Compute is
[billed by the second](https://docs.runpod.io/pods/pricing) from the moment the pod is running, and
on the live run behind this README a pod reported `RUNNING` in the response to its own create call —
before its image had finished downloading. A pod that is broken therefore looks the same from
outside as a pod that is merely slow, and the failure is easy to miss until the invoice arrives.

These are public reports of exactly that shape. Each one was read before being linked here:

| Report | What it describes |
| --- | --- |
| [petteriTeikari/vascadia#757](https://github.com/petteriTeikari/vascadia/issues/757) (Mar 2026) | Provisioning hangs in `STARTING` for 38+ minutes, "No error message — just perpetual STARTING", recurring across attempts. Records the cost of the hang directly: "controller running at $0.17/hr during entire hang" |
| [petteriTeikari/vascadia#754](https://github.com/petteriTeikari/vascadia/issues/754) (Mar 2026) | The same project, a separate report: jobs "stuck in STARTING status for 40+ minutes without producing any logs" |
| [alphaXiv/openresearch-feedback#5](https://github.com/alphaXiv/openresearch-feedback/issues/5) (Jul 2026) | "stuck in starting at ~25 min, no logs", then failure with no recorded reason |
| [skypilot-org/skypilot#4285](https://github.com/skypilot-org/skypilot/issues/4285) (Nov 2024) | A custom image on Runpod: the cluster provisions and the job is submitted, but the replica never leaves `STARTING`. Closed without a documented cause |
| [ai-dock/comfyui#16](https://github.com/ai-dock/comfyui/issues/16) (Dec 2023) | A deployment that "cannot initialize itself and does not also log anything" |

**The set is small, and it is skewed.** These are developer issue trackers, which is where reports
end up with enough detail to verify. Three of the five reach Runpod through
[SkyPilot](https://github.com/skypilot-org/skypilot), so the `STARTING` state they describe is
SkyPilot's view of the pod, not necessarily the literal field returned by Runpod's REST API. Runpod's
review-site and support-forum threads were not used, because those sites block automated fetching and
this file does not link pages that were not read. Treat the table as a demonstration that the failure
mode is real and recurring, not as a measurement of how often it happens.

The account-level guardrail is a spend limit, visible in the billing screenshots taken during this
project's live run: `Spend limit $80/hr`
([demo/billing-before.png](demo/billing-before.png)). That is a ceiling on catastrophe, not a
guard on one pod.

## Why Runpod cannot ship this as a platform default

Two reasons, and both are the reason this is opt-in rather than a feature request.

**1. Healthy has no universal definition.** A web server is healthy when its port answers. A training
job is healthy when it prints a first loss value, and its ports may never open at all. A batch job is
healthy when it exits zero, which for a web server would be a crash. There is no rule that is correct
across a million strangers' workloads, so the definition has to come from the person who wrote the
workload.

**2. `RUNNING` arrives before the pod is usable.** This was measured on the live run, on one pod, from
[demo/01-port-field-question.txt](demo/01-port-field-question.txt):

| Time (UTC) | What happened |
| --- | --- |
| 16:50:14 | The create call returns with `status: "RUNNING"` |
| 16:50:27 | nginx prints `start worker processes` — 13 seconds later, the software starts |
| 16:50:38 | A public address first appears in `runtime.ports` — 24 seconds later, it is reachable |

Across that window the pod's own log shows the image still being fetched: `Extracting`,
`Pull complete`, `Status: Downloaded newer image for nginx:alpine`. That was a 20 MB image; a
multi-gigabyte machine-learning image takes minutes, and the pod reports `RUNNING` for every one of
them. Neither pod in the run was ever observed in `PROVISIONING` or `STARTING` at all. Details and
the disagreement with the API document are in
[docs/adr/0005-live-findings.md](docs/adr/0005-live-findings.md).

So from outside, slow and broken are the same picture. A platform-wide timer would have to guess, and
a wrong guess kills a healthy job someone paid for. **A false kill by a platform default is a support
ticket; a false kill by an opt-in tool is a settings tweak.**

Runpod's own CLI reaches the same conclusion from the other direction.
[runpodctl](https://github.com/runpod/runpodctl) has a `--wait` flag that blocks until a pod is
genuinely usable — for pods it "returns when ssh answers, not when the pod is scheduled" — and it
defaults to 10 minutes. On timeout it deliberately does not clean up: "on timeout or ctrl-c the
resource is **not** deleted — you paid for it, and you need the id to debug or clean up." That is a
correct default for a general-purpose CLI. The gap it leaves is the pod that is still running, still
billing, and never going to work. This tool fills that gap by asking you to opt in and say what
healthy means first.

On the serverless side Runpod does act on crash-loops — its docs say that "When an endpoint
consistently produces unhealthy (crashing) workers, Runpod scales it down to stop billing and reduce
thrashing" ([serverless troubleshooting](https://docs.runpod.io/serverless/troubleshooting)).
Pods have no equivalent, because a pod has no health contract to violate. This tool is a way for you
to supply one.

## Prior art

Both of these solve the *idle* pod — the one you started, used, and forgot about.

- **[Runpod-Idle-Pod-Monitor](https://github.com/runpod/Runpod-Idle-Pod-Monitor)** — "Idle monitoring
  solution for pods with a UI". It watches "CPU/GPU/Memory thresholds" and no-change detection over a
  duration, and stops pods that fall below them. It began as a personal project,
  [justinwlin/Runpod-Idle-Pod-Monitor](https://github.com/justinwlin/Runpod-Idle-Pod-Monitor)
  (created 12 August 2025), and a copy now lives in Runpod's own GitHub organisation (created
  5 November 2025). GitHub does not record the org copy as a fork, so this is two repositories with
  the same name and purpose rather than a documented transfer — but a community tool ending up under
  the vendor's org is the precedent this project is aiming at.
- **[stlaurentjr/runpod-auto-stop](https://github.com/stlaurentjr/runpod-auto-stop)** (Dec 2023) —
  "Checks CPU and VRAM utilization and stops the pod after a set period of time to avoid wasting
  money."

Neither addresses a pod that never worked in the first place. An idle monitor watching a pod stuck
mid-image-pull sees low utilisation and cannot tell that from a pod between jobs. **Failed startup is
a different niche, and it is the one this tool takes.** Runpod's own suggested workaround for the
adjacent problem is a manual sleep timer in the Docker command field —
`bash -c "nohup sleep 2h; runpodctl stop pod $RUNPOD_POD_ID" &` — because, as their blog puts it,
"there's not really a way to stop a pod based on mere idleness"
([Runpod blog](https://www.runpod.io/blog/manage-runpod-account-funding)).

## The loop this is meant to close

An opt-in tool is not the end state. It is the way to find out what the end state should be.

1. People who have been burned opt in and set their own `--max-minutes`, their own port, their own
   crash phrase.
2. Those settings are the data nobody has today: what real users consider a reasonable startup
   budget for a real workload.
3. That is the evidence for what a safe platform default could be — and a vendor can only ship a
   default it can defend.

Prior art shows the path is real: a community tool for a billing problem was picked up under Runpod's
own organisation. This one hopes to follow it.

## Stopped is not free

Stopping a pod is not the same as deleting it, and the difference costs money.

[Runpod's pricing documentation](https://docs.runpod.io/pods/pricing) states plainly: "Storage
charges continue to accrue on stopped Pods." Its table shows a volume disk billed at **$0.20/GB/month
while stopped**, against $0.10/GB/month while running — stopping a pod does not halve that charge, it
doubles the rate. Container disk is not charged while stopped.

- **Stop** (the default) ends the per-hour compute charge and keeps the pod and its disk, so you can
  restart it and debug what went wrong. Storage keeps billing.
- **`--terminate`** deletes the pod outright. That ends storage charges too, and destroys everything
  on the disk with no undo.

The default is stop, because a watchdog that silently destroys evidence of a failure is worse than
the failure. Two things the live run found that bear on this:

- **A stopped pod's reported hourly cost does not drop to zero.** Runpod's API document describes the
  `cost` field as "0.0 when `EXITED` or `TERMINATED`". Live, the stopped crash-test pod read `EXITED`
  with `cost: 0.06` ([demo/02-proving-run-a-broken-pod.txt](demo/02-proving-run-a-broken-pod.txt)).
  So `cost` cannot be used to check whether a pod has stopped billing. This tool never reads it to
  decide anything.
- **A stopped pod's container log is gone.** After the watchdog stopped the pod, re-reading its log
  returned only `system` events; the container's own lines — including the failure phrase the verdict
  was based on — were no longer in the tail, and the last system line is `remove container` (same
  transcript). **The watchdog's printed output is therefore the only record of what it saw.** That is
  why it prints a line on every poll.

## What the live run found

Everything in this README was run against the real Runpod API on 12 August 2026, between 16:49 and
16:59 UTC, on two `cpu3c` pods with 2 vCPU — the cheapest flavor Runpod offers. Full write-up:
[docs/adr/0005-live-findings.md](docs/adr/0005-live-findings.md). These are observations, recorded
because they cost time to discover and might save someone else the same time.

**A real bug in this tool, which the mocked tests could not catch.** The log reader was discarding
every line it had collected, so `--success-phrase` and `--failure-phrase` would never have fired —
every phrase-based run would have ended in a timeout. The cause: `stream_pod_logs` detected a quiet
stream by catching `requests.exceptions.ReadTimeout`, but requests does not raise that for a timeout
partway through a streamed body — `iter_content` re-raises urllib3's `ReadTimeoutError` as
`ConnectionError`. The exception escaped and took the collected lines with it. Every log read ends by
going quiet, so this was not an edge case. Mocked tests all passed, because a mock stream ends
cleanly and a real one goes silent. Fixed by keeping whatever arrived regardless of how the stream
ended; `tests/test_api.py::test_read_pod_logs_keeps_the_lines_it_already_collected` is the regression
test and it fails against the old code. **This is the argument for the proving run existing at all.**

**A port exposed as `http` cannot be probed; expose it as `tcp`.** A `RUNNING` pod returns
`runtime.ports` populated, and the `tcp` entry carries a publicly routable address. The `http` entry
for the same pod carried ip `100.65.23.30`, inside `100.64.0.0/10` — the range reserved for
carrier-grade NAT, which is not reachable from your machine. Same pod, two entries, only one of them
connectable ([demo/01-port-field-question.txt](demo/01-port-field-question.txt) and
[demo/03-proving-run-b-healthy-pod.txt](demo/03-proving-run-b-healthy-pod.txt)).

Where the live API differed from the published API document, all reproducible by reading the linked
transcript:

| Observation | The document says | Live | Receipt |
| --- | --- | --- | --- |
| Pod status on create | The pod "starts in `PROVISIONING`, transitions through `STARTING`, and reaches `RUNNING` once its container is healthy" | Both pods were `RUNNING` in the create response itself, and neither was ever seen in `PROVISIONING` or `STARTING` | [demo/01](demo/01-port-field-question.txt) |
| `cost` on a stopped pod | "Current cost in USD per hour (0.0 when `EXITED` or `TERMINATED`)" | `EXITED` with `cost: 0.06` | [demo/02](demo/02-proving-run-a-broken-pod.txt) |
| CPU flavor ids | The example response for `GET /v2/catalog/cpus` shows `cpu3c-2-4` | The live catalog returns `cpu3c`, `cpu5c`. The `BaseCpuConfig` example (`cpu5c`) does match | [demo/00](demo/00-baseline-and-catalog.txt) |
| `runtime.ports` ordering | Not specified | Entries come back in different orders on different reads, so nothing may depend on position | [demo/01](demo/01-port-field-question.txt) |

Two things behaved exactly as documented and are worth recording as such: `runtime` is null early in
a pod's life, which the tool reports as "no public mapping published yet" rather than as a fault; and
a CPU pod's `memory` is derived by the API from the flavor's RAM multiplier, as `CreatePodRequest`
describes.

## Proven live

The terminal transcripts are committed unedited.

| Transcript | What it shows |
| --- | --- |
| [00-baseline-and-catalog.txt](demo/00-baseline-and-catalog.txt) | The account before anything was created, and how the cheapest instance was chosen |
| [01-port-field-question.txt](demo/01-port-field-question.txt) | Whether REST v2 publishes a pod's public port address — it does — plus the raw pod response |
| [02-proving-run-a-broken-pod.txt](demo/02-proving-run-a-broken-pod.txt) | A pod that crash-loops: stopped on the repeated failure phrase, exit code 4 |
| [03-proving-run-b-healthy-pod.txt](demo/03-proving-run-b-healthy-pod.txt) | A pod that is genuinely serving: healthy, exit code 0, pod untouched |
| [04-proving-run-c-dry-run.txt](demo/04-proving-run-c-dry-run.txt) | `--dry-run --terminate` against a healthy live pod: exit code 3, pod still there afterwards |
| [05-account-after.txt](demo/05-account-after.txt) | Both pods terminated, account empty, nothing left billing |

What the whole run cost, from the account balance page before and after:

| Before | After |
| --- | --- |
| [demo/billing-before.png](demo/billing-before.png) — $10.00 | [demo/billing-after.png](demo/billing-after.png) — $9.99 |

One cent, for two pods, three watchdog runs and every API call in the transcripts.

## Limitations

Honest list. Everything here is true of the current version.

- **It has to be running to protect anything.** This is a process on your machine, not a service. If
  you close the terminal, lose the network, or your laptop sleeps, nothing is guarding the pod. It
  reduces the cost of a failed startup you are present for; it does not make you safe while away.
- **One pod, one startup.** No fleets, no lists. Once the pod is healthy the tool exits and stops
  watching — it will not notice a crash five minutes later. It is not an idle monitor either; for
  that, see the [prior art](#prior-art) above.
- **It does not create the pod.** You pass a pod id that already exists, so there is a window between
  creating a pod and starting the watchdog in which nothing is watching. Creating pods is a separate
  command on purpose (see below), so the tool that decides to stop a pod cannot also make one.
- **Log phrases only work if the image actually prints them** to the container log, on stdout or
  stderr. A workload that logs to a file inside the container is invisible to this tool. Matching is
  plain, case-sensitive substring — `Uvicorn running` does not match `uvicorn running`.
- **An `http`-exposed port cannot be probed.** It gets a carrier-grade-NAT address that is not
  reachable from your machine. Expose the port as `N/tcp` if you intend to watch it with `--port`.
- **`--failure-phrase` on its own can never produce a healthy verdict.** It is a failure signal, so
  with no success signal set there is nothing that can end the run early in the pod's favour: the run
  ends at the deadline with a stop (exit 3), or sooner on the failure phrase (exit 4). Pair it with
  `--port` or `--success-phrase` if you want the pod to be able to pass.
- **All success signals must fire, not any.** Setting both `--port` and `--success-phrase` means the
  pod is healthy only when both are satisfied. If one can never fire, a healthy pod gets stopped.
  This is deliberate ([ADR-0004](docs/adr/0004-health-verdicts.md)); set one signal if you want
  one signal.
- **Fixed numbers you cannot configure.** The failure phrase must appear twice; polling is every 10
  seconds; the TCP probe times out after 5 seconds; `--retry` gives exactly one extra attempt. A pod
  that fails and recovers between two polls is never seen. Reasoning in
  [ADR-0004](docs/adr/0004-health-verdicts.md).
- **Stopping does not end all charges,** and the tool cannot verify that it did — see [Stopped is not
  free](#stopped-is-not-free). Runpod's `cost` field does not go to zero on a stopped pod, so the
  tool does not read it.
- **Runpod's REST API v2 is in beta.** This tool is built directly on it
  ([ADR-0001](docs/adr/0001-build-on-rest-v2.md)), and the findings above show its documentation and
  its behaviour already disagree in places. Things may move.

## Settings

Settings come from these flags, from a TOML config file, or from both. TOML is a plain-text settings
format; the file is read with Python's built-in `tomllib`. **A flag always overrides the file.**

| Flag | What it does |
| --- | --- |
| `--pod-id ID` | The Runpod pod to watch. Required. |
| `--max-minutes N` | How many minutes the pod gets to become healthy before the tool acts. Required. Fractions are allowed, so `--max-minutes 0.5` waits 30 seconds. |
| `--port N` | Success signal: the pod counts as healthy once this TCP port answers. |
| `--success-phrase TEXT` | Success signal: the pod counts as healthy once this text appears in its log. |
| `--failure-phrase TEXT` | Failure signal: the pod counts as broken if this text appears in its log twice. |
| `--retry` / `--no-retry` | Give the pod one more startup attempt before giving up: stop it, wait for it to become startable, start it again, and watch for another full `--max-minutes`. Off by default. |
| `--terminate` / `--no-terminate` | Delete the pod instead of stopping it. Off by default. |
| `--dry-run` / `--no-dry-run` | Report what would happen and change nothing. Off by default. |
| `--config PATH` | Read settings from this TOML file. |
| `--version` | Print the version and exit. |

At least one of `--port`, `--success-phrase`, or `--failure-phrase` must be set; without one, the
tool has no way to tell a healthy pod from a broken one. Each switch has a matching `--no-` form so a
value set in a config file can be turned back off for a single run.

### Exit codes

The exit codes are the tool's real output for anything scripting around it, and a dry run returns the
same code the real run would have.

| Code | Meaning |
| --- | --- |
| 0 | The pod became healthy |
| 2 | A settings problem — a bad flag or a bad config file |
| 3 | Stopped because the time limit ran out |
| 4 | Stopped because the failure signal repeated, or the pod died on its own |
| 5 | Tool error — no API key, Runpod unreachable, the key was rejected, or `--port` names a port the pod does not publish and is the only signal |

### What it prints

One line per poll, so you can watch it decide:

```
[0:00] RUNNING       |  port 8888: no public mapping published yet  |  failure phrase: 0 of 2
[0:10] RUNNING       |  port 8888: mapped to 45.23.12.1:43210 but not answering: connection refused  |  failure phrase: 0 of 2
[0:20] RUNNING       |  port 8888: answered at 45.23.12.1:43210  |  failure phrase: 0 of 2
[0:20] every success signal fired
Result: healthy — every success signal fired. Nothing was stopped.
```

When it decides the other way, it says which signal decided it and what it did:

```
[0:10] RUNNING       |  failure phrase: 2 of 2
[0:10] the failure phrase 'CUDA out of memory' appeared 2 times
stopped pod abc123xyz
Result: stopped, because the failure phrase 'CUDA out of memory' appeared 2 times.
```

The status column is information, never a verdict — `RUNNING` is the status a pod reports while its
image is still downloading. If you ask for a port the pod never exposed, the tool says so and lists
the ports the pod actually exposes.

### How long to allow

Give `--max-minutes` enough room for the image to download, and then some. A cold pull of a 20 MB
image took about 13 seconds on the live run; a multi-gigabyte machine-learning image will take
minutes, and the pod will report `RUNNING` for every one of them.

### Config file

The keys are the flag names with underscores instead of dashes. Every key is optional, but a key that
is not a setting — or a value of the wrong type — stops the run rather than being ignored, because a
typo such as `max_minute` would otherwise leave the run with no time limit at all.

```toml
pod_id = "abc123xyz"
max_minutes = 10
port = 8888
success_phrase = "Uvicorn running"
failure_phrase = "CUDA out of memory"
retry = false
terminate = false
dry_run = false
```

```
runpod-watchdog --config watchdog.toml
runpod-watchdog --config watchdog.toml --pod-id someotherpod --max-minutes 15
```

The API key is not a setting. It is read only from the `RUNPOD_API_KEY` environment variable, never
from a config file, never written to disk, and never printed.

## Making a pod to watch

The watchdog only watches. Creating, reading, listing and deleting pods is a second command,
`runpod-watchdog-pod`, kept separate so the tool that decides to stop a pod is not also the tool that
can make one ([ADR-0006](docs/adr/0006-pod-lifecycle-is-a-separate-command.md)).

```
runpod-watchdog-pod create --name my-pod --image nginx:alpine --cpu cpu3c --vcpu 2 --port 80/tcp --disk 5
runpod-watchdog-pod show POD_ID          # read one pod back; --json for the whole response
runpod-watchdog-pod list                 # every pod on the account
runpod-watchdog-pod terminate POD_ID     # delete one, irreversibly
```

`create` takes `--dry-run` too, which prints the exact request body it would send and sends nothing.
The `--json` output hides the values of the pod's environment variables and keeps their names,
because this command exists partly to produce transcripts that get published.

## Installing

Python 3.12 or newer, and [uv](https://docs.astral.sh/uv/).

```
git clone https://github.com/kbennett2000/runpod-startup-watchdog.git
cd runpod-startup-watchdog
uv sync
uv run runpod-watchdog --version
```

The only runtime dependency is [requests](https://requests.readthedocs.io/). The tests run against a
mocked API and need no network access and no API key:

```
uv run pytest
```

## Design decisions

Each one is a short numbered file explaining a single decision.

| ADR | Decision |
| --- | --- |
| [0001](docs/adr/0001-build-on-rest-v2.md) | Build on Runpod's REST API v2 with plain HTTP, not the `runpod-python` library |
| [0002](docs/adr/0002-settings-resolution.md) | How flags, the config file, and defaults are layered |
| [0003](docs/adr/0003-api-operations.md) | Which API operations the tool uses, and how it handles their failures |
| [0004](docs/adr/0004-health-verdicts.md) | How the watch loop decides healthy, broken, or out of time |
| [0005](docs/adr/0005-live-findings.md) | What the live proving run found |
| [0006](docs/adr/0006-pod-lifecycle-is-a-separate-command.md) | Why creating pods is a separate command |

## License

MIT — see [LICENSE](LICENSE). Built by Kris Bennett ([Twelve Rocks LLC](https://github.com/kbennett2000)).
