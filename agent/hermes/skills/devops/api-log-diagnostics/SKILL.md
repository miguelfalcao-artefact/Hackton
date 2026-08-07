---
name: api-log-diagnostics
description: Use when diagnosing API logs for technical or non-technical users. Run the bundled analyzer against repository-local JSON, JSONL, or text logs; correlate failures, latency, rate limits, business events, and dependency errors; then produce plain-language Markdown and HTML reports plus machine-readable JSON. Explain what happened, customer and business impact, confidence, recommended next actions, and technical evidence without requiring the reader to understand status codes or percentiles.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [sre, api, logs, diagnostics, monitoring]
    related_skills: [systematic-debugging]
---

# API Log Diagnostics

## Overview

Analyze repository-local API logs without Google Drive. The primary audience may not be technical. Lead with a short explanation of what customers experienced, why it matters, and what the team should do next. Put status codes, percentiles, correlation details, and machine metadata in later sections for engineers.

The bundled analyzer is available to the agent. Run it for supported request logs instead of manually reproducing its calculations. Treat its output as measured evidence, then add context from related manifests, domain events, raw errors, and repository code when available.

## When to Use

- Analyze `.json`, `.jsonl`, `.log`, or `.txt` API telemetry stored in a repository.
- Diagnose latency, HTTP errors, rate limiting, concurrency failures, or database connectivity.
- Correlate request logs, domain events, and chaos run manifests.
- Produce diagnostics under a repository-local output directory.

Do not use this skill to modify production infrastructure, rotate secrets, or patch application code automatically.

## Workflow

1. Resolve the repository root and candidate paths. Reject symlinks and paths outside the repository. Exclude `.git`, `.agents`, `.codex`, dependency folders, and the diagnostic output directory. Continue only when every candidate is within scope.
2. Detect each input by content, not extension. Try one complete JSON object, then newline-delimited JSON, then text-log parsing. Record every malformed nonblank entry. Complete when each source has a declared format or a safe parse failure.
3. Normalize timestamps to UTC. Accept ISO-8601 and Unix seconds/milliseconds. Validate HTTP status and non-negative latency values. Complete when invalid observations are excluded and counted in data-quality warnings.
4. Correlate sources by exact `request_id`, then run/event time windows, then event/API identity/error proximity. Describe aggregate-only matches as associations. Complete when each conclusion names its correlation method.
5. For a supported request stream, run `scripts/analyze_logs.py` first. Use its request totals, status classes, error rate, success rate, and nearest-rank p50/p95/p99 latency as the deterministic baseline. Use `null` for unavailable metrics. Complete when the command succeeds and counts reconcile, or its failure is reported honestly.
6. Classify evidence using [references/classification.md](references/classification.md). Permit primary, secondary, mixed, and unclassified results. Complete when each classification has evidence for, evidence against, and confidence.
7. Write `diagnostic.md`, `diagnostic.html`, and `metadata.json` below `diagnostics/<date>_<safe-id>/`. In human reports, order content as: plain-language outcome, customer/business impact, recommended next actions, simple metric explanations, then technical evidence. Escape log-derived HTML and redact credentials and customer identifiers. Complete when all three artifacts contain equivalent core facts and valid metadata.
8. Propose prioritized mitigation, durable remediation, and verification steps. Do not edit application code. Complete when every recommendation maps to an observed signal.

## Deterministic local analysis

For API request JSONL, prefer the bundled analyzer:

```bash
python skills/devops/api-log-diagnostics/scripts/analyze_logs.py \
  --input /absolute/path/to/api.jsonl \
  --output /absolute/path/to/diagnostics/local-run \
  --source-root /absolute/path/to/repository
```

The script validates individual records and atomically produces the three report formats. Read the generated `metadata.json`, inspect its data-quality warnings, and then enrich the human reports with correlated context. Do not make the user run or interpret the script when the agent has execution access.

For this repository's primary runtime, use `agent/monitor.py`. It reads five-minute request windows from `error_generator/logs/api.jsonl`, notes the availability of `domain-events.jsonl`, searches `mock_api/` for source evidence, and creates timestamped reports under `reports/ecommerce-mock-api/`:

```bash
python agent/monitor.py --once
./scripts/run_monitor.sh
```

Use `--once` for immediate validation. The shell runner currently checks every 30 seconds for testing while retaining a five-minute analysis window. Name received logs and report files with local `YYYY-MM-DD_HH-MM-SS` time; the default timezone is `America/Sao_Paulo`. Use `--timezone` to change it and stop continuous processing with Ctrl+C.

## Non-technical communication

- Start with **Overall result:** `Healthy`, `Needs attention`, or `Critical`. Explain the choice in one sentence.
- Directly beneath the overall result, cite the values that caused it: failed count/total, error rate, observed error statuses, and p95 latency.
- Translate classifications on first mention. Example: “Rate limiting means the safety gate rejected excess requests so the service would not become overloaded.”
- Explain error rate as “how many requests did not complete successfully out of every 100.”
- Explain p95 latency as “95 out of every 100 requests finished within this time; the slowest 5 took longer.”
- Describe likely customer experience: slow screens, rejected requests, failed payments, unavailable products, or no visible impact.
- State business impact cautiously: possible lost transactions, retries, support demand, or degraded trust. Do not invent money values or affected customer counts.
- Give no more than three prioritized actions in the executive section. Name the responsible role when evident, such as application developer, platform engineer, or API owner.
- Separate **Observed facts**, **Likely explanation**, and **Unknowns**. A non-technical reader must be able to tell what is proven.
- List every distinct observed error in a table with status, error name, affected endpoint, count, plain-language meaning, and likely customer experience.
- Put raw status counts, code-level suggestions, and JSON metadata after the plain-language sections.
- When `--source-root` is available, use the script's exact literal matches to cite repository-relative file, line, and a short code excerpt. Call these search matches, not proven causes. If none are found, say “No matching source line was found” and name the endpoint/error engineers should trace.
- Use a brief everyday analogy only when it genuinely clarifies the failure.

## Output rules

- Use repository-relative source paths where possible.
- Never reproduce secrets, full API keys, raw database hosts, or complete stack traces.
- Distinguish expected domain responses from defects.
- Distinguish `ETIMEDOUT` from `ECONNREFUSED`; preserve contradictory evidence as ambiguity.
- Do not recommend scaling without saturation or load evidence.
- Do not call payment decline, stock rejection, validation failure, or HTTP 404 a code defect without additional evidence.
- Keep transport/parser failures separate from HTTP response counts.
- Make HTML standalone with embedded CSS and no scripts or remote assets.
- Never make the reader decode `Mixed`, `p95`, `429`, or another specialist label without an adjacent plain-language explanation.
- In technical evidence, show the observed timestamp, request ID in masked form, endpoint, status, error code, latency, chaos label, and source-code search match for each error group.

## Common Pitfalls

1. **Trusting extensions.** A `.json` can contain JSONL and a `.jsonl` can contain one pretty-printed object. Parse by content.
2. **Claiming causation from aggregates.** Require exact IDs or temporal evidence; otherwise use “associated.”
3. **Leaking identifiers.** Mask identities before writing reports.
4. **Using zero for missing data.** Use `null` so absence is not mistaken for a measured zero.
5. **Retriggering on reports.** Exclude `diagnostics/` from scanners and watchers.
6. **Inventing source patches.** Label generic diffs illustrative unless the actual implementation was inspected.

## Verification Checklist

- [ ] Every source path is repository-local and not a symlink.
- [ ] Every nonblank record was parsed or counted as malformed.
- [ ] Metrics reconcile with validated request records.
- [ ] Correlation methods and confidence are explicit.
- [ ] Sensitive values are redacted.
- [ ] Markdown, HTML, and JSON outputs agree.
- [ ] JSON parses and HTML contains escaped source-derived text.
- [ ] Recommendations are evidence-linked and application code is unchanged.
