# 모창봇 사이트 일시중지 · 재개 절차 (방화벽 규칙 `Mochang-Site-Pause`)

기준일 2026-10-28. 이 PC(`DESKTOP-BMG5FOD`)의 관리자 PowerShell 에서 하는 작업이다.

## 1. 무엇으로 멈춰 있나

모창봇 공개 주소 `https://bustartup.kr:50001/` 은 이 PC 의 nginx 가 TCP **50001** 로 받는다.
이 포트에는 Windows 방화벽 규칙이 **두 개** 걸려 있다.

| 규칙 이름 | 방향 | 동작 | 포트 | 뜻 |
|---|---|---|---|---|
| `mochang-bot 50001` | Inbound | **Allow** | TCP 50001 | 2026-09 공개 때 만든 허용 규칙. 항상 켜 둔다 |
| `Mochang-Site-Pause` | Inbound | **Block** | TCP 50001 | **사이트 일시중지 스위치.** 켜면(Enabled) 외부 접속이 막힌다 |

Windows 방화벽은 **Block 이 Allow 보다 우선**한다. 그래서 `Mochang-Site-Pause` 를 켜는 것만으로 사이트가 밖에서 안 열리고,
끄면 바로 다시 열린다. 서비스(`mochang-api`)·nginx·GPU 서버는 건드리지 않는다.

- 2026-10-28 17:40 현재 **`Mochang-Site-Pause` 가 Enabled(켜짐)** = 외부에서는 접속이 안 되는 상태다.
  `mochang-api` 서비스는 Running 이라 "API 는 살아 있는데 문만 닫힌" 상태다.
- `bustartup.kr` 의 443(다른 프로젝트 VSP 사이트)은 이 규칙과 무관하다.

## 2. 지금 상태 보기

```powershell
Get-NetFirewallRule -DisplayName "Mochang-Site-Pause" | Select-Object DisplayName, Enabled, Action
Get-Service mochang-api, mochang-tunnel, vsp-nginx
```

- `Enabled : True`  → 일시중지 중(막힘)
- `Enabled : False` → 공개 중(열림)

**주의 — 이 PC 에서 `curl https://bustartup.kr:50001/` 이 200 이어도 외부에서 열린다는 뜻이 아니다.**
자기 자신으로 가는 트래픽은 방화벽 인바운드 규칙을 타지 않는다. 외부 확인은 **휴대폰 LTE/5G(와이파이 끄고)** 로 주소를 열어 본다.

## 3. 사이트 다시 여는 법 (일시중지 해제)

관리자 PowerShell(시작 메뉴 > PowerShell 우클릭 > 관리자 권한으로 실행) 에서 한 줄씩:

```powershell
Get-Service mochang-api, mochang-tunnel, mochang-dashboard, vsp-nginx    # 넷 다 Running 인지
Start-Service mochang-api                                                 # Stopped 였을 때만
Invoke-RestMethod http://127.0.0.1:8000/health | Select-Object ok, llm_reachable
Disable-NetFirewallRule -DisplayName "Mochang-Site-Pause"                 # ← 이것이 "문 열기"
Get-NetFirewallRule -DisplayName "Mochang-Site-Pause" | Select-Object Enabled   # False 여야 한다
```

그 다음 휴대폰 LTE 로 `https://bustartup.kr:50001/` 을 열어 화면이 뜨고, 상단에 모델 이름(`백석대학교 로컬 LLM`)이 보이면 끝.

열기 전에 `llm_reachable` 이 `False` 면 문을 열어도 글 생성이 전부 실패한다 — 먼저 §5 의 "터널/모델" 항목을 본다.
(`llm_reachable` 은 30초 캐시라 터널을 막 살린 직후엔 False 가 최대 30초 더 보인다.)

## 4. 사이트 멈추는 법 (일시중지)

```powershell
Invoke-RestMethod http://127.0.0.1:8000/jobs      # running / queued 가 0 인지 — 학생이 작업 중이면 끝날 때까지 기다린다
Enable-NetFirewallRule -DisplayName "Mochang-Site-Pause"     # ← 문 닫기. 이것만으로 충분하다
```

`Stop-Service mochang-api` 까지 할지는 선택이다.

| 방법 | 장점 | 단점 |
|---|---|---|
| 규칙만 켠다 (권장) | 즉시 되돌릴 수 있다. 워치독·대시보드·백업이 평소대로 돈다. 열어 둔 학생 탭이 있어도 서버 쪽 마무리 작업자(finisher)가 빈 문항을 채워 둔다 | 백엔드 프로세스는 계속 돈다 (메모리 수백 MB) |
| 규칙 + `Stop-Service mochang-api` | 자원을 완전히 놓는다 | 워치독이 5분마다 "심각 — API 무응답" 팝업을 띄운다. 다시 열 때 `Start-Service` 를 잊기 쉽다 |

바탕화면 `모창봇 명령어.txt` 에 적힌 "시작/종료" 두 줄짜리는 후자(규칙 + 서비스)다. 둘 다 맞는 방법이다.

## 5. 열었는데 안 될 때 — 증상별

| 밖에서 보이는 증상 | 원인 | 확인 | 조치 |
|---|---|---|---|
| 연결 시간 초과 / 사이트에 연결할 수 없음 | 규칙이 아직 켜져 있음 **또는** nginx 가 죽음 | `Get-NetFirewallRule -DisplayName Mochang-Site-Pause` / `Get-Service vsp-nginx` / `netstat -ano \| findstr :50001` | `Disable-NetFirewallRule ...` / `Restart-Service vsp-nginx` |
| 502 Bad Gateway | nginx 는 살았는데 `mochang-api`(8000) 가 죽음 | `Get-Service mochang-api`, `C:\logs\mochang-api.err.log` | `Start-Service mochang-api` |
| 화면은 뜨는데 "모델 서버 연결 끊김" / 생성이 전부 실패 | SSH 터널(`mochang-tunnel`) 또는 GPU 서버 vLLM 이 죽음 | `Invoke-RestMethod http://127.0.0.1:8000/health` 의 `llm_reachable`; `Get-Content C:\logs\mochang-tunnel.log -Tail 20` | 터널: `Restart-Service mochang-tunnel`. 로그에 `connect failed: Connection refused` 가 반복되면 터널은 살았고 **서버 쪽 vLLM 이 내려간 것** → `ssh gpu` 후 `kubectl get pods -l serving=qwen` |
| nginx 가 아예 안 뜸 (443·50001 전부 연결거부) | 다른 프로세스가 nginx 포트(특히 8080) 선점 — 2026-09-10 사고 | `netsh interface portproxy show all`, `netstat -ano \| findstr :8080` | 선점 규칙 삭제 후 `sc.exe stop vsp-nginx; sc.exe start vsp-nginx` (PAUSED 는 stop→start 로 푼다) |

## 6. 규칙이 사라졌거나 다시 만들어야 할 때

```powershell
# 일시중지 스위치 (Block)
New-NetFirewallRule -DisplayName "Mochang-Site-Pause" -Direction Inbound -Protocol TCP -LocalPort 50001 -Action Block -Profile Any -Enabled False
# 허용 규칙 (없어졌을 때만)
New-NetFirewallRule -DisplayName "mochang-bot 50001" -Direction Inbound -Protocol TCP -LocalPort 50001 -Action Allow -Profile Any
```

`-Enabled False` 로 만들면 "열린 상태"로 생성된다. 규칙 **삭제**(`Remove-NetFirewallRule`)는 하지 말 것 — 다음 사람이 스위치를 못 찾는다.

## 7. 관련 문서

- 전체 인수인계: 저장소 루트 `인수인계용.txt`
- 운영 인계 안내(점검·백업·복구): `docs/OPERATIONS_HANDOVER.md`
- 장애 기록(2026-09-01 25시간, 2026-09-10 6시간 30분): `docs/SYSTEM_ARCHITECTURE.md` §26, 바탕화면 `vsp자동복구.txt`
