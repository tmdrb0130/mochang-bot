// 백엔드(FastAPI) 호출 모듈. 주소는 frontend/.env 의 VITE_API_BASE 로 바꿀 수 있음 (기본 http://localhost:8000).
export const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

// 프론트 form(camelCase) → 백엔드 스키마(snake_case). model 은 선택(없으면 백엔드 기본 모델).
export function toPayload(form) {
  return {
    track: form.track,
    idea: form.idea,
    is_business: form.isBusiness,
    current_item: form.currentItem || "",
    team: form.team,
    capability: form.capability || "",
    ...(form.model ? { model: form.model } : {}),
    ...(form.answers?.length ? { answers: form.answers } : {}),
  };
}

async function request(path, options) {
  const res = await fetch(`${API_BASE}${path}`, options);
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch { /* 본문 없음 */ }
    const err = new Error(detail || `요청 실패 (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function post(path, body) {
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** 아이디어 인테이크 → { summary, slots[{id,label,status,known}], cards[{slot,label,type,question,why,question_ids,options[{label,hint}]}], ready, model } */
export function intake(form) {
  return post("/intake", toPayload(form));
}

/** 카드 재생성: 보기가 안 맞는 슬롯만 다시 → { cards[슬롯별 새 카드], slots, model, error? }
 *  seen: { slot: [이미 보여준 보기 label] } — 같은 보기는 백엔드가 걸러냄. keep: { slot: [{label,hint}] } 이미 고른 보기(유지). note: 메모(선택). */
export function intakeRegenerate(form, slots, seen, note = "", keep = {}) {
  return post("/intake/regenerate", { ...toPayload(form), slots, seen, note, keep });
}

/** 문항 하나 생성 → { question_id, style, text, length, limit, model } */
export function generate(form, questionId, styleId) {
  return post("/generate", { ...toPayload(form), question_id: questionId, style: styleId });
}

/** 기존 글 뒤에 이어쓰기 → { text(합친 전체), added, length, limit, model } */
export function extend(form, questionId, styleId, current) {
  return post("/extend", { ...toPayload(form), question_id: questionId, style: styleId, current });
}

/** 서버 상태 → { ok, model, base_url, fallback, usage: { used, limit, remaining, reset } } */
export function health() {
  return request("/health");
}

/** 선택 가능한 모델 → { default, models: [{ id, name, note }] } */
export function models() {
  return request("/models");
}
