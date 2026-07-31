param(
    [string]$ConfigPath = "",
    [switch]$OnlyFailed,
    [switch]$QuietOutput
)

Set-StrictMode -Version Latest

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$GitLabExportScript = Join-Path $ScriptDir "export-gitlab-repos.py"
$GitHubMigrateScript = Join-Path $ScriptDir "migrate-gitlab-to-github.py"

function Get-SyncConfig {
    param(
        [string]$Path
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        $Path = Join-Path $ScriptDir "config.json"
    }

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Missing config file: $Path. Copy config.example.json to config.json and fill in your values."
    }

    $config = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    foreach ($name in @("gitlab_url", "gitlab_token", "github_token")) {
        if (-not $config.PSObject.Properties.Name.Contains($name) -or [string]::IsNullOrWhiteSpace($config.$name)) {
            throw "Missing required config value: $name"
        }
    }

    if (-not $config.PSObject.Properties.Name.Contains("github_owner")) {
        $config | Add-Member -NotePropertyName "github_owner" -NotePropertyValue ""
    }
    if (-not $config.PSObject.Properties.Name.Contains("github_owner_type") -or [string]::IsNullOrWhiteSpace($config.github_owner_type)) {
        $config | Add-Member -Force -NotePropertyName "github_owner_type" -NotePropertyValue "org"
    }
    if (-not $config.PSObject.Properties.Name.Contains("jobs") -or $null -eq $config.jobs) {
        $config | Add-Member -Force -NotePropertyName "jobs" -NotePropertyValue 3
    }

    return $config
}

function Assert-CommandExists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$CommandName
    )

    if (-not (Get-Command $CommandName -ErrorAction SilentlyContinue)) {
        throw "$CommandName was not found in PATH."
    }
}

function Invoke-GitHubApi {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject]$Config,

        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $headers = @{
        Accept                 = "application/vnd.github+json"
        Authorization          = "Bearer $($Config.github_token)"
        "X-GitHub-Api-Version" = "2022-11-28"
        "User-Agent"           = "gitlab-to-github-one-click-sync"
    }

    Invoke-RestMethod -Method Get -Uri "https://api.github.com$Path" -Headers $headers
}

function Resolve-GitHubTarget {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject]$Config
    )

    if (-not [string]::IsNullOrWhiteSpace($Config.github_owner)) {
        return [pscustomobject]@{
            Owner = $Config.github_owner
            Type  = $Config.github_owner_type
        }
    }

    $user = Invoke-GitHubApi -Config $Config -Path "/user"
    $orgResponse = Invoke-GitHubApi -Config $Config -Path "/user/orgs"
    $orgs = if ($null -eq $orgResponse) { @() } else { @($orgResponse) }
    $orgs = @($orgs | Where-Object { $null -ne $_ -and -not [string]::IsNullOrWhiteSpace($_.login) })

    if ($orgs.Count -eq 1) {
        return [pscustomobject]@{
            Owner = $orgs[0].login
            Type  = "org"
        }
    }

    if ($orgs.Count -eq 0) {
        return [pscustomobject]@{
            Owner = $user.login
            Type  = "user"
        }
    }

    $orgNames = ($orgs | ForEach-Object { $_.login }) -join ", "
    throw "This token can see multiple GitHub organizations: $orgNames. Set github_owner in config.json."
}

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Title,

        [Parameter(Mandatory = $true)]
        [scriptblock]$Action,

        [switch]$Quiet
    )

    if (-not $Quiet) {
        Write-Host ""
        Write-Host "==> $Title" -ForegroundColor Cyan
    }
    & $Action
}

Assert-CommandExists -CommandName "git"
Assert-CommandExists -CommandName "python"

if (-not (Test-Path -LiteralPath $GitLabExportScript)) {
    throw "Missing script: $GitLabExportScript"
}

if (-not (Test-Path -LiteralPath $GitHubMigrateScript)) {
    throw "Missing script: $GitHubMigrateScript"
}

Push-Location $ScriptDir
try {
    $config = Get-SyncConfig -Path $ConfigPath
    $configFilePath = if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
        Join-Path $ScriptDir "config.json"
    } else {
        (Resolve-Path -LiteralPath $ConfigPath).Path
    }
    $workDir = Split-Path -Parent $configFilePath
    $projectsJson = Join-Path $workDir "gitlab_projects.json"
    $failureRecord = Join-Path $workDir ".gitlab-migration-failures.json"
    $mirrorDir = Join-Path $workDir ".gitlab-mirror-cache"

    $env:GITLAB_URL = $config.gitlab_url
    $env:GITLAB_TOKEN = $config.gitlab_token
    $env:GITHUB_TOKEN = $config.github_token

    $target = Resolve-GitHubTarget -Config $config
    if (-not $QuietOutput) {
        Write-Host "GitHub target: $($target.Owner) ($($target.Type))" -ForegroundColor Green
    }

    Invoke-Step -Title "Export GitLab projects" -Quiet:$QuietOutput -Action {
        if ($QuietOutput) {
            $exportLog = Join-Path $workDir "export-gitlab-repos.log"
            python $GitLabExportScript --output-dir $workDir --protocol ssh *> $exportLog
            if ($LASTEXITCODE -ne 0) {
                throw "GitLab export failed. See log: $exportLog"
            }
        } else {
            python $GitLabExportScript --output-dir $workDir --protocol ssh
            if ($LASTEXITCODE -ne 0) {
                throw "GitLab export failed with exit code $LASTEXITCODE."
            }
        }

        if (-not (Test-Path -LiteralPath $projectsJson)) {
            throw "GitLab export did not create expected file: $projectsJson"
        }
    }

    Invoke-Step -Title "Mirror GitLab repositories to GitHub private repositories" -Quiet:$QuietOutput -Action {
        $migrateArgs = @(
            $GitHubMigrateScript,
            "--input", $projectsJson,
            "--github-owner", $target.Owner,
            "--owner-type", $target.Type,
            "--github-push-protocol", "https",
            "--mirror-dir", $mirrorDir,
            "--failed-record-file", $failureRecord,
            "--strip-large-files",
            "--max-blob-size-mb", "100",
            "--jobs", "$($config.jobs)"
        )

        if ($OnlyFailed) {
            $migrateArgs += "--only-failed"
        }

        if ($QuietOutput) {
            $migrateArgs += "--quiet-output"
        }

        python @migrateArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Migration failed with exit code $LASTEXITCODE."
        }
    }

    if (-not $QuietOutput) {
        Write-Host ""
        Write-Host "Sync completed." -ForegroundColor Green
    }
}
finally {
    Remove-Item Env:\GITLAB_URL -ErrorAction SilentlyContinue
    Remove-Item Env:\GITLAB_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:\GITHUB_TOKEN -ErrorAction SilentlyContinue
    Pop-Location
}
