$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  Overseas Meeting Translator" -ForegroundColor Cyan
Write-Host "  Path: $PSScriptRoot" -ForegroundColor DarkGray
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host ""

$env:PIP_CACHE_DIR = Join-Path $PSScriptRoot ".pip_cache"

$venvDir = Join-Path $PSScriptRoot ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$venvPythonW = Join-Path $venvDir "Scripts\pythonw.exe"

function Test-ValidPython {
    param(
        [string]$Exe,
        [string]$Arg1 = "",
        [bool]$SkipWindowsApps = $true
    )
    try {
        $cmd = Get-Command $Exe -ErrorAction Stop
        if ($SkipWindowsApps -and ($cmd.Source -like "*WindowsApps*")) {
            return $false
        }
        $checkCode = "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
        if ($Arg1 -ne "") {
            & $Exe $Arg1 -c $checkCode 2>$null | Out-Null
        } else {
            & $Exe -c $checkCode 2>$null | Out-Null
        }
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Find-Python {
    # Optional local override file (gitignored) for custom portable Python paths
    $customFile = Join-Path $PSScriptRoot "python_path.txt"
    if (Test-Path -LiteralPath $customFile) {
        $customPath = (Get-Content -LiteralPath $customFile -First 1).Trim().Trim('"')
        if ($customPath -ne "" -and (Test-Path -LiteralPath $customPath)) {
            if (Test-ValidPython -Exe $customPath -SkipWindowsApps $false) {
                return @{ Exe = $customPath; Arg1 = "" }
            }
        }
    }

    if (Test-ValidPython -Exe "python" -SkipWindowsApps $true) {
        return @{ Exe = "python"; Arg1 = "" }
    }
    if (Test-ValidPython -Exe "py" -Arg1 "-3" -SkipWindowsApps $true) {
        return @{ Exe = "py"; Arg1 = "-3" }
    }

    $patterns = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "C:\Python3*\python.exe",
        "D:\Python3*\python.exe",
        "$env:ProgramFiles\Python3*\python.exe",
        "$env:USERPROFILE\miniconda3\python.exe",
        "$env:USERPROFILE\anaconda3\python.exe"
    )
    foreach ($pattern in $patterns) {
        $found = Get-Item -Path $pattern -ErrorAction SilentlyContinue | Sort-Object FullName -Descending
        foreach ($item in $found) {
            if (Test-ValidPython -Exe $item.FullName -SkipWindowsApps $false) {
                return @{ Exe = $item.FullName; Arg1 = "" }
            }
        }
    }
    return $null
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    $py = Find-Python
    if ($null -eq $py) {
        Write-Host "[ERROR] Python 3.10+ was not found on this computer." -ForegroundColor Red
        Write-Host "Please install Python 3.10 - 3.12 from:" -ForegroundColor Yellow
        Write-Host "  https://www.python.org/downloads/" -ForegroundColor White
        Write-Host "Make sure to check 'Add Python to PATH' during installation." -ForegroundColor Yellow
        Write-Host "Tip: If you use a portable Python, put its full python.exe path in python_path.txt." -ForegroundColor DarkGray
        Write-Host ""
        exit 1
    }

    Write-Host "[1/2] Creating isolated virtual environment (.venv)..." -ForegroundColor Green
    Write-Host "      Using: $($py.Exe) $($py.Arg1)" -ForegroundColor DarkGray
    if ($py.Arg1 -ne "") {
        & $py.Exe $py.Arg1 -m venv $venvDir
    } else {
        & $py.Exe -m venv $venvDir
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
        Write-Host "[ERROR] Failed to create virtual environment (.venv)." -ForegroundColor Red
        exit 1
    }
}

& $venvPython -c "import PyQt6, pyaudiowpatch, numpy, requests" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[2/2] Installing dependencies (PyQt6, PyAudioWPatch, numpy, requests)..." -ForegroundColor Green
    $reqFile = Join-Path $PSScriptRoot "requirements.txt"
    & $venvPython -m pip install -r $reqFile -i https://pypi.tuna.tsinghua.edu.cn/simple
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[INFO] Tsinghua mirror failed, retrying with official PyPI..." -ForegroundColor Yellow
        & $venvPython -m pip install -r $reqFile
        if ($LASTEXITCODE -ne 0) {
            Write-Host ""
            Write-Host "[ERROR] Dependency installation failed." -ForegroundColor Red
            Write-Host "Please check your network or Python version (3.10 - 3.12 recommended)." -ForegroundColor Yellow
            exit 1
        }
    }
}

Write-Host "[START] Launching floating subtitle window..." -ForegroundColor Green
if (Test-Path -LiteralPath $venvPythonW) {
    Start-Process -FilePath $venvPythonW -ArgumentList "main.py" -WorkingDirectory $PSScriptRoot
} else {
    Start-Process -FilePath $venvPython -ArgumentList "main.py" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
}
Start-Sleep -Milliseconds 600
exit 0
