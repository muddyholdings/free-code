# free-code installer (Windows PowerShell). Usage:
#   irm https://free.ai/install.ps1 | iex
# Installs the freeai-code package from PyPI with uv (installed to the user dir
# if missing), else pipx, else a private venv in ~\.free-code. No admin rights.
# Re-run to upgrade. Pin a version: $env:FREE_CODE_VERSION = "0.1.4" before running.
# Force a method: $env:FREE_CODE_INSTALLER = "uv", "pipx" or "venv".

function Install-FreeCode {
    $ErrorActionPreference = 'Stop'
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

    $pkg = 'freeai-code'
    $spec = $pkg
    if ($env:FREE_CODE_VERSION) { $spec = "$pkg==$($env:FREE_CODE_VERSION)" }
    $bin = Join-Path $HOME '.local\bin'
    $venv = Join-Path $HOME '.free-code\venv'
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    $env:UV_TOOL_BIN_DIR = $bin
    $env:PIPX_BIN_DIR = $bin
    if (($env:Path -split ';') -notcontains $bin) { $env:Path = "$bin;$env:Path" }

    function Test-Cmd($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }
    function Assert-Ok($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)." } }

    function Install-Uv {
        $env:UV_INSTALL_DIR = $bin
        try { Invoke-RestMethod 'https://astral.sh/uv/install.ps1' | Invoke-Expression } catch { return $false }
        return (Test-Cmd 'uv')
    }

    $method = $env:FREE_CODE_INSTALLER
    if (-not $method) {
        if (Test-Cmd 'uv') { $method = 'uv' }
        elseif (Test-Cmd 'pipx') { $method = 'pipx' }
        elseif (Install-Uv) { $method = 'uv' }
        else { $method = 'venv' }
    }

    Write-Host "Installing $spec with $method ..."
    switch ($method) {
        'uv' {
            if (-not (Test-Cmd 'uv') -and -not (Install-Uv)) { throw 'Could not install uv (needs access to astral.sh).' }
            uv tool install --force --refresh $spec
            Assert-Ok 'uv tool install'
        }
        'pipx' {
            if (-not (Test-Cmd 'pipx')) { throw 'pipx not found.' }
            pipx install --force $spec
            Assert-Ok 'pipx install'
        }
        'venv' {
            $py = $null
            foreach ($c in 'py', 'python', 'python3') { if (Test-Cmd $c) { $py = $c; break } }
            if (-not $py) { throw 'Python 3.9+ not found. Install it from https://python.org/downloads/ and re-run.' }
            New-Item -ItemType Directory -Force -Path (Split-Path $venv) | Out-Null
            & $py -m venv $venv
            Assert-Ok 'python -m venv'
            & (Join-Path $venv 'Scripts\python.exe') -m pip install --quiet --upgrade pip $spec
            Assert-Ok 'pip install'
            $shim = Join-Path $venv 'Scripts\free-code.exe'
            Set-Content -Path (Join-Path $bin 'free-code.cmd') -Value "@echo off`r`n`"$shim`" %*" -Encoding ASCII
        }
        default { throw "Unknown FREE_CODE_INSTALLER '$method' (use uv, pipx or venv)." }
    }

    # Put the bin dir on the user PATH (persistent) if it is not there yet.
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (($userPath -split ';') -notcontains $bin) {
        [Environment]::SetEnvironmentVariable('Path', "$bin;$userPath", 'User')
        Write-Host "Added $bin to your user PATH. Open a new terminal to use free-code."
    }

    $exe = Join-Path $bin 'free-code.exe'
    if (-not (Test-Path $exe)) { $exe = Join-Path $bin 'free-code.cmd' }
    if (-not (Test-Path $exe)) { throw "free-code was not created in $bin." }
    & $exe --version
    Write-Host ''
    Write-Host "Next: run 'free-code' in a project folder. Docs: https://free.ai/code/"
}

Install-FreeCode
