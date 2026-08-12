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
