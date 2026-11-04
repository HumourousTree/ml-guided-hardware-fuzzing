@echo off
rem Runs scripts/run_tracks.sh in WSL; meant for Task Scheduler so the jobs do not depend on any open terminal.
rem Assumes the repository is on drive C: (the WSL path is /mnt/c/...).
set "HERE=%~dp0"
set "HERE=%HERE:\=/%"
"%WINDIR%\System32\wsl.exe" -d Ubuntu bash "/mnt/c%HERE:~2%run_tracks.sh"
