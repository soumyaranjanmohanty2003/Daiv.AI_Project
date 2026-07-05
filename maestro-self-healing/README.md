# 🩹 Maestro Self-Healing

Production-grade self-healing for [Maestro](https://maestro.mobile.dev) YAML flows, packaged as a reusable **composite GitHub Action**.

When a flow fails in CI, the tool:

1. Parses the Maestro failure logs and debug artifacts.
2. Asks **Groq** (free API, default `llama-3.3-70b-versatile`) to diagnose and repair the flow YAML.
3. Validates the proposed fix against strict guardrails.
4. Re-runs the flow. Only if it **passes** does the action open a **pull request** with the fix — it never pushes to your branch directly.

## Quick start

**1.** Get a free API key at [console.groq.com](https://console.groq.com) and add it as a repo secret named `GROQ_API_KEY`.

**2.** Add the action to your workflow (see `examples/maestro-selfheal.yml` for a full example with an Android emulator):

```yaml
permissions:
  contents: write
  pull-requests: write

steps:
  - uses: actions/checkout@v4
  # ... start your emulator/simulator and install the app ...
  - uses: your-org/maestro-self-healing@v1
    with:
      flows: ".maestro/"
      groq-api-key: ${{ secrets.GROQ_API_KEY }}
      install-maestro: "true"
```

That's it. Passing flows run normally; failing flows get healed; a PR like this appears when a fix works:

> 🩹 **Auto-heal Maestro flows** — 1 auto-healed: `login.yaml` (element `Sign In` renamed to `Log In`; selector updated, confidence 85%) with a full diff and root-cause analysis in the PR body.

## Inputs

| Input | Default | Description |
|---|---|---|
| `flows` | *(required)* | Files, globs, or directories of flows |
| `groq-api-key` | *(required)* | Groq API key |
| `groq-model` | `llama-3.3-70b-versatile` | Any Groq chat model |
| `max-heal-attempts` | `3` | Fix attempts per failing flow |
| `min-confidence` | `0.5` | Reject fixes below this model confidence |
| `maestro-timeout` | `900` | Per-flow timeout (seconds) |
| `maestro-extra-args` | `""` | Extra `maestro test` args (e.g. `--device emulator-5554`) |
| `install-maestro` | `false` | Auto-install the Maestro CLI |
| `dry-run` | `false` | Diagnose + propose, but change nothing |
| `create-pr` | `true` | Open a PR when flows are healed |
| `pr-branch` / `base-branch` / `github-token` | sensible defaults | PR plumbing |

## Outputs

`any_healed`, `all_passed`, `healed_files`, `failing_files`, `pr-url`.

## Safety guardrails (why you can trust the PRs)

Every LLM-proposed fix must survive **all** of these before it is even applied:

- **YAML must parse** in Maestro's `header --- commands` format.
- **Command whitelist** — only real Maestro commands allowed (no hallucinated steps).
- **`appId` is immutable** — a fix can never retarget the test at another app.
- **Assertions can't be deleted** — the model can't "fix" a test by removing what it checks (`ALLOW_ASSERTION_REMOVAL=true` to override).
- **Confidence threshold** — low-confidence guesses are discarded.
- And then the fix must actually **pass a real re-run**. If it doesn't after `max-heal-attempts`, the original file is restored and the flow is reported as needing human attention.

Failed runs upload full Maestro debug artifacts, and every run writes a Markdown report to the job summary.

## Extra features

- **Fix-attempt memory**: each retry tells the model what was already tried and why it failed, so it doesn't repeat itself.
- **Rate-limit aware**: exponential backoff with `Retry-After` support for Groq's free-tier limits.
- **Budget cap**: `MAX_FLOWS_TO_HEAL` (default 10) stops runaway API usage when everything is broken (e.g. the app itself crashed).
- **Unfixable detection**: if the model determines the failure isn't in the YAML (crash, device offline, missing APK), it says so instead of thrashing.
- **Dry-run mode** for evaluating the tool before trusting it.
- **Local use**: `GROQ_API_KEY=... python -m selfheal .maestro/ --verbose` works on your machine too.

## Repo layout

```
action.yml            # composite GitHub Action
selfheal/             # Python package
  cli.py              # orchestrator (run -> heal -> re-run -> report)
  runner.py           # runs `maestro test`, captures logs + debug output
  failure_parser.py   # distills failure logs for the LLM
  groq_client.py      # resilient Groq API client (retries, JSON mode)
  fixer.py            # prompting + fix application
  validator.py        # YAML validation + safety guardrails
  report.py           # Markdown report / PR body
examples/             # ready-to-copy caller workflow
tests/                # unit tests (run: python -m pytest)
```

## Exit codes

`0` all green (originally or after healing) · `1` unhealed failures remain · `2` config error.
