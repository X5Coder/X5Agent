# Installs X5Agent's Python dependencies into the bundled interpreter.
# Runs once, from the installer, with a visible console so pip's progress is
# honest. Safe to run again: pip skips what is already satisfied.
param(
    [Parameter(Mandatory = $true)][string]$AppDir
)

# Native tools (python/pip) write progress and warnings to stderr; with 'Stop'
# PowerShell would turn those into terminating errors, so failures are checked
# through $LASTEXITCODE and thrown deliberately instead.
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
# A bundled runtime must never see the machine's global site-packages.
$env:PYTHONNOUSERSITE = '1'

$python = Join-Path $AppDir 'runtime\python.exe'
if (-not (Test-Path $python)) {
    throw "bundled interpreter not found at $python"
}

Write-Host ''
Write-Host '  X5Agent - preparing the bundled Python runtime' -ForegroundColor Cyan
Write-Host ''

# The embeddable distribution ignores site-packages until its ._pth says so.
$pth = Get-ChildItem -Path (Join-Path $AppDir 'runtime') -Filter 'python3*._pth' -File -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($pth) {
    $lines = @(Get-Content -LiteralPath $pth.FullName)
    if ($lines -notcontains 'import site') { $lines += 'import site' }
    if ($lines -notcontains 'Lib\site-packages') { $lines += 'Lib\site-packages' }
    Set-Content -LiteralPath $pth.FullName -Value $lines -Encoding ASCII
}
$sitePackages = Join-Path $AppDir 'runtime\Lib\site-packages'
New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null

& $python -c "import sys; print('  python', sys.version.split()[0])"
if ($LASTEXITCODE -ne 0) { throw 'the bundled interpreter does not start' }

& $python -m pip --version 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host '  bootstrapping pip ...' -ForegroundColor Cyan
    $getPip = Join-Path $env:TEMP 'x5-get-pip.py'
    Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getPip -UseBasicParsing
    & $python $getPip --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw 'pip could not be installed' }
}

$requirements = Join-Path $AppDir 'requirements.txt'
if (-not (Test-Path $requirements)) {
    throw "requirements.txt is missing from $AppDir"
}

Write-Host ''
Write-Host '  Installing dependencies (several minutes, ~2 GB on disk)' -ForegroundColor Cyan
Write-Host ''

$installed = $false
for ($attempt = 1; $attempt -le 3; $attempt++) {
    & $python -m pip install --no-warn-script-location --disable-pip-version-check -r $requirements
    if ($LASTEXITCODE -eq 0) { $installed = $true; break }
    Write-Host "  attempt $attempt failed - retrying" -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}
if (-not $installed) {
    throw 'dependency installation failed after 3 attempts (check the network and run the installer again)'
}

Set-Content -LiteralPath (Join-Path $AppDir 'installed.txt') -Value (Get-Date -Format o) -Encoding ASCII
Write-Host ''
Write-Host '  Dependencies ready.' -ForegroundColor Green
Write-Host ''
