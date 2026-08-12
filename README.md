# runpod-startup-watchdog

A command-line tool that watches one newly created [Runpod](https://www.runpod.io/) pod while it
starts up. You define what healthy means for your pod: a maximum number of startup minutes, a
success signal (a TCP port that answers, a phrase in the pod log, or both), and a failure signal (a
crash message that repeats in the log). If the pod does not become healthy inside the time limit, or
if the failure signal shows up, the tool stops the pod so the per-hour billing meter stops too.
Stopping a pod does not end every charge — [Runpod's pricing
docs](https://docs.runpod.io/pods/pricing) say storage keeps accruing on a stopped pod — so there is
also a flag to delete the pod outright. It is a timer plus text checks. No cleverness.

The full write-up — the problem and the public sources behind it — is still being written.

## Running it

The API key is read only from the `RUNPOD_API_KEY` environment variable. It is never read from a
config file, never written to disk, and never printed.

```
export RUNPOD_API_KEY=your-key-here
runpod-watchdog --pod-id abc123xyz --max-minutes 10 --port 8888 --failure-phrase "CUDA out of memory"
```

The tool polls the pod every 10 seconds and prints one line per poll, so you can watch it decide:

```
[0:00] PROVISIONING  |  port 8888: no public mapping published yet  |  failure phrase: 0 of 2
[0:10] STARTING      |  port 8888: no public mapping published yet  |  failure phrase: 0 of 2
[0:20] RUNNING       |  port 8888: mapped to 45.23.12.1:43210 but not answering: connection refused  |  failure phrase: 0 of 2
[0:30] RUNNING       |  port 8888: answered at 45.23.12.1:43210  |  failure phrase: 0 of 2
[0:30] every success signal fired
Result: healthy — every success signal fired. Nothing was stopped.
```

When it decides the other way, it says which signal decided it and what it did:

```
[0:10] STARTING      |  failure phrase: 2 of 2
[0:10] the failure phrase 'CUDA out of memory' appeared 2 times
stopped pod abc123xyz
Result: stopped, because the failure phrase 'CUDA out of memory' appeared 2 times.
```

Add `--dry-run` to see what it would do without doing it. A dry run returns the same exit code the
real run would have returned, so you can test the script around it without spending money.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | The pod became healthy |
| 2 | A settings problem — a bad flag or a bad config file |
| 3 | Stopped because the time limit ran out |
| 4 | Stopped because the failure signal repeated |
| 5 | Tool error — no API key, Runpod unreachable, or the key was rejected |

### What counts as healthy

If you set more than one success signal, **all of them** have to fire. Each one alone is weak in its
own direction: a port can answer before the software behind it is ready, and a phrase can print
before the port is open. If you want either signal on its own to be enough, set just that one.

`--port` works, and it was the part of this tool least certain to. Runpod's REST API v2 does publish
a pod's public address once the pod is running, and the watchdog opens a real TCP connection to it —
[demo/01-port-field-question.txt](demo/01-port-field-question.txt) is the run that settled it. Two
things to know before relying on it:

- **Expose the port as `tcp`, not `http`.** A port exposed as `80/http` gets an address inside
  `100.64.0.0/10`, the range reserved for carrier-grade NAT, which is not reachable from your
  machine. Only a `tcp` port gets a publicly routable address. Same port, different answer.
- **There is no address at all for the first half-minute or so.** The tool reports that on every
  line as "no public mapping published yet" rather than treating it as a fault.

If you ask for a port the pod never exposed, the tool says so and lists the ports the pod actually
exposes. If that port was your only success signal, it stops before watching rather than running the
clock down on a pod it could never have observed.

### How long to allow

Give `--max-minutes` enough room for the image to download, and then some.

A pod reports its status as `RUNNING` from the moment it is created — before the image has been
fetched, and long before anything inside it is serving. On the live run, Runpod called a pod
`RUNNING` thirteen seconds before nginx started and twenty-four seconds before there was an address
to connect to, and it never passed through `PROVISIONING` or `STARTING` at all. That was a 20 MB
image. A multi-gigabyte machine-learning image will take minutes, and the pod will say `RUNNING` for
every one of them.

This is not a complaint about Runpod. It is the reason the tool exists: from outside, a pod that is
slow and a pod that is broken look exactly the same, which is why *you* have to say what healthy
means. It is also why the status column in the output is information and never a verdict.

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
| `--retry` / `--no-retry` | Give the pod one more startup attempt before giving up: stop it, start it again, and watch for another full `--max-minutes`. Off by default. |
| `--terminate` / `--no-terminate` | Delete the pod instead of stopping it. Off by default. |
| `--dry-run` / `--no-dry-run` | Report what would happen and change nothing. Off by default. |
| `--config PATH` | Read settings from this TOML file. |
| `--version` | Print the version and exit. |

At least one of `--port`, `--success-phrase`, or `--failure-phrase` must be set; without one, the
tool has no way to tell a healthy pod from a broken one.

Stopping and terminating are not the same thing. Stopping a pod ends the per-hour charge but not
every charge — [Runpod's pricing docs](https://docs.runpod.io/pods/pricing) say storage keeps
accruing on a stopped pod, and the pod's disk is still there. `--terminate` deletes the pod outright,
which ends storage charges too and destroys anything on its disk. The default is stop.

Each switch has a matching `--no-` form so a value set in a config file can be turned back off for a
single run.

### Config file

The keys are the flag names with underscores instead of dashes. Every key is optional, but a key
that is not a setting — or a value of the wrong type — stops the run rather than being ignored,
because a typo such as `max_minute` would otherwise leave the run with no time limit at all.

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
from a config file.

## Making a pod to watch

The watchdog only watches. Creating, reading, listing and deleting pods is a second command,
`runpod-watchdog-pod`, kept separate so the tool that decides to stop a pod is not also the tool that
can make one.

```
runpod-watchdog-pod create --name my-pod --image nginx:alpine --cpu cpu3c --vcpu 2 --port 80/tcp --disk 5
runpod-watchdog-pod show POD_ID          # read one pod back; --json for the whole response
runpod-watchdog-pod list                 # every pod on the account
runpod-watchdog-pod terminate POD_ID     # delete one, irreversibly
```

`create` takes `--dry-run` too, which prints the exact request body it would send and sends nothing.

The `--json` output hides the values of the pod's environment variables and keeps their names,
because this command exists partly to produce transcripts that get published.

## Proven live

Everything above was run against the real Runpod API on 12 August 2026, on the cheapest CPU
instance Runpod offers. The terminal transcripts are committed unedited:

| Transcript | What it shows |
| --- | --- |
| [00-baseline-and-catalog.txt](demo/00-baseline-and-catalog.txt) | The account before anything was created, and how the instance was chosen |
| [01-port-field-question.txt](demo/01-port-field-question.txt) | Whether the public port address is published — it is — and the raw pod response |
| [02-proving-run-a-broken-pod.txt](demo/02-proving-run-a-broken-pod.txt) | A pod that crash-loops: stopped on the repeated failure phrase, exit code 4 |
| [03-proving-run-b-healthy-pod.txt](demo/03-proving-run-b-healthy-pod.txt) | A pod that is genuinely serving: healthy, exit code 0, untouched |
| [04-proving-run-c-dry-run.txt](demo/04-proving-run-c-dry-run.txt) | `--dry-run --terminate` against a healthy live pod: exit code 3, pod still there |
| [05-account-after.txt](demo/05-account-after.txt) | Both pods terminated, account empty, nothing left billing |

The run also found a real bug: the log reader was discarding every line it collected, so both log
signals would never have fired. Mocked tests could not catch it, because a mock stream ends cleanly
and a real one goes quiet. What was found, what was fixed, and where the API differs from its own
documentation is written up in
[docs/adr/0005-live-findings.md](docs/adr/0005-live-findings.md).
