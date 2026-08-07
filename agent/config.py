"""Environment-backed paths shared by the monitoring agents."""

from __future__ import annotations

import os
from pathlib import Path


# Relative environment paths are anchored here so commands work from any cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def path_setting(name: str, default: str) -> Path:
    """Read a path setting, resolving relative values from the project root."""
    value = Path(os.environ.get(name, default)).expanduser()
    return value if value.is_absolute() else PROJECT_ROOT / value


def log_paths() -> list[Path]:
    """Return one or more JSONL inputs from the platform path separator list."""
    configured = os.environ.get("API_LOG_PATHS", "logs/api.jsonl")
    return [
        path if path.is_absolute() else PROJECT_ROOT / path
        for item in configured.split(os.pathsep)
        if item.strip()
        for path in [Path(item.strip()).expanduser()]
    ]


# Defaults keep generated data inside ignored project directories.
CSV_PATH = path_setting("MONITOR_CSV_PATH", "runtime/logs.csv")
HTML_PATH = path_setting("DASHBOARD_HTML_PATH", "reports/api/dashboard.html")
SOURCE_ROOT = path_setting("API_SOURCE_ROOT", ".")
