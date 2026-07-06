# ===========================================================================
#  MT5 Smart Trading Bot - Visual C++ Redistributable helper (Windows 7)
# ---------------------------------------------------------------------------
#  numpy / pandas / lightgbm wheels and the MetaTrader5 terminal are built
#  against the Microsoft Visual C++ 2015-2019 (x64) runtime. On a fresh
#  Windows 7 this runtime is often missing, producing "DLL load failed" errors
#  when importing numpy.
#
#  This script:
#    1. Checks the registry for an installed VC++ 14.x x64 runtime.
#    2. If missing, downloads and silently installs the official Microsoft
#       vc_redist.x64.exe.
#
#  It is safe to run repeatedly; if the runtime is present it does nothing.
#  It never throws to the caller (errors are printed and swallowed) so the
#  main installer keeps going.
#
#  Standard ASCII English only.
# ===========================================================================

$ErrorActionPreference = "Stop"

function Test-VCRedistInstalled {
    # VC++ 2015-2022 share the same 14.x runtime family. Check the standard key.
    $keys = @(
        "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
    )
    foreach ($k in $keys) {
        try {
            if (Test-Path $k) {
                $installed = (Get-ItemProperty -Path $k -Name "Installed" -ErrorAction SilentlyContinue).Installed
                if ($installed -eq 1) { return $true }
            }
        } catch { }
    }
    return $false
}

try {
    if (Test-VCRedistInstalled) {
        Write-Host "[ OK ]  Visual C++ 2015-2019 x64 runtime already installed."
        exit 0
    }

    Write-Host "[WARN]  Visual C++ x64 runtime not detected. Downloading..."

    # Microsoft's evergreen redirect for the latest supported 14.x x64 runtime.
    # (This package still installs on Windows 7 SP1.)
    $url = "https://aka.ms/vs/16/release/vc_redist.x64.exe"
    $out = Join-Path $env:TEMP "vc_redist.x64.exe"

    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    } catch { }

    Invoke-WebRequest -Uri $url -OutFile $out
    Write-Host "[INFO]  Installing Visual C++ runtime silently..."
    $p = Start-Process -FilePath $out -ArgumentList "/install", "/quiet", "/norestart" -Wait -PassThru
    if ($p.ExitCode -eq 0 -or $p.ExitCode -eq 3010) {
        Write-Host "[ OK ]  Visual C++ runtime installed (exit $($p.ExitCode))."
        exit 0
    } else {
        Write-Host "[WARN]  VC++ installer exit code $($p.ExitCode). If you later see"
        Write-Host "        'DLL load failed' importing numpy, install vc_redist.x64.exe"
        Write-Host "        manually from https://aka.ms/vs/16/release/vc_redist.x64.exe"
        exit 0
    }
} catch {
    Write-Host "[WARN]  Could not auto-install the VC++ runtime: $($_.Exception.Message)"
    Write-Host "        If numpy import fails with 'DLL load failed', install"
    Write-Host "        vc_redist.x64.exe manually from Microsoft, then retry."
    exit 0
}
