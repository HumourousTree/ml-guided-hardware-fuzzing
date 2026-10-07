@echo off
set "WSL=%WINDIR%\System32\wsl.exe"
"%WSL%" bash -lc "cd '/mnt/c/Users/subod/Downloads/ML Guided Hardware fuzzing'; source venv/bin/activate; python3 -m pip install -r requirements.txt; streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8501"
