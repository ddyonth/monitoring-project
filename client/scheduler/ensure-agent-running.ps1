<#
.SYNOPSIS
  Сторож: если процесс client_agent не запущен — запустить client_agent.exe.
  Вызывается задачей MonitoringAgent-Watchdog (см. install-tasks.ps1),
  можно запустить и вручную.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$AgentPath
)

$ErrorActionPreference = "Stop"
$AgentPath = [System.IO.Path]::GetFullPath($AgentPath)
$ProcessName = [System.IO.Path]::GetFileNameWithoutExtension($AgentPath)

if (Get-Process -Name $ProcessName -ErrorAction SilentlyContinue) {
    exit 0
}
if (-not (Test-Path -LiteralPath $AgentPath)) {
    Write-Error "agent not found: $AgentPath"
    exit 1
}
Start-Process -FilePath $AgentPath -WorkingDirectory (Split-Path -Parent $AgentPath) -WindowStyle Hidden
exit 0
