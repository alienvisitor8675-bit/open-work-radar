import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from scripts import fetch_all, fetch_github, fetch_opire


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.ok = 200 <= status < 300
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        page = int((params or {}).get("page", 1))
        self.calls.append(page)
        payload = self.pages.get(page, [])
        return payload if isinstance(payload, FakeResponse) else FakeResponse(payload)


class FakeOpireClient:
    def __init__(self, rewards=None, complete=True, error=None):
        self.rewards = rewards or []
        self.complete = complete
        self.error = error

    def fetch_rewards(self, **kwargs):
        if self.error:
            raise self.error
        return list(self.rewards), self.complete


class FakeGitHubClient:
    def __init__(self, issues=None, search_rows=None):
        self.issues = issues or {}
        self.search_rows = search_rows or []

    def search_issues(self, query, max_pages=1):
        return iter(self.search_rows)

    def fetch_issue(self, project, number):
        return self.issues.get((project, number))

    def lifecycle_signals(self, project, number):
        return {"comments": [], "timeline": []}


class Issue18Tests(unittest.TestCase):
    NOW = dt.datetime(2026, 9, 30, 4, 0, tzinfo=dt.timezone.utc)

    def issue(
        self,
        number=7,
        *,
        project="example/project",
        state="open",
        title="Fix parser edge case",
        body="Please fix the parser.",
        assignees=None,
        labels=None,
        association="OWNER",
    ):
        return {
            "number": number,
            "state": state,
            "title": title,
            "body": body,
            "html_url": f"https://github.com/{project}/issues/{number}",
            "repository_url": f"https://api.github.com/repos/{project}",
            "labels": [{"name": label} for label in (labels or [])],
            "assignees": [{"login": login} for login in (assignees or [])],
            "author_association": association,
            "created_at": "2026-09-20T00:00:00Z",
            "updated_at": "2026-09-29T00:00:00Z",
        }

    def reward(
        self,
        *,
        reward_id="r1",
        url="https://github.com/example/project/issues/7",
        cents=5000,
        claimers=None,
        trying=None,
    ):
        return {
            "id": reward_id,
            "title": "Fix parser edge case",
            "url": url,
            "pendingPrice": {"value": cents, "unit": "USD_CENT"},
            "claimerUsers": claimers or [],
            "tryingUsers": trying or [],
            "createdAt": 1790121600000,
        }

    def sources(self, directory):
        path = Path(directory) / "sources.yaml"
        path.write_text(
            "stale_after_days: 180\n"
            "exclude_repositories: []\n"
            "sources:\n"
            "  - id: one\n"
            "    query: one\n"
            "    max_pages: 2\n",
            encoding="utf-8",
        )
        return path

    def test_price_and_canonical_issue_parsing_are_conservative(self):
        self.assertEqual(fetch_opire.opire_price_usd(self.reward(cents=4822)), 48.22)
        self.assertIsNone(
            fetch_opire.opire_price_usd(
                {"pendingPrice": {"value": 50, "unit": "EUR"}}
            )
        )
        self.assertEqual(
            fetch_opire.canonical_github_issue(
                "https://github.com/example/project/issues/7?x=1"
            ),
            ("example/project", 7, "https://github.com/example/project/issues/7"),
        )
        self.assertIsNone(
            fetch_opire.canonical_github_issue(
                "https://github.com/example/project/pull/7"
            )
        )

    def test_provisional_endpoint_pagination_is_bounded_and_detects_completion(self):
        client = fetch_opire.OpireClient()
        client.session = FakeSession(
            {
                1: [self.reward(reward_id="r1"), self.reward(reward_id="r2")],
                2: [self.reward(reward_id="r3")],
            }
        )
        rows, complete = client.fetch_rewards(max_pages=5, items_per_page=2)
        self.assertTrue(complete)
        self.assertEqual([row["id"] for row in rows], ["r1", "r2", "r3"])
        self.assertEqual(client.session.calls, [1, 2])

        repeated = fetch_opire.OpireClient()
        repeated.session = FakeSession(
            {1: [self.reward(reward_id="r1")], 2: [self.reward(reward_id="r1")]}
        )
        rows, complete = repeated.fetch_rewards(max_pages=3, items_per_page=1)
        self.assertFalse(complete)
        self.assertEqual(len(rows), 1)
        self.assertEqual(repeated.session.calls, [1, 2])

    def test_opire_record_revalidates_github_and_does_not_persist_usernames(self):
        issue = self.issue()
        reward = self.reward(
            claimers=[{"id": "1", "username": "alice"}],
            trying=[
                {"id": "1", "username": "alice"},
                {"id": "2", "username": "bob"},
            ],
        )
        github = FakeGitHubClient({("example/project", 7): issue})

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "opire.json"
            rows, status = fetch_opire.collect(
                self.sources(directory),
                output,
                FakeOpireClient([reward]),
                github,
                now=self.NOW,
            )

            self.assertEqual(status["opportunity_count"], 1)
            self.assertEqual(rows[0]["reward"]["amount"], 50)
            self.assertTrue(rows[0]["reward"]["verified"])
            self.assertEqual(rows[0]["competition"]["claims"], 1)
            self.assertEqual(rows[0]["competition"]["attempts"], 2)
            serialized = json.dumps(rows)
            self.assertNotIn("alice", serialized)
            self.assertNotIn("bob", serialized)

    def test_closed_assigned_or_funding_pending_backing_issue_is_excluded(self):
        cases = [
            self.issue(state="closed"),
            self.issue(assignees=["someone"]),
            self.issue(
                labels=["funding-pending"],
                body="This bounty is not yet funded or claimable.",
            ),
        ]
        for issue in cases:
            with self.subTest(issue=issue):
                record = fetch_opire.normalize_opire_reward(
                    self.reward(),
                    issue,
                    {"comments": [], "timeline": []},
                    checked_at=fetch_github.iso_z(self.NOW),
                    now=self.NOW,
                    stale_days=180,
                    excluded=set(),
                )
                self.assertIsNone(record)

    def test_multiple_opire_rows_use_largest_amount_not_sum(self):
        issue = self.issue()
        signals = {"comments": [], "timeline": []}
        records = [
            fetch_opire.normalize_opire_reward(
                self.reward(reward_id="r1", cents=2000),
                issue,
                signals,
                checked_at=fetch_github.iso_z(self.NOW),
                now=self.NOW,
                stale_days=180,
                excluded=set(),
            ),
            fetch_opire.normalize_opire_reward(
                self.reward(reward_id="r2", cents=3500),
                issue,
                signals,
                checked_at=fetch_github.iso_z(self.NOW),
                now=self.NOW,
                stale_days=180,
                excluded=set(),
            ),
        ]
        merged = fetch_opire.merge_opire_records([row for row in records if row])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["reward"]["amount"], 35)
        self.assertEqual(merged[0]["opire_reward_ids"], ["r1", "r2"])
        self.assertIn("avoid double-counting", merged[0]["notes"])

    def test_cross_source_duplicate_becomes_one_row_with_both_provenances(self):
        issue = self.issue(
            title="Bounty: $25",
            body="Bounty: $25 for the parser fix.",
        )
        github_row = fetch_github.normalize_issue(
            issue,
            "one",
            fetch_github.iso_z(self.NOW),
            self.NOW,
            180,
            set(),
        )
        opire_row = fetch_opire.normalize_opire_reward(
            self.reward(cents=5000),
            issue,
            {"comments": [], "timeline": []},
            checked_at=fetch_github.iso_z(self.NOW),
            now=self.NOW,
            stale_days=180,
            excluded=set(),
        )
        rows = fetch_all.merge_sources([github_row], [opire_row])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["platform_sources"], ["github", "opire"])
        self.assertEqual(rows[0]["source"], "multi")
        self.assertEqual(rows[0]["reward"]["amount"], 50)
        self.assertEqual(set(rows[0]["discovery_sources"]), {"one", "opire"})

    def test_opire_failure_preserves_old_opire_row_while_github_refreshes(self):
        github_issue = self.issue(
            number=8,
            project="example/github",
            title="Bounty: $25",
            body="Bounty: $25 for this work.",
        )
        old_opire = {
            "id": "github-example-old-3",
            "source": "opire",
            "source_url": "https://github.com/example/old/issues/3",
            "title": "Old platform reward",
            "project": "example/old",
            "issue_number": 3,
            "category": "unknown",
            "status": "open",
            "github_state": "open",
            "reward": {
                "amount": 40,
                "currency": "USD",
                "provenance": "verified",
                "verified": True,
                "evidence": "Opire reward old",
            },
            "difficulty": "unknown",
            "ai_assistability": "unknown",
            "deadline": None,
            "competition": {"attempts": 0, "claims": 0, "open_prs": None},
            "assignees": [],
            "labels": [],
            "author_association": "OWNER",
            "body_excerpt": "",
            "published_at": "2026-09-01T00:00:00Z",
            "updated_at": "2026-09-29T00:00:00Z",
            "last_checked_at": "2026-09-29T04:00:00Z",
            "last_changed_at": "2026-09-29T04:00:00Z",
            "discovery_sources": ["opire"],
            "platform_sources": ["opire"],
            "opire_reward_ids": ["old"],
            "notes": None,
        }

        github = FakeGitHubClient(search_rows=[github_issue])
        failing_opire = FakeOpireClient(
            error=fetch_opire.OpireSourceError("temporary failure")
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "opportunities.json"
            fetch_github.write_json(output, [old_opire])
            count, successful, failed = fetch_all.collect(
                self.sources(directory),
                output,
                github,
                failing_opire,
                now=self.NOW,
            )
            rows = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(count, 2)
        self.assertIn("one", successful)
        self.assertIn("opire", failed)
        self.assertEqual(
            {row["source_url"] for row in rows},
            {
                "https://github.com/example/old/issues/3",
                "https://github.com/example/github/issues/8",
            },
        )

    def test_combined_noop_does_not_rewrite_output_for_check_time_only(self):
        issue = self.issue(
            title="Bounty: $25",
            body="Bounty: $25 for the parser fix.",
        )
        github = FakeGitHubClient(
            issues={("example/project", 7): issue},
            search_rows=[issue],
        )
        opire_client = FakeOpireClient([self.reward(cents=5000)])

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "opportunities.json"
            sources = self.sources(directory)
            fetch_all.collect(
                sources, output, github, opire_client, now=self.NOW
            )
            first = output.read_text(encoding="utf-8")

            fetch_all.collect(
                sources,
                output,
                github,
                opire_client,
                now=self.NOW + dt.timedelta(days=1),
            )
            second = output.read_text(encoding="utf-8")

        self.assertEqual(first, second)
        self.assertFalse(fetch_all.LAST_RUN_STATUS["substantive_changed"])


if __name__ == "__main__":
    unittest.main()
