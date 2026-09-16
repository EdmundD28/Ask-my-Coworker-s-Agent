param(
    [string]$Python = "python",
    [string]$Cloudflared = ".amca\tools\cloudflared.exe",
    [string]$Destination = "dist\Ask My Coworker's Agent latest"
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$CloudflaredPath = Join-Path $ProjectRoot $Cloudflared
if (-not (Test-Path -LiteralPath $CloudflaredPath -PathType Leaf)) {
    throw "cloudflared was not found: $CloudflaredPath"
}
Push-Location $ProjectRoot
try {
    & $Python -m PyInstaller --noconfirm --clean --onefile `
        --name "Ask My Coworker's Agent" `
        --distpath $Destination `
        --workpath "build\setup-pyinstaller" --specpath "build\setup-pyinstaller" `
        --paths "src" --add-binary "$CloudflaredPath;." "packaging\setup_entry.py"
    if ($LASTEXITCODE -ne 0) { throw "Ask My Coworker's Agent build failed." }
}
finally { Pop-Location }
