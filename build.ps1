param(
    [switch]$Clean,
    [switch]$Release,
    [string]$PythonExecutable = '',
    [string]$SignTool = '',
    [string]$CertificateThumbprint = '',
    [string]$TrustedSignerSha256 = '',
    [string]$TimestampUrl = ''
)

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ProjectRoot 'build-common.ps1')
Set-Location -LiteralPath $ProjectRoot
Assert-ReleaseConfiguration -Release:$Release -SignTool $SignTool -CertificateThumbprint $CertificateThumbprint -TrustedSignerSha256 $TrustedSignerSha256 -TimestampUrl $TimestampUrl
if (-not $PythonExecutable) {
    $launcher = (Get-Command py.exe -ErrorAction Stop).Source
    $PythonExecutable = (& $launcher -3.14 -c 'import sys; print(sys.executable)')
    if ($LASTEXITCODE -ne 0) { throw 'CPython 3.14.7 is required; install it explicitly before building.' }
    $PythonExecutable = $PythonExecutable.Trim()
}
if (-not [IO.Path]::IsPathRooted($PythonExecutable)) { throw 'PythonExecutable must be an absolute path.' }
$runtimeArguments = @('tools/check_build_runtime.py')
if ($Release) { $runtimeArguments += '--release' }
Invoke-NativeChecked -Executable $PythonExecutable -Arguments $runtimeArguments
$env:PIP_CACHE_DIR = Join-Path $ProjectRoot '.tools\pip-cache'
$env:PYINSTALLER_CONFIG_DIR = Join-Path $ProjectRoot '.tools\pyinstaller-cache'
$env:TEMP = Join-Path $ProjectRoot '.tools\tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
if ($Clean) {
    Remove-BuildDirectory -Root $ProjectRoot -Name 'build'
    Remove-BuildDirectory -Root $ProjectRoot -Name 'dist'
}
# Recreate the build environment so stale installed packages cannot leak in.
Remove-BuildDirectory -Root $ProjectRoot -Name '.venv-build'
Invoke-NativeChecked -Executable $PythonExecutable -Arguments @('-m', 'venv', '.venv-build')
$buildPython = Join-Path $ProjectRoot '.venv-build\Scripts\python.exe'
Invoke-NativeChecked -Executable $buildPython -Arguments @('-m', 'pip', 'install', '--require-hashes', '--only-binary=:all:', '-r', 'requirements-build.txt')
Invoke-NativeChecked -Executable $buildPython -Arguments @('-m', 'pip', 'check')
Invoke-NativeChecked -Executable $buildPython -Arguments @('-m', 'unittest', 'discover', '-v')
Invoke-NativeChecked -Executable $buildPython -Arguments @('tools/smoke_setup_runtime.py')
Invoke-NativeChecked -Executable $buildPython -Arguments @('tools/smoke_window_layout.py')
Invoke-NativeChecked -Executable $buildPython -Arguments @('tools/smoke_notification_delivery.py')
Invoke-NativeChecked -Executable $buildPython -Arguments @('tools/smoke_announcement_layout.py')
Invoke-NativeChecked -Executable $buildPython -Arguments @('tools/write_version_info.py')
Invoke-NativeChecked -Executable $buildPython -Arguments @('-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--windowed', '--noupx', '--name', 'SLS_Mass_Notify', '--icon', 'favicon.ico', '--version-file', 'version_info_app.txt', '--add-data', 'icon.png;.', '--add-data', 'favicon.ico;.', '--add-data', 'audio;audio', 'sls_mass_notify.py')
$AppExe = Join-Path $ProjectRoot 'dist\SLS_Mass_Notify\SLS_Mass_Notify.exe'
if (-not (Test-Path -LiteralPath $AppExe -PathType Leaf)) { throw 'Application output was not created.' }
if ($Release) {
    Sign-ReleaseExecutable -Path $AppExe -SignTool $SignTool -CertificateThumbprint $CertificateThumbprint -TrustedSignerSha256 $TrustedSignerSha256 -TimestampUrl $TimestampUrl
}
Invoke-NativeChecked -Executable $buildPython -Arguments @('tools/write_build_inventory.py', 'dist/SLS_Mass_Notify')
Write-Host "Built onedir application: $AppExe"
