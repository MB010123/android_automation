#Requires -Version 5.1
<#
.SYNOPSIS
  Install, start, stop, restart, status, and uninstall the background farm agent.

.EXAMPLE
  .\tools\manage_farm_agent.ps1 install
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("install", "start", "stop", "restart", "status", "uninstall")]
    [string] $Command
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TaskName = "MobiRentFarmAgent"
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$PythonW = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$Supervisor = Join-Path $ProjectRoot "tools\farm_agent_supervisor.py"
$StopFile = Join-Path $ProjectRoot "logs\farm_agent.stop"
$LogFile = Join-Path $ProjectRoot "logs\farm_agent.log"
$DefaultHost = "0.0.0.0"
$DefaultPort = 8790

function Read-ListenSettings {
    $hostName = $DefaultHost
    $port = $DefaultPort
    $envFile = Join-Path $ProjectRoot ".env"
    if (Test-Path -LiteralPath $envFile) {
        foreach ($line in Get-Content -LiteralPath $envFile) {
            $trim = $line.Trim()
            if (-not $trim -or $trim.StartsWith("#") -or -not $trim.Contains("=")) {
                continue
            }
            $name, $value = $trim.Split("=", 2)
            $name = $name.Trim()
            $value = $value.Trim().Trim('"').Trim("'")
            if ($name -eq "FARM_AGENT_LISTEN_HOST" -and $value) { $hostName = $value }
            if ($name -eq "FARM_AGENT_LISTEN_PORT" -and $value) { $port = [int]$value }
        }
    }
    if ($env:FARM_AGENT_LISTEN_HOST) { $hostName = $env:FARM_AGENT_LISTEN_HOST }
    if ($env:FARM_AGENT_LISTEN_PORT) { $port = [int]$env:FARM_AGENT_LISTEN_PORT }
    [pscustomobject]@{ Host = $hostName; Port = $port }
}

function Get-FarmAgentProcesses {
    param([switch] $All)
    $filtered = @(Get-FarmAgentProcessPairs)
    if ($All) { return $filtered }
    $filtered | Where-Object {
        $candidate = $_
        -not ($filtered | Where-Object { $_.ParentPid -eq $candidate.Pid -and $_.Kind -eq $candidate.Kind })
    }
}

function Get-FarmAgentProcessPairs {
    $serverNeedle = "farm_agent_status" + "_server.py"
    $supervisorNeedle = "farm_agent_super" + "visor.py"
    $launcherNeedle = "service_" + "launcher.py"
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -and (
            $_.CommandLine.Contains($serverNeedle) -or
            $_.CommandLine.Contains($supervisorNeedle) -or
            $_.CommandLine.Contains($launcherNeedle)
        )
    } | ForEach-Object {
        $kind = "other"
        if ($_.CommandLine.Contains($supervisorNeedle)) { $kind = "supervisor" }
        elseif ($_.CommandLine.Contains($serverNeedle)) { $kind = "server" }
        elseif ($_.CommandLine.Contains($launcherNeedle)) { $kind = "launcher" }
        [pscustomobject]@{ Pid = $_.ProcessId; ParentPid = $_.ParentProcessId; Kind = $kind }
    }
}

function Get-ListenPids {
    param([int] $Port)
    @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Assert-Paths {
    if (-not (Test-Path -LiteralPath $PythonW)) {
        throw "Missing venv pythonw: $PythonW"
    }
    if (-not (Test-Path -LiteralPath $Supervisor)) {
        throw "Missing supervisor: $Supervisor"
    }
}

function Get-Task {
    Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

function Start-SupervisorHidden {
    New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "logs") | Out-Null
    if (Test-Path -LiteralPath $StopFile) {
        Remove-Item -LiteralPath $StopFile -Force
    }
    $existing = @(Get-FarmAgentProcesses | Where-Object { $_.Kind -eq "supervisor" })
    if ($existing.Count -gt 0) {
        Write-Host "Supervisor already running (pid $($existing.Pid -join ','))."
        return
    }
    Start-Process -FilePath $PythonW -ArgumentList @("`"$Supervisor`"") -WorkingDirectory $ProjectRoot -WindowStyle Hidden
}

function Stop-FarmAgent {
    New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "logs") | Out-Null
    Set-Content -LiteralPath $StopFile -Value "stop" -Encoding ascii
    $task = Get-Task
    if ($task) {
        if ($task.State -eq "Running") {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        }
    }
    $deadline = (Get-Date).AddSeconds(12)
    do {
        $procs = @(Get-FarmAgentProcesses -All)
        if ($procs.Count -eq 0) { break }
        foreach ($kind in @("server", "launcher", "supervisor")) {
            foreach ($proc in @($procs | Where-Object { $_.Kind -eq $kind })) {
                Stop-Process -Id $proc.Pid -Force -ErrorAction SilentlyContinue
            }
        }
        Start-Sleep -Milliseconds 400
    } while ((Get-Date) -lt $deadline)
    $left = @(Get-FarmAgentProcesses)
    if ($left.Count -gt 0) {
        throw "Failed to stop farm-agent process(es): $($left.Pid -join ', ')"
    }
}

function Show-Status {
    $listen = Read-ListenSettings
    $task = Get-Task
    $procs = @(Get-FarmAgentProcesses)
    $listenPids = @(Get-ListenPids -Port $listen.Port)
    Write-Host "task=$TaskName"
    if ($task) {
        Write-Host "task_state=$($task.State)"
    } else {
        Write-Host "task_state=not_registered"
    }
    Write-Host "listen=$($listen.Host):$($listen.Port)"
    if ($listenPids.Count -gt 0) {
        Write-Host "port_listen=yes pids=$($listenPids -join ',')"
    } else {
        Write-Host "port_listen=no"
    }
    if ($procs.Count -eq 0) {
        Write-Host "processes=none"
    } else {
        foreach ($proc in $procs) {
            Write-Host ("process kind={0} pid={1}" -f $proc.Kind, $proc.Pid)
        }
    }
}

switch ($Command) {
    "install" {
        Assert-Paths
        $listen = Read-ListenSettings
        $argument = '"{0}"' -f $Supervisor
        $action = New-ScheduledTaskAction -Execute $PythonW -Argument $argument -WorkingDirectory $ProjectRoot
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -MultipleInstances IgnoreNew `
            -Hidden
        $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "MobiRent farm agent (hidden, auto-start at logon, restart on crash)" -Force | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 1
        if (-not (Get-FarmAgentProcesses | Where-Object { $_.Kind -eq "supervisor" })) {
            Start-SupervisorHidden
        }
        Write-Host "Installed scheduled task $TaskName (start at logon)."
        Write-Host "Started background supervisor for $($listen.Host):$($listen.Port)."
    }
    "start" {
        Assert-Paths
        $task = Get-Task
        if ($task) {
            if ($task.State -ne "Running") {
                Start-ScheduledTask -TaskName $TaskName
            }
            Start-Sleep -Seconds 1
        }
        if (-not (Get-FarmAgentProcesses | Where-Object { $_.Kind -eq "supervisor" })) {
            Start-SupervisorHidden
        }
        Write-Host "Start requested."
    }
    "stop" {
        Stop-FarmAgent
        Write-Host "Stopped."
    }
    "restart" {
        Assert-Paths
        Stop-FarmAgent
        Start-Sleep -Seconds 1
        $task = Get-Task
        if ($task) {
            Start-ScheduledTask -TaskName $TaskName
            Start-Sleep -Seconds 1
        }
        if (-not (Get-FarmAgentProcesses | Where-Object { $_.Kind -eq "supervisor" })) {
            Start-SupervisorHidden
        }
        Write-Host "Restarted."
    }
    "status" {
        Show-Status
    }
    "uninstall" {
        try { Stop-FarmAgent } catch { Write-Host "Stop during uninstall: $($_.Exception.Message)" }
        if (Get-Task) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Host "Removed scheduled task $TaskName."
        } else {
            Write-Host "Scheduled task $TaskName was not registered."
        }
        if (Test-Path -LiteralPath $StopFile) {
            Remove-Item -LiteralPath $StopFile -Force
        }
        Write-Host "Uninstalled."
    }
}
