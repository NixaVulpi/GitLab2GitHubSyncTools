param(
    [string]$Root = $PSScriptRoot,
    [switch]$IncludeCurrent,
    [switch]$UpdateSubmodulesToRemote
)

Set-StrictMode -Version Latest

$ErrorActionPreference = "Stop"

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoPath,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & git -C $RepoPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git was not found in PATH."
}

$rootPath = (Resolve-Path -LiteralPath $Root).Path
$repoDirs = @()

if ($IncludeCurrent) {
    $repoDirs += Get-Item -LiteralPath $rootPath
}

$repoDirs += Get-ChildItem -LiteralPath $rootPath -Directory -Force |
    Where-Object { $_.Name -notin @(".git") } |
    Sort-Object Name

$total = 0
$succeeded = 0
$failed = 0
$skipped = 0

foreach ($repo in $repoDirs) {
    $total++
    $gitPath = Join-Path $repo.FullName ".git"

    if (-not (Test-Path -LiteralPath $gitPath)) {
        Write-Host "SKIP $($repo.Name): not a Git repository" -ForegroundColor DarkYellow
        $skipped++
        continue
    }

    Write-Host ""
    Write-Host "==> Pulling $($repo.Name)" -ForegroundColor Cyan

    try {
        Invoke-Git -RepoPath $repo.FullName -Arguments @("fetch", "--all", "--prune", "--recurse-submodules=yes")
        Invoke-Git -RepoPath $repo.FullName -Arguments @("pull", "--recurse-submodules")
        Invoke-Git -RepoPath $repo.FullName -Arguments @("submodule", "sync", "--recursive")
        Invoke-Git -RepoPath $repo.FullName -Arguments @("submodule", "update", "--init", "--recursive")

        if ($UpdateSubmodulesToRemote) {
            Invoke-Git -RepoPath $repo.FullName -Arguments @("submodule", "update", "--init", "--recursive", "--remote")
        }

        Write-Host "OK   $($repo.Name)" -ForegroundColor Green
        $succeeded++
    }
    catch {
        Write-Host "FAIL $($repo.Name): $($_.Exception.Message)" -ForegroundColor Red
        $failed++
    }
}

Write-Host ""
Write-Host "Done. Total: $total, OK: $succeeded, Failed: $failed, Skipped: $skipped"

if ($failed -gt 0) {
    exit 1
}
