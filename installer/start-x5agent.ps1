# Starts the X5Agent console (if it is not already running) and opens it.
#   start-x5agent.ps1            -> start + open the browser
#   start-x5agent.ps1 -NoBrowser -> start only (used at Windows startup)
param(
    [switch]$NoBrowser,
    [int]$Port = 7777
)

$ErrorActionPreference = 'Stop'
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $AppDir 'runtime\python.exe'
$url = "http://127.0.0.1:$Port/"
# Keep the console on its bundled packages only.
$env:PYTHONNOUSERSITE = '1'
Add-Type -AssemblyName System.Windows.Forms

function Test-ConsolePort {
    param([int]$Port)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect('127.0.0.1', $Port)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

if (-not (Test-Path $python)) {
    [System.Windows.Forms.MessageBox]::Show("The bundled runtime is missing from:`n$AppDir", 'X5Agent') | Out-Null
    exit 1
}

if (-not (Test-ConsolePort -Port $Port)) {
    Start-Process -FilePath $python `
        -ArgumentList 'launch.pyc' `
        -WorkingDirectory $AppDir `
        -WindowStyle Hidden

    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline -and -not (Test-ConsolePort -Port $Port)) {
        Start-Sleep -Milliseconds 500
    }
}

if (-not (Test-ConsolePort -Port $Port)) {
    $log = Join-Path $AppDir 'console.log'
    [System.Windows.Forms.MessageBox]::Show(
        "X5Agent did not start on port $Port.`n`nLog file: $log",
        'X5Agent') | Out-Null
    exit 1
}

if (-not $NoBrowser) {
    Start-Process $url
}
