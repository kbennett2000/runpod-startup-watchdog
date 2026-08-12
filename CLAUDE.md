# CLAUDE.md — runpod-startup-watchdog

## What this is
A command-line tool that watches one newly created Runpod pod during startup. If the pod does not become healthy — by the user's own definition — within a time limit, the tool stops the pod so billing ends. It is a timer plus text checks. No cleverness.

Public portfolio project for Kris Bennett (GitHub: kbennett2000, Twelve Rocks LLC, MIT license). Hard deadline: working demo by Thursday evening, August 13, 2026.

## The story the README must tell
- The most common complaint in public Runpod reviews: pods that fail to start or crash while the billing meter runs. The only guardrail reviewers mention is a default $80/hour account spend cap.
- Runpod cannot ship this as an automatic platform feature. "Healthy" has no universal definition across a million strangers' workloads, and the API reports a pod as RUNNING while its software image is still downloading, so broken and slow look identical from outside. A false kill by a platform default is a support ticket; a false kill by an opt-in tool is a settings tweak.
- Supporting receipt: Runpod's own blog says there is no built-in way to stop a pod based on idleness and suggests a manual sleep-timeout workaround (https://www.runpod.io/blog/manage-runpod-account-funding).
- Prior art, positioned respectfully: Runpod-Idle-Pod-Monitor (a community tool Runpod adopted into their own GitHub org) and stlaurentjr/runpod-auto-stop solve IDLE pods you forgot about. Nothing solves failed STARTUP. Different niche. The org-adoption precedent is part of the point: community tool → adopted by Runpod is the intended path for this one too.
- The loop: opt-in tool → adoption → the settings users choose become the evidence for what a safe product default would be.
- Every claim in the README links to a public source (Runpod docs, Runpod repos, public reviews). Never cite Kris's private research or any of its numbers.

## Settled decisions — do not reopen, do not redesign
1. Health is user-defined via three settings: a maximum number of startup minutes; a success signal (a TCP port that answers, and/or a phrase appearing in the pod log); a failure signal (a crash message that repeats in the log).
2. Opt-in, one pod at a time. A personal tool, not fleet management.
3. On failure the tool stops the pod. Stopping does not end all charges: Runpod's docs confirm storage charges continue to accrue on stopped pods (https://docs.runpod.io/pods/pricing). So the default action is stop, a --terminate flag deletes the pod entirely instead, and the README states the difference plainly.
4. Optional single retry before giving up.
5. Talk to Runpod's REST API v2 directly with plain HTTP calls from Python. Do NOT use the runpod-python library — its API layer rides the legacy GraphQL surface. This is ADR-0001. Building on the current surface is deliberate.
6. Settings come from command-line flags or a TOML config file (TOML is a plain-text settings format; read it with Python's built-in tomllib). Flags override the file.

## Verified technical facts — treat as landmines
- The REST v2 field is `status`, not `desiredStatus` — that is the legacy GraphQL name, still used by runpodctl. status RUNNING does not mean usable. The image may still be downloading.
- Runpod's own open-source Go CLI, runpodctl, has a --wait flag that blocks until the pod is actually usable, with a 10-minute default. Before writing the health-wait logic, read how runpodctl decides "usable" and mirror it: https://github.com/runpod/runpodctl
- Pods restart automatically after their startup command exits, so a broken container crash-loops. That is why the failure signal is a repeating log pattern, not a single line.
- Runpod has four live API surfaces: REST v2 (beta, current — build on this), REST v1 (deprecated), GraphQL (legacy), and serverless. The v2 spec is OpenAPI 3.1, 29 paths, 44 operations, fetchable without auth from Runpod's docs. Kris holds a pinned snapshot dated 2026-07-30 — ask him for it rather than guessing.
- Everything needed exists in v2: create pod, get pod, list pods, stream pod logs, stop, terminate.

## Build rules
- One cycle = one branch = one pull request. Kris reviews on the GitHub website. After the bootstrap commit, never commit directly to main.
- Small cycles. Each delivers the smallest reviewable, load-bearing unit.
- Every design decision gets an ADR (architecture decision record — a short numbered file explaining one decision) in docs/adr/, numbered 0001 upward.
- Tests: pytest. All tests run against a mocked API and must pass with no network access.
- The tool must have a --dry-run mode: report what it would stop, stop nothing.
- No live API calls of any kind until Kris explicitly starts a live run and supplies the key. During a live run, halt and ask Kris in two cases: a platform surprise (the API or the pod behaves in a way the plan did not anticipate), and anything that would spend money beyond the steps he listed. A defect in our own code is not a halt: fix it, verify the fix against the mocked tests, then continue the listed steps. Never invent new live experiments to investigate something — write the open question down instead.
- Secrets: the API key comes only from the RUNPOD_API_KEY environment variable. Never write it to a file, never print it, never commit it. .env stays in .gitignore.
- This repo is public from the first push. Everything committed is public immediately.
- Python 3.12+, uv for environment and dependencies. Runtime dependency: requests only. Dev dependencies: pytest, plus a requests-mocking helper if needed. Commit uv.lock.

## Writing style for everything in the repo
- Lab-notebook tone. Plain sentences. Claims link to receipts.
- No marketing voice, no hype words.

## Reporting to Kris
- Status updates come in exactly two parts: "What happened" (short plain sentences, one idea per sentence) and "What you do" (numbered steps with exact copy-pasteable text).
- Define every technical term in the same sentence it first appears. No metaphors, no coined nicknames. Every message stands alone.
- Always full copy-pasteable commands, never fragments.
- Any dollar figures Claude Code displays are usage estimates against Kris's Claude Max subscription, not money spent. Label them that way if they come up.
