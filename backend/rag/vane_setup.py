"""Vane(구 Perplexica) 컨테이너에 브라우저 없이 모델 제공자를 등록하는 스크립트.

Vane 은 설정을 컨테이너 볼륨의 /home/vane/data/config.json 에 저장하고, 같은 내용을 HTTP API 로 다룰 수 있다.
(컴파일된 라우트 .next/server/app/api/providers/route.js 에서 확인한 형식)

  GET    /api/providers                → {providers:[{id,name,chatModels:[{name,key}],embeddingModels:[...]}]}
  POST   /api/providers                → body {type:"openai"|"ollama"|..., name, config:{apiKey, baseURL}}
  PATCH  /api/providers/{id}           → body {name, config}
  DELETE /api/providers/{id}
  POST   /api/config/setup-complete    → 첫 실행 설정 마법사 건너뛰기
  GET    /api/config                   → fields.modelProviders 에 제공자 타입별 필드 정의 (env: OPENAI_API_KEY, OPENAI_BASE_URL 등)

사용 (프로젝트 루트):
  python -m backend.rag.vane_setup                # .env 의 OPENROUTER_API_KEY 로 OpenRouter 등록
  python -m backend.rag.vane_setup --show         # 현재 제공자/모델 목록만 출력

이 스크립트는 모델을 호출하지 않는다 (제공자 등록 시 Vane 이 /models 목록만 조회).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DEFAULT_VANE = os.environ.get("VANE_URL", "http://localhost:3000")
PROVIDER_NAME = "OpenRouter"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def _req(base: str, path: str, method: str = "GET", body: dict | None = None, timeout: int = 60):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def list_providers(base: str = DEFAULT_VANE) -> list[dict]:
    return _req(base, "/api/providers").get("providers", [])


def find_provider(providers: list[dict], name: str) -> dict | None:
    return next((p for p in providers if p.get("name") == name), None)


def ensure_openrouter(base: str = DEFAULT_VANE, api_key: str | None = None) -> dict:
    """OpenRouter 를 type=openai 제공자로 등록(이미 있으면 그대로). 등록된 제공자 dict 반환."""
    api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY 가 없습니다 (.env 확인).")
    existing = find_provider(list_providers(base), PROVIDER_NAME)
    if existing:
        return existing
    res = _req(base, "/api/providers", "POST",
               {"type": "openai", "name": PROVIDER_NAME, "config": {"apiKey": api_key, "baseURL": OPENROUTER_BASE}},
               timeout=120)
    return res.get("provider", res)


def ensure_model(provider_id: str, key: str, name: str | None = None, model_type: str = "chat",
                 base: str = DEFAULT_VANE) -> bool:
    """제공자에 모델을 수동 추가. Vane 의 OpenAI 타입 제공자는 baseURL 이 api.openai.com 이 아니면
    기본 모델 목록을 비워 두므로(컴파일된 provider 코드 확인) OpenRouter 모델은 이렇게 넣어야 한다.
    POST /api/providers/{id}/models  body {type:"chat"|"embedding", key, name}. 이미 있으면 False."""
    providers = list_providers(base)
    p = next((p for p in providers if p["id"] == provider_id), None)
    if p and any(m.get("key") == key for m in p.get("chatModels" if model_type == "chat" else "embeddingModels", [])):
        return False
    _req(base, f"/api/providers/{provider_id}/models", "POST", {"type": model_type, "key": key, "name": name or key})
    return True


def mark_setup_complete(base: str = DEFAULT_VANE) -> None:
    try:
        _req(base, "/api/config/setup-complete", "POST", {})
    except urllib.error.HTTPError:
        pass  # 이미 완료된 경우 등 — 치명적이지 않음


def pick_ids(providers: list[dict], chat_model_key: str, embedding_key: str = "Xenova/nomic-embed-text-v1") -> dict:
    """research.py 가 /api/search 에 넣을 {chatModel:{providerId,key}, embeddingModel:{providerId,key}} 를 만든다."""
    chat = next((p for p in providers if any(m.get("key") == chat_model_key for m in p.get("chatModels", []))), None)
    emb = next((p for p in providers if any(m.get("key") == embedding_key for m in p.get("embeddingModels", []))), None)
    if not chat:
        raise LookupError(f"채팅 모델 {chat_model_key} 을 가진 제공자가 없습니다.")
    if not emb:
        raise LookupError(f"임베딩 모델 {embedding_key} 을 가진 제공자가 없습니다.")
    return {"chatModel": {"providerId": chat["id"], "key": chat_model_key},
            "embeddingModel": {"providerId": emb["id"], "key": embedding_key}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vane", default=DEFAULT_VANE)
    ap.add_argument("--show", action="store_true", help="등록하지 않고 현재 상태만 출력")
    ap.add_argument("--model", default="minimax/minimax-m3:free", help="확인할 채팅 모델 key")
    args = ap.parse_args()

    try:
        if not args.show:
            p = ensure_openrouter(args.vane)
            mark_setup_complete(args.vane)
            print(f"제공자 '{p.get('name')}' id={p.get('id')} 등록/확인 완료")
            added = ensure_model(p["id"], args.model, args.model.split("/")[-1], "chat", args.vane)
            print(f"채팅 모델 {args.model} {'추가' if added else '이미 있음'}")
        providers = list_providers(args.vane)
    except urllib.error.URLError as e:
        raise SystemExit(f"Vane({args.vane})에 연결할 수 없습니다: {e}. docker ps 로 컨테이너 상태를 확인하세요.")

    for p in providers:
        chats = p.get("chatModels", [])
        embs = p.get("embeddingModels", [])
        print(f"- {p['name']} ({p['id'][:8]}…): chat {len(chats)}개, embedding {len(embs)}개")
        if embs:
            print("    embedding:", ", ".join(m["key"] for m in embs))
    try:
        ids = pick_ids(providers, args.model)
        print("search 용 모델 지정:", json.dumps(ids, ensure_ascii=False))
    except LookupError as e:
        print("주의:", e)
        sys.exit(2)


if __name__ == "__main__":
    main()
