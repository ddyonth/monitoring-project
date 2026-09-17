<#
.SYNOPSIS
  Регистрирует в Планировщике заданий Windows две задачи для агента мониторинга
  от имени ТЕКУЩЕГО пользователя, без прав администратора и без диалогов.

  A) MonitoringAgent-OnLogon  — запуск client_agent.exe при входе этого пользователя.
  B) MonitoringAgent-Watchdog — каждые 5 минут: если процесс client_agent не
     запущен, запустить его (подстраховка; основной перезапуск после
     обновления делает сам агент, см. check_and_apply_update).

.PARAMETER AgentPath
  Полный путь к client_agent.exe. По умолчанию — client_agent.exe рядом с этим скриптом.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File install-tasks.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File install-tasks.ps1 -AgentPath "C:\monitoring\client_agent.exe"
#>
[CmdletBinding()]
param(
    [string]$AgentPath = (Join-Path $PSScriptRoot "client_agent.exe"),
    [int]$WatchdogIntervalMinutes = 5
)

$ErrorActionPreference = "Stop"

$AgentPath = [System.IO.Path]::GetFullPath($AgentPath)
if (-not (Test-Path -LiteralPath $AgentPath)) {
    throw "client_agent.exe not found: $AgentPath"
}
$AgentDir = Split-Path -Parent $AgentPath
$WatchdogScript = Join-Path $PSScriptRoot "ensure-agent-running.ps1"
if (-not (Test-Path -LiteralPath $WatchdogScript)) {
    throw "ensure-agent-running.ps1 not found next to this script: $WatchdogScript"
}

# Задачи регистрируются под текущим пользователем, интерактивный вход,
# без повышения прав (RunLevel Limited) — администратор не нужен.
$UserId = "$env:USERDOMAIN\$env:USERNAME"
$Principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

# --- A: при входе пользователя -------------------------------------------
# WorkingDirectory = каталог exe: агент пишет activity.db относительно cwd.
$ActionA = New-ScheduledTaskAction -Execute $AgentPath -WorkingDirectory $AgentDir
$TriggerA = New-ScheduledTaskTrigger -AtLogOn -User $UserId
Register-ScheduledTask -TaskName "MonitoringAgent-OnLogon" `
    -Action $ActionA -Trigger $TriggerA -Principal $Principal -Settings $Settings `
    -Description "Monitoring agent: start at user logon" -Force | Out-Null
Write-Host "Registered task MonitoringAgent-OnLogon ($AgentPath)"

# --- B: сторож раз в N минут ----------------------------------------------
$WatchdogArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$WatchdogScript`" -AgentPath `"$AgentPath`""
$ActionB = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $WatchdogArgs -WorkingDirectory $AgentDir
$TriggerB = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $WatchdogIntervalMinutes)
Register-ScheduledTask -TaskName "MonitoringAgent-Watchdog" `
    -Action $ActionB -Trigger $TriggerB -Principal $Principal -Settings $Settings `
    -Description "Monitoring agent: start if not running (every $WatchdogIntervalMinutes min)" -Force | Out-Null
Write-Host "Registered task MonitoringAgent-Watchdog (every $WatchdogIntervalMinutes min)"

Write-Host "Done. Check: Get-ScheduledTask -TaskName 'MonitoringAgent-*' | Format-Table TaskName, State"
