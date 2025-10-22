# Set working directory to script location
Set-Location -Path $PSScriptRoot

# Step 0: Ensure backup folder exists
$backupDir = "backup"
if (!(Test-Path $backupDir)) {
    New-Item -ItemType Directory -Path $backupDir | Out-Null
}

# Step 1: Version tracking
$versionFile = "version.txt"
if (!(Test-Path $versionFile)) {
    "1.0.0" | Out-File $versionFile -Encoding ASCII
}

$version = Get-Content $versionFile
$parts = $version -split "\."
[int]$major = $parts[0]
[int]$minor = $parts[1]
[int]$patch = $parts[2]

# Increment version
$patch++
if ($patch -ge 10) {
    $patch = 0
    $minor++
}
if ($minor -ge 10) {
    $minor = 0
    $major++
}
if ($major -ge 100) {
    Write-Host "ERROR: Version limit exceeded (max is 99.9.9)"
    exit 1
}

$newVersion = "$major.$minor.$patch"
$newVersion | Out-File $versionFile -Encoding ASCII
Write-Host "New version: $newVersion"

# Step 1a: Backup current script
$scriptPath = "triquetra.py"
$backupPath = Join-Path $backupDir ("triquetra_$newVersion.py")
try {
    Copy-Item $scriptPath $backupPath -Force
    Write-Host "Backup created: $backupPath"
} catch {
    Write-Host "Failed to create backup: $($_.Exception.Message)"
}

# Step 1b: Update version in triquetra.py
$scriptContent = Get-Content $scriptPath -Raw
$updatedContent = $scriptContent -replace 'log\("Triquetra Updater [0-9]+\.[0-9]+\.[0-9]+"\)', "log(`"Triquetra Updater $newVersion`")"
Set-Content -Path $scriptPath -Value $updatedContent -Encoding UTF8
Write-Host "Updated triquetra.py version to $newVersion"

# Step 2: Compile with Nuitka
$nuitkaCommand = @(
    "nuitka",
    "--onefile",
    "--clang",
    "--lto=yes",
    "--quiet",
    "--show-progress",
    "--remove-output",
    "triquetra.py",
    "--windows-icon-from-ico=triquetra.ico",
    "--windows-product-name='Triquetra Updater'",
    "--windows-company-name='SMCE'",
    "--file-description='Triquetra Updater'",
    "--copyright='SMCE 2025'",
    "--windows-file-version=$newVersion",
    "--windows-product-version=$newVersion"
) -join " "

Write-Host "Compiling with Nuitka..."
Invoke-Expression $nuitkaCommand

if (!(Test-Path "triquetra.exe")) {
    Write-Host "ERROR: triquetra.exe not found. Compilation may have failed."
    pause
    exit 1
}

# Step 3: Generate MD5 checksum
$hash = Get-FileHash -Algorithm MD5 "triquetra.exe"
"$($hash.Hash) *triquetra.exe" | Out-File "triquetra.exe.md5" -Encoding ASCII

# Step 4: Map network share persistently, but ONLY if not already mounted
$sharePath = "\\192.168.1.2\docker\w11updater\share"
$driveLetter = "Z:"

Write-Host "Checking if $driveLetter is already mapped..."
$existing = net use | Select-String $driveLetter

if (-not $existing) {
    Write-Host "Mapping network share persistently..."
    $mapResult = net use $driveLetter $sharePath /persistent:yes
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to map network drive."
        pause
        exit 1
    }
} else {
    Write-Host "Drive $driveLetter is already mapped. Skipping mapping."
}

# Step 5: Transfer files
try {
    Copy-Item "triquetra.exe" "$driveLetter\" -Force
    Copy-Item "triquetra.exe.md5" "$driveLetter\" -Force

    if ((Test-Path "$driveLetter\triquetra.exe") -and (Test-Path "$driveLetter\triquetra.exe.md5")) {
        Write-Host "Files transferred successfully to $driveLetter"
    } else {
        Write-Host "Transfer failed. Files not found on $driveLetter"
    }
} catch {
    Write-Host "PowerShell error: $($_.Exception.Message)"
}

Write-Host "Build and deployment completed. Network drive $driveLetter remains mounted persistently."

pause
