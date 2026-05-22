import csv
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import generate_org_repo_report as report

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in os.sys.path:
    os.sys.path.insert(0, str(SCRIPT_DIR))


class TestHelpers(unittest.TestCase):
    def test_normalize_timestamp_formats_utc(self) -> None:
        value = report.normalize_timestamp("2026-05-22T12:34:56Z")
        self.assertEqual(value, "2026-05-22 12:34:56 UTC")

    def test_normalize_timestamp_returns_original_for_invalid(self) -> None:
        value = report.normalize_timestamp("not-a-date")
        self.assertEqual(value, "not-a-date")

    def test_extract_next_link(self) -> None:
        link_header = (
            '<https://api.github.com/orgs/test/repos?page=2>; rel="next", '
            '<https://api.github.com/orgs/test/repos?page=8>; rel="last"'
        )
        next_link = report.GitHubClient._extract_next_link(link_header)
        self.assertEqual(next_link, "https://api.github.com/orgs/test/repos?page=2")

    def test_extract_audit_repo_name_prefers_repo_field(self) -> None:
        event = {"repo": "morganstanley/my-repo", "repo_name": "fallback"}
        repo_name = report.GitHubClient._extract_audit_repo_name(event, "morganstanley")
        self.assertEqual(repo_name, "my-repo")


class TestFileOutputs(unittest.TestCase):
    def test_write_csv_replaces_empty_values_with_unknown(self) -> None:
        rows = [
            {
                "repo_name": "demo",
                "repo_created_at": "2026-05-22 00:00:00 UTC",
                "repo_created_by": "",
                "most_recent_update_at": "2026-05-22 01:00:00 UTC",
                "most_recent_updated_by": "",
            }
        ]

        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "report.csv"
            report.write_csv(rows, str(csv_path))

            with csv_path.open("r", encoding="utf-8", newline="") as fh:
                parsed = list(csv.DictReader(fh))

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["repo_created_by"], report.UNKNOWN_VALUE)
        self.assertEqual(parsed[0]["most_recent_updated_by"], report.UNKNOWN_VALUE)

    def test_write_markdown_includes_audit_access_note(self) -> None:
        rows = [
            {
                "repo_name": "demo",
                "repo_created_at": "2026-05-22 00:00:00 UTC",
                "repo_created_by": "alice",
                "most_recent_update_at": "2026-05-22 01:00:00 UTC",
                "most_recent_updated_by": "bob",
            }
        ]

        with tempfile.TemporaryDirectory() as td:
            md_path = Path(td) / "report.md"
            report.write_markdown(
                rows, str(md_path), org="morganstanley", audit_supported=False
            )
            text = md_path.read_text(encoding="utf-8")

        self.assertIn("# morganstanley Repository Report", text)
        self.assertIn("| demo |", text)
        self.assertIn("Creator information is unavailable", text)


class TestClientBehavior(unittest.TestCase):
    def test_get_repo_creators_marks_audit_unsupported_on_403(self) -> None:
        client = report.GitHubClient(token="fake")

        with mock.patch.object(
            client,
            "paginate",
            side_effect=RuntimeError(
                "GitHub API error (403) for /orgs/test/audit-log: denied"
            ),
        ):
            creators = client.get_repo_creators("test")

        self.assertEqual(creators, {})
        self.assertIs(client.audit_supported, False)

    def test_get_latest_updaters_prefers_committer_login(self) -> None:
        client = report.GitHubClient(token="fake")

        def fake_graphql(_query, variables):
            if variables["name"] == "repo-a":
                return {
                    "data": {
                        "repository": {
                            "defaultBranchRef": {
                                "target": {
                                    "history": {
                                        "nodes": [
                                            {
                                                "author": {
                                                    "name": "A",
                                                    "user": {"login": "author-login"},
                                                },
                                                "committer": {
                                                    "name": "C",
                                                    "user": {
                                                        "login": "committer-login"
                                                    },
                                                },
                                            }
                                        ]
                                    }
                                }
                            }
                        }
                    }
                }
            return {
                "data": {
                    "repository": {
                        "defaultBranchRef": {
                            "target": {
                                "history": {
                                    "nodes": [
                                        {
                                            "author": {
                                                "name": "Only Name",
                                                "user": None,
                                            },
                                            "committer": {"name": "", "user": None},
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            }

        with mock.patch.object(client, "_graphql_request", side_effect=fake_graphql):
            updaters = client.get_latest_updaters(
                "morganstanley", ["repo-a", "repo-b", ""]
            )

        self.assertEqual(updaters["repo-a"], "committer-login")
        self.assertEqual(updaters["repo-b"], "Only Name")
        self.assertNotIn("", updaters)


class TestMain(unittest.TestCase):
    def test_main_requires_gh_token(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            exit_code = report.main()
        self.assertEqual(exit_code, 1)

    def test_main_writes_reports_with_mocked_client(self) -> None:
        class FakeClient:
            def __init__(self, token: str):
                self.token = token
                self.audit_supported = True

            def list_org_repos(self, _org: str):
                return [
                    {
                        "name": "zeta",
                        "created_at": "2026-05-21T00:00:00Z",
                        "updated_at": "2026-05-22T00:00:00Z",
                        "pushed_at": "2026-05-22T01:00:00Z",
                    },
                    {
                        "name": "alpha",
                        "created_at": "2026-05-20T00:00:00Z",
                        "updated_at": "2026-05-20T01:00:00Z",
                        "pushed_at": None,
                    },
                ]

            def get_repo_creators(self, _org: str):
                return {"alpha": ("alice", "2026-05-20T00:30:00Z")}

            def get_latest_updaters(self, _org: str, _repo_names):
                return {"zeta": "zane", "alpha": ""}

        with tempfile.TemporaryDirectory() as td:
            output_csv = Path(td) / "out" / "report.csv"
            output_md = Path(td) / "out" / "report.md"

            env = {
                "GH_TOKEN": "fake-token",
                "ORG_NAME": "morganstanley",
                "OUTPUT_CSV": str(output_csv),
                "OUTPUT_MD": str(output_md),
            }

            with mock.patch.object(report, "GitHubClient", FakeClient):
                with mock.patch.dict(os.environ, env, clear=True):
                    exit_code = report.main()

            self.assertEqual(exit_code, 0)
            self.assertTrue(output_csv.exists())
            self.assertTrue(output_md.exists())

            csv_text = output_csv.read_text(encoding="utf-8")
            self.assertLess(csv_text.find("alpha"), csv_text.find("zeta"))
            self.assertIn(report.UNKNOWN_VALUE, csv_text)


if __name__ == "__main__":
    unittest.main()
