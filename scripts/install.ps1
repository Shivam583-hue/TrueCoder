$ErrorActionPreference = "Stop"

$Version = "1.0.1"
$Repository = "Shivam583-hue/TrueCoder"
$Wheel = "truecoder-$Version-py3-none-any.whl"
$DefaultReleaseBase = "https://github.com/$Repository/releases/download/v$Version"
$ReleaseBase = if ($env:TRUECODER_RELEASE_BASE_URL) {
    $env:TRUECODER_RELEASE_BASE_URL.TrimEnd("/")
} else {
    $DefaultReleaseBase
}
$InstallRoot = if ($env:TRUECODER_INSTALL_DIR) {
    $env:TRUECODER_INSTALL_DIR
} else {
    Join-Path $env:LOCALAPPDATA "TrueCoder"
}
$BinDir = if ($env:TRUECODER_BIN_DIR) {
    $env:TRUECODER_BIN_DIR
} else {
    Join-Path $InstallRoot "bin"
}
$VenvDir = Join-Path $InstallRoot "venv"

$PythonExe = $null
$PythonPrefix = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonExe = "py"
    $PythonPrefix = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonExe = "python"
} elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
    $PythonExe = "python3"
} else {
    throw "TrueCoder installer: Python 3.10 or newer is required"
}

& $PythonExe @PythonPrefix -c `
    "import sys; raise SystemExit(sys.version_info < (3, 10))"
if ($LASTEXITCODE -ne 0) {
    throw "TrueCoder installer: Python 3.10 or newer is required"
}

$TempDir = Join-Path ([System.IO.Path]::GetTempPath()) `
    ("truecoder-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $TempDir | Out-Null

try {
    $WheelPath = Join-Path $TempDir $Wheel
    $ChecksumsPath = Join-Path $TempDir "SHA256SUMS"
    Write-Host "Downloading TrueCoder $Version..."
    Invoke-WebRequest -Uri "$ReleaseBase/$Wheel" -OutFile $WheelPath
    Invoke-WebRequest -Uri "$ReleaseBase/SHA256SUMS" -OutFile $ChecksumsPath

    $EscapedWheel = [regex]::Escape($Wheel)
    $Record = Get-Content $ChecksumsPath |
        Where-Object { $_ -match "\s+$EscapedWheel$" } |
        Select-Object -First 1
    if (-not $Record) {
        throw "TrueCoder installer: the release checksum for $Wheel is missing"
    }
    $Expected = ($Record -split "\s+")[0].ToLowerInvariant()
    $Actual = (Get-FileHash -Algorithm SHA256 $WheelPath).Hash.ToLowerInvariant()
    if ($Expected -ne $Actual) {
        throw "TrueCoder installer: the downloaded wheel failed checksum verification"
    }

    New-Item -ItemType Directory -Force -Path $InstallRoot, $BinDir | Out-Null
    & $PythonExe @PythonPrefix -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        throw "TrueCoder installer: Python could not create a virtual environment"
    }

    $VenvPython = Join-Path $VenvDir "Scripts\python.exe"
    & $VenvPython -m pip install `
        --disable-pip-version-check `
        --upgrade `
        $WheelPath
    if ($LASTEXITCODE -ne 0) {
        throw "TrueCoder installer: package installation failed"
    }

    $Launcher = Join-Path $BinDir "truecoder.cmd"
    $Executable = Join-Path $VenvDir "Scripts\truecoder.exe"
    Set-Content -Encoding ASCII -Path $Launcher -Value @(
        "@echo off",
        "`"$Executable`" %*"
    )

    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $Entries = @($UserPath -split ";" | Where-Object { $_ })
    if ($Entries -notcontains $BinDir) {
        $NewPath = (@($Entries) + $BinDir) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
    }
    $env:Path = "$BinDir;$env:Path"

    $Installed = & $Launcher --version
    Write-Host "Installed $Installed at $Launcher"
    Write-Host "Open a new terminal, then run: truecoder"
} finally {
    Remove-Item -Recurse -Force $TempDir -ErrorAction SilentlyContinue
}
