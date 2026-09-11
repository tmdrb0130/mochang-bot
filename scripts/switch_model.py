"""운영 모델 갈아끼우기 — config.yaml 을 준비된 설정 파일로 바꾼다 (코드·프론트 수정 없음).

    .venv/Scripts/python scripts/switch_model.py            # 현재 설정과 고를 수 있는 것 보기
    .venv/Scripts/python scripts/switch_model.py qwen       # backend/config.yaml.qwen  → config.yaml
    .venv/Scripts/python scripts/switch_model.py llama      # backend/config.yaml.llama.bak → config.yaml

바꾼 뒤에는 관리자 PowerShell 에서  nssm restart mochang-api  를 해야 반영된다 (이 스크립트는 재시작하지 않는다).
직전 config.yaml 은 config.yaml.prev 로 남긴다. 새 모델을 추가하려면 config.yaml.<이름> 파일을 만들면 된다.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import yaml

sys.stdout.reconfigure(encoding="utf-8")
BACKEND = Path(__file__).resolve().parent.parent / "backend"
CONFIG = BACKEND / "config.yaml"
CHOICES = {"qwen": "config.yaml.qwen", "llama": "config.yaml.llama.bak"}


def describe(path: Path) -> str:
    try:
        c = yaml.safe_load(path.read_text(encoding="utf-8"))
        rl = (c.get("research") or {}).get("llm") or c["llm"]
        return f"본문 {c['llm']['model']} @ {c['llm']['base_url']} | 조사 {rl['model']} @ {rl['base_url']} | 워커 {c.get('max_workers')}/{rl.get('max_workers')}"
    except Exception as e:
        return f"(읽기 실패: {e})"


def main() -> int:
    for name, fn in list(CHOICES.items()):
        if (BACKEND / fn).exists():
            print(f"  {name:6} ← {fn}: {describe(BACKEND / fn)}")
    for extra in sorted(BACKEND.glob("config.yaml.*")):
        if extra.name not in CHOICES.values() and extra.suffix not in (".prev", ".bak"):
            print(f"  {extra.name[len('config.yaml.'):]:6} ← {extra.name}: {describe(extra)}")
    print(f"\n현재 config.yaml: {describe(CONFIG)}")
    if len(sys.argv) < 2:
        return 0
    name = sys.argv[1]
    src = BACKEND / CHOICES.get(name, f"config.yaml.{name}")
    if not src.exists():
        print(f"없음: {src}")
        return 1
    shutil.copy(CONFIG, CONFIG.with_suffix(".yaml.prev"))
    shutil.copy(src, CONFIG)
    print(f"\n교체됨 → {describe(CONFIG)}\n이제 관리자 PowerShell 에서:  nssm restart mochang-api")
    return 0


if __name__ == "__main__":
    sys.exit(main())
