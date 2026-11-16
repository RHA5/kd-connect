# ==============================================================================
# KD Connect Release Build Script
# ==============================================================================
# Steps:
# 1. Compile Tailwind CSS v3 and inline it into preview.html
# 2. Build single-file executable with PyInstaller (KD-Connect.exe)
# 3. Build setup package with Inno Setup (KD-Connect-Setup-<version>.exe)
# 4. Calculate SHA-256 checksums (sha256_exe, sha256_setup)
# 5. Copy release artifacts to saas-react-calculator/public/downloads/
# 6. Update version.json with actual hashes (strictly UTF-8 without BOM)
# ==============================================================================

param(
    [string]$Version = "v0.11.0",
    [string]$Suffix = ""
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$kdRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
$projectRoot = (Resolve-Path (Join-Path $kdRoot "..")).Path
$saasDownloads = Join-Path $projectRoot "saas-react-calculator\public\downloads"

$isccPath = "C:\Users\Dima\AppData\Local\Programs\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $isccPath)) {
    if (Test-Path "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") {
        $isccPath = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    } elseif (Test-Path "$env:ProgramFiles\Inno Setup 6\ISCC.exe") {
        $isccPath = "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    } else {
        throw "ISCC.exe not found! Please check Inno Setup 6 installation path."
    }
}

Set-Location $kdRoot

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  KD CONNECT RELEASE BUILD: $Version (suffix: $Suffix)" -ForegroundColor Cyan
Write-Host "  Working Directory: $kdRoot" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# --- STEP 1: Tailwind CSS v3 compilation & inlining ---
Write-Host "`n[1/6] Compiling Tailwind CSS v3..." -ForegroundColor Yellow
if (-not (Test-Path "dist")) {
    New-Item -ItemType Directory -Force -Path "dist" | Out-Null
}
npx -y tailwindcss@3 -i ./input.css -o ./dist/output.css --minify
if (-not (Test-Path "./dist/output.css")) {
    throw "Tailwind compilation failed: ./dist/output.css not produced!"
}

Write-Host "Inlining compiled CSS into preview.html..." -ForegroundColor Yellow
$css = Get-Content -Raw -Encoding utf8 "./dist/output.css"
$html = Get-Content -Raw -Encoding utf8 "./preview.html"

$pattern = '(?s)/\* TAILWIND_START \*/.*?/\* TAILWIND_END \*/'
$replacement = "/* TAILWIND_START */`n    /* TAILWIND V3 BUNDLE */`n    " + $css.Trim() + "`n    /* TAILWIND_END */"

if ($html -match $pattern) {
    $html = [regex]::Replace($html, $pattern, [System.Text.RegularExpressions.MatchEvaluator]{ return $replacement })
    [System.IO.File]::WriteAllText((Join-Path $kdRoot "preview.html"), $html, [System.Text.UTF8Encoding]::new($false))
    Write-Host "Tailwind CSS successfully inlined into preview.html." -ForegroundColor Green
} else {
    throw "TAILWIND_START/END markers not found in preview.html! Cannot safely inline compiled CSS."
}

# --- STEP 2: PyInstaller compilation ---
Write-Host "`n[2/6] Building PyInstaller executable..." -ForegroundColor Yellow
pyinstaller --noconfirm --clean "KD Connect.spec"

$exePath = Join-Path $kdRoot "dist\KD-Connect.exe"
if (-not (Test-Path $exePath)) {
    throw "PyInstaller build failed: $exePath not found!"
}
$exeSize = (Get-Item $exePath).Length
Write-Host "PyInstaller build successful: $exePath ($exeSize bytes)" -ForegroundColor Green

# --- STEP 3: Inno Setup compilation ---
Write-Host "`n[3/6] Building Inno Setup installer..." -ForegroundColor Yellow
& $isccPath "installer\KD-Connect.iss"

$versionRaw = $Version.TrimStart('v')
$setupName = "KD-Connect-Setup-v$versionRaw.exe"
$setupPath = Join-Path $kdRoot "installer\$setupName"
if (-not (Test-Path $setupPath)) {
    # Fallback search if name differs
    $fallbackSetup = Get-ChildItem -Path (Join-Path $kdRoot "installer") -Filter "KD-Connect-Setup-*.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($fallbackSetup) {
        $setupPath = $fallbackSetup.FullName
    } else {
        throw "Inno Setup compilation failed: installer exe not found in installer/!"
    }
}
$setupSize = (Get-Item $setupPath).Length
Write-Host "Inno Setup build successful: $setupPath ($setupSize bytes)" -ForegroundColor Green

# --- STEP 4: SHA-256 calculation ---
Write-Host "`n[4/6] Computing SHA-256 hashes..." -ForegroundColor Yellow
$sha256_exe = (Get-FileHash -Path $exePath -Algorithm SHA256).Hash.ToLower()
$sha256_setup = (Get-FileHash -Path $setupPath -Algorithm SHA256).Hash.ToLower()
Write-Host "  sha256_exe:   $sha256_exe" -ForegroundColor White
Write-Host "  sha256_setup: $sha256_setup" -ForegroundColor White

# --- STEP 5: Copy to web downloads ---
Write-Host "`n[5/6] Copying artifacts to saas-react-calculator/public/downloads/..." -ForegroundColor Yellow
if (-not (Test-Path $saasDownloads)) {
    New-Item -ItemType Directory -Force -Path $saasDownloads | Out-Null
}

# Copy installer
$dstSetupRc = Join-Path $saasDownloads "KD-Connect-Setup-$Version$Suffix.exe"
$dstSetupLatest = Join-Path $saasDownloads "KD-Connect-Setup.exe"
Copy-Item -Path $setupPath -Destination $dstSetupRc -Force
Copy-Item -Path $setupPath -Destination $dstSetupLatest -Force
Write-Host "  Installer copied -> KD-Connect-Setup-$Version$Suffix.exe" -ForegroundColor Green
Write-Host "  Installer copied -> KD-Connect-Setup.exe" -ForegroundColor Green

# Copy standalone exe
$dstExeRc = Join-Path $saasDownloads "KD-Connect-$Version$Suffix.exe"
$dstExeLatest = Join-Path $saasDownloads "KD-Connect.exe"
Copy-Item -Path $exePath -Destination $dstExeRc -Force
Copy-Item -Path $exePath -Destination $dstExeLatest -Force
Write-Host "  Executable copied -> KD-Connect-$Version$Suffix.exe" -ForegroundColor Green
Write-Host "  Executable copied -> KD-Connect.exe" -ForegroundColor Green

# --- STEP 6: Update version.json ---
Write-Host "`n[6/6] Updating version.json (UTF-8 without BOM)..." -ForegroundColor Yellow
$versionJsonPath = Join-Path $saasDownloads "version.json"
$versionObj = [ordered]@{
    version       = $Version
    download_url  = "/downloads/KD-Connect.exe"
    installer_url = "/downloads/KD-Connect-Setup.exe"
    sha256_setup  = $sha256_setup
    sha256_exe    = $sha256_exe
}
$versionJsonText = ($versionObj | ConvertTo-Json -Depth 4) + "`n"
[System.IO.File]::WriteAllText($versionJsonPath, $versionJsonText, [System.Text.UTF8Encoding]::new($false))
Write-Host "version.json updated successfully without BOM:" -ForegroundColor Green
Write-Host $versionJsonText -ForegroundColor Gray

Write-Host "==================================================" -ForegroundColor Green
Write-Host "  ALL BUILD STEPS COMPLETED SUCCESSFULLY!" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
