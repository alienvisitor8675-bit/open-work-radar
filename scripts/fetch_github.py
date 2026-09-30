#!/usr/bin/env python3
"""Hardened GitHub opportunity collector for issue #14.

The bulk of the parser/classifier lives in ``fetch_github_core``.  This module
adds the bounded refresh semantics learned from real scheduled runs: direct
revalidation for records that fall out of search windows, conservative PR and
deadline handling, and commit-worthy output that ignores check-time-only churn.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    from . import fetch_github_core as core
except ImportError:  # pragma: no cover - used when executed as a script
    import fetch_github_core as core

# Preserve the existing module API for callers/tests while overriding the few
# pieces whose semantics are intentionally hardened below.
for _name in dir(core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(core, _name)

# Real daily-run review found planned/unfunded offers carrying this label.
core.NOT_ACTIONABLE_LABELS.add("funding-pending")

# Post-hardening daily runs also surfaced two maintainer-authored meta issues
# that contain bounty/reward amounts but are not offers of work themselves:
# a roundup/scan issue and an incident report about an internal test bounty.
# Keep the rule deliberately title-shaped so ordinary bug-labelled bounties and
# legitimate multi-claim offers remain eligible.
core.INDIRECT = re.compile(
    rf"(?:{core.INDIRECT.pattern})"
    r"|^\s*build-to-earn\s+scan\b"
    r"|^\s*internal\s+test\s+bounty\b.*\b(?:publicly\s+listed|accepted\s+hunter)\b",
    core.INDIRECT.flags,
)


class DirectValidationUnavailable(core.CollectorError):
    """A retained candidate could not be directly revalidated."""


def parse_deadline(body: str, title: str = "") -> dt.datetime | None:
    """Parse only deadlines whose timezone is explicit and understood.

    A bare date/time can be meaningful to a human but is unsafe to expire
    automatically because the source's timezone is unknown.
    """
    text = f"{title}\n{body}"
    match = core.DEADLINE.search(text)
    if not match or not match.group("tz"):
        return None
    dangling = core.TZ_TAIL.match(text[match.end():])
    if dangling:
        return None
    offset = core._tz_offset(match.group("tz"))
    if offset is None:
        return None
    date = match.group("date")
    time_part = match.group("time") or "23:59"
    try:
        naive = dt.datetime.fromisoformat(f"{date}T{time_part}:00")
    except ValueError:
        return None
    return (naive - offset).replace(tzinfo=dt.timezone.utc)


core.parse_deadline = parse_deadline


def linked_work_prs(issue_number: int, body: str, signals: dict[str, Any]) -> list[str]:
    """Return only actual, open PRs that attach work to this issue.

    Body text such as ``PR submitted`` and ordinary comments containing
    ``Fixes #N`` are hints, not proof.  GitHub timeline evidence must identify an
    open PR whose closing keyword points at the issue.
    """
    found: list[str] = []
    for event in signals.get("timeline") or []:
        if not isinstance(event, dict):
            continue
        source = core._source_issue(event)
        if not source.get("pull_request"):
            continue
        if str(source.get("state") or "").lower() != "open":
            continue
        if not core._pr_attaches_work(issue_number, source, event):
            continue
        found.append(str(source.get("html_url") or source.get("url") or "timeline-pr"))
    return found


core.linked_work_prs = linked_work_prs


def availability_unclear(body: str, signals: dict[str, Any]) -> bool:
    """Treat the newest visible availability signal as authoritative enough.

    Lifecycle comments arrive oldest-to-newest.  A maintainer confirmation can
    clear an earlier question, while a newer unanswered question restores the
    unclear state.
    """
    unclear = bool(core.STILL_ACTIVE_Q.search(body or ""))
    for comment in signals.get("comments") or []:
        if not isinstance(comment, dict):
            continue
        text = str(comment.get("body") or "")
        association = str(comment.get("author_association") or "NONE").upper()
        if core.STILL_ACTIVE_Q.search(text):
            unclear = True
        if association in core.MAINTAINERS and core.MAINTAINER_CONFIRM.search(text):
            unclear = False
    return unclear


core.availability_unclear = availability_unclear


class GitHubClient(core.GitHubClient):
    """GitHub client with bounded direct issue revalidation."""

    def fetch_issue(self, project: str, number: int) -> dict[str, Any] | None:
        if "/" not in project:
            return None
        owner, repo = project.split("/", 1)
        try:
            response = self.session.get(
                f"{self.api_url}/repos/{owner}/{repo}/issues/{number}",
                timeout=self.timeout,
            )
        except core.requests.RequestException as exc:
            raise DirectValidationUnavailable(str(exc)) from exc
        if response.status_code in {404, 410}:
            return None
        if not response.ok:
            raise DirectValidationUnavailable(
                f"GitHub API HTTP {response.status_code}: {response.text[:300]}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise DirectValidationUnavailable("direct issue lookup did not return an object")
        return payload


def _lifecycle_signals(
    client: Any,
    record: dict[str, Any],
    item: dict[str, Any],
    cache: dict[tuple[str, int], dict[str, Any]],
) -> dict[str, Any]:
    supplied = item.get("_lifecycle")
    if supplied is not None:
        return supplied if isinstance(supplied, dict) else {}
    key = (str(record["project"]), int(record["issue_number"]))
    if key in cache:
        return cache[key]
    getter = getattr(client, "lifecycle_signals", None)
    if not callable(getter):
        signals: dict[str, Any] = {}
    else:
        try:
            signals = getter(*key) or {}
        except Exception as exc:
            raise DirectValidationUnavailable(str(exc)) from exc
        if not isinstance(signals, dict):
            signals = {}
    cache[key] = signals
    return signals


def _evaluate_issue(
    item: dict[str, Any],
    source_ids: Iterable[str],
    client: Any,
    checked_at: str,
    now: dt.datetime,
    stale_days: int,
    excluded: set[str],
    lifecycle_cache: dict[tuple[str, int], dict[str, Any]],
) -> dict[str, Any] | None:
    ids = sorted({str(value) for value in source_ids if value})
    source_id = ids[0] if ids else "direct-revalidation"
    record = core.normalize_issue(item, source_id, checked_at, now, stale_days, excluded)
    if record is None:
        return None
    signals = _lifecycle_signals(client, record, item, lifecycle_cache)
    record = core.second_stage(record, item, signals, now)
    if record is not None:
        record["discovery_sources"] = ids or [source_id]
    return record


def _commit_projection(records: list[dict[str, Any]]) -> str:
    """Canonical representation used to decide whether Git data changed.

    ``last_checked_at`` is operational freshness, not a material opportunity
    change.  ``last_changed_at`` remains included.
    """
    projected = []
    for record in records:
        projected.append({k: v for k, v in record.items() if k != "last_checked_at"})
    projected.sort(key=lambda row: str(row.get("source_url") or ""))
    return json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


LAST_RUN_STATUS: dict[str, Any] = {}


def collect(
    sources_path: Path,
    output_path: Path,
    client: Any,
    now: dt.datetime | None = None,
    *,
    status_path: Path | None = None,
) -> tuple[int, int, list[str]]:
    """Collect actionable rows with bounded revalidation of search-window misses."""
    global LAST_RUN_STATUS

    sources, stale_days, excluded = core.load_config(sources_path)
    existing = core.load_existing(output_path)
    now = now or core.now_utc()
    checked_at = core.iso_z(now)

    fresh: list[dict[str, Any]] = []
    successful: set[str] = set()
    failed: list[str] = []
    evaluated_urls: set[str] = set()
    lifecycle_cache: dict[tuple[str, int], dict[str, Any]] = {}

    for source in sources:
        try:
            kept = 0
            for item in client.search_issues(source["query"], source["max_pages"]):
                url = str(item.get("html_url") or "")
                try:
                    record = _evaluate_issue(
                        item,
                        [source["id"]],
                        client,
                        checked_at,
                        now,
                        stale_days,
                        excluded,
                        lifecycle_cache,
                    )
                except DirectValidationUnavailable:
                    # A lifecycle lookup failure is not evidence that a new item
                    # is actionable.  Existing rows are handled below.
                    continue
                if url:
                    evaluated_urls.add(url)
                if record is not None:
                    fresh.append(record)
                    kept += 1
            successful.add(source["id"])
            print(f"{source['id']}: kept {kept} candidates", file=sys.stderr)
        except core.SourceFetchError as exc:
            failed.append(source["id"])
            print(f"warning: {source['id']} failed: {exc}", file=sys.stderr)

    if not successful:
        raise core.CollectorError(
            "all sources failed; existing output was left unchanged"
            if existing
            else "all sources failed; no output was written"
        )

    fresh_urls = {str(row.get("source_url") or "") for row in fresh}
    preserved: list[dict[str, Any]] = []
    direct_getter = getattr(client, "fetch_issue", None)
    failed_set = set(failed)

    for old in existing:
        url = str(old.get("source_url") or "")
        if not url or url in fresh_urls or url in evaluated_urls:
            continue

        source_ids = {str(value) for value in old.get("discovery_sources") or [] if value}
        # If every known discovery path failed, absence from search means nothing.
        if source_ids and source_ids <= failed_set:
            preserved.append(old)
            continue

        project = str(old.get("project") or "")
        number = old.get("issue_number")
        if not callable(direct_getter) or not project or not isinstance(number, int):
            preserved.append(old)
            continue

        try:
            item = direct_getter(project, number)
            if item is None:
                continue
            record = _evaluate_issue(
                item,
                source_ids,
                client,
                checked_at,
                now,
                stale_days,
                excluded,
                lifecycle_cache,
            )
        except DirectValidationUnavailable:
            preserved.append(old)
            continue
        if record is not None:
            fresh.append(record)
            fresh_urls.add(url)

    output = core.merge_records([*fresh, *preserved])
    output = core.apply_freshness(output, existing)

    substantive_changed = (
        not output_path.exists()
        or _commit_projection(output) != _commit_projection(existing)
    )
    if substantive_changed:
        core.write_json(output_path, output)

    LAST_RUN_STATUS = {
        "checked_at": checked_at,
        "opportunity_count": len(output),
        "successful_sources": sorted(successful),
        "failed_sources": sorted(failed),
        "substantive_changed": substantive_changed,
    }
    if status_path is not None:
        _write_status(status_path, LAST_RUN_STATUS)

    return len(output), len(successful), failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=Path("sources.yaml"))
    parser.add_argument("--output", type=Path, default=Path("data/opportunities.json"))
    parser.add_argument("--status-output", type=Path)
    args = parser.parse_args(argv)
    try:
        count, successful, failed = collect(
            args.sources,
            args.output,
            GitHubClient(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")),
            status_path=args.status_output,
        )
    except core.CollectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    verb = "updated" if LAST_RUN_STATUS.get("substantive_changed") else "checked"
    print(
        f"{verb} {count} opportunities from {successful} successful sources"
        + (f"; failed: {', '.join(failed)}" if failed else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
