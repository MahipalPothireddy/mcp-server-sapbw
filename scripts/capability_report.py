"""B2 capability-discovery runner (one-off, LIVE).

Connects to a named profile with the read-only connection pool (which runs the fail-closed grant
check), runs the :class:`CapabilityResolver`, and writes a capability report to the git-ignored
``output/`` directory for review. This is the B2 gate deliverable — it is not an MCP tool.

Usage (from the repo root, with the project venv):

    python scripts/capability_report.py qa

Reads connection secrets from environment variables, auto-loading a local ``.env`` if present.
The report contains only release, schema, table-presence, object-model, repo-style, and retention
information — no credentials and no customer object names.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from mcp_server_sapbw.core.capabilities import CapabilityResolver
from mcp_server_sapbw.core.connection import ReadOnlyConnectionPool
from mcp_server_sapbw.core.profiles import ProfileManager
from mcp_server_sapbw.models.capability import OBJECT_MODEL_KEYS, CapabilityRecord

_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no dependency): KEY=VALUE lines, does not override existing vars."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def render_markdown(record: CapabilityRecord) -> str:
    """Human-readable capability report (no secrets)."""
    present = sorted(name for name, ts in record.tables.items() if ts.present)
    absent = sorted(name for name, ts in record.tables.items() if not ts.present)

    lines: list[str] = [
        f"# Capability report — {record.system}",
        "",
        f"- Generated: {datetime.now(UTC).isoformat()}",
        f"- BW release: **{record.bw_release}**",
        f"- ABAP schema: **{record.abap_schema}**",
        f"- HANA repository style: **{record.hana_repo_style}**",
        f"- Runtime analysis window: **{record.processlog_retention_days} days** "
        "(capped at 1 year; bounded by RSPCLOGCHAIN.DATUM)",
        f"- Tables present: **{len(present)}** / {len(record.tables)}",
        "",
        "## Object-model variants",
        "",
    ]
    for key in OBJECT_MODEL_KEYS:
        lines.append(f"- {key}: {'yes' if record.has_object_model(key) else 'no'}")

    lines += ["", "## Discover-tier resolutions", ""]
    for name, status in sorted(record.tables.items()):
        if status.tier == "discover":
            resolved = status.resolved_name or "(not found)"
            lines.append(f"- {name}: {resolved}")

    lines += ["", f"## Absent on this release ({len(absent)})", ""]
    lines += [f"- {name}" for name in absent] or ["- (none)"]
    lines += ["", f"## Present ({len(present)})", ""]
    for name in present:
        status = record.tables[name]
        rows = f" ({status.row_estimate:,} rows)" if status.row_estimate is not None else ""
        lines.append(f"- {name}: {status.resolved_name}{rows}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: python scripts/capability_report.py <profile>\n")
        return 2
    profile_name = argv[0]

    _load_dotenv(_ROOT / ".env")

    manager = ProfileManager()  # BW_PROFILES_PATH (defaults handled by the env)
    profile = manager.get(profile_name)

    pool = ReadOnlyConnectionPool()
    try:
        connection = pool.acquire(profile)  # runs the fail-closed read-only grant check
        record = CapabilityResolver().resolve(profile, connection)
    finally:
        pool.close_all()

    output_dir = _ROOT / "output"
    output_dir.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = output_dir / f"capability-{record.system}-{stamp}.json"
    md_path = output_dir / f"capability-{record.system}-{stamp}.md"
    json_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    markdown = render_markdown(record)
    md_path.write_text(markdown, encoding="utf-8")

    sys.stdout.write(markdown)
    sys.stdout.write(f"\nWrote {json_path.name} and {md_path.name} to output/ (git-ignored).\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
