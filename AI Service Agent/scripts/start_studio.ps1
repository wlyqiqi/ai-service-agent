#Requires -Version 5.1
<#
.SYNOPSIS
  Quick-start LangGraph Studio for AI Service Agent.

.DESCRIPTION
  Defaults to git branch "main" (acceptance baseline). Use -Branch for feature
  work, or -SkipGit to keep the current checkout.

  Also fixes the common Windows failure mode: global `langgraph` CLI cannot
  import `ai_service_agent` unless VIRTUAL_ENV + PYTHONPATH=src are set.
  Clears process-level ARK_API_KEY so a stale User ark- token does not override .env.

.EXAMPLE
  .\scripts\start_studio.ps1
  .\scripts\start_studio.ps1 -Branch codex/model-node-harness
  .\scripts\start_studio.ps1 -SkipGit -Tunnel
#>
[CmdletBinding()]
param(
    [string]$Branch = "main",
    [switch]$NoPull,
    [switch]$SkipGit,
    [switch]$Tunnel,
    [switch]$OpenBrowser,
    [int]$Port = 2024
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
# Monorepo git root is parent of "AI Service Agent"
$GitRoot = (Resolve-Path (Join-Path $ProjectRoot "..")).Path

Set-Location $ProjectRoot

function Assert-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Command not found: $Name"
    }
}

Assert-Command langgraph
Assert-Command git

if (-not $SkipGit) {
    Set-Location $GitRoot
    $current = (git branch --show-current).Trim()
    if ($current -ne $Branch) {
        Write-Host "Checking out $Branch (was $current)..."
        git checkout $Branch
    }
    if (-not $NoPull) {
        Write-Host "Pulling origin/$Branch..."
        git pull --ff-only origin $Branch
    }
    Set-Location $ProjectRoot
    Write-Host "Git: $(git -C $GitRoot branch --show-current) @ $(git -C $GitRoot rev-parse --short HEAD)"
}
else {
    Write-Host "SkipGit: using $(git -C $GitRoot branch --show-current) @ $(git -C $GitRoot rev-parse --short HEAD)"
}

$venv = Join-Path $ProjectRoot ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Missing venv python: $venvPython"
}

# Drop process overrides that shadow .env (User-level ark- keys caused 401 before).
foreach ($k in @("ARK_API_KEY", "ARK_BASE_URL", "ARK_MODEL")) {
    Remove-Item "Env:$k" -ErrorAction SilentlyContinue
}
$env:NO_PROXY = "127.0.0.1,localhost"
$env:VIRTUAL_ENV = (Resolve-Path $venv).Path
$env:PATH = "$(Join-Path $env:VIRTUAL_ENV 'Scripts');$env:PATH"
$env:PYTHONPATH = (Resolve-Path (Join-Path $ProjectRoot "src")).Path

& $venvPython -c "import ai_service_agent; print('import_ok', ai_service_agent.__file__)"
if ($LASTEXITCODE -ne 0) {
    throw "ai_service_agent import failed under project venv"
}

$config = Join-Path $ProjectRoot "langgraph.studio.json"
if (-not (Test-Path $config)) {
    throw "Missing $config"
}

$langgraphArgs = @(
    "dev",
    "--config", $config,
    "--port", "$Port"
)
if (-not $OpenBrowser) {
    $langgraphArgs += "--no-browser"
}
if ($Tunnel) {
    $langgraphArgs += "--tunnel"
}

Write-Host ""
Write-Host "API:    http://127.0.0.1:$Port"
Write-Host "Studio: https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:$Port"
Write-Host "Config: langgraph.studio.json"
Write-Host ""

& langgraph @langgraphArgs
