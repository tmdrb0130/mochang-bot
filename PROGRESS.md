# PROGRESS — 2026-10-28 저녁 (인수인계 문서 일괄 작성 · setup-services.ps1 정리)

> 이 절이 최신. 코드 변경 0, 실호출 0, `pytest -q` **494 passed**. 운영 서비스는 건드리지 않았다.

## 한 것

1. **`인수인계용.txt`(저장소 루트)** — 다음 관리자가 이 파일만 보고 전체를 파악하도록 썼다. 지금 상태(17:50 직접 조회) · 켜고 끄는 명령 ·
   외부 접속법 · 전체 아키텍처(운영 PC / GPU 서버 / SSH 터널) · 데이터·백업 · 장애 플레이북 · 배포 절차 · 넘겨받을 권한 · 미결 11건 · 문서 지도 · 용어.
2. **`docs/SITE_PAUSE.md`** — 방화벽 규칙 `Mochang-Site-Pause`(TCP 50001 Block) 로 사이트를 닫고 여는 절차와 증상별 대처.
   바탕화면 `모창봇 명령어.txt` 두 줄의 상세판.
3. **바탕화면 `setup-services.ps1` 정리** (실행은 안 함 — 돌아가는 서비스를 내렸다 올리는 스크립트라 실행할 이유가 없다).
   구문 오류는 0 이었지만 내용이 **실제 등록값과 어긋나** 있었다: `vsp-spring`/`vsp-front` 를 `mvnw.cmd`/`npm.cmd` 직접 호출로
   등록하는데 지금은 `C:\Users\bon505\svc\spring.bat`/`front.bat` 런처다(따옴표 전달 문제로 fix-services.ps1 이 생겼던 바로 그 지점).
   런처로 맞추고, 인자를 전부 배열로, 프로세스를 죽이기 전에 서비스를 먼저 정지(NSSM 5초 재기동 경합 제거), nginx.conf 문법 검사를
   다운타임 전에, 끝난 일회성 단계(`front.bat.new`) 삭제, 실행 전 `yes` 확인(`-Force` 로 생략), 50001 검증 추가. 이전 판 `*.bak-20261028-1750`.
4. `docs/README.md` 시작점에 위 두 문서 연결. 직전 세션의 미커밋 문서(`OPERATIONS_HANDOVER.md`, GPU 저장공간 조사, `.env.example` 인계 주석)도 함께 커밋.

## 직접 확인한 사실 (문서에 반영)

- **사이트는 일시중지 상태**: `Mochang-Site-Pause` Enabled. `mochang-api`·터널·GPU 모델은 전부 정상. 최근 7일 스냅샷 크기가 전부
  5,654,430 B 로 동일 = 서비스 DB 가 일주일간 안 바뀜 = 실사용 없음(일시중지와 부합).
- **`.env` 키가 오늘 17:25 에 전부 비워졌다.** `mochang-api` 는 10-16 부팅 때 떠서 아직 옛 키를 메모리에 들고 있다 →
  **다음 재시작부터 네이버·공공데이터 조사가 빠지고 ddgs 만 남는다.** 공용 계정으로 재발급해 채우면 복귀.
- GPU 서버: Qwen 2개 Running(24h 전 기동), DiskPressure 오늘 16:26 해제, **Evicted 파드 45개 잔존**(정리 명령은 인수인계용 §13).
  시스템 SSD 180G 가용. vLLM NodePort 30801 은 외부에서 닫혀 있음(확인) — SSH 키가 유일한 입구.
- Ollama(bge-m3)는 서비스가 아니라 **로그인 세션의 앱 프로세스** — 숨은 의존성(없으면 벡터DB 만 건너뜀).
- 저장소는 GitHub **public**. 문서에 비밀값·개인정보가 없는지 전수 검색: API 키 값 0건. GPU 서버 공인 IP·SSH 계정명은 내가 쓴 초안에
  들어갔던 것을 **지웠다**(이 PC `~\.ssh\config` 가 유일한 자리). `k8s/vane-deployment.yaml` 의 SearXNG `secret_key` 는 9월부터 커밋돼 있음(미사용 기능, 저위험 — 결정 필요).

## 결정 필요

- `k8s/vane-deployment.yaml` 의 `secret_key` 를 자리표시자로 바꿀지 (Vane 은 꺼져 있어 영향 없음. 바꾸면 Vane 재배포 때 세션 키만 달라진다).
- 사이트를 다시 열 시점과, 열기 전 API 키 재발급을 먼저 할지.

## 다음 순서 (인수인계용.txt §13 과 동일)

1. 외부 감시 등록 2. API 키 재발급 → `.env` → 재시작 3. Evicted 파드 정리 4. GPU 디스크 여유 5. 백업 예약을 로그인 무관하게
6. 복원 리허설 7. TLS 만료일 8. 운영 브랜치 → main 정리 9. 잔재 정리 10. portproxy 재발 확인 11. Ollama 서비스화

---

# PROGRESS — 2026-09-11 밤 (DB 이름 정리: backup → archive)

> 이 절이 최신. 코드·문서·파일 이름 개명 + 서비스 재시작 완료. 실호출 0.

## 왜 바꿨나 — 이름이 실제와 반대였다

사용자 지적: "백업 DB 라는 건 사실 개발용 db잖아, 테스트도 저장되고."

확인해 보니 그대로였다.

| 파일 | 실제 내용 | 성격 |
|---|---|---|
| `mochang.sqlite` | 실사용만 (`is_test=1` **0건**) — 272건 | 운영 데이터 |
| `mochang-backup.sqlite` | **전부** — 실사용 277 + 테스트 204 = 481건 | 백업이 **아님** |
| `snapshots/*.sqlite.gz` → GPU 서버 | **서비스 DB** 를 뜬 것 (`src = _path(st.url)`) | **진짜 백업** (로컬 7일·원격 30일) |

`mochang-backup.sqlite` 는 같은 PC 같은 폴더에 있어 **디스크·PC 가 죽으면 서비스 DB 와 같이 죽는다** — 백업의 요건을 애초에 못 갖췄다.

**이 혼동이 실제 판단에 끼어들었다.** 실사용 초안 5건을 지울 때 내가 "백업 DB 에 남아 있으니 복구 가능"이라고 말했는데,
그건 **백업이라서가 아니라 전체기록이라서** 남아 있던 것이다.

## 바꾼 것

| 전 | 후 |
|---|---|
| `mochang-backup.sqlite` | `mochang-archive.sqlite` |
| `storage.backup_url` | `storage.archive_url` |
| `MOCHANG_BACKUP_DATABASE_URL` | `MOCHANG_ARCHIVE_DATABASE_URL` |
| `Storage.backup` | `Storage.archive` |
| `drafts_delete.py --backup` | **`--archive`** |
| `db_copy_to_backup.py` | `db_copy_to_archive.py` |
| `/health.storage.backup` | `.archive` |
| 문서의 "백업 DB" | "**전체기록 DB**" |

**건드리지 않은 것**: GPU 서버 원격 디렉터리 `gpu:mochang-backup` 과 `sqlite3 backup API` — 둘 다 **진짜 백업**을 뜻한다.

## 안전장치 둘

1. **옛 설정 키·환경변수를 폴백으로 계속 읽는다** (`backup_url`, `MOCHANG_BACKUP_DATABASE_URL`).
   예전 설정으로 뜨면 전체기록이 **조용히 꺼져** 테스트 기록이 사라진다 — 그 사고만 막았다.
2. 파일 이름 변경은 **서비스 정지 중에** 했다. config 가 이미 새 이름을 가리키므로,
   파일을 안 바꾸고 재시작하면 **빈 전체기록 DB 가 새로 생기고** 481건이 옛 파일에 고아로 남는다.

## 검증

- `pytest -q` **494 passed**
- 개명 후 무결성 `ok` · 초안 481(테스트 204) · 생성문 3,644 · 조사 3,799 · 스키마 v6
- 재시작 후 `/health.storage.archive: True` · 빈 DB 새로 안 생김 · 서비스 272 / 전체기록 481

## 같은 날 함께 한 것 (앞 절들 참고)

- 실사용 테스트 초안 5건을 **서비스 DB 에서만** 삭제(`is_test=0` 이라 `drafts_delete.py` 가 못 지우는 행 — 직접 SQL).
  삭제 직전 스냅샷을 떴고 전체기록 DB 에는 그대로 남아 있다. 277 → 272건.
- 구조 변경 전후 비교(같은 아이디어 3건): **인테이크 158.7·139.5초 → 75.8초**,
  그중 **조사 110.7·88.5초 → 25.8초**. 카드 생성(48~51초)과 문항 생성은 안 변했다 — 손대지 않았으니 당연하다.
  긴 문항 평균 글자 1,502·1,698 → 1,606 으로 **품질 퇴행 신호 없음**(다만 3건은 표본이 너무 작다).

## 다음 순서

1. **외부 감시 등록** — 여전히 1순위.
2. 조사 25.8초의 다음 표적: **fetch 와 추출 겹치기**(품질 위험 0). 지금은 `collect_pages` 가 전부 끝난 뒤 추출이 시작한다.
3. 카드 생성 50초는 **더 줄일 실익이 작다** — 스트리밍으로 약 6초에 한 장씩 도착하는데 학생은 한 장에 10~20초를 쓴다.
   첫 카드(25초)만이 실제 대기이고, 그 안은 조사 + preamble(summary·slots) 이다.
   (`slots[].known` 다이어트는 실측 58자 = 전체의 1% 라 **효과 없음**. 앞서 "8~10초" 라고 한 내 추정은 틀렸다.)
4. 원본 `.vectorstore/`(362MB) 정리 — 며칠 뒤.
5. `tests/test_pipeline.py.before-fix` 잔재, 전체기록 DB 의 `is_test=1` 스모크 초안 정리.

---

