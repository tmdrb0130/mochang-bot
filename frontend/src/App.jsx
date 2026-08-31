import { useEffect, useMemo, useState } from "react";
import * as api from "./api.js";

// ───────────────────────── 데이터 정의 ─────────────────────────
// 문항 의도·작성 지침은 백엔드 backend/prompts/ 의 md 파일이 단일 출처. 여기엔 UI 표시용 메타만 둔다.
const TRACKS = {
  tech: {
    name: "일반/기술 분야",
    desc: "창의적 아이디어·기술로 불편을 해소하고 새로운 가치를 만드는 아이템",
    // Q6 사업 분야 선택지 (modoo.or.kr 도전하기 화면과 동일) — 사람이 직접 고른다
    fields: ["IT", "교육", "금융", "운영관리", "네트워킹", "농축/수산업", "라이프스타일", "마케팅/PR", "모빌리티", "미디어/엔터테인먼트", "바이오/의료", "에너지/자원", "유통/물류", "임팩트", "재무", "프롭테크", "하드웨어", "기타"],
  },
  local: {
    name: "로컬 분야",
    desc: "지역 자원을 기반으로 창의성과 혁신을 결합하는 아이템 (패션·F&B·뷰티·생활)",
    fields: ["패션", "F&B", "뷰티", "생활"],
  },
};

// Q10(대중 공개용 자랑)을 비공개로 선택하면 AI 호출 없이 이 문장을 넣는다. 사용자가 고칠 수 있음.
const Q10_PRIVATE_TEXT = "아이디어 보호를 위해 심사 기간 동안은 공개하지 않겠습니다.";

const STYLES = [
  { id: "story", name: "스토리텔링형", desc: "경험과 장면으로 시작해 공감을 끌어내는 글" },
  { id: "logic", name: "논리·근거형", desc: "문제 → 원인 → 해결 → 근거 순서로 설득하는 글" },
  { id: "plain", name: "간결·실무형", desc: "짧은 문장으로 핵심만 담백하게 정리한 글" },
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
const PARALLEL = 2;

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
          <button key={o.label} onClick={() => toggle(o.label)} title={o.hint}
            className={`${btn} rounded-lg border text-left ${isOn(o.label) ? "border-indigo-600 bg-indigo-50 text-indigo-900" : "border-slate-200 hover:border-slate-400"}`}>
            {isOn(o.label) && <span className="mr-1">✓</span>}{o.label}
          </button>
        ))}
        <button onClick={() => onChange({ answer: null, unknown: true })}
          className={`${btn} rounded-lg border ${value?.unknown ? "border-amber-500 bg-amber-50 text-amber-900" : "border-dashed border-slate-300 text-slate-500 hover:border-slate-400"}`}>
          모르겠어요
        </button>
      </div>
      {!compact && <p className="text-xs text-slate-400">여러 개 골라도 됩니다.{hints.length > 0 && ` → ${hints.join(" / ")}`}</p>}
      <div className="flex gap-2 flex-wrap">
        {isNumber && (
          <input type="number" min="0" value={count} onChange={(e) => setCount(e.target.value)} placeholder="몇 명?"
            className={`${compact ? "text-xs py-1" : "text-sm py-2"} px-3 rounded-lg border border-slate-300 w-24`} />
        )}
        <input value={other} onChange={(e) => setOther(e.target.value)} onKeyDown={(e) => e.key === "Enter" && submitOther()}
          placeholder="기타 — 직접 한 줄 입력 후 Enter"
          className={`${compact ? "text-xs py-1" : "text-sm py-2"} px-3 rounded-lg border border-slate-300 flex-1 min-w-[200px]`} />
      </div>
      {pickedList.filter((p) => !labels.includes(base(p))).map((p) => (
        <button key={p} onClick={() => commit(pickedList.filter((x) => x !== p))} title="클릭하면 제거"
          className="inline-block mr-2 text-xs px-2 py-0.5 rounded bg-indigo-50 text-indigo-800 hover:bg-red-50 hover:text-red-700">기타: {p} ×</button>
      ))}
    </div>
  );
}

// ───────────────────────── 컴포넌트 ─────────────────────────
export default function ModooWriter() {
  const [step, setStep] = useState(0); // 0 입력, 1 생성·선택, 2 제출용 정리
  const [form, setForm] = useState({
    track: "tech", idea: "", isBusiness: false, currentItem: "", team: "팀원 없음", capability: "",
    field: "",          // Q6 사업 분야 — 사람이 직접 선택
    q10Public: true,    // Q10 공개 여부 — 공개면 AI 생성, 비공개면 고정 문장
    styles: ["story"], // 테스트 중엔 기본 1개 (무료 티어 요청 수 절약). 사용자가 더 고를 수 있음.
  });
  const [texts, setTexts] = useState({});   // texts[qid][styleId] = string
  const [status, setStatus] = useState({}); // status[qid][styleId] = 'loading' | 'done' | 'error'
  const [errors, setErrors] = useState({}); // errors[qid][styleId] = message
  const [picked, setPicked] = useState({}); // picked[qid] = styleId
  const [running, setRunning] = useState(false);
  const [copied, setCopied] = useState("");
  // ── 인테이크(정보 보충) ──
  const [intake, setIntake] = useState(null);      // /intake 결과 { summary, slots, cards, ready }
  const [intakeBusy, setIntakeBusy] = useState(false);
  const [answers, setAnswers] = useState({});      // answers[slot] = { answer: string|string[]|null, unknown: bool }
  const [cardIdx, setCardIdx] = useState(0);
  const [assistOpen, setAssistOpen] = useState({}); // assistOpen[qid] = bool (초안 화면의 "정보 보태기" 패널)
  const [server, setServer] = useState(null); // { ok, model, usage } | { ok:false, error }
  const [models, setModels] = useState([]);   // [{ id, name, note }] — 백엔드 config.yaml 의 목록
  const [modelUsed, setModelUsed] = useState({}); // modelUsed[qid][styleId] = 실제 응답한 모델 id

  const refreshHealth = () =>
    api.health()
      .then((h) => setServer(h))
      .catch((e) => setServer({ ok: false, error: String(e.message || e) }));

  useEffect(() => {
    refreshHealth();
    api.models()
      .then((m) => {
        setModels(m.models || []);
        setForm((f) => (f.model ? f : { ...f, model: m.default }));
      })
      .catch(() => {});
  }, []);

  const setUsed = (qid, sid, value) =>
    setModelUsed((p) => ({ ...p, [qid]: { ...(p[qid] || {}), [sid]: value } }));

  // 카드 답변 → 백엔드 형식 [{slot,label,answer,unknown}]. 카드가 있는 슬롯만.
  const buildAnswers = () =>
    (intake?.cards || [])
      .filter((c) => answers[c.slot] && (answers[c.slot].unknown || answers[c.slot].answer))
      .map((c) => ({ slot: c.slot, label: c.label, answer: answers[c.slot].answer, unknown: !!answers[c.slot].unknown }));
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
    ideaLen < IDEA_MIN && `아이디어를 ${IDEA_MIN}자 이상 적어주세요 (지금 ${ideaLen}자)`,
    form.styles.length === 0 && "글 스타일을 하나 이상 골라주세요",
    server === null && "백엔드 연결 확인 중…",
    server && !server.ok && "백엔드에 연결되지 않았어요 (상단 안내 참고)",
  ].filter(Boolean);
  const canStart = blockers.length === 0;

  const setText = (qid, sid, value) =>
    setTexts((p) => ({ ...p, [qid]: { ...(p[qid] || {}), [sid]: value } }));
  const setStat = (qid, sid, value) =>
    setStatus((p) => ({ ...p, [qid]: { ...(p[qid] || {}), [sid]: value } }));
  const setErr = (qid, sid, value) =>
    setErrors((p) => ({ ...p, [qid]: { ...(p[qid] || {}), [sid]: value } }));

  async function generateOne(q, style) {
    if (q.id === "q10" && !form.q10Public) {
      // 비공개 선택 → AI 호출 없이 고정 문장
      setText(q.id, style.id, Q10_PRIVATE_TEXT);
      setUsed(q.id, style.id, "");
      setStat(q.id, style.id, "done");
      setPicked((p) => (p[q.id] ? p : { ...p, [q.id]: style.id }));
      return;
    }
    setStat(q.id, style.id, "loading");
    try {
      const res = await api.generate(formForApi(), q.id, style.id);
      setText(q.id, style.id, res.text.slice(0, q.limit));
      setUsed(q.id, style.id, res.model);
      setStat(q.id, style.id, "done");
      setPicked((p) => (p[q.id] ? p : { ...p, [q.id]: style.id }));
    } catch (e) {
      setErr(q.id, style.id, String(e.message || e));
      setStat(q.id, style.id, "error");
    } finally {
      refreshHealth();
    }
  }

  // 1) 아이디어 읽기(인테이크) → 충분하면 바로 생성, 부족하면 카드 단계로
  async function startIntake() {
    setIntakeBusy(true);
    try {
      const r = await api.intake(form);
      setIntake(r);
      setAnswers({});
      setCardIdx(0);
      if (r.ready || !r.cards?.length) await generateAll();
      else setStep(1);
    } catch {
      setIntake(null);
      await generateAll();           // 인테이크가 실패해도 생성은 막지 않는다
    } finally {
      setIntakeBusy(false);
      refreshHealth();
    }
  }

  const setAnswer = (slot, value) => setAnswers((p) => ({ ...p, [slot]: value }));

  // 카드 한 장 넘기기 — 마지막이면 생성 시작
  function nextCard() {
    const total = intake?.cards?.length || 0;
    if (cardIdx + 1 >= total) generateAll();
    else setCardIdx(cardIdx + 1);
  }

  async function generateAll() {
    setRunning(true);
    setStep(2);
    const tasks = [];
    for (const q of activeQuestions) for (const s of selectedStyles) tasks.push(() => generateOne(q, s));
    await runLimited(tasks, PARALLEL);
    setRunning(false);
    refreshHealth();
  }

  async function extend(q, style) {
    const current = texts[q.id]?.[style.id] || "";
    if (q.limit - current.length < 150) return;
    setStat(q.id, style.id, "loading");
    try {
      const res = await api.extend(formForApi(), q.id, style.id, current);
      setText(q.id, style.id, res.text.slice(0, q.limit));
      setUsed(q.id, style.id, res.model);
      setStat(q.id, style.id, "done");
    } catch (e) {
      setErr(q.id, style.id, String(e.message || e));
      setStat(q.id, style.id, "error");
    } finally {
      refreshHealth();
    }
  }

  function copy(key, value) {
    navigator.clipboard?.writeText(value).then(() => {
      setCopied(key);
      setTimeout(() => setCopied(""), 1500);
    });
  }

  const doneCount = activeQuestions.filter((q) => picked[q.id] && texts[q.id]?.[picked[q.id]]).length;

  // ───────── 렌더 ─────────
  return (
    <div className="min-h-screen bg-white text-slate-900" style={{ fontFamily: "'Apple SD Gothic Neo','Noto Sans KR','Malgun Gothic',sans-serif" }}>
      <header className="border-b border-slate-200">
        <div className="max-w-5xl mx-auto px-5 py-5 flex items-end justify-between gap-4 flex-wrap">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">모두의 창업 신청서 작성 도우미</h1>
            <p className="text-sm text-slate-500 mt-1">아이디어 한 문단이면 됩니다. 문항별 초안을 여러 스타일로 받아서 고르고, 붙여넣기만 하세요.</p>
          </div>
          <nav className="flex gap-1 text-sm">
            {["아이디어 입력", "정보 보충", "초안 고르기", "제출용 정리"].map((n, i) => {
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
          {server === null && <span className="text-slate-400">백엔드 연결 확인 중…</span>}
          {server?.ok && (
            <>
              <label className="flex items-center gap-2 text-slate-600">
                <span>모델</span>
                <select value={form.model || ""} onChange={(e) => setForm({ ...form, model: e.target.value })}
                  className="px-2 py-1 rounded-md border border-slate-300 bg-white text-xs max-w-[220px]">
                  {models.length === 0 && <option value="">{server.model}</option>}
                  {models.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
                </select>
              </label>
              {models.find((m) => m.id === form.model)?.note && (
                <span className="text-slate-400 hidden md:inline">{models.find((m) => m.id === form.model).note}</span>
              )}
              {server.usage?.limit != null && (() => {
                const { used, limit, remaining, by_model: byModel = {} } = server.usage;
                const tone = remaining <= 0 ? "bg-red-50 text-red-700 border-red-200"
                  : remaining <= 10 ? "bg-amber-50 text-amber-800 border-amber-200"
                  : "bg-slate-50 text-slate-600 border-slate-200";
                const breakdown = Object.entries(byModel).map(([id, n]) => `${shortModel(id)} ${n}회`).join(", ");
                const tip = [
                  "OpenRouter 무료 한도는 계정(API 키) 단위 — 어떤 무료 모델을 써도 같은 숫자가 줄어듭니다. 모델별 한도는 없습니다.",
                  breakdown && `오늘 모델별 사용: ${breakdown}`,
                  `분당 20회 별도. ${server.usage.reset}에 초기화`,
                ].filter(Boolean).join("\n");
                return (
                  <span className={`ml-auto px-2 py-0.5 rounded-md border cursor-help ${tone}`} title={tip}>
                    오늘 무료 요청 {used}/{limit}회{remaining <= 0 ? " · 한도 도달" : remaining <= 10 ? ` · ${remaining}회 남음` : ""}
                  </span>
                );
              })()}
              {server.fallback && <span className="text-slate-400" title="선택 모델이 429/다운이면 목록의 다른 모델로 자동 전환">자동 폴백 켜짐</span>}
            </>
          )}
          {server && !server.ok && (
            <span className="text-red-600">백엔드({api.API_BASE})에 연결할 수 없어요. 프로젝트 루트에서 <code className="font-mono bg-red-50 px-1">uvicorn backend.main:app --port 8000</code> 을 실행해 주세요.</span>
          )}
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-5 py-8">
        {step === 0 && (
          <div className="grid md:grid-cols-5 gap-8">
            <div className="md:col-span-3 space-y-7">
              <section>
                <h2 className="font-semibold mb-2">어느 분야로 지원하나요?</h2>
                <div className="grid grid-cols-2 gap-2">
                  {Object.entries(TRACKS).map(([k, t]) => (
                    <button key={k} onClick={() => setForm({ ...form, track: k, field: "" })}
                      className={`text-left p-3 rounded-lg border ${form.track === k ? "border-indigo-600 bg-indigo-50" : "border-slate-200 hover:border-slate-400"}`}>
                      <div className="font-medium">{t.name}</div>
                      <div className="text-xs text-slate-500 mt-1 leading-relaxed">{t.desc}</div>
                    </button>
                  ))}
                </div>
              </section>

              <section>
                <h2 className="font-semibold mb-1">아이디어를 설명해 주세요</h2>
                <p className="text-xs text-slate-500 mb-2">누구의 어떤 불편을, 무엇으로, 어떻게 해결하는지. 생각난 계기가 있으면 같이 적어주세요. 길수록 초안이 좋아집니다.</p>
                <textarea value={form.idea} onChange={(e) => setForm({ ...form, idea: e.target.value })} rows={8}
                  placeholder="예) 자취하는 20대는 배달 음식이 지겨운데 요리는 부담스럽다. 동네 반찬가게와 연결해 그날 남은 반찬을 저녁 7시 이후 할인 꾸러미로 예약·수령하는 앱을 만들고 싶다. 내가 직접 반찬가게 사장님께 여쭤보니 매일 20~30%가 폐기된다고 했다…"
                  className="w-full p-3 rounded-lg border border-slate-300 focus:border-indigo-600 focus:outline-none text-sm leading-relaxed" />
                <div className="text-xs text-slate-400 text-right">
                  {form.idea.length}자
                  {ideaLen < IDEA_MIN && ` · ${IDEA_MIN}자 이상 적어주세요`}
                  {ideaLen >= IDEA_MIN && ideaLen < 60 && " · 짧아도 되지만, 누구의 어떤 불편인지 한 줄 더 적으면 초안이 훨씬 구체적이 돼요"}
                </div>
              </section>

              <section>
                <h2 className="font-semibold mb-1">본인의 경력·경험·기술</h2>
                <p className="text-xs text-slate-500 mb-2">Q8에 그대로 반영됩니다. 없는 건 지어내지 않으니, 아이디어와 조금이라도 관련 있는 건 다 적어주세요.</p>
                <textarea value={form.capability} onChange={(e) => setForm({ ...form, capability: e.target.value })} rows={4}
                  placeholder="예) 식품회사 영업 3년, 동네 반찬가게 5곳과 이미 친분, 노코드 앱 제작 경험, 인스타 팔로워 4천명…"
                  className="w-full p-3 rounded-lg border border-slate-300 focus:border-indigo-600 focus:outline-none text-sm leading-relaxed" />
              </section>
            </div>

            <div className="md:col-span-2 space-y-7">
              <section>
                <h2 className="font-semibold mb-2">현재 창업 여부</h2>
                <div className="flex gap-2">
                  {[false, true].map((v) => (
                    <button key={String(v)} onClick={() => setForm({ ...form, isBusiness: v })}
                      className={`flex-1 py-2 rounded-lg border text-sm ${form.isBusiness === v ? "border-indigo-600 bg-indigo-50" : "border-slate-200"}`}>
                      {v ? "현재 사업자" : "사업자 아님"}
                    </button>
                  ))}
                </div>
                {form.isBusiness && (
                  <textarea value={form.currentItem} onChange={(e) => setForm({ ...form, currentItem: e.target.value })} rows={3}
                    placeholder="현재 운영 중인 사업 (업종, 아이템, 업력)" className="mt-2 w-full p-3 rounded-lg border border-slate-300 text-sm" />
                )}
                <p className="text-xs text-slate-500 mt-2">공고일 기준 사업자등록 여부. 사업자면 Q7-1이 추가됩니다.</p>
              </section>

              <section>
                <h2 className="font-semibold mb-2">팀원 수 (본인 제외)</h2>
                <select value={form.team} onChange={(e) => setForm({ ...form, team: e.target.value })}
                  className="w-full p-2 rounded-lg border border-slate-300 text-sm bg-white">
                  {TEAM_OPTIONS.map((t) => <option key={t}>{t}</option>)}
                </select>
              </section>

              <section>
                <h2 className="font-semibold mb-2">사업 분야 (Q6)</h2>
                <select value={form.field} onChange={(e) => setForm({ ...form, field: e.target.value })}
                  className="w-full p-2 rounded-lg border border-slate-300 text-sm bg-white">
                  <option value="">나중에 고르기</option>
                  {TRACKS[form.track].fields.map((f) => <option key={f} value={f}>{f}</option>)}
                </select>
              </section>

              <section>
                <h2 className="font-semibold mb-1">아이디어 자랑 글 (Q10)</h2>
                <p className="text-xs text-slate-500 mb-2">홈페이지에 공개되어 '좋아요'를 받는 항목입니다.</p>
                <div className="flex gap-2">
                  {[true, false].map((v) => (
                    <button key={String(v)} onClick={() => setForm({ ...form, q10Public: v })}
                      className={`flex-1 py-2 rounded-lg border text-sm ${form.q10Public === v ? "border-indigo-600 bg-indigo-50" : "border-slate-200"}`}>
                      {v ? "공개" : "비공개"}
                    </button>
                  ))}
                </div>
              </section>

              <section>
                <h2 className="font-semibold mb-2">받아볼 글 스타일</h2>
                <div className="space-y-2">
                  {STYLES.map((s) => {
                    const on = form.styles.includes(s.id);
                    return (
                      <button key={s.id} onClick={() => setForm({ ...form, styles: on ? form.styles.filter((x) => x !== s.id) : [...form.styles, s.id] })}
                        className={`w-full text-left p-3 rounded-lg border ${on ? "border-indigo-600 bg-indigo-50" : "border-slate-200"}`}>
                        <div className="flex items-center justify-between">
                          <span className="font-medium text-sm">{s.name}</span>
                          <span className={`w-4 h-4 rounded-full border ${on ? "bg-indigo-600 border-indigo-600" : "border-slate-300"}`} />
                        </div>
                        <div className="text-xs text-slate-500 mt-1">{s.desc}</div>
                      </button>
                    );
                  })}
                </div>
              </section>

              <button onClick={startIntake} disabled={!canStart || intakeBusy}
                className="w-full py-3 rounded-lg bg-indigo-600 text-white font-medium disabled:bg-slate-300 disabled:cursor-not-allowed">
                {intakeBusy ? "아이디어를 읽고 있어요…" : "신청서 초안 만들기"}
              </button>
              {intakeBusy && <p className="text-xs text-slate-500 text-center">설명이 충분하면 바로 초안을 만들고, 부족한 부분이 있으면 몇 가지 골라달라고 할게요.</p>}
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
                <h2 className="text-lg font-semibold">몇 가지만 골라주세요</h2>
                <p className="text-sm text-slate-500 mt-1">정답이 아니어도 됩니다. 가장 가까운 것을 고르면 AI가 그걸 바탕으로 씁니다. 모르면 "모르겠어요" — 지어내지 않고 가정으로 씁니다.</p>
              </div>

              {(intake.summary || known.length > 0) && (
                <div className="p-3 rounded-lg bg-slate-50 border border-slate-200 text-xs text-slate-600 space-y-1">
                  {intake.summary && <div><span className="font-medium text-slate-700">AI가 이해한 아이디어:</span> {intake.summary}</div>}
                  {known.length > 0 && <div><span className="font-medium text-slate-700">설명에서 확인됨:</span> {known.map((s) => s.label).join(" · ")}</div>}
                </div>
              )}

              {card && (
                <section className="border border-slate-200 rounded-xl p-5 space-y-4">
                  <div className="flex items-center justify-between text-xs text-slate-400">
                    <span>{cardIdx + 1} / {cards.length} · {card.label}</span>
                    <span>{card.why}</span>
                  </div>
                  <h3 className="font-semibold text-base">{card.question}</h3>
                  <CardOptions key={card.slot} card={card} value={answers[card.slot]} onChange={(v) => setAnswer(card.slot, v)} />
                  <div className="flex items-center justify-between pt-2 flex-wrap gap-2">
                    <button onClick={() => setCardIdx(Math.max(cardIdx - 1, 0))} disabled={cardIdx === 0} className="text-sm text-slate-500 underline disabled:text-slate-300">이전</button>
                    <div className="flex gap-2">
                      <button onClick={generateAll} className="px-3 py-2 text-sm rounded-lg border border-slate-300">나머지 건너뛰고 바로 만들기</button>
                      <button onClick={nextCard} className="px-4 py-2 text-sm rounded-lg bg-indigo-600 text-white">
                        {cardIdx + 1 >= cards.length ? "초안 만들기" : "다음"}
                      </button>
                    </div>
                  </div>
                </section>
              )}

              <div className="flex gap-1 justify-center">
                {cards.map((c, i) => (
                  <button key={c.slot} onClick={() => setCardIdx(i)} title={c.label}
                    className={`w-2.5 h-2.5 rounded-full ${i === cardIdx ? "bg-indigo-600" : answers[c.slot]?.unknown ? "bg-amber-400" : answers[c.slot]?.answer ? "bg-emerald-500" : "bg-slate-200"}`} />
                ))}
              </div>
            </div>
          );
        })()}

        {step === 2 && (
          <div className="space-y-8">
            {intake?.summary && (
              <p className="text-xs text-slate-500">AI가 이해한 아이디어: {intake.summary}{answeredCount > 0 && ` · 보충 답변 ${answeredCount}개 반영`}</p>
            )}
            <div className="flex items-center justify-between flex-wrap gap-3">
              <p className="text-sm text-slate-600">
                {running ? "초안을 만드는 중입니다. 완성된 문항부터 읽고 골라두세요." : `${doneCount}/${activeQuestions.length}개 문항 선택됨. 글은 직접 고쳐도 됩니다.`}
              </p>
              <div className="flex gap-2">
                <button onClick={() => setStep(0)} className="px-3 py-1.5 text-sm rounded-lg border border-slate-300">입력 수정</button>
                <button onClick={() => setStep(3)} disabled={doneCount === 0} className="px-3 py-1.5 text-sm rounded-lg bg-indigo-600 text-white disabled:bg-slate-300">제출용으로 정리</button>
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
                      <h3 className="font-semibold">{q.title}</h3>
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
                            {s.name}
                          </button>
                        );
                      })}
                    </div>
                  </div>

                  <div className="px-5 pb-4">
                    {st === "loading" && !text && <div className="h-32 rounded-lg bg-slate-50 animate-pulse" />}
                    {st === "error" && (
                      <div className="p-4 rounded-lg bg-red-50 text-sm text-red-700 flex justify-between items-center gap-3">
                        <span>생성에 실패했어요. {errors[q.id]?.[cur] ? <span className="text-red-600">{errors[q.id][cur]}</span> : "무료 모델은 잠시 후 다시 시도하면 대개 됩니다."}</span>
                        <button onClick={() => generateOne(q, STYLES.find((s) => s.id === cur))} className="underline shrink-0">다시 생성</button>
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
                            {text.length} / {q.limit}자
                            {modelUsed[q.id]?.[cur] && (
                              <span className={`ml-2 ${modelUsed[q.id][cur] !== form.model ? "text-amber-600" : "text-slate-400"}`}
                                title={modelUsed[q.id][cur] !== form.model ? "선택한 모델이 막혀서 다른 모델로 자동 전환됨" : "실제 응답한 모델"}>
                                · {shortModel(modelUsed[q.id][cur])}{modelUsed[q.id][cur] !== form.model && " (폴백)"}
                              </span>
                            )}
                          </span>
                          <div className="flex gap-3">
                            {q.limit > 100 && q.limit - text.length >= 150 && (
                              <button onClick={() => extend(q, STYLES.find((s) => s.id === cur))} disabled={st === "loading"} className="underline disabled:text-slate-300">
                                {st === "loading" ? "이어쓰는 중…" : "내용 더 보태기"}
                              </button>
                            )}
                            <button onClick={() => generateOne(q, STYLES.find((s) => s.id === cur))} disabled={st === "loading"} className="underline disabled:text-slate-300">새로 생성</button>
                            <button onClick={() => copy(q.id + cur, text)} className="underline">{copied === q.id + cur ? "복사됨" : "복사"}</button>
                            {cardsForQuestion(q.id).length > 0 && (
                              <button onClick={() => setAssistOpen((p) => ({ ...p, [q.id]: !p[q.id] }))} className="underline text-indigo-600">
                                {assistOpen[q.id] ? "정보 보태기 닫기" : "정보 보태기"}
                              </button>
                            )}
                          </div>
                        </div>
                        {assistOpen[q.id] && (
                          <div className="mt-3 p-4 rounded-lg bg-indigo-50/50 border border-indigo-100 space-y-4">
                            <p className="text-xs text-slate-600">이 문항에 들어갈 정보입니다. 고치거나 채운 뒤 <b>새로 생성</b>(다시 쓰기) 또는 <b>내용 더 보태기</b>(이어쓰기)를 누르면 반영됩니다.</p>
                            {cardsForQuestion(q.id).map((c) => (
                              <div key={c.slot} className="space-y-1.5">
                                <div className="text-xs font-medium text-slate-700">{c.question} <span className="text-slate-400 font-normal">· {c.label}</span></div>
                                <CardOptions card={c} value={answers[c.slot]} onChange={(v) => setAnswer(c.slot, v)} compact />
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
                <h2 className="font-semibold text-lg">제출용 정리</h2>
                <p className="text-sm text-slate-500">modoo.or.kr 도전하기 화면을 옆에 열고 문항 순서대로 붙여넣으세요.</p>
              </div>
              <button onClick={() => copy("all", activeQuestions.map((q) => `[${q.label}] ${q.title}\n${texts[q.id]?.[picked[q.id]] || "(미작성)"}`).join("\n\n"))}
                className="px-4 py-2 rounded-lg bg-indigo-600 text-white text-sm">{copied === "all" ? "전체 복사됨" : "전체 복사"}</button>
            </div>

            <div className="grid md:grid-cols-2 gap-3 text-sm">
              <div className="p-4 rounded-lg bg-slate-50 border border-slate-200">
                <div className="font-medium mb-1">직접 입력할 항목</div>
                <ul className="text-slate-600 space-y-1">
                  <li>Q5 소개 영상 링크 (선택)</li>
                  <li>Q6 사업 분야: {form.field || "아직 안 고름 — 제출 화면에서 선택"}</li>
                  <li>Q7 창업 여부: {form.isBusiness ? "현재 사업자" : "사업자 아님"}</li>
                  <li>Q9 팀원 수: {form.team}</li>
                  <li>Q10 공개 여부: {form.q10Public ? "공개 (자랑 글 붙여넣기)" : "비공개 (안내 문장 또는 빈칸)"}</li>
                  <li>Q11 서약서 동의 3개</li>
                  <li>03. 멘토 기관 (예: 충남 &gt; 백석대학교 산학협력단)</li>
                </ul>
              </div>
              <div className="p-4 rounded-lg bg-amber-50 border border-amber-200 text-amber-900">
                <div className="font-medium mb-1">제출 전에 확인하세요</div>
                <ul className="space-y-1">
                  <li>이미지는 전체 문항 합쳐 최대 10장까지만 첨부됩니다.</li>
                  <li>초안의 수치·사례는 본인이 확인한 사실로 바꿔 넣으세요. 심사에서 사실 확인이 있을 수 있어요.</li>
                  <li>다른 정부 공모전 수상작이거나 이미 상용화된 아이디어는 선정이 취소될 수 있습니다.</li>
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
                      <h3 className="font-semibold text-sm">{q.title}</h3>
                    </div>
                    <button onClick={() => copy("final" + q.id, text)} disabled={!text}
                      className="px-3 py-1 text-xs rounded-lg border border-slate-300 disabled:text-slate-300 shrink-0">{copied === "final" + q.id ? "복사됨" : "복사"}</button>
                  </div>
                  {text ? (
                    <p className="text-sm leading-relaxed whitespace-pre-wrap text-slate-700">{text}</p>
                  ) : (
                    <p className="text-sm text-slate-400">아직 고르지 않았어요. <button onClick={() => setStep(2)} className="underline">초안 고르기</button>로 돌아가세요.</p>
                  )}
                  <div className="text-xs text-slate-400 mt-2">{text.length} / {q.limit}자</div>
                </section>
              );
            })}
          </div>
        )}
      </main>
    </div>
  );
}
