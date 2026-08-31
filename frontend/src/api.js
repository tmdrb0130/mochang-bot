// 백엔드(FastAPI) 호출 모듈. 주소는 frontend/.env 의 VITE_API_BASE 로 바꿀 수 있음 (기본 http://localhost:8000).
export const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

// 프론트 form(camelCase) → 백엔드 스키마(snake_case)
export function toPayload(form) {
  return {
    track: form.track,
    idea: form.idea,
    is_business: form.isBusiness,
    current_item: form.currentItem || "",
    team: form.team,
    capability: form.capability || "",
  };
}

async function post(path, body) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch { /* 본문 없음 */ }
    throw new Error(`요청 실패 (${res.status}) ${detail}`.trim());
  }
  return res.json();
}

/** 문항 하나 생성 → { question_id, style, text, length, limit } */
export function generate(form, questionId, styleId) {
  return post("/generate", { ...toPayload(form), question_id: questionId, style: styleId });
}

/** 기존 글 뒤에 이어쓰기 → { text(합친 전체), added, length, limit } */
export function extend(form, questionId, styleId, current) {
  return post("/extend", { ...toPayload(form), question_id: questionId, style: styleId, current });
}

/** Q6 사업 분야 추천 → { field, reason, options } (실패 시 field === "") */
export function recommendField(form) {
  return post("/recommend-field", toPayload(form));
}

/** 서버 상태 → { ok, model, base_url } */
export async function health() {
  const res = await fetch(`${API_BASE}/health`);
  if (!res.ok) throw new Error(`health ${res.status}`);
  return res.json();
}
