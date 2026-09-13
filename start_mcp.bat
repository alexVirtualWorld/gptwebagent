@echo off
cd /d %~dp0
python agent.py --transport streamable-http --host 127.0.0.1 --port 8001
pause
