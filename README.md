# runpod-startup-watchdog

A command-line tool that watches one newly created [Runpod](https://www.runpod.io/) pod while it
starts up. You define what healthy means for your pod: a maximum number of startup minutes, a
success signal (a TCP port that answers, a phrase in the pod log, or both), and a failure signal (a
crash message that repeats in the log). If the pod does not become healthy inside the time limit, or
if the failure signal shows up, the tool stops the pod so the per-hour billing meter stops too.
Stopping a pod does not end every charge — [Runpod's pricing
docs](https://docs.runpod.io/pods/pricing) say storage keeps accruing on a stopped pod — so there is
also a flag to delete the pod outright. It is a timer plus text checks. No cleverness.

The full write-up — the problem, the public sources behind it, and how to run the tool — lands with
the first working version.

## Settings

Settings come from these flags, from a TOML config file, or from both. TOML is a plain-text settings
format; the file is read with Python's built-in `tomllib`. **A flag always overrides the file.**

| Flag | What it does |
| --- | --- |
| `--pod-id ID` | The Runpod pod to watch. Required. |
| `--max-minutes N` | How many minutes the pod gets to become healthy before the tool acts. Required. Fractions are allowed, so `--max-minutes 0.5` waits 30 seconds. |
| `--port N` | Success signal: the pod counts as healthy once this TCP port answers. |
| `--success-phrase TEXT` | Success signal: the pod counts as healthy once this text appears in its log. |
| `--failure-phrase TEXT` | Failure signal: the pod counts as broken if this text keeps repeating in its log. |
| `--retry` / `--no-retry` | Give the pod one more startup attempt before giving up. Off by default. |
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
