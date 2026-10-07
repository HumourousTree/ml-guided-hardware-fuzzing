@echo off
set "SECONDS=%~1"
if "%SECONDS%"=="" set "SECONDS=60"
set "ITERATIONS=%~2"
if "%ITERATIONS%"=="" set "ITERATIONS=3"
set "FUZZ_EVERY=%~3"
if "%FUZZ_EVERY%"=="" set "FUZZ_EVERY=8"
set "WSL=%WINDIR%\System32\wsl.exe"
"%WSL%" bash -lc "cd '/mnt/c/Users/subod/Downloads/ML Guided Hardware fuzzing'; source venv/bin/activate; python3 -m harness.pico_campaign --seconds %SECONDS% --iterations %ITERATIONS% --seed 20260825 --fuzz-every %FUZZ_EVERY%"
