param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

Write-Host "Building main application exe..." -ForegroundColor Green
& "$ProjectRoot\build.ps1" -Clean:$Clean

$AppExe = "$ProjectRoot\dist\SLS_Mass_Notify.exe"
if (-not (Test-Path $AppExe)) {
    throw "Failed to build SLS_Mass_Notify.exe"
}
Write-Host "Main exe built successfully." -ForegroundColor Green

$PythonBase = (& py -3.13 -c "import sys; print(sys.base_prefix)").Trim()
$env:TCL_LIBRARY = Join-Path $PythonBase "tcl\tcl8.6"
$env:TK_LIBRARY = Join-Path $PythonBase "tcl\tk8.6"

if (-not (Test-Path "$ProjectRoot\.venv")) {
    py -3.13 -m venv .venv
}

& "$ProjectRoot\.venv\Scripts\python.exe" -m pip install --upgrade pip
& "$ProjectRoot\.venv\Scripts\python.exe" -m pip install -r requirements-build.txt

Write-Host ""
Write-Host "Building Program Files installer exe..." -ForegroundColor Green
& "$ProjectRoot\.venv\Scripts\python.exe" -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --uac-admin `
    --name SLS_Mass_Notify_Installer `
    --icon "$ProjectRoot\favicon.ico" `
    --add-data "$AppExe;." `
    --add-data "$ProjectRoot\favicon.ico;." `
    "$ProjectRoot\sls_installer.py"

if ($LASTEXITCODE -ne 0) {
    throw "Installer PyInstaller build failed with exit code $LASTEXITCODE"
}

$InstallerExe = "$ProjectRoot\dist\SLS_Mass_Notify_Installer.exe"
if (-not (Test-Path $InstallerExe)) {
    throw "Installer output was not created: $InstallerExe"
}

Write-Host ""
Write-Host "Installer built successfully." -ForegroundColor Green
Write-Host "Installer location:" -ForegroundColor Cyan
Write-Host "  $InstallerExe" -ForegroundColor White
Write-Host ""
Write-Host "The installer requests Administrator permission, installs to Program Files,"
Write-Host "adds Start Menu shortcuts, registers uninstall, and opens Settings after install."
