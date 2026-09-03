<#
.SYNOPSIS
Safely move N.E.K.O's built-in Study Companion to a recoverable backup.

.EXAMPLE
pwsh -File .\tools\safe_clean_builtin_study_companion.ps1 -WhatIf

.EXAMPLE
pwsh -File .\tools\safe_clean_builtin_study_companion.ps1 -Confirm:$false
#>

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [string]$HostRoot,
    [string]$BackupRoot,
    [switch]$AllowDirty,
    [switch]$AllowRunningHost
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$comparison = [System.StringComparison]::OrdinalIgnoreCase

function Get-FullPath([string]$Path) {
    $providerPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    return [System.IO.Path]::GetFullPath($providerPath)
}

# Default layout: <workspace>/n.e.k.o_plugin_study_companion and <workspace>/N.E.K.O.
$pluginRepo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($HostRoot)) {
    $HostRoot = Join-Path (Split-Path -Parent $pluginRepo) "N.E.K.O"
}
$nekoRoot = (Resolve-Path -LiteralPath $HostRoot).Path
$pluginsRoot = Get-FullPath (Join-Path $nekoRoot "plugin\plugins")
$source = Get-FullPath (Join-Path $pluginsRoot "study_companion")
$pluginsPrefix = $pluginsRoot.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
if (-not $source.StartsWith($pluginsPrefix, $comparison)) {
    throw "Unexpected plugin path: $source"
}

if ([string]::IsNullOrWhiteSpace($BackupRoot)) {
    $BackupRoot = Join-Path (Split-Path -Parent $nekoRoot) "release-artifacts"
}
$backup = Get-FullPath $BackupRoot
$hostPrefix = $nekoRoot.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
if ($backup.Equals($nekoRoot, $comparison) -or $backup.StartsWith($hostPrefix, $comparison)) {
    throw "BackupRoot must be outside the N.E.K.O repository: $backup"
}

$state = if ($env:LOCALAPPDATA) {
    Get-FullPath (Join-Path $env:LOCALAPPDATA "N.E.K.O\plugins\study_companion")
}
else {
    ""
}

# Idempotent: an already-clean checkout is a successful no-op.
if (-not (Test-Path -LiteralPath $source)) {
    [pscustomobject]@{
        Status = "already_absent"
        BuiltinPluginPath = $source
        PersistentStatePreserved = [bool]($state -and (Test-Path -LiteralPath $state))
    }
    return
}
if (-not (Test-Path -LiteralPath $source -PathType Container)) {
    throw "Plugin path is not a directory: $source"
}
$sourceItem = Get-Item -LiteralPath $source -Force
if (($sourceItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "Refusing to move a symlink or reparse point: $source"
}

# Verify that the target is exactly the Study Companion plugin.
$manifest = Join-Path $source "plugin.toml"
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
    throw "plugin.toml is missing: $manifest"
}
$manifestText = Get-Content -LiteralPath $manifest -Raw
$pluginSection = [regex]::Match($manifestText, '(?ms)^\[plugin\]\s*(?<body>.*?)(?=^\[|\z)')
$idMatch = [regex]::Match($pluginSection.Groups["body"].Value, '(?m)^\s*id\s*=\s*"(?<v>[^"]+)"\s*$')
$versionMatch = [regex]::Match($pluginSection.Groups["body"].Value, '(?m)^\s*version\s*=\s*"(?<v>[^"]+)"\s*$')
if (-not $pluginSection.Success -or -not $idMatch.Success -or $idMatch.Groups["v"].Value -ne "study_companion") {
    throw "Plugin identity mismatch: $manifest"
}
if (-not $versionMatch.Success) {
    throw "Plugin version is missing: $manifest"
}
$version = $versionMatch.Groups["v"].Value

# Preserve uncommitted work unless the caller explicitly opts in.
if (-not (Test-Path -LiteralPath (Join-Path $nekoRoot ".git"))) {
    throw "HostRoot is not a Git checkout: $nekoRoot"
}
$changes = @(& git -C $nekoRoot status --porcelain=v1 -- "plugin/plugins/study_companion")
if ($LASTEXITCODE -ne 0) {
    throw "git status failed for $nekoRoot"
}
if ($changes.Count -gt 0 -and -not $AllowDirty) {
    throw "Plugin source has uncommitted changes. Review them or use -AllowDirty."
}

# Avoid replacing source files while the selected N.E.K.O checkout is active.
if (-not $AllowRunningHost) {
    try {
        $running = @(
            Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
                $exe = [string]$_.ExecutablePath
                $cmd = [string]$_.CommandLine
                $exe.StartsWith($hostPrefix, $comparison) -and
                    $cmd -match '(?i)(^|[\\/\s])launcher\.py([\s]|$)'
            }
        )
        if ($running.Count -gt 0) {
            throw "N.E.K.O is running from this checkout. Close it before cleaning."
        }
    }
    catch {
        if ($_.Exception.Message -like "N.E.K.O is running*") { throw }
        Write-Warning "Could not inspect running processes: $($_.Exception.Message)"
    }
}

$safeVersion = $version -replace '[^A-Za-z0-9._-]', '_'
$destination = Get-FullPath (Join-Path $backup (
    "study_companion_builtin_${safeVersion}_backup_$(Get-Date -Format 'yyyyMMdd-HHmmss')"
))
$backupPrefix = $backup.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
if (-not $destination.StartsWith($backupPrefix, $comparison) -or (Test-Path -LiteralPath $destination)) {
    throw "Unsafe or existing backup destination: $destination"
}

$action = "Move Study Companion $version to backup; persistent state is preserved"
if (-not $PSCmdlet.ShouldProcess($source, $action)) { return }

New-Item -ItemType Directory -Path $backup -Force | Out-Null
Move-Item -LiteralPath $source -Destination $destination
if ((Test-Path -LiteralPath $source) -or -not (Test-Path -LiteralPath (Join-Path $destination "plugin.toml"))) {
    throw "Cleanup verification failed. Restore the source from: $destination"
}

[pscustomobject]@{
    Status = "moved_to_backup"
    Version = $version
    BackupPath = $destination
    PersistentStatePath = $state
    PersistentStatePreserved = [bool]($state -and (Test-Path -LiteralPath $state))
    RestoreCommand = "Move-Item -LiteralPath '$destination' -Destination '$source'"
}
