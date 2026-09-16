param(
    [string]$Python = "python",
    [string]$Cloudflared = ".amca\tools\cloudflared.exe"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$CloudflaredPath = Join-Path $ProjectRoot $Cloudflared
if (-not (Test-Path -LiteralPath $CloudflaredPath -PathType Leaf)) {
    throw "cloudflared was not found: $CloudflaredPath"
}

Push-Location $ProjectRoot
try {
    & $Python -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is not installed in the build environment."
    }
    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --name "AMCA-member" `
        --distpath "dist\bin" `
        --workpath "build\pyinstaller" `
        --specpath "build\pyinstaller" `
        --paths "src" `
        --add-binary "$CloudflaredPath;." `
        "packaging\member_entry.py"
    if ($LASTEXITCODE -ne 0) {
        throw "The AMCA member build failed."
    }
    Write-Host "Build complete: $ProjectRoot\dist\bin\AMCA-member.exe"
}
finally {
    Pop-Location
}
