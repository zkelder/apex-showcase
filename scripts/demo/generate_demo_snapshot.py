"""Crawl a locally running Apex Operator API into the static demo's retained snapshot.

The API MUST be backed by a throwaway database seeded only by Apex's own fixture
services. This script refuses to run against anything but a loopback URL, only issues
GET requests, and stores each response verbatim (status plus envelope) so the static
console renders exactly what the real API returned.

Usage:
    uv run python scripts/demo/generate_demo_snapshot.py --base http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
DEFAULT_OUT = ROOT / "frontend" / "public" / "demo-data"
LITERAL = re.compile(r"[\"'`](/api/v1/[A-Za-z0-9_\-./]+(?:\?[A-Za-z0-9_=&\-.%]*)?)[\"'`]")
TEMPLATE = re.compile(r"`(/api/v1/[^`]*\$\{[^`]*)`")
MAX_FILES = 600


def fetch(base: str, path: str) -> tuple[int, object]:
    request = urllib.request.Request(base + path, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read())
        except ValueError:
            return error.code, {"error_code": "HTTP_ERROR", "message": str(error)}


# Wall-clock-derived fields differ on every reseed. They are pinned to the fixture clock so
# the committed snapshot regenerates byte-identically; every other value is verbatim.
VOLATILE_TIMESTAMPS = {"created_at", "recorded_at", "timestamp", "as_of", "last_activity"}
VOLATILE_IDS = {"evaluation_run_id", "request_id"}
FIXTURE_CLOCK = "2026-07-25T12:00:00Z"


def _is_volatile_timestamp(key: str) -> bool:
    return key in VOLATILE_TIMESTAMPS or key.startswith(("created_at", "recorded_at"))


def normalize(value: object) -> object:
    if isinstance(value, list):
        items = [normalize(item) for item in value]
        # Rows whose only ordering signal was a wall-clock timestamp tie once pinned; give
        # them a deterministic order (canonical JSON) so regeneration is byte-stable.
        if items and all(isinstance(i, dict) and "timestamp" in i for i in items):
            items.sort(key=lambda i: json.dumps(i, sort_keys=True))
        return items
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            if _is_volatile_timestamp(key) and isinstance(item, str):
                out[key] = FIXTURE_CLOCK
            elif key in VOLATILE_IDS and isinstance(item, str):
                out[key] = "normalized-for-demo"
            else:
                out[key] = normalize(item)
        return out
    return value


def links(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        if value.startswith("/api/v1/") and " " not in value:
            found.append(value)
    elif isinstance(value, list):
        for item in value:
            found.extend(links(item))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(links(item))
    return found


def ids(body: object, key: str) -> list[str]:
    data = body.get("data") if isinstance(body, dict) else None
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    return sorted({str(item[key]) for item in items if isinstance(item, dict) and key in item})


def frontend_paths() -> tuple[set[str], set[str]]:
    literals: set[str] = set()
    templates: set[str] = set()
    for source in sorted(SRC.glob("*.ts*")):
        if ".test." in source.name:
            continue
        text = source.read_text()
        literals.update(match.group(1) for match in LITERAL.finditer(text))
        templates.update(match.group(1) for match in TEMPLATE.finditer(text))
    return literals, templates


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    arguments = parser.parse_args()
    host = urllib.parse.urlparse(arguments.base).hostname
    if host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("refusing to crawl a non-loopback API")
    base = arguments.base.rstrip("/")

    literals, templates = frontend_paths()
    bare_templates = {t.split("?")[0] for t in templates if "${" not in t.split("?")[0]}
    queue: deque[str] = deque(sorted({p for p in literals if not p.endswith("/")} | bare_templates))
    seen: set[str] = set()
    results: dict[str, tuple[int, object]] = {}
    campaign_ids: list[str] = []
    strategy_ids: list[str] = []
    expanded = False

    def expand_templates() -> None:
        for template in sorted(templates):
            head = template.split("?")[0]
            if "campaigns/${" in head:
                for identifier in campaign_ids:
                    queue.append(
                        re.sub(r"\$\{[^}]*\}", urllib.parse.quote(identifier), template, count=1)
                    )
            elif "strategies/${" in head:
                for identifier in strategy_ids:
                    queue.append(
                        re.sub(r"\$\{[^}]*\}", urllib.parse.quote(identifier), template, count=1)
                    )

    while len(results) < MAX_FILES:
        if not queue:
            if expanded or not (campaign_ids or strategy_ids):
                break
            expanded = True
            expand_templates()
            continue
        path = queue.popleft()
        if path in seen or "${" in path:
            continue
        seen.add(path)
        status, body = fetch(base, path)
        if status == 405:  # POST-only route: nothing to retain for a GET-only demo
            continue
        results[path] = (status, body)
        if status < 400:
            for link in links(body):
                if link not in seen:
                    queue.append(link)
        if path == "/api/v1/campaigns":
            campaign_ids = ids(body, "campaign_id")
        if path == "/api/v1/strategies":
            strategy_ids = ids(body, "strategy_id")

    arguments.out.mkdir(parents=True, exist_ok=True)
    for old in arguments.out.glob("*.json"):
        if old.name != "fixture-outcomes.json":
            old.unlink()
    files: dict[str, str] = {}
    for path, (status, body) in sorted(results.items()):
        name = hashlib.sha256(path.encode()).hexdigest()[:16] + ".json"
        (arguments.out / name).write_text(
            json.dumps(
                {"path": path, "status": status, "body": normalize(body)}, indent=1, sort_keys=True
            )
            + "\n"
        )
        files[path] = name
        bare = path.split("?")[0]
        files.setdefault(bare, name)
    (arguments.out / "manifest.json").write_text(
        json.dumps({"files": files}, indent=1, sort_keys=True) + "\n"
    )
    ok = sum(1 for status, _ in results.values() if status < 400)
    print(
        f"captured {len(results)} responses ({ok} OK, {len(results) - ok} error) -> {arguments.out}"
    )


if __name__ == "__main__":
    main()
