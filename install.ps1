# OptionLens installer for Windows (x64): installs (or updates to) the latest release for the current user.
# No administrator rights. Windows PowerShell 5.1 or PowerShell 7:
#
#   irm https://raw.githubusercontent.com/davidl09/optionlens-mac/main/install.ps1 | iex
#
# Steps: find the latest release (GitHub API), download OptionLens-<version>-win-x64-setup.exe over HTTPS,
# check its SHA-256 (Get-FileHash) against the digest GitHub publishes for it (no digest, no install), keep
# a copy in %APPDATA%\OptionLens\backups (the in-app updater can roll back to it later), run it silently
# (per user: %LOCALAPPDATA%\Programs, Start-menu and desktop shortcuts) and start OptionLens. A file this
# script downloads has no Mark-of-the-Web, so SmartScreen does not prompt for it.
# The script only defines functions until its last lines, so a truncated download runs nothing.

$OptionLensRepo = 'davidl09/optionlens-mac'

function Write-OptionLensStep([string]$Message) {
    Write-Host "==> $Message"
}

# The installer asset of a GitHub release object (Invoke-RestMethod output): version, name, URL, SHA-256.
function Get-OptionLensSetupAsset($Release, [string]$Repo = $OptionLensRepo) {
    if ($Release.draft -or $Release.prerelease) { throw 'the latest release is a draft or a prerelease.' }
    $version = ([string]$Release.tag_name) -replace '^v', ''
    if ($version -notmatch '^\d+\.\d+\.\d+$') { throw "unexpected release version: $version" }
    $name = "OptionLens-$version-win-x64-setup.exe"
    $asset = @($Release.assets | Where-Object { $_.name -ceq $name })
    if ($asset.Count -ne 1) { throw "the latest release ($version) has no $name." }
    $url = [string]$asset[0].browser_download_url
    if ($url -cne "https://github.com/$Repo/releases/download/v$version/$name") { throw "unexpected download URL: $url" }
    $digest = [string]$asset[0].digest
    if ($digest -cnotmatch '^sha256:[0-9a-f]{64}$') { throw "the release does not publish a SHA-256 digest for $name; refusing to install it." }
    [pscustomobject]@{ Version = $version; Name = $name; Url = $url; Sha256 = $digest.Substring(7) }
}

# Whether the file's SHA-256 is the expected lowercase hex digest.
function Test-OptionLensSha256([string]$Path, [string]$Expected) {
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -ceq $Expected
}

function Install-OptionLens {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'   # Windows PowerShell's progress bar slows Invoke-WebRequest down a lot
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'OptionLens needs 64-bit Windows.' }
    if (Get-Process -Name OptionLens -ErrorAction SilentlyContinue) {
        throw 'OptionLens is running. Quit it first (right-click its tray icon, Quit OptionLens), then run the installer again.'
    }
    # Windows PowerShell 5.1 may not offer TLS 1.2 by default; GitHub requires it.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    Write-OptionLensStep 'Looking up the latest OptionLens release'
    $headers = @{ Accept = 'application/vnd.github+json'; 'User-Agent' = 'OptionLens-installer' }
    try {
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$OptionLensRepo/releases/latest" -Headers $headers -UseBasicParsing
    } catch {
        throw "could not reach the GitHub API (offline, or rate limited: try again in an hour). $($_.Exception.Message)"
    }
    $asset = Get-OptionLensSetupAsset $release

    $work = Join-Path ([IO.Path]::GetTempPath()) ('optionlens-install-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $work | Out-Null
    try {
        $setup = Join-Path $work $asset.Name
        Write-OptionLensStep "Downloading OptionLens $($asset.Version) (Windows x64)"
        Invoke-WebRequest -Uri $asset.Url -OutFile $setup -UseBasicParsing -Headers @{ 'User-Agent' = 'OptionLens-installer' }
        if (-not (Test-OptionLensSha256 $setup $asset.Sha256)) {
            throw "the download's SHA-256 does not match the release ($($asset.Sha256)); refusing to install it."
        }
        Write-OptionLensStep 'Checksum verified'
        Unblock-File -LiteralPath $setup

        $backups = Join-Path $env:APPDATA 'OptionLens\backups'
        New-Item -ItemType Directory -Force -Path $backups | Out-Null
        Copy-Item -LiteralPath $setup -Destination (Join-Path $backups $asset.Name) -Force

        Write-OptionLensStep "Installing OptionLens $($asset.Version) for $env:USERNAME"
        # /S: silent, per user. --force-run: the installer starts OptionLens when it is done.
        $process = Start-Process -FilePath $setup -ArgumentList '/S', '--force-run' -PassThru
        $null = $process.Handle   # keeps the exit code readable after the process ends
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) { throw "the installer failed (exit code $($process.ExitCode))." }
        Write-OptionLensStep "Installed OptionLens $($asset.Version). It checks for updates itself from now on (Settings, Updates)."
    } finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# Dot-sourced by scripts/test-install-ps1.ps1 with $OptionLensInstallerNoRun set: functions only.
if (-not (Get-Variable -Name OptionLensInstallerNoRun -ValueOnly -ErrorAction SilentlyContinue)) {
    try { Install-OptionLens } catch { Write-Host "OptionLens install failed: $($_.Exception.Message)" -ForegroundColor Red }
}
