# Transcriber setup for a new Windows machine. Safe to run again: every step
# checks first and skips what is already done.
#
#   double-click setup.bat
#   or: powershell -ExecutionPolicy Bypass -File setup.ps1 [-Bundle path\to\models.zip]
#
# What it does, in order:
#   1. Python 3.10+ and a .venv with requirements.txt
#   2. ffmpeg on PATH (audio download and recordings need it)
#   3. whisper\ and models\ from the bundle zip (both folders at its root)
#   4. checks that whisper runs on this machine
#   5. per-machine settings: CPU threads, and a model that actually exists
#   6. registers the native host with Chrome (host\install.ps1)
#   7. checks Claude Code (only analysis needs it)
# Then it prints how to load the extension — the one step Chrome won't let
# a script do.

# -SkipHost: leave Chrome's registration alone (for testing a second copy).
param([string]$Bundle = "", [switch]$SkipHost)

# Not "Stop": in Windows PowerShell 5.1 that turns any stderr line of a native
# program (whisper-cli prints its help there) into a fatal error. Each step
# checks $LASTEXITCODE itself instead.
$ErrorActionPreference = "Continue"
$Root = $PSScriptRoot
Set-Location $Root

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "   ok  $text" -ForegroundColor Green }
function Warn($text) { Write-Host "   !!  $text" -ForegroundColor Yellow }
function Fail($text) { Write-Host "   xx  $text" -ForegroundColor Red; exit 1 }

function Ask($question) {
    $a = Read-Host "   $question [Y/n]"
    return ($a -eq "" -or $a -match "^[yYдД]")
}

# A program winget just installed is on the user PATH in the registry, not in
# this window's copy of it.
function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

# --- 1. Python --------------------------------------------------------------

Step "Python"
$Venv = Join-Path $Root ".venv\Scripts\python.exe"
# The command that runs a suitable Python, as an array: @("py", "-3") or
# @("python"). "python" may be the Microsoft Store stub, which exits 9009.
function Find-Python {
    foreach ($try in @(@("py", "-3"), @("python"))) {
        if (-not (Have $try[0])) { continue }
        $rest = @($try | Select-Object -Skip 1)
        $ver = & $try[0] @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver -match "^\d+\.\d+$" -and [version]$ver -ge [version]"3.10") {
            return ,$try
        }
    }
    return $null
}

if (-not (Test-Path $Venv)) {
    $py = Find-Python
    if (-not $py) {
        Warn "Python 3.10 or newer was not found."
        if ((Have "winget") -and (Ask "Install Python 3.13 with winget?")) {
            winget install -e --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements
            Refresh-Path
            $py = Find-Python
        }
        if (-not $py) { Fail "Install Python 3.10+ from python.org (tick 'Add to PATH'), then run setup again." }
    }
    $rest = @($py | Select-Object -Skip 1)
    Write-Host "   creating .venv with $($py -join ' ')"
    & $py[0] @rest -m venv (Join-Path $Root ".venv")
    if ($LASTEXITCODE -ne 0) { Fail "Could not create .venv" }
}
Write-Host "   installing packages (requirements.txt)"
& $Venv -m pip install --disable-pip-version-check -q --upgrade -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "pip install failed — see the error above" }
Ok (& $Venv --version)

# --- 2. ffmpeg --------------------------------------------------------------

Step "ffmpeg"
$FfmpegNew = $false
if (-not (Have "ffmpeg")) {
    if ((Have "winget") -and (Ask "ffmpeg is missing. Install it with winget?")) {
        winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
        Refresh-Path
        $FfmpegNew = $true
    }
}
if (Have "ffmpeg") { Ok "ffmpeg found" }
else { Warn "ffmpeg not found: downloads and recordings won't work until it is on PATH." }

# --- 3. whisper and models --------------------------------------------------

Step "whisper.cpp and models"
$Cli = Join-Path $Root "whisper\whisper-cli.exe"
$HaveModel = [bool](Get-ChildItem (Join-Path $Root "models") -Filter "ggml-*.bin" -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notmatch "silero" })
if (-not ((Test-Path $Cli) -and $HaveModel)) {
    if (-not $Bundle) {
        # The zip next to this script, or one on the Desktop / in Downloads.
        $places = @($Root, "$env:USERPROFILE\Desktop", "$env:USERPROFILE\Downloads")
        $Bundle = Get-ChildItem $places -Filter "*.zip" -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match "model|whisper|transcriber" } |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $Bundle -or -not (Test-Path $Bundle)) {
        Fail "whisper\ and models\ are missing. Put the bundle zip (models.zip) next to setup.bat, or run: setup.ps1 -Bundle <path>"
    }
    Write-Host "   unpacking $Bundle (a gigabyte — a minute or two)"
    # tar (built into Windows 10+) is many times faster than Expand-Archive on a 1 GB zip.
    tar -xf $Bundle -C $Root
    if ($LASTEXITCODE -ne 0) { Fail "Could not unpack $Bundle" }
}
if (-not (Test-Path $Cli)) { Fail "The bundle has no whisper\whisper-cli.exe at its root" }
Get-ChildItem (Join-Path $Root "models") -Filter "*.bin" | ForEach-Object { Ok "model $($_.Name)" }

# --- 4. does whisper run here ----------------------------------------------

Step "whisper on this machine"
$help = & $Cli -h 2>&1 | Out-String
if ($LASTEXITCODE -eq -1073741515) {
    # 0xC0000135: a DLL is missing — on a fresh Windows, the VC++ runtime.
    Warn "whisper-cli can't start: the Microsoft Visual C++ runtime is missing."
    if ((Have "winget") -and (Ask "Install it with winget?")) {
        winget install -e --id Microsoft.VCRedist.2015+.x64 --accept-package-agreements --accept-source-agreements
        $help = & $Cli -h 2>&1 | Out-String
    }
}
if ($help -notmatch "usage") { Fail "whisper-cli.exe does not run on this machine:`n$help" }
Ok "whisper-cli runs (CPU)"
$Gpu = Join-Path $Root "whisper\vulkan\whisper-cli.exe"
if (Test-Path $Gpu) {
    # whisper-cli -h never touches the GPU, so it can't tell. The Vulkan
    # loader comes with the graphics driver: it, plus a real (non-Microsoft)
    # adapter, is a good sign the GPU build will run.
    $cards = Get-CimInstance Win32_VideoController |
        Where-Object { $_.Name -notmatch "Microsoft|Basic Display|Remote" } | ForEach-Object { $_.Name }
    if ((Test-Path "$env:WINDIR\System32\vulkan-1.dll") -and $cards) {
        Ok "Vulkan driver found ($($cards -join ', ')): try Use GPU in Settings, usually several times faster"
    } else {
        Warn "GPU build present, but no Vulkan driver here: stay on CPU"
    }
}

# --- 5. per-machine settings -------------------------------------------------

Step "Settings for this machine"
# Logical processors, capped at 8: on the machine this was tuned on (6 cores,
# 12 threads) -t 8 measured fastest, beyond the physical core count; a small
# laptop gets what it has.
$threads = [Math]::Max(1, [Math]::Min(8, [Environment]::ProcessorCount))
$script = @"
import json, sys
from pathlib import Path
import settings, store
store.init()
stored = settings.stored()
out = []
if 'whisper_threads' not in stored:
    settings.set('whisper_threads', $threads)
    out.append('CPU threads: $threads')
model = Path(settings.get('whisper_model'))
if not model.is_file():
    found = sorted(p for p in Path('models').glob('ggml-*.bin') if 'silero' not in p.name)
    if found:
        settings.set('whisper_model', str(found[0]).replace('\\', '/'))
        out.append('whisper model: ' + found[0].name)
if settings.get('vad_enabled') is False and Path('models/ggml-silero-v6.2.0.bin').is_file() and 'vad_enabled' not in stored:
    out.append('VAD model present (off by default; Settings can turn it on)')
print('\n'.join(out) or 'already set')
"@
$out = $script | & $Venv -
if ($LASTEXITCODE -ne 0) { Fail "Could not write settings" }
$out -split "`n" | ForEach-Object { if ($_) { Ok $_ } }

# --- 6. native host -----------------------------------------------------------

Step "Chrome native host"
if ($SkipHost) { Warn "skipped (-SkipHost)" } else {
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "host\install.ps1")
if ($LASTEXITCODE -ne 0) { Fail "host\install.ps1 failed" }
}

# --- 7. Claude Code -----------------------------------------------------------

Step "Analysis: Claude Code or ChatGPT (Codex)"
# Found the way analyze.py finds them — Codex also inside the VS Code
# extension. With only Codex here, ChatGPT becomes the provider, so Analyse
# works on the first try; a choice already made in Settings is left alone.
$script = @"
import analyze, settings
found = {}
for name, look in (('claude', analyze.claude_path), ('chatgpt', analyze.codex_path)):
    try:
        found[name] = look()
    except Exception:
        pass
for name, path in found.items():
    print('found', name, path)
if found and 'analysis_provider' not in settings.stored() and settings.get('analysis_provider') not in found:
    only = next(iter(found))
    settings.set('analysis_provider', only)
    print('provider', only)
if not found:
    print('none')
"@
$out = $script | & $Venv -
foreach ($line in ($out -split "`n")) {
    $line = $line.Trim()
    if ($line -match "^found (\w+) (.+)$") { Ok "$(if ($Matches[1] -eq 'claude') { 'Claude Code' } else { 'Codex (ChatGPT)' }): $($Matches[2])" }
    elseif ($line -match "^provider (\w+)$") { Ok "analyses will use $($Matches[1]) (change in Settings)" }
    elseif ($line -eq "none") {
        Warn "neither Claude Code nor Codex found: transcription works; for analyses install one and sign in ('claude', or 'codex login' with a ChatGPT account)."
    }
}

# --- done ---------------------------------------------------------------------

Write-Host ""
Write-Host "All set. Last step, in Chrome:" -ForegroundColor Green
Write-Host "   1. open chrome://extensions and turn on Developer mode (top right)"
Write-Host "   2. Load unpacked -> $(Join-Path $Root 'extension')"
Write-Host "   3. pin Transcriber and click it: the side panel opens"
if ($FfmpegNew) {
    Write-Host ""
    Warn "ffmpeg was just installed: quit Chrome completely and reopen it, so the panel sees it."
}
