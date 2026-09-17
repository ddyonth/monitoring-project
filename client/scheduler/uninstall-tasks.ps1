<#
.SYNOPSIS
  Удаляет задачи MonitoringAgent-OnLogon и MonitoringAgent-Watchdog текущего
  пользователя. Сам агент не останавливает.
#>
[CmdletBinding()]
param()

foreach ($name in @("MonitoringAgent-OnLogon", "MonitoringAgent-Watchdog")) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "Removed task $name"
    } else {
        Write-Host "Task $name not found"
    }
}
