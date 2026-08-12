# 0002 — How settings are resolved: flags over a TOML file, with every problem reported at once

Status: Accepted

## Context

The tool takes eight settings: which pod to watch (`pod_id`), how long it gets (`max_minutes`),
three health signals (`port`, `success_phrase`, `failure_phrase`), and three switches (`retry`,
`terminate`, `dry_run`). They arrive from two places — command-line flags, and an optional TOML
config file (TOML is a plain-text settings format, read with Python's built-in `tomllib`). CLAUDE.md
settles the direction: flags override the file.

That sentence is easy to say and easy to get wrong in four specific ways, and every one of the four
fails in the same direction — the tool ends up running with settings the user did not choose. For a
tool whose whole job is to stop a pod that is billing by the hour, a setting silently changing
underfoot is the failure mode, not an inconvenience. This ADR records the four rules that prevent
it.

## Decision

### 1. Every flag defaults to `None`, so "not typed" is not the same as "false"

The obvious way to declare `--terminate` in argparse is `action="store_true"`, which makes the value
`False` whenever the flag is absent. Merging that against a config file would then overwrite
`terminate = true` in the file with `False` on every single run, because the merge cannot tell "the
user asked for false" from "the user said nothing".

So every option is declared `default=None`, including the three switches, and the merge treats
`None` as "this source said nothing":

```
flag value if it is not None,  otherwise the file value,  otherwise the built-in default
```

`tests/test_config.py::test_file_value_survives_when_the_flag_is_absent` runs once per setting and
is the regression guard for this. If a default is ever changed to a real value, that test fails for
that setting instead of the tool quietly ignoring the config file.

### 2. The switches get paired off-switches, so the override works in both directions

`None` defaults fix half the problem. The other half: with only `--terminate`, a config file holding
`terminate = true` could never be turned back off from the command line, and "flags override the
file" would be true one way and false the other.

The three switches therefore use argparse's built-in `BooleanOptionalAction`, which generates
`--retry/--no-retry`, `--terminate/--no-terminate`, and `--dry-run/--no-dry-run`. The cost is three
extra names in `--help`. The benefit is that `--no-terminate` exists, which matters most for the one
switch that deletes a pod and everything on its disk.

### 3. An unknown key in the config file is a hard error, and so is a wrong type

The file is a flat table — no section header — because one tool reads it and nesting buys nothing.
Any key that is not a setting name stops the run, and so does any value of the wrong type.

Ignoring unknown keys is the friendlier-looking choice and the wrong one here. A user who typed
`max_minute = 10` instead of `max_minutes = 10` would get a tool that reports no error, watches a
pod, and never times out — the exact failure this project exists to catch, reintroduced by a typo.
Failing loudly at the first read is cheap; discovering it on the billing statement is not.

One sharp edge is written into the code with a comment because it is invisible otherwise: in Python
`isinstance(True, int)` is `True`, so a plain integer check accepts `port = true` and quietly reads
it as port 1. Booleans are rejected before the number checks.

### 4. All problems are reported together, one plain sentence each

`validate` returns a list and the command line prints every line, rather than raising on the first
problem. Running a tool three times to discover three missing settings is a bad trade for slightly
simpler code.

The one exception is a malformed file: a missing file, invalid TOML, an unknown key, or a wrong type
stops the run before the merge happens. A file that cannot be read cannot be layered, and reporting
"pod-id is required" next to "that key is a typo" would point the user at the wrong problem.

### The validation rules

| Rule | Why |
| --- | --- |
| `pod_id` required, not blank | No safe default for which pod to stop. |
| `max_minutes` required, greater than zero | No safe default for how long to wait. Fractions allowed, so a demo can use `--max-minutes 0.5`. |
| At least one of `port` / `success_phrase` / `failure_phrase` | With none of them the tool cannot tell a healthy pod from a broken one. |
| `port` between 1 and 65535 | It is a TCP port number. |
| A phrase may not be blank | A blank phrase matches every log line, so the pod would look healthy on its first line of output and never be stopped. |

Phrases are stored exactly as typed — leading and trailing whitespace can be part of a log match, so
only `pod_id` is stripped.

## Consequences

- `config.resolve(flag_values, config_path)` is the single entry point. Later cycles receive one
  frozen `Settings` object and never re-read flags or the file.
- `--config` itself can only come from the command line. A config file cannot name another config
  file.
- There is no automatic config file discovery. Nothing is read unless `--config` names it, so a
  stray `watchdog.toml` in a working directory can never change what a run does.
- `RUNPOD_API_KEY` is not a setting and is not read here. It is read only by the cycle that makes
  HTTP calls, only from the environment, and it is never written to a file or printed.
- Exit code 2 means a settings problem. That is argparse's own code for a bad command line, so a bad
  flag and a bad config file report the same way.
