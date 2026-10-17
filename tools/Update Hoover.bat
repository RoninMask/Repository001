@echo off
cd /d "%~dp0.."
echo === Update Hoover: %date% %time% > update_log.txt
git fetch origin >> update_log.txt 2>&1
git checkout claude/interview-ingest >> update_log.txt 2>&1
git pull --ff-only >> update_log.txt 2>&1
echo === version >> update_log.txt
python T11_F125_Baby_Hoover_V4_05OCT26.py --version >> update_log.txt 2>&1
echo === git >> update_log.txt
git log --oneline -3 >> update_log.txt 2>&1
git status --short >> update_log.txt 2>&1
echo === done >> update_log.txt
type update_log.txt
echo.
echo Done. This window can be closed.
pause
