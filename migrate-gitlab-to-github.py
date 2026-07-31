#!/usr/bin/env python3
import argparse
from contextlib import contextmanager
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


PRINT_LOCK = threading.Lock()


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Migrate GitLab repositories from gitlab_projects.json to private GitHub "
            "repositories using SSH for Git data transfer."
        )
    )
    parser.add_argument(
        "--input",
        default="gitlab_projects.json",
        help="Path to gitlab_projects.json exported by export-gitlab-repos.py.",
    )
    parser.add_argument(
        "--github-owner",
        required=True,
        help="GitHub user or organization that will own the new repositories.",
    )
    parser.add_argument(
        "--owner-type",
        choices=("user", "org"),
        default="user",
        help="Whether --github-owner is a user account or organization. Default: user.",
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub token used only to create private repositories. Can also use GITHUB_TOKEN.",
    )
    parser.add_argument(
        "--github-api-url",
        default=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        help="GitHub REST API base URL. Use GitHub Enterprise API URL if needed.",
    )
    parser.add_argument(
        "--github-ssh-host",
        default=os.environ.get("GITHUB_SSH_HOST", "github.com"),
        help="SSH host for GitHub push URLs. Default: github.com.",
    )
    parser.add_argument(
        "--github-push-protocol",
        choices=("ssh", "https"),
        default=os.environ.get("GITHUB_PUSH_PROTOCOL", "ssh"),
        help="Protocol used for GitHub mirror pushes. Default: ssh.",
    )
    parser.add_argument(
        "--repo-name-mode",
        choices=("name", "basename", "flatten"),
        default="name",
        help=(
            "name uses the GitLab project name; basename uses the last path segment; "
            "flatten turns group/subgroup/project into group--subgroup--project. "
            "Default: name."
        ),
    )
    parser.add_argument(
        "--repo-prefix",
        default="",
        help="Prefix added to every target GitHub repository name.",
    )
    parser.add_argument(
        "--mirror-dir",
        default=".gitlab-mirror-cache",
        help="Directory used for local bare mirror repositories.",
    )
    parser.add_argument(
        "--push-existing",
        action="store_true",
        help="Deprecated: existing GitHub repositories are pushed by default.",
    )
    parser.add_argument(
        "--skip-existing-push",
        action="store_true",
        help="For existing GitHub repositories, only sync the default branch setting.",
    )
    parser.add_argument(
        "--delete-mirror-after-push",
        action="store_true",
        help="Delete each local mirror after a successful push.",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help=(
            "Only migrate projects whose path_with_namespace or target repo name contains "
            "this text. Can be specified multiple times."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Migrate at most this many repositories.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=2,
        help="Number of repositories to process in parallel. Default: 2.",
    )
    parser.add_argument(
        "--max-blob-size-mb",
        type=int,
        default=100,
        help="Maximum Git blob size allowed before push. Default: 100.",
    )
    parser.add_argument(
        "--strip-large-files",
        action="store_true",
        help="Rewrite the migration mirror to remove file paths whose blobs exceed --max-blob-size-mb.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without creating repositories, cloning, or pushing.",
    )
    parser.add_argument(
        "--repo-lock-wait-seconds",
        type=int,
        default=1800,
        help="How long to wait when another migration process owns the same cache repository. Default: 1800.",
    )
    parser.add_argument(
        "--stale-repo-lock-seconds",
        type=int,
        default=21600,
        help="Remove this script's own per-repository lock after it is this old. Default: 21600.",
    )
    parser.add_argument(
        "--git-lock-wait-seconds",
        type=int,
        default=60,
        help="How long to wait for Git internal *.lock files inside a cache repository. Default: 60.",
    )
    parser.add_argument(
        "--stale-git-lock-seconds",
        type=int,
        default=300,
        help="Remove Git internal *.lock files after they are this old. Default: 300.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Retry GitHub API calls and network Git commands this many times after the first failure. Default: 3.",
    )
    parser.add_argument(
        "--retry-delay-seconds",
        type=int,
        default=10,
        help="Seconds to wait between retry attempts. Default: 10.",
    )
    parser.add_argument(
        "--fetch-chunk-size",
        type=int,
        default=20,
        help="Number of refs to fetch per git fetch command. Failed chunks are split smaller. Default: 20.",
    )
    parser.add_argument(
        "--failed-record-file",
        default=".gitlab-migration-failures.json",
        help="JSON file used to record failed repositories. Default: .gitlab-migration-failures.json.",
    )
    parser.add_argument(
        "--only-failed",
        action="store_true",
        help="Only migrate repositories currently listed in --failed-record-file.",
    )
    parser.add_argument(
        "--quiet-output",
        action="store_true",
        help=(
            "CI-friendly output: do not print repository names or detailed Git output. "
            "Failures are still recorded in --failed-record-file."
        ),
    )
    return parser.parse_args()


def emit(log, message=""):
    if log is None:
        print(message, flush=True)
    else:
        log.append(message)


def print_realtime(message=""):
    with PRINT_LOCK:
        print(message, flush=True)


def failure_key(project, target_name):
    return str(
        project.get("id")
        or project.get("path_with_namespace")
        or project.get("ssh_url_to_repo")
        or target_name
    )


def load_failure_records(path):
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    if isinstance(data, dict) and isinstance(data.get("failures"), dict):
        return data["failures"]
    if isinstance(data, dict):
        return data
    return {}


def save_failure_records(path, failures):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not failures:
        if path.exists():
            path.unlink()
        return

    payload = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(failures),
        "failures": dict(sorted(failures.items(), key=lambda item: item[1].get("path_with_namespace", item[0]))),
    }
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    tmp_path.replace(path)


def update_failure_record(failures, project, target_name, cache_name, result, exc, log):
    key = failure_key(project, target_name)
    if result == "failed":
        failures[key] = {
            "project_id": project.get("id"),
            "path_with_namespace": project.get("path_with_namespace"),
            "name": project.get("name"),
            "target_name": target_name,
            "cache_name": cache_name,
            "source_url": project.get("ssh_url_to_repo"),
            "default_branch": project.get("default_branch"),
            "failed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "error": str(exc) if exc else "unknown error",
            "log_tail": list(log[-80:]) if log else [],
        }
    else:
        failures.pop(key, None)


def retry_delay(args):
    return max(0, getattr(args, "retry_delay_seconds", 10))


def retry_count(args):
    return max(0, getattr(args, "retries", 3))


def is_retryable_git_command(cmd):
    if not cmd:
        return False
    if Path(cmd[0]).name.lower() != "git":
        return False
    return any(part in {"fetch", "push", "ls-remote"} for part in cmd[1:])


def run(cmd, cwd=None, env=None, log=None, retries=0, retry_delay_seconds=10, input_text=None, display_cmd=None):
    command_text = "+ " + " ".join(display_cmd or cmd)
    emit(log, command_text)

    attempts = 1 + (retries if is_retryable_git_command(cmd) else 0)
    for attempt in range(1, attempts + 1):
        if log is None:
            result = subprocess.run(cmd, cwd=cwd, env=env, input=input_text, text=input_text is not None)
            if result.returncode == 0:
                return
            if attempt >= attempts:
                raise subprocess.CalledProcessError(result.returncode, cmd)
        else:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                input=input_text,
            )
            if result.stdout:
                log.extend(result.stdout.rstrip().splitlines())
            if result.returncode == 0:
                return
            if attempt >= attempts:
                raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout)

        emit(log, f"Command failed; retrying in {retry_delay_seconds}s ({attempt}/{attempts - 1})...")
        time.sleep(retry_delay_seconds)


def github_api(args, method, path, payload=None, log=None):
    if not args.github_token:
        raise RuntimeError("Missing --github-token or GITHUB_TOKEN.")

    api_url = args.github_api_url.rstrip("/")
    url = api_url + path
    body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {args.github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "gitlab-to-github-migration-script",
    }

    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=body, headers=headers, method=method)

    attempts = 1 + retry_count(args)
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=60) as response:
                data = response.read().decode("utf-8")
                if not data:
                    return None
                return json.loads(data)
        except HTTPError as exc:
            data = exc.read().decode("utf-8", errors="replace")
            if exc.code == 404:
                return None
            if exc.code not in {408, 429, 500, 502, 503, 504} or attempt >= attempts:
                raise RuntimeError(f"GitHub API {method} {path} returned HTTP {exc.code}: {data}") from exc
            emit(log, f"GitHub API HTTP {exc.code}; retrying in {retry_delay(args)}s ({attempt}/{attempts - 1})...")
            time.sleep(retry_delay(args))
        except URLError as exc:
            if attempt >= attempts:
                raise RuntimeError(f"Cannot connect to GitHub API: {exc.reason}") from exc
            emit(log, f"Cannot connect to GitHub API: {exc.reason}; retrying in {retry_delay(args)}s ({attempt}/{attempts - 1})...")
            time.sleep(retry_delay(args))

    raise RuntimeError(f"GitHub API {method} {path} failed after {attempts} attempts.")


def repo_exists(args, repo_name, log=None):
    owner = quote(args.github_owner, safe="")
    repo = quote(repo_name, safe="")
    return github_api(args, "GET", f"/repos/{owner}/{repo}", log=log)


def repo_is_empty(repo_info):
    if not repo_info:
        return False
    return int(repo_info.get("size") or 0) == 0 and not repo_info.get("pushed_at")


def create_private_repo(args, repo_name, description, log=None):
    payload = {
        "name": repo_name,
        "private": True,
        "auto_init": False,
        "description": description[:350] if description else "",
    }

    if args.owner_type == "org":
        owner = quote(args.github_owner, safe="")
        return github_api(args, "POST", f"/orgs/{owner}/repos", payload, log=log)

    return github_api(args, "POST", "/user/repos", payload, log=log)


def set_default_branch(args, repo_name, branch_name, dry_run, log=None):
    if not branch_name:
        emit(log, "No GitLab default_branch value; skipping GitHub default branch update.")
        return

    owner = quote(args.github_owner, safe="")
    repo = quote(repo_name, safe="")

    if dry_run:
        emit(log, f"Would set GitHub default branch: {branch_name}")
        return

    github_api(
        args,
        "PATCH",
        f"/repos/{owner}/{repo}",
        {"default_branch": branch_name},
        log=log,
    )
    emit(log, f"Set GitHub default branch: {branch_name}")


def load_projects(path):
    with Path(path).open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, list):
        raise RuntimeError(f"Expected {path} to contain a JSON list.")
    return data


def sanitize_repo_name(name):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-_")
    if not cleaned:
        cleaned = "repo"
    return cleaned[:100]


def sanitize_github_repo_base(name):
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", str(name)).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    if not cleaned:
        cleaned = "repo"
    return cleaned


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


def target_repo_name(project, args, used_names=None):
    project_id = str(project.get("id") or "no-id")
    suffix = f"-{project_id}"
    base = sanitize_github_repo_base(args.repo_prefix + str(project_raw_name(project, args)))
    max_base_len = max(1, 100 - len(suffix))
    return base[:max_base_len].rstrip("-") + suffix


def legacy_cache_name(project, args, used_names):
    raw = project_raw_name(project, args)
    base = sanitize_repo_name(args.repo_prefix + str(raw))
    name = base
    project_id = project.get("id")

    if name in used_names:
        suffix = f"-{project_id}" if project_id else "-duplicate"
        name = sanitize_repo_name(base[: 100 - len(suffix)] + suffix)

    used_names.add(name)
    return name


def safe_dir_name(project, cache_name):
    project_id = str(project.get("id") or "no-id")
    return sanitize_repo_name(f"{cache_name}-{project_id}") + ".git"


def should_include(project, target_name, filters):
    if not filters:
        return True
    haystack = "\n".join(
        [
            str(project.get("path_with_namespace", "")),
            str(project.get("name_with_namespace", "")),
            target_name,
        ]
    ).lower()
    return any(item.lower() in haystack for item in filters)


def github_ssh_url(args, repo_name):
    return f"git@{args.github_ssh_host}:{args.github_owner}/{repo_name}.git"


def github_push_url(args, repo_name):
    if args.github_push_protocol == "https":
        if not args.github_token:
            raise RuntimeError("HTTPS GitHub push requires --github-token or GITHUB_TOKEN.")
        owner = quote(args.github_owner, safe="")
        repo = quote(repo_name, safe="")
        return f"https://x-access-token:{quote(args.github_token, safe='')}@github.com/{owner}/{repo}.git"
    return github_ssh_url(args, repo_name)


def display_url(url):
    return re.sub(r"https://x-access-token:[^@]+@", "https://x-access-token:***@", url)


def clone_or_update_mirror(source_url, mirror_path, args, log=None):
    dry_run = args.dry_run
    if mirror_path.exists():
        if dry_run:
            emit(log, f"Would update existing mirror: {mirror_path}")
            return
        ensure_origin_remote(mirror_path, source_url, log=log)
        normalize_existing_mirror_refs(mirror_path, log=log)
        fetch_selected_refs(source_url, mirror_path, args, log=log)
        return

    if dry_run:
        emit(log, f"Would create mirror cache: {source_url} -> {mirror_path}")
        return

    mirror_path.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "--bare", str(mirror_path)], log=log)
    ensure_origin_remote(mirror_path, source_url, log=log)
    fetch_selected_refs(source_url, mirror_path, args, log=log)


def ensure_origin_remote(mirror_path, source_url, log=None):
    remotes = git_capture(["git", "-C", str(mirror_path), "remote"]).splitlines()
    if "origin" in remotes:
        run(["git", "remote", "set-url", "origin", source_url], cwd=str(mirror_path), log=log)
    else:
        run(["git", "remote", "add", "origin", source_url], cwd=str(mirror_path), log=log)
    run(["git", "config", "--replace-all", "remote.origin.fetch", "+refs/*:refs/*"], cwd=str(mirror_path), log=log)
    run(["git", "config", "remote.origin.mirror", "true"], cwd=str(mirror_path), log=log)


def push_mirror(mirror_path, target_url, args, log=None):
    dry_run = args.dry_run
    if dry_run:
        emit(log, f"Would push mirror: {mirror_path} -> {display_url(target_url)}")
        return

    run(
        ["git", "push", "--mirror", target_url],
        cwd=str(mirror_path),
        log=log,
        retries=retry_count(args),
        retry_delay_seconds=retry_delay(args),
        display_cmd=["git", "push", "--mirror", display_url(target_url)],
    )


def sh_quote(value):
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def git_filter_repo_command():
    local_tool = Path(__file__).resolve().parent / ".migration-tools" / "git_filter_repo.py"
    if local_tool.exists():
        return [sys.executable, str(local_tool)]

    tool = shutil.which("git-filter-repo")
    if tool:
        return [tool]

    return None


def git_capture(cmd, cwd=None, log=None, retries=0, retry_delay_seconds=10):
    attempts = 1 + (retries if is_retryable_git_command(cmd) else 0)
    for attempt in range(1, attempts + 1):
        result = subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode == 0:
            return result.stdout
        if attempt >= attempts:
            raise subprocess.CalledProcessError(
                result.returncode,
                cmd,
                output=result.stdout,
                stderr=result.stderr,
            )
        message = (result.stderr or result.stdout or "").strip().splitlines()
        if message:
            emit(log, message[-1])
        emit(log, f"Command failed; retrying in {retry_delay_seconds}s ({attempt}/{attempts - 1})...")
        time.sleep(retry_delay_seconds)

    return ""


def refs_in_namespaces(mirror_path, namespaces):
    output = git_capture(
        [
            "git",
            "-C",
            str(mirror_path),
            "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(creatordate:unix)",
            *namespaces,
        ]
    )
    refs = []
    for line in output.splitlines():
        if not line:
            continue
        parts = line.split("\0")
        if len(parts) != 3:
            continue
        refname, object_id, created_text = parts
        try:
            created = int(created_text)
        except ValueError:
            created = 0
        refs.append((refname, object_id, created))
    return refs


def ref_file_path(mirror_path, refname):
    return mirror_path.joinpath(*refname.split("/"))


def remove_refs_from_packed_refs(mirror_path, refnames):
    packed_refs = mirror_path / "packed-refs"
    if not packed_refs.exists():
        return

    refnames = set(refnames)
    lines = packed_refs.read_text(encoding="utf-8", errors="surrogateescape").splitlines(keepends=True)
    kept = []
    dropping_peeled = False

    for line in lines:
        if line.startswith("^"):
            if not dropping_peeled:
                kept.append(line)
            continue

        dropping_peeled = False
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            kept.append(line)
            continue

        parts = stripped.split(" ", 1)
        if len(parts) == 2 and parts[1] in refnames:
            dropping_peeled = True
            continue

        kept.append(line)

    packed_refs.write_text("".join(kept), encoding="utf-8", errors="surrogateescape")


def clean_packed_refs(mirror_path, log=None):
    packed_refs = mirror_path / "packed-refs"
    if not packed_refs.exists():
        return

    lines = packed_refs.read_text(encoding="utf-8", errors="surrogateescape").splitlines(keepends=True)
    kept = []
    dropping_peeled = False
    previous_kept_ref = False

    for line in lines:
        if line.startswith("^"):
            peeled_id = line[1:].strip()
            valid_peeled = bool(re.fullmatch(r"[0-9a-fA-F]{40}", peeled_id))
            if not dropping_peeled and previous_kept_ref and valid_peeled:
                kept.append(line)
            else:
                emit(log, f"Removing malformed or orphan packed-refs peeled line: {line.strip()}")
            previous_kept_ref = False
            continue

        dropping_peeled = False
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            kept.append(line)
            previous_kept_ref = False
            continue

        parts = stripped.split(" ", 1)
        if len(parts) != 2:
            kept.append(line)
            previous_kept_ref = False
            continue

        object_id, refname = parts
        reason = unsafe_windows_ref_reason(refname)
        if object_id == "0" * 40:
            emit(log, f"Removing broken zero-object packed ref: {refname}")
            dropping_peeled = True
            previous_kept_ref = False
            continue
        if reason:
            emit(log, f"Removing Windows-unsafe packed ref: {refname} ({reason})")
            dropping_peeled = True
            previous_kept_ref = False
            continue

        kept.append(line)
        previous_kept_ref = True

    packed_refs.write_text("".join(kept), encoding="utf-8", errors="surrogateescape")


def delete_refs_direct(mirror_path, refnames, log=None):
    refnames = sorted(set(refnames))
    if not refnames:
        return

    clean_packed_refs(mirror_path, log=log)
    run(["git", "-C", str(mirror_path), "pack-refs", "--all", "--prune"], log=log)
    clean_packed_refs(mirror_path, log=log)
    remove_refs_from_packed_refs(mirror_path, refnames)

    for refname in refnames:
        remove_file_if_exists(ref_file_path(mirror_path, refname))


def delete_remote_tracking_refs(mirror_path, log=None):
    refs = refs_in_namespaces(mirror_path, ["refs/remotes"])
    refnames = [refname for refname, _object_id, _created in refs]
    for refname in refnames:
        emit(log, f"Deleting cache-only remote tracking ref: {refname}")
    delete_refs_direct(mirror_path, refnames, log=log)

    remotes_dir = mirror_path / "refs" / "remotes"
    if remotes_dir.exists():
        shutil.rmtree(remotes_dir)


def resolve_case_colliding_refs(mirror_path, namespaces, log=None):
    refs = refs_in_namespaces(mirror_path, namespaces)
    groups = {}
    for refname, object_id, created in refs:
        groups.setdefault(refname.lower(), []).append((refname, object_id, created))

    for variants in groups.values():
        if len(variants) < 2:
            continue

        variants.sort(key=lambda item: (item[2], item[0]), reverse=True)
        keep = variants[0]
        emit(log, f"Case-colliding refs found; keeping {keep[0]} ({keep[1][:12]}).")
        delete_refnames = []
        for refname, object_id, _created in variants[1:]:
            emit(log, f"Deleting case-colliding ref from migration cache: {refname} ({object_id[:12]})")
            delete_refnames.append(refname)
        delete_refs_direct(mirror_path, delete_refnames, log=log)


def normalize_existing_mirror_refs(mirror_path, log=None):
    if not mirror_path.exists():
        return

    clean_packed_refs(mirror_path, log=log)
    run(["git", "-C", str(mirror_path), "pack-refs", "--all", "--prune"], log=log)
    clean_packed_refs(mirror_path, log=log)
    delete_remote_tracking_refs(mirror_path, log=log)
    resolve_case_colliding_refs(mirror_path, ["refs/heads", "refs/tags"], log=log)
    run(["git", "-C", str(mirror_path), "pack-refs", "--all", "--prune"], log=log)
    clean_packed_refs(mirror_path, log=log)


def ls_remote_refs(source_url, args, log=None):
    output = git_capture(
        ["git", "ls-remote", source_url],
        log=log,
        retries=retry_count(args),
        retry_delay_seconds=retry_delay(args),
    )
    refs = []
    for line in output.splitlines():
        if not line or "\t" not in line:
            continue
        object_id, refname = line.split("\t", 1)
        if refname.endswith("^{}"):
            continue
        if refname.startswith("refs/heads/") or refname.startswith("refs/tags/"):
            refs.append((refname, object_id))
    return refs


def unsafe_windows_ref_reason(refname):
    if refname.startswith("refs/tags/") and any(ord(char) > 127 for char in refname):
        return "tag contains non-ASCII characters that break packed-refs in this Windows Git environment"

    invalid_chars = set('<>:"\\|?*')
    for part in refname.split("/"):
        if any(char in invalid_chars or ord(char) < 32 for char in part):
            return "contains a Windows-invalid path character"
        if part.endswith(".") or part.endswith(" "):
            return "contains a path segment ending with dot or space"
        if part.upper() in {"CON", "PRN", "AUX", "NUL", "CLOCK$"}:
            return "contains a reserved Windows device name"
        if re.fullmatch(r"COM[1-9]|LPT[1-9]", part.upper()):
            return "contains a reserved Windows device name"
    return ""


def selected_remote_refs(source_url, args, log=None):
    refs = ls_remote_refs(source_url, args, log=log)
    selected = []
    by_lower = {}

    for refname, object_id in sorted(refs, key=lambda item: item[0].lower()):
        reason = unsafe_windows_ref_reason(refname)
        if reason:
            emit(log, f"Skipping ref not safe to migrate from Windows: {refname} ({reason})")
            continue
        by_lower.setdefault(refname.lower(), []).append((refname, object_id))

    for variants in by_lower.values():
        keep = variants[0]
        selected.append(keep)
        for refname, object_id in variants[1:]:
            emit(log, f"Skipping case-colliding ref from migration cache: {refname} ({object_id[:12]}); kept {keep[0]}")

    return sorted(selected, key=lambda item: item[0])


def fetch_selected_refs(source_url, mirror_path, args, log=None):
    refs = selected_remote_refs(source_url, args, log=log)
    wanted_refnames = {refname for refname, _object_id in refs}

    local_refs = refs_in_namespaces(mirror_path, ["refs/heads", "refs/tags", "refs/remotes", "refs/merge-requests"])
    removable = [
        refname
        for refname, _object_id, _created in local_refs
        if refname not in wanted_refnames or unsafe_windows_ref_reason(refname)
    ]
    if removable:
        for refname in removable:
            emit(log, f"Deleting ref not selected for GitHub migration: {refname}")
        delete_refs_direct(mirror_path, removable, log=log)

    if not refs:
        emit(log, "No heads or tags selected for fetch.")
        return

    chunk_size = max(1, args.fetch_chunk_size)
    for index in range(0, len(refs), chunk_size):
        chunk = refs[index : index + chunk_size]
        fetch_ref_chunk(source_url, mirror_path, chunk, args, log=log)


def fetch_ref_chunk(source_url, mirror_path, refs, args, log=None):
    refspecs = [f"+{refname}:{refname}" for refname, _object_id in refs]
    try:
        run(
            ["git", "fetch", "--prune", source_url, *refspecs],
            cwd=str(mirror_path),
            log=log,
            retries=retry_count(args),
            retry_delay_seconds=retry_delay(args),
        )
    except subprocess.CalledProcessError:
        if len(refs) <= 1:
            raise
        midpoint = len(refs) // 2
        emit(log, f"Fetch chunk failed after retries; splitting {len(refs)} refs into {midpoint} and {len(refs) - midpoint}.")
        fetch_ref_chunk(source_url, mirror_path, refs[:midpoint], args, log=log)
        fetch_ref_chunk(source_url, mirror_path, refs[midpoint:], args, log=log)


def file_age_seconds(path):
    try:
        return time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return 0


def internal_git_lock_files(mirror_path):
    if not mirror_path.exists():
        return []
    return sorted(path for path in mirror_path.rglob("*.lock") if path.is_file())


def remove_file_if_exists(path):
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def wait_for_internal_git_locks(mirror_path, args, log=None):
    deadline = time.monotonic() + max(0, args.git_lock_wait_seconds)

    while True:
        lock_files = internal_git_lock_files(mirror_path)
        if not lock_files:
            return

        stale = [path for path in lock_files if file_age_seconds(path) >= args.stale_git_lock_seconds]
        if len(stale) == len(lock_files):
            for path in stale:
                if remove_file_if_exists(path):
                    emit(log, f"Removed stale Git lock: {path}")
            return

        if time.monotonic() >= deadline:
            details = ", ".join(str(path) for path in lock_files[:5])
            if len(lock_files) > 5:
                details += f", ... {len(lock_files) - 5} more"
            raise RuntimeError(f"Git lock file(s) still exist in {mirror_path}: {details}")

        emit(log, f"Waiting for Git lock file(s) in {mirror_path}...")
        time.sleep(2)


@contextmanager
def repo_operation_lock(mirror_path, args, log=None):
    if args.dry_run:
        yield
        return

    lock_root = mirror_path.parent / ".locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / f"{mirror_path.name}.migration.lock"
    deadline = time.monotonic() + max(0, args.repo_lock_wait_seconds)
    acquired = False

    while not acquired:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"pid={os.getpid()}\n")
                handle.write(f"mirror={mirror_path}\n")
                handle.write(f"created={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            acquired = True
        except FileExistsError:
            age = file_age_seconds(lock_path)
            if age >= args.stale_repo_lock_seconds:
                if remove_file_if_exists(lock_path):
                    emit(log, f"Removed stale migration lock: {lock_path}")
                continue

            if time.monotonic() >= deadline:
                raise RuntimeError(f"Timed out waiting for migration lock: {lock_path}")

            emit(log, f"Waiting for migration lock: {lock_path}")
            time.sleep(2)

    try:
        wait_for_internal_git_locks(mirror_path, args, log=log)
        yield
    finally:
        remove_file_if_exists(lock_path)


def large_blobs_in_mirror(mirror_path, max_size_bytes):
    rev_list = git_capture(["git", "-C", str(mirror_path), "rev-list", "--objects", "--all"])
    object_paths = {}
    object_ids = []

    for line in rev_list.splitlines():
        if not line:
            continue
        if " " not in line:
            continue
        object_id, path = line.split(" ", 1)
        object_paths.setdefault(object_id, set()).add(path)
        object_ids.append(object_id)

    if not object_ids:
        return []

    batch = subprocess.run(
        ["git", "-C", str(mirror_path), "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
        input="\n".join(sorted(set(object_ids))) + "\n",
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    large = []
    for line in batch.stdout.splitlines():
        parts = line.split(" ", 2)
        if len(parts) != 3:
            continue
        object_id, object_type, size_text = parts
        if object_type != "blob":
            continue
        size = int(size_text)
        if size <= max_size_bytes:
            continue
        for path in object_paths.get(object_id, []):
            large.append((path, size, object_id))

    large.sort(key=lambda item: (-item[1], item[0]))
    return large


def strip_paths_from_mirror(mirror_path, paths, log=None):
    filter_repo = git_filter_repo_command()
    if filter_repo:
        cmd = [*filter_repo, "--force", "--invert-paths"]
        for path in sorted(set(paths)):
            cmd.extend(["--path", path])
        run(cmd, cwd=str(mirror_path), log=log, input_text="y\n")
        return

    chunk_size = 40
    path_list = sorted(set(paths))
    commands = []

    for index in range(0, len(path_list), chunk_size):
        chunk = path_list[index : index + chunk_size]
        quoted_paths = " ".join(sh_quote(path) for path in chunk)
        commands.append(f"git rm -r --cached --ignore-unmatch -- {quoted_paths}")

    env = os.environ.copy()
    env["FILTER_BRANCH_SQUELCH_WARNING"] = "1"
    run(
        [
            "git",
            "filter-branch",
            "--force",
            "--index-filter",
            " && ".join(commands),
            "--prune-empty",
            "--tag-name-filter",
            "cat",
            "--",
            "--all",
        ],
        cwd=str(mirror_path),
        env=env,
        log=log,
    )

    original_refs = git_capture(
        ["git", "-C", str(mirror_path), "for-each-ref", "--format=%(refname)", "refs/original"]
    )
    for ref in original_refs.splitlines():
        if ref:
            run(["git", "-C", str(mirror_path), "update-ref", "-d", ref], log=log)

    run(["git", "-C", str(mirror_path), "reflog", "expire", "--expire=now", "--all"], log=log)
    run(["git", "-C", str(mirror_path), "gc", "--prune=now"], log=log)


def prepare_mirror_for_github(args, mirror_path, log=None):
    max_size_bytes = args.max_blob_size_mb * 1024 * 1024

    if args.dry_run and not mirror_path.exists():
        emit(log, f"Would scan mirror for blobs larger than {args.max_blob_size_mb} MB.")
        return "ok"

    large = large_blobs_in_mirror(mirror_path, max_size_bytes)
    if not large:
        emit(log, f"No blobs larger than {args.max_blob_size_mb} MB found.")
        return "ok"

    emit(log, f"Found {len(large)} path(s) with blobs larger than {args.max_blob_size_mb} MB:")
    for path, size, object_id in large[:20]:
        emit(log, f"  {size / (1024 * 1024):.2f} MB  {path}  {object_id[:12]}")
    if len(large) > 20:
        emit(log, f"  ... {len(large) - 20} more")

    if not args.strip_large_files:
        emit(log, "Skipping push. Re-run with --strip-large-files to remove these paths from the migration mirror.")
        return "skipped-large-files"

    if args.dry_run:
        emit(log, "Would rewrite mirror history to remove large file paths.")
        return "ok"

    strip_paths_from_mirror(mirror_path, [path for path, _size, _object_id in large], log=log)

    remaining = large_blobs_in_mirror(mirror_path, max_size_bytes)
    if remaining:
        raise RuntimeError(f"{len(remaining)} oversized path(s) remain after history rewrite.")

    emit(log, "Large files stripped from migration mirror.")
    return "ok"


def migrate_one(project, args, mirror_root, target_name, cache_name, repo_info, log=None):
    source_url = project.get("ssh_url_to_repo")
    if not source_url:
        raise RuntimeError("Project has no ssh_url_to_repo.")

    target_url = github_push_url(args, target_name)
    mirror_path = mirror_root / safe_dir_name(project, cache_name)

    emit(log, "")
    emit(log, f"==> {project.get('path_with_namespace', target_name)}")
    emit(log, f"GitLab: {source_url}")
    emit(log, f"GitHub: {display_url(target_url)}")
    emit(log, f"Cache: {mirror_path}")

    if repo_info:
        emit(log, "GitHub repository already exists.")
        if args.skip_existing_push:
            emit(log, "Skipping mirror push because --skip-existing-push was provided.")
            set_default_branch(args, target_name, project.get("default_branch"), args.dry_run, log=log)
            return "default-branch-only"
    else:
        if args.dry_run:
            emit(log, f"Would create private GitHub repository: {args.github_owner}/{target_name}")

    clone_or_update_mirror(source_url, mirror_path, args, log=log)
    prepare_result = prepare_mirror_for_github(args, mirror_path, log=log)
    if prepare_result != "ok":
        return prepare_result
    if not args.dry_run:
        ensure_origin_remote(mirror_path, source_url, log=log)

    if not repo_info and not args.dry_run:
        description = f"Migrated from GitLab project {project.get('path_with_namespace', '')}".strip()
        create_private_repo(args, target_name, description, log=log)
        emit(log, "Created private GitHub repository.")

    push_mirror(mirror_path, target_url, args, log=log)
    set_default_branch(args, target_name, project.get("default_branch"), args.dry_run, log=log)

    if args.delete_mirror_after_push and mirror_path.exists():
        emit(log, f"Deleting local mirror: {mirror_path}")
        shutil.rmtree(mirror_path)

    return "migrated"


def migrate_worker(project, target_name, cache_name, args, mirror_root, log=None, announce_start=False):
    if log is None:
        worker_log = None
    else:
        worker_log = log
    if announce_start and worker_log is not None:
        print_realtime(f"Starting repository: {project.get('path_with_namespace', target_name)}")
    try:
        mirror_path = mirror_root / safe_dir_name(project, cache_name)
        with repo_operation_lock(mirror_path, args, log=worker_log):
            repo_info = None if args.dry_run else repo_exists(args, target_name, log=worker_log)
            result = migrate_one(project, args, mirror_root, target_name, cache_name, repo_info, log=worker_log)
        return result, worker_log or [], None, project, target_name
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        emit(worker_log, f"FAILED {project.get('path_with_namespace', target_name)}: {exc}")
        return "failed", worker_log or [], exc, project, target_name


def main():
    args = parse_args()

    if not shutil.which("git"):
        print("git was not found in PATH.", file=sys.stderr)
        return 2

    input_path = Path(args.input).resolve()
    mirror_root = Path(args.mirror_dir).resolve()
    failed_record_path = Path(args.failed_record_file).resolve()
    failure_records = load_failure_records(failed_record_path)
    projects = load_projects(input_path)

    cache_used_names = set()
    selected = []
    for project in projects:
        target_name = target_repo_name(project, args)
        cache_name = legacy_cache_name(project, args, cache_used_names)
        if should_include(project, target_name, args.only):
            selected.append((project, target_name, cache_name))

    if args.only_failed:
        selected = [
            (project, target_name, cache_name)
            for project, target_name, cache_name in selected
            if failure_key(project, target_name) in failure_records
        ]

    if args.limit is not None:
        selected = selected[: args.limit]

    if not args.quiet_output:
        print(f"Input: {input_path}")
        print(f"Projects selected: {len(selected)}")
        print(f"GitHub owner: {args.github_owner} ({args.owner_type})")
        print(f"Mirror cache: {mirror_root}")
        print(f"Failure record: {failed_record_path}")
        if args.only_failed:
            print(f"Only failed mode: {len(failure_records)} recorded failure(s)")
    else:
        print(f"Projects selected: {len(selected)}")
    jobs = max(1, args.jobs)
    if not args.quiet_output:
        print(f"Parallel jobs: {jobs}")

    counts = {
        "migrated": 0,
        "default-branch-only": 0,
        "skipped-existing": 0,
        "skipped-large-files": 0,
        "failed": 0,
    }

    if jobs == 1:
        total = len(selected)
        for index, (project, target_name, cache_name) in enumerate(selected, 1):
            worker_log = [] if args.quiet_output else None
            result, log, exc, project, target_name = migrate_worker(
                project,
                target_name,
                cache_name,
                args,
                mirror_root,
                log=worker_log,
            )
            counts[result] += 1
            update_failure_record(failure_records, project, target_name, cache_name, result, exc, log)
            save_failure_records(failed_record_path, failure_records)
            if args.quiet_output:
                print(f"Processed {index}/{total}: {result}", flush=True)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            futures = [
                executor.submit(
                    migrate_worker,
                    project,
                    target_name,
                    cache_name,
                    args,
                    mirror_root,
                    [],
                    not args.quiet_output,
                )
                for project, target_name, cache_name in selected
            ]

            completed = 0
            total = len(futures)
            for future in as_completed(futures):
                result, log, exc, project, target_name = future.result()
                completed += 1
                if args.quiet_output:
                    print(f"Processed {completed}/{total}: {result}", flush=True)
                else:
                    print("\n".join(log))
                counts[result] += 1
                cache_name = next(
                    selected_cache_name
                    for selected_project, selected_target_name, selected_cache_name in selected
                    if selected_project is project and selected_target_name == target_name
                )
                update_failure_record(failure_records, project, target_name, cache_name, result, exc, log)
                save_failure_records(failed_record_path, failure_records)

    print("")
    print(
        "Done. "
        f"Migrated: {counts['migrated']}, "
        f"Default branch only: {counts['default-branch-only']}, "
        f"Skipped existing: {counts['skipped-existing']}, "
        f"Skipped large files: {counts['skipped-large-files']}, "
        f"Failed: {counts['failed']}"
    )
    if not args.quiet_output:
        print(f"Recorded failures remaining: {len(failure_records)}")
    elif counts["failed"]:
        first_failure = next(iter(failure_records.values()), {})
        first_error = first_failure.get("error") or "unknown error"
        print(f"First failure: {first_error}")
        print(f"Failure record: {failed_record_path}")

    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
