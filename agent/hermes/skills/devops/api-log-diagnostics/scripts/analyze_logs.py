#!/usr/bin/env python3
"""Create deterministic diagnostic artifacts from API request JSONL."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ERROR_MEANINGS = {
    "internal_error": "The server could not complete the request because of an internal failure.",
    "rate_limit_exceeded": "The API safety limit rejected the request because too many requests arrived.",
    "not_found": "The requested page or item could not be found.",
    "payment_declined": "The payment request was rejected.",
    "insufficient_stock": "The order requested more stock than was available.",
    "validation_error": "The submitted information did not satisfy the API rules.",
}

STATUS_MEANINGS = {
    400: "The request was not valid.", 401: "Authentication was required or failed.",
    403: "The caller was not allowed to perform this action.", 404: "The requested page or item could not be found.",
    409: "The request conflicted with the current state.", 422: "The submitted information could not be processed.",
    429: "The API rejected excess traffic to protect the service.", 500: "The server encountered an internal failure.",
    502: "An upstream service returned an invalid response.", 503: "The service was temporarily unavailable.",
    504: "An upstream service took too long to respond.",
}


def nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 2)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def parse_requests(path: Path) -> tuple[list[dict], list[str]]:
    records: list[dict] = []
    warnings: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            warnings.append(f"line {number}: malformed JSON")
            continue
        if not isinstance(item, dict):
            warnings.append(f"line {number}: record is not an object")
            continue
        status = item.get("status")
        latency = item.get("latency_ms")
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            warnings.append(f"line {number}: invalid HTTP status")
            continue
        if isinstance(latency, bool) or not isinstance(latency, (int, float)) or latency < 0:
            warnings.append(f"line {number}: invalid latency")
            continue
        records.append(item)
    return records, warnings


def mask_request_id(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value if len(value) <= 8 else f"{value[:6]}…{value[-2:]}"


def display_timestamp(value: object) -> object:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    seconds = value / 1000 if value > 10_000_000_000 else value
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return value


def find_code_references(needles: set[str], source_root: Path | None) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {needle: [] for needle in needles}
    if source_root is None:
        return found
    extensions = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".rb", ".php", ".cs"}
    excluded = {".git", ".agents", ".codex", "node_modules", "vendor", "diagnostics", "__pycache__"}
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in extensions or any(part in excluded for part in path.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line_number, line in enumerate(lines, 1):
            for needle in needles:
                if needle and needle in line and len(found[needle]) < 3:
                    found[needle].append({"file": str(path.relative_to(source_root)), "line": line_number, "excerpt": line.strip()[:180]})
    return found


def build_error_details(records: list[dict], code_refs: dict[str, list[dict]]) -> list[dict]:
    groups: dict[tuple[int, str, str], list[dict]] = {}
    for record in records:
        if record["status"] < 400:
            continue
        error = str(record.get("error_code") or f"http_{record['status']}")
        path = str(record.get("route") or record.get("path") or "unknown endpoint")
        groups.setdefault((record["status"], error, path), []).append(record)
    details = []
    for (status, error, path), items in sorted(groups.items()):
        sample = items[0]
        meaning = ERROR_MEANINGS.get(error, STATUS_MEANINGS.get(status, "The request did not complete successfully."))
        details.append({
            "status": status, "error_code": error, "endpoint": path, "count": len(items), "meaning": meaning,
            "sample": {"timestamp": display_timestamp(sample.get("ts")), "request_id": mask_request_id(sample.get("request_id")), "latency_ms": sample.get("latency_ms"), "chaos_event": sample.get("chaos_event")},
            "code_references": code_refs.get(error, []) + code_refs.get(path, []),
        })
    return details


def classification(statuses: Counter, errors: Counter, latencies: list[float]) -> tuple[str, list[str]]:
    matches: list[str] = []
    if statuses[429] or errors["rate_limit_exceeded"]:
        matches.append("Abuse / rate-limit policy")
    if any(statuses[s] for s in (402, 409, 422)) or any(errors[e] for e in ("payment_declined", "insufficient_stock", "validation_error")):
        matches.append("Business logic / concurrency")
    if sum(count for status, count in statuses.items() if status >= 500) or (latencies and nearest_rank(latencies, .99) >= 1000):
        matches.append("Capacity / resilience")
    if not matches:
        return "Unclassified", []
    return matches[0] if len(matches) == 1 else "Mixed", matches if len(matches) > 1 else []


def analyze(source: Path, output: Path, source_root: Path | None = None, filename_stamp: str | None = None) -> dict:
    records, warnings = parse_requests(source)
    statuses = Counter(record["status"] for record in records)
    errors = Counter(record.get("error_code") for record in records if record.get("error_code"))
    latencies = [float(record["latency_ms"]) for record in records]
    total = len(records)
    successes = sum(count for status, count in statuses.items() if 200 <= status <= 299)
    failures = sum(count for status, count in statuses.items() if 400 <= status <= 599)
    primary, secondary = classification(statuses, errors, latencies)
    needles = {str(record.get("error_code")) for record in records if record.get("error_code")}
    needles.update(str(record.get("route") or record.get("path")) for record in records if record.get("status", 0) >= 400 and (record.get("route") or record.get("path")))
    error_details = build_error_details(records, find_code_references(needles, source_root))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    metrics = {
        "total_requests": total,
        "success_rate_percent": round(100 * successes / total, 2) if total else None,
        "error_rate_percent": round(100 * failures / total, 2) if total else None,
        "p50_latency_ms": nearest_rank(latencies, .50),
        "p95_latency_ms": nearest_rank(latencies, .95),
        "p99_latency_ms": nearest_rank(latencies, .99),
    }
    metadata = {
        "schema_version": "1.0",
        "analysis_id": f"{source.stem}-{digest[:12]}",
        "generated_at": generated_at,
        "sources": [str(source)],
        "classification": {"primary": primary, "secondary": secondary, "confidence": "medium" if matches_evidence(primary, total) else "low"},
        "metrics": metrics,
        "critical_events": [error for error, _ in errors.most_common()],
        "error_details": error_details,
        "recommended_actions": recommendations(primary),
        "data_quality_warnings": warnings,
    }
    write_reports(output, metadata, statuses, errors, filename_stamp)
    return metadata


def matches_evidence(primary: str, total: int) -> bool:
    return total > 0 and primary != "Unclassified"


def recommendations(primary: str) -> list[str]:
    choices = {
        "Abuse / rate-limit policy": ["API owner: verify that request limits match intended customer usage", "Application developer: tell clients when to retry and add gradual retry delays"],
        "Business logic / concurrency": ["Product owner: confirm which business rejections are expected", "Application developer: test simultaneous updates and duplicate requests"],
        "Capacity / resilience": ["Platform engineer: inspect server and dependency pressure during the incident", "Performance engineer: reproduce the load before changing capacity"],
        "Mixed": ["API owner: separate results by failure type, endpoint, and client before choosing a fix", "Engineering team: address confirmed server failures before tuning expected client rejections"],
    }
    return choices.get(primary, ["Engineering team: collect request IDs and time-based telemetry before choosing a remediation"])


def plain_language(primary: str, metrics: dict) -> dict[str, str]:
    error_rate = metrics["error_rate_percent"]
    total = metrics["total_requests"]
    if total == 0:
        result = "Needs attention"
        summary = "No valid API requests were available, so service health could not be measured."
    elif error_rate is not None and error_rate >= 20:
        result = "Critical"
        summary = f"About {error_rate:.2f} out of every 100 requests failed, so customers were likely to notice unreliable service."
    elif error_rate is not None and error_rate > 0:
        result = "Needs attention"
        summary = f"About {error_rate:.2f} out of every 100 requests failed. Most requests worked, but some customers may have seen errors."
    else:
        result = "Healthy"
        summary = "All measured requests completed successfully."
    explanations = {
        "Abuse / rate-limit policy": "The service's safety gate rejected excess traffic to protect the API from overload.",
        "Business logic / concurrency": "Some requests conflicted with business rules or simultaneous updates.",
        "Capacity / resilience": "The service or one of its dependencies showed signs of failing under demand.",
        "Mixed": "More than one kind of problem appeared, so there is no single safe fix yet.",
        "Unclassified": "The available evidence is not enough to identify a specific type of problem.",
    }
    impacts = {
        "Healthy": "No customer-facing impact is evident in the analyzed requests.",
        "Needs attention": "Some users may have needed to retry or may not have completed their intended action.",
        "Critical": "Users likely experienced failed actions, retries, or loss of confidence in the service during this sample.",
    }
    return {"result": result, "summary": summary, "explanation": explanations[primary], "impact": impacts[result]}


def value_reference(metrics: dict, statuses: Counter) -> str:
    total = metrics["total_requests"]
    failed = sum(count for status, count in statuses.items() if status >= 400)
    error_statuses = ", ".join(f"HTTP {status} × {count}" for status, count in sorted(statuses.items()) if status >= 400) or "none"
    return f"{failed} of {total} requests failed ({metrics['error_rate_percent']}%); observed errors: {error_statuses}; p95 response time: {metrics['p95_latency_ms']} ms."


def markdown_error_table(details: list[dict]) -> str:
    if not details:
        return "No HTTP errors were observed."
    rows = ["| Status | Error | Endpoint | Count | What it means |", "|---:|---|---|---:|---|"]
    for item in details:
        safe = lambda value: str(value).replace("|", "\\|").replace("\n", " ")
        rows.append(f"| {item['status']} | `{safe(item['error_code'])}` | `{safe(item['endpoint'])}` | {item['count']} | {safe(item['meaning'])} |")
    return "\n".join(rows)


def markdown_error_evidence(details: list[dict]) -> str:
    if not details:
        return "No error evidence was available."
    sections = []
    for item in details:
        sample = item["sample"]
        references = item["code_references"]
        source_text = "\n".join(f"  - `{ref['file']}:{ref['line']}` — `{ref['excerpt'].replace('`', "'")}`" for ref in references) if references else "  - No matching source line was found. Trace the endpoint handler and search for the error code."
        sections.append(f"""### HTTP {item['status']} — `{item['error_code']}` on `{item['endpoint']}`

- **Occurrences:** {item['count']}
- **Meaning:** {item['meaning']}
- **Observed sample:** timestamp `{sample['timestamp']}`, request `{sample['request_id']}`, latency `{sample['latency_ms']} ms`, chaos event `{sample['chaos_event']}`
- **Source-code search matches:**
{source_text}

These lines are literal search matches, not proof that the matched code caused the error.""")
    return "\n\n".join(sections)


def write_reports(output: Path, metadata: dict, statuses: Counter, errors: Counter, filename_stamp: str | None = None) -> None:
    payload = json.dumps(metadata, indent=2, ensure_ascii=False) + "\n"
    metrics = metadata["metrics"]
    plain = plain_language(metadata["classification"]["primary"], metrics)
    p95_text = "not available" if metrics["p95_latency_ms"] is None else f"{metrics['p95_latency_ms']} milliseconds"
    warning_text = "No input-quality problems were found." if not metadata["data_quality_warnings"] else f"{len(metadata['data_quality_warnings'])} input-quality warning(s) were found."
    values = value_reference(metrics, statuses)
    error_table = markdown_error_table(metadata["error_details"])
    error_evidence = markdown_error_evidence(metadata["error_details"])
    md = f"""# API Diagnostic Report: {metadata['analysis_id']}

## Overall result: {plain['result']}

**Values behind this result:** {values}

## Executive summary

{plain['summary']}

**What appears to be happening:** {plain['explanation']} The complete list of observed errors is shown below.

{error_table}

**Possible customer and business impact:** {plain['impact']}

**What is proven:** The analyzer measured {metrics['total_requests']} valid requests from the supplied log.

**What is not yet proven:** This report identifies patterns, but the log alone may not prove the underlying root cause.

## Recommended next actions

""" + "\n".join(f"{i}. {item}" for i, item in enumerate(metadata["recommended_actions"], 1)) + f"""

## Metrics explained

- **Successful requests:** {metrics['success_rate_percent']}%. This is how many requests completed successfully out of every 100.
- **Failed requests:** {metrics['error_rate_percent']}%. This is how many requests returned a customer or server error out of every 100.
- **Typical upper response time (p95):** {p95_text}. This means 95 out of every 100 measured requests finished within this time; the slowest 5 took longer.
- **Input quality:** {warning_text}

## Technical evidence

**Technical classification:** {metadata['classification']['primary']}  
**Confidence:** {metadata['classification']['confidence']}

- Status counts: `{json.dumps(dict(sorted(statuses.items())))}`
- Error counts: `{json.dumps(dict(errors.most_common()))}`

{error_evidence}

## Agent metadata

```json
{payload.rstrip()}
```
"""
    labels = {"total_requests": "Requests analyzed", "success_rate_percent": "Successful requests (%)", "error_rate_percent": "Failed requests (%)", "p50_latency_ms": "Median response time (ms)", "p95_latency_ms": "95-in-100 response time (ms)", "p99_latency_ms": "99-in-100 response time (ms)"}
    rows = "".join(f"<tr><th>{html.escape(labels[key])}</th><td>{html.escape(str(value))}</td></tr>" for key, value in metrics.items())
    metric_cards = "".join(f'<article class="metric"><span>{html.escape(labels[key])}</span><strong>{html.escape(str(value))}</strong></article>' for key, value in metrics.items())
    actions = "".join(f"<li>{html.escape(item)}</li>" for item in metadata["recommended_actions"])
    error_rows = "".join(f"<tr><td><span class=\"status status-{item['status']}\">{item['status']}</span></td><td><code>{html.escape(item['error_code'])}</code></td><td><code>{html.escape(item['endpoint'])}</code></td><td>{item['count']}</td><td>{html.escape(item['meaning'])}</td></tr>" for item in metadata["error_details"])
    error_rows = error_rows or '<tr><td colspan="5">No HTTP errors were observed.</td></tr>'
    evidence_sections = []
    for item in metadata["error_details"]:
        sample = item["sample"]
        refs = "".join(f"<li><code>{html.escape(ref['file'])}:{ref['line']}</code><pre><code>{html.escape(ref['excerpt'])}</code></pre></li>" for ref in item["code_references"])
        refs = refs or "<li>No matching source line was found. Trace the endpoint handler and search for the error code.</li>"
        evidence_sections.append(f"<article class=\"evidence-card\"><h3><span class=\"status status-{item['status']}\">HTTP {item['status']}</span> <code>{html.escape(item['error_code'])}</code></h3><p class=\"endpoint\">Endpoint <code>{html.escape(item['endpoint'])}</code></p><p><strong>{item['count']} occurrence(s).</strong> {html.escape(item['meaning'])}</p><dl><div><dt>Timestamp</dt><dd><code>{html.escape(str(sample['timestamp']))}</code></dd></div><div><dt>Request</dt><dd><code>{html.escape(str(sample['request_id']))}</code></dd></div><div><dt>Latency</dt><dd>{html.escape(str(sample['latency_ms']))} ms</dd></div><div><dt>Chaos event</dt><dd><code>{html.escape(str(sample['chaos_event']))}</code></dd></div></dl><h4>Source-code search matches</h4><ul class=\"code-list\">{refs}</ul><p class=\"caveat\">Search matches help investigation, but do not prove root cause.</p></article>")
    result_class = plain["result"].lower().replace(" ", "-")
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>API Diagnostic Report</title><style>
:root{{--navy:#11183f;--navy-2:#25215f;--pink:#f5328c;--blue:#3d63ff;--cyan:#40d9ff;--ink:#171a36;--muted:#667085;--paper:#f6f7fb;--line:#e4e7f0;--white:#fff;--danger:#e62964;--warning:#f59e0b;--success:#12a878}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.6 Inter,Arial,sans-serif}}body:before{{content:"";display:block;height:8px;background:linear-gradient(90deg,var(--blue),var(--cyan) 35%,var(--pink) 78%,#ff725e)}}
.hero{{position:relative;overflow:hidden;background:var(--navy);color:var(--white);padding:44px max(24px,calc((100vw - 1120px)/2)) 92px}}.hero:after{{content:"";position:absolute;width:520px;height:520px;right:-180px;top:-290px;border-radius:50%;background:radial-gradient(circle at 35% 60%,var(--cyan),var(--blue) 38%,var(--pink) 72%,transparent 73%);opacity:.9}}.brand{{position:relative;z-index:1;font-size:1.05rem;font-weight:900;letter-spacing:.22em}}.tagline{{color:#bfc8ff;font-size:.75rem;letter-spacing:.1em;text-transform:uppercase}}.hero h1{{position:relative;z-index:1;max-width:760px;margin:46px 0 8px;font-size:clamp(2rem,5vw,4rem);line-height:1.03;letter-spacing:-.04em}}.analysis-id{{position:relative;z-index:1;color:#d7dcff;font-family:ui-monospace,monospace}}
main{{max-width:1120px;margin:-58px auto 70px;padding:0 24px;position:relative}}.card{{background:var(--white);border:1px solid var(--line);border-radius:22px;padding:clamp(22px,4vw,38px);margin-bottom:22px;box-shadow:0 18px 55px rgba(17,24,63,.08)}}h2{{font-size:clamp(1.35rem,3vw,2rem);line-height:1.2;margin:0 0 18px;letter-spacing:-.025em}}h3{{line-height:1.3}}.eyebrow{{color:var(--pink);font-weight:800;font-size:.75rem;letter-spacing:.13em;text-transform:uppercase}}.result{{border-top:6px solid var(--warning)}}.result.critical{{border-color:var(--danger)}}.result.healthy{{border-color:var(--success)}}.result h2{{font-size:clamp(2rem,5vw,3.6rem)}}.plain{{font-size:1.18rem;max-width:850px}}.value-strip{{background:#f1f3ff;border-radius:14px;padding:15px 18px;color:var(--navy);font-weight:650}}
.error-table-wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:14px}}table{{border-collapse:collapse;width:100%;background:var(--white)}}th,td{{padding:14px 16px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}thead th{{background:var(--navy);color:white;font-size:.78rem;text-transform:uppercase;letter-spacing:.06em}}tbody tr:last-child td,tbody tr:last-child th{{border-bottom:0}}code{{background:#eef0ff;color:#28256f;border-radius:5px;padding:.12rem .35rem}}.status{{display:inline-block;border-radius:999px;padding:.18rem .6rem;background:#ffedf4;color:#a30e48;font-size:.78rem;font-weight:850;white-space:nowrap}}.status-404{{background:#fff5d9;color:#7a5200}}.status-429{{background:#f2eaff;color:#6535a3}}.status-500{{background:#ffebf2;color:#ac174f}}
.impact-grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}.impact-grid article{{background:#f8f8ff;border-radius:14px;padding:20px}}.impact-grid h3{{margin-top:0;color:var(--navy)}}.actions{{counter-reset:action;list-style:none;padding:0;display:grid;gap:12px}}.actions li{{counter-increment:action;background:#f7f4ff;border-left:4px solid var(--pink);padding:15px 18px;border-radius:0 12px 12px 0}}.actions li:before{{content:counter(action,decimal-leading-zero);display:inline-block;color:var(--pink);font-weight:900;margin-right:12px}}
.metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}.metric{{min-height:125px;padding:18px;border:1px solid var(--line);border-radius:15px;background:linear-gradient(145deg,#fff,#f7f5ff)}}.metric span{{display:block;color:var(--muted);font-size:.82rem;line-height:1.3}}.metric strong{{display:block;color:var(--navy);font-size:clamp(1.45rem,3vw,2.2rem);margin-top:12px}}.explanation{{color:var(--muted);font-size:.92rem}}
.technical{{background:#f0f1f8}}.classification{{display:inline-flex;gap:8px;align-items:center;background:var(--navy);color:white;border-radius:999px;padding:7px 13px;margin-bottom:18px}}.evidence-grid{{display:grid;grid-template-columns:1fr;gap:16px}}.evidence-card{{background:white;border-radius:16px;padding:22px;border:1px solid var(--line)}}.evidence-card h3{{margin-top:0}}.endpoint{{color:var(--muted)}}dl{{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}}dl div{{background:#f6f7fb;padding:10px;border-radius:9px}}dt{{color:var(--muted);font-size:.74rem;text-transform:uppercase;letter-spacing:.06em}}dd{{margin:2px 0 0;overflow-wrap:anywhere}}.code-list{{padding-left:20px}}.code-list pre{{margin:.45rem 0 1rem}}.caveat{{color:var(--muted);font-size:.85rem;font-style:italic}}details{{background:var(--navy);color:white;border-radius:16px;padding:20px}}summary{{cursor:pointer;font-weight:800}}pre{{overflow:auto;background:#080d29;color:#dfe6ff;padding:18px;border-radius:10px;font-size:.82rem}}details code,pre code{{background:transparent;color:inherit;padding:0}}footer{{max-width:1120px;margin:auto;padding:0 24px 40px;color:var(--muted);font-size:.82rem}}
@media(max-width:720px){{.hero{{padding-bottom:78px}}main{{margin-top:-42px}}.metrics{{grid-template-columns:1fr 1fr}}.impact-grid,dl{{grid-template-columns:1fr}}th,td{{padding:11px 12px}}}}@media(max-width:460px){{.metrics{{grid-template-columns:1fr}}}}
</style></head>
<body><header class="hero"><div class="brand">ARTEFACT</div><div class="tagline">AI is about people</div><h1>API diagnostic report</h1><div class="analysis-id">Analysis {html.escape(metadata['analysis_id'])}</div></header><main>
<section class="card result {result_class}"><div class="eyebrow">System health</div><h2>{html.escape(plain['result'])}</h2><p class="plain">{html.escape(plain['summary'])}</p><p class="value-strip"><strong>Values behind this result:</strong> {html.escape(values)}</p></section>
<section class="card"><div class="eyebrow">Diagnosis</div><h2>What appears to be happening</h2><p>{html.escape(plain['explanation'])} The complete list of observed errors is below.</p><div class="error-table-wrap"><table><thead><tr><th>Status</th><th>Error</th><th>Endpoint</th><th>Count</th><th>What it means</th></tr></thead><tbody>{error_rows}</tbody></table></div></section>
<section class="card"><div class="eyebrow">Impact &amp; certainty</div><h2>What this may mean for the business</h2><div class="impact-grid"><article><h3>Possible impact</h3><p>{html.escape(plain['impact'])}</p></article><article><h3>What we know</h3><p><strong>Proven:</strong> {metrics['total_requests']} valid requests were measured.</p><p><strong>Not yet proven:</strong> the underlying root cause.</p></article></div></section>
<section class="card"><div class="eyebrow">Action plan</div><h2>Recommended next actions</h2><ol class="actions">{actions}</ol></section>
<section class="card"><div class="eyebrow">Performance</div><h2>Metrics at a glance</h2><div class="metrics">{metric_cards}</div><p class="explanation">The p95 value means 95 out of every 100 measured requests finished within that time; the slowest 5 took longer.</p></section>
<section class="card technical"><div class="eyebrow">For engineering teams</div><h2>Technical evidence</h2><p class="classification">{html.escape(metadata['classification']['primary'])} · {html.escape(metadata['classification']['confidence'])} confidence</p><div class="evidence-grid">{''.join(evidence_sections)}</div></section>
<details><summary>Agent metadata — structured JSON</summary><pre><code>{html.escape(payload)}</code></pre></details></main><footer>Artefact-inspired presentation · Generated API diagnostic · No remote assets or tracking</footer></body></html>
"""
    suffix = f"_{filename_stamp}" if filename_stamp else ""
    atomic_write(output / f"metadata{suffix}.json", payload)
    atomic_write(output / f"diagnostic{suffix}.md", md)
    atomic_write(output / f"diagnostic{suffix}.html", doc)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-root", type=Path, help="optional repository root for literal code-reference searches")
    parser.add_argument("--filename-stamp", help="safe timestamp appended to output filenames, for example YYYY-MM-DD_HH-MM-SS")
    args = parser.parse_args()
    source = args.input.resolve(strict=True)
    if source.is_symlink() or not source.is_file():
        parser.error("input must be a regular non-symlink file")
    source_root = args.source_root.resolve(strict=True) if args.source_root else None
    if source_root is not None and not source_root.is_dir():
        parser.error("source root must be a directory")
    if args.filename_stamp and (len(args.filename_stamp) > 32 or any(character not in "0123456789-_" for character in args.filename_stamp)):
        parser.error("filename stamp may contain only digits, hyphens, and underscores")
    result = analyze(source, args.output.resolve(), source_root, args.filename_stamp)
    print(json.dumps({"status": "OK", "analysis_id": result["analysis_id"], "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
