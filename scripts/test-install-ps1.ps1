# Tests install.ps1's logic (dot-sourced, functions only) with Windows PowerShell 5.1 or PowerShell 7:
# asset selection from a GitHub-like release (every OS's assets present, digest required, URL pinned to the
# repository, drafts refused) and the SHA-256 check. Usage: powershell -File scripts/test-install-ps1.ps1
$ErrorActionPreference = 'Stop'
Set-Variable -Name OptionLensInstallerNoRun -Value $true   # read by install.ps1: define its functions only
. (Join-Path $PSScriptRoot '..\install.ps1')

$script:failures = 0
function Assert-Equal($Name, $Actual, $Expected) {
    if ($Actual -ceq $Expected) { Write-Host "ok   $Name" } else { Write-Host "FAIL $Name : expected [$Expected], got [$Actual]"; $script:failures++ }
}
function Assert-Throws($Name, [scriptblock]$Block, [string]$Pattern) {
    try { & $Block; Write-Host "FAIL $Name : no error"; $script:failures++ }
    catch { if ($_.Exception.Message -match $Pattern) { Write-Host "ok   $Name" } else { Write-Host "FAIL $Name : $($_.Exception.Message)"; $script:failures++ } }
}

$hex = 'c' * 64
function Get-TestRelease([string]$Version, [bool]$Draft = $false, [string]$Digest = "sha256:$hex", [string]$Owner = $OptionLensRepo) {
    $names = @("$Version-arm64.zip", "$Version-x64.zip", "$Version-win-x64.zip", "$Version-win-x64-setup.exe", "$Version-linux-x64.AppImage")
    $json = @{
        tag_name = "v$Version"; draft = $Draft; prerelease = $false
        assets = @($names | ForEach-Object { @{ name = "OptionLens-$_"; digest = $(if ($_ -like '*setup.exe') { $Digest } else { "sha256:$('a' * 64)" })
            browser_download_url = "https://github.com/$Owner/releases/download/v$Version/OptionLens-$_" } })
    } | ConvertTo-Json -Depth 5
    $json | ConvertFrom-Json   # the shape Invoke-RestMethod returns
}

$a = Get-OptionLensSetupAsset (Get-TestRelease '0.1.4')
Assert-Equal 'picks the Windows installer' $a.Name 'OptionLens-0.1.4-win-x64-setup.exe'
Assert-Equal 'version' $a.Version '0.1.4'
Assert-Equal 'url' $a.Url "https://github.com/$OptionLensRepo/releases/download/v0.1.4/OptionLens-0.1.4-win-x64-setup.exe"
Assert-Equal 'sha256' $a.Sha256 $hex
Assert-Throws 'refuses a draft' { Get-OptionLensSetupAsset (Get-TestRelease '0.1.4' $true) } 'draft'
Assert-Throws 'refuses a missing digest' { Get-OptionLensSetupAsset (Get-TestRelease '0.1.4' $false '') } 'SHA-256 digest'
Assert-Throws 'refuses an upper-case or short digest' { Get-OptionLensSetupAsset (Get-TestRelease '0.1.4' $false "sha256:$('C' * 64)") } 'SHA-256 digest'
Assert-Throws 'refuses another repository''s URL' { Get-OptionLensSetupAsset (Get-TestRelease '0.1.4' $false "sha256:$hex" 'someone/else') } 'unexpected download URL'
Assert-Throws 'refuses a macOS-only release' { Get-OptionLensSetupAsset ('{"tag_name":"v0.1.3","draft":false,"prerelease":false,"assets":[{"name":"OptionLens-0.1.3-arm64.zip"}]}' | ConvertFrom-Json) } 'has no OptionLens-0.1.3-win-x64-setup.exe'
Assert-Throws 'refuses an odd version' { Get-OptionLensSetupAsset (Get-TestRelease '0.1.4-beta.1') } 'unexpected release version'

$file = Join-Path ([IO.Path]::GetTempPath()) ('ol-hash-' + [guid]::NewGuid().ToString('N'))
[IO.File]::WriteAllText($file, 'hello')
Assert-Equal 'sha256 matches' (Test-OptionLensSha256 $file '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824') $true
Assert-Equal 'sha256 mismatch' (Test-OptionLensSha256 $file $hex) $false
Remove-Item -LiteralPath $file

if ($script:failures -gt 0) { throw "$($script:failures) check(s) failed" }
Write-Host 'install.ps1: all checks passed'
