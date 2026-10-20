#Requires -Version 5.1
<#
.SYNOPSIS
  Quick-start LangGraph Studio for AI Service Agent.

.DESCRIPTION
  Defaults to git branch "main" (acceptance baseline). Use -Branch for feature
  work, or -SkipGit to keep the current checkout. A dirty worktree refuses
  checkout/pull — pass -SkipGit instead.

  Always launches `.venv\Scripts\langgraph.exe` (not a global CLI). Sets
  VIRTUAL_ENV + PYTHONPATH=src, clears stale process-level ARK_* keys, and
  sets NO_PROXY for local Studio.

  -Tracing writes a temp dotenv overlay (LANGSMITH_TRACING=true) and a temp
  langgraph config whose `env` points at that overlay — required because
  langgraph-api patch_environment applies the config env file over process
  env. The real `.env` is not modified. -WithApp also starts FastAPI on :8000.

.EXAMPLE
  .\scripts\start_studio.ps1
  .\scripts\start_studio.ps1 -Branch codex/model-node-harness
  .\scripts\start_studio.ps1 -SkipGit -Tracing -OpenBrowser
  .\scripts\start_studio.ps1 -SkipGit -WithApp
#>
[CmdletBinding()]
param(
    [string]$Branch = "main",
    [switch]$NoPull,
    [switch]$SkipGit,
    [switch]$Tunnel,
    [switch]$OpenBrowser,
    [switch]$Tracing,
    [switch]$WithApp,
    [int]$Port = 2024
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
# Monorepo git root is parent of "AI Service Agent"
$GitRoot = (Resolve-Path (Join-Path $ProjectRoot "..")).Path
$overlayDir = $null

Set-Location $ProjectRoot

function Assert-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Command not found: $Name"
    }
}

function Test-GitWorktreeDirty([string]$RepoRoot) {
    $porcelain = @(git -C $RepoRoot status --porcelain)
    return ($porcelain.Count -gt 0)
}

function New-StudioTracingOverlay {
    param(
        [string]$ProjectRoot,
        [string]$BaseConfigPath
    )
    $dotenvPath = Join-Path $ProjectRoot ".env"
    if (-not (Test-Path $dotenvPath)) {
        throw "Missing $dotenvPath (required for -Tracing overlay)"
    }
    if (-not (Test-Path $BaseConfigPath)) {
        throw "Missing $BaseConfigPath"
    }

    $dir = Join-Path $env:TEMP ("lg-studio-trace-" + [guid]::NewGuid().ToString("n"))
    New-Item -ItemType Directory -Path $dir | Out-Null
    $overlayEnv = Join-Path $dir "studio.trace.env"
    $overlayConfig = Join-Path $dir "langgraph.studio.trace.json"

    # Preserve BOM / encoding; only force LANGSMITH_TRACING=true for this run.
    $raw = [System.IO.File]::ReadAllText($dotenvPath)
    if ($raw -match '(?m)^\s*LANGSMITH_TRACING\s*=') {
        $raw = [regex]::Replace($raw, '(?m)^\s*LANGSMITH_TRACING\s*=.*$', 'LANGSMITH_TRACING=true')
    }
    else {
        if ($raw.Length -gt 0 -and -not $raw.EndsWith("`n")) {
            $raw += "`n"
        }
        $raw += "LANGSMITH_TRACING=true`n"
    }
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($overlayEnv, $raw, $utf8)

    $cfg = Get-Content -LiteralPath $BaseConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    # Absolute path: langgraph-api DotEnv loads this after config parse.
    $cfg.env = $overlayEnv
    $json = $cfg | ConvertTo-Json -Depth 20
    [System.IO.File]::WriteAllText($overlayConfig, $json, $utf8)

    return @{
        Dir    = $dir
        Config = $overlayConfig
        Env    = $overlayEnv
    }
}

try {
    Assert-Command git

    if (-not $SkipGit) {
        if (Test-GitWorktreeDirty $GitRoot) {
            throw "Git worktree is dirty under $GitRoot. " +
                  "Refusing checkout/pull of '$Branch'. " +
                  "Commit/stash first, or re-run with -SkipGit to keep the current checkout."
        }
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
    $langgraphExe = Join-Path $venv "Scripts\langgraph.exe"
    if (-not (Test-Path $venvPython)) {
        throw "Missing venv python: $venvPython"
    }
    if (-not (Test-Path $langgraphExe)) {
        throw "Missing $langgraphExe — run: uv sync --group dev"
    }

    # A stale `langgraph dev` from a previous session makes the new one fail with a
    # bare bind error. Fail fast with the owning PID instead.
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($listener) {
        $ownerPid = $listener.OwningProcess
        $owner = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
        $ownerName = "unknown"
        if ($owner) { $ownerName = $owner.ProcessName }
        throw "Port $Port is already in use by PID $ownerPid ($ownerName). " +
              "Stop it first: Stop-Process -Id $ownerPid   (or use -Port <other>)"
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

    $baseConfig = Join-Path $ProjectRoot "langgraph.studio.json"
    if (-not (Test-Path $baseConfig)) {
        throw "Missing $baseConfig"
    }

    $config = $baseConfig
    $configLabel = "langgraph.studio.json"
    if ($Tracing) {
        $overlay = New-StudioTracingOverlay -ProjectRoot $ProjectRoot -BaseConfigPath $baseConfig
        $overlayDir = $overlay.Dir
        $config = $overlay.Config
        $configLabel = "temp overlay (LANGSMITH_TRACING=true; .env untouched)"
        Write-Host "Tracing: dotenv overlay at $($overlay.Env)"
    }

    if ($WithApp) {
        $appExe = Join-Path $venv "Scripts\ai-service-agent.exe"
        if (-not (Test-Path $appExe)) {
            throw "Missing $appExe"
        }
        Write-Host "Starting FastAPI runtime on :8000 ..."
        Start-Process -FilePath $appExe -WorkingDirectory $ProjectRoot
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
    Write-Host "CLI:     $langgraphExe"
    Write-Host "API:     http://127.0.0.1:$Port"
    Write-Host "Health:  curl --noproxy `"*`" http://127.0.0.1:$Port/ok"
    Write-Host "Studio:  https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:$Port"
    Write-Host "Config:  $configLabel"
    Write-Host ""
    Write-Host "If Studio cannot connect: add 127.0.0.1;localhost to the system proxy"
    Write-Host "bypass list (this machine sets HTTP_PROXY/HTTPS_PROXY), and open the URL"
    Write-Host "in a normal browser rather than an embedded preview panel."
    Write-Host ""

    & $langgraphExe @langgraphArgs
    exit $LASTEXITCODE
}
finally {
    if ($overlayDir -and (Test-Path $overlayDir)) {
        Remove-Item -LiteralPath $overlayDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
