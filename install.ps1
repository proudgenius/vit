# ---------------------------------------------
#  Vit Installer -- Git for Video Editing (Windows)
#  Usage: irm https://raw.githubusercontent.com/proudgenius/vit/main/install.ps1 | iex
# ---------------------------------------------

$ErrorActionPreference = "Stop"

$VIT_HOME = "$env:USERPROFILE\.vit"
$VIT_SRC = "$VIT_HOME\vit-src"
$REPO_URL = "https://github.com/proudgenius/vit.git"

Write-Host ""
Write-Host "  Vit -- Git for Video Editing"
Write-Host "  -----------------------------"
Write-Host ""

# -- Check prerequisites ----------------------

function Check-Command($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        Write-Host "  Error: '$name' is not installed. Please install it and try again."
        exit 1
    }
}

Check-Command "git"

# Find Python 3.8+
$PYTHON = $null
foreach ($cmd in @("python3", "python")) {
    $found = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($found) {
        try {
            $versionOutput = & $cmd --version 2>&1
            if ($versionOutput -match "(\d+)\.(\d+)") {
                $major = [int]$Matches[1]
                $minor = [int]$Matches[2]
                if ($major -ge 3 -and $minor -ge 8) {
                    $PYTHON = $cmd
                    break
                }
            }
        } catch { continue }
    }
}

if (-not $PYTHON) {
    Write-Host "  Error: Python 3.8+ is required. Please install it and try again."
    exit 1
}

$pyVersion = & $PYTHON --version 2>&1
$gitVersion = git --version
Write-Host "  Using: $pyVersion, $gitVersion"

# Check pip
$hasPip = $false
try {
    & $PYTHON -m pip --version 2>&1 | Out-Null
    $hasPip = $true
} catch {}

if (-not $hasPip) {
    Write-Host "  Error: pip is not installed. Please install it and try again."
    exit 1
}

# -- Download / update source -----------------

if (-not (Test-Path $VIT_HOME)) {
    New-Item -ItemType Directory -Path $VIT_HOME -Force | Out-Null
}

if (Test-Path "$VIT_SRC\.git") {
    Write-Host "  Updating existing installation..."
    git -C $VIT_SRC pull --quiet
} else {
    if (Test-Path $VIT_SRC) {
        Remove-Item -Recurse -Force $VIT_SRC
    }
    Write-Host "  Downloading Vit..."
    git clone --quiet $REPO_URL $VIT_SRC
}

# -- Install into venv -----------------------

$VIT_VENV = "$VIT_HOME\venv"

if (-not (Test-Path $VIT_VENV)) {
    Write-Host "  Creating virtual environment..."
    & $PYTHON -m venv $VIT_VENV
}

Write-Host "  Installing Vit package..."
& "$VIT_VENV\Scripts\pip.exe" install $VIT_SRC --quiet

# -- Add venv Scripts to PATH -----------------

$VIT_BIN = "$VIT_VENV\Scripts"
$userPath = [System.Environment]::GetEnvironmentVariable("PATH", "User")
if (-not ($userPath -split ';' | Where-Object { $_ -eq $VIT_BIN })) {
    Write-Host "  Adding $VIT_BIN to PATH..."
    $env:PATH = "$VIT_BIN;$env:PATH"
    [System.Environment]::SetEnvironmentVariable(
        "PATH",
        "$VIT_BIN;" + $userPath,
        "User"
    )
}

# -- Write vit.cmd wrapper (avoids Defender blocking vit.exe) ----

$vitCmd = "$VIT_BIN\vit.cmd"
if (-not (Test-Path $vitCmd)) {
    Write-Host "  Writing vit.cmd wrapper..."
    Set-Content -Path $vitCmd -Value "@`"%~dp0python.exe`" -m vit.cli %*" -Encoding ASCII
}

# -- Install Resolve plugin scripts -----------

Write-Host "  Installing DaVinci Resolve scripts..."
try {
    & "$VIT_VENV\Scripts\python.exe" -m vit.cli install-resolve
} catch {
    Write-Host ""
    Write-Host "  Note: Could not auto-install Resolve scripts."
    Write-Host "  After restarting your terminal, run: vit install-resolve"
}

# -- Done -------------------------------------

Write-Host ""
Write-Host "  Vit installed."
Write-Host ""
Write-Host "  Setting up the record folder and MCP config..."
Write-Host ""
try {
    & "$VIT_VENV\Scripts\python.exe" -m vit setup "$VIT_HOME\record"
} catch {
    Write-Host "  Could not run setup. After restarting your terminal, run: vit setup"
}
Write-Host ""
Write-Host "  For the panel inside Resolve (branch, merge, push, pull):"
Write-Host "    Workspace > Scripts > Vit. Add AI merge help with:"
Write-Host "    $VIT_VENV\Scripts\pip.exe install `"$VIT_SRC[all]`""
Write-Host ""
