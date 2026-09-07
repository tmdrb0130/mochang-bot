# =====================================================================
#  모창봇 관리자 대시보드를 Windows 서비스로 + 워치독을 작업 스케줄러에 (2026-09-07)
#  관리자 권한 PowerShell 에서 실행할 것.  Desktop\add-mochang-service.ps1 과 같은 형식.
#
#  하는 일
#    1. 수동으로 떠 있는 admin_dashboard.py / watchdog.py --watch 종료
#    2. mochang-dashboard 서비스 등록 (자동 시작 / 죽으면 5초 뒤 재시작 / 로그 로테이션, 127.0.0.1:8001)
#    3. mochang-watchdog 작업 스케줄러 등록 (5분마다 검사, 이상 시 팝업)
#       — 서비스가 아니라 작업인 이유: 서비스는 바탕화면이 없어 팝업(--notify)을 띄울 수 없다.
#         작업 스케줄러의 "로그온한 사용자로 실행" 은 바탕화면에 붙어 팝업이 보인다. (로그오프하면 멈춘다 — 이 PC 는 로그오프 안 함)
#    4. 검증
#
#  되돌리기:  .\add-dashboard-service.ps1 -Uninstall
# =====================================================================

param([switch]$Uninstall)

$ErrorActionPreference = 'Continue'

$SVC     = 'mochang-dashboard'
$TASK    = 'mochang-watchdog'
$APPDIR  = 'C:\Users\bon505\Desktop\mochang-bot'
$LAUNCH  = "$APPDIR\scripts\svc\mochang-dashboard.bat"
$PYW     = "$APPDIR\.venv\Scripts\pythonw.exe"
$LOG_DIR = 'C:\logs'
$CMD     = "$env:WINDIR\System32\cmd.exe"

function Step($m) { Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "   OK  $m" -ForegroundColor Green }
function Bad($m)  { Write-Host "   !!  $m" -ForegroundColor Yellow }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Host "  [중단] 관리자 권한으로 실행하세요." -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- nssm
$nssm = (Get-Command nssm -ErrorAction SilentlyContinue).Source
if (-not $nssm) {
    $nssm = (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Filter nssm.exe -Recurse -ErrorAction SilentlyContinue |
             Where-Object { $_.FullName -match 'win64' } | Select-Object -First 1).FullName
}
if (-not $nssm) { Write-Host "  [중단] nssm.exe 를 못 찾았습니다." -ForegroundColor Red; exit 1 }

if ($Uninstall) {
    Step "$SVC 제거"
    & $nssm stop $SVC | Out-Null
    & $nssm remove $SVC confirm | Out-Null
    Ok "서비스 제거"
    Step "$TASK 제거"
    schtasks /Delete /F /TN $TASK 2>$null | Out-Null
    Ok "작업 제거 — 이제 둘 다 수동으로 켜야 합니다 (scripts\admin_dashboard.py / scripts\watchdog.py --watch)"
    exit 0
}

Step "사전 확인"
if (Test-Path $LAUNCH) { Ok "런처 $LAUNCH" } else { Write-Host "  [중단] 런처 없음: $LAUNCH" -ForegroundColor Red; exit 1 }
if (Test-Path $PYW)    { Ok "pythonw $PYW" }  else { Write-Host "  [중단] pythonw 없음: $PYW" -ForegroundColor Red; exit 1 }
New-Item -ItemType Directory -Force -Path $LOG_DIR | Out-Null

# ---------------------------------------------------------------- 1. 수동 프로세스 정리
Step "수동으로 떠 있는 대시보드·워치독 종료"
$manual = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match 'admin_dashboard\.py' -or ($_.CommandLine -match 'watchdog\.py' -and $_.CommandLine -match '--watch')
}
if ($manual) {
    foreach ($p in $manual) { "   kill $($p.ProcessId)  $($p.Name)"; Stop-Process -Id $p.ProcessId -Force -EA SilentlyContinue }
    Start-Sleep 3
} else { Ok "없음" }

# ---------------------------------------------------------------- 2. 대시보드 서비스
Step "$SVC 서비스 등록"
if (Get-Service $SVC -ErrorAction SilentlyContinue) {
    & $nssm stop $SVC | Out-Null
    & $nssm remove $SVC confirm | Out-Null
    Start-Sleep 1
}

& $nssm install $SVC $CMD '/c' $LAUNCH | Out-Null
& $nssm set $SVC AppDirectory        $APPDIR                   | Out-Null
& $nssm set $SVC AppStdout           "$LOG_DIR\$SVC.log"       | Out-Null
& $nssm set $SVC AppStderr           "$LOG_DIR\$SVC.err.log"   | Out-Null
& $nssm set $SVC AppRotateFiles      1        | Out-Null
& $nssm set $SVC AppRotateBytes      10485760 | Out-Null      # 10MB
& $nssm set $SVC Start               SERVICE_AUTO_START | Out-Null
& $nssm set $SVC AppExit Default     Restart  | Out-Null
& $nssm set $SVC AppRestartDelay     5000     | Out-Null
& $nssm set $SVC AppKillProcessTree  1        | Out-Null
& $nssm set $SVC AppStopMethodConsole 5000    | Out-Null
& $nssm set $SVC Description "모창봇 관리자 대시보드 (읽기 전용, 127.0.0.1:8001)" | Out-Null

# mochang-api 와 같은 계정(bon505)으로 — .venv 와 backend\.data, nginx 로그를 읽어야 한다
Write-Host ""
Write-Host "  서비스를 'bon505' 계정으로 등록합니다 (mochang-api 와 같게). 취소하면 LocalSystem." -ForegroundColor Yellow
$cred = Get-Credential -UserName "$env:COMPUTERNAME\$env:USERNAME" -Message "$SVC 실행 계정"
if ($cred) {
    & $nssm set $SVC ObjectName $cred.UserName $cred.GetNetworkCredential().Password | Out-Null
    Ok "실행 계정: $($cred.UserName)"
} else {
    Bad "LocalSystem 으로 진행 — nginx 로그를 못 읽으면 접속자 칸이 비어 보인다. 계정 지정으로 다시 실행할 것"
}

Step "$SVC 시작"
& $nssm start $SVC | Out-Null
Start-Sleep 8
$st = (Get-Service $SVC -EA SilentlyContinue).Status
if ($st -eq 'Running') { Ok "$SVC $st" } else { Bad "$SVC $st — $LOG_DIR\$SVC.err.log 확인" }

# ---------------------------------------------------------------- 3. 워치독 작업
Step "$TASK 작업 스케줄러 등록 (5분마다, 팝업은 로그온한 사용자 화면에)"
# Register-ScheduledTask 의 -RepetitionDuration 은 PowerShell 5.1 에서 "무기한" 을 표현할 수 없다
# ([TimeSpan]::MaxValue 는 XML 범위 초과로 거부됨 — 2026-09-07 실제로 그랬다). schtasks 의 /SC MINUTE 는 무기한이 기본이다.
# /IT = 로그온한 사용자의 화면에서 실행(팝업이 보인다). 경로에 공백이 없어 따옴표가 필요 없다.
schtasks /Delete /F /TN $TASK 2>$null | Out-Null
$tr = "$PYW $APPDIR\scripts\watchdog.py --notify"
schtasks /Create /F /TN $TASK /SC MINUTE /MO 5 /IT /RL LIMITED /TR $tr | Out-Null
if (schtasks /Query /TN $TASK 2>$null) { Ok "$TASK 등록 (5분마다, 첫 검사는 다음 5분 경계)" } else { Bad "$TASK 등록 실패" }

# ---------------------------------------------------------------- 4. 검증
Step "검증"
$up = [bool](netstat -ano | Select-String ":8001\s+.*LISTENING")
$mark = 'X'; if ($up) { $mark = 'O' }
Write-Host ("   {0}  8001 대시보드" -f $mark)
& curl.exe -s -o NUL -w "   dashboard  %{http_code}`n" http://127.0.0.1:8001/
& curl.exe -s -o NUL -w "   api        %{http_code}`n" http://127.0.0.1:8000/health
Write-Host ""
Write-Host "  등록 완료. 대시보드는 재부팅/장애에 자동 복구되고, 워치독은 5분마다 돕니다." -ForegroundColor Green
Write-Host "  대시보드: http://127.0.0.1:8001     상태: Get-Service $SVC     재시작: nssm restart $SVC" -ForegroundColor Green
Write-Host "  워치독:   Get-ScheduledTask $TASK    기록: $APPDIR\backend\.data\alerts.log" -ForegroundColor Green
Write-Host "  로그: $LOG_DIR\$SVC.log / .err.log"
Write-Host ""
