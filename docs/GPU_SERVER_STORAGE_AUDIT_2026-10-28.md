# GPU 서버 시스템 SSD 저장공간 조사

## 최신 후속 확인: 시스템 SSD 여유 180GiB 및 모델 연결 복구

사용자가 이전 후 여유가 225GiB에서 180GiB로 줄어든 이유를 요청하여 추가 조사했다. 시스템 SSD는 710GiB 사용, 180GiB 가용이며 추가 SSD의 `/app`은 158GiB로 유지된다.

증가 위치는 **`/var/lib/rancher/k3s` (이전 약 1.8GiB → 현재 약 48GiB)**다. 공간 압력 해소 후 대기 중인 vLLM·Open WebUI·Vane이 이미지를 다운로드하고 컨테이너 파일시스템을 준비한 이벤트를 확인했다. 현재 k3s containerd의 콘텐츠 blob은 약 14GiB, overlayfs 스냅샷은 약 34GiB다. 기존 Docker 저장소 `/var/lib/containerd` 388GiB 및 m3s 126GiB, 모델 폴더 84GiB는 기존 표시값과 같다. 단위 반올림 때문에 1.8→48GiB와 df 증가량 약 45GiB가 정확히 일치하지는 않는다.

Qwen 두 배포, Open WebUI, Vane 모두 1/1 Ready로 실행 중임을 확인했다. Windows 모창 백엔드 `/health`도 `ok: true`, `llm_reachable: true`로 확인했다. 실제 생성 요청은 수행하지 않았다. 아래 이전 완료 직후 기록의 225GiB 및 모델 0/1은 당시 상태를 남긴 것이다.

## 후속 작업 완료: `/app` 추가 SSD 이전 (2026-10-28)

사용자 승인에 따라 아래 이전을 완료했다. **이 절 아래의 최초 조사 수치는 이전 전 스냅샷**이며 현재 배치는 다음과 같다.

| 항목 | 이전 전 | 이전 후 |
|---|---:|---:|
| 시스템 SSD 사용 | 822GiB | 665GiB |
| 시스템 SSD 가용 | 68GiB | **225GiB** |
| 추가 SSD 사용 | 거의 없음 | 158GiB |
| 추가 SSD 가용 | 891GiB | 734GiB |

- 실제 저장 폴더: `/mnt/3c45d9a1-ba58-4236-adf4-7b31d21601f5/hunyuan-app`.
- 기존 서비스 경로: `/app` 유지. `findmnt`로 `/dev/nvme0n1[/hunyuan-app]` 연결 확인.
- 이전 파일: `hunyuan_weights.tar` (168,657,793,024바이트), `image_edit.png` (1,803,821바이트).
- `rsync -aHAX --numeric-ids` 복사 후 원본·복사본 SHA-256, 크기·소유자·권한·수정시간 일치 확인. 메타데이터 dry-run 차이 없음.
- 전환 후 검증된 시스템 SSD 원본 두 파일을 제거하여 실제 공간을 회수했다. 임시 원본 디렉터리도 제거 완료.
- `/etc/fstab` 백업: `/etc/fstab.before-app-ssd-20261028`.
- 검증 해시 기록: `/var/tmp/app-ssd-migration-20261028.json`.
- fstab에 아래 항목을 추가하고 systemd를 다시 읽혔다. `app.mount`가 추가 SSD 마운트를 Requires/After로 기다리는 것을 확인했다.

```fstab
/mnt/3c45d9a1-ba58-4236-adf4-7b31d21601f5/hunyuan-app /app none bind,x-systemd.requires-mounts-for=/mnt/3c45d9a1-ba58-4236-adf4-7b31d21601f5 0 0
```

검증 SHA-256:

```text
hunyuan_weights.tar 4f0df3fa24db5a963b83e093bf9fe17e5573bc25473c50d45379b2e67f4036b2
image_edit.png     e2a7f13bd86fd10d5d71f749c5a119710e0b367c212385a4062304c72f739078
```

기존 Hunyuan 컨테이너·Docker·k3s·Jupyter를 중지하지 않았다. Hunyuan API `/openapi.json` HTTP 200, 기존 Jupyter와 커널 및 Hunyuan API PID 유지 확인. 컨테이너 설정과 서비스 코드 변경 없음. 빌드 캐시·연구 데이터·모델 파일은 정리하지 않았다.

재부팅은 수행하지 않았다. 자동 마운트 설정으로 실제 `/app` 마운트에 성공했고 systemd 의존성을 확인한 수준이다. 최종 조회 당시 Qwen 두 개와 Open WebUI·Vane은 여전히 0/1이었다. 공간 확보 완료와 모델 재가동 완료는 별개이며 이 작업에서 모델 정상화까지 확인한 것은 아니다.

---

조사일: 2026-10-28, Asia/Seoul. 마지막 전체 용량 조회: 14:32 KST, 이후 세부 폴더·코드 추가 확인.
대상: SSH 별칭 `gpu`, 호스트 `Precision-7960-T2`.
목적: 저장된 프로젝트·모델·컨테이너의 정체와 용량, 현재 사용 여부를 파악하고 이동·정리 대상을 판단한다.

이 문서는 읽기 전용 조사 결과다. 원격 파일 삭제·이동, 서비스 재시작, 모델 실행, 설정 변경은 하지 않았다. 용량은 조사 시점의 값이며 실행 중인 연구 작업에 따라 달라질 수 있다.

## 1. 핵심 현황

- 시스템 SSD 파일시스템: 약 **937GiB**, 사용 **822GiB**, 일반적으로 사용할 수 있는 여유 **68GiB**.
- 별도 NVMe SSD 1개와 HDD 2개는 마운트되어 있으며 사실상 비어 있다.
- 현재 실행 중인 주요 작업은 **Hunyuan3D API와 m3s Jupyter 연구 환경**이다.
- Qwen 두 배포는 각각 실행 목표 1개지만 실행 0개, 대기 상태다. 사용자가 이전에 의도적으로 내려두었다는 이력과, 현재 디스크 압력 때문에 스케줄링이 차단되는 현상은 별개다. 최초 중단 원인을 디스크 부족으로 단정하지 않는다.
- 가장 범위가 작은 정리 후보는 **Hunyuan3D 이미지 제작용 Docker 빌드 캐시 144.5GB ≈ 134.6GiB**다. 실제 회수량은 정리 후 확인해야 한다.

## 2. 용량 해석 방법과 조사 한계

1. Linux `du -h`, `df -h`의 G는 이 문서에서 GiB로 표기한다. Docker의 GB는 십진 단위다. **1GB ≈ 0.931GiB**이므로 서로 혼용하지 않는다.
2. 상위 폴더와 하위 폴더는 중복이다. `/var/lib/containerd` 388GiB에 컨테이너와 빌드 캐시가 포함된다. 이들을 더하지 않는다.
3. Docker의 이미지·캐시·컨테이너 집계는 파일 공유와 집계 기준이 달라 `du`의 물리 사용량과 정확히 일치하지 않는다. 컨테이너 `virtual` 크기는 기반 이미지까지 포함하므로 별도로 더하지 않는다.
4. `m3s`에는 하드링크가 확인됐다. `du`는 같은 파일을 한 번만 세므로 조회 순서와 범위에 따라 하위 폴더에 귀속되는 용량이 달라진다. 개별 조회값을 모두 더하거나, 캐시 크기를 전부 삭제 회수량으로 간주하면 안 된다.
5. `df` 전체 크기에서 사용량을 뺀 값과 가용 공간의 차이는 주로 ext4 예약 공간 등이다. 현재 약 47GiB 차이가 있으며, 일반적인 파일 정리 후보가 아니다.
6. 파일의 존재·폴더 이름·실행 중인 프로세스·코드를 확인했다. 모든 파일의 내용이나 중요도를 검수한 것은 아니다. 파일을 현재 열고 있지 않다는 사실은 삭제해도 된다는 뜻이 아니다.

## 3. 물리 디스크 구성

| 장치 | 종류·모델 | 파일시스템 크기 | 사용 | 가용 | 역할 |
|---|---|---:|---:|---:|---|
| `/dev/nvme1n1p2` | Samsung PM9F1 SED 1024GB NVMe SSD의 시스템 파티션 | 937GiB | 822GiB | 68GiB | `/`, OS와 거의 모든 데이터 |
| `/dev/nvme1n1p1` | 같은 SSD의 EFI 파티션 | 약 1.1GiB | 27MiB | 약 1.1GiB | `/boot/efi`, 부팅용 |
| `/dev/nvme0n1` | Samsung PM9F1 SED 1024GB NVMe SSD | 938GiB | 약 28KiB | 891GiB | 추가 SSD, 사실상 비어 있음 |
| `/dev/sda` | Seagate ST2000NM012B-2TD130 HDD | 약 1.8TiB | 약 28KiB | 약 1.7TiB | 추가 HDD, 사실상 비어 있음 |
| `/dev/sdb` | Seagate ST2000NM012B-2TD130 HDD | 약 1.8TiB | 약 28KiB | 약 1.7TiB | 추가 HDD, 사실상 비어 있음 |

추가 디스크의 실제 마운트 경로:

```text
추가 SSD /dev/nvme0n1
/mnt/3c45d9a1-ba58-4236-adf4-7b31d21601f5

HDD /dev/sda
/mnt/3541720b-30c5-46ec-b9a5-3d8aaf56eabb

HDD /dev/sdb
/mnt/091fcd0c-3784-4777-8658-da879b81a963
```

세 디스크 모두 ext4이며 `/etc/fstab`에 UUID 기반 자동 마운트 설정이 있다. 현재 마운트 상태도 확인했다. `fstab`에는 현재 장치 목록에 없는 과거 UUID 항목도 있어, `/mnt`의 모든 디렉터리가 실제 디스크인 것은 아니다. 위의 세 경로가 현재 확인된 추가 디스크다.

`/run`, `/dev/shm` 등 tmpfs는 RAM 기반이다. `lsblk`에 보이는 다수의 loop 장치는 Snap 패키지용이며 별도 물리 디스크가 아니다.

## 4. 시스템 SSD 최상위 구성

아래는 `du -xhd1 /` 기준이다. `-x`로 다른 파일시스템을 제외했다.

| 경로 | 용량 | 내용 |
|---|---:|---|
| `/var` | 395GiB | 컨테이너 저장소, Kubernetes, Snap, 로그 등 |
| `/home` | 161GiB | 사용자 연구 프로젝트, Python 환경, 모델 캐시, 다운로드 |
| `/app` | 158GiB | 대부분 `hunyuan_weights.tar` |
| `/opt` | 85GiB | Qwen·Llama 모델, Open WebUI 데이터 |
| `/usr` | 16GiB | OS 프로그램·라이브러리 |
| `/swap.img` | 약 8GiB | 시스템 스왑 파일. 파일 논리 크기 8,589,934,592바이트 |
| `/boot` | 347MiB | 커널 등 부팅 관련 파일. 별도 EFI 파티션은 제외 |
| `/etc` | 13MiB | 시스템 설정 |
| `/root` | 5.4MiB | 관리자 홈 |
| `/tmp` | 620KiB | 임시 파일 |
| 나머지 최상위 디렉터리 | 소량 | 마운트 지점, 시스템 디렉터리 등 |

반올림 및 집계 기준 때문에 표시된 숫자의 합이 `df`와 완전히 같지는 않다. `/swap.img`는 이전 주요 폴더 요약에 빠져 있던 항목으로, 임의 정리 대상이 아니다.

## 5. `/var`: 컨테이너와 시스템 데이터

### 5.1 실제 저장 위치

| 경로 | 용량 | 의미 |
|---|---:|---|
| `/var/lib/containerd` | 388GiB | Docker와 연결된 containerd의 이미지·스냅샷 저장소 |
| `…/io.containerd.snapshotter.v1.overlayfs` | 337GiB | 컨테이너 및 빌드 단계의 파일시스템 스냅샷 |
| `…/io.containerd.content.v1.content` | 51GiB | 이미지 계층 등 콘텐츠 blob 저장소 |
| `…/io.containerd.metadata.v1.bolt` | 1.3MiB | 관리 메타데이터 |
| `/var/lib/docker` | 약 62MiB | Docker 관리 데이터. DockerRootDir은 이 경로지만 큰 데이터는 위 containerd에 존재 |
| `/var/lib/rancher/k3s` | 1.8GiB | Kubernetes(k3s) 데이터. Docker의 388GiB 저장소와 별도 경로 |
| `/var/lib/snapd` | 4.8GiB | Snap 앱·패키지 |
| `/var/lib/apt` | 약 234MiB | 패키지 관리자 데이터 |
| `/var/log` | 약 571MiB | 시스템·서비스 로그 |
| `/var/cache` | 약 198MiB | 시스템 캐시 |

containerd 내부에는 사람이 붙인 프로젝트 이름 대신 스냅샷 ID·해시가 사용된다. 특정 숫자 폴더를 직접 삭제하면 관리 정보와 불일치할 수 있으므로, 정리 시 Docker 등 해당 관리 도구를 사용해야 한다.

### 5.2 컨테이너 두 개

| 컨테이너 | 쓰기 영역 크기 | 기반 이미지 | 상태·용도 |
|---|---:|---|---|
| `hunyuan3d_env` | 38.1GB ≈ 35.5GiB | `nvidia/cuda:12.4.1-devel-ubuntu22.04` | 실행 중. 이미지에서 3D 형태·텍스처를 만드는 API |
| `hunyuan_image_env` | 약 181GB ≈ 168GiB | `pytorch/pytorch:2.1.0-cuda11.8-cudnn8-devel` | 두 달째 중단. 이름과 `/app` 연결로 Hunyuan 이미지 작업 환경임을 파악했으나 내부 작업물 전체는 조사하지 않음 |

`hunyuan3d_env`는 호스트 `/home/user/Hunyuan3D-2.1`을 컨테이너 `/workspace/Hunyuan3D-2.1`에 연결한다. `python3 -u api_server.py`가 실행 중이며 8080 포트를 공개한다. 조사 당시 GPU 메모리 약 14.3GiB를 사용했다.

`hunyuan_image_env`는 호스트 `/app`을 컨테이너 `/app`에 연결한다. 컨테이너 쓰기 영역 약 168GiB와 호스트 `/app`의 158GiB는 별도 데이터다. 중단 상태라는 이유만으로 내부 파일을 불필요하다고 판단할 수 없다.

### 5.3 완성 이미지

Docker 이미지 총 표시는 76.82GB이고, 그중 회수 가능 표시는 38.69GB다. 공유 계층이 있어 단순 합산하지 않는다.

| 이미지 | Docker 표시 크기 | 연결 상태 |
|---|---:|---|
| `hunyuan3d-fastapi:latest` | 38.7GB | 연결된 컨테이너 없음. 아래 빌드 캐시가 만들어진 이미지 |
| `nvidia/cuda:12.4.1-devel-ubuntu22.04` | 11.4GB | 실행 중인 `hunyuan3d_env`의 기반 |
| `pytorch/pytorch:2.1.0-cuda11.8-cudnn8-devel` | 26.7GB | 중단된 `hunyuan_image_env`의 기반 |
| `nvidia/cuda:12.4.1-base-ubuntu22.04` | 348MB | 연결된 컨테이너 없음. 다른 이미지와 공유 계층 있음 |
| `hello-world:latest` | 25.9kB | 연결된 컨테이너 없음 |

### 5.4 Docker 빌드 캐시의 정체

총 **24개, 144.5GB ≈ 134.6GiB**. 모두 `Reclaimable: true`, `Shared: false`, 마지막 사용은 두 달 전으로 표시됐다.

- 프로젝트: `/home/user/Hunyuan3D-2.1`.
- 원본 저장소: `https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1.git`.
- 빌드일: 2026-08-06, 기록상 성공 8회·실패 1회.
- 결과 이미지: `hunyuan3d-fastapi:latest`. 최종 빌드 로그의 이미지 이름과 이미지 digest를 대조했다.
- 목적: Hunyuan3D를 FastAPI 서버로 패키징. Dockerfile은 `fastapi_app:app`을 Uvicorn으로 실행하며 포트 7860을 사용한다.
- 기반: PyTorch 2.2.0, CUDA 12.1, cuDNN 8 런타임.

| 캐시 단계 | 개수 | Docker 기준 합계 | 내용 |
|---|---:|---:|---|
| `COPY . .` | 7 | 약 96.9GB | 프로젝트 전체를 복사한 여러 빌드의 중간 결과 |
| Python `site-packages` 복사 | 2 | 약 26GB | 호스트 Python 라이브러리를 이미지에 복사 |
| PyTorch·CUDA 기반 파일 | 5 | 약 11.4GB | 기반 이미지에서 가져온 계층 |
| 빌드 입력 파일 사본 | 1 | 약 9.2GB | Docker에 전달된 프로젝트 파일 |
| apt 패키지 설치 | 2 | 약 0.6GB | OpenGL 관련 라이브러리, wget 등 |
| Transformers 설치 | 3 | 약 0.35GB | `transformers<4.40.0` 설치 결과 |
| 기타 | 4 | 수십 KB | Dockerfile·작업 폴더·requirements 관련 |

프로젝트의 `myenv` 자체가 약 8.2GiB다. Dockerfile은 라이브러리 폴더를 한 번 복사한 뒤 프로젝트 전체를 다시 복사한다. `.dockerignore`는 확인되지 않았다. 큰 파일을 포함하는 반복 빌드가 캐시 증가에 기여했다.

현재 소스의 `fastapi_app.py`에는 이미지 배경 제거 → 3D 형태 생성 → 텍스처 적용 → GLB 반환 기능이 있다. `add_text_api.py`에는 텍스트 → 이미지 → 3D 변환 기능을 추가하려는 코드가 있다. **소스가 이미지 빌드 이후에도 변경됐으므로 이 모든 기능이 완성 이미지에 들어 있다고 보장하지 않는다.** 특정 외부 웹사이트와의 최종 연결은 확인되지 않았다.

현재 8080에서 실행하는 컨테이너는 `hunyuan3d-fastapi` 이미지가 아니라 CUDA 기반의 `hunyuan3d_env`다. 빌드 캐시는 기존 완성 이미지의 실행을 위해 필요한 데이터가 아니다. 정리하면 다음 이미지 재빌드 시 중간 단계를 다시 수행한다.

## 6. `/app`: Hunyuan 가중치 보관 파일

| 파일 | 용량 | 확인 내용 |
|---|---:|---|
| `/app/hunyuan_weights.tar` | 158GiB | 8월 13일 수정된 tar 파일. 파일명상 Hunyuan 가중치 보관 파일 |
| `/app/image_edit.png` | 약 1.8MiB | 이미지 파일 |

tar 내부 목록·원본과의 동일성은 조사하지 않았다. 조회 시 해당 tar를 열고 있는 프로세스는 발견되지 않았다. `lsof`는 다른 FUSE 마운트 접근 경고를 출력했으므로 서버 전체 파일 사용 관계를 완전하게 조사한 결과로 확대 해석하지 않는다.

중단된 이미지 컨테이너가 `/app`을 연결한다. 보관 파일이라면 HDD 이동 후보지만, 복원·재설치에 필요한지와 기존 경로를 참조하는 작업이 있는지는 이동 전에 확인해야 한다.

## 7. `/home/user`: 연구와 Hunyuan 작업 공간

| 경로 | 용량 | 역할·상태 |
|---|---:|---|
| `m3s` | 126GiB | 사용 중인 연구 환경, 아래 세부 설명 |
| `.cache` | 22GiB | Hunyuan·pip 등의 사용자 캐시 |
| `Hunyuan3D-2.1` | 9GiB | 실행 중인 Hunyuan3D 컨테이너에 연결된 소스·환경·결과 |
| `Downloads` | 4.1GiB | 대부분 CUDA 설치 파일 |
| `.u2net` | 168MiB | 이름상 배경 제거 모델 관련 파일. 개별 사용 관계 미확인 |
| `mochang-backup` | 168MiB | 모창 원격 백업 폴더. 백업 내용 검수·복원 검증은 이번 범위 밖 |
| `snap` | 173MiB | 사용자 Snap 데이터 |
| `myenv` | 24MiB | 별도의 작은 Python 환경. Hunyuan 프로젝트 내부 `myenv`와 다름 |

### 7.1 m3s 연구 환경

현재 `/home/user/m3s/py311/bin/python`으로 Jupyter Lab과 커널 3개가 실행 중이다. 노트북 루트는 `/home/user/m3s/notebooks`, Jupyter 포트는 로컬 8890이다. 커널 두 개가 GPU 메모리 합계 약 16.4GiB를 사용했다. 어떤 노트북 셀·실험이 실행 중인지는 조사하지 않았다.

| 하위 경로 | 표시 용량 | 내용 |
|---|---:|---|
| `notebooks` | 74GiB | 연구 노트북·코드·실험 결과 |
| `notebooks/conveyor_pdm` | 53GiB | README로 확인한 컨베이어 예지보전 VLA 실험 |
| `notebooks/twinwm` | 22GiB | `pusht`, `grasp_vla`, 논문 그림·압축 파일. 정확한 전체 연구 목표는 미확인 |
| `mamba` | 18GiB | 대부분 `pkgs`, 패키지 저장소. 실행 환경과 공유 파일 여부를 고려해야 함 |
| `data` | 15GiB | LORIS corridor 데이터 등. 개별 파일 전체 분류는 미실시 |
| `py311` | 8.4GiB | 현재 사용 중인 Python 실행 환경 |
| `MASt3R-SLAM` | 3.7GiB | 실시간 조밀 SLAM 및 3D 재구성 연구 코드·파일 |
| `hf_cache` | 2.8GiB | Hugging Face 캐시 경로. 구체적 모델 전체 목록 미조사 |
| `cache` | 전체 조회에서 5.7GiB, 별도 조회에서 14GiB | 하드링크 중복 계산 차이가 있음. 아래 설명 참조 |
| `python` | 98MiB | Python 관련 파일 |
| `bin` | 54MiB | 실행 도구 |
| `cuda128` | 28MiB | CUDA 관련 폴더. 전체 툴킷 설치 크기를 의미하지 않음 |

`cache` 개별 조회에서는 `uv` 9.4GiB, `hf` 4.6GiB로 표시됐다. 같은 파일에 링크가 둘 이상인 사례를 실제 확인했다. **앞서 안내한 cache 5.7GiB와 이번 개별 14GiB는 같은 집계 범위가 아니며, 14GiB 전체를 삭제 회수량으로 계산하면 안 된다.**

#### 컨베이어 예지보전 프로젝트

README 제목은 “컨베이어 예지보전 VLA 실험 (KCI용)”이다. Unity에서 토크·속도오차 등 신호와 장면 영상을 생성하고, SmolVLA가 정상 운전·감속/경보·정지 명령을 내도록 학습한다. 랜덤포레스트·1D-CNN과 비교하는 실험 구성이다.

| 하위 폴더 | 용량 | 용도 |
|---|---:|---|
| `python` | 49GiB | 학습·평가 코드가 있는 작업 트리. 이 49GiB를 순수 소스 코드 크기로 해석하지 않는다. 내부 대용량 파일은 추가 조사 필요 |
| `PdMData` | 1.7GiB | 실험 데이터 |
| `lerobot_data` | 396MiB | 변환된 LeRobot 학습 데이터 |
| `train_run` | 2.2MiB | 실행 관련 결과 폴더 |
| `unity`, `paper_figs` 등 | 소량 | 시뮬레이션 코드·논문 그림 등 |

`twinwm`에는 `pusht` 14GiB, `grasp_vla` 5.2GiB, `pusht_real.zip` 약 2.3GiB, 그 외 압축 파일·그림 코드가 있다. 폴더명으로 작업 분야는 추정할 수 있지만 실험 성과나 불필요 여부는 판단하지 않는다.

### 7.2 사용자 캐시

| 경로 | 용량 | 해석 |
|---|---:|---|
| `.cache/hy3dgen` | 12GiB | Hunyuan3D 관련 캐시. 다운로드된 모델이 포함될 수 있어 전부 임시 찌꺼기로 취급하지 않는다 |
| `.cache/pip` | 10GiB | Python 패키지 설치용 캐시 |
| `.cache/huggingface` | 256KiB | 이 기본 경로는 작다. 실제 대용량 모델은 다른 경로에도 존재 |
| 기타 | 소량 | NVIDIA·데스크톱·썸네일 캐시 등 |

호스트 `.cache`와 컨테이너 내부 `/root/.cache`는 서로 다른 경로다. 실행 중인 Hunyuan 프로세스의 컨테이너 내부 `/root/.cache`도 존재한다. 호스트 캐시 전체의 실사용 여부는 확정하지 않았다.

### 7.3 Hunyuan3D 프로젝트

- `myenv`: 8.2GiB, 프로젝트 폴더 용량 대부분을 차지하는 Python 환경.
- `save_dir`: 약 242MiB, 생성 작업별 결과 폴더.
- `assets`: 약 68MiB, 예제 이미지 등.
- `ckpt`: 약 64MiB.
- `outputs`: 조사 시 4KiB로 표시.
- `api_server.py`: 현재 실행 중인 API. 이미지 입력으로 형태·텍스처 생성, GLB 저장, 다운로드 URL 제공.
- `fastapi_app.py`와 여러 변형 파일: 과거 서버 구현·수정본. 파일이 있다는 사실만으로 현재 실행 중인 것은 아님.

### 7.4 다운로드 폴더

대부분이 `cuda_12.1.1_530.30.02_linux.run` 약 4.1GiB이며 `cuda-keyring_1.1-1_all.deb` 약 4.3KiB도 있다. 설치 프로그램 보관 파일이지 현재 GPU 드라이버 실행 파일 자체는 아니다. 설치 복구에 보관할 필요가 있는지 판단 후 이동 후보로 삼을 수 있다.

## 8. `/opt`: 모창 모델과 관련 서비스 데이터

| 경로 | 용량 | 내용·사용 관계 |
|---|---:|---|
| `/opt/llm-models/qwen3.8-27b-fp8` | 29GiB | 모창 운영 모델로 문서에 기록된 Qwen 파일. 현재 모델 실행은 대기 상태 |
| `/opt/llm-models/qwen25-32b-awq` | 19GiB | 이전 Qwen 2.5 32B AWQ 모델 폴더. 현재 실행 프로세스 미확인 |
| `/opt/llm-models/hub/models--casperhansen--llama-3.3-70b-instruct-awq` | 38GiB | Llama 3.3 70B AWQ 모델 캐시. 현재 실행 프로세스 미확인 |
| `/opt/llm-models/xet` | 21MiB | 모델 다운로드 관련 캐시 경로 |
| `/opt/open-webui-data` | 897MiB | Open WebUI 데이터. 배포는 대기 중 |
| `/opt/vane-data` | 48KiB | Vane 데이터 경로. 배포는 대기 중 |

`/opt/llm-models` 전체는 84GiB로 조회됐다. 개별 값의 반올림·파일 공유 때문에 하위 표시값을 단순 합산하지 않는다. 모델 재실행에 필요한 경로는 실제 배포 설정과 대조한 후 이동해야 한다.

## 9. GPU가 비어 있어도 실행이 막히는 이유

GPU 메모리와 디스크 공간은 별도 자원이다. 현재 Kubernetes 노드에는 `node.kubernetes.io/disk-pressure:NoSchedule`이 있어 새 Pod 배치를 막는다.

실제 kubelet 설정에서 읽은 값:

```yaml
evictionHard:
  imagefs.available: 5%
  nodefs.available: 5%
evictionMinimumReclaim:
  imagefs.available: 10%
  nodefs.available: 10%
evictionPressureTransitionPeriod: 5m0s
```

실제 통계에서 nodefs와 imagefs는 모두 같은 약 937GiB 용량과 약 68GiB 가용 공간을 보고했다. 부족 상태 진입 기준은 약 47GiB지만, 이미 발생한 부족 상태에서 추가 회수 목표 10%가 적용되므로 해소 목표는 합계 약 **141GiB 가용 공간**이다. 현재보다 약 **73GiB 추가 확보**가 필요하다. 로그에서도 지속적인 ephemeral-storage 회수 시도가 확인됐다.

**200GiB는 필수 고정 요구량이 아니라, 모델 이미지·임시 파일 증가를 고려한 이번 운영 여유 목표 제안이다.** 모델 자체 크기와 혼동하지 않는다. 공간 확보만으로 이후 모든 모델 로딩 오류까지 해결된다고 보장할 수 없으며, 부족 상태 해제와 모델 정상 응답을 별도로 확인해야 한다.

현재 대기 중인 배포는 `vllm-qwen`, `vllm-qwen-b`, `open-webui`, `vane`다. 공간 부족이 해제되면 모두 자동 실행을 시도할 수 있다.

## 10. 정리·이동 후보 비교

| 후보 | 기대 효과 | 작업 규모 | 확인할 사항 |
|---|---|---|---|
| Docker 빌드 캐시만 정리 | 표시상 약 135GiB, 여유 약 203GiB 예상 | 가장 작음. 보통 서비스 정지·경로 변경 불필요 | 실제 회수량 확인. 다음 이미지 빌드는 느려질 수 있음 |
| `hunyuan_weights.tar`를 HDD로 이전 | 약 158GiB, 여유 약 226GiB 예상 | 복사·검증·원본 공간 회수 필요 | 압축 파일 용도·참조 경로 확인, 파일 보존 |
| 중단된 이미지 컨테이너 정리 | 쓰기 영역 약 168GiB | 내부 작업물 검수·보관 필요 | 중단은 불필요를 의미하지 않음 |
| containerd 저장소를 추가 SSD로 이전 | 시스템 SSD에서 약 388GiB 분산 | 관련 서비스 중지·일관된 복사·마운트/설정·검증 필요 | 현재 Hunyuan3D 서비스 영향 고려 |
| 모델 폴더를 추가 SSD로 이전 | 약 84GiB 분산 | 경로 유지 마운트 또는 설정 변경 필요 | 대기 중인 모델 자동 실행과 충돌하지 않도록 순서 설계 |
| m3s 이전 | 약 126GiB 분산 | 실행 중 연구 환경 조정 필요 | Jupyter·절대경로·하드링크 보존 |

조사에 근거한 최소 작업 후보 명령은 아래와 같다. **이 문서 작성 중 실행하지 않았다.**

```bash
# GPU 서버에서 7일 이상 사용하지 않은 빌드 캐시만 정리
docker builder prune --all --filter "until=168h"
```

범위가 더 넓은 `docker system prune`과 다르다. 중단된 컨테이너·연구 원본까지 일괄 정리하는 명령으로 바꾸지 않는다. 실행 후 `df`, `DiskPressure`, 대기 Pod와 실제 API 응답으로 결과를 검증한다.

## 11. 근거와 미확인 범위

조사 명령: `lsblk`, `df`, `du -x`, `find`, `docker ps --size`, `docker inspect`, `docker system df -v`, `docker buildx du --verbose`, `docker buildx history ls/inspect/logs`, `docker history`, `nvidia-smi`, `ps`, `lsof`, Kubernetes configz·stats·배포·노드·이벤트 조회, k3s journal 조회.

확인한 설명 파일·소스:

- `/home/user/Hunyuan3D-2.1/Dockerfile`
- `/home/user/Hunyuan3D-2.1/fastapi_app.py`
- `/home/user/Hunyuan3D-2.1/api_server.py`
- `/home/user/Hunyuan3D-2.1/add_text_api.py`
- `/home/user/Hunyuan3D-2.1/update_cors.py`
- `/home/user/m3s/notebooks/conveyor_pdm/README.md`
- `/home/user/m3s/MASt3R-SLAM/README.md`
- `/etc/fstab`, `/etc/containerd/config.toml`

공식 동작 설명:

- [Kubernetes 디스크 압력 및 최소 회수량](https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/#minimum-eviction-reclaim)
- [Docker builder prune](https://docs.docker.com/reference/cli/docker/builder/prune/)

미확인 범위: 중단 컨테이너 내부의 모든 파일, 가중치 tar 내부와 원본 중복 여부, 연구 폴더별 모든 체크포인트·데이터의 중요도, 현재 실행 노트북의 정확한 실험 내용, 과거 Hunyuan 이미지의 모든 소스 내용, 최종 외부 서비스 연결, 삭제 후 실제 회수량. 해당 부분은 추가 검수 없이 삭제 대상으로 확정하지 않는다.

## 12. `/app` 이전 후 남은 Hunyuan 데이터 추가 조사

2026-10-28 후속 읽기 전용 조사. `/app` 외의 추가 이동·삭제·컨테이너 재시작은 하지 않았다.

### 실제 사용 경로

```text
시스템 SSD /home/user/Hunyuan3D-2.1
  → Docker bind mount /workspace/Hunyuan3D-2.1
  → python3 -u api_server.py (8080 포트)
  → outputs/에 GLB 결과 저장

시스템 SSD /var/lib/containerd의 컨테이너 쓰기 영역
  → hunyuan3d_env 내부 /root/.cache/hy3dgen
  → hunyuan3d_env 내부 /root/.cache/huggingface/hub
  → 시작 시 형태·텍스처 모델을 읽어 GPU로 로드
```

실행 컨테이너의 호스트 bind mount는 프로젝트 폴더 하나다. 호스트 `/home/user/.cache/hy3dgen`과 컨테이너 `/root/.cache/hy3dgen`은 별개다. 해당 호스트 캐시를 옮겨도 컨테이너 내부 모델은 함께 옮겨지지 않는다.

### 실행 컨테이너 내부 세부 용량

아래는 기존 컨테이너 쓰기 영역 약 35.5GiB 등에 포함되는 내부 조회값이며 별도로 합산하지 않는다. 기반 이미지 파일도 섞일 수 있다.

| 컨테이너 내부 경로 | 크기 | 용도·근거 |
|---|---:|---|
| `/root/.cache/hy3dgen/tencent/Hunyuan3D-2.1/hunyuan3d-dit-v2-1` | 6.9GiB | 형태 생성 모델. 로딩 코드가 `HY3DGEN_MODELS` 미설정 시 `~/.cache/hy3dgen` 사용 |
| `/root/.cache/huggingface/hub/models--tencent--Hunyuan3D-2.1` | 6.5GiB | 텍스처 생성 관련 모델. multiview 코드가 `hunyuan3d-paintpbr-v2-1`을 다운로드·로드 |
| `/root/.cache/huggingface/hub/models--facebook--dinov2-giant` | 4.3GiB | 텍스처 파이프라인에서 사용하는 시각 특징 모델 설정 |
| `/root/.cache/pip` | 6.3GiB | 패키지 설치 캐시 |
| `/root/.u2net/u2net.onnx` | 168MiB | 배경 제거 모델 파일. 현재 API 경로에서 실제 호출하는지는 미확인 |
| `/usr/local/lib` | 11GiB | Python 라이브러리 등 실행 환경. 전부 Hunyuan 전용인지 개별 검수하지 않음 |
| `/usr/local/cuda-12.4` | 4.7GiB | CUDA 툴킷. 기반 이미지 파일 포함 가능 |

프로젝트의 `ckpt/RealESRGAN_x4plus.pth` 64MiB는 텍스처 파이프라인 설정이 참조하는 화질 개선 모델이다. 프로젝트 폴더 이전 시 함께 보존해야 한다.

### 중단된 이미지 컨테이너의 추가 근거

`docker diff hunyuan_image_env`에서 `/root/.cache/huggingface/modules/transformers_modules/HunyuanImage_hyphen_3_hyphen_Instruct` 및 `hunyuan_image_3_pipeline.py`, 모델 구성·토크나이저·이미지 처리 코드가 확인됐다. 따라서 HunyuanImage-3-Instruct 계열 이미지 생성/편집 환경의 흔적이 있다. 다만 쓰기 영역 약 168GiB 전체의 용량 배분과 실제 실행 이력은 아직 확인하지 않았다. 조사를 위해 중단 컨테이너를 기동하지 않았다.

### 이전 가능성과 작업 범위

| 대상 | 이전 가능 여부 | 권장 접근·주의점 |
|---|---|---|
| 호스트 프로젝트 9GiB | 가능 | Hunyuan API 및 해당 컨테이너 중지 → SSD로 복사·검증 → 기존 호스트 경로 bind mount → 컨테이너와 API 재실행. 살아 있는 컨테이너의 기존 bind mount가 새 연결을 자동 추종한다고 가정하지 않음 |
| 호스트 `.cache/hy3dgen` 12GiB | 가능 | 기존 경로를 유지하는 bind mount 가능. 현재 컨테이너는 이 호스트 경로를 연결하지 않음. 조사 시 열린 파일 없음이나 다른 작업의 향후 사용 가능성은 남음 |
| 호스트 `.u2net` 168MiB | 가능 | 사용 관계 확인 후 이전 가능하나 확보 용량 작음 |
| 실행 컨테이너 내부 모델 캐시 약 18GiB | 가능, 추가 설계 필요 | SSD에 외부 모델 폴더를 만들고 컨테이너에 연결하려면 컨테이너 재생성 등 구성 변경 필요. 원본 쓰기 영역과 설치 환경 보존 필수. 단순히 새 mount로 덮으면 기존 데이터 공간이 남을 수 있음 |
| 중단된 이미지 컨테이너 약 168GiB | 가능, 추가 설계 필요 | 컨테이너 환경을 보존하는 이관 또는 전체 런타임 저장소 이전. 일반 폴더처럼 snapshot 디렉터리만 이동하지 않음 |
| 빌드 캐시 약 135GiB | 이전보다 정리 후보 | 재빌드용 중간 결과. 유지가 필요하면 Docker 저장소 이전에 포함되지만 이전할 실익은 사용자가 판단 |
| containerd 저장소 전체 약 388GiB | 가능 | Docker·containerd 관련 서비스 중지와 저장소 이전 필요. Hunyuan만이 아니라 기반 이미지·hello-world 등 공용 데이터도 포함. k3s의 별도 런타임 경로와 구분 |

호스트 프로젝트의 `myenv` 약 8.2GiB는 과거 이미지 빌드가 복사했던 Python 환경이다. 현재 API는 컨테이너 Python으로 실행하며 이 `myenv`에서 열린 파일은 발견되지 않았다. 향후 호스트 실행·재빌드에서 쓸 수 있으므로 임의 삭제 판정은 하지 않는다.

추가 SSD는 `/app` 이전 후 약 734GiB 가용이다. containerd 전체 약 388GiB + 프로젝트 약 9GiB + 호스트 Hunyuan 캐시 약 12GiB를 이전하는 경우 표시 용량상 약 409GiB가 추가되어 약 325GiB가 남는 계산이다. 실제 이관 전 최신 용량·공유 파일·복사 방식과 서비스 정지 범위를 다시 확인해야 한다.
