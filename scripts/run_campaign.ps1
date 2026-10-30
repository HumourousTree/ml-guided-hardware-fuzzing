param(
    [int]$Seconds = 60,
    [int]$Iterations = 3,
    [int]$FuzzEvery = 8
)
# Runs the campaign inside WSL from wherever the repository lives (no hard-coded path).

$ErrorActionPreference = 'Stop'
$wsl = Join-Path $env:WINDIR 'System32\wsl.exe'
if (-not (Test-Path $wsl)) {
    throw "WSL launcher not found at $wsl"
}

$repo = (Split-Path -Parent $PSScriptRoot) -replace '\\', '/'
$linuxProject = '/mnt/' + $repo.Substring(0, 1).ToLower() + $repo.Substring(2)
$command = "cd '$linuxProject'; source venv/bin/activate; python3 -m harness.pico_campaign --seconds $Seconds --iterations $Iterations --seed 20260825 --fuzz-every $FuzzEvery"
& $wsl bash -lc $command
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
