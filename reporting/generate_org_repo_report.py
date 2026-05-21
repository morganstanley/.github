#!/usr/bin/env python3
"""Generate organization repository metadata report.

Report fields per repository:
- Repository name
- Repository created date/time
- Repository creator (best effort via org audit log)
- Most recent update date/time
- Most recent updater (push actor when available)
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

API_BASE = "https://api.github.com"


class GitHubClient:
    def __init__(self, token: str):
        self.token = token
        self.audit_supported: Optional[bool] = None

    def _request(
        self,
        path: str,
        params: Optional[Dict[str, str]] = None,
        accept: str = "application/vnd.github+json",
        retries: int = 3,
    ) -> Tuple[object, Dict[str, str]]:
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params)
        url = f"{API_BASE}{path}{query}"

        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "org-repo-report-generator",
        }

        last_error: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=60) as response:
                    body = response.read().decode("utf-8")
                    data = json.loads(body) if body else None
                    response_headers = {k.lower(): v for k, v in response.headers.items()}
                    return data, response_headers
            except urllib.error.HTTPError as err:
                if err.code == 403 and err.headers.get("X-RateLimit-Remaining") == "0":
                    reset_epoch = int(err.headers.get("X-RateLimit-Reset", "0"))
                    sleep_for = max(reset_epoch - int(time.time()), 1)
                    print(
                        f"Rate limited. Sleeping for {sleep_for} seconds before retrying {path}.",
                        file=sys.stderr,
                    )
                    time.sleep(sleep_for)
                    continue
                body = err.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"GitHub API error ({err.code}) for {path}: {body}") from err
            except Exception as err:  # pragma: no cover - defensive
                last_error = err
                if attempt < retries:
                    time.sleep(attempt)
                else:
                    raise RuntimeError(f"Failed to call GitHub API for {path}: {err}") from err

        raise RuntimeError(f"Unexpected API failure for {path}: {last_error}")

    def paginate(self, path: str, params: Optional[Dict[str, str]] = None) -> Iterable[object]:
        next_path = path
        next_params = dict(params or {})

        while next_path:
            data, headers = self._request(next_path, next_params)
            if not isinstance(data, list):
                raise RuntimeError(f"Expected list response for {next_path}, got {type(data)}")

            for item in data:
                yield item

            link_header = headers.get("link", "")
            next_link = self._extract_next_link(link_header)
            if next_link:
                parsed = urllib.parse.urlparse(next_link)
                next_path = parsed.path
                next_params = dict(urllib.parse.parse_qsl(parsed.query))
            else:
                next_path = ""
                next_params = {}

    @staticmethod
    def _extract_next_link(link_header: str) -> Optional[str]:
        if not link_header:
            return None
        parts = [p.strip() for p in link_header.split(",")]
        for part in parts:
            sections = [s.strip() for s in part.split(";")]
            if len(sections) < 2:
                continue
            if sections[1] == 'rel="next"' and sections[0].startswith("<") and sections[0].endswith(">"):
                return sections[0][1:-1]
        return None

    def list_org_repos(self, org: str) -> List[Dict[str, object]]:
        repos: List[Dict[str, object]] = []
        for repo in self.paginate(
            f"/orgs/{org}/repos",
            params={
                "per_page": "100",
                "type": "all",
                "sort": "full_name",
                "direction": "asc",
            },
        ):
            if isinstance(repo, dict):
                repos.append(repo)
        return repos

    def get_latest_push_event_info(self, org: str, repo: str) -> Tuple[str, str]:
        data, _ = self._request(
            f"/repos/{org}/{repo}/events",
            params={"per_page": "100"},
        )
        if not isinstance(data, list) or not data:
            return "", ""

        for event in data:
            if not isinstance(event, dict):
                continue
            if event.get("type") != "PushEvent":
                continue
            actor = event.get("actor", {}) if isinstance(event.get("actor"), dict) else {}
            pushed_by = str(actor.get("login") or "")
            pushed_at = str(event.get("created_at") or "")
            return pushed_at, pushed_by

        return "", ""

    def get_latest_commit_info(self, org: str, repo: str, default_branch: Optional[str]) -> Tuple[str, str]:
        if not default_branch:
            return "", ""

        data, _ = self._request(
            f"/repos/{org}/{repo}/commits",
            params={"per_page": "1", "sha": default_branch},
        )
        if not isinstance(data, list) or not data:
            return "", ""

        latest = data[0]
        commit = latest.get("commit", {}) if isinstance(latest, dict) else {}
        committer_data = commit.get("committer", {}) if isinstance(commit, dict) else {}
        author_data = commit.get("author", {}) if isinstance(commit, dict) else {}

        update_at = committer_data.get("date") or author_data.get("date") or ""

        updater = ""
        if isinstance(latest, dict):
            if isinstance(latest.get("committer"), dict):
                updater = latest["committer"].get("login") or ""
            if not updater and isinstance(latest.get("author"), dict):
                updater = latest["author"].get("login") or ""

        if not updater:
            updater = committer_data.get("name") or author_data.get("name") or ""

        return str(update_at or ""), str(updater or "")

    def get_latest_update_info(self, org: str, repo: str, default_branch: Optional[str]) -> Tuple[str, str]:
        pushed_at, pushed_by = self.get_latest_push_event_info(org, repo)
        if pushed_at and pushed_by:
            return pushed_at, pushed_by
        return self.get_latest_commit_info(org, repo, default_branch)

    def get_repo_creator(self, org: str, repo: str) -> Tuple[str, str]:
        if self.audit_supported is False:
            return "", ""

        try:
            data, _ = self._request(
                f"/orgs/{org}/audit-log",
                params={
                    "per_page": "1",
                    "phrase": f"action:repo.create repo:{repo}",
                },
            )
        except RuntimeError as err:
            text = str(err)
            if "(403)" in text or "(404)" in text:
                # Token lacks org audit-log access.
                self.audit_supported = False
                return "", ""
            raise

        self.audit_supported = True

        if not isinstance(data, list) or not data:
            return "", ""

        event = data[0]
        if not isinstance(event, dict):
            return "", ""

        created_by = str(event.get("actor") or "")
        created_at = str(event.get("created_at") or "")
        return created_by, created_at


def normalize_timestamp(timestamp: str) -> str:
    if not timestamp:
        return ""
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return timestamp


def write_csv(rows: List[Dict[str, str]], output_csv: str) -> None:
    headers = [
        "repo_name",
        "repo_created_at",
        "repo_created_by",
        "most_recent_update_at",
        "most_recent_updated_by",
    ]
    with open(output_csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_markdown(rows: List[Dict[str, str]], output_md: str, org: str, audit_supported: bool) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"# {org} Repository Report",
        "",
        f"Generated: {now}",
        "",
        "| Repo Name | Repo Created At | Repo Created By | Most Recent Update At | Most Recent Updated By |",
        "| --- | --- | --- | --- | --- |",
    ]

    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    row["repo_name"] or "n/a",
                    row["repo_created_at"] or "n/a",
                    row["repo_created_by"] or "unknown",
                    row["most_recent_update_at"] or "n/a",
                    row["most_recent_updated_by"] or "unknown",
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- `repo_created_by` is sourced from the organization audit log when accessible.",
            "- `most_recent_updated_by` uses the latest push event actor when available, otherwise latest default-branch commit metadata.",
        ]
    )

    if not audit_supported:
        lines.append(
            "- Creator information is unavailable because the token could not access organization audit log data (typically requires org-level `admin:org`)."
        )

    with open(output_md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main() -> int:
    org = os.getenv("ORG_NAME", "morganstanley")
    token = os.getenv("GH_TOKEN")
    output_csv = os.getenv("OUTPUT_CSV", "reporting/org-repo-report.csv")
    output_md = os.getenv("OUTPUT_MD", "reporting/org-repo-report.md")

    if not token:
        print("GH_TOKEN environment variable is required.", file=sys.stderr)
        return 1

    output_csv_dir = os.path.dirname(output_csv) or "."
    output_md_dir = os.path.dirname(output_md) or "."
    os.makedirs(output_csv_dir, exist_ok=True)
    os.makedirs(output_md_dir, exist_ok=True)

    client = GitHubClient(token)
    repos = client.list_org_repos(org)
    rows: List[Dict[str, str]] = []

    for repo in repos:
        repo_name = str(repo.get("name") or "")
        created_at = str(repo.get("created_at") or "")
        default_branch = repo.get("default_branch")

        latest_update_at, latest_updated_by = client.get_latest_update_info(
            org=org,
            repo=repo_name,
            default_branch=str(default_branch) if default_branch else None,
        )

        created_by, created_from_audit_at = client.get_repo_creator(org, repo_name)

        # Prefer audit-log create timestamp if available; otherwise use repo metadata created_at.
        created_time = created_from_audit_at or created_at

        rows.append(
            {
                "repo_name": repo_name,
                "repo_created_at": normalize_timestamp(created_time),
                "repo_created_by": created_by,
                "most_recent_update_at": normalize_timestamp(latest_update_at),
                "most_recent_updated_by": latest_updated_by,
            }
        )

    rows.sort(key=lambda x: x["repo_name"].lower())
    write_csv(rows, output_csv)
    write_markdown(rows, output_md, org=org, audit_supported=bool(client.audit_supported))

    print(f"Wrote {len(rows)} rows to {output_csv} and {output_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
