import { useState, useMemo } from "react";

// ───────────────────────── 데이터 정의 ─────────────────────────
const TRACKS = {
  tech: {
    name: "일반/기술 분야",
    desc: "창의적 아이디어·기술로 불편을 해소하고 새로운 가치를 만드는 아이템",
    fields: ["IT", "교육", "금융", "운영관리", "네트워킹", "농축/수산업", "라이프스타일", "마케팅/PR", "모빌리티", "미디어/엔터테인먼트", "바이오/의료", "에너지/자원", "유통/물류", "임팩트", "재무", "프롭테크", "하드웨어", "기타"],
  },
  local: {
    name: "로컬 분야",
    desc: "지역 자원을 기반으로 창의성과 혁신을 결합하는 아이템 (패션·F&B·뷰티·생활)",
    fields: ["패션", "F&B", "뷰티", "생활"],
  },
};

const STYLES = [
  { id: "story", name: "스토리텔링형", desc: "경험과 장면으로 시작해 공감을 끌어내는 글" },
  { id: "logic", name: "논리·근거형", desc: "문제 → 원인 → 해결 → 근거 순서로 설득하는 글" },
  { id: "plain", name: "간결·실무형", desc: "짧은 문장으로 핵심만 담백하게 정리한 글" },
];

const TEAM_OPTIONS = ["팀원 없음", "1명", "2명", "3명", "4명", "5명", "6~9명", "10명 이상"];

const QUESTIONS = [
  { id: "q1", label: "Q1", title: "나의 아이디어를 한줄로 소개해주세요", limit: 100, target: "60~95자 사이의 한 문장", guide: "무엇을(제품/서비스) 누구에게 어떤 가치를 주는지가 한 문장에 담기게. 유행어나 과장 없이." },
  { id: "q2", label: "Q2", title: "아이디어를 떠올린 배경 이야기를 들려주세요", limit: 2000, target: "1000~1300자", guide: "직접 겪었거나 관찰한 문제 상황, 왜 지금 이 문제가 중요한지, 어떤 계기로 해결 아이디어에 이르렀는지." },
  { id: "q3_1", label: "Q3-1", title: "기존 제품·서비스 대비 차별점과 해결할 수 있는 문제", limit: 2000, target: "1000~1300자", guide: "해결하는 문제를 먼저 명확히 정의하고, 현재 대안(기존 제품/서비스/우회 방법)이 왜 부족한지, 이 아이디어는 무엇이 다른지 구체적으로." },
  { id: "q3_2", label: "Q3-2", title: "어떻게 수익을 창출할 계획인가요", limit: 2000, target: "1000~1300자", guide: "누가 돈을 내는지, 무엇에 대해 얼마를 내는지, 초기 수익 모델과 확장 가능한 수익 모델을 구분해서. 숫자는 가정임을 밝히고 현실적으로." },
  { id: "q4_1", label: "Q4-1", title: "아이디어를 어떻게 사업화할 계획인가요", limit: 2000, target: "1000~1300자", guide: "검증 → 시제품/MVP → 첫 고객 확보 → 확장의 단계별 계획. 6개월~1년 안에 할 수 있는 현실적인 첫 단계를 강조." },
  { id: "q4_2", label: "Q4-2", title: "책임멘토를 통해 어떤 도움을 받고 싶은가요", limit: 2000, target: "900~1200자", guide: "본인이 부족한 부분을 솔직히 인정하고, 멘토에게 구체적으로 어떤 영역(시장검증, 원가/수익구조, 법·인허가, 제조/유통, 마케팅 등)의 조언을 원하는지." },
  { id: "q7_1", label: "Q7-1", title: "현재 사업 아이템과 이번 도전 아이디어는 어떻게 다른가요", limit: 2000, target: "800~1100자", guide: "기존 사업과 업종·고객·수익구조가 어떻게 다른지(이종창업 강조), 기존 사업 경험이 새 아이디어에 어떻게 도움이 되는지.", onlyBusiness: true },
  { id: "q8", label: "Q8", title: "아이디어를 사업화할 수 있는 본인의 역량", limit: 2000, target: "900~1200자", guide: "입력된 경력·경험·기술을 아이디어와 연결. 없는 역량을 지어내지 말고, 부족한 부분은 어떻게 보완할지 함께." },
  { id: "q10", label: "Q10", title: "내 아이디어를 사람들에게 자랑해보세요 (홈페이지 공개)", limit: 100, target: "70~95자", guide: "일반 대중이 읽고 '좋아요'를 누르고 싶게. 친근하고 생동감 있게, 이모지 없이." },
];

// ───────────────────────── API 호출 ─────────────────────────
async function callClaude(system, user) {
  const res = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "claude-sonnet-4-6",
      max_tokens: 1000,
      system,
      messages: [{ role: "user", content: user }],
    }),
  });
  if (!res.ok) throw new Error(`요청 실패 (${res.status})`);
  const data = await res.json();
  let text = data.content.filter((c) => c.type === "text").map((c) => c.text).join("\n").trim();
  if (data.stop_reason === "max_tokens") {
    const cut = Math.max(text.lastIndexOf("."), text.lastIndexOf("다."), text.lastIndexOf("요."));
    if (cut > text.length * 0.5) text = text.slice(0, cut + 1);
  }
  return text;
}

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

function buildContext(form) {
  const track = TRACKS[form.track];
  return [
    `지원 트랙: ${track.name}`,
    `아이디어 설명:\n${form.idea}`,
    `지원자 창업 여부: ${form.isBusiness ? "현재 사업자" : "예비창업자(현재 사업자 아님)"}`,
    form.isBusiness && form.currentItem ? `현재 운영 중인 사업: ${form.currentItem}` : "",
    `팀원 수(본인 제외): ${form.team}`,
    `지원자 역량·경력: ${form.capability || "(입력 없음 — 아이디어 설명에서 유추 가능한 범위만 언급하고 지어내지 말 것)"}`,
  ].filter(Boolean).join("\n\n");
}

function buildSystem(q, style, form) {
  return `당신은 중소벤처기업부 '모두의 창업 프로젝트' 도전신청서를 지원자 입장에서 대신 작성하는 전문 작가입니다.
심사위원이 읽는 글이므로 과장·허위·근거 없는 수치는 금지하고, 지원자가 제공한 정보에 없는 사실을 만들어내지 마십시오.

작성 문항: ${q.label}. ${q.title}
문항 의도와 작성 지침: ${q.guide}
분량: ${q.target} (절대 ${q.limit}자를 넘기지 말 것)

글 스타일: ${style.name} — ${style.desc}
- 1인칭('저', '저희')으로 씁니다.
- 제목, 소제목, 마크다운 기호(#, *, -), 이모지를 쓰지 않습니다. 문단은 줄바꿈으로만 구분합니다.
- 답변 본문만 출력하고 다른 설명은 붙이지 않습니다.`;
}

// ───────────────────────── 컴포넌트 ─────────────────────────
export default function ModooWriter() {
  const [step, setStep] = useState(0); // 0 입력, 1 생성·선택, 2 제출용 정리
  const [form, setForm] = useState({
    track: "tech", idea: "", isBusiness: false, currentItem: "", team: "팀원 없음", capability: "",
    styles: ["story", "logic"],
  });
  const [texts, setTexts] = useState({});   // texts[qid][styleId] = string
  const [status, setStatus] = useState({}); // status[qid][styleId] = 'loading' | 'done' | 'error'
  const [picked, setPicked] = useState({}); // picked[qid] = styleId
  const [field, setField] = useState(null); // Q6 추천
  const [running, setRunning] = useState(false);
  const [copied, setCopied] = useState("");

  const activeQuestions = useMemo(
    () => QUESTIONS.filter((q) => !q.onlyBusiness || form.isBusiness),
    [form.isBusiness]
  );
  const selectedStyles = STYLES.filter((s) => form.styles.includes(s.id));
  const canStart = form.idea.trim().length >= 30 && form.styles.length > 0;

  const setText = (qid, sid, value) =>
    setTexts((p) => ({ ...p, [qid]: { ...(p[qid] || {}), [sid]: value } }));
  const setStat = (qid, sid, value) =>
    setStatus((p) => ({ ...p, [qid]: { ...(p[qid] || {}), [sid]: value } }));

  async function generateOne(q, style) {
    setStat(q.id, style.id, "loading");
    try {
      const text = await callClaude(buildSystem(q, style, form), buildContext(form));
      setText(q.id, style.id, text.slice(0, q.limit));
      setStat(q.id, style.id, "done");
      setPicked((p) => (p[q.id] ? p : { ...p, [q.id]: style.id }));
    } catch (e) {
      setStat(q.id, style.id, "error");
    }
  }

  async function recommendField() {
    const options = TRACKS[form.track].fields.join(", ");
    try {
      const raw = await callClaude(
        `다음 아이디어에 가장 맞는 사업 분야를 아래 목록에서 하나만 고르고, JSON으로만 답하십시오. 형식: {"field":"분야명","reason":"한 문장 이유"}\n목록: ${options}`,
        buildContext(form)
      );
      const parsed = JSON.parse(raw.replace(/```json|```/g, "").trim());
      setField(parsed);
    } catch {
      setField({ field: "", reason: "자동 추천에 실패했어요. 직접 선택해 주세요." });
    }
  }

  async function generateAll() {
    setRunning(true);
    setStep(1);
    const tasks = [];
    for (const q of activeQuestions) for (const s of selectedStyles) tasks.push(() => generateOne(q, s));
    tasks.push(recommendField);
    await runLimited(tasks, 4);
    setRunning(false);
  }

  async function extend(q, style) {
    const current = texts[q.id]?.[style.id] || "";
    const room = q.limit - current.length;
    if (room < 150) return;
    setStat(q.id, style.id, "loading");
    try {
      const more = await callClaude(
        buildSystem(q, style, form) +
          `\n\n지금은 이미 작성된 글에 이어질 내용만 추가로 씁니다. 기존 내용을 반복하거나 요약하지 말고, 새로운 근거·사례·계획을 ${Math.min(room - 30, 700)}자 이내로 이어서 작성하십시오. 이어지는 부분만 출력합니다.`,
        buildContext(form) + `\n\n이미 작성된 글:\n${current}`
      );
      setText(q.id, style.id, (current + "\n\n" + more).slice(0, q.limit));
      setStat(q.id, style.id, "done");
    } catch {
      setStat(q.id, style.id, "error");
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
            {["아이디어 입력", "초안 고르기", "제출용 정리"].map((n, i) => (
              <button key={n} onClick={() => i < 2 || doneCount > 0 ? setStep(i) : null}
                className={`px-3 py-1.5 rounded-full ${step === i ? "bg-indigo-600 text-white" : "text-slate-500 hover:bg-slate-100"}`}>
                {i + 1}. {n}
              </button>
            ))}
          </nav>
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
                    <button key={k} onClick={() => setForm({ ...form, track: k })}
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
                <div className="text-xs text-slate-400 text-right">{form.idea.length}자 {form.idea.trim().length < 30 && "· 30자 이상 적어주세요"}</div>
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
                <p className="text-xs text-slate-500 mt-2">사업자면 Q7-1(기존 사업과의 차이)이 자동으로 추가됩니다. 공고일 기준 사업자등록 여부로 판단되니 정확히 골라주세요.</p>
              </section>

              <section>
                <h2 className="font-semibold mb-2">팀원 수 (본인 제외)</h2>
                <select value={form.team} onChange={(e) => setForm({ ...form, team: e.target.value })}
                  className="w-full p-2 rounded-lg border border-slate-300 text-sm bg-white">
                  {TEAM_OPTIONS.map((t) => <option key={t}>{t}</option>)}
                </select>
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
                <p className="text-xs text-slate-500 mt-2">스타일 하나당 문항 {activeQuestions.length}개씩 생성됩니다. 많이 고를수록 오래 걸려요.</p>
              </section>

              <button onClick={generateAll} disabled={!canStart}
                className="w-full py-3 rounded-lg bg-indigo-600 text-white font-medium disabled:bg-slate-300 disabled:cursor-not-allowed">
                초안 {activeQuestions.length * selectedStyles.length}개 만들기
              </button>
            </div>
          </div>
        )}

        {step === 1 && (
          <div className="space-y-8">
            <div className="flex items-center justify-between flex-wrap gap-3">
              <p className="text-sm text-slate-600">
                {running ? "초안을 만드는 중입니다. 완성된 문항부터 읽고 골라두세요." : `${doneCount}/${activeQuestions.length}개 문항 선택됨. 글은 직접 고쳐도 됩니다.`}
              </p>
              <div className="flex gap-2">
                <button onClick={() => setStep(0)} className="px-3 py-1.5 text-sm rounded-lg border border-slate-300">입력 수정</button>
                <button onClick={() => setStep(2)} disabled={doneCount === 0} className="px-3 py-1.5 text-sm rounded-lg bg-indigo-600 text-white disabled:bg-slate-300">제출용으로 정리</button>
              </div>
            </div>

            {field && (
              <div className="p-4 rounded-lg bg-slate-50 border border-slate-200 text-sm">
                <span className="font-medium">Q6 사업 분야 추천:</span> {field.field || "—"} <span className="text-slate-500">— {field.reason}</span>
              </div>
            )}

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
                      <div className="p-4 rounded-lg bg-red-50 text-sm text-red-700 flex justify-between items-center">
                        <span>생성에 실패했어요. 다시 시도해 주세요.</span>
                        <button onClick={() => generateOne(q, STYLES.find((s) => s.id === cur))} className="underline">다시 생성</button>
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
                          <span>{text.length} / {q.limit}자</span>
                          <div className="flex gap-3">
                            {q.limit > 100 && q.limit - text.length >= 150 && (
                              <button onClick={() => extend(q, STYLES.find((s) => s.id === cur))} disabled={st === "loading"} className="underline disabled:text-slate-300">
                                {st === "loading" ? "이어쓰는 중…" : "내용 더 보태기"}
                              </button>
                            )}
                            <button onClick={() => generateOne(q, STYLES.find((s) => s.id === cur))} disabled={st === "loading"} className="underline disabled:text-slate-300">새로 생성</button>
                            <button onClick={() => copy(q.id + cur, text)} className="underline">{copied === q.id + cur ? "복사됨" : "복사"}</button>
                          </div>
                        </div>
                      </>
                    )}
                  </div>
                </section>
              );
            })}
          </div>
        )}

        {step === 2 && (
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
                  <li>Q6 사업 분야: {field?.field ? `${field.field} 추천` : "직접 선택"}</li>
                  <li>Q7 창업 여부: {form.isBusiness ? "현재 사업자" : "사업자 아님"}</li>
                  <li>Q9 팀원 수: {form.team}</li>
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
                    <p className="text-sm text-slate-400">아직 고르지 않았어요. <button onClick={() => setStep(1)} className="underline">초안 고르기</button>로 돌아가세요.</p>
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
