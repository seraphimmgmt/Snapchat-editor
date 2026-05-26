#!/usr/bin/env python3
"""
refresh-ios-pool.py — keep dist/index.html's IPHONE_DEVICE_POOL fresh.

Fetches the latest iOS releases per supported iPhone model from AppleDB (a
community-maintained Apple device + OS database that updates daily), figures
out which model + iOS pairings are current, and rewrites the
IPHONE_DEVICE_POOL block in dist/index.html.

Goals:
  - Latest iPhone Pro/Pro Max + base + Pro generations are present
  - Each device's `software` field is the latest released iOS version that
    actually supports it
  - Last 4 hardware generations only (current + 3 previous) so the random
    pool gives forensically believable variety without including ancient
    iOS versions that would themselves look suspicious
  - Lens specs preserved from a manual mapping (Apple rarely changes camera
    optics within a hardware generation, so this is updated by a human only)
  - Fails loudly (non-zero exit + ::error:: GH log line) if AppleDB returns
    something we can't parse — never silently commits broken data

Output:
  - dist/index.html rewritten in place (only the IPHONE_DEVICE_POOL block)
  - scripts/last-refresh.json audit log (timestamp, source URL, diff summary)

Exit codes:
  0 — pool updated successfully (or no changes were needed)
  1 — fatal error: source unreachable, malformed payload, or unsafe diff

Run manually:
  python3 scripts/refresh-ios-pool.py
  python3 scripts/refresh-ios-pool.py --dry-run    # don't write, just report
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys
import textwrap
import urllib.error
import urllib.request
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DIST_INDEX = REPO_ROOT / "dist" / "index.html"
AUDIT_LOG = REPO_ROOT / "scripts" / "last-refresh.json"

# AppleDB — comprehensive Apple device + OS database, JSON, daily-updated.
# https://github.com/littlebyteorg/appledb · API: https://api.appledb.dev
# Both endpoints are flat JSON arrays. /device/main.json ≈ 650 KB, iOS ≈ 22 MB.
APPLEDB_DEVICES_URL = "https://api.appledb.dev/device/main.json"
APPLEDB_IOS_URL = "https://api.appledb.dev/ios/iOS/main.json"

# How many hardware generations to keep in the pool. "Generation" here means
# numbered model family (iPhone 17, iPhone 16, ...). With 4, the pool covers
# roughly 3 years of devices — plenty of variety, no ancient iOS versions.
GENERATIONS_TO_KEEP = 4

# Manual lens-spec mapping. Apple writes specific lens-model strings into
# EXIF based on the SKU (Pro Max / Pro / non-Pro), the camera count, and
# the focal length of the main lens. These rarely change within a hardware
# generation, so we maintain them by hand and only edit when Apple ships
# new camera hardware. Keyed by canonical model name.
#
# Source: cross-referenced from real iPhone EXIF dumps + Apple specs page.
LENS_SPECS = {
    "iPhone 17 Pro Max": {
        "lensModel": "iPhone 17 Pro Max back triple camera 6.765mm f/1.78",
        "lensSpec": [[222,100],[16890625,1000000],[178,100],[2798828125,1000000000]],
        "focalLength": [676, 100],
        "focalLength35mm": 24,
        "fNumber": [178, 100],
        "apertureValue": [167, 100],
    },
    "iPhone 17 Pro": {
        "lensModel": "iPhone 17 Pro back triple camera 6.765mm f/1.78",
        "lensSpec": [[222,100],[16890625,1000000],[178,100],[2798828125,1000000000]],
        "focalLength": [676, 100],
        "focalLength35mm": 24,
        "fNumber": [178, 100],
        "apertureValue": [167, 100],
    },
    "iPhone 17": {
        "lensModel": "iPhone 17 back dual camera 5.96mm f/1.6",
        "lensSpec": [[159,100],[596,100],[16,10],[24,10]],
        "focalLength": [596, 100],
        "focalLength35mm": 26,
        "fNumber": [16, 10],
        "apertureValue": [136, 100],
    },
    "iPhone 17 Plus": {
        "lensModel": "iPhone 17 Plus back dual camera 5.96mm f/1.6",
        "lensSpec": [[159,100],[596,100],[16,10],[24,10]],
        "focalLength": [596, 100],
        "focalLength35mm": 26,
        "fNumber": [16, 10],
        "apertureValue": [136, 100],
    },
    "iPhone 16 Pro Max": {
        "lensModel": "iPhone 16 Pro Max back triple camera 6.765mm f/1.78",
        "lensSpec": [[222,100],[16890625,1000000],[178,100],[2798828125,1000000000]],
        "focalLength": [676, 100],
        "focalLength35mm": 24,
        "fNumber": [178, 100],
        "apertureValue": [167, 100],
    },
    "iPhone 16 Pro": {
        "lensModel": "iPhone 16 Pro back triple camera 6.765mm f/1.78",
        "lensSpec": [[222,100],[16890625,1000000],[178,100],[2798828125,1000000000]],
        "focalLength": [676, 100],
        "focalLength35mm": 24,
        "fNumber": [178, 100],
        "apertureValue": [167, 100],
    },
    "iPhone 16 Plus": {
        "lensModel": "iPhone 16 Plus back dual camera 5.96mm f/1.6",
        "lensSpec": [[159,100],[596,100],[16,10],[24,10]],
        "focalLength": [596, 100],
        "focalLength35mm": 26,
        "fNumber": [16, 10],
        "apertureValue": [136, 100],
    },
    "iPhone 16": {
        "lensModel": "iPhone 16 back dual camera 5.96mm f/1.6",
        "lensSpec": [[159,100],[596,100],[16,10],[24,10]],
        "focalLength": [596, 100],
        "focalLength35mm": 26,
        "fNumber": [16, 10],
        "apertureValue": [136, 100],
    },
    "iPhone 15 Pro Max": {
        "lensModel": "iPhone 15 Pro Max back triple camera 6.86mm f/1.78",
        "lensSpec": [[222,100],[1715,100],[178,100],[28,10]],
        "focalLength": [686, 100],
        "focalLength35mm": 24,
        "fNumber": [178, 100],
        "apertureValue": [167, 100],
    },
    "iPhone 15 Pro": {
        "lensModel": "iPhone 15 Pro back triple camera 6.86mm f/1.78",
        "lensSpec": [[222,100],[686,100],[178,100],[28,10]],
        "focalLength": [686, 100],
        "focalLength35mm": 24,
        "fNumber": [178, 100],
        "apertureValue": [167, 100],
    },
    "iPhone 15 Plus": {
        "lensModel": "iPhone 15 Plus back dual camera 5.96mm f/1.6",
        "lensSpec": [[159,100],[596,100],[16,10],[24,10]],
        "focalLength": [596, 100],
        "focalLength35mm": 26,
        "fNumber": [16, 10],
        "apertureValue": [136, 100],
    },
    "iPhone 15": {
        "lensModel": "iPhone 15 back dual camera 5.96mm f/1.6",
        "lensSpec": [[159,100],[596,100],[16,10],[24,10]],
        "focalLength": [596, 100],
        "focalLength35mm": 26,
        "fNumber": [16, 10],
        "apertureValue": [136, 100],
    },
    "iPhone 14 Pro Max": {
        "lensModel": "iPhone 14 Pro Max back triple camera 6.86mm f/1.78",
        "lensSpec": [[222,100],[686,100],[178,100],[28,10]],
        "focalLength": [686, 100],
        "focalLength35mm": 24,
        "fNumber": [178, 100],
        "apertureValue": [167, 100],
    },
    "iPhone 14 Pro": {
        "lensModel": "iPhone 14 Pro back triple camera 6.86mm f/1.78",
        "lensSpec": [[222,100],[686,100],[178,100],[28,10]],
        "focalLength": [686, 100],
        "focalLength35mm": 24,
        "fNumber": [178, 100],
        "apertureValue": [167, 100],
    },
    "iPhone 14 Plus": {
        "lensModel": "iPhone 14 Plus back dual camera 5.7mm f/1.5",
        "lensSpec": [[15,10],[57,10],[15,10],[24,10]],
        "focalLength": [570, 100],
        "focalLength35mm": 26,
        "fNumber": [15, 10],
        "apertureValue": [117, 100],
    },
    "iPhone 14": {
        "lensModel": "iPhone 14 back dual camera 5.7mm f/1.5",
        "lensSpec": [[15,10],[57,10],[15,10],[24,10]],
        "focalLength": [570, 100],
        "focalLength35mm": 26,
        "fNumber": [15, 10],
        "apertureValue": [117, 100],
    },
}

# Which iPhone SKUs we want in the pool, per generation. Pro Max + Pro + base
# is enough variety; Plus is optional (low sales share, but Apple still
# ships them). We include Plus when AppleDB confirms it exists.
DESIRED_SKUS = ["Pro Max", "Pro", "Plus", ""]  # "" = the non-Pro non-Plus base


# ----------------------------------------------------------------------------
# Errors that should fail the workflow loudly (open a GitHub issue rather
# than commit broken data).
# ----------------------------------------------------------------------------
class RefreshError(Exception):
    pass


def fail(msg: str) -> None:
    """Print a GitHub Actions-style error annotation and exit non-zero."""
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(1)


def fetch_json(url: str, *, timeout: float = 30.0) -> Any:
    """GET a URL with a real User-Agent, parse JSON, raise RefreshError on fail."""
    req = urllib.request.Request(
        url,
        headers={
            # AppleDB's mirror occasionally 403s default Python urllib UA
            "User-Agent": "seraphim-studio-injector-refresher/1.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        raise RefreshError(f"HTTP {e.code} fetching {url}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise RefreshError(f"network error fetching {url}: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise RefreshError(f"malformed JSON from {url}: {e}") from e


# ----------------------------------------------------------------------------
# AppleDB shape: /ios/iOS/index.json is a flat array of iOS release records.
# Each looks roughly like:
#   {
#     "osStr": "iOS",
#     "version": "18.3.2",
#     "build": "...",
#     "released": "2025-03-11",
#     "deviceMap": ["iPhone17,3", "iPhone17,4", ...],
#     "beta": false,
#     "rc": false,
#     ...
#   }
# We want the latest stable (non-beta, non-rc) version per identifier, then
# map identifier → marketing name via the device index.
# ----------------------------------------------------------------------------
def parse_release_date(s: str) -> dt.date | None:
    """AppleDB uses YYYY-MM-DD or YYYY-MM or YYYY. Return a date if parseable."""
    if not isinstance(s, str):
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def latest_ios_per_device(ios_releases: list[dict]) -> dict[str, dict]:
    """Map device identifier (e.g. 'iPhone17,3') → latest stable iOS record."""
    if not isinstance(ios_releases, list):
        raise RefreshError(
            f"AppleDB iOS index has unexpected shape: expected list, got "
            f"{type(ios_releases).__name__}"
        )
    by_device: dict[str, dict] = {}
    for rel in ios_releases:
        if not isinstance(rel, dict):
            continue
        if rel.get("beta") or rel.get("rc"):
            continue
        version = rel.get("version")
        if not version or not isinstance(version, str):
            continue
        # Skip versions with non-numeric prefix (rare AppleDB "0.0.1" placeholders)
        if not re.match(r"^\d+(\.\d+){0,3}$", version):
            continue
        released = parse_release_date(rel.get("released", ""))
        for dev in rel.get("deviceMap", []) or []:
            existing = by_device.get(dev)
            if existing is None:
                by_device[dev] = rel
                continue
            # Compare versions as tuples-of-ints for correct ordering
            if version_tuple(version) > version_tuple(existing["version"]):
                by_device[dev] = rel
            elif version_tuple(version) == version_tuple(existing["version"]):
                # Tie-breaker: newer release date wins
                ex_date = parse_release_date(existing.get("released", ""))
                if released and ex_date and released > ex_date:
                    by_device[dev] = rel
    return by_device


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def build_device_index(appledb_devices: Any) -> dict[str, str]:
    """
    Map device identifier (e.g. 'iPhone18,2') → marketing name (e.g. 'iPhone 17 Pro Max').

    AppleDB's /device/main.json is a flat array. Each entry shape:
      {
        "name": "iPhone 17 Pro Max",
        "identifier": ["iPhone18,2"],
        "type": "iPhone",
        "released": "2025-09-19",
        ...
      }
    """
    if not isinstance(appledb_devices, list):
        raise RefreshError(
            f"AppleDB /device/main.json has unexpected shape: expected list, got "
            f"{type(appledb_devices).__name__}"
        )
    mapping: dict[str, str] = {}
    for d in appledb_devices:
        if not isinstance(d, dict):
            continue
        if d.get("type") != "iPhone":
            continue
        name = d.get("name")
        if not isinstance(name, str) or not name.startswith("iPhone"):
            continue
        # Skip unreleased / placeholder entries
        if d.get("released") in (None, "", []):
            continue
        ids = d.get("identifier") or d.get("identifiers") or []
        if isinstance(ids, str):
            ids = [ids]
        for ident in ids:
            if isinstance(ident, str):
                mapping[ident] = name
    if not mapping:
        raise RefreshError(
            "AppleDB returned 0 iPhone identifiers — index is empty or "
            "schema changed; refusing to commit a wiped pool"
        )
    return mapping


def parse_generation(model_name: str) -> int | None:
    """'iPhone 17 Pro Max' → 17. Returns None if the model doesn't match."""
    m = re.match(r"^iPhone\s+(\d+)\b", model_name)
    return int(m.group(1)) if m else None


def parse_sku(model_name: str) -> str:
    """'iPhone 17 Pro Max' → 'Pro Max'. 'iPhone 16' → ''. Unknowns return ''."""
    # Strip 'iPhone <num>' prefix; what's left is the SKU
    m = re.match(r"^iPhone\s+\d+\s*(.*)$", model_name)
    if not m:
        return ""
    tail = m.group(1).strip()
    if tail in ("Pro Max", "Pro", "Plus", ""):
        return tail
    return ""  # Mini, SE, etc — treat as base/unknown


def select_pool(
    device_index: dict[str, str],
    latest_per_device: dict[str, dict],
) -> list[dict]:
    """
    Build the new IPHONE_DEVICE_POOL from current AppleDB data.

    Strategy:
      1. Find the highest iPhone generation present (e.g. 17)
      2. Keep generations [N, N-1, ..., N-GENERATIONS_TO_KEEP+1]
      3. Within each generation, prefer Pro Max + Pro + Plus + base (in that order)
      4. Skip any device we don't have lens specs for (LENS_SPECS keyed on name)
      5. Software = latest iOS shipping for that specific device
    """
    # Collect (gen, name, ident) for every iPhone in the index
    candidates: list[tuple[int, str, str, str]] = []  # gen, sku, name, ident
    for ident, name in device_index.items():
        gen = parse_generation(name)
        if gen is None:
            continue
        sku = parse_sku(name)
        candidates.append((gen, sku, name, ident))

    if not candidates:
        raise RefreshError(
            "no parseable iPhone generations found in AppleDB device index"
        )

    max_gen = max(c[0] for c in candidates)
    min_gen = max_gen - GENERATIONS_TO_KEEP + 1

    # Deduplicate: many iPhones have multiple identifiers (US vs intl).
    # Prefer the identifier with a fresher iOS record so we don't accidentally
    # take a discontinued regional SKU's stale OS version.
    by_name: dict[str, str] = {}  # name → best ident
    for gen, sku, name, ident in candidates:
        if gen < min_gen or gen > max_gen:
            continue
        if name not in LENS_SPECS:
            # No lens spec for this device — skip (we'd write a hole otherwise).
            continue
        # Pick the identifier with the highest known iOS version
        prev = by_name.get(name)
        if prev is None:
            by_name[name] = ident
            continue
        prev_ios = latest_per_device.get(prev, {}).get("version", "0")
        this_ios = latest_per_device.get(ident, {}).get("version", "0")
        if version_tuple(this_ios) > version_tuple(prev_ios):
            by_name[name] = ident

    # Order: newest generation first, then SKU priority (Pro Max > Pro > Plus > base)
    sku_rank = {"Pro Max": 0, "Pro": 1, "Plus": 2, "": 3}
    ordered = sorted(
        by_name.items(),
        key=lambda kv: (
            -parse_generation(kv[0]),
            sku_rank.get(parse_sku(kv[0]), 9),
        ),
    )

    pool: list[dict] = []
    for name, ident in ordered:
        ios_rec = latest_per_device.get(ident)
        if not ios_rec:
            # Device has no shipped iOS in AppleDB (shouldn't happen for
            # released phones); skip rather than write bogus version
            continue
        software = ios_rec["version"]
        # QuickTime software in real iPhone MP4s is the major.minor only.
        qt_software = ".".join(software.split(".")[:2])
        lens = LENS_SPECS[name]
        pool.append({
            "model": name,
            "software": software,
            "qtSoftware": qt_software,
            **lens,
        })

    if len(pool) < 3:
        raise RefreshError(
            f"selected pool has only {len(pool)} devices — refusing to "
            f"commit (probable AppleDB parse failure)"
        )

    return pool


# ----------------------------------------------------------------------------
# Index.html rewriting. We do a regex-pinned slice from `const IPHONE_DEVICE_POOL = [`
# to the matching `];` — exactly one occurrence in the file. The injector
# functions read from this const, so as long as the shape matches, the rest
# of the file is untouched.
# ----------------------------------------------------------------------------
POOL_START_RE = re.compile(r"^const IPHONE_DEVICE_POOL = \[$", re.MULTILINE)
LAST_REFRESHED_RE = re.compile(
    r"^// Last refreshed: \d{4}-\d{2}-\d{2}\. To force a refresh:.*$",
    re.MULTILINE,
)


def render_pool_js(pool: list[dict]) -> str:
    """Render the pool as JS, matching the existing formatting closely."""
    def fmt_rational(r):
        return "[" + ",".join(str(x) for x in r) + "]"

    def fmt_rational_list(rl):
        return "[" + ",".join(fmt_rational(r) for r in rl) + "]"

    lines = ["const IPHONE_DEVICE_POOL = ["]
    for d in pool:
        lines.append("  {")
        lines.append(f"    model: {json.dumps(d['model'])},")
        lines.append(f"    software: {json.dumps(d['software'])},")
        lines.append(f"    qtSoftware: {json.dumps(d['qtSoftware'])},")
        lines.append(f"    lensModel: {json.dumps(d['lensModel'])},")
        lines.append(f"    lensSpec: {fmt_rational_list(d['lensSpec'])},")
        lines.append(f"    focalLength: {fmt_rational(d['focalLength'])},")
        lines.append(f"    focalLength35mm: {d['focalLength35mm']},")
        lines.append(f"    fNumber: {fmt_rational(d['fNumber'])},")
        lines.append(f"    apertureValue: {fmt_rational(d['apertureValue'])},")
        lines.append("  },")
    lines.append("];")
    return "\n".join(lines)


def find_pool_block(src: str) -> tuple[int, int]:
    """Return (start, end) byte indices of the `const IPHONE_DEVICE_POOL = [ ... ];` block."""
    m = POOL_START_RE.search(src)
    if not m:
        raise RefreshError(
            "could not locate `const IPHONE_DEVICE_POOL = [` in dist/index.html — "
            "the injector has been refactored without updating the refresher"
        )
    start = m.start()
    # Walk forward, tracking bracket depth, until we find the closing `];`
    depth = 0
    i = m.end()  # just after the opening [
    depth = 1
    while i < len(src):
        ch = src[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                # expect `];` immediately
                if i + 1 < len(src) and src[i + 1] == ";":
                    return start, i + 2
                raise RefreshError(
                    "found `]` ending IPHONE_DEVICE_POOL but no trailing `;` — "
                    "file is malformed"
                )
        elif ch == "\"" or ch == "'":
            # Skip past strings (simple — no escapes inside our literal-only data)
            quote = ch
            i += 1
            while i < len(src) and src[i] != quote:
                if src[i] == "\\":
                    i += 2
                    continue
                i += 1
        i += 1
    raise RefreshError(
        "unterminated IPHONE_DEVICE_POOL array in dist/index.html"
    )


def update_last_refreshed_comment(src: str, today: dt.date) -> str:
    """Bump the `// Last refreshed: YYYY-MM-DD.` line near the pool."""
    new_line = (
        f"// Last refreshed: {today.isoformat()}. "
        f"To force a refresh: gh workflow run refresh-ios-pool.yml"
    )
    if LAST_REFRESHED_RE.search(src):
        return LAST_REFRESHED_RE.sub(new_line, src, count=1)
    # Not found — leave file alone rather than insert in wrong place
    return src


def parse_existing_pool(src: str) -> list[dict]:
    """
    Best-effort parse of the current pool block so we can diff against the
    new pool. Each entry has a fixed shape, so we extract the key fields with
    regex rather than building a full JS parser.
    """
    start, end = find_pool_block(src)
    block = src[start:end]
    items: list[dict] = []
    for entry in re.finditer(r"\{(.*?)\}", block, re.DOTALL):
        body = entry.group(1)
        model = _str_field(body, "model")
        software = _str_field(body, "software")
        if not model or not software:
            continue
        items.append({"model": model, "software": software})
    return items


def _str_field(body: str, name: str) -> str | None:
    m = re.search(rf"{name}\s*:\s*'([^']*)'", body)
    if m:
        return m.group(1)
    m = re.search(rf'{name}\s*:\s*"([^"]*)"', body)
    return m.group(1) if m else None


def diff_pools(old: list[dict], new: list[dict]) -> dict:
    """Summarize what changed between two pools (for PR body + audit log)."""
    old_by_model = {d["model"]: d for d in old}
    new_by_model = {d["model"]: d for d in new}
    added = [m for m in new_by_model if m not in old_by_model]
    removed = [m for m in old_by_model if m not in new_by_model]
    ios_changed = []
    for model in new_by_model:
        if model in old_by_model:
            if old_by_model[model]["software"] != new_by_model[model]["software"]:
                ios_changed.append({
                    "model": model,
                    "from": old_by_model[model]["software"],
                    "to": new_by_model[model]["software"],
                })
    return {
        "added": added,
        "removed": removed,
        "ios_changed": ios_changed,
        "old_count": len(old),
        "new_count": len(new),
    }


def write_audit_log(
    source_url: str,
    source_fetched_at: dt.datetime,
    diff: dict,
    pool: list[dict],
) -> None:
    AUDIT_LOG.write_text(json.dumps({
        "run_timestamp": dt.datetime.utcnow().isoformat() + "Z",
        "source_url": source_url,
        "source_fetched_at": source_fetched_at.isoformat() + "Z",
        "diff": diff,
        "current_pool": [{"model": d["model"], "software": d["software"]} for d in pool],
    }, indent=2) + "\n")


def format_pr_body(diff: dict, pool: list[dict]) -> str:
    today = dt.date.today().isoformat()
    parts = [
        "## Automated injector refresh",
        "",
        f"Sources: [AppleDB iOS index]({APPLEDB_IOS_URL}) · "
        f"[device index]({APPLEDB_DEVICES_URL})",
        f"Run date: {today}",
        "",
        "### Changes",
        "",
    ]
    if diff["added"]:
        parts.append("**Added devices:**")
        for m in diff["added"]:
            parts.append(f"- `{m}`")
        parts.append("")
    if diff["removed"]:
        parts.append("**Deprecated devices (rotated out of pool):**")
        for m in diff["removed"]:
            parts.append(f"- `{m}`")
        parts.append("")
    if diff["ios_changed"]:
        parts.append("**iOS version updates:**")
        for c in diff["ios_changed"]:
            parts.append(f"- `{c['model']}`: {c['from']} → **{c['to']}**")
        parts.append("")
    if not (diff["added"] or diff["removed"] or diff["ios_changed"]):
        parts.append("_No semantic changes_ — header timestamp refreshed only.")
        parts.append("")
    parts.extend([
        "### Resulting pool",
        "",
    ])
    for d in pool:
        parts.append(f"- `{d['model']}` → iOS `{d['software']}`")
    parts.extend([
        "",
        "### Review checklist",
        "",
        "- [ ] iOS versions look plausible (no betas, no future-dated builds)",
        "- [ ] No device dropped that shouldn't have been (a generation cliff is fine; a single random model missing isn't)",
        "- [ ] Lens specs unchanged unless Apple shipped new camera hardware",
        "",
        "`[autobot]` — opened by `.github/workflows/refresh-ios-pool.yml`. "
        "Do not auto-merge. Approve manually after spot-checking the diff.",
    ])
    return "\n".join(parts)


# ----------------------------------------------------------------------------
# Main entry
# ----------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change but don't write any files",
    )
    parser.add_argument(
        "--output-summary",
        help="write a markdown summary to this path (used by the workflow to "
             "populate the PR body)",
    )
    args = parser.parse_args()

    fetched_at = dt.datetime.utcnow()
    try:
        appledb_devices = fetch_json(APPLEDB_DEVICES_URL)
        ios_releases = fetch_json(APPLEDB_IOS_URL)
    except RefreshError as e:
        fail(str(e))
        return 1  # unreachable, but appeases type-checkers

    try:
        device_index = build_device_index(appledb_devices)
        latest_per_device = latest_ios_per_device(ios_releases)
        new_pool = select_pool(device_index, latest_per_device)
    except RefreshError as e:
        fail(str(e))
        return 1

    src = DIST_INDEX.read_text()
    old_pool = parse_existing_pool(src)
    diff = diff_pools(old_pool, new_pool)

    print(f"AppleDB: {len(device_index)} iPhone identifiers, "
          f"{len(latest_per_device)} have iOS releases")
    print(f"Pool: {len(old_pool)} → {len(new_pool)} devices")
    if diff["added"]:
        print(f"  + added: {', '.join(diff['added'])}")
    if diff["removed"]:
        print(f"  - removed: {', '.join(diff['removed'])}")
    for c in diff["ios_changed"]:
        print(f"  ~ {c['model']}: {c['from']} → {c['to']}")

    no_changes = (
        not diff["added"]
        and not diff["removed"]
        and not diff["ios_changed"]
    )

    if args.dry_run:
        print("(dry-run — not writing)")
        if args.output_summary:
            pathlib.Path(args.output_summary).write_text(format_pr_body(diff, new_pool))
        return 0

    # Rewrite dist/index.html
    start, end = find_pool_block(src)
    new_pool_js = render_pool_js(new_pool)
    today = dt.date.today()
    new_src = src[:start] + new_pool_js + src[end:]
    new_src = update_last_refreshed_comment(new_src, today)

    if new_src != src:
        DIST_INDEX.write_text(new_src)
        print(f"wrote {DIST_INDEX}")
    else:
        print("dist/index.html unchanged")

    write_audit_log(APPLEDB_IOS_URL, fetched_at, diff, new_pool)
    print(f"wrote {AUDIT_LOG}")

    if args.output_summary:
        pathlib.Path(args.output_summary).write_text(format_pr_body(diff, new_pool))

    # Emit a `::set-output` / GITHUB_OUTPUT marker so the workflow can decide
    # whether to open a PR. "changes=true" → at least one device/iOS moved.
    has_changes = not no_changes or (new_src != src)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"changes={'true' if has_changes else 'false'}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
