# GitLab to GitHub Sync Tools

Utilities for exporting accessible GitLab projects and mirroring them to private GitHub repositories over SSH.

## Setup

Copy the example config and fill in your own values:

```powershell
Copy-Item .\config.example.json .\config.json
notepad .\config.json
```

Required config values:

- `gitlab_url`: GitLab base URL.
- `gitlab_token`: GitLab personal, project, or group access token.
- `github_token`: GitHub token with permission to create and administer repositories.
- `github_owner`: GitHub user or organization that owns the target repositories.
- `github_owner_type`: `org` or `user`.
- `jobs`: number of repositories to process in parallel.

`config.json` is ignored by git and must not be committed.

## One-Click Sync

```powershell
.\sync-gitlab-to-github.ps1
```

CI-friendly output:

```powershell
.\sync-gitlab-to-github.ps1 -QuietOutput
```

Only retry repositories recorded as failed:

```powershell
.\sync-gitlab-to-github.ps1 -OnlyFailed
```

Use a custom config file:

```powershell
.\sync-gitlab-to-github.ps1 -ConfigPath .\my-config.local.json
```

## Scripts

- `export-gitlab-repos.py`: export visible GitLab projects to JSON, CSV, and clone URL text files.
- `migrate-gitlab-to-github.py`: mirror GitLab repositories to private GitHub repositories.
- `seed-mirror-cache-from-local.py`: seed mirror cache from already cloned local repositories.
- `rename-github-repos-to-id-names.py`: rename GitHub repositories to the normalized name plus GitLab id scheme.
- `pull-all-repos.ps1`: recursively update local Git repositories and their submodules.
- `sync-gitlab-to-github.ps1`: config-driven wrapper for export plus migration.

## Notes

- Git data transfer uses SSH, so GitLab and GitHub SSH access must be configured.
- GitHub API access is used for repository creation and default branch updates.
- Repositories with blobs larger than GitHub's 100 MB limit are rewritten in the local migration cache only; the GitLab source repository is not modified.
- Failed repositories are recorded in `.gitlab-migration-failures.json` and removed from that file after a successful retry.
