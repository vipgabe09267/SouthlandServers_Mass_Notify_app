Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-NativeChecked {
    param([Parameter(Mandatory = $true)][string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable failed with exit code $LASTEXITCODE" }
}

function Remove-BuildDirectory {
    param([string]$Root, [string]$Name)
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $targetPath = [IO.Path]::GetFullPath((Join-Path $rootPath $Name))
    if ($targetPath -eq $rootPath -or -not $targetPath.StartsWith($rootPath + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean outside workspace: $targetPath"
    }
    if (Test-Path -LiteralPath $targetPath) {
        $running = @(Get-CimInstance Win32_Process -Property Name, ExecutablePath, ProcessId | Where-Object {
            $_.ExecutablePath -and $_.ExecutablePath.StartsWith($targetPath + '\', [StringComparison]::OrdinalIgnoreCase)
        })
        if ($running.Count) {
            throw "Close the running build output before rebuilding: $($running.ExecutablePath -join ', '). No files in $targetPath were removed."
        }
        $entry = Get-Item -LiteralPath $targetPath -Force
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Refusing reparse point: $targetPath" }
        $reparse = Get-ChildItem -LiteralPath $targetPath -Recurse -Force | Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 }
        if ($reparse) { throw "Build directory contains a reparse point: $targetPath" }
        Remove-Item -LiteralPath $targetPath -Recurse -Force
    }
}

function Assert-ReleaseConfiguration {
    param([switch]$Release, [string]$SignTool, [string]$CertificateThumbprint, [string]$TrustedSignerSha256, [string]$TimestampUrl)
    if (-not $Release) { return }
    if (-not [IO.Path]::IsPathRooted($SignTool) -or -not (Test-Path -LiteralPath $SignTool -PathType Leaf)) {
        throw 'Release requires an absolute Windows SDK signtool.exe path.'
    }
    if ($CertificateThumbprint -notmatch '^[A-Fa-f0-9]{40}$' -or $TrustedSignerSha256 -notmatch '^[A-Fa-f0-9]{64}$') {
        throw 'Release requires a signing certificate SHA-1 store thumbprint and independent SHA-256 certificate pin.'
    }
    if ($TimestampUrl -notmatch '^https://[^/]+') { throw 'Release requires an HTTPS RFC3161 timestamp endpoint.' }
}

function Sign-ReleaseExecutable {
    param([string]$Path, [string]$SignTool, [string]$CertificateThumbprint, [string]$TrustedSignerSha256, [string]$TimestampUrl)
    Invoke-NativeChecked -Executable $SignTool -Arguments @('sign', '/sha1', $CertificateThumbprint, '/fd', 'SHA256', '/tr', $TimestampUrl, '/td', 'SHA256', $Path)
    Invoke-NativeChecked -Executable $SignTool -Arguments @('verify', '/pa', '/all', $Path)
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate -or $null -eq $signature.TimeStamperCertificate) {
        throw "Executable lacks a valid timestamped signature: $Path"
    }
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { $pin = [BitConverter]::ToString($algorithm.ComputeHash($signature.SignerCertificate.RawData)).Replace('-', '') }
    finally { $algorithm.Dispose() }
    if ($pin -ne $TrustedSignerSha256.ToUpperInvariant()) { throw "Unexpected signing publisher: $Path" }
}
