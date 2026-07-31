#!/usr/bin/env python3
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Seed .gitlab-mirror-cache from already cloned local Git repositories "
            "using gitlab_projects.json for project matching."
        )
    )
    parser.add_argument(
        "--projects-json",
        default="gitlab_projects.json",
        help="Path to gitlab_projects.json exported by export-gitlab-repos.py.",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Directory whose direct child directories will be scanned. Default: current directory.",
    )
    parser.add_argument(
        "--mirror-dir",
        default=".gitlab-mirror-cache",
        help="Mirror cache directory used by migrate-gitlab-to-github.py.",
    )
    parser.add_argument(
        "--repo-prefix",
        default="",
        help="Prefix used by migrate-gitlab-to-github.py when naming target repositories.",
    )
    parser.add_argument(
        "--repo-name-mode",
        choices=("name", "basename", "flatten"),
        default="name",
        help="Must match migrate-gitlab-to-github.py. Default: name.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Delete and recreate cache entries that already exist.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show matches and cache paths without writing anything.",
    )
    return parser.parse_args()


def run(cmd, cwd=None, capture=False):
    if capture:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return result.stdout.strip()

    print("+ " + " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)
    return ""


def git(repo_path, *args, capture=False):
    return run(["git", "-C", str(repo_path), *args], capture=capture)


def load_projects(path):
    with Path(path).open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, list):
        raise RuntimeError(f"Expected {path} to contain a JSON list.")
    return data


def normalize_repo_ref(value):
    if not value:
        return ""

    raw = value.strip()
    if raw.endswith(".git"):
        raw = raw[:-4]

    scp_match = re.match(r"^(?:[^@]+@)?([^:]+):(.+)$", raw)
    if scp_match and "://" not in raw:
        host = scp_match.group(1).lower()
        path = scp_match.group(2).strip("/")
        return f"{host}/{path}".lower()

    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        host = parsed.netloc.lower()
        path = parsed.path.strip("/")
        return f"{host}/{path}".lower()

    return raw.strip("/").lower()


def sanitize_repo_name(name):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name)).strip(".-_")
    if not cleaned:
        cleaned = "repo"
    return cleaned[:100]


def target_repo_name(project, repo_name_mode, repo_prefix, used_names):
    path = project.get("path_with_namespace") or project.get("name_with_namespace") or ""
    if repo_name_mode == "name":
        raw = (
            project.get("name")
            or str(project.get("name_with_namespace", "")).split("/")[-1].strip()
            or str(project.get("path_with_namespace", "")).split("/")[-1].strip()
            or project.get("id", "repo")
        )
    elif repo_name_mode == "basename":
        raw = path.split("/")[-1] if path else project.get("id", "repo")
    else:
        raw = path.replace("/", "--") if path else project.get("id", "repo")

    base = sanitize_repo_name(repo_prefix + str(raw))
    name = base
    project_id = project.get("id")

    if name in used_names:
        suffix = f"-{project_id}" if project_id else "-duplicate"
        name = sanitize_repo_name(base[: 100 - len(suffix)] + suffix)

    used_names.add(name)
    return name


def safe_dir_name(project, target_name):
    project_id = str(project.get("id") or "no-id")
    return sanitize_repo_name(f"{target_name}-{project_id}") + ".git"


def build_project_indexes(projects, args):
    by_url = {}
    by_path = {}
    by_name = {}
    used_names = set()
    target_names = {}

    for project in projects:
        target_names[str(project.get("id"))] = target_repo_name(
            project,
            args.repo_name_mode,
            args.repo_prefix,
            used_names,
        )

        for key in ("ssh_url_to_repo", "http_url_to_repo", "web_url"):
            normalized = normalize_repo_ref(project.get(key, ""))
            if normalized:
                by_url[normalized] = project

        path = str(project.get("path_with_namespace", "")).lower()
        if path:
            by_path[path] = project

        name = str(project.get("name") or project.get("path") or "").lower()
        if name:
            by_name.setdefault(name, []).append(project)

    return by_url, by_path, by_name, target_names


def is_direct_repo(path):
    try:
        inside = git(path, "rev-parse", "--is-inside-work-tree", capture=True)
        top = git(path, "rev-parse", "--show-toplevel", capture=True)
    except subprocess.CalledProcessError:
        return False

    return inside == "true" and Path(top).resolve() == path.resolve()


def local_origin_url(path):
    try:
        return git(path, "remote", "get-url", "origin", capture=True)
    except subprocess.CalledProcessError:
        return ""


def match_project(local_dir, by_url, by_path, by_name):
    origin = local_origin_url(local_dir)
    normalized_origin = normalize_repo_ref(origin)
    if normalized_origin and normalized_origin in by_url:
        return by_url[normalized_origin], f"origin URL {origin}"

    dir_name = local_dir.name.lower()
    for path, project in by_path.items():
        if path.split("/")[-1].lower() == dir_name:
            return project, f"directory name matches path {project.get('path_with_namespace')}"

    name_matches = by_name.get(dir_name, [])
    if len(name_matches) == 1:
        return name_matches[0], f"directory name matches unique project name {name_matches[0].get('name')}"

    return None, "no unique match"


def for_each_ref(repo_path, namespace):
    output = git(repo_path, "for-each-ref", "--format=%(refname)%00%(objectname)", namespace, capture=True)
    refs = []
    for line in output.splitlines():
        if not line:
            continue
        refname, sha = line.split("\0", 1)
        refs.append((refname, sha))
    return refs


def cache_has_refs(mirror_path):
    try:
        output = git(mirror_path, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags", capture=True)
    except subprocess.CalledProcessError:
        return False
    return bool(output.strip())


def fetch_refspecs_from_local(local_dir, mirror_path):
    run(
        [
            "git",
            "-C",
            str(mirror_path),
            "fetch",
            "--prune",
            str(local_dir),
            "+refs/heads/*:refs/heads/*",
            "+refs/tags/*:refs/tags/*",
        ]
    )

    for refname, _sha in for_each_ref(local_dir, "refs/remotes/origin"):
        if refname == "refs/remotes/origin/HEAD":
            continue
        branch = refname.removeprefix("refs/remotes/origin/")
        run(
            [
                "git",
                "-C",
                str(mirror_path),
                "fetch",
                str(local_dir),
                f"+{refname}:refs/heads/{branch}",
            ]
        )


def seed_mirror_from_local(local_dir, mirror_path, project, source_url, overwrite, dry_run):
    default_branch = project.get("default_branch")

    if mirror_path.exists():
        if not overwrite:
            if not cache_has_refs(mirror_path):
                print(f"Recreating incomplete cache: {mirror_path}")
                if not dry_run:
                    shutil.rmtree(mirror_path)
            else:
                print(f"SKIP cache exists: {mirror_path}")
                return "skipped-existing"
        else:
            if dry_run:
                print(f"Would delete existing cache: {mirror_path}")
            else:
                shutil.rmtree(mirror_path)

    if mirror_path.exists():
        if not overwrite:
            print(f"SKIP cache exists: {mirror_path}")
            return "skipped-existing"

    if dry_run:
        print(f"Would create mirror cache: {mirror_path}")
        return "seeded"

    mirror_path.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "--bare", str(mirror_path)])
    fetch_refspecs_from_local(local_dir, mirror_path)

    git(mirror_path, "remote", "add", "origin", source_url)
    git(mirror_path, "config", "remote.origin.fetch", "+refs/*:refs/*")
    git(mirror_path, "config", "remote.origin.mirror", "true")

    if default_branch:
        default_ref = f"refs/heads/{default_branch}"
        try:
            git(mirror_path, "show-ref", "--verify", "--quiet", default_ref)
            git(mirror_path, "symbolic-ref", "HEAD", default_ref)
        except subprocess.CalledProcessError:
            print(f"WARN default branch not found in local refs: {default_branch}")

    return "seeded"


def main():
    args = parse_args()

    if not shutil.which("git"):
        print("git was not found in PATH.", file=sys.stderr)
        return 2

    root = Path(args.root).resolve()
    projects_path = Path(args.projects_json).resolve()
    mirror_root = Path(args.mirror_dir).resolve()
    projects = load_projects(projects_path)
    by_url, by_path, by_name, target_names = build_project_indexes(projects, args)

    print(f"Root: {root}")
    print(f"Projects JSON: {projects_path}")
    print(f"Mirror cache: {mirror_root}")

    counts = {
        "seeded": 0,
        "skipped-existing": 0,
        "skipped-no-match": 0,
        "skipped-not-repo": 0,
        "failed": 0,
    }

    for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir():
            continue
        if child.name in {".gitlab-mirror-cache", "__pycache__"}:
            continue
        if child.resolve() == mirror_root:
            continue

        if not is_direct_repo(child):
            counts["skipped-not-repo"] += 1
            continue

        project, reason = match_project(child, by_url, by_path, by_name)
        if not project:
            print(f"SKIP {child.name}: {reason}")
            counts["skipped-no-match"] += 1
            continue

        target_name = target_names[str(project.get("id"))]
        mirror_path = mirror_root / safe_dir_name(project, target_name)
        source_url = project.get("ssh_url_to_repo")

        print("")
        print(f"==> {child.name}")
        print(f"Matched: {project.get('path_with_namespace')} ({reason})")
        print(f"Target repo name: {target_name}")
        print(f"Cache path: {mirror_path}")

        try:
            result = seed_mirror_from_local(
                child,
                mirror_path,
                project,
                source_url,
                args.overwrite,
                args.dry_run,
            )
            counts[result] += 1
        except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
            counts["failed"] += 1
            print(f"FAIL {child.name}: {exc}", file=sys.stderr)

    print("")
    print(
        "Done. "
        f"Seeded: {counts['seeded']}, "
        f"Existing: {counts['skipped-existing']}, "
        f"No match: {counts['skipped-no-match']}, "
        f"Not repo: {counts['skipped-not-repo']}, "
        f"Failed: {counts['failed']}"
    )

    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
