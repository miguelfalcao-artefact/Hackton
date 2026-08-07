#!/usr/bin/env python3
"""Watch the cumulative log CSV and populate one HTML dashboard."""

from __future__ import annotations

import argparse
import csv
import fcntl
import html
import json
import math
import os
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from config import CSV_PATH, HTML_PATH, SOURCE_ROOT


ERROR_MEANINGS = {
    "rate_limit_exceeded": "Traffic exceeded the configured API limit.",
    "payment_declined": "A payment provider or payment rule rejected the charge.",
    "insufficient_stock": "The requested quantity was not available.",
    "internal_error": "The server failed while processing the request.",
    "validation_error": "Submitted data did not satisfy the API rules.",
    "not_found": "The requested resource was not found.",
    "invalid_status_transition": "The operation was not valid for the resource's current state.",
}


def percentile(values: list[float], fraction: float) -> float | None:
    """Return the nearest-rank percentile, or None for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 2)


def atomic_write(path: Path, content: str) -> None:
    """Replace the dashboard only after its complete contents are on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def error_origin(status: int, error: str) -> tuple[str, str]:
    """Classify observable origin without claiming that a person caused an error."""
    if status >= 500 or error == "internal_error":
        return "Server / application", "high"
    if status == 429 or error == "rate_limit_exceeded":
        return "Client automation / script traffic", "medium"
    if status in (400, 402, 404, 409, 422):
        return "User or business request", "medium"
    return "Unknown", "low"


def find_source_references(source_root: Path, needles: set[str]) -> dict[str, list[tuple[str, int, str]]]:
    """Find up to three source lines for each route or error-code needle."""
    references: dict[str, list[tuple[str, int, str]]] = {needle: [] for needle in needles if needle}
    extensions = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".rb", ".php", ".cs"}
    excluded = {".git", "node_modules", "__pycache__"}
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in extensions or any(part in excluded for part in path.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line_number, line in enumerate(lines, 1):
            for needle in references:
                if needle in line and len(references[needle]) < 3:
                    references[needle].append((str(path.relative_to(source_root)), line_number, line.strip()[:220]))
    return references


def render(csv_path: Path, source_root: Path) -> tuple[str, int]:
    """Aggregate collected records and render the complete dashboard document."""
    with csv_path.open(newline="", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_SH)
        all_rows = list(csv.DictReader(stream))
        fcntl.flock(stream, fcntl.LOCK_UN)
    requests = [row for row in all_rows if row.get("event") == "http_request"]
    valid: list[tuple[dict[str, str], int, float]] = []
    for row in requests:
        try:
            status, latency = int(row["status"]), float(row["latency_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        valid.append((row, status, latency))

    total = len(valid)
    successes = sum(200 <= status < 300 for _, status, _ in valid)
    failures = sum(status >= 400 for _, status, _ in valid)
    latencies = [latency for _, _, latency in valid]
    status_counts = Counter(status for _, status, _ in valid)
    error_groups = Counter(
        (status, row.get("error_code") or f"http_{status}", row.get("route") or row.get("path") or "unknown")
        for row, status, _ in valid if status >= 400
    )
    error_counts = Counter({name: sum(count for (status, error, route), count in error_groups.items() if error == name) for _, name, _ in error_groups})
    route_counts = Counter((row.get("route") or row.get("path") or "unknown") for row, _, _ in valid)
    updated = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def metric(label: str, value: object) -> str:
        shown = "—" if value is None else html.escape(str(value))
        return f'<article class="metric"><span>{html.escape(label)}</span><strong>{shown}</strong></article>'

    cards = "".join([
        metric("Requests", total),
        metric("Success rate", f"{100 * successes / total:.2f}%" if total else None),
        metric("Error rate", f"{100 * failures / total:.2f}%" if total else None),
        metric("p50 latency", f"{percentile(latencies, .50)} ms" if latencies else None),
        metric("p95 latency", f"{percentile(latencies, .95)} ms" if latencies else None),
        metric("p99 latency", f"{percentile(latencies, .99)} ms" if latencies else None),
    ])
    status_rows = "".join(f"<tr><td>{status}</td><td>{count}</td></tr>" for status, count in sorted(status_counts.items())) or '<tr><td colspan="2">No request data</td></tr>'
    error_rows = "".join(
        f"<tr><td><strong>{status}</strong></td><td><code>{html.escape(error)}</code></td><td><code>{html.escape(route)}</code></td><td>{count}</td><td>{html.escape(error_origin(status, error)[0])}<br><small>{error_origin(status, error)[1]} confidence</small></td><td>{html.escape(ERROR_MEANINGS.get(error, 'The request did not complete successfully.'))}</td></tr>"
        for (status, error, route), count in error_groups.most_common()
    ) or '<tr><td colspan="6">No errors observed</td></tr>'
    route_rows = "".join(f"<tr><td><code>{html.escape(route)}</code></td><td>{count}</td></tr>" for route, count in route_counts.most_common()) or '<tr><td colspan="2">No routes observed</td></tr>'
    recent = list(reversed(valid[-100:]))
    needles = {row.get("error_code", "") for row, status, _ in recent if status >= 400}
    needles.update((row.get("route") or "") for row, status, _ in recent if status >= 400)
    source_refs = find_source_references(source_root, needles)
    recent_parts: list[str] = []
    for index, (row, status, latency) in enumerate(recent):
        detail_id = f"request-detail-{index}"
        result = row.get("error_code") or ("OK" if status < 400 else f"http_{status}")
        try:
            raw = json.dumps(json.loads(row.get("raw_json") or "{}"), indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            raw = row.get("raw_json") or "No raw JSON available"
        refs = source_refs.get(row.get("error_code", ""), []) + source_refs.get(row.get("route", ""), [])
        unique_refs = list(dict.fromkeys(refs))
        if status < 400:
            source_detail = "<p>No application error was reported for this request.</p>"
        elif unique_refs:
            items = "".join(f"<li><code>{html.escape(file)}:{line}</code><pre><code>{html.escape(excerpt)}</code></pre></li>" for file, line, excerpt in unique_refs)
            source_detail = f"<h4>Relevant source lines</h4><p>Candidate investigation locations; a literal match does not prove this line is wrong.</p><ul>{items}</ul>"
        else:
            source_detail = "<p>No matching application source line was found. Use the JSON evidence below for investigation.</p>"
        recent_parts.append(f'''<tr class="request-row" tabindex="0" role="button" aria-expanded="false" aria-controls="{detail_id}" onclick="toggleRequest(this,'{detail_id}')" onkeydown="if(event.key==='Enter'||event.key===' '){{event.preventDefault();toggleRequest(this,'{detail_id}')}}"><td>{html.escape(row.get('ts', ''))}</td><td>{html.escape(row.get('method', ''))}</td><td><code>{html.escape(row.get('route') or row.get('path') or '')}</code></td><td>{status}</td><td>{latency:g}</td><td>{html.escape(result)} <span class="open-hint">View ›</span></td></tr>
<tr id="{detail_id}" class="request-detail" hidden><td colspan="6"><div class="detail-grid"><section>{source_detail}</section><section><h4>Original log JSON</h4><pre><code>{html.escape(raw)}</code></pre></section></div></td></tr>''')
    recent_rows = "".join(recent_parts) or '<tr><td colspan="6">No request data</td></tr>'

    success_rate = 100 * successes / total if total else 0
    error_rate = 100 * failures / total if total else 0
    if not total:
        health, summary, impact = "Unknown", "There are no valid requests to evaluate.", "Business impact cannot yet be measured."
    elif error_rate >= 20:
        health, summary, impact = "Critical", f"{error_rate:.2f}% of requests failed ({failures} of {total}).", "Customers are likely experiencing failed actions and unreliable service."
    elif failures:
        health, summary, impact = "Needs attention", f"{error_rate:.2f}% of requests failed ({failures} of {total}).", "Some customers may be unable to complete specific actions."
    else:
        health, summary, impact = "Healthy", f"All {total} measured requests succeeded.", "No request failures are visible in the collected data."
    primary_error = error_counts.most_common(1)[0][0] if error_counts else None
    diagnosis = ERROR_MEANINGS.get(primary_error, "No dominant error is currently visible.")
    actions = {
        "rate_limit_exceeded": ["Review limits against expected customer traffic.", "Add retry guidance and exponential backoff for API clients."],
        "internal_error": ["Trace affected request IDs in application logs.", "Inspect server and dependency failures around the error timestamps."],
        "payment_declined": ["Confirm expected decline rules with the payments owner.", "Separate business declines from provider failures."],
        "insufficient_stock": ["Verify inventory freshness and reservation concurrency.", "Explain unavailable quantities clearly to customers."],
    }.get(primary_error, ["Review errors by route and request time.", "Correlate affected request IDs with application and dependency logs."])
    action_items = "".join(f"<li>{html.escape(action)}</li>" for action in actions)
    origin_counts = Counter()
    for (status, error, _), count in error_groups.items():
        origin_counts[error_origin(status, error)[0]] += count
    origin_summary = ", ".join(f"{name}: {count}" for name, count in origin_counts.most_common()) or "No errors to classify"
    timestamps = [row.get("ts", "") for row, _, _ in valid if row.get("ts")]
    measured_from = min(timestamps) if timestamps else "Not available"
    measured_to = max(timestamps) if timestamps else "Not available"
    business_error_rows = "".join(
        f"<tr><td>{html.escape(error.replace('_', ' ').title())}</td><td>{count}</td><td>{(100 * count / failures):.2f}% of failures</td><td>{(100 * count / total):.2f}% of all requests</td><td>{html.escape(ERROR_MEANINGS.get(error, 'The request did not complete successfully.'))}</td></tr>"
        for error, count in error_counts.most_common()
    ) if failures else '<tr><td colspan="5">No failures were observed.</td></tr>'
    business_origin_rows = "".join(
        f"<tr><td>{html.escape(name)}</td><td>{count}</td><td>{(100 * count / failures):.2f}%</td><td>{html.escape('Inferred from HTTP status and error code; this does not prove individual fault.')}</td></tr>"
        for name, count in origin_counts.most_common()
    ) if failures else '<tr><td colspan="4">No error origins to assess.</td></tr>'
    busiest_routes = route_counts.most_common(5)
    business_route_rows = "".join(
        f"<tr><td><code>{html.escape(route)}</code></td><td>{count}</td><td>{(100 * count / total):.2f}%</td></tr>"
        for route, count in busiest_routes
    ) or '<tr><td colspan="3">No route data available.</td></tr>'
    agent_payload = {
        "schema_version": "2.0",
        "generated_at": updated,
        "source": str(csv_path),
        "health": health,
        "metrics": {"total_log_records": len(all_rows), "total_requests": total, "successes": successes, "failures": failures, "error_rate_percent": round(error_rate, 2), "p50_latency_ms": percentile(latencies, .50), "p95_latency_ms": percentile(latencies, .95), "p99_latency_ms": percentile(latencies, .99)},
        "http_statuses": dict(sorted(status_counts.items())),
        "error_origin_method": "Origin is inferred from HTTP status and error code. 'User or business request' does not prove human error.",
        "error_origins": dict(origin_counts),
        "errors": [{"status": status, "error_code": error, "route": route, "count": count, "origin": error_origin(status, error)[0], "origin_confidence": error_origin(status, error)[1], "meaning": ERROR_MEANINGS.get(error, "The request did not complete successfully.")} for (status, error, route), count in error_groups.most_common()],
        "routes": [{"route": route, "count": count} for route, count in route_counts.most_common()],
        "recommended_actions": actions,
    }
    agent_json = html.escape(json.dumps(agent_payload, indent=2, ensure_ascii=False))

    document = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>API Log Dashboard</title>
<style>:root{{--paper:#f7f4ed;--ink:#101a3a;--navy:#101a3a;--blue:#3155d9;--cyan:#13c8d3;--pink:#ee3f91;--coral:#ff7058;--card:#fff;--muted:#667085}}*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 Arial,sans-serif}}body:before{{content:"";display:block;height:9px;background:linear-gradient(90deg,var(--blue),var(--cyan) 34%,var(--pink) 70%,var(--coral))}}header,main,footer{{max-width:1200px;margin:auto;padding:24px}}header{{padding-top:44px}}header:after{{content:"";display:block;width:90px;height:7px;margin-top:28px;background:linear-gradient(90deg,var(--pink),var(--coral))}}h1{{font-size:clamp(2rem,5vw,4.5rem);line-height:1.02;letter-spacing:-.045em;margin:.1em 0}}h2{{font-size:1.7rem;letter-spacing:-.02em}}.subtitle{{color:var(--muted)}}.eyebrow{{color:var(--blue);font-size:.75rem;font-weight:800;letter-spacing:.14em}}nav{{display:flex;gap:10px;flex-wrap:wrap;margin-top:20px}}nav a{{background:var(--navy);color:#fff;padding:10px 16px;border-radius:2px;text-decoration:none;font-weight:bold;transition:.2s}}nav a:nth-child(2){{background:var(--blue)}}nav a:nth-child(3){{background:var(--pink)}}nav a:hover{{transform:translateY(-2px)}}.front{{margin:28px 0 60px;scroll-margin-top:20px}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px}}.metric,.card{{background:var(--card);border-radius:2px;box-shadow:0 10px 30px #101a3a12}}.metric{{padding:20px;border-top:5px solid var(--blue)}}.metric:nth-child(2n){{border-color:var(--cyan)}}.metric:nth-child(3n){{border-color:var(--pink)}}.metric span{{display:block;color:var(--muted)}}.metric strong{{font-size:1.65rem}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:18px;margin-top:18px}}.card{{padding:22px;overflow:auto}}.health{{position:relative;border-left:8px solid {('#ee3f91' if health == 'Critical' else '#ff7058' if health == 'Needs attention' else '#13c8d3')}}}.health:after{{content:"";position:absolute;right:0;top:0;width:22%;height:6px;background:linear-gradient(90deg,var(--cyan),var(--pink))}}.health h2{{font-size:2.2rem;margin:.2em 0}}table{{border-collapse:collapse;width:100%}}th,td{{padding:10px;text-align:left;border-bottom:1px solid #e4e1da;vertical-align:top}}th{{color:var(--blue);font-size:.78rem;text-transform:uppercase;letter-spacing:.06em}}tbody tr:hover{{background:#f3f7ff}}.request-row{{cursor:pointer}}.request-row:hover,.request-row:focus{{background:#eaf7fa;outline:2px solid var(--cyan);outline-offset:-2px}}.open-hint{{float:right;color:var(--pink);font-weight:bold}}.request-detail td{{padding:0 12px 20px;background:#f1f5ff}}.detail-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px;padding:18px}}.detail-grid pre{{max-height:420px;overflow:auto}}code,pre{{font-family:ui-monospace,monospace}}code{{color:#283fa7}}pre{{white-space:pre-wrap;word-break:break-word;background:var(--navy);color:#eaf8fa;padding:18px}}pre code{{color:inherit}}details summary{{cursor:pointer;font-weight:bold;color:var(--pink)}}footer{{color:var(--muted);border-top:1px solid #ddd8cd}}</style></head>
<body data-version="{stat_signature(csv_path)}"><header><div class="subtitle">CUMULATIVE LOG MONITOR · LIVE</div><h1>API health report</h1><p class="subtitle">Populated from <code>{html.escape(csv_path.name)}</code> · Updated {updated} · {len(all_rows)} total log records</p><nav><a href="#business">Non-technical view</a><a href="#developer">Developer view</a><a href="#agent">Agent data</a></nav></header><main>
<section id="business" class="front"><div class="eyebrow">FRONT 1 · NON-TECHNICAL / BUSINESS</div><article class="card health"><h2>{health}</h2><p><strong>{html.escape(summary)}</strong></p><p>{html.escape(diagnosis)}</p><p><strong>Measured period:</strong> <code>{html.escape(measured_from)}</code> to <code>{html.escape(measured_to)}</code></p></article>
<section class="metrics">{metric('Total requests', total)}{metric('Successful', f'{successes} ({success_rate:.2f}%)' if total else None)}{metric('Failed', f'{failures} ({error_rate:.2f}%)' if total else None)}{metric('Typical response', f'{percentile(latencies, .50)} ms' if latencies else None)}</section>
<section class="grid"><article class="card"><h2>Where these values come from</h2><p><strong>Source:</strong> <code>{html.escape(str(csv_path))}</code></p><p>The collector combines the configured JSONL logs into this CSV and removes exact duplicates using each record's SHA-256 ID.</p><dl><dt><strong>Success percentage</strong></dt><dd>{successes} HTTP 2xx requests ÷ {total} valid HTTP requests × 100 = <strong>{success_rate:.2f}%</strong></dd><dt><strong>Error percentage</strong></dt><dd>{failures} HTTP 4xx/5xx requests ÷ {total} valid HTTP requests × 100 = <strong>{error_rate:.2f}%</strong></dd><dt><strong>Typical response</strong></dt><dd>The p50 (median) of {len(latencies)} measured request latencies: half were faster and half were slower.</dd></dl><p><small>Non-request events remain in the CSV but are not included in request percentages.</small></p></article><article class="card"><h2>Possible business impact</h2><p>{html.escape(impact)}</p><p>The percentages describe observed API requests, not a percentage of unique customers. One customer or automated client can make many requests.</p></article><article class="card"><h2>Recommended next actions</h2><ul>{action_items}</ul></article></section>
<section class="card" style="margin-top:18px"><h2>What caused the failed requests?</h2><p>Each percentage is shown both as a share of failures and as a share of all measured requests.</p><table><thead><tr><th>Error</th><th>Requests</th><th>Share of failures</th><th>Share of all traffic</th><th>Plain-language meaning</th></tr></thead><tbody>{business_error_rows}</tbody></table></section>
<section class="grid"><article class="card"><h2>Likely origin of failures</h2><p>{html.escape(origin_summary)}</p><table><thead><tr><th>Origin</th><th>Failures</th><th>Percentage</th><th>How to read it</th></tr></thead><tbody>{business_origin_rows}</tbody></table></article><article class="card"><h2>Most-used customer actions</h2><p>Route share is the route's request count divided by {total} total requests.</p><table><thead><tr><th>API action</th><th>Requests</th><th>Traffic share</th></tr></thead><tbody>{business_route_rows}</tbody></table></article></section>
<article class="card" style="margin-top:18px"><h2>Limits of this conclusion</h2><p>The report proves request counts, HTTP outcomes, and recorded latency in the collected logs. It does not prove unique customer impact, financial loss, or that a specific person caused an error. Those conclusions require customer, transaction, and application traces correlated by request ID and timestamp.</p></article></section>
<section id="developer" class="front"><div class="eyebrow">FRONT 2 · DEVELOPER / TECHNICAL</div><h2>Measured evidence</h2><section class="metrics">{cards}</section><section class="grid"><article class="card"><h2>HTTP status</h2><table><thead><tr><th>Status</th><th>Count</th></tr></thead><tbody>{status_rows}</tbody></table></article><article class="card"><h2>Routes</h2><table><thead><tr><th>Route</th><th>Count</th></tr></thead><tbody>{route_rows}</tbody></table></article></section>
<section class="card" style="margin-top:18px"><h2>Complete error breakdown</h2><p>Origin is inferred from the observed status and error code; it is not proof of individual human fault.</p><table><thead><tr><th>Status</th><th>Error code</th><th>Route</th><th>Count</th><th>Likely origin</th><th>Meaning</th></tr></thead><tbody>{error_rows}</tbody></table></section><section class="card" style="margin-top:18px"><h2>Latest requests</h2><table><thead><tr><th>Timestamp</th><th>Method</th><th>Route</th><th>Status</th><th>Latency ms</th><th>Result</th></tr></thead><tbody>{recent_rows}</tbody></table></section></section>
<section id="agent" class="front"><div class="eyebrow">FRONT 3 · MACHINE / OTHER AGENT</div><article class="card"><h2>Structured diagnostic payload</h2><p>Stable JSON containing the complete current aggregates for downstream agent consumption.</p><details><summary>View JSON</summary><pre><code>{agent_json}</code></pre></details></article></section></main><footer>Three-audience report · live background updates · no remote assets or tracking</footer>
<script>function toggleRequest(row,id){{const detail=document.getElementById(id);detail.hidden=!detail.hidden;row.setAttribute('aria-expanded',String(!detail.hidden));row.querySelector('.open-hint').textContent=detail.hidden?'View ›':'Close ×'}}setInterval(async()=>{{try{{const response=await fetch('/dashboard.html?now='+Date.now(),{{cache:'no-store'}});if(!response.ok)return;const next=new DOMParser().parseFromString(await response.text(),'text/html');if(next.body.dataset.version!==document.body.dataset.version){{document.body.dataset.version=next.body.dataset.version;document.querySelector('header').replaceWith(next.querySelector('header'));document.querySelector('main').replaceWith(next.querySelector('main'));document.querySelector('footer').replaceWith(next.querySelector('footer'));}}}}catch(error){{console.debug('Dashboard update waiting',error)}}}},2000);</script></body></html>'''
    return document, total


def stat_signature(path: Path) -> str:
    """Return the lightweight version marker embedded in the dashboard page."""
    stat = path.stat()
    return f"{stat.st_mtime_ns}-{stat.st_size}"


def start_server(html_path: Path, host: str, port: int) -> ThreadingHTTPServer:
    """Serve only the generated dashboard from a background thread."""
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            """Return the dashboard for supported paths and reject all others."""
            if urlsplit(self.path).path not in ("/", "/dashboard.html"):
                self.send_error(404)
                return
            try:
                content = html_path.read_bytes()
            except FileNotFoundError:
                self.send_error(503, "Dashboard is not ready")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: object) -> None:
            """Suppress access logs; agent state is emitted as structured JSON."""
            return

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> int:
    """Watch the CSV and regenerate the dashboard whenever it changes."""
    parser = argparse.ArgumentParser(description="Update one HTML dashboard when the log CSV changes")
    parser.add_argument("--csv", type=Path, default=CSV_PATH)
    parser.add_argument("--html", type=Path, default=HTML_PATH)
    parser.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--serve", action="store_true", help="serve the live dashboard over HTTP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval < 0.2:
        parser.error("interval must be at least 0.2 seconds")
    csv_path, html_path, source_root = args.csv.resolve(), args.html.resolve(), args.source_root.resolve(strict=True)
    if not source_root.is_dir():
        parser.error("source root must be a directory")
    server = start_server(html_path, args.host, args.port) if args.serve else None
    if server:
        print(json.dumps({"status": "SERVING", "url": f"http://{args.host}:{args.port}/"}), flush=True)
    previous_signature: tuple[int, int] | None = None
    while True:
        try:
            stat = csv_path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature != previous_signature:
                document, requests = render(csv_path, source_root)
                atomic_write(html_path, document)
                previous_signature = signature
                print(json.dumps({"status": "UPDATED", "requests": requests, "html": str(html_path)}), flush=True)
        except FileNotFoundError:
            print(json.dumps({"status": "WAITING_FOR_CSV", "csv": str(csv_path)}), flush=True)
        except (OSError, csv.Error) as exc:
            print(json.dumps({"status": "ERROR", "error": type(exc).__name__, "message": str(exc)}), flush=True)
            if args.once:
                return 1
        if args.once:
            return 0 if previous_signature else 1
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            if server:
                server.shutdown()
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
