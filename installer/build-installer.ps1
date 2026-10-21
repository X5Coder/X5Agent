# Builds x5agent-setup-<version>.exe from a compiled Windows release.
#
#   .\installer\build-installer.ps1
#   .\installer\build-installer.ps1 -ReleaseZip dist\x5agent-windows-amd64-full.zip
#
# Steps: extract the release into installer\payload, download the matching
# CPython embeddable runtime into installer\runtime, then run Inno Setup's
# compiler (ISCC.exe). Requires Inno Setup 6 installed on this machine.
param(
    [string]$ReleaseZip = '',
    [string]$PythonVersion = '3.12.10',
    [string]$Iscc = ''
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here

if (-not $ReleaseZip) {
    $ReleaseZip = Join-Path $root 'dist\x5agent-windows-amd64-full.zip'
}
if (-not (Test-Path $ReleaseZip)) {
    throw "release zip not found: $ReleaseZip (run project\scripts\build_release.py first)"
}

$version = (Get-Content (Join-Path $root 'updates.json') -Raw | ConvertFrom-Json).version
Write-Host "  version $version" -ForegroundColor Cyan

$payload = Join-Path $here 'payload'
if (Test-Path $payload) { Remove-Item -Recurse -Force $payload }
New-Item -ItemType Directory -Path $payload | Out-Null

# The archive holds one top folder (x5agent-<platform>-<flavor>); flatten it.
$temp = Join-Path $env:TEMP ("x5payload-" + [guid]::NewGuid().ToString('N'))
Expand-Archive -LiteralPath $ReleaseZip -DestinationPath $temp -Force
$inner = Get-ChildItem $temp -Directory | Select-Object -First 1
if (-not $inner) { throw 'the release zip has no top folder' }
Copy-Item -Path (Join-Path $inner.FullName '*') -Destination $payload -Recurse -Force
Remove-Item -Recurse -Force $temp
$pythonFiles = @(Get-ChildItem $payload -Recurse -Filter *.py -File)
if ($pythonFiles.Count -gt 0) {
    throw "refusing to package source files: $($pythonFiles[0].FullName)"
}
if (-not (Test-Path (Join-Path $payload 'launch.pyc'))) {
    throw 'launch.pyc is missing from the release payload'
}
Write-Host '  payload ready (0 .py)' -ForegroundColor Green

$runtime = Join-Path $here 'runtime'
if (-not (Test-Path (Join-Path $runtime 'python.exe'))) {
    if (Test-Path $runtime) { Remove-Item -Recurse -Force $runtime }
    $embed = Join-Path $env:TEMP "python-$PythonVersion-embed-amd64.zip"
    if (-not (Test-Path $embed)) {
        $url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
        Write-Host "  downloading $url" -ForegroundColor Cyan
        Invoke-WebRequest -Uri $url -OutFile $embed -UseBasicParsing
    }
    Expand-Archive -LiteralPath $embed -DestinationPath $runtime -Force
}
if (-not (Test-Path (Join-Path $runtime 'python.exe'))) {
    throw 'the embedded Python runtime was not extracted'
}
Write-Host '  runtime ready' -ForegroundColor Green

if (-not $Iscc) {
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
    )
    $Iscc = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
if (-not $Iscc -or -not (Test-Path $Iscc)) {
    throw 'ISCC.exe not found: install Inno Setup 6 (https://jrsoftware.org/isdl.php) or pass -Iscc'
}

& $Iscc "/DAppVersion=$version" (Join-Path $here 'x5agent.iss')
if ($LASTEXITCODE -ne 0) { throw "ISCC failed with exit code $LASTEXITCODE" }

$out = Get-ChildItem (Join-Path $here 'out') -Filter '*.exe' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Write-Host "  built $($out.FullName) ($([math]::Round($out.Length / 1MB, 1)) MB)" -ForegroundColor Green
