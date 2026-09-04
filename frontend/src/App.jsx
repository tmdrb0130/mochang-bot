import { useEffect, useMemo, useRef, useState } from "react";
import * as api from "./api.js";
import { LANGS, LANG_NAMES, LangContext, loadLang, makeT, saveLang, useLang } from "./i18n.jsx";

// ───────────────────────── 데이터 정의 ─────────────────────────
// 문항 의도·작성 지침은 백엔드 backend/prompts/ 의 md 파일이 단일 출처. 여기엔 UI 표시용 메타만 둔다.
// 2026-09-02 결정: 일반/기술 트랙만 쓴다. 로컬 트랙은 UI 에서만 뺐고 백엔드 프롬프트(tracks/local.md)·API 는 그대로다 —
// 되살릴 때 아래 항목과 선택 화면만 복원하면 된다.
const TRACKS = {
  tech: {
    name: "일반/기술 분야",
    desc: "창의적 아이디어·기술로 불편을 해소하고 새로운 가치를 만드는 아이템",
    // Q6 사업 분야 선택지 (modoo.or.kr 도전하기 화면과 동일) — 사람이 직접 고른다
    fields: ["IT", "교육", "금융", "운영관리", "네트워킹", "농축/수산업", "라이프스타일", "마케팅/PR", "모빌리티", "미디어/엔터테인먼트", "바이오/의료", "에너지/자원", "유통/물류", "임팩트", "재무", "프롭테크", "하드웨어", "기타"],
  },
};

// Q10(대중 공개용 자랑)을 비공개로 선택하면 AI 호출 없이 이 문장을 넣는다. 사용자가 고칠 수 있음.
const Q10_PRIVATE_TEXT = "아이디어 보호를 위해 심사 기간 동안은 공개하지 않겠습니다.";

// 2026-09-02 결정: 논리·근거형 하나만 쓴다 (실호출 품질이 가장 안정적이고, 문항당 생성 호출이 1회로 고정된다).
// 스토리텔링·간결실무도 UI 에서만 뺐다 — 백엔드 styles/story.md, styles/plain.md 는 그대로 살아 있다.
const STYLES = [
  { id: "logic", name: "논리·근거형", desc: "문제 → 원인 → 해결 → 근거 순서로 설득하는 글" },
];

const TEAM_OPTIONS = ["팀원 없음", "1명", "2명", "3명", "4명", "5명", "6~9명", "10명 이상"];

const QUESTIONS = [
  { id: "q1", label: "Q1", title: "나의 아이디어를 한줄로 소개해주세요", limit: 100 },
  { id: "q2", label: "Q2", title: "아이디어를 떠올린 배경 이야기를 들려주세요", limit: 2000 },
  { id: "q3_1", label: "Q3-1", title: "기존 제품·서비스 대비 차별점과 해결할 수 있는 문제", limit: 2000 },
  { id: "q3_2", label: "Q3-2", title: "어떻게 수익을 창출할 계획인가요", limit: 2000 },
  { id: "q4_1", label: "Q4-1", title: "아이디어를 어떻게 사업화할 계획인가요", limit: 2000 },
  { id: "q4_2", label: "Q4-2", title: "책임멘토를 통해 어떤 도움을 받고 싶은가요", limit: 2000 },
  { id: "q7_1", label: "Q7-1", title: "현재 사업 아이템과 이번 도전 아이디어는 어떻게 다른가요", limit: 2000, onlyBusiness: true },
  { id: "q8", label: "Q8", title: "아이디어를 사업화할 수 있는 본인의 역량", limit: 2000 },
  { id: "q10", label: "Q10", title: "내 아이디어를 사람들에게 자랑해보세요 (홈페이지 공개)", limit: 100 },
];

// 브라우저 쪽 동시 요청 수. 실제 LLM 동시성은 백엔드 config.yaml 의 concurrency 가 최종 제한한다.
// 서버 워커가 30개로 올랐고 IP 당 동시 3건까지 허용되므로(백엔드 max_jobs_per_client) 3 까지는 429 없이 간다.
const PARALLEL = 3;
// 조사 동시 실행 수. 백엔드 조사 모델은 로컬 70B 라 무료 한도가 없어 생성보다 높여도 된다.
const RESEARCH_PARALLEL = 3;

async function runLimited(tasks, limit) {
  const queue = [...tasks];
  const workers = Array.from({ length: limit }, async () => {
    while (queue.length) {
      const t = queue.shift();
      await t();
    }
  });
  await Promise.all(workers);
}

// ───────────────────────── 카드 입력 (인테이크 / 정보 보태기 공용) ─────────────────────────
// value: { answer: string | string[] | null, unknown: bool } | undefined
// 모든 카드가 다중 선택. 답은 항상 배열. number 카드는 보기 + "몇 명" 숫자를 함께 받는다.
function CardOptions({ card, value, onChange, compact = false }) {
  const { t, tr } = useLang();               // tr: 카드 문구(한국어) → 화면 언어 번역 (없으면 원문). 답은 한국어 원문으로 저장된다.
  const [other, setOther] = useState("");
  const isNumber = card.type === "number";
  const picked = value?.answer;
  const pickedList = Array.isArray(picked) ? picked : picked ? [picked] : [];
  const labels = card.options.map((o) => o.label);
  // "보기 (12명)" 형태로 저장된 숫자를 다시 꺼낸다
  const countOf = (list) => { const m = list.map((p) => p.match(/\((\d+)명\)$/)).find(Boolean); return m ? m[1] : ""; };
  const count = countOf(pickedList);
  const base = (p) => p.replace(/ \(\d+명\)$/, "");

  const commit = (next) => onChange({ answer: next, unknown: false });
  const withCount = (list, n) => list.map((p) => (labels.includes(base(p)) && n ? `${base(p)} (${n}명)` : base(p)));

  const toggle = (label) => {
    const on = pickedList.some((p) => base(p) === label);
    const nextList = on ? pickedList.filter((p) => base(p) !== label) : [...pickedList, label];
    commit(isNumber ? withCount(nextList, count) : nextList);
  };

  const setCount = (n) => commit(withCount(pickedList, n));

  const submitOther = () => {
    const t = other.trim();
    if (!t) return;
    commit([...pickedList, t]);
    setOther("");
  };

  const isOn = (label) => pickedList.some((p) => base(p) === label);
  const btn = compact ? "px-2.5 py-1 text-xs" : "px-3 py-2 text-sm";
  const hints = card.options.filter((o) => isOn(o.label)).map((o) => o.hint).filter(Boolean);

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        {card.options.map((o) => (
          <button key={o.label} onClick={() => toggle(o.label)} title={o.source ? `${tr(o.hint)}
${t("opt.source")}${o.source}` : tr(o.hint)}
            className={`${btn} rounded-lg border text-left ${isOn(o.label) ? "border-indigo-600 bg-indigo-50 text-indigo-900" : "border-slate-200 hover:border-slate-400"}`}>
            {isOn(o.label) && <span className="mr-1">✓</span>}{tr(o.label)}
            {/* 웹 조사에서 근거가 나온 보기에만 출처를 붙인다 — 지어낸 보기와 구분된다 */}
            {o.source && <span className="ml-1.5 text-[10px] text-sky-700 bg-sky-100 rounded px-1 py-0.5 align-middle">{o.source}</span>}
          </button>
        ))}
        <button onClick={() => onChange({ answer: null, unknown: true })}
          className={`${btn} rounded-lg border ${value?.unknown ? "border-amber-500 bg-amber-50 text-amber-900" : "border-dashed border-slate-300 text-slate-500 hover:border-slate-400"}`}>
          {t("opt.unknown")}
        </button>
      </div>
      {!compact && <p className="text-xs text-slate-400">{t("opt.multi")}{hints.length > 0 && ` → ${hints.map(tr).join(" / ")}`}</p>}
      <div className="flex gap-2 flex-wrap">
        {isNumber && (
          <input type="number" min="0" value={count} onChange={(e) => setCount(e.target.value)} placeholder={t("opt.count")}
            className={`${compact ? "text-xs py-1" : "text-sm py-2"} px-3 rounded-lg border border-slate-300 w-24`} />
        )}
        <input value={other} onChange={(e) => setOther(e.target.value)} onKeyDown={(e) => e.key === "Enter" && submitOther()}
          placeholder={t("opt.other.ph")}
          className={`${compact ? "text-xs py-1" : "text-sm py-2"} px-3 rounded-lg border border-slate-300 flex-1 min-w-[200px]`} />
      </div>
      {pickedList.filter((p) => !labels.includes(base(p))).map((p) => (
        <button key={p} onClick={() => commit(pickedList.filter((x) => x !== p))} title={t("opt.remove")}
          className="inline-block mr-2 text-xs px-2 py-0.5 rounded bg-indigo-50 text-indigo-800 hover:bg-red-50 hover:text-red-700">{t("opt.other")}{p} ×</button>
      ))}
    </div>
  );
}

// "다른 보기 보기" — 보기가 안 맞을 때 메모(선택)와 함께 그 카드만 다시 생성
function RegenerateBar({ slot, state, onNote, onRun, compact = false }) {
  const { t } = useLang();
  const busy = state?.busy;
  const size = compact ? "text-xs py-1" : "text-xs py-1.5";
  return (
    <div className="space-y-1">
      <div className="flex gap-2 flex-wrap items-center">
        <input value={state?.note || ""} onChange={(e) => onNote(slot, e.target.value)} onKeyDown={(e) => e.key === "Enter" && !busy && onRun(slot)}
          placeholder={t("regen.ph")} disabled={busy}
          className={`${size} px-3 rounded-lg border border-slate-200 flex-1 min-w-[220px] disabled:bg-slate-50`} />
        <button onClick={() => onRun(slot)} disabled={busy}
          className={`${size} px-3 rounded-lg border border-indigo-200 text-indigo-700 hover:bg-indigo-50 disabled:text-slate-400 disabled:border-slate-200`}>
          {busy ? t("regen.busy") : `${t("regen.btn")}${state?.count ? ` (${state.count})` : ""}`}
        </button>
      </div>
      {state?.error && <p className="text-xs text-red-600">{state.error}</p>}
    </div>
  );
}

// ───────────────────────── 컴포넌트 ─────────────────────────
// KCI(한국학술지인용색인) 이용 조건 — 서비스 화면에 출처를 **항상** 표시해야 한다
// (docs/RESEARCH_PLAN.md "KCI 이용 준수 사항" 4항). 푸터에 상시 표기하고, 조사 근거 패널의
// KCI 항목에는 배지를 붙인다. 문구는 한 곳에서만 고치도록 상수로 둔다.
const KCI_NOTICE = "KCI(한국학술지인용색인) 데이터 활용";

// 이 근거가 KCI 에서 온 것인지. 세션3의 KCI 어댑터가 붙기 전에도(지금은 ddgs 가 kci.go.kr 문서를
// 물어 오는 경우) 표기가 빠지지 않도록 세 가지를 모두 본다. 어댑터가 source_kind 를 붙이면 첫 조건이 잡는다.
const isKciFact = (f) =>
  String(f?.source_kind || "").toLowerCase() === "kci" ||
  /(^|\.)kci\.go\.kr/i.test(String(f?.url || "").replace(/^https?:\/\//, "").split("/")[0]) ||
  String(f?.publisher || "").trim().toUpperCase() === "KCI";

// ───────────────────────── 웹 조사 근거 패널 ─────────────────────────
// 백엔드 /research 결과. 조사는 로컬 라마 70B 가 하고, 이 사실들이 [웹 참고자료] 로 작성 모델에 넘어간다.
function ResearchPanel({ state, warm = true }) {
  const { t } = useLang();
  const [open, setOpen] = useState(false);
  if (!state) return null;
  // 아이디어 공통 조사는 이 아이디어의 첫 문항에서만 실제로 돈다 (모델 호출 4회) — 나머지 문항보다 몇 배 느리다.
  // 문구가 같으면 첫 문항에서 멈춘 것처럼 보이므로, 아직 조사 결과가 하나도 없을 때는 그렇다고 알린다.
  if (state.busy) return <div className="px-4 py-2 text-xs text-slate-500 bg-sky-50 border-b border-sky-100">{warm ? t("rs.busy") : t("rs.first")}</div>;
  if (state.error) return <div className="px-4 py-2 text-xs text-amber-700 bg-amber-50 border-b border-amber-100">{t("rs.fail", { e: state.error })}</div>;
  const facts = state.facts || [];
  if (!facts.length) return <div className="px-4 py-2 text-xs text-slate-500 bg-slate-50 border-b border-slate-200">{t("rs.none")}</div>;
  const hasKci = facts.some(isKciFact);
  return (
    <div className="bg-sky-50 border-b border-sky-100">
      <button onClick={() => setOpen((v) => !v)} className="w-full px-4 py-2 text-xs text-left text-sky-800 hover:bg-sky-100">
        {t("rs.head", { n: facts.length, cached: state.cached ? t("rs.cached") : "", q: (state.queries || []).join(" / ") })} {open ? "▲" : "▼"}
      </button>
      {open && (
        <ol className="px-5 pb-3 space-y-2 text-xs text-slate-700 list-decimal">
          {facts.map((f, i) => (
            <li key={i}>
              <div>{f.fact}</div>
              <div className="text-slate-500">
                {isKciFact(f) && (
                  <span className="mr-1.5 px-1 py-0.5 rounded bg-white border border-sky-300 text-sky-800 text-[10px] align-middle" title={KCI_NOTICE}>KCI</span>
                )}
                {f.publisher || f.source_title || t("rs.unknown")}{f.date ? `, ${String(f.date).slice(0, 4)}` : ""}
                {f.use_for ? ` · ${f.use_for}` : ""}
                {f.url && <> · <a href={f.url} target="_blank" rel="noreferrer" className="text-sky-700 underline">{t("rs.orig")}</a></>}
              </div>
            </li>
          ))}
        </ol>
      )}
      {/* KCI 항목이 섞여 있으면 패널 안에서도 출처를 밝힌다 (준수 사항 4항). 접힌 상태에서도 보인다. */}
      {hasKci && (
        <div className="px-4 pb-2 text-[11px] text-sky-800">
          {KCI_NOTICE}{t("rs.kci")}
        </div>
      )}
    </div>
  );
}

// ───────────────────────── 작업 큐 진행 표시 ─────────────────────────
// 생성은 큐에 들어간다. 사람이 몰리면 몇 분 기다릴 수 있으므로 "지금 몇 번째인지"를 보여준다 —
// 진행 표시가 없으면 멈춘 것으로 오해하고 새로고침·재시도해서 큐를 더 밀리게 만든다.
function JobProgress({ info }) {
  const { t } = useLang();
  if (!info) return <div className="text-xs text-slate-500 mb-2">{t("job.queueing")}</div>;
  const { status, position, busy } = info;
  const label = status === "resuming"
    ? t("job.resuming")
    : busy
    ? t("job.busy")
    : status === "queued"
      ? (position == null ? t("job.waiting")
        : position === 0 ? t("job.next")
        : t("job.ahead", { n: position }))
      : status === "running" ? t("job.running")
      : t("job.processing");
  return <div className="text-xs text-slate-500 mb-2">{label}</div>;
}

// ───────────────────────── 작업 내용 저장 (새로고침 대비) ─────────────────────────
// 인테이크 카드·답변·초안은 메모리에만 있어서 새로고침하면 사라졌다.
// 조사 + 인테이크에 2분 가까이 걸리므로 유실이 크다 → localStorage 에 담아둔다.
// 브라우저에만 남고 서버로는 안 간다. 다른 아이디어를 입력하면 덮어쓴다.
const SAVE_KEY = "modoo-writer-state-v1";

function loadSaved() {
  try {
    const raw = localStorage.getItem(SAVE_KEY);
    return raw ? migrate(JSON.parse(raw)) : null;
  } catch {
    return null;                      // 사생활 보호 모드 등에서 접근이 막히면 그냥 저장 없이 동작
  }
}

// 예전 저장본에는 지금은 없는 트랙(local)·스타일(story, plain)이 들어 있을 수 있다.
// 그대로 두면 없는 스타일로 생성을 시도하다 터지므로, 살아 있는 항목만 남긴다.
function migrate(saved) {
  if (!saved?.form) return saved;
  const styleIds = STYLES.map((s) => s.id);
  const styles = (saved.form.styles || []).filter((id) => styleIds.includes(id));
  saved.form = {
    ...saved.form,
    track: TRACKS[saved.form.track] ? saved.form.track : "tech",
    styles: styles.length ? styles : [styleIds[0]],
    // 사업자 선택 UI 를 뺀 뒤(2026-09-03) 옛 저장본의 true 가 Q7-1 을 되살리지 않게
    isBusiness: false,
    currentItem: "",
    // 초안 id 가 생기기 전 저장본 — 지금 아이디어로 새 초안을 연다
    draftId: saved.form.draftId || api.newDraftId(),
    draftIdea: saved.form.draftIdea ?? saved.form.idea ?? "",
    draftKey: saved.form.draftKey || "",     // 접근 열쇠 (2026-09-04). 옛 저장본은 비어 있다 — 같은 IP 면 서버가 다음 응답에 채워 준다
  };
  // 고른 초안이 사라진 스타일이면 선택을 비운다 (남은 스타일의 초안이 있으면 그게 보이고, 없으면 다시 생성하면 된다)
  saved.picked = Object.fromEntries(Object.entries(saved.picked || {}).filter(([, sid]) => styleIds.includes(sid)));
  return saved;
}

// ── 진행 중인 작업 id (V6: 새로고침 이어받기) ──
// 생성/이어쓰기는 큐에 들어가 있으므로 job id 만 있으면 탭을 새로고침해도 결과를 다시 받을 수 있다.
// 탭을 닫으면 지워지는 sessionStorage 를 쓴다 — 다른 탭·다음 방문까지 끌고 갈 이유는 없다.
const JOBS_KEY = "modoo-writer-jobs-v1";

// 조사 결과를 재사용해도 되는 범위. 이 값이 달라지면 이전 조사는 다른 아이디어의 것이므로 버린다.
// (2026-09-02 버그: 문항 id 로만 캐시해서 아이디어를 바꿔도 옛 조사를 그대로 재사용했다.)
const researchSignature = (form) => `${form?.track || ""}|${(form?.idea || "").trim()}`;

// ── 초안 id (서버 저장의 묶음 키) ──
// 아이디어를 조금 고치면(오타·문장 추가) 같은 초안이고, 완전히 다른 아이디어를 쓰면 새 초안이다.
// 판정은 글자 2-gram 겹침 비율(공통 ÷ 짧은 쪽) — 한국어라 단어 분리 없이 쓸 수 있고, 덧붙이기에는 1.0 에 가깝고
// 전혀 다른 글에는 0.1~0.2 대라 0.5 로 가른다. 비교 기준(draftIdea)은 id 를 만들 때와 인테이크를 시작할 때의 아이디어.
const DRAFT_SAME_THRESHOLD = 0.5;
function bigrams(text) {
  const t = (text || "").replace(/\s+/g, "");
  const out = new Set();
  for (let i = 0; i + 1 < t.length; i++) out.add(t.slice(i, i + 2));
  return out;
}
function ideaSimilarity(a, b) {
  const A = bigrams(a), B = bigrams(b);
  if (!A.size || !B.size) return 1;          // 빈 칸에서 시작·비우는 중 — 같은 초안으로 본다
  let common = 0;
  for (const g of A) if (B.has(g)) common++;
  return common / Math.min(A.size, B.size);
}
// form 변경을 적용하면서 아이디어가 기준과 많이 달라졌으면 새 초안 id 를 매긴다. 서버에는 보내는 순간에만 반영된다.
function withDraft(next) {
  if (next.draftId && ideaSimilarity(next.idea, next.draftIdea) >= DRAFT_SAME_THRESHOLD) return next;
  return { ...next, draftId: api.newDraftId(), draftIdea: next.idea || "", shareToken: "", draftKey: "" };   // 새 초안 = 공유 링크·열쇠도 새로
}

// ── 서버 저장본 복원 (2026-09-03) ──
// 학생이 탭을 닫으면 (1) 서버에서 돌던 문항의 결과와 (2) 마무리 작업자(backend/finisher.py)가 뒤에서 채운 문항이 DB 에만 남는다.
// 재접속·개인 링크(?draft=id)로 돌아오면 /drafts/{id} 를 받아 비어 있는 문항만 채운다 (학생이 고친 글은 덮지 않는다).
const DRAFT_ID_RE = /^[A-Za-z0-9_-]{8,64}$/;
function latestTexts(row) {
  const out = {};                          // out[qid][styleId] = { text, model, auto } — generations 는 오래된 순이라 마지막이 최신
  for (const g of row?.generations || []) {
    if (g.kind !== "generate" && g.kind !== "extend") continue;
    if (!g.style || !STYLES.some((s) => s.id === g.style)) continue;
    out[g.question_id] = { ...(out[g.question_id] || {}), [g.style]: { text: g.text || "", model: g.model || "", auto: !!g.meta?.auto } };
  }
  return out;
}
function answersFromServer(list) {
  const out = {};
  for (const a of list || []) if (a?.slot) out[a.slot] = { answer: a.answer ?? null, unknown: !!a.unknown };
  return out;
}
// 공유 링크(2026-09-04): DB 키(draft_id)가 아니라 서버가 따로 만든 난수 토큰(/drafts/{id}/share)을 싣는다.
// 예전에 복사해 둔 ?draft=<id> 링크도 그대로 열린다(아래 draftIdFromUrl).
const SHARE_TOKEN_RE = /^[A-Za-z0-9_-]{16,64}$/;
function shareLinkFor(token) {
  return `${location.origin}${location.pathname}?share=${token}`;
}
function urlParam(name, re) {
  try {
    const v = new URLSearchParams(location.search).get(name) || "";
    return re.test(v) ? v : "";
  } catch { return ""; }
}
const draftIdFromUrl = () => urlParam("draft", DRAFT_ID_RE);
const shareFromUrl = () => urlParam("share", SHARE_TOKEN_RE);

function loadJobs() {
  try {
    return JSON.parse(sessionStorage.getItem(JOBS_KEY) || "{}");
  } catch {
    return {};
  }
}

function saveJobs(map) {
  try { sessionStorage.setItem(JOBS_KEY, JSON.stringify(map)); } catch { /* 접근 차단 — 이어받기만 못 함 */ }
}

function saveState(data) {
  try {
    localStorage.setItem(SAVE_KEY, JSON.stringify(data));
  } catch { /* 용량 초과·접근 차단 — 저장만 못 할 뿐 동작에는 지장 없음 */ }
}

export default function ModooWriter() {
  const saved = useMemo(() => loadSaved(), []);
  const [step, setStep] = useState(saved?.step ?? 0); // 0 입력, 1 정보 보충, 2 초안 고르기, 3 제출용 정리
  const [form, setForm] = useState(saved?.form ?? {
    track: "tech", idea: "", isBusiness: false, currentItem: "", team: "팀원 없음", capability: "",
    field: "",          // Q6 사업 분야 — 사람이 직접 선택
    q10Public: true,    // Q10 공개 여부 — 공개면 AI 생성, 비공개면 고정 문장
    styles: ["logic"], // 기본 1개 (무료 티어 요청 수 절약). 2026-09-01: 논리·근거형이 실호출 품질 최고라 기본으로. 사용자가 더 고를 수 있음.
    draftId: api.newDraftId(),   // 서버 저장 묶음 키 (withDraft 가 아이디어가 확 바뀌면 새로 매김)
    draftIdea: "",
    draftKey: "",                // 초안 접근 열쇠 — 첫 저장 응답(draft_key)에서 받아 보관, 모든 요청·복원에 실어 보낸다 (2026-09-04)
  });
  // 서버 응답에 실려 온 접근 열쇠를 이 초안의 것으로 보관한다. 응답이 늦게 도착해 이미 다른 초안으로 바뀐 경우는 무시.
  const adoptDraftKey = (res, draftId) => {
    const key = res?.draft_key;
    if (!key) return;
    setForm((f) => (f.draftId === draftId && f.draftKey !== key ? { ...f, draftKey: key } : f));
  };
  const [texts, setTexts] = useState(saved?.texts ?? {});   // texts[qid][styleId] = string
  // ── 화면 언어 (2026-09-03, 외국인 지원자) ──
  // 문구는 i18n.jsx 사전, 초안·카드는 한국어 그대로 두고 /jobs/translate 로 읽기 번역을 병기한다. 서버로 가는 값은 전부 한국어.
  const [lang, setLangState] = useState(() => loadLang());
  const t = useMemo(() => makeT(lang), [lang]);
  const setLang = (id) => { setLangState(id); saveLang(id); };
  const foreign = lang !== "ko";
  useEffect(() => { document.documentElement.lang = lang; document.title = t("app.title"); }, [lang, t]);
  // 카드 문구 번역: { maps: { en: { 한국어 원문: 번역 }, zh: {...}, ja: {...} }, error } — 언어마다 따로 보관해 언어를 오가도 다시 받지 않는다.
  // (13:20 배포본은 언어 하나만 보관해 영어→중국어로 바꾸면 영어 지도를 버렸고, 번역 도중 바꾸면 멈췄다.) 원문이 키라 카드가 재생성돼도 새 문구만 번역.
  const [cardI18n, setCardI18n] = useState(() => {
    const c = saved?.cardI18n;
    if (c?.maps) return c;
    return c?.lang && c?.map ? { maps: { [c.lang]: Object.fromEntries(Object.entries(c.map).filter(([, v]) => v)) }, error: "" } : null;   // 옛 저장본
  });
  const [cardBusy, setCardBusy] = useState(false);
  const tr = (text) => (foreign && cardI18n?.maps?.[lang]?.[text]) || text;
  // 초안 번역: trans[qid][lang] = { text, source(번역 당시 한국어 원문), error } — 원문이 바뀌면 stale 로 보여주고 버튼으로 다시 번역한다.
  const [trans, setTrans] = useState(saved?.trans ?? {});
  const [transBusy, setTransBusy] = useState({});                 // transBusy["qid|lang"] = true
  const [status, setStatus] = useState(saved?.status ?? {}); // status[qid][styleId] = 'loading' | 'done' | 'error'
  const [errors, setErrors] = useState({}); // errors[qid][styleId] = message
  const [picked, setPicked] = useState(saved?.picked ?? {}); // picked[qid] = styleId
  const [running, setRunning] = useState(false);
  const [copied, setCopied] = useState("");
  // 서버가 뒤에서 채우는 중인지 (마무리 작업자). { remaining, inProgress } | null — step 2 에서 안내 문구와 폴링에 쓴다.
  const [autoFill, setAutoFill] = useState(null);
  // 개인 링크로 복원했을 때 카드(intake) 없이 서버 답변만 있는 경우 — buildAnswers 가 이걸로 대신한다.
  const restoredAnswersRef = useRef(null);
  // ── 인테이크(정보 보충) ──
  const [intake, setIntake] = useState(saved?.intake ?? null);      // /intake 결과 { summary, slots, cards, ready }
  const [intakeBusy, setIntakeBusy] = useState(false);
  const [intakePos, setIntakePos] = useState(null);      // 인테이크 작업의 큐 대기 순번 (0 = 다음 차례, null = 실행 중/모름)
  const [answers, setAnswers] = useState(saved?.answers ?? {});      // answers[slot] = { answer: string|string[]|null, unknown: bool }
  const [cardIdx, setCardIdx] = useState(saved?.cardIdx ?? 0);
  const [regen, setRegen] = useState({});          // regen[slot] = { busy: bool, note: string, error?: string, count: number }
  const [assistOpen, setAssistOpen] = useState({}); // assistOpen[qid] = bool (초안 화면의 "정보 보태기" 패널)
  const [server, setServer] = useState(null); // { ok, model, usage } | { ok:false, error }
  const [models, setModels] = useState([]);   // [{ id, name, note }] — 백엔드 config.yaml 의 목록
  const [modelUsed, setModelUsed] = useState(saved?.modelUsed ?? {}); // modelUsed[qid][styleId] = 실제 응답한 모델 id
  // ── 웹 조사 ──
  // 생성 전에 문항마다 /research 를 돌려 출처 있는 사실을 모으고, 그걸 references 로 생성에 넘긴다.
  // 조사는 백엔드의 조사 전용 모델(로컬 라마 70B)이 하고, 본문 작성은 llm: 모델(OpenRouter)이 한다.
  const [researchState, setResearchState] = useState(saved?.researchState ?? {}); // researchState[qid] = { busy, error, facts, queries, pages, backend }
  // 이 아이디어로 조사 결과를 한 번이라도 받았는지. 첫 조사만 느리다는 것을 화면에 알리는 데 쓴다 (공통 조사).
  const [researchWarm, setResearchWarm] = useState(() => Object.values(saved?.researchState ?? {}).some((v) => v && !v.error));
  // 생성 태스크가 즉시 읽어야 해서(setState 는 비동기) 사실은 ref 에도 둔다.
  // 저장본에서 복원할 때 ref 도 같이 채워야 재생성 시 조사를 다시 돌지 않는다.
  // 단 실패했던 문항(error)은 복원하지 않는다 — 복원하면 다음에도 조사를 건너뛴다.
  const researchRef = useRef(Object.fromEntries(
    Object.entries(saved?.researchState ?? {}).filter(([, v]) => v && !v.error).map(([k, v]) => [k, v.facts ?? []])
  ));
  // 그 조사 결과가 "어느 아이디어의 것인지". 아이디어·트랙이 바뀌면 옛 결과를 통째로 버린다.
  const researchKeyRef = useRef(saved?.researchKey ?? researchSignature(saved?.form ?? {}));
  // ── 작업 큐 ──
  const jobsRef = useRef(loadJobs());                     // { "qid|styleId": job_id } — 진행 중인 것만
  const [jobInfo, setJobInfo] = useState({});             // jobInfo[qid][styleId] = { status, position, busy } (대기 순번 표시용)
  const setJobTick = (qid, sid, value) =>
    setJobInfo((p) => ({ ...p, [qid]: { ...(p[qid] || {}), [sid]: value } }));
  const rememberJob = (key, id) => { jobsRef.current = { ...jobsRef.current, [key]: id }; saveJobs(jobsRef.current); };
  const forgetJob = (key) => {
    const { [key]: _drop, ...rest } = jobsRef.current;
    jobsRef.current = rest;
    saveJobs(rest);
  };

  const refreshHealth = () =>
    api.health()
      .then((h) => setServer(h))
      .catch((e) => setServer({ ok: false, error: String(e.message || e) }));

  // ── 새 배포 자동 적용 ──
  // 브라우저는 처음 받은 JS 를 탭을 닫을 때까지 쓴다. 재빌드(배포) 뒤에도 열어 둔 탭은 옛 코드라 새 기능이 안 먹는다.
  // → 1분마다(그리고 탭이 다시 보일 때) index.html 을 새로 받아 번들 이름이 바뀌었는지 보고, 생성·인테이크가 도는 중이 아니면
  //   조용히 새로고침한다. 초안·답변은 localStorage, 진행 중 작업은 sessionStorage 에 있어 새로고침해도 이어진다.
  const reloadWhenIdleRef = useRef(false);
  useEffect(() => {
    if (import.meta.env.DEV) return undefined;          // 개발 서버는 번들 이름이 없다
    const current = [...document.scripts].map((s) => s.src).find((src) => /\/assets\/index-[\w-]+\.js/.test(src));
    if (!current) return undefined;
    const check = async () => {
      try {
        const html = await (await fetch(`/?_=${Date.now()}`, { cache: "no-store" })).text();
        const m = html.match(/\/assets\/index-[\w-]+\.js/);
        if (m && !current.endsWith(m[0])) reloadWhenIdleRef.current = true;
      } catch { /* 서버 재시작 중 등 — 다음 번에 */ }
    };
    const timer = setInterval(check, 60_000);
    const onVisible = () => { if (document.visibilityState === "visible") check(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { clearInterval(timer); document.removeEventListener("visibilitychange", onVisible); };
  }, []);
  useEffect(() => {
    if (reloadWhenIdleRef.current && !running && !intakeBusy) location.reload();
  });

  useEffect(() => {
    refreshHealth();
    api.models()
      .then((m) => {
        setModels(m.models || []);
        setForm((f) => {
          // 저장된 모델이 목록에 없으면(config.yaml 에서 모델을 갈아끼운 경우) 기본값으로 되돌린다.
          // 안 그러면 예전 localStorage 를 가진 브라우저가 계속 옛 모델을 보내 400 을 받는다.
          const ids = (m.models || []).map((x) => x.id);
          return f.model && ids.includes(f.model) ? f : { ...f, model: m.default };
        });
      })
      .catch(() => {});
  }, []);

  const setUsed = (qid, sid, value) =>
    setModelUsed((p) => ({ ...p, [qid]: { ...(p[qid] || {}), [sid]: value } }));

  // 카드 답변 → 백엔드 형식 [{slot,label,answer,unknown}]. 카드가 있는 슬롯만.
  const buildAnswers = () => {
    const cards = intake?.cards || [];
    if (!cards.length && restoredAnswersRef.current?.length) return restoredAnswersRef.current;   // 서버 저장본에서 복원한 경우
    return cards
      .filter((c) => answers[c.slot] && (answers[c.slot].unknown || answers[c.slot].answer))
      .map((c) => ({ slot: c.slot, label: c.label, answer: answers[c.slot].answer, unknown: !!answers[c.slot].unknown }));
  };
  // 값이 바뀔 때마다 저장. loading 상태는 저장하지 않는다 — 새로고침 후 영원히 도는 것처럼 보이면 안 되므로.
  useEffect(() => {
    const cleanStatus = {};
    for (const [qid, byStyle] of Object.entries(status)) {
      const kept = Object.fromEntries(Object.entries(byStyle).filter(([, v]) => v !== "loading"));
      if (Object.keys(kept).length) cleanStatus[qid] = kept;
    }
    // pages(조사에서 본문을 받은 URL 목록)는 화면에서 한 번도 쓰지 않는데 문항마다 수 KB 다.
    // localStorage 가 꽉 차면 saveState 가 통째로 실패해 초안까지 저장되지 않으므로 저장 대상에서 뺀다.
    const leanResearch = Object.fromEntries(
      Object.entries(researchState).map(([qid, v]) => { const { pages: _pages, ...rest } = v || {}; return [qid, rest]; })
    );
    saveState({ step, form, texts, status: cleanStatus, picked, intake, answers, cardIdx, modelUsed, researchState: leanResearch, researchKey: researchKeyRef.current,
      cardI18n, trans });
  }, [step, form, texts, status, picked, intake, answers, cardIdx, modelUsed, researchState, cardI18n, trans]);

  // 마운트 시점 클로저(이어받기)가 잡은 texts 는 곧 낡는다 — "무엇이 아직 비었는지" 는 항상 최신값으로 판단해야 한다.
  const textsRef = useRef(texts);
  useEffect(() => { textsRef.current = texts; }, [texts]);

  const formForApi = () => ({ ...form, answers: buildAnswers() });
  const answeredCount = Object.values(answers).filter((a) => a && (a.unknown || a.answer)).length;
  // 문항에 연결된 카드. 모델이 question_ids 를 비워 보냈으면 모든 문항에 보여준다.
  const cardsForQuestion = (qid) => (intake?.cards || []).filter((c) => !c.question_ids?.length || c.question_ids.includes(qid));
  const shortModel = (id) => (models.find((m) => m.id === id)?.name) || (id || "").split("/").pop();

  const activeQuestions = useMemo(
    () => QUESTIONS.filter((q) => !q.onlyBusiness || form.isBusiness),
    [form.isBusiness]
  );
  const selectedStyles = STYLES.filter((s) => form.styles.includes(s.id));
  // 아이디어는 "자격증 공부 도와주는 AI 앱" 같은 짧은 한 줄도 허용. 부족한 정보는 이후 단계에서 AI가 채우거나 물어보는 구조로 간다.
  const IDEA_MIN = 10;
  const ideaLen = form.idea.trim().length;
  const blockers = [
    ideaLen < IDEA_MIN && t("blk.idea", { min: IDEA_MIN, n: ideaLen }),
    form.styles.length === 0 && t("blk.style"),
    server === null && t("blk.checking"),
    server && !server.ok && t("blk.down"),
  ].filter(Boolean);
  const canStart = blockers.length === 0;

  const setText = (qid, sid, value) =>
    setTexts((p) => ({ ...p, [qid]: { ...(p[qid] || {}), [sid]: value } }));
  const setStat = (qid, sid, value) =>
    setStatus((p) => ({ ...p, [qid]: { ...(p[qid] || {}), [sid]: value } }));
  const setErr = (qid, sid, value) =>
    setErrors((p) => ({ ...p, [qid]: { ...(p[qid] || {}), [sid]: value } }));

  // 아이디어·트랙이 바뀌었으면 이전 조사 결과를 전부 버린다. 조사를 시작하는 지점에서 한 번만 확인한다.
  function dropStaleResearch() {
    const key = researchSignature(form);
    if (key === researchKeyRef.current) return;
    researchKeyRef.current = key;
    researchRef.current = {};
    setResearchState({});
    setResearchWarm(false);
  }

  // 문항 하나에 대한 조사. 이미 받아온 게 있으면 재사용(백엔드도 7일 디스크 캐시).
  // 진행 중인 조사 Promise. 카드 단계의 선조사(prefetchResearch)와 생성 단계가 같은 문항을 동시에 요청하면 하나만 돈다.
  const researchInflight = useRef({});
  async function ensureResearch(qid) {
    dropStaleResearch();
    // 조사는 했지만 인용할 사실이 없었던 문항은 []. JS 에서 빈 배열은 truthy 라 그대로 재사용된다 —
    // 같은 아이디어면 같은 검색을 또 돌릴 이유가 없으니 의도한 동작이다.
    // 반대로 조사가 "실패"한 경우는 아래 catch 에서 키 자체를 지워두므로 다음 생성 때 다시 시도한다.
    if (researchRef.current[qid]) return researchRef.current[qid];
    if (researchInflight.current[qid]) return researchInflight.current[qid];
    const run = (async () => {
      setResearchState((p) => ({ ...p, [qid]: { ...(p[qid] || {}), busy: true, error: "" } }));
      try {
        const r = await api.research(formForApi(), qid);
        // 인테이크가 실패해 바로 생성으로 넘어온 경우 이 조사 요청이 이 초안의 첫 쓰기가 된다 —
        // 그때 서버가 발급한 열쇠를 여기서 받아 두지 않으면 뒤따르는 생성이 저장되지 않는다 (2026-09-04).
        adoptDraftKey(r, form.draftId);
        researchRef.current[qid] = r.facts || [];
        setResearchState((p) => ({ ...p, [qid]: { busy: false, error: "", facts: r.facts || [], queries: r.queries || [], pages: r.pages || [], backend: r.backend, cached: r.cached } }));
        setResearchWarm(true);
        return researchRef.current[qid];
      } catch (e) {
        // 조사가 실패해도 생성은 계속한다 (참고자료 없이 작성).
        // ref 에는 아무것도 남기지 않는다 → 다음에 생성을 다시 돌리면 조사도 다시 시도한다.
        setResearchState((p) => ({ ...p, [qid]: { busy: false, error: String(e.message || e), facts: [] } }));
        delete researchRef.current[qid];
        return [];
      } finally {
        delete researchInflight.current[qid];
      }
    })();
    researchInflight.current[qid] = run;
    return run;
  }

  // 카드에 답하는 1~2분 동안 서버가 놀지 않게, 카드가 뜨는 순간 문항 조사를 뒤에서 시작한다 (2026-09-03).
  // 조사 캐시 키는 아이디어+문항이라(카드 답은 키에 없음) "초안 만들기" 때 그대로 재사용된다. 실패해도 조용히 — 생성 때 다시 시도한다.
  function prefetchResearch() {
    runLimited(activeQuestions.map((q) => () => ensureResearch(q.id).catch(() => [])), RESEARCH_PARALLEL).catch(() => {});
  }

  // 문항별 파이프라인 (2026-09-03): "조사가 끝난 문항부터 바로 생성". 조사 RESEARCH_PARALLEL 개와 생성 PARALLEL 개가 동시에 돈다 —
  // 백엔드가 조사 큐와 본문 큐를 나눠 IP 당 제한도 큐별이라 한 사람이 조사 3 + 생성 3 을 가질 수 있다.
  // 예전(조사 8개 전부 → 생성 8개)엔 첫 초안이 전체 조사가 끝나야 나왔고, 서버는 조사 파도 뒤 생성 파도가 몰렸다.
  // pairs 가 있으면 그 (문항, 스타일)만 만든다 (generateMissing).
  async function runPipeline(questions, pairs = null) {
    const ready = [];
    let researchDone = false;
    const research = runLimited(questions.map((q) => () => ensureResearch(q.id).then(() => { ready.push(q); })), RESEARCH_PARALLEL)
      .finally(() => { researchDone = true; });
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    const writers = Array.from({ length: PARALLEL }, async () => {
      for (;;) {
        const q = ready.shift();
        if (!q) { if (researchDone) return; await wait(250); continue; }
        for (const s of selectedStyles) if (!pairs || pairs.has(`${q.id}|${s.id}`)) await generateOne(q, s);
      }
    });
    await Promise.all([research, ...writers]);
  }

  // 생성·이어쓰기·이어받기의 공통 처리. run(opts) 은 { text, model } 을 주는 큐 호출이고,
  // opts 를 통해 job id(저장용)와 대기 순번(표시용)이 흘러나온다.
  async function runQuestionJob(q, style, run) {
    const key = `${q.id}|${style.id}`;
    setStat(q.id, style.id, "loading");
    setErr(q.id, style.id, "");
    try {
      const draftId = form.draftId;
      const res = await run({
        onSubmit: (id) => rememberJob(key, id),
        onTick: (snap) => setJobTick(q.id, style.id, { status: snap.status, position: snap.position, busy: snap.busy }),
      });
      adoptDraftKey(res, draftId);
      setText(q.id, style.id, res.text.slice(0, q.limit));
      setUsed(q.id, style.id, res.model);
      setStat(q.id, style.id, "done");
      setPicked((p) => (p[q.id] ? p : { ...p, [q.id]: style.id }));
    } catch (e) {
      setErr(q.id, style.id, e instanceof api.JobExpiredError ? t("dr.expired") : String(e.message || e));
      setStat(q.id, style.id, "error");
    } finally {
      forgetJob(key);
      setJobTick(q.id, style.id, null);
      refreshHealth();
    }
  }

  async function generateOne(q, style) {
    if (q.id === "q10" && !form.q10Public) {
      // 비공개 선택 → AI 호출 없이 고정 문장
      setText(q.id, style.id, Q10_PRIVATE_TEXT);
      setUsed(q.id, style.id, "");
      setStat(q.id, style.id, "done");
      setPicked((p) => (p[q.id] ? p : { ...p, [q.id]: style.id }));
      return;
    }
    // 아이디어를 고친 뒤 "새로 생성"을 누른 경우 옛 아이디어의 조사 결과가 참고자료로 끼어들면 안 된다.
    // 여기서는 조사를 새로 돌리지 않는다 — 참고자료 없이 쓰고, 조사는 다음 "초안 만들기"에서 다시 한다.
    dropStaleResearch();
    const references = researchRef.current[q.id] || [];
    await runQuestionJob(q, style, (opts) => api.generate(formForApi(), q.id, style.id, references, opts));
  }

  // 새로고침으로 끊긴 작업 이어받기 (V6). 큐에 남아 있으면 결과를 그대로 받고, 없으면 만료 안내를 띄운다.
  // 이어받기만으로는 부족하다: 새로고침 시점에 아직 큐에 넣지도 못한 문항은 job id 가 없어 영영 안 만들어진다
  // (9문항 중 먼저 제출된 2~3개만 채워진 채 멈춘다) → 이어받기가 끝나면 남은 문항을 계속 만든다.
  useEffect(() => {
    const pending = Object.entries(jobsRef.current);
    if (!pending.length) return;
    (async () => {
      setRunning(true);
      await Promise.all(pending.map(([key, id]) => {
        const [qid, sid] = key.split("|");
        const q = QUESTIONS.find((x) => x.id === qid);
        const style = STYLES.find((x) => x.id === sid);
        if (!q || !style) { forgetJob(key); return null; }
        // 첫 폴링(2.5초 뒤)까지는 "대기열에 넣는 중"으로 보여 새로 제출한 것처럼 오해된다 → 이어받는 중임을 먼저 알린다.
        setJobTick(qid, sid, { status: "resuming" });
        return runQuestionJob(q, style, (opts) => api.followJob(id, opts));
      }));
      await generateMissing();
      setRunning(false);
    })();
  }, []);

  // 1) 아이디어 읽기(인테이크) → 충분하면 바로 생성, 부족하면 카드 단계로
  async function startIntake() {
    setIntakeBusy(true);
    setIntakePos(null);
    // 지금 제출하는 아이디어가 이 초안의 기준이 된다 — 이후 편집은 이 글과 비교해 같은 초안인지 가른다
    setForm((f) => ({ ...f, draftIdea: f.idea }));
    try {
      const r = await api.intake(form, { onTick: (snap) => setIntakePos(snap.status === "queued" ? snap.position : null) });
      adoptDraftKey(r, form.draftId);
      setIntake(r);
      setAnswers({});
      setCardIdx(0);
      setRegen({});
      if (!r.cards?.length) await generateAll();
      else {
        setStep(1);                   // ready 여도 카드(멘토링 등)가 있으면 보여준다 — 건너뛰기 버튼이 있다
        prefetchResearch();           // 카드에 답하는 동안 문항 조사를 미리 (2026-09-03)
      }
    } catch {
      setIntake(null);
      await generateAll();           // 인테이크가 실패해도 생성은 막지 않는다
    } finally {
      setIntakeBusy(false);
      refreshHealth();
    }
  }

  const setAnswer = (slot, value) => setAnswers((p) => ({ ...p, [slot]: value }));

  // 카드 재생성: 이 슬롯의 보기가 안 맞을 때 다른 방향의 보기를 다시 받는다 (LLM 1회).
  // 이미 고른 보기(keep)는 그대로 남기고 나머지만 바뀐다. 이미 보여준 보기는 금지 목록(seen). 답(answers)은 건드리지 않는다.
  async function regenerateCard(slot) {
    const card = intake?.cards?.find((c) => c.slot === slot);
    if (!card || regen[slot]?.busy) return;
    const prev = regen[slot] || {};
    const seen = { [slot]: [...(prev.seen || []), ...card.options.map((o) => o.label)] };
    const pickedRaw = answers[slot]?.answer;
    const pickedList = (Array.isArray(pickedRaw) ? pickedRaw : pickedRaw ? [pickedRaw] : []).map((x) => x.replace(/ \(\d+명\)$/, ""));
    const keep = { [slot]: card.options.filter((o) => pickedList.includes(o.label)) };
    setRegen((p) => ({ ...p, [slot]: { ...prev, busy: true, error: undefined } }));
    try {
      const r = await api.intakeRegenerate(formForApi(), [slot], seen, prev.note || "", keep);
      const fresh = r.cards?.find((c) => c.slot === slot);
      if (!fresh) throw new Error(r.error || t("regen.fail"));
      setIntake((p) => ({ ...p, cards: p.cards.map((c) => (c.slot === slot ? fresh : c)) }));
      setRegen((p) => ({ ...p, [slot]: { ...prev, busy: false, seen: seen[slot], count: (prev.count || 0) + 1, note: "" } }));
    } catch (e) {
      setRegen((p) => ({ ...p, [slot]: { ...prev, busy: false, error: String(e.message || e) } }));
    } finally {
      refreshHealth();
    }
  }
  const setRegenNote = (slot, note) => setRegen((p) => ({ ...p, [slot]: { ...(p[slot] || {}), note } }));

  // 카드 한 장 넘기기 — 마지막이면 생성 시작
  function nextCard() {
    const total = intake?.cards?.length || 0;
    if (cardIdx + 1 >= total) generateAll();
    else setCardIdx(cardIdx + 1);
  }

  async function generateAll() {
    setRunning(true);
    setStep(2);
    // 조사는 선택이 아니라 필수 단계다 — 근거 없는 지원서를 만들지 않기 위해. 문항별로 조사가 끝나는 대로 바로 쓴다.
    await runPipeline(activeQuestions);
    setRunning(false);
    refreshHealth();
  }

  // 본문이 아직 비어 있는 (문항, 스타일) 쌍. 무엇을 더 만들어야 하는지 판단하는 기준이다.
  const missingPairs = (src) => {
    const out = [];
    for (const q of activeQuestions) for (const s of selectedStyles) if (!(src[q.id]?.[s.id] || "").trim()) out.push([q, s]);
    return out;
  };

  // 서버 저장본으로 빈 문항을 채운다. 돌려주는 값: 아직 빈 (문항, 스타일) 수. 학생이 이미 쓴 글은 덮지 않는다.
  function applyServerTexts(row) {
    const latest = latestTexts(row);
    const merged = { ...textsRef.current };
    for (const [qid, byStyle] of Object.entries(latest)) {
      if (qid === "q10" && !form.q10Public) continue;                       // 비공개 Q10 은 고정 문장 유지
      for (const [sid, g] of Object.entries(byStyle)) {
        if ((merged[qid]?.[sid] || "").trim() || !g.text) continue;
        const q = QUESTIONS.find((x) => x.id === qid);
        const text = q ? g.text.slice(0, q.limit) : g.text;
        merged[qid] = { ...(merged[qid] || {}), [sid]: text };
        setText(qid, sid, text);
        setUsed(qid, sid, g.model);
        setStat(qid, sid, "done");
        setPicked((p) => (p[qid] ? p : { ...p, [qid]: sid }));
      }
    }
    textsRef.current = merged;
    return missingPairs(merged).length;
  }

  // /drafts/{id} 를 한 번 받아 반영하고, 서버가 아직 채우는 중이면 true (→ 호출자가 다시 폴링).
  async function syncFromServer() {
    if (!form.draftId) return false;
    try {
      const row = await api.getDraft(form.draftId, form.draftKey);
      adoptDraftKey(row, form.draftId);
      const left = applyServerTexts(row);
      const active = !!row.finisher?.enabled && left > 0;
      setAutoFill(active ? { remaining: left, inProgress: row.finisher?.in_progress || [] } : null);
      return active;
    } catch {
      setAutoFill(null);                                                     // 404(아직 저장 전)·네트워크 — 조용히
      return false;
    }
  }

  // 초안 화면에서 빈 문항이 있고 이 탭이 만드는 중이 아니면: 서버에 결과가 있는지 보고, 마무리 작업자가 켜져 있으면 15초마다 다시 본다 (최대 20분).
  useEffect(() => {
    if (step !== 2 || running || Object.keys(jobsRef.current).length) return undefined;
    if (!missingPairs(textsRef.current).length) { setAutoFill(null); return undefined; }
    let stopped = false;
    const started = Date.now();
    const loop = async () => {
      const active = await syncFromServer();
      if (stopped) return;
      if (!active || Date.now() - started > 20 * 60_000) { setAutoFill(null); return; }
      setTimeout(loop, 15_000);
    };
    loop();
    return () => { stopped = true; };
  }, [step, running]);

  // 공유 링크(?share=토큰) 또는 예전 개인 링크(?draft=id): 이 브라우저의 저장본과 다른 초안이면 서버 저장본으로 화면을 세운다
  // (다른 PC·폰에서 이어 보기). 같은 초안이면 아무것도 안 한다 — 저장본이 더 최신일 수 있다.
  useEffect(() => {
    const share = shareFromUrl();
    const id = share ? "" : draftIdFromUrl();
    if (!share && !id) return;
    if (id && id === saved?.form?.draftId) return;
    if (share && share === saved?.form?.shareToken) return;
    (async () => {
      try {
        const row = share ? await api.getShared(share) : await api.getDraft(id);
        const did = row.draft_id || id;
        const styles = [...new Set((row.generations || []).map((g) => g.style).filter((sid) => STYLES.some((x) => x.id === sid)))];
        setForm((f) => ({
          ...f, track: TRACKS[row.track] ? row.track : "tech", idea: row.idea || "", isBusiness: false, currentItem: "",
          team: row.team || "팀원 없음", capability: row.capability || "",
          styles: styles.length ? styles : [STYLES[0].id], draftId: did, draftIdea: row.idea || "", shareToken: share || "",
          draftKey: row.draft_key || "",       // 공유 링크로 받은 초안은 열쇠도 함께 와서 이 기기에서 이어 쓸 수 있다
        }));
        restoredAnswersRef.current = row.answers || [];
        setAnswers(answersFromServer(row.answers));
        setIntake(null); setTexts({}); setStatus({}); setPicked({}); setModelUsed({});
        textsRef.current = {};
        jobsRef.current = {}; saveJobs({});
        const left = applyServerTexts(row);
        setAutoFill(row.finisher?.enabled && left > 0 ? { remaining: left, inProgress: row.finisher?.in_progress || [] } : null);
        setStep(2);
      } catch (e) {
        alert(`${t("dr.notfound")}${e.message || e}`);
      }
    })();
  }, []);

  // "공유 링크 만들기": 서버에서 토큰을 받아 폼에 보관하고 바로 복사까지 시도한다 (Safari 는 비동기 뒤 복사가 막힐 수 있어
  // 링크가 화면에 남고 복사 버튼을 한 번 더 누르면 된다). 서버에 아직 없는 초안(생성 전·테스트 모드)은 실패 문구.
  const [shareBusy, setShareBusy] = useState(false);
  const [shareError, setShareError] = useState("");
  async function makeShareLink() {
    if (!form.draftId || shareBusy) return;
    setShareBusy(true); setShareError("");
    try {
      const r = await api.shareDraft(form.draftId, form.draftKey);
      const token = r.share || "";
      if (!token) throw new Error("empty");
      setForm((f) => (f.draftId === form.draftId ? { ...f, shareToken: token } : f));
      copy("link", shareLinkFor(token));
    } catch (e) {
      setShareError(String(e.message || e));
    } finally {
      setShareBusy(false);
    }
  }

  // 남은 문항만 이어서 만든다. 이어받기 직후 자동으로, 그리고 "남은 N개 계속 만들기" 버튼에서 부른다.
  async function generateMissing() {
    const todo = missingPairs(textsRef.current);
    if (!todo.length) return;
    setRunning(true);
    const qids = new Set(todo.map(([q]) => q.id));
    await runPipeline(activeQuestions.filter((q) => qids.has(q.id)), new Set(todo.map(([q, s]) => `${q.id}|${s.id}`)));
    setRunning(false);
    refreshHealth();
  }

  async function extend(q, style) {
    const current = texts[q.id]?.[style.id] || "";
    if (q.limit - current.length < 150) return;
    dropStaleResearch();
    const references = researchRef.current[q.id] || [];
    await runQuestionJob(q, style, (opts) => api.extend(formForApi(), q.id, style.id, current, references, opts));
  }

  // ── 읽기 번역 (2026-09-03) ──
  // 카드 문구: 인테이크가 오거나 언어를 바꾸면 아직 번역 안 된 한국어 문구만 모아 한 번에 번역한다 (목록 모드, 40개씩 호출).
  const cardStringsOf = (c) => {
    const out = [];
    for (const v of [c?.question, c?.why, c?.label]) if (v) out.push(v);
    for (const o of c?.options || []) { if (o.label) out.push(o.label); if (o.hint) out.push(o.hint); }
    return out;
  };
  const cardStrings = () => {
    const out = new Set();
    if (intake?.summary) out.add(intake.summary);
    for (const sl of intake?.slots || []) if (sl.label) out.add(sl.label);
    for (const c of intake?.cards || []) for (const v of cardStringsOf(c)) out.add(v);
    return [...out];
  };
  // 진행 중 요청은 ref 로 하나만. 결과는 요청 당시 언어(reqLang)의 지도에 합친다 — 상태가 바뀌어도 버리지 않는다.
  // 빈 결과·실패 문구는 저장본에 남기지 않고(다음 방문 때 다시 시도) 이번 세션 안에서만 cardFailedRef 로 재시도를 막는다.
  // 순서 (2026-09-04, 사용자 관찰 "언어를 바꿔도 카드가 한국어"): 서버 번역은 80문구에 15~18초라 그동안 한국어가 보였다.
  //   ① 지금 보고 있는 카드의 문구만 먼저(작은 요청, 3~5초) ② 이 언어의 나머지 ③ 다른 외국어를 미리(언어를 바꿔도 기다리지 않게).
  const FOREIGN_LANGS = LANGS.map((l) => l.id).filter((id) => id !== "ko");
  const cardReqRef = useRef(null);
  const cardFailedRef = useRef({});                 // cardFailedRef[lang] = Set(한국어 원문)
  const [cardTick, setCardTick] = useState(0);       // 요청이 끝나거나 "다시 시도" 를 누르면 effect 를 한 번 더 돌린다
  const cardPending = (L) => {
    const have = cardI18n?.maps?.[L] || {}; const failed = cardFailedRef.current[L] || new Set();
    return cardStrings().filter((sKo) => !have[sKo] && !failed.has(sKo));
  };
  const cardUntranslated = foreign ? cardStrings().filter((sKo) => !cardI18n?.maps?.[lang]?.[sKo]).length : 0;
  // 이 카드에 아직 이 언어로 안 바뀐 문구가 있는지 (카드 안의 "번역 중" 표시용)
  const cardNeedsTr = (c) => foreign && cardStringsOf(c).some((sKo) => !cardI18n?.maps?.[lang]?.[sKo]);
  useEffect(() => {
    if (!foreign || !intake || cardReqRef.current) return;
    let reqLang = lang;
    let todo = cardPending(lang);
    if (todo.length) {
      const cur = intake.cards?.[cardIdx];
      const first = new Set([intake.summary, ...cardStringsOf(cur)].filter(Boolean));
      const head = todo.filter((sKo) => first.has(sKo));
      if (head.length && head.length < todo.length) todo = head;          // ① 보고 있는 카드 먼저
    } else {
      const other = FOREIGN_LANGS.find((L) => L !== lang && cardPending(L).length);   // ③ 다른 외국어 미리 받기
      if (!other) return;
      reqLang = other; todo = cardPending(other);
    }
    const failed = cardFailedRef.current[reqLang] || new Set();
    cardReqRef.current = { lang: reqLang };
    setCardBusy(true);
    api.translate(todo, reqLang, form.draftId)
      .then((r) => {
        const got = {}; const bad = new Set(failed);
        todo.forEach((sKo, i) => { const v = (r.translations?.[i] || "").trim(); if (v) got[sKo] = v; else bad.add(sKo); });
        cardFailedRef.current[reqLang] = bad;
        // 요청 당시 언어(reqLang)의 지도에만 합친다 — 그새 언어를 바꿨어도 결과는 남고, 바꾼 언어는 다음 effect 가 받는다
        setCardI18n((prev) => ({ maps: { ...(prev?.maps || {}), [reqLang]: { ...(prev?.maps?.[reqLang] || {}), ...got } }, error: "" }));
      })
      .catch((e) => {
        todo.forEach((sKo) => failed.add(sKo));
        cardFailedRef.current[reqLang] = failed;
        // 미리 받던 다른 언어의 실패는 화면에 알리지 않는다 (그 언어로 바꾸면 "다시 시도" 로 다시 받는다)
        if (reqLang === lang) setCardI18n((prev) => ({ maps: prev?.maps || {}, error: String(e.message || e) }));
      })
      .finally(() => { cardReqRef.current = null; setCardBusy(false); setCardTick((n) => n + 1); });
  }, [foreign, lang, intake, cardI18n, cardTick, cardIdx]);
  const retryCardTranslation = () => { cardFailedRef.current[lang] = new Set(); setCardTick((n) => n + 1); };
  // 번역 진행·미번역 안내 한 줄 (카드 화면 머리와 초안 화면의 "정보 보태기" 패널에 같이 쓴다)
  const cardI18nNote = () => {
    if (!foreign || cardUntranslated === 0) return null;     // 이 언어는 다 됐으면(다른 언어를 미리 받는 중이어도) 조용히
    if (cardBusy) return <p className="text-xs text-indigo-700 mt-2">{t("card.translating", { lang: LANG_NAMES[lang] })}</p>;
    if (cardUntranslated > 0) return (
      <p className="text-xs text-amber-700 mt-2">
        {cardI18n?.error ? t("card.translate.fail") : t("card.untranslated", { n: cardUntranslated })}{" "}
        <button onClick={retryCardTranslation} className="underline">{t("card.retry")}</button>
      </p>
    );
    return null;
  };

  // 문항 본문: 평문 모드 1회. 원문(source)을 같이 저장해 학생이 글을 고치면 "이전 원문 기준" 으로 표시한다.
  const TRANSLATE_PARALLEL = 3;
  async function translateQuestion(q, koText) {
    const key = `${q.id}|${lang}`;
    if (!foreign || !koText.trim() || transBusy[key]) return;
    setTransBusy((p) => ({ ...p, [key]: true }));
    try {
      const r = await api.translate([koText], lang, form.draftId);
      const out = (r.translations?.[0] || "").trim();
      if (!out) throw new Error("empty");
      setTrans((p) => ({ ...p, [q.id]: { ...(p[q.id] || {}), [lang]: { text: out, source: koText, error: "" } } }));
    } catch (e) {
      setTrans((p) => ({ ...p, [q.id]: { ...(p[q.id] || {}), [lang]: { ...(p[q.id]?.[lang] || {}), source: koText, error: String(e.message || e) } } }));
    } finally {
      setTransBusy((p) => { const { [key]: _drop, ...rest } = p; return rest; });
    }
  }
  // 초안·제출 화면에서 완성된 문항 중 이 언어 번역이 없는 것을 자동으로 번역한다 (동시 3개). 실패한 것은 버튼으로만 다시.
  useEffect(() => {
    if (!foreign || (step !== 2 && step !== 3)) return;
    let slots = TRANSLATE_PARALLEL - Object.keys(transBusy).length;
    for (const q of activeQuestions) {
      if (slots <= 0) break;
      const cur = picked[q.id] || selectedStyles[0]?.id;
      const text = texts[q.id]?.[cur] || "";
      if (!text.trim() || status[q.id]?.[cur] !== "done") continue;
      const cur_t = trans[q.id]?.[lang];
      if (cur_t?.text || cur_t?.error || transBusy[`${q.id}|${lang}`]) continue;
      translateQuestion(q, text);
      slots--;
    }
  }, [foreign, lang, step, texts, status, picked, transBusy, trans]);

  // 초안 아래에 붙는 번역 상자. 한국어 화면에서는 아무것도 안 그린다.
  function renderTranslation(q, text) {
    if (!foreign || !text) return null;
    const cur_t = trans[q.id]?.[lang];
    const busy = !!transBusy[`${q.id}|${lang}`];
    const stale = !!cur_t?.text && cur_t.source !== text;
    return (
      <div className="mt-3 p-3 rounded-lg bg-emerald-50 border border-emerald-100 text-sm">
        <div className="text-[11px] text-emerald-800 mb-1 flex items-center justify-between gap-2 flex-wrap">
          <span>{t("tr.head", { lang: LANG_NAMES[lang] })}</span>
          {!busy && (stale || cur_t?.error || !cur_t?.text) && (
            <button onClick={() => translateQuestion(q, text)} className="underline">{cur_t?.text ? t("tr.redo") : t("tr.show")}</button>
          )}
        </div>
        {busy && <div className="text-xs text-slate-500">{t("tr.busy")}</div>}
        {!busy && stale && <div className="text-xs text-amber-700 mb-1">{t("tr.stale")}</div>}
        {!busy && cur_t?.error && !cur_t?.text && <div className="text-xs text-red-600">{t("tr.fail")}{cur_t.error}</div>}
        {/* 문항 본문이 2,000자까지라 번역도 길다 — 일정 높이 안에서 스크롤 (2026-09-04 사용자 요청). */}
        {cur_t?.text && <div className="max-h-56 overflow-y-auto pr-2"><p className="whitespace-pre-wrap leading-relaxed text-slate-700">{cur_t.text}</p></div>}
      </div>
    );
  }

  // 복사는 조용히 실패할 수 있다 (비보안 컨텍스트·권한 거부). 알려주지 않으면 붙여넣기가 안 되는 이유를 모른다.
  function copy(key, value) {
    const mark = (k, ms) => { setCopied(k); setTimeout(() => setCopied(""), ms); };
    const fail = () => mark(`${key}!`, 3000);
    if (!navigator.clipboard?.writeText) return fail();
    navigator.clipboard.writeText(value).then(() => mark(key, 1500), fail);
  }
  // 복사 버튼 문구 — 성공은 1.5초, 실패는 3초 동안 바뀐다.
  const copyLabel = (key, idle = t("copy"), done = t("copied")) =>
    copied === key ? done : copied === `${key}!` ? t("copy.fail") : idle;

  const doneCount = activeQuestions.filter((q) => picked[q.id] && texts[q.id]?.[picked[q.id]]).length;
  const missingCount = missingPairs(texts).length;

  // ───────── 렌더 ─────────
  return (
    <LangContext.Provider value={{ lang, t, tr }}>
    <div className="min-h-screen bg-white text-slate-900" style={{ fontFamily: "'Apple SD Gothic Neo','Noto Sans KR','Malgun Gothic','Noto Sans SC','Noto Sans JP',sans-serif" }}>
      <header className="border-b border-slate-200">
        <div className="max-w-5xl mx-auto px-5 pt-3 flex justify-end">
          {/* 언어 선택 (2026-09-03). 화면 문구만 바뀐다 — 초안은 한국어로 만들어지고 외국어 화면에선 번역을 병기한다. */}
          {/* ?test=1 로 연 탭: 요청이 서비스 DB 에 안 남는다 (api.js testMode). 운영자가 알아보게 배지로. */}
          {api.testMode() && <span className="mr-auto text-xs px-2 py-0.5 rounded-md bg-amber-100 text-amber-900 border border-amber-300" title="?test=0 으로 끄기">{t("hdr.testmode")}</span>}
          <div className="flex items-center gap-1 text-xs" role="group" aria-label={t("lang.label")}>
            <span className="text-slate-400 mr-1" aria-hidden="true">🌐</span>
            {LANGS.map((l) => (
              <button key={l.id} onClick={() => setLang(l.id)} lang={l.id} aria-pressed={lang === l.id}
                className={`px-2 py-0.5 rounded-md border ${lang === l.id ? "border-slate-900 bg-slate-900 text-white" : "border-slate-200 text-slate-600 hover:border-slate-400"}`}>
                {l.label}
              </button>
            ))}
          </div>
        </div>
        <div className="max-w-5xl mx-auto px-5 pb-5 pt-2 flex items-end justify-between gap-4 flex-wrap">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">{t("app.title")}</h1>
            <p className="text-sm text-slate-500 mt-1">{t("app.subtitle")}</p>
          </div>
          <nav className="flex gap-1 text-sm">
            {[0, 1, 2, 3].map((i) => {
              const n = t(`step.${i}`);
              const started = Object.keys(status).length > 0;
              const enabled = i === 0 || (i === 1 && intake?.cards?.length > 0) || (i === 2 && started) || (i === 3 && doneCount > 0);
              return (
                <button key={n} disabled={!enabled} onClick={() => enabled && setStep(i)}
                  className={`px-3 py-1.5 rounded-full ${step === i ? "bg-indigo-600 text-white" : enabled ? "text-slate-500 hover:bg-slate-100" : "text-slate-300"}`}>
                  {i + 1}. {n}
                </button>
              );
            })}
          </nav>
        </div>
        <div className="max-w-5xl mx-auto px-5 pb-3 text-xs flex items-center gap-4 flex-wrap">
          {server === null && <span className="text-slate-400">{t("hdr.checking")}</span>}
          {server?.ok && (
            <>
              {/* 고를 수 있는 모델이 하나뿐이면 드롭다운은 고르라는 신호만 주고 할 일이 없다 → 글자로만 보여준다. */}
              {models.length <= 1 ? (
                <span className="text-slate-600">{t("hdr.model")} {models[0]?.name || server.model}</span>
              ) : (
                <label className="flex items-center gap-2 text-slate-600">
                  <span>{t("hdr.model")}</span>
                  <select value={form.model || ""} onChange={(e) => setForm({ ...form, model: e.target.value })}
                    className="px-2 py-1 rounded-md border border-slate-300 bg-white text-xs max-w-[220px]">
                    {models.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
                  </select>
                </label>
              )}
              {models.find((m) => m.id === form.model)?.note && (
                <span className="text-slate-400 hidden md:inline">{models.find((m) => m.id === form.model).note}</span>
              )}
              {server.usage?.limit != null && (() => {
                const { used, limit, remaining, by_model: byModel = {} } = server.usage;
                const tone = remaining <= 0 ? "bg-red-50 text-red-700 border-red-200"
                  : remaining <= 10 ? "bg-amber-50 text-amber-800 border-amber-200"
                  : "bg-slate-50 text-slate-600 border-slate-200";
                const breakdown = Object.entries(byModel).map(([id, n]) => `${shortModel(id)} ${n}`).join(", ");
                const tip = [
                  t("hdr.usage.tip1"),
                  breakdown && t("hdr.usage.tip2", { b: breakdown }),
                  t("hdr.usage.tip3", { reset: server.usage.reset }),
                ].filter(Boolean).join("\n");
                return (
                  <span className={`ml-auto px-2 py-0.5 rounded-md border cursor-help ${tone}`} title={tip}>
                    {t("hdr.usage", { used, limit })}{remaining <= 0 ? t("hdr.usage.limit") : remaining <= 10 ? t("hdr.usage.left", { n: remaining }) : ""}
                  </span>
                );
              })()}
              {server.fallback && <span className="text-slate-400" title={t("hdr.fallback.tip")}>{t("hdr.fallback")}</span>}
            </>
          )}
          {server && !server.ok && (
            /* 공개 서비스라 일반 사용자가 본다 — 실행 명령 대신 사람 말로. 주소·원인은 title 에만 남겨 개발자가 확인한다. */
            <span className="text-red-600" title={`${api.API_BASE} 연결 실패: ${server.error || ""}`}>{t("hdr.down")}</span>
          )}
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-5 py-8">
        {step === 0 && (
          <div className="grid md:grid-cols-5 gap-8">
            <div className="md:col-span-3 space-y-7">
              {/* 트랙이 하나뿐이라 선택 화면 대신 안내만 둔다. 로컬 트랙을 되살리려면
                  TRACKS 에 항목을 되돌리고 이 절을 버튼 목록으로 바꾸면 된다. */}
              <section>
                <h2 className="font-semibold mb-2">{t("in.track")}</h2>
                <div className="p-3 rounded-lg border border-slate-200 bg-slate-50">
                  <div className="font-medium">{t(`track.${form.track}.name`)}</div>
                  <div className="text-xs text-slate-500 mt-1 leading-relaxed">{t(`track.${form.track}.desc`)}</div>
                </div>
              </section>

              <section>
                <h2 className="font-semibold mb-1">{t("in.idea")}</h2>
                <p className="text-xs text-slate-500 mb-2">{t("in.idea.help")}</p>
                {/* 외국어 화면: 자기 언어로 써도 된다는 안내. 초안은 한국어로 나오고(프롬프트가 고정) 번역이 병기된다. */}
                {foreign && <p className="text-xs text-indigo-800 bg-indigo-50 border border-indigo-100 rounded-lg px-3 py-2 mb-2">{t("in.idea.foreign")}</p>}
                <textarea value={form.idea} onChange={(e) => setForm(withDraft({ ...form, idea: e.target.value }))} rows={8}
                  placeholder={t("in.idea.ph")}
                  className="w-full p-3 rounded-lg border border-slate-300 focus:border-indigo-600 focus:outline-none text-sm leading-relaxed" />
                <div className="text-xs text-slate-400 text-right">
                  {t("in.chars", { n: form.idea.length })}
                  {ideaLen < IDEA_MIN && t("in.idea.min", { min: IDEA_MIN })}
                  {ideaLen >= IDEA_MIN && ideaLen < 60 && t("in.idea.short")}
                </div>
              </section>

              <section>
                <h2 className="font-semibold mb-1">{t("in.cap")}</h2>
                <p className="text-xs text-slate-500 mb-2">{t("in.cap.help")}</p>
                <textarea value={form.capability} onChange={(e) => setForm({ ...form, capability: e.target.value })} rows={4}
                  placeholder={t("in.cap.ph")}
                  className="w-full p-3 rounded-lg border border-slate-300 focus:border-indigo-600 focus:outline-none text-sm leading-relaxed" />
              </section>
            </div>

            <div className="md:col-span-2 space-y-7">
              {/* "현재 창업 여부"(사업자 선택)는 2026-09-03 뺐다 — 사업자 여부·Q7 은 모두의 창업 사이트에서 직접 입력한다.
                  isBusiness 는 항상 false 라 Q7-1 은 안 나온다. 백엔드 스키마·프롬프트(q7_1)는 그대로 살아 있다. */}
              <section>
                <h2 className="font-semibold mb-2">{t("in.team")}</h2>
                {/* value 는 한국어 원문(서버 스키마) — 표시만 번역한다 */}
                <select value={form.team} onChange={(e) => setForm({ ...form, team: e.target.value })}
                  className="w-full p-2 rounded-lg border border-slate-300 text-sm bg-white">
                  {TEAM_OPTIONS.map((opt) => <option key={opt} value={opt}>{t(`team.${opt}`)}</option>)}
                </select>
              </section>

              <section>
                <h2 className="font-semibold mb-2">{t("in.field")}</h2>
                <select value={form.field} onChange={(e) => setForm({ ...form, field: e.target.value })}
                  className="w-full p-2 rounded-lg border border-slate-300 text-sm bg-white">
                  <option value="">{t("in.field.later")}</option>
                  {TRACKS[form.track].fields.map((f) => <option key={f} value={f}>{t(`field.${f}`)}</option>)}
                </select>
              </section>

              <section>
                <h2 className="font-semibold mb-1">{t("in.q10")}</h2>
                <p className="text-xs text-slate-500 mb-2">{t("in.q10.help")}</p>
                <div className="flex gap-2">
                  {[true, false].map((v) => (
                    <button key={String(v)} onClick={() => setForm({ ...form, q10Public: v })}
                      className={`flex-1 py-2 rounded-lg border text-sm ${form.q10Public === v ? "border-indigo-600 bg-indigo-50" : "border-slate-200"}`}>
                      {v ? t("in.public") : t("in.private")}
                    </button>
                  ))}
                </div>
              </section>

              <section>
<div className="mb-4 p-3 rounded-lg bg-sky-50 border border-sky-100 text-xs text-slate-600">
                  <span className="font-medium text-slate-800">{t("in.research.head")}</span>{t("in.research.body")}
                </div>
                {/* 스타일도 하나뿐이라 선택 UI 를 접었다 (작업 11). STYLES 에 항목을 되돌리면 여기를 버튼 목록으로 되살린다. */}
                <h2 className="font-semibold mb-2">{t("in.style")}</h2>
                <div className="p-3 rounded-lg border border-slate-200 bg-slate-50">
                  <div className="font-medium text-sm">{t(`style.${STYLES[0].id}.name`)}</div>
                  <div className="text-xs text-slate-500 mt-1">{t(`style.${STYLES[0].id}.desc`)}</div>
                </div>
              </section>

              <button onClick={startIntake} disabled={!canStart || intakeBusy}
                className="w-full py-3 rounded-lg bg-indigo-600 text-white font-medium disabled:bg-slate-300 disabled:cursor-not-allowed">
                {intakeBusy ? (intakePos != null && intakePos > 0 ? t("in.start.wait", { n: intakePos }) : t("in.start.reading")) : t("in.start")}
              </button>
              {intakeBusy && <p className="text-xs text-slate-500 text-center">{t("in.start.hint")}</p>}
              {!canStart && (
                <ul className="text-xs text-slate-500 space-y-0.5">
                  {blockers.map((b) => <li key={b}>· {b}</li>)}
                </ul>
              )}
            </div>
          </div>
        )}

        {step === 1 && intake && (() => {
          const cards = intake.cards || [];
          const card = cards[cardIdx];
          const known = intake.slots.filter((s) => s.status === "known");
          return (
            <div className="max-w-2xl mx-auto space-y-6">
              <div>
                <h2 className="text-lg font-semibold">{intake.ready ? t("card.title.ready") : t("card.title")}</h2>
                <p className="text-sm text-slate-500 mt-1">
                  {intake.ready ? t("card.skip.hint") : ""}
                  {t("card.help1")}<b>{t("card.help.btn")}</b>{t("card.help2")}
                </p>
                {cardI18nNote()}
              </div>

              {(intake.summary || known.length > 0) && (
                <div className="p-3 rounded-lg bg-slate-50 border border-slate-200 text-xs text-slate-600 space-y-1">
                  {intake.summary && <div><span className="font-medium text-slate-700">{t("card.understood")}</span> {tr(intake.summary)}</div>}
                  {known.length > 0 && <div><span className="font-medium text-slate-700">{t("card.confirmed")}</span> {known.map((s) => tr(s.label)).join(" · ")}</div>}
                </div>
              )}

              {card && (
                <section className="border border-slate-200 rounded-xl p-5 space-y-4">
                  <div className="flex items-center justify-between text-xs text-slate-400">
                    <span>{cardIdx + 1} / {cards.length} · {tr(card.label)}</span>
                    <span>{tr(card.why)}</span>
                  </div>
                  <h3 className="font-semibold text-base">{tr(card.question)}</h3>
                  {/* 외국어 화면: 이 카드의 보기가 아직 한국어면 카드 안에서도 알린다 (머리의 한 줄은 놓치기 쉽다) */}
                  {cardNeedsTr(card) && cardBusy && <p className="text-xs text-indigo-700">{t("card.translating.card", { lang: LANG_NAMES[lang] })}</p>}
                  <div className={cardNeedsTr(card) && cardBusy ? "opacity-60 transition-opacity" : ""}>
                    <CardOptions key={`${card.slot}-${regen[card.slot]?.count || 0}`} card={card} value={answers[card.slot]} onChange={(v) => setAnswer(card.slot, v)} />
                  </div>
                  <RegenerateBar slot={card.slot} state={regen[card.slot]} onNote={setRegenNote} onRun={regenerateCard} />
                  <div className="flex items-center justify-between pt-2 flex-wrap gap-2">
                    <button onClick={() => setCardIdx(Math.max(cardIdx - 1, 0))} disabled={cardIdx === 0} className="text-sm text-slate-500 underline disabled:text-slate-300">{t("card.prev")}</button>
                    <div className="flex gap-2">
                      <button onClick={generateAll} className="px-3 py-2 text-sm rounded-lg border border-slate-300">{t("card.skip")}</button>
                      <button onClick={nextCard} className="px-4 py-2 text-sm rounded-lg bg-indigo-600 text-white">
                        {cardIdx + 1 >= cards.length ? t("card.make") : t("card.next")}
                      </button>
                    </div>
                  </div>
                </section>
              )}

              <div className="flex gap-1 justify-center">
                {cards.map((c, i) => (
                  <button key={c.slot} onClick={() => setCardIdx(i)} title={tr(c.label)}
                    className={`w-2.5 h-2.5 rounded-full ${i === cardIdx ? "bg-indigo-600" : answers[c.slot]?.unknown ? "bg-amber-400" : answers[c.slot]?.answer ? "bg-emerald-500" : "bg-slate-200"}`} />
                ))}
              </div>
            </div>
          );
        })()}

        {step === 2 && (
          <div className="space-y-8">
            {intake?.summary && (
              <p className="text-xs text-slate-500">{t("dr.understood")}{tr(intake.summary)}{answeredCount > 0 && t("dr.answers", { n: answeredCount })}</p>
            )}
            {autoFill && (
              <p className="text-xs text-indigo-700 bg-indigo-50 rounded-lg px-3 py-2">
                {t("dr.autofill", { n: autoFill.remaining })}
              </p>
            )}
            {form.draftId && form.shareToken && (
              <p className="text-xs text-slate-500 break-all">
                {t("dr.link")}<span className="font-mono">{shareLinkFor(form.shareToken)}</span>{" "}
                <button onClick={() => copy("link", shareLinkFor(form.shareToken))} className="underline">{copied === "link" ? t("copied") : copied === "link!" ? t("copy.fail.short") : t("copy")}</button>
              </p>
            )}
            {form.draftId && !form.shareToken && Object.keys(status).length > 0 && (
              <p className="text-xs text-slate-500">
                <button onClick={makeShareLink} disabled={shareBusy} className="underline disabled:opacity-50">{shareBusy ? t("dr.share.busy") : t("dr.share")}</button>
                {shareError && <span className="ml-2 text-red-600">{t("dr.share.fail")}{shareError}</span>}
              </p>
            )}
            <div className="flex items-center justify-between flex-wrap gap-3">
              <p className="text-sm text-slate-600">
                {running ? t("dr.generating") : t("dr.picked", { done: doneCount, total: activeQuestions.length })}
              </p>
              <div className="flex gap-2">
                {/* 새로고침 등으로 중간에 끊기면 큐에 못 들어간 문항이 남는다 — 이어서 만들 수단을 눈에 보이게 둔다. */}
                {!running && missingCount > 0 && Object.keys(status).length > 0 && (
                  <button onClick={generateMissing} className="px-3 py-1.5 text-sm rounded-lg border border-indigo-300 text-indigo-700">{t("dr.continue", { n: missingCount })}</button>
                )}
                <button onClick={() => setStep(0)} className="px-3 py-1.5 text-sm rounded-lg border border-slate-300">{t("dr.edit")}</button>
                {/* 저장본까지 비우고 처음부터. 새로고침으로는 안 지워지므로 명시적 버튼이 필요하다. */}
                <button onClick={() => { if (confirm(t("dr.restart.confirm"))) { try { localStorage.removeItem(SAVE_KEY); sessionStorage.removeItem(JOBS_KEY); } catch { /* 접근 차단 */ } location.reload(); } }}
                  className="px-3 py-1.5 text-sm rounded-lg border border-slate-300 text-slate-500">{t("dr.restart")}</button>
                <button onClick={() => setStep(3)} disabled={doneCount === 0} className="px-3 py-1.5 text-sm rounded-lg bg-indigo-600 text-white disabled:bg-slate-300">{t("dr.final")}</button>
              </div>
            </div>

            {activeQuestions.map((q) => {
              const cur = picked[q.id] || selectedStyles[0]?.id;
              const text = texts[q.id]?.[cur] || "";
              const st = status[q.id]?.[cur];
              const ratio = Math.min(text.length / q.limit, 1);
              return (
                <section key={q.id} className="border border-slate-200 rounded-xl overflow-hidden">
                  <div className="px-5 pt-4 pb-3 flex items-start justify-between gap-4 flex-wrap">
                    <div>
                      <div className="text-xs text-indigo-600 font-semibold">{q.label}</div>
                      <h3 className="font-semibold">{t(`q.${q.id}`)}</h3>
                    </div>
                    <div className="flex gap-1">
                      {selectedStyles.map((s) => {
                        const ss = status[q.id]?.[s.id];
                        const isPick = picked[q.id] === s.id;
                        return (
                          <button key={s.id} onClick={() => setPicked({ ...picked, [q.id]: s.id })}
                            className={`px-3 py-1.5 text-sm rounded-lg border flex items-center gap-1.5 ${cur === s.id ? "border-slate-900 bg-slate-900 text-white" : "border-slate-200 text-slate-600"}`}>
                            {ss === "loading" && <span className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" />}
                            {ss === "error" && <span className="w-2 h-2 rounded-full bg-red-500" />}
                            {isPick && ss === "done" && <span className="w-2 h-2 rounded-full bg-emerald-400" />}
                            {t(`style.${s.id}.name`)}
                          </button>
                        );
                      })}
                    </div>
                  </div>

                  <ResearchPanel state={researchState[q.id]} warm={researchWarm} />

                  <div className="px-5 pb-4">
                    {st === "loading" && <JobProgress info={jobInfo[q.id]?.[cur]} />}
                    {st === "loading" && !text && <div className="h-32 rounded-lg bg-slate-50 animate-pulse" />}
                    {/* 조사는 3개씩 병렬로 돈다 — 아직 차례가 안 온 문항은 상태가 아예 없어서 제목만 있는 빈 카드로 보였다. */}
                    {!st && running && (
                      <>
                        {!researchState[q.id]?.busy && <div className="text-xs text-slate-500 mb-2">{t("dr.waiting")}</div>}
                        <div className="h-32 rounded-lg bg-slate-50" />
                      </>
                    )}
                    {st === "error" && (
                      <div className="p-4 rounded-lg bg-red-50 text-sm text-red-700 flex justify-between items-center gap-3">
                        <span>{t("dr.fail")}{errors[q.id]?.[cur] ? <span className="text-red-600">{errors[q.id][cur]}</span> : t("dr.fail.hint")}</span>
                        <button onClick={() => generateOne(q, STYLES.find((s) => s.id === cur))} className="underline shrink-0">{t("dr.retry")}</button>
                      </div>
                    )}
                    {(st === "done" || (st === "loading" && text)) && (
                      <>
                        <textarea value={text} onChange={(e) => setText(q.id, cur, e.target.value.slice(0, q.limit))}
                          rows={q.limit > 100 ? 12 : 2}
                          className="w-full p-3 rounded-lg border border-slate-200 focus:border-indigo-600 focus:outline-none text-sm leading-relaxed" />
                        <div className="mt-2 h-1 rounded bg-slate-100 overflow-hidden">
                          <div className="h-full bg-indigo-600" style={{ width: `${ratio * 100}%` }} />
                        </div>
                        <div className="mt-2 flex items-center justify-between flex-wrap gap-2 text-xs text-slate-500">
                          <span>
                            {t("dr.chars", { n: text.length, limit: q.limit })}
                            {modelUsed[q.id]?.[cur] && (
                              <span className={`ml-2 ${modelUsed[q.id][cur] !== form.model ? "text-amber-600" : "text-slate-400"}`}
                                title={modelUsed[q.id][cur] !== form.model ? t("dr.fallback.tip") : t("dr.model.tip")}>
                                · {shortModel(modelUsed[q.id][cur])}{modelUsed[q.id][cur] !== form.model && t("dr.fallback")}
                              </span>
                            )}
                          </span>
                          <div className="flex gap-3">
                            {q.limit > 100 && q.limit - text.length >= 150 && (
                              <button onClick={() => extend(q, STYLES.find((s) => s.id === cur))} disabled={st === "loading"} className="underline disabled:text-slate-300">
                                {st === "loading" ? t("dr.extending") : t("dr.extend")}
                              </button>
                            )}
                            <button onClick={() => generateOne(q, STYLES.find((s) => s.id === cur))} disabled={st === "loading"} className="underline disabled:text-slate-300">{t("dr.new")}</button>
                            <button onClick={() => copy(q.id + cur, text)} className="underline">{copyLabel(q.id + cur)}</button>
                            {cardsForQuestion(q.id).length > 0 && (
                              <button onClick={() => setAssistOpen((p) => ({ ...p, [q.id]: !p[q.id] }))} className="underline text-indigo-600">
                                {assistOpen[q.id] ? t("dr.assist.close") : t("dr.assist")}
                              </button>
                            )}
                          </div>
                        </div>
                        {/* 외국어 화면: 완성된 한국어 초안 아래에 읽기 번역 (2026-09-03) */}
                        {st === "done" && renderTranslation(q, text)}
                        {assistOpen[q.id] && (
                          <div className="mt-3 p-4 rounded-lg bg-indigo-50/50 border border-indigo-100 space-y-4">
                            <p className="text-xs text-slate-600">{t("dr.assist.help1")}<b>{t("dr.new")}</b>{t("dr.assist.help2")}<b>{t("dr.extend")}</b>{t("dr.assist.help3")}</p>
                            {cardI18nNote()}
                            {cardsForQuestion(q.id).map((c) => (
                              <div key={c.slot} className="space-y-1.5">
                                <div className="text-xs font-medium text-slate-700">{tr(c.question)} <span className="text-slate-400 font-normal">· {tr(c.label)}</span></div>
                                {cardNeedsTr(c) && cardBusy && <p className="text-xs text-indigo-700">{t("card.translating.card", { lang: LANG_NAMES[lang] })}</p>}
                                <div className={cardNeedsTr(c) && cardBusy ? "opacity-60 transition-opacity" : ""}>
                                  <CardOptions key={`${c.slot}-${regen[c.slot]?.count || 0}`} card={c} value={answers[c.slot]} onChange={(v) => setAnswer(c.slot, v)} compact />
                                </div>
                                <RegenerateBar slot={c.slot} state={regen[c.slot]} onNote={setRegenNote} onRun={regenerateCard} compact />
                              </div>
                            ))}
                          </div>
                        )}
                      </>
                    )}
                  </div>
                </section>
              );
            })}
          </div>
        )}

        {step === 3 && (
          <div className="space-y-6">
            <div className="flex items-center justify-between flex-wrap gap-3">
              <div>
                <h2 className="font-semibold text-lg">{t("fn.title")}</h2>
                <p className="text-sm text-slate-500">{t("fn.help")}</p>
              </div>
              {/* 전체 복사는 한국어 제출용이라 문항 제목도 한국어(q.title) 그대로 */}
              <button onClick={() => copy("all", activeQuestions.map((q) => `[${q.label}] ${q.title}\n${texts[q.id]?.[picked[q.id]] || "(미작성)"}`).join("\n\n"))}
                className="px-4 py-2 rounded-lg bg-indigo-600 text-white text-sm">{copyLabel("all", t("copy.all"), t("copied.all"))}</button>
            </div>

            {/* 외국어 화면에만: 최종 제출은 한국어로 (2026-09-03 요청). 번역문 말고 한국어 원문을 붙여넣으라는 빨간 안내. */}
            {foreign && (
              <div role="alert" className="p-4 rounded-lg bg-red-50 border-2 border-red-400 text-red-800 text-sm space-y-1">
                <div className="font-semibold">{t("fn.korean.only")}</div>
                <div className="text-xs text-red-700" lang="ko">{makeT("ko")("fn.korean.only")}</div>
              </div>
            )}

            <div className="grid md:grid-cols-2 gap-3 text-sm">
              {/* 2026-09-03: "직접 입력할 항목"(Q5·Q6·Q7·Q9·Q10·Q11·멘토 기관) 목록을 빼고, 이 화면이 실제 신청이 아니라는 안내로 바꿨다. */}
              <div className="p-4 rounded-lg bg-indigo-50 border border-indigo-200 text-indigo-900">
                <div className="font-medium mb-1">{t("fn.notreal")}</div>
                <ul className="space-y-1">
                  <li>{t("fn.notreal.1a")}<b>{t("fn.notreal.1b")}</b>{t("fn.notreal.1c")}</li>
                  <li>{t("fn.notreal.2a")}<b>{t("fn.notreal.2b")}</b>{t("fn.notreal.2c")}</li>
                  <li>{t("fn.notreal.3a")}<b>{t("fn.notreal.3b")}</b>{t("fn.notreal.3c")}</li>
                </ul>
              </div>
              <div className="p-4 rounded-lg bg-amber-50 border border-amber-200 text-amber-900">
                <div className="font-medium mb-1">{t("fn.check")}</div>
                <ul className="space-y-1">
                  <li>{t("fn.check.1")}</li>
                  <li>{t("fn.check.2")}</li>
                </ul>
              </div>
            </div>

            {activeQuestions.map((q) => {
              const text = texts[q.id]?.[picked[q.id]] || "";
              return (
                <section key={q.id} className="border border-slate-200 rounded-xl p-5">
                  <div className="flex items-start justify-between gap-3 mb-2">
                    <div>
                      <span className="text-xs text-indigo-600 font-semibold">{q.label}</span>
                      <h3 className="font-semibold text-sm">{t(`q.${q.id}`)}</h3>
                    </div>
                    <button onClick={() => copy("final" + q.id, text)} disabled={!text}
                      className="px-3 py-1 text-xs rounded-lg border border-slate-300 disabled:text-slate-300 shrink-0">{copyLabel("final" + q.id)}</button>
                  </div>
                  {text ? (
                    <p className="text-sm leading-relaxed whitespace-pre-wrap text-slate-700" lang="ko">{text}</p>
                  ) : (
                    <p className="text-sm text-slate-400">{t("fn.unpicked")}<button onClick={() => setStep(2)} className="underline">{t("fn.back")}</button>{t("fn.back.suffix")}</p>
                  )}
                  <div className="text-xs text-slate-400 mt-2">{t("dr.chars", { n: text.length, limit: q.limit })}</div>
                  {renderTranslation(q, text)}
                </section>
              );
            })}
          </div>
        )}
      </main>

      {/* KCI 이용 준수 사항 4항 — 화면 어디에 있든 보이도록 모든 단계 아래에 상시 표시한다. */}
      <footer className="border-t border-slate-200 mt-12">
        <div className="max-w-5xl mx-auto px-5 py-4 text-xs text-slate-500 flex flex-wrap gap-x-3 gap-y-1">
          {/* KCI 표기는 한국어 원문을 항상 남기고, 외국어 화면엔 번역을 덧붙인다 */}
          <span lang="ko">{KCI_NOTICE}</span>
          {foreign && <span>{t("ft.kci")}</span>}
          <span className="text-slate-400">{t("ft.note")}</span>
        </div>
      </footer>
    </div>
    </LangContext.Provider>
  );
}
