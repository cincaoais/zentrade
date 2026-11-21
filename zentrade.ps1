# zentrade - start / stop / status for the backend and the forward dry-run daemon.
#
#   .\zentrade.ps1 start     start both in the background (no terminal window)
#   .\zentrade.ps1 stop      stop both
#   .\zentrade.ps1 status    show what is running, health and the forward record
#   .\zentrade.ps1 stop -Force   stop even while the forward daemon is mid-step
#
# Both run on this machine with backend\.venv, which is the environment the forward
# record's pins were taken in. The web UI is served by the backend at
# http://localhost:3018 from frontend\dist (rebuild with `pnpm build` after
# frontend changes). The forward daemon runs in dry-run mode only: it sends no
# orders. Logs go to data\logs\ (the previous run's logs are kept as *.prev.log).

param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'status')]
    [string]$Command = 'status',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$Root       = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir = Join-Path $Root 'backend'
$Python     = Join-Path $BackendDir '.venv\Scripts\python.exe'
$DataDir    = Join-Path $Root 'data'
$LogDir     = Join-Path $DataDir 'logs'
$ForwardDir = Join-Path $DataDir 'forward'
$Port       = 3018
$Url        = "http://127.0.0.1:$Port"

# How each component is recognised in the process list. The venv python.exe is a
# launcher that starts a second python.exe with the same command line, so every
# component normally shows two processes; both are stopped together.
$Components = @(
    @{
        Name    = 'backend'
        Args    = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$Port")
        Pattern = 'uvicorn\s+app\.main:app'
    },
    @{
        Name    = 'forward'
        Args    = @('scripts/forward/run.py', 'dry-run')
        Pattern = 'scripts[\\/]forward[\\/]run\.py\s+(dry-run|daemon)'
    }
)

function Get-ComponentProcesses($component) {
    @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $component.Pattern })
}

function Write-State($label, $ok, $text) {
    $color = if ($ok) { 'Green' } else { 'Yellow' }
    Write-Host ("{0,-9}" -f $label) -NoNewline
    Write-Host $text -ForegroundColor $color
}

function Get-LastAuditEvent {
    $audit = Join-Path $ForwardDir 'audit.jsonl'
    if (-not (Test-Path $audit)) { return $null }
    $line = Get-Content $audit -Tail 1 -Encoding UTF8
    if (-not $line) { return $null }
    return $line | ConvertFrom-Json
}

function Test-Health {
    try {
        $r = Invoke-WebRequest -Uri "$Url/health" -UseBasicParsing -TimeoutSec 3
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Start-Component($component) {
    $running = Get-ComponentProcesses $component
    if ($running.Count -gt 0) {
        Write-State $component.Name $true ("already running (PID " + (($running | ForEach-Object { $_.ProcessId }) -join ', ') + ")")
        return
    }
    New-Item -ItemType Directory -Force $LogDir | Out-Null
    $out = Join-Path $LogDir ($component.Name + '.out.log')
    $err = Join-Path $LogDir ($component.Name + '.err.log')
    foreach ($log in @($out, $err)) {
        if (Test-Path $log) {
            Move-Item -Force $log ($log -replace '\.log$', '.prev.log')
        }
    }
    $env:PYTHONUNBUFFERED = '1'
    $env:PYTHONUTF8 = '1'
    $p = Start-Process -FilePath $Python -ArgumentList $component.Args -WorkingDirectory $BackendDir `
        -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru
    Write-State $component.Name $true ("started (PID " + $p.Id + "), log: data\logs\" + $component.Name + ".err.log")
}

function Stop-Component($component) {
    $running = Get-ComponentProcesses $component
    if ($running.Count -eq 0) {
        Write-State $component.Name $true 'not running'
        return
    }
    foreach ($proc in $running) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
    $left = Get-ComponentProcesses $component
    if ($left.Count -gt 0) {
        Write-State $component.Name $false ("still running: PID " + (($left | ForEach-Object { $_.ProcessId }) -join ', '))
    } else {
        Write-State $component.Name $true ("stopped (PID " + (($running | ForEach-Object { $_.ProcessId }) -join ', ') + ")")
    }
}

if (-not (Test-Path $Python)) {
    Write-Host "backend\.venv is missing; run .\dev.ps1 once to install the backend." -ForegroundColor Red
    exit 1
}

$backend = $Components[0]
$forward = $Components[1]

switch ($Command) {
    'start' {
        $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
        if ($listener -and (Get-ComponentProcesses $backend).Count -eq 0) {
            Write-Host "port $Port is used by PID $($listener[0].OwningProcess), not by the zentrade backend; stop it first." -ForegroundColor Red
            exit 1
        }
        Start-Component $backend
        Start-Component $forward
        $deadline = (Get-Date).AddSeconds(90)
        while (-not (Test-Health) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
        if (Test-Health) {
            Write-Host "web UI: http://localhost:$Port" -ForegroundColor Cyan
        } else {
            Write-Host "backend did not answer $Url/health within 90 s; see data\logs\backend.err.log" -ForegroundColor Yellow
        }
    }
    'stop' {
        # A hard stop skips the daemon's own audit "stop" line; it recovers its lock on
        # the next start. Refuse while it is inside a step (e.g. placing orders).
        $last = Get-LastAuditEvent
        if ($last -and $last.event -eq 'step_start' -and -not $Force -and (Get-ComponentProcesses $forward).Count -gt 0) {
            Write-Host "forward daemon is mid-step ($($last.step) since $($last.ts) UTC); wait or use: .\zentrade.ps1 stop -Force" -ForegroundColor Yellow
            exit 1
        }
        Stop-Component $forward
        Stop-Component $backend
    }
    'status' {
        $b = Get-ComponentProcesses $backend
        if ($b.Count -gt 0) {
            $health = if (Test-Health) { 'healthy' } else { 'NOT answering /health' }
            Write-State 'backend' ($health -eq 'healthy') ("running (PID " + (($b | ForEach-Object { $_.ProcessId }) -join ', ') + "), $health, http://localhost:$Port")
        } else {
            Write-State 'backend' $false 'stopped'
        }
        $f = Get-ComponentProcesses $forward
        if ($f.Count -gt 0) {
            Write-State 'forward' $true ("running (PID " + (($f | ForEach-Object { $_.ProcessId }) -join ', ') + "), dry-run")
        } else {
            Write-State 'forward' $false 'stopped'
        }
        $reg = Join-Path $ForwardDir 'registration.json'
        if (Test-Path $reg) {
            $r = Get-Content $reg -Raw -Encoding UTF8 | ConvertFrom-Json
            Write-Host ("record   start {0}, account {1}" -f $r.start_date, $r.account_id)
        }
        $last = Get-LastAuditEvent
        if ($last) {
            $what = if ($last.step) { "$($last.event) $($last.step)" } else { $last.event }
            if ($last.error) { $what = "$what - $($last.error)" }
            Write-Host ("last     {0} UTC: {1}" -f $last.ts, $what)
        }
    }
}
