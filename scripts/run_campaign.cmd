@echo off
rem Usage: run_campaign.cmd [seconds] [iterations] [fuzz_every]
set "SECONDS=%~1"
if "%SECONDS%"=="" set "SECONDS=60"
set "ITERATIONS=%~2"
if "%ITERATIONS%"=="" set "ITERATIONS=3"
set "FUZZ_EVERY=%~3"
if "%FUZZ_EVERY%"=="" set "FUZZ_EVERY=8"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_campaign.ps1" -Seconds %SECONDS% -Iterations %ITERATIONS% -FuzzEvery %FUZZ_EVERY%
