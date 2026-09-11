@echo off
REM mochang-dashboard 런처 (모창봇 관리자 대시보드, 127.0.0.1:8001)
REM NSSM 서비스가 이 파일을 부른다 - scripts\svc\add-dashboard-service.ps1 이 등록한다.
REM   nssm install mochang-dashboard C:\WINDOWS\System32\cmd.exe /c C:\Users\bon505\Desktop\mochang-bot\scripts\svc\mochang-dashboard.bat
REM 경로에 공백이 없는 곳에 두어야 NSSM 인자 전달이 깨지지 않는다 (svc\mochang-api.bat 과 같은 형식).
REM
REM 라이브 API(mochang-api, 8000)와 별개의 프로세스다. 읽기만 하므로 죽어도 서비스에 영향이 없다.
REM 외부에 열려면 --host 0.0.0.0 --port 50002 --password ... 로 바꾼다 (비밀번호 없이는 스크립트가 거부한다).

cd /d "C:\Users\bon505\Desktop\mochang-bot"
"C:\Users\bon505\Desktop\mochang-bot\.venv\Scripts\python.exe" scripts\admin_dashboard.py --host 127.0.0.1 --port 8001
