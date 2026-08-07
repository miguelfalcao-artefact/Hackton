# E-commerce API Diagnostic Agent

This project monitors an e-commerce mock API with two independent agents: a collector stores every new log record in one cumulative CSV, and a dashboard agent reads that CSV and updates one HTML report.

The collector checks the API log every **30 seconds**. The dashboard watches the CSV and rewrites the same HTML file only when the CSV changes.

## Project structure

```text
agent/
├── monitor.py       JSONL-to-CSV collector
├── dashboard.py     CSV-to-HTML dashboard agent
└── hermes/          Hermes Agent framework and api-log-diagnostics skill
mock_api/            Observed e-commerce API source
error_generator/
├── gen_api_logs.py  Error and traffic generator
└── logs/            API and domain-event JSONL inputs
scripts/
└── run_monitor.sh   Continuous 30-second testing entry point
reports/             One continuously updated HTML dashboard
runtime/             Cumulative logs.csv data store
docs/
└── ARCHITECTURE.md  Mermaid architecture documentation
```

## Requirements

- Python 3.11 or newer.
- The included API and error-generator logs.
- No external network connection is required for log analysis.
- Hermes model credentials are needed only when running the complete interactive Hermes framework; the deterministic monitor works without them.

## Inputs

The monitor reads:

- `error_generator/logs/api.jsonl` — generated request and application events, including rotated siblings such as `api.jsonl.1`.
- `mock_api/logs/api.jsonl` — API runtime request events, including rotated siblings.
- `error_generator/logs/domain-events.jsonl` — business events available for correlation.
- `mock_api/` — source code searched for matching routes and error definitions.

## Run one collection and dashboard update

Create a report immediately and exit:

```bash
python3 agent/monitor.py --once
python3 agent/dashboard.py --once
```

## Run continuously for testing

Check for new log content every 30 seconds:

```bash
./scripts/run_monitor.sh
```

Press `Ctrl+C` to stop.

The two agents:

1. Read every valid JSON object from the source log.
2. Use a stable SHA-256 record ID to append only unseen records to `runtime/logs.csv`.
3. Preserve common fields as columns and every original field in `raw_json`.
4. Read the CSV independently and calculate request, error, route, and latency metrics.
5. Atomically update `reports/ecommerce-mock-api/dashboard.html` instead of creating timestamped reports.
6. Serve the dashboard locally and update its content in the background without reloading the page.

## Reports

The pipeline maintains only these active artifacts:

```text
runtime/logs.csv
reports/ecommerce-mock-api/dashboard.html
```

The HTML contains three fronts in the same live page: a non-technical business summary, detailed developer diagnostics, and a structured JSON payload for another agent. Recent request rows are clickable and reveal the original log JSON plus relevant application `file:line` matches for failed requests. Source matches are investigation candidates, not automatic proof of faulty code. The page has no remote scripts, fonts, or tracking. While the runner is active, open `http://127.0.0.1:8000/`; it checks for changed dashboard content every two seconds and updates without reloading.

## Change the interval

The testing runner passes `--interval 30`. For example, to check every five minutes instead:

```bash
python3 agent/monitor.py --interval 300
```

The dashboard agent checks the CSV every two seconds by default, but does no work while it is unchanged.

## Main commands

```bash
# Collect current unseen records, then update the dashboard
python3 agent/monitor.py --once
python3 agent/dashboard.py --once

# Continuous 30-second test monitoring
./scripts/run_monitor.sh

# Run the error generator when a new synthetic stream is needed
python3 error_generator/gen_api_logs.py --out-dir error_generator/logs
```

The error generator runs until interrupted with `Ctrl+C`.

## Safety behavior

- Input logs and mock API source code are read-only.
- The monitor does not automatically modify application code.
- HTML values are escaped.
- Duplicate source records are not appended to the CSV.
- CSV and HTML writes are flushed or atomic so the agents do not consume partial output.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the data flow and component diagrams.
