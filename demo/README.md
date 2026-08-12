# demo — transcripts of the live proving run

Unedited terminal output from running this tool against the real Runpod API on **12 August 2026**,
between 16:49 and 16:59 UTC. Read them in order; each one says at the top what it is testing and
what it expected.

| File | What it shows |
| --- | --- |
| [00-baseline-and-catalog.txt](00-baseline-and-catalog.txt) | The account before anything was created, and how the cheapest instance was chosen |
| [01-port-field-question.txt](01-port-field-question.txt) | Whether REST v2 publishes a pod's public port address — it does — plus the raw pod response |
| [02-proving-run-a-broken-pod.txt](02-proving-run-a-broken-pod.txt) | A pod that crash-loops: stopped on the repeated failure phrase, exit code 4 |
| [03-proving-run-b-healthy-pod.txt](03-proving-run-b-healthy-pod.txt) | A pod that is genuinely serving: healthy, exit code 0, pod untouched |
| [04-proving-run-c-dry-run.txt](04-proving-run-c-dry-run.txt) | `--dry-run --terminate` against a healthy live pod: exit code 3, pod still there afterwards |
| [05-account-after.txt](05-account-after.txt) | Both pods terminated, account empty, nothing left billing |

Two pods were created in total, both on `cpu3c` with 2 vCPU — the cheapest flavor Runpod offers, at
0.03 US dollars per vCPU per hour of Runpod account spend. Both were terminated rather than stopped,
because [Runpod's pricing docs](https://docs.runpod.io/pods/pricing) say storage keeps accruing on a
stopped pod. Total pod time was under fifteen minutes.

Pod ids and public addresses appear throughout. They are gone — both pods were terminated, which
`05-account-after.txt` shows two ways. The API key appears nowhere: it is read only from the
`RUNPOD_API_KEY` environment variable, is never passed as an argument, and is redacted out of any
error message that might echo it.

What these runs found, including a real bug in the log reader and three places where the API
disagrees with its own documentation, is written up in
[docs/adr/0005-live-findings.md](../docs/adr/0005-live-findings.md).

Notes written *before* reading a command's output are corrected in place where the output
contradicted them, rather than quietly fixed — see the two corrections in
[02-proving-run-a-broken-pod.txt](02-proving-run-a-broken-pod.txt).
