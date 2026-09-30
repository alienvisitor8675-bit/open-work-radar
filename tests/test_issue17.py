import datetime as dt
import unittest

from scripts import fetch_github


class Issue17RealDataTests(unittest.TestCase):
    NOW = dt.datetime(2026, 9, 30, 0, 0, tzinfo=dt.timezone.utc)

    def issue(self, number, *, project, title, body):
        return {
            "number": number,
            "state": "open",
            "title": title,
            "body": body,
            "html_url": f"https://github.com/{project}/issues/{number}",
            "repository_url": f"https://api.github.com/repos/{project}",
            "labels": [],
            "assignees": [],
            "author_association": "OWNER",
            "created_at": "2026-09-25T00:00:00Z",
            "updated_at": "2026-09-29T00:00:00Z",
        }

    def normalize(self, item):
        return fetch_github.normalize_issue(
            item,
            "bounty",
            fetch_github.iso_z(self.NOW),
            self.NOW,
            180,
            set(),
        )

    def test_build_to_earn_scan_is_meta_not_actionable_work(self):
        item = self.issue(
            9,
            project="uknwplayer/coins-on-the-ground",
            title="Build-to-earn scan — apps, sites & tools — 2026-09-26",
            body=(
                "## Goal\nFind legitimate public paid opportunities.\n"
                "## High-fit live candidates\n"
                "### 1) Devpost\nPrize pool: $2,500 cash.\n"
                "Source: https://example.invalid/hackathon"
            ),
        )
        self.assertIsNone(self.normalize(item))

    def test_internal_test_bounty_incident_report_is_not_an_offer(self):
        item = self.issue(
            864,
            project="kodaksax/Bounty-production",
            title=(
                "Internal test bounty 2a39dbf2 is publicly listed as open with "
                "an accepted hunter; external applicants told 'no response'"
            ),
            body=(
                "## What's wrong\n"
                "Bounty has status = 'open' while also having an accepted hunter.\n"
                "amount / funding | $1.00\n"
                "It is an internal test bounty that is publicly listed."
            ),
        )
        self.assertIsNone(self.normalize(item))

    def test_normal_bug_bounty_remains_eligible(self):
        item = self.issue(
            42,
            project="example/project",
            title="Bounty: $75 fix retry bug",
            body="Bounty: $75 for fixing the retry bug and adding regression tests.",
        )
        self.assertIsNotNone(self.normalize(item))


if __name__ == "__main__":
    unittest.main()
