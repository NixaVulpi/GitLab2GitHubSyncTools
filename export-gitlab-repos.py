#!/usr/bin/env python3
import argparse
import csv
import json
import os
import ssl
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


CSV_FIELDS = [
    "id",
    "name",
    "path",
    "path_with_namespace",
    "name_with_namespace",
    "default_branch",
    "visibility",
    "archived",
    "ssh_url_to_repo",
    "http_url_to_repo",
    "web_url",
    "last_activity_at",
]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Export GitLab projects visible to a token and record clone URLs."
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("GITLAB_URL"),
        help="GitLab base URL, for example https://gitlab.example.com. Can also use GITLAB_URL.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GITLAB_TOKEN"),
        help="GitLab personal/project/group access token. Can also use GITLAB_TOKEN.",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for output files. Default: current directory.",
    )
    parser.add_argument(
        "--protocol",
        choices=("ssh", "http", "both"),
        default="ssh",
        help="Which clone URLs to write to gitlab_clone_urls.txt. Default: ssh.",
    )
    parser.add_argument(
        "--membership",
        action="store_true",
        help="Only include projects where the current user is a direct member.",
    )
    parser.add_argument(
        "--archived",
        choices=("all", "true", "false"),
        default="all",
        help="Filter archived projects. Default: all.",
    )
    parser.add_argument(
        "--min-access-level",
        type=int,
        choices=(5, 10, 15, 20, 25, 30, 40, 50),
        help="Only include projects where the user has at least this access level.",
    )
    parser.add_argument(
        "--search",
        help="Server-side project search text.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification for self-signed internal GitLab instances.",
    )
    return parser.parse_args()


def normalize_api_url(gitlab_url):
    base = gitlab_url.rstrip("/")
    if base.endswith("/api/v4"):
        return base
    return base + "/api/v4"


def build_context(insecure):
    if not insecure:
        return None
    return ssl._create_unverified_context()


def gitlab_get_json(api_url, token, params, context):
    url = api_url + "/projects?" + urlencode(params)
    request = Request(url, headers={"PRIVATE-TOKEN": token, "Accept": "application/json"})

    try:
        response = urlopen(request, context=context, timeout=60)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitLab API returned HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cannot connect to GitLab API: {exc.reason}") from exc

    with response:
        body = response.read().decode("utf-8")
        data = json.loads(body)
        if not isinstance(data, list):
            raise RuntimeError(f"Expected a list response, got: {type(data).__name__}")
        return data, response.headers


def iter_projects(api_url, token, args):
    context = build_context(args.insecure)
    page = 1
    per_page = 100

    while True:
        params = {
            "page": page,
            "per_page": per_page,
            "order_by": "path",
            "sort": "asc",
            "simple": "false",
        }

        if args.membership:
            params["membership"] = "true"
        if args.archived != "all":
            params["archived"] = args.archived
        if args.min_access_level is not None:
            params["min_access_level"] = str(args.min_access_level)
        if args.search:
            params["search"] = args.search

        projects, headers = gitlab_get_json(api_url, token, params, context)
        for project in projects:
            yield project

        next_page = headers.get("X-Next-Page")
        if next_page:
            page = int(next_page)
            continue
        break


def project_row(project):
    return {field: project.get(field, "") for field in CSV_FIELDS}


def clone_urls_for(project, protocol):
    urls = []
    if protocol in ("ssh", "both") and project.get("ssh_url_to_repo"):
        urls.append(project["ssh_url_to_repo"])
    if protocol in ("http", "both") and project.get("http_url_to_repo"):
        urls.append(project["http_url_to_repo"])
    return urls


def write_outputs(projects, output_dir, protocol):
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_path = output_dir / "gitlab_projects.csv"
    json_path = output_dir / "gitlab_projects.json"
    clone_urls_path = output_dir / "gitlab_clone_urls.txt"

    rows = [project_row(project) for project in projects]

    with csv_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    with json_path.open("w", encoding="utf-8") as fp:
        json.dump(rows, fp, indent=2, ensure_ascii=False)
        fp.write("\n")

    seen = set()
    clone_urls = []
    for project in projects:
        for url in clone_urls_for(project, protocol):
            if url not in seen:
                seen.add(url)
                clone_urls.append(url)

    with clone_urls_path.open("w", encoding="utf-8", newline="\n") as fp:
        for url in clone_urls:
            fp.write(url + "\n")

    return csv_path, json_path, clone_urls_path, len(clone_urls)


def main():
    args = parse_args()

    if not args.url:
        print("Missing --url or GITLAB_URL.", file=sys.stderr)
        return 2
    if not args.token:
        print("Missing --token or GITLAB_TOKEN.", file=sys.stderr)
        return 2

    api_url = normalize_api_url(args.url)
    output_dir = Path(args.output_dir).resolve()

    print("Fetching projects...")

    projects = list(iter_projects(api_url, args.token, args))
    projects.sort(key=lambda item: item.get("path_with_namespace", "").lower())

    csv_path, json_path, clone_urls_path, clone_count = write_outputs(
        projects,
        output_dir,
        args.protocol,
    )

    print(f"Projects: {len(projects)}")
    print(f"Clone URLs: {clone_count}")
    print(f"CSV: {csv_path}")
    print(f"JSON: {json_path}")
    print(f"Clone URL list: {clone_urls_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
