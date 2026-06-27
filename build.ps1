param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

if ($Clean) {
    Remove-Item -LiteralPath "$ProjectRoot\build" -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath "$ProjectRoot\dist" -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath "$ProjectRoot\SLS_Mass_Notify.spec" -Force -ErrorAction SilentlyContinue
}

if (-not (Test-Path "$ProjectRoot\.venv")) {
    py -3.13 -m venv .venv
}

$PythonBase = (& py -3.13 -c "import sys; print(sys.base_prefix)").Trim()
$env:TCL_LIBRARY = Join-Path $PythonBase "tcl\tcl8.6"
$env:TK_LIBRARY = Join-Path $PythonBase "tcl\tk8.6"

& "$ProjectRoot\.venv\Scripts\python.exe" -m pip install --upgrade pip
& "$ProjectRoot\.venv\Scripts\python.exe" -m pip install -r requirements-build.txt

& "$ProjectRoot\.venv\Scripts\python.exe" -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name SLS_Mass_Notify `
    --icon "$ProjectRoot\favicon.ico" `
    --add-data "$ProjectRoot\icon.png;." `
    --add-data "$ProjectRoot\favicon.ico;." `
    --add-data "$ProjectRoot\audio;audio" `
    "$ProjectRoot\sls_mass_notify.py"

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Write-Host ""
Write-Host "Built: $ProjectRoot\dist\SLS_Mass_Notify.exe"
Write-Host "Use build-installer.ps1 to create the Program Files installer."
