# 모창 운영 인계 안내

기준일: 2026-10-28. 과거 설계 기록과 구분되는 현재 운영 안내다.

## 1. 가장 먼저 확인할 사항

**기존 API 인증정보는 이전 담당자의 개인 계정으로 발급됐다. 사용자는 개인 계정 연동 API를 제거할 예정이라고 알렸다.** 실제 폐기 완료 여부는 확인하지 않았다. 아래 정상 점검 결과는 개인 키 폐기 전 기준이며, 이후 외부 API의 정상 동작을 보장하지 않는다.

다음 담당자는 모창 공용 계정으로 각 서비스의 인증정보를 발급하고 `.env` 값을 교체해야 한다. 변수명과 설명은 `.env.example` 상단에 있다.

| 서비스 | 변수 |
|---|---|
| OpenRouter | `OPENROUTER_API_KEY` |
| 네이버 검색 | `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` |
| 공공데이터포털 | `DATA_GO_KR_API_KEY` — K-Startup 창업지원사업 공고·소상공인 상권정보 API 활용 신청 필요 |
| KOSIS | `KOSIS_API_KEY` |
| 한국은행 ECOS | `ECOS_API_KEY` |
| KIPRISPlus | `KIPRIS_API_KEY` — 미사용이면 사용 여부·상품부터 확인 |

승인·한도·결제 조건을 확인하고 키를 교체한 뒤 관리자 PowerShell에서 `Restart-Service mochang-api`를 실행한다. `/health`만으로 외부 검색 API의 인증 성공을 판단하지 말고 각 기능을 실제 확인한다. 키를 문서에 붙이거나 `.env`를 커밋하지 않는다.

본문 생성은 현재 GPU Qwen이고 OpenRouter 폴백은 꺼져 있다. 키 폐기 후 Qwen 연결이 정상이어도 검색·통계·공고 조회가 실패할 수 있다. 이번 점검에서는 외부 API별 실호출이나 실제 초안 생성을 수행하지 않았다.

## 2. 주소와 서비스 구조

| 용도 | 주소·구성 |
|---|---|
| 공개 웹 | `https://bustartup.kr:50001/` |
| Windows API | `http://127.0.0.1:8000` |
| 상태 | `http://127.0.0.1:8000/health` |
| 작업 큐 | `http://127.0.0.1:8000/jobs` |
| 관리자 대시보드 로컬 | `http://127.0.0.1:8001/` |
| Windows의 모델 접속점 | `http://127.0.0.1:30801/v1` |
| GPU 서버 | SSH 별칭 `gpu`, `Precision-7960-T2`, 내부 IP `192.168.8.55` |

브라우저 → Windows nginx → API → SSH 터널 → GPU 서버 k3s의 vLLM Qwen 두 배포 구조다. 현재 모델 식별자는 `Qwen/Qwen3.8-27B-FP8`이다.

Windows 서비스 `mochang-api`, `mochang-dashboard`, `mochang-tunnel`, `vsp-nginx`는 자동 시작 설정이다. SSH 터널은 현재 NSSM 서비스이며 로그인 콘솔에서 수동으로 실행하는 과거 구성과 다르다.

## 3. 일상 점검과 재시작

Windows PowerShell:

```powershell
Get-Service mochang-api,mochang-dashboard,mochang-tunnel,vsp-nginx
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/jobs
Get-ScheduledTaskInfo -TaskName mochang-db-snapshot
Get-ScheduledTaskInfo -TaskName mochang-watchdog
```

`ok`, `llm_reachable`, `storage.enabled`, `storage.archive`를 확인한다. 정상 HTTP 응답만으로 모델·검색·DB 전체 정상이라고 결론 내리지 않는다. `llm_reachable`에는 캐시가 있어 터널 복구 직후 잠시 이전 결과가 보일 수 있다.

서비스가 중단됐을 때 관리자 PowerShell의 `Start-Service 서비스명`, 실행 중이지만 재시작이 필요한 경우 `Restart-Service 서비스명`을 사용한다. 백엔드 재시작 전 작업 큐를 확인한다. 바탕화면의 `add-mochang-service.ps1`은 서비스 재등록용이며 일상 재시작용이 아니다. `setup-services.ps1`, `fix-services.ps1`은 다른 VSP 서비스에도 영향을 준다.

로그는 `C:\logs\mochang-api.log`, `C:\logs\mochang-api.err.log`, `C:\logs\mochang-tunnel.log`와 프로젝트 `backend/.timing.jsonl` 등을 확인한다.

GPU 조회:

```powershell
ssh gpu
```

접속 후 Linux 명령:

```bash
df -h / /app
findmnt /app
kubectl get deploy -n default
kubectl describe node precision-7960-t2
nvidia-smi
```

## 4. 백업: 실제 사본과 전체기록 DB 구분

- 운영 DB: `backend/.data/mochang.sqlite`.
- 전체기록 DB: `backend/.data/mochang-archive.sqlite`. 같은 PC의 전체기록 보관용이며 재해 대비 백업과 다르다.
- 예약 작업: `mochang-db-snapshot`, 매일 04:30.
- 실행: `scripts/db_snapshot.py --remote gpu:mochang-backup`.
- 로컬 사본: `backend/.data/snapshots/mochang-YYYY-MM-DD.sqlite.gz`, 기본 7일 보관.
- 원격 사본: GPU 서버 `/home/user/mochang-backup/`, 기본 30일 보관.
- 스크립트는 운영 DB를 SQLite backup API로 복사한다. 전체기록 DB, `.env`, 첨부파일, 코드 전체를 백업하는 작업이 아니다.

**현재 예약 작업은 `bon505`, LogonType `Interactive`다. 해당 계정이 로그인하지 않은 상태에서의 백업 실행을 보장하지 않는다.** 무인 운영을 원하면 별도 승인된 서비스 계정·자격증명·SSH 키 접근을 마련해 예약 작업을 재구성하고 로그아웃 상태 실행을 검증해야 한다. 이번 작업에서는 설정을 변경하지 않았다.

수동 백업은 프로젝트 루트에서:

```powershell
.\.venv\Scripts\python.exe scripts/db_snapshot.py --remote gpu:mochang-backup
```

종료 코드와 원격 파일 존재를 확인한다. 스크립트 실행 성공 외에도 정기적으로 압축 해제·SQLite 무결성·복원 시험을 해야 한다.

### 복구 절차의 원칙

이번에는 실제 운영 DB를 교체하는 복원 시험을 수행하지 않았다. 복구 시에는 다음 순서로 작업한다.

1. 복구 시점을 정하고 백업을 별도 임시 경로에 다운로드·압축 해제한다.
2. SQLite `PRAGMA quick_check` 또는 `integrity_check`로 사본 무결성을 확인하고 필요한 데이터가 있는지 검수한다.
3. 현재 작업 큐와 데이터 변경을 확인한 뒤 `mochang-api`를 중지하고, DB를 여는 다른 프로세스도 확인한다.
4. 현 운영 DB와 존재하는 `-wal`, `-shm` 파일을 함께 별도 보존한다. 백업 DB 위에 기존 WAL 파일을 남겨 섞지 않는다.
5. 검증한 사본을 실제 설정의 운영 DB 경로에 배치하고 계정의 파일 권한을 확인한다.
6. 서비스를 시작하고 `/health`, 기존 초안 조회, 저장 기능을 검증한다. 이상이 있으면 중지 후 보존한 원본 세트로 되돌린다.

복구는 해당 시점 이후 변경을 되돌리는 작업이므로 운영 담당자의 범위 확인 후 수행한다. 전체기록 DB까지 덮어쓰는 작업과 혼동하지 않는다.

## 5. SSD 이전 및 공간 부족 이력

최신 조회: 시스템 SSD 약 710GiB 사용/180GiB 가용, 추가 SSD 약 158GiB 사용/734GiB 가용.

`/app`의 Hunyuan 가중치 압축 파일 등은 추가 SSD의 아래 폴더로 이전됐다.

```text
/mnt/3c45d9a1-ba58-4236-adf4-7b31d21601f5/hunyuan-app
```

기존 `/app` 경로는 bind mount로 유지한다. `/etc/fstab`에 자동 마운트와 SSD 선행 의존성이 있다. 재부팅 시험은 하지 않았지만 실제 마운트 및 systemd 의존성은 검증했다. 원본 공간 회수와 SHA-256 검증 완료. Docker 저장소 388GiB와 연구 프로젝트 등은 시스템 SSD에 그대로 유지하기로 했다.

이전 직후 여유는 225GiB였으나 대기하던 Qwen·Open WebUI·Vane 자동 실행으로 k3s 이미지·스냅샷이 약 45GiB 늘어 현재 180GiB다. `/app` 중복 사본 때문이 아니다.

디스크 압력은 GPU 여유와 별개다. 현재 kubelet은 여유 5% 기준과 추가 회수 10% 설정이 있어 부족 상태 해소 목표가 약 141GiB다. 200GiB는 이번에 제안한 운영 여유 목표이지 고정 요구 사양은 아니다. 상세는 [저장공간 조사 문서](GPU_SERVER_STORAGE_AUDIT_2026-10-28.md)를 참고한다.

## 6. 2026-10-28 실제 점검 결과와 한계

| 항목 | 결과 |
|---|---|
| Windows 4개 서비스 | 모두 Running/Automatic |
| 공개 웹·로컬 대시보드 | HTTP 200 |
| API 상태·Qwen 연결 | `ok=true`, `llm_reachable=true` |
| 운영/전체기록 저장 상태 | enabled/archive=true, error_count=0 |
| 생성·조사 큐 | 대기·진행 0, 보고된 오류 0 |
| k3s Qwen 2개·Open WebUI·Vane | 모두 1/1 Ready |
| 백업 예약 | 10월 28일 04:30 실행, 결과 0, 다음 10월 29일 04:30 |
| 감시 예약 | 최근 실행 결과 0, 5분 주기 |
| 최신 로컬·원격 백업 | 둘 다 `mochang-2026-10-28.sqlite.gz`, 5,654,430바이트 |
| 백업 SHA-256 | 로컬·원격 일치 |
| 백업 임시 압축 해제 사본 | SQLite `quick_check=ok` |

백업 SHA-256: `5480d110d4104a6880ecc8eda34efa947a20589f67aef7216b5273ba83554f5d`.

이는 점검 시점의 상태다. 개인 API 키 폐기 이후 외부 API 정상 여부, 실제 생성 전체 흐름, 운영 DB 복원, 재부팅 후 무인 백업은 별도 검증이 필요하다. 서비스 실행 상태만으로 이 항목들을 모두 검증한 것으로 간주하지 않는다.

## 7. 별도로 인계할 접근권한

모창 공용 API 계정 외에도 Windows 서비스 실행 계정, GPU SSH 계정과 키, 예약 작업 계정, 도메인·TLS 인증서·nginx 관리, 백업 접근권한이 필요하다. 비밀번호나 개인키를 이 문서에 기록하지 말고 승인된 방식으로 전달한다.

기존 문서는 날짜별 변경 이력이 섞여 있다. [문서 시작점](README.md)을 먼저 읽고, 상세 구조는 `SYSTEM_ARCHITECTURE.md`, 설계 배경은 `PROJECT_CONTEXT.md`를 참고한다.
