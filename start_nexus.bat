@echo off
cd /d "%~dp0"
REM Optional settings (uncomment to change):
REM set NEXUS_MODEL=llama3.1
REM set SD_API_URL=http://127.0.0.1:7860
REM set SD_SIZE=768
if exist venv\Scripts\activate.bat call venv\Scripts\activate.bat
python main.py
pause
