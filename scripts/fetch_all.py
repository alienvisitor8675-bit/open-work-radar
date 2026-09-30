#!/usr/bin/env python3
"""Run GitHub + Opire collectors and merge them without cross-source data loss."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

try:
    from . import fetch_github as github
    from . import fetch_opire as opire
except ImportError:  # pragma: no cover - script execution
    import fetch_github as github
    import fetch_opire as opire


LAST_RUN_STATUS: dict[str, Any] = {}


def _platforms(record: dict[str, Any]) -> set[str]:
    values = {
        str(value)
        for value in record.get("platform_sources") or []
        if value
    }
    if values:
        return values
    source = str(record.get("source") or "")
    if source in {"github", "opire"}:
        return {source}
    # All pre-v0.2 rows came from GitHub.
    return {"github"}


def _source_view(record: dict[str, Any], platform: str) -> dict[str, Any]:
    """Build a source-specific prior row from the public merged record."""
    row = dict(record)
    row["source"] = platform
    row["platform_sources"] = [platform]

    if platform == "github":
        discovery = [
            str(value)
            for value in row.get("discovery_sources") or []
            if value and str(value) != "opire"
        ]
        row["discovery_sources"] = discovery
        row.pop("opire_reward_ids", None)
    elif platform == "opire":
        row["discovery_sources"] = ["opire"]

    return row


def _source_rows(
    existing: Iterable[dict[str, Any]], platform: str
) -> list[dict[str, Any]]:
    return [
        _source_view(record, platform)
        for record in existing
        if platform in _platforms(record)
    ]


def merge_sources(
    github_rows: Iterable[dict[str, Any]],
    opire_rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge source records by canonical GitHub Issue URL.

    GitHub owns Issue metadata/lifecycle. Opire enriches reward provenance and
    aggregate competition signals. Both discovery provenances are retained.
    """
    merged: dict[str, dict[str, Any]] = {}

    for record in github_rows:
        url = str(record.get("source_url") or "")
        if not url:
            continue
        row = dict(record)
        row["platform_sources"] = ["github"]
        row["source"] = "github"
        merged[url] = row

    for record in opire_rows:
        url = str(record.get("source_url") or "")
        if not url:
            continue
        incoming = dict(record)
        incoming["platform_sources"] = ["opire"]
        incoming["source"] = "opire"

        if url not in merged:
            merged[url] = incoming
            continue

        current = merged[url]
        platforms = sorted(
            set(current.get("platform_sources") or [])
            | set(incoming.get("platform_sources") or [])
        )
        discovery = sorted(
            set(current.get("discovery_sources") or [])
            | set(incoming.get("discovery_sources") or [])
        )

        # Keep GitHub Issue metadata from the existing GitHub record while using
        # the explicit platform listing for reward/competition evidence.
        current["reward"] = incoming.get("reward", current.get("reward"))
        current["competition"] = incoming.get(
            "competition", current.get("competition")
        )
        current["opire_reward_ids"] = list(incoming.get("opire_reward_ids") or [])
        if incoming.get("notes"):
            current["notes"] = incoming["notes"]

        current["platform_sources"] = platforms
        current["discovery_sources"] = discovery
        current["source"] = "multi" if len(platforms) > 1 else platforms[0]

        checked = [
            str(value)
            for value in (
                current.get("last_checked_at"),
                incoming.get("last_checked_at"),
            )
            if value
        ]
        if checked:
            current["last_checked_at"] = max(checked)

    return sorted(merged.values(), key=lambda row: str(row.get("source_url") or ""))


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def collect(
    sources_path: Path,
    output_path: Path,
    github_client: Any,
    opire_client: Any,
    now: dt.datetime | None = None,
    *,
    status_path: Path | None = None,
) -> tuple[int, list[str], list[str]]:
    """Collect both sources and preserve failed-source rows."""
    global LAST_RUN_STATUS

    existing = github.load_existing(output_path)
    now = now or github.now_utc()
    checked_at = github.iso_z(now)

    github_previous = _source_rows(existing, "github")
    opire_previous = _source_rows(existing, "opire")
    configured_sources, _stale, _excluded = github.load_config(sources_path)
    configured_ids = [str(row["id"]) for row in configured_sources]

    successful: list[str] = []
    failed: list[str] = []

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        github_path = root / "github.json"
        opire_path = root / "opire.json"
        github.write_json(github_path, github_previous)
        github.write_json(opire_path, opire_previous)

        try:
            github.collect(
                sources_path,
                github_path,
                github_client,
                now=now,
            )
            github_rows = github.load_existing(github_path)
            gh_status = dict(github.LAST_RUN_STATUS)
            successful.extend(
                str(value) for value in gh_status.get("successful_sources") or []
            )
            failed.extend(
                str(value) for value in gh_status.get("failed_sources") or []
            )
        except github.CollectorError as exc:
            print(f"warning: GitHub sources failed: {exc}", file=sys.stderr)
            github_rows = github_previous
            failed.extend(configured_ids)

        try:
            opire_rows, _opire_status = opire.collect(
                sources_path,
                opire_path,
                opire_client,
                github_client,
                now=now,
            )
            successful.append("opire")
        except opire.OpireSourceError as exc:
            print(f"warning: opire failed: {exc}", file=sys.stderr)
            opire_rows = opire_previous
            failed.append("opire")

    successful = sorted(set(successful))
    failed = sorted(set(failed) - set(successful))

    if not successful:
        LAST_RUN_STATUS = {
            "checked_at": checked_at,
            "opportunity_count": len(existing),
            "successful_sources": [],
            "failed_sources": failed,
            "substantive_changed": False,
        }
        if status_path is not None:
            _write_status(status_path, LAST_RUN_STATUS)
        raise github.CollectorError(
            "all GitHub and Opire sources failed; existing output was left unchanged"
        )

    merged = merge_sources(github_rows, opire_rows)
    merged = github.apply_freshness(merged, existing)
    substantive_changed = (
        not output_path.exists()
        or github._commit_projection(merged)
        != github._commit_projection(existing)
    )
    if substantive_changed:
        github.write_json(output_path, merged)

    LAST_RUN_STATUS = {
        "checked_at": checked_at,
        "opportunity_count": len(merged),
        "successful_sources": successful,
        "failed_sources": failed,
        "substantive_changed": substantive_changed,
    }
    if status_path is not None:
        _write_status(status_path, LAST_RUN_STATUS)

    return len(merged), successful, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=Path("sources.yaml"))
    parser.add_argument(
        "--output", type=Path, default=Path("data/opportunities.json")
    )
    parser.add_argument("--status-output", type=Path)
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    try:
        count, successful, failed = collect(
            args.sources,
            args.output,
            github.GitHubClient(token),
            opire.OpireClient(),
            status_path=args.status_output,
        )
    except github.CollectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    verb = "updated" if LAST_RUN_STATUS.get("substantive_changed") else "checked"
    print(
        f"{verb} {count} opportunities from {', '.join(successful)}"
        + (f"; failed: {', '.join(failed)}" if failed else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
