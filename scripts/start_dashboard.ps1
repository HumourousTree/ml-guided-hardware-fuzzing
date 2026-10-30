# Starts the Streamlit dashboard inside WSL from wherever the repository lives (no hard-coded path).
$ErrorActionPreference = 'Stop'
$wsl = Join-Path $env:WINDIR 'System32\wsl.exe'
if (-not (Test-Path $wsl)) {
    throw "WSL launcher not found at $wsl"
}
$repo = (Split-Path -Parent $PSScriptRoot) -replace '\\', '/'
$linuxProject = '/mnt/' + $repo.Substring(0, 1).ToLower() + $repo.Substring(2)
& $wsl bash -lc "cd '$linuxProject'; source venv/bin/activate; python3 -m pip install -r requirements.txt; streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8501"
