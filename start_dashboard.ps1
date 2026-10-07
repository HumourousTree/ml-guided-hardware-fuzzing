$ErrorActionPreference = 'Stop'
$wsl = Join-Path $env:WINDIR 'System32\wsl.exe'
if (-not (Test-Path $wsl)) {
    throw "WSL launcher not found at $wsl"
}
$linuxProject = '/mnt/c/Users/subod/Downloads/ML Guided Hardware fuzzing'
& $wsl bash -lc "cd '$linuxProject'; source venv/bin/activate; python3 -m pip install -r requirements.txt; streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8501"
