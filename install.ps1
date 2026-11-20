$ErrorActionPreference = "Stop"
$Repo = "apolloskim/suplex"
$Asset = "suplex-windows-x64.exe"
$InstallDir = "$env:LOCALAPPDATA\suplex"

Write-Host "Installing suplex for Windows..."

$Release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest"
$Url = ($Release.assets | Where-Object { $_.name -eq $Asset }).browser_download_url

if (-not $Url) {
    Write-Error "Could not find $Asset in latest release."
    exit 1
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$OutFile = Join-Path $InstallDir "suplex.exe"
Write-Host "Downloading from $Url..."
Invoke-WebRequest -Uri $Url -OutFile $OutFile

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notlike "*$InstallDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$UserPath;$InstallDir", "User")
    Write-Host "Added $InstallDir to PATH."
}

Write-Host ""
Write-Host "suplex installed to $OutFile"
Write-Host ""
Write-Host "Restart your terminal, then run:"
Write-Host "  suplex setup"
Write-Host ""
