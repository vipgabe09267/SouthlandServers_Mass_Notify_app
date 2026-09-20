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
$buildArgs = @{ Clean=$Clean; Release=$Release; PythonExecutable=$PythonExecutable; SignTool=$SignTool; CertificateThumbprint=$CertificateThumbprint; TrustedSignerSha256=$TrustedSignerSha256; TimestampUrl=$TimestampUrl }
& (Join-Path $ProjectRoot 'build.ps1') @buildArgs
$buildPython = Join-Path $ProjectRoot '.venv-build\Scripts\python.exe'
$appDirectory = Join-Path $ProjectRoot 'dist\SLS_Mass_Notify'
Invoke-NativeChecked -Executable $buildPython -Arguments @('-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--windowed', '--noupx', '--distpath', 'build/installer-payload', '--name', 'SLS_Mass_Notify_Installer', '--icon', 'favicon.ico', '--version-file', 'version_info_installer.txt', '--add-data', "$appDirectory;SLS_Mass_Notify", '--add-data', 'favicon.ico;.', '--add-data', 'audio;audio', 'sls_installer.py')
$installerDirectory = Join-Path $ProjectRoot 'build\installer-payload\SLS_Mass_Notify_Installer'
$installerExe = Join-Path $installerDirectory 'SLS_Mass_Notify_Installer.exe'
if (-not (Test-Path -LiteralPath $installerExe -PathType Leaf)) { throw 'Installer output was not created.' }
if ($Release) {
    Sign-ReleaseExecutable -Path $installerExe -SignTool $SignTool -CertificateThumbprint $CertificateThumbprint -TrustedSignerSha256 $TrustedSignerSha256 -TimestampUrl $TimestampUrl
}
Invoke-NativeChecked -Executable $buildPython -Arguments @('tools/write_build_inventory.py', 'build/installer-payload/SLS_Mass_Notify_Installer')
Invoke-NativeChecked -Executable $buildPython -Arguments @('tools/build_setup_bootstrap.py')
$bootstrap = Join-Path $ProjectRoot 'dist\SLS_Mass_Notify_Installer.exe'
if ($Release) {
    Sign-ReleaseExecutable -Path $bootstrap -SignTool $SignTool -CertificateThumbprint $CertificateThumbprint -TrustedSignerSha256 $TrustedSignerSha256 -TimestampUrl $TimestampUrl
}
# Retire the old loose Python installer bundle; users launch the self-contained
# bootstrap now. This helper checks the workspace boundary and reparse points.
Remove-BuildDirectory -Root $ProjectRoot -Name 'dist\SLS_Mass_Notify_Installer'
Invoke-NativeChecked -Executable $buildPython -Arguments @('tools/verify_build_artifacts.py')
Invoke-NativeChecked -Executable $buildPython -Arguments @('tools/smoke_setup_runtime.py', '--full-payload')
$packageItems = @($bootstrap)
$packageDirectory = Join-Path $ProjectRoot 'dist\packages'
New-Item -ItemType Directory -Force -Path $packageDirectory | Out-Null
if ($Release) {
    # The bootstrap's signature covers its embedded runtime and application.
    $catalog = Join-Path $packageDirectory 'SLS_Mass_Notify_Installer.cat'
    New-FileCatalog -Path $bootstrap -CatalogFilePath $catalog -CatalogVersion 2.0 | Out-Null
    Sign-ReleaseExecutable -Path $catalog -SignTool $SignTool -CertificateThumbprint $CertificateThumbprint -TrustedSignerSha256 $TrustedSignerSha256 -TimestampUrl $TimestampUrl
    if ((Test-FileCatalog -Path $bootstrap -CatalogFilePath $catalog) -ne 'Valid') {
        throw 'Installer catalog verification failed.'
    }
    $packageItems += $catalog
}
$archive = Join-Path $packageDirectory 'SLS_Mass_Notify_Installer.zip'
Compress-Archive -LiteralPath $packageItems -DestinationPath $archive -Force
$digest = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText($archive + '.sha256', "$digest  SLS_Mass_Notify_Installer.zip`n")
# Retire only the old generated top-level packaging files, even without -Clean.
foreach ($oldName in @('README.txt', 'SLS_Mass_Notify_Installer.zip', 'SLS_Mass_Notify_Installer.zip.sha256', 'SLS_Mass_Notify_Installer.cat')) {
    $oldPath = Join-Path (Join-Path $ProjectRoot 'dist') $oldName
    if (Test-Path -LiteralPath $oldPath -PathType Leaf) { Remove-Item -LiteralPath $oldPath -Force }
}
if (-not $Release) { Write-Warning 'Development package is unsigned. Do not publish it or use it for production deployment.' }
Write-Host "Built installer package: $archive"
