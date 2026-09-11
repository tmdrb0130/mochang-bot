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

