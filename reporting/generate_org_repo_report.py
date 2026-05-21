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
from email.utils import parsedate_to_datetime
from typing import Dict, Iterable, List, Optional, Tuple

API_BASE = "https://api.github.com"
GRAPHQL_URL = "https://api.github.com/graphql"
UNKNOWN_VALUE = "unknown"


class GitHubClient:
    def __init__(self, token: str):
        self.token = token
        self.audit_supported: Optional[bool] = None

    def _send_request(
        self,
        req: urllib.request.Request,
        request_label: str,
        retries: int = 3,
    ) -> Tuple[object, Dict[str, str]]:
        last_error: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=60) as response:
                    body = response.read().decode("utf-8")
                    data = json.loads(body) if body else None
                    response_headers = {
                        k.lower(): v for k, v in response.headers.items()
                    }
                    return data, response_headers
            except urllib.error.HTTPError as err:
                body = err.read().decode("utf-8", errors="replace")
                retry_after_seconds = self._parse_retry_after_seconds(
                    err.headers.get("Retry-After")
                )
                body_lower = body.lower()
                is_primary_limit = (
                    err.code == 403 and err.headers.get("X-RateLimit-Remaining") == "0"
                )
                is_secondary_limit = err.code == 403 and (
                    "secondary rate limit" in body_lower
                    or "abuse detection" in body_lower
                )
                is_retryable_rate_limit = (
                    err.code == 429 or is_primary_limit or is_secondary_limit
                )

                if is_retryable_rate_limit and attempt < retries:
                    if is_primary_limit:
                        reset_epoch = int(err.headers.get("X-RateLimit-Reset", "0"))
                        sleep_for = max(reset_epoch - int(time.time()), 1)
                    elif retry_after_seconds is not None:
                        sleep_for = max(retry_after_seconds, 1)
                    else:
                        # Backoff for secondary/abuse limits when Retry-After is absent.
                        sleep_for = min(30 * attempt, 300)

                    print(
                        f"Rate limited ({err.code}). Sleeping for {sleep_for} seconds before retrying {request_label}.",
                        file=sys.stderr,
                    )
                    time.sleep(sleep_for)
                    continue

                raise RuntimeError(
                    f"GitHub API error ({err.code}) for {request_label}: {body}"
                ) from err
            except Exception as err:  # pragma: no cover - defensive
                last_error = err
                if attempt < retries:
                    time.sleep(attempt)
                else:
                    raise RuntimeError(
                        f"Failed to call GitHub API for {request_label}: {err}"
                    ) from err

        raise RuntimeError(f"Unexpected API failure for {request_label}: {last_error}")

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
        req = urllib.request.Request(url, headers=headers, method="GET")
        return self._send_request(req, path, retries=retries)

    def _graphql_request(
        self,
        query: str,
        variables: Dict[str, object],
        retries: int = 3,
    ) -> Dict[str, object]:
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "org-repo-report-generator",
        }
        req = urllib.request.Request(
            GRAPHQL_URL, headers=headers, data=payload, method="POST"
        )
        data, _ = self._send_request(req, "graphql", retries=retries)
        if not isinstance(data, dict):
            raise RuntimeError("Unexpected GraphQL response format")
        if isinstance(data.get("errors"), list) and data["errors"]:
            raise RuntimeError(f"GitHub GraphQL error: {data['errors']}")
        return data

    @staticmethod
    def _parse_retry_after_seconds(retry_after: Optional[str]) -> Optional[int]:
        if not retry_after:
            return None

        retry_after = retry_after.strip()
        if retry_after.isdigit():
            return int(retry_after)

        try:
            retry_at = parsedate_to_datetime(retry_after)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(int((retry_at - datetime.now(timezone.utc)).total_seconds()), 0)
        except (TypeError, ValueError, OverflowError):
            return None

    def paginate(
        self, path: str, params: Optional[Dict[str, str]] = None
    ) -> Iterable[object]:
        next_path = path
        next_params = dict(params or {})

        while next_path:
            data, headers = self._request(next_path, next_params)
            if not isinstance(data, list):
                raise RuntimeError(
                    f"Expected list response for {next_path}, got {type(data)}"
                )

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
            if (
                sections[1] == 'rel="next"'
                and sections[0].startswith("<")
                and sections[0].endswith(">")
            ):
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

    def get_repo_creators(self, org: str) -> Dict[str, Tuple[str, str]]:
        if self.audit_supported is False:
            return {}

        try:
            audit_events = self.paginate(
                f"/orgs/{org}/audit-log",
                params={
                    "per_page": "100",
                    "phrase": "action:repo.create",
                },
            )
        except RuntimeError as err:
            text = str(err)
            if "(403)" in text or "(404)" in text:
                # Token lacks org audit-log access.
                self.audit_supported = False
                return {}
            raise

        self.audit_supported = True
        creators: Dict[str, Tuple[str, str]] = {}
        for event in audit_events:
            if not isinstance(event, dict):
                continue
            repo_name = self._extract_audit_repo_name(event, org)
            if not repo_name or repo_name in creators:
                continue
            created_by = str(event.get("actor") or "")
            created_at = str(event.get("created_at") or "")
            creators[repo_name] = (created_by, created_at)

        return creators

    def get_latest_updaters(self, org: str, repo_names: List[str]) -> Dict[str, str]:
        if not repo_names:
            return {}

        query = """
        query RepoLatestUpdater($org: String!, $name: String!) {
        repository(owner: $org, name: $name) {
            name
            defaultBranchRef {
            target {
                __typename
                ... on Commit {
                history(first: 1) {
                    nodes {
                    author {
                        name
                        user {
                        login
                        }
                    }
                    committer {
                        name
                        user {
                        login
                        }
                    }
                    }
                }
                }
            }
            }
        }
        }
        """

        latest_updaters: Dict[str, str] = {}

        for repo_name in repo_names:
            if not repo_name:
                continue

            data = self._graphql_request(query, {"org": org, "name": repo_name})
            repository = (
                data.get("data", {}).get("repository", {})
                if isinstance(data.get("data"), dict)
                else {}
            )
            if not isinstance(repository, dict) or not repository:
                continue

            branch_ref = (
                repository.get("defaultBranchRef", {})
                if isinstance(repository.get("defaultBranchRef"), dict)
                else {}
            )
            target = (
                branch_ref.get("target", {}) if isinstance(branch_ref, dict) else {}
            )
            history = target.get("history", {}) if isinstance(target, dict) else {}
            history_nodes = (
                history.get("nodes", []) if isinstance(history, dict) else []
            )
            if not history_nodes:
                continue

            latest_commit = (
                history_nodes[0] if isinstance(history_nodes[0], dict) else {}
            )
            author = (
                latest_commit.get("author", {})
                if isinstance(latest_commit.get("author"), dict)
                else {}
            )
            committer = (
                latest_commit.get("committer", {})
                if isinstance(latest_commit.get("committer"), dict)
                else {}
            )

            committer_user = (
                committer.get("user", {})
                if isinstance(committer.get("user"), dict)
                else {}
            )
            author_user = (
                author.get("user", {}) if isinstance(author.get("user"), dict) else {}
            )

            updater = str(committer_user.get("login") or author_user.get("login") or "")
            if not updater:
                updater = str(committer.get("name") or author.get("name") or "")

            latest_updaters[repo_name] = updater

        return latest_updaters

    @staticmethod
    def _extract_audit_repo_name(event: Dict[str, object], org: str) -> str:
        repo_value = event.get("repo")
        if isinstance(repo_value, str) and repo_value:
            prefix = f"{org}/"
            return (
                repo_value[len(prefix) :]
                if repo_value.startswith(prefix)
                else repo_value
            )

        repository = event.get("repository")
        if isinstance(repository, dict):
            name = repository.get("name")
            if isinstance(name, str):
                return name

        repo_name = event.get("repo_name")
        if isinstance(repo_name, str):
            return repo_name

        return ""


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
            writer.writerow(
                {
                    "repo_name": row["repo_name"],
                    "repo_created_at": row["repo_created_at"],
                    "repo_created_by": row["repo_created_by"] or UNKNOWN_VALUE,
                    "most_recent_update_at": row["most_recent_update_at"],
                    "most_recent_updated_by": row["most_recent_updated_by"]
                    or UNKNOWN_VALUE,
                }
            )


def write_markdown(
    rows: List[Dict[str, str]], output_md: str, org: str, audit_supported: bool
) -> None:
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
                    row["repo_created_by"] or UNKNOWN_VALUE,
                    row["most_recent_update_at"] or "n/a",
                    row["most_recent_updated_by"] or UNKNOWN_VALUE,
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
    repo_creators = client.get_repo_creators(org)
    latest_updaters = client.get_latest_updaters(
        org, [str(repo.get("name") or "") for repo in repos]
    )
    rows: List[Dict[str, str]] = []

    for repo in repos:
        repo_name = str(repo.get("name") or "")
        created_at = str(repo.get("created_at") or "")
        latest_update_at = str(repo.get("pushed_at") or repo.get("updated_at") or "")
        latest_updated_by = latest_updaters.get(repo_name, "")

        created_by, created_from_audit_at = repo_creators.get(repo_name, ("", ""))

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
    audit_supported = (
        True
        if client.audit_supported is True
        else False if client.audit_supported is False else None
    )
    write_markdown(rows, output_md, org=org, audit_supported=audit_supported)

    print(f"Wrote {len(rows)} rows to {output_csv} and {output_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
