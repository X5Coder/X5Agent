# Stops the X5Agent console started from this folder (used by the uninstaller).
param(
    [string]$AppDir = (Split-Path -Parent $MyInvocation.MyCommand.Path)
)

$ErrorActionPreference = 'SilentlyContinue'

Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object {
        $_.CommandLine -and
        $_.CommandLine -like '*launch.pyc*' -and
        $_.CommandLine -like "*$AppDir*"
    } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
