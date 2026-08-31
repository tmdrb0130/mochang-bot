"""LLM 서버 없이도 프롬프트 조립을 확인하는 CLI.

사용법 (프로젝트 루트에서):
  python -m backend.test_generate q2                  # 조립된 프롬프트 출력 (LLM 호출 없음)
  python -m backend.test_generate q2 --style logic --track local
  python -m backend.test_generate q2 --call           # config.yaml의 LLM으로 실제 생성
  python -m backend.test_generate q2 --call --model z-ai/glm-5.2:free   # 모델만 바꿔서 호출 (OpenRouter 비교용)
"""
import argparse
import asyncio

from .pipeline import assemble

SAMPLE_FORM = {
    "question_id": "q2",
    "style": "story",
    "track": "tech",
    "idea": (
        "자취하는 20대는 배달 음식이 지겨운데 요리는 부담스럽다. "
        "동네 반찬가게와 연결해 그날 남은 반찬을 저녁 7시 이후 할인 꾸러미로 예약·수령하는 앱을 만들고 싶다. "
        "직접 반찬가게 사장님 다섯 분께 여쭤보니 매일 만든 반찬의 20~30%가 폐기된다고 했다."
    ),
    "is_business": False,
    "current_item": "",
    "team": "1명",
    "capability": "식품회사 영업 3년, 동네 반찬가게 5곳과 이미 친분, 노코드 앱 제작 경험",
}


def main():
    parser = argparse.ArgumentParser(description="프롬프트 조립/생성 테스트")
    parser.add_argument("question_id", nargs="?", default="q2")
    parser.add_argument("--style", default="story", choices=["story", "logic", "plain"])
    parser.add_argument("--track", default="tech", choices=["tech", "local"])
    parser.add_argument("--call", action="store_true", help="실제 LLM 호출 (기본은 조립 결과만 출력)")
    parser.add_argument("--model", default=None, help="config.yaml의 model 을 이번 호출만 덮어씀")
    args = parser.parse_args()

    form = {**SAMPLE_FORM, "question_id": args.question_id, "style": args.style, "track": args.track}
    system, user, meta = assemble.build_prompts(form)

    if not args.call:
        print("=" * 60)
        print(f"문항: {meta['label']} {meta['title']}  (limit {meta['limit']}자, target {meta['target']})")
        print("=" * 60)
        print("\n───── SYSTEM 프롬프트 ─────\n")
        print(system)
        print("\n───── USER 프롬프트 ─────\n")
        print(user)
        return

    from .llm.client import LLMClient, load_config
    from .pipeline import generate

    async def run():
        import time

        config = load_config()
        if args.model:
            config["llm"]["model"] = args.model
        client = LLMClient(config)
        t0 = time.time()
        result = await generate.generate_one(client, form)
        print(f"[{result['question_id']} / {result['style']}] {result['length']}/{result['limit']}자  "
              f"model={config['llm']['model']}  {time.time() - t0:.1f}초\n")
        print(result["text"])

    asyncio.run(run())


if __name__ == "__main__":
    main()
