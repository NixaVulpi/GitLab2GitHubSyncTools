#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def parse_args():
    parser = argparse.ArgumentParser(
        description="Rename existing GitHub repositories to normalized GitLab-name-plus-id names."
    )
    parser.add_argument("--input", default="gitlab_projects.json")
    parser.add_argument("--github-owner", required=True)
    parser.add_argument("--github-token", default=os.environ.get("GITHUB_TOKEN"))
    parser.add_argument("--github-api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    parser.add_argument("--repo-name-mode", choices=("name", "basename", "flatten"), default="name")
    parser.add_argument("--repo-prefix", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def github_api(args, method, path, payload=None):
    if not args.github_token:
        raise RuntimeError("Missing --github-token or GITHUB_TOKEN.")

    url = args.github_api_url.rstrip("/") + path
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {args.github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "gitlab-to-github-rename-script",
    }
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=60) as response:
            data = response.read().decode("utf-8")
            return json.loads(data) if data else None
    except HTTPError as exc:
        data = exc.read().decode("utf-8", errors="replace")
        if exc.code == 404:
            return None
        raise RuntimeError(f"GitHub API {method} {path} returned HTTP {exc.code}: {data}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cannot connect to GitHub API: {exc.reason}") from exc


def load_projects(path):
    with Path(path).open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, list):
        raise RuntimeError(f"Expected {path} to contain a JSON list.")
    return data


def sanitize_legacy_repo_name(name):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name)).strip(".-_")
    return cleaned[:100] if cleaned else "repo"


def sanitize_github_repo_base(name):
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", str(name)).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned if cleaned else "repo"


def project_raw_name(project, args):
    path = project.get("path_with_namespace") or project.get("name_with_namespace") or ""
    if args.repo_name_mode == "name":
        return (
            project.get("name")
            or str(project.get("name_with_namespace", "")).split("/")[-1].strip()
            or str(project.get("path_with_namespace", "")).split("/")[-1].strip()
            or project.get("id", "repo")
        )
    if args.repo_name_mode == "basename":
        return path.split("/")[-1] if path else project.get("id", "repo")
    return path.replace("/", "--") if path else project.get("id", "repo")


def new_repo_name(project, args):
    project_id = str(project.get("id") or "no-id")
    suffix = f"-{project_id}"
    base = sanitize_github_repo_base(args.repo_prefix + str(project_raw_name(project, args)))
    max_base_len = max(1, 100 - len(suffix))
    return base[:max_base_len].rstrip("-") + suffix


def old_repo_name(project, args, used_names):
    base = sanitize_legacy_repo_name(args.repo_prefix + str(project_raw_name(project, args)))
    name = base
    project_id = project.get("id")
    if name in used_names:
        suffix = f"-{project_id}" if project_id else "-duplicate"
        name = sanitize_legacy_repo_name(base[: 100 - len(suffix)] + suffix)
    used_names.add(name)
    return name


def repo_exists(args, repo_name):
    owner = quote(args.github_owner, safe="")
    repo = quote(repo_name, safe="")
    return github_api(args, "GET", f"/repos/{owner}/{repo}") is not None


def rename_repo(args, old_name, new_name):
    owner = quote(args.github_owner, safe="")
    repo = quote(old_name, safe="")
    return github_api(args, "PATCH", f"/repos/{owner}/{repo}", {"name": new_name})


def main():
    args = parse_args()
    projects = load_projects(args.input)
    used_names = set()
    planned = []

    for project in projects:
        old_name = old_repo_name(project, args, used_names)
        new_name = new_repo_name(project, args)
        planned.append((project, old_name, new_name))

    renamed = 0
    skipped_missing = 0
    skipped_already = 0
    skipped_conflict = 0
    failed = 0

    for project, old_name, new_name in planned:
        label = project.get("path_with_namespace", old_name)
        if old_name == new_name:
            print(f"SKIP same name: {label} -> {new_name}")
            skipped_already += 1
            continue

        try:
            old_exists = repo_exists(args, old_name)
            new_exists = repo_exists(args, new_name)

            if new_exists:
                print(f"SKIP target exists: {label}: {old_name} -> {new_name}")
                skipped_conflict += 1
                continue
            if not old_exists:
                print(f"SKIP missing old repo: {label}: {old_name}")
                skipped_missing += 1
                continue

            if args.dry_run:
                print(f"Would rename: {label}: {old_name} -> {new_name}")
            else:
                rename_repo(args, old_name, new_name)
                print(f"Renamed: {label}: {old_name} -> {new_name}")
            renamed += 1
        except RuntimeError as exc:
            failed += 1
            print(f"FAILED {label}: {exc}", file=sys.stderr)

    print(
        "Done. "
        f"Renamed: {renamed}, "
        f"Already/same: {skipped_already}, "
        f"Missing old: {skipped_missing}, "
        f"Target exists: {skipped_conflict}, "
        f"Failed: {failed}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
