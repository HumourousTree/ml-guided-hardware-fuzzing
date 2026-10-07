param(
    [int]$Seconds = 60,
    [int]$Iterations = 3,
    [int]$FuzzEvery = 8
)

$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$wsl = Join-Path $env:WINDIR 'System32\wsl.exe'
if (-not (Test-Path $wsl)) {
    throw "WSL launcher not found at $wsl"
}

$linuxProject = '/mnt/c/Users/subod/Downloads/ML Guided Hardware fuzzing'
$command = "cd '$linuxProject'; source venv/bin/activate; python3 -m harness.pico_campaign --seconds $Seconds --iterations $Iterations --seed 20260825 --fuzz-every $FuzzEvery"
& $wsl bash -lc $command
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
