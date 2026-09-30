#!/usr/bin/env python3
"""Collect Opire-listed rewards after conservative GitHub revalidation.

Opire's public rewards JSON endpoint is currently useful but not documented in
the official Opire docs reviewed for Issue #18. Treat its schema as provisional:
bound pagination, reject unknown shapes/units, and never let a source failure
erase previously known data.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

try:
    from . import fetch_github as gh
except ImportError:  # pragma: no cover - script execution
    import fetch_github as gh

API_URL = "https://api.opire.dev/rewards"
DEFAULT_ITEMS_PER_PAGE = 100
DEFAULT_MAX_PAGES = 10
USER_AGENT = "open-work-radar/0.2 (+https://github.com/yo4e/open-work-radar)"


class OpireSourceError(gh.CollectorError):
    """The provisional Opire feed could not be fetched or understood."""


class OpireClient:
    def __init__(self, api_url: str = API_URL, timeout: int = 30) -> None:
        self.api_url = api_url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"Accept": "application/json", "User-Agent": USER_AGENT}
        )

    def fetch_rewards(
        self,
        *,
        max_pages: int = DEFAULT_MAX_PAGES,
        items_per_page: int = DEFAULT_ITEMS_PER_PAGE,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return rewards and whether pagination reached a definite end.

        The endpoint is provisional, so pagination is deliberately bounded.
        A short/empty page proves completion. Hitting the page cap, or seeing a
        repeated full page, is treated as incomplete so unseen prior records are
        preserved instead of being deleted.
        """
        if not 1 <= max_pages <= 20 or not 1 <= items_per_page <= 100:
            raise ValueError("invalid Opire pagination bounds")

        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        complete = False

        for page in range(1, max_pages + 1):
            try:
                response = self.session.get(
                    self.api_url,
                    params={"page": page, "itemsPerPage": items_per_page},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise OpireSourceError(str(exc)) from exc

            if not response.ok:
                raise OpireSourceError(
                    f"Opire API HTTP {response.status_code}: {response.text[:300]}"
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise OpireSourceError("Opire API did not return JSON") from exc

            if isinstance(payload, list):
                page_rows = payload
            elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
                page_rows = payload["data"]
            else:
                raise OpireSourceError("Opire API schema did not contain a reward list")

            page_rows = [row for row in page_rows if isinstance(row, dict)]
            new_count = 0
            for row in page_rows:
                key = str(row.get("id") or "") or repr(
                    (row.get("url"), row.get("title"), row.get("createdAt"))
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
                new_count += 1

            if len(page_rows) < items_per_page:
                complete = True
                break
            if page > 1 and new_count == 0:
                # Some undocumented endpoints ignore page parameters. Do not
                # interpret a repeated full page as complete coverage.
                break

        return rows, complete


def canonical_github_issue(url: Any) -> tuple[str, int, str] | None:
    raw = str(url or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 4 or parts[2] != "issues":
        return None
    try:
        number = int(parts[3])
    except ValueError:
        return None
    if number <= 0:
        return None
    project = f"{parts[0]}/{parts[1]}"
    return project, number, f"https://github.com/{project}/issues/{number}"


def opire_price_usd(reward: dict[str, Any]) -> float | int | None:
    price = reward.get("pendingPrice")
    if not isinstance(price, dict):
        return None
    try:
        value = float(price.get("value"))
    except (TypeError, ValueError):
        return None
    unit = str(price.get("unit") or "").upper()
    if unit == "USD_CENT":
        value /= 100.0
    elif unit != "USD":
        return None
    if value <= 0:
        return None
    return int(value) if value.is_integer() else round(value, 8)


def _count_users(value: Any) -> int:
    if not isinstance(value, list):
        return 0
    # Count only. Usernames/ids are intentionally not persisted.
    return sum(1 for row in value if isinstance(row, dict))


def _opire_created_at(value: Any) -> str | None:
    if isinstance(value, (int, float)) and value > 0:
        try:
            return gh.iso_z(dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc))
        except (OverflowError, OSError, ValueError):
            return None
    parsed = gh.parse_time(value)
    return gh.iso_z(parsed) if parsed else None


def normalize_opire_reward(
    reward: dict[str, Any],
    issue: dict[str, Any],
    signals: dict[str, Any],
    *,
    checked_at: str,
    now: dt.datetime,
    stale_days: int,
    excluded: set[str],
) -> dict[str, Any] | None:
    target = canonical_github_issue(reward.get("url"))
    amount = opire_price_usd(reward)
    if target is None or amount is None:
        return None
    project, number, canonical_url = target

    if str(issue.get("state") or "").lower() != "open" or issue.get("pull_request"):
        return None
    if issue.get("number") != number:
        return None
    issue_project = gh.repo_name(issue)
    if issue_project.lower() != project.lower() or project.lower() in excluded:
        return None

    updated = gh.parse_time(issue.get("updated_at"))
    if stale_days and updated and now - updated > dt.timedelta(days=stale_days):
        return None

    assignees = sorted(
        str(row.get("login"))
        for row in issue.get("assignees", [])
        if isinstance(row, dict) and row.get("login")
    )
    if assignees:
        return None

    title = str(issue.get("title") or reward.get("title") or "")
    body = str(issue.get("body") or "")
    labels = sorted(
        str(row.get("name"))
        for row in issue.get("labels", [])
        if isinstance(row, dict) and row.get("name")
    )
    association = str(issue.get("author_association") or "NONE").upper()
    text = f"{title}\n{body}"
    label_set = {label.lower() for label in labels}

    # Opire supplies the reward evidence, so the GitHub Issue does not need to
    # repeat an amount. It still must pass the same explicit unavailability
    # signals used by the GitHub collector.
    if (
        gh.UNAVAILABLE.search(text)
        or gh.NOT_ACTIONABLE_TITLE.search(title)
        or gh.NOT_ACTIONABLE.search(text)
        or label_set & gh.NOT_ACTIONABLE_LABELS
        or gh.core.INDIRECT.search(title)
        or gh.SECONDARY_SOURCE.search(text)
    ):
        return None

    reward_id = str(reward.get("id") or "").strip()
    record: dict[str, Any] = {
        "id": f"github-{project.replace('/', '-')}-{number}",
        "source": "opire",
        "source_url": canonical_url,
        "title": title,
        "project": project,
        "issue_number": number,
        "category": gh.category(title, labels),
        "status": "open",
        "github_state": "open",
        "reward": {
            "amount": amount,
            "currency": "USD",
            "provenance": "verified",
            "verified": True,
            "evidence": f"Opire reward {reward_id}" if reward_id else "Opire reward listing",
        },
        "difficulty": "unknown",
        "ai_assistability": "unknown",
        "deadline": None,
        "competition": {
            "attempts": _count_users(reward.get("tryingUsers")),
            "claims": _count_users(reward.get("claimerUsers")),
            "open_prs": None,
        },
        "assignees": assignees,
        "labels": labels,
        "author_association": association,
        "body_excerpt": gh.compact(body),
        "published_at": _opire_created_at(reward.get("createdAt"))
        or issue.get("created_at"),
        "updated_at": issue.get("updated_at"),
        "last_checked_at": checked_at,
        "discovery_sources": ["opire"],
        "platform_sources": ["opire"],
        "opire_reward_ids": [reward_id] if reward_id else [],
        "notes": (
            "Reward is listed by Opire; payout remains subject to creator "
            "acceptance/payment arrangement."
        ),
    }
    deadline = gh.parse_deadline(body, title)
    if deadline:
        record["deadline"] = gh.iso_z(deadline)

    return gh.second_stage(record, issue, signals, now)


def merge_opire_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate by GitHub Issue without over-stating duplicate rewards."""
    merged: dict[str, dict[str, Any]] = {}
    for record in records:
        url = str(record.get("source_url") or "")
        if not url:
            continue
        if url not in merged:
            merged[url] = dict(record)
            continue

        current = merged[url]
        ids = sorted(
            set(current.get("opire_reward_ids") or [])
            | set(record.get("opire_reward_ids") or [])
        )
        current["opire_reward_ids"] = ids

        old_amount = (current.get("reward") or {}).get("amount")
        new_amount = (record.get("reward") or {}).get("amount")
        if isinstance(new_amount, (int, float)) and (
            not isinstance(old_amount, (int, float)) or new_amount > old_amount
        ):
            current["reward"] = dict(record["reward"])

        for field in ("attempts", "claims"):
            old_value = (current.get("competition") or {}).get(field)
            new_value = (record.get("competition") or {}).get(field)
            values = [v for v in (old_value, new_value) if isinstance(v, int)]
            if values:
                current.setdefault("competition", {})[field] = max(values)

        if len(ids) > 1:
            current["notes"] = (
                "Multiple Opire reward records map to this Issue. The displayed "
                "amount is the largest single listed pending amount to avoid "
                "double-counting while the provisional API aggregation semantics "
                "remain undocumented."
            )

    return sorted(merged.values(), key=lambda row: str(row.get("source_url") or ""))


def collect(
    sources_path: Path,
    output_path: Path,
    client: Any,
    github_client: Any,
    now: dt.datetime | None = None,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    items_per_page: int = DEFAULT_ITEMS_PER_PAGE,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collect Opire rewards, preserving prior rows on uncertain validation."""
    _sources, stale_days, excluded = gh.load_config(sources_path)
    existing = gh.load_existing(output_path)
    now = now or gh.now_utc()
    checked_at = gh.iso_z(now)

    rewards, complete_scan = client.fetch_rewards(
        max_pages=max_pages, items_per_page=items_per_page
    )
    fresh: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    blocked_urls: set[str] = set()

    for reward in rewards:
        target = canonical_github_issue(reward.get("url"))
        if target is None:
            continue
        project, number, canonical_url = target
        seen_urls.add(canonical_url)

        try:
            issue = github_client.fetch_issue(project, number)
        except Exception:
            blocked_urls.add(canonical_url)
            continue
        if issue is None:
            continue

        try:
            signals = github_client.lifecycle_signals(project, number) or {}
        except Exception:
            blocked_urls.add(canonical_url)
            continue
        if not isinstance(signals, dict):
            blocked_urls.add(canonical_url)
            continue

        record = normalize_opire_reward(
            reward,
            issue,
            signals,
            checked_at=checked_at,
            now=now,
            stale_days=stale_days,
            excluded=excluded,
        )
        if record is not None:
            fresh.append(record)

    fresh = merge_opire_records(fresh)
    fresh_urls = {str(row.get("source_url") or "") for row in fresh}
    preserved: list[dict[str, Any]] = []
    for old in existing:
        url = str(old.get("source_url") or "")
        if not url or url in fresh_urls:
            continue
        if url in blocked_urls or (not complete_scan and url not in seen_urls):
            preserved.append(old)

    output = merge_opire_records([*fresh, *preserved])
    output = gh.apply_freshness(output, existing)

    substantive_changed = (
        not output_path.exists()
        or gh._commit_projection(output) != gh._commit_projection(existing)
    )
    if substantive_changed:
        gh.write_json(output_path, output)

    status = {
        "checked_at": checked_at,
        "opportunity_count": len(output),
        "complete_scan": complete_scan,
        "substantive_changed": substantive_changed,
    }
    return output, status
