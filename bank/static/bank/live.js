/* Live quiz room. The server holds the room (current question, answers, reveal, leader); every phone
   polls it every second and draws the same screen, so joining late or after losing the
   connection lands you on the current question. Answers and leader moves are sent as actions. */
(() => {
  // The page wears the quiz screen's id, so it gets the same question styles (and no "Go back to quiz" reminder).
  const root = document.querySelector("#quiz[data-live]");
  if (!root) return;
  const D = root.dataset;
  const ME = +D.userId;
  const TIMERS = [0, 20, 30, 45, 60, 90];
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const csrf = () => (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
  const friendsList = JSON.parse(document.getElementById("live-friends").textContent || "[]");

  let Q = [], S = null, sig = "", busy = false, error = "", offline = false;
  let pollTimer = null, deadline = null, timerFired = false;
  let tfDraft = {}, panelOpen = false, pendingRender = false, navOpen = false, blockNavOpen = false;
  // Blocks of 50, exactly like the quiz screen.
  const BLOCK = () => (S && S.block_size) || 50;
  const blockCount = () => Math.max(1, Math.ceil(S.total / BLOCK()));
  const blockOf = p => Math.floor(p / BLOCK());
  const blockStart = b => b * BLOCK();
  const blockEnd = b => Math.min(blockStart(b) + BLOCK(), S.total);
  const blockPositions = b => Array.from({length: blockEnd(b) - blockStart(b)}, (_, i) => blockStart(b) + i);
  const blockTally = b => {
    const marks = S.marks || {}, t = {ok: 0, bad: 0};
    blockPositions(b).forEach(p => { if (marks[p] === "ok") t.ok++; else if (marks[p] === "bad") t.bad++; });
    return t;
  };
  const inviteSel = new Set();
  const nt = {};   // personal note panel per question id: {open, text, busy, err, msg}
  const cm = {};   // comment panel per question id: {open, text, err, msg}
  const PERSONAL = () => S && !S.can_save;      // My Space questions: no save, notes or comments
  const AI_SITES = {
    chatgpt: {name: "ChatGPT", url: "https://chatgpt.com/"},
    claude: {name: "Claude", url: "https://claude.ai/new"},
    gemini: {name: "Gemini", url: "https://gemini.google.com/app"},
    deepseek: {name: "DeepSeek", url: "https://chat.deepseek.com/"},
    grok: {name: "Grok", url: "https://grok.com/"},
    copilot: {name: "Copilot", url: "https://copilot.microsoft.com/"},
    perplexity: {name: "Perplexity", url: "https://www.perplexity.ai/"},
  };

  const avatar = p => p.avatar
    ? `<img class="avatar avatar-sm" src="${esc(p.avatar)}" alt="" width="30" height="30">`
    : `<span class="avatar avatar-sm avatar-initials ${esc(p.color)}" aria-hidden="true">${esc(p.initials)}</span>`;
  const firstName = m => m.id === ME ? "You" : String(m.name).split(" ")[0];
  const names = list => (list || []).map(esc).join(", ");

  // ------------------------------------------------------------ talking to the server
  async function loadQuestions() {
    const r = await fetch(D.questions, {cache: "no-store"});
    if (!r.ok) throw new Error("questions");
    Q = (await r.json()).questions || [];
  }

  function scrollToCurrentQuestion() {
    const questionContent = root.querySelector(".quiz-question .stem") || root.querySelector(".quiz-question");
    if (questionContent) questionContent.scrollIntoView({behavior: "smooth", block: "start"});
  }

  function scrollToAnswerAction() {
    const action = root.querySelector(".quiz-controls .btn.pri") || root.querySelector(".live-card .fb, .live-card .live-lock, .live-wait");
    if (!action) return;
    const targetTop = Math.round(window.innerHeight * 0.68);
    const scrollTop = window.scrollY + action.getBoundingClientRect().top - targetTop;
    window.scrollTo({top: Math.max(0, scrollTop), behavior: "smooth"});
  }

  function apply(state) {
    const {seconds_left: secondsLeft, ...rest} = state;
    const next = JSON.stringify(rest);
    const moved = S && S.position !== state.position;
    const justRevealed = S && !moved && !S.revealed && state.revealed;
    const reportOpened = S && S.results_block == null && state.results_block != null;
    const newDeadline = secondsLeft === null || secondsLeft === undefined ? null : Date.now() + secondsLeft * 1000;
    if (!deadline || !newDeadline || Math.abs(newDeadline - deadline) > 1500) { deadline = newDeadline; timerFired = false; }
    if (moved) tfDraft = {};
    S = state;
    if (next !== sig) {
      sig = next;
      render();
      if (reportOpened) window.scrollTo({top: 0, behavior: "smooth"});
      else if (moved) scrollToCurrentQuestion();
      else if (justRevealed) scrollToAnswerAction();
    }
    tickTimer();
  }

  async function poll() {
    clearTimeout(pollTimer);
    try {
      const r = await fetch(D.state, {cache: "no-store"});
      if (r.status === 403) { gone(); return; }
      offline = !r.ok;
      if (r.ok) apply(await r.json());
    } catch (e) {
      offline = true;
    }
    showConnection();
    pollTimer = setTimeout(poll, S && S.ended ? 15000 : document.hidden ? 8000 : 1000);
  }

  async function act(action, extra = {}) {
    if (busy) return false;
    busy = true;
    error = "";
    let ok = false;
    try {
      const r = await fetch(D.actUrl, {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-CSRFToken": csrf()},
        body: JSON.stringify({action, ...extra}),
      });
      const j = await r.json().catch(() => ({}));
      if (j.url) { location.href = j.url; return true; }
      if (j.error) error = j.error;
      if (j.state) { busy = false; apply(j.state); ok = !j.error; }
    } catch (e) {
      error = "Connection problem. Please try again.";
    }
    busy = false;
    sig = "";
    render();
    return ok;
  }

  async function toggleMySave(q) {
    const shown = savedShown(q);
    try {
      const r = await fetch(D.save.replace("/0/", "/" + q.id + "/"), {
        method: "POST", headers: {"Content-Type": "application/json", "X-CSRFToken": csrf()},
        body: JSON.stringify({saved: !shown}),
      });
      const j = await r.json();
      if (typeof j.saved === "boolean") q.localSaved = j.saved;
    } catch (e) {
      error = "Couldn't save. Please try again.";
    }
    sig = "";
    render();
  }

  const savedShown = q => q.localSaved !== undefined ? q.localSaved : !!(q.saved || (S.group_saved && S.me.follow));

  async function postJson(url, payload) {
    const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json", "X-CSRFToken": csrf()},
                                body: JSON.stringify(payload || {})});
    let j = {};
    try { j = await r.json(); } catch (e) {}
    return {status: r.status, json: j};
  }
  const forQuestion = (template, id) => template.replace("/0/", "/" + id + "/");

  async function saveNote(q, text) {
    const n = nt[q.id] || (nt[q.id] = {});
    n.busy = true; n.err = ""; n.msg = ""; render(true);
    const r = await postJson(forQuestion(D.note, q.id), {text});
    n.busy = false;
    if (r.json && typeof r.json.note === "string") {
      q.my_note = r.json.note;
      q.localSaved = r.json.saved;
      n.text = r.json.note;
      n.msg = r.json.note ? "✓ Note saved." : "Note deleted.";
    } else n.err = (r.json && r.json.error) || "Could not save the note.";
    render(true);
  }

  async function sendComment(q) {
    const c = cm[q.id] || (cm[q.id] = {});
    const text = (c.text || "").trim();
    if (!text) { c.err = "Please write a comment first."; render(true); return; }
    const r = await postJson(forQuestion(D.comment, q.id), {text});
    if (r.json && r.json.ok) { c.msg = "✓ Sent to the admins — thank you!"; c.text = ""; c.err = ""; }
    else c.err = (r.json && r.json.error) || "Could not send the comment.";
    render(true);
  }

  function noteBox(q) {
    const n = nt[q.id] || {};
    const label = n.open ? "📝 Hide note ▴" : q.my_note ? "📝 Show note" : "📝 Add note";
    const button = `<button class="btn${q.my_note ? " has-note" : ""}" data-act="note-toggle" aria-expanded="${!!n.open}">${label}</button>`;
    if (!n.open) return button;
    const text = n.text ?? q.my_note ?? "";
    return `${button}<div class="cmtbox question-note">
      <textarea class="question-note-text" data-note-for="${q.id}" rows="4" maxlength="5000" placeholder="Something to remember next time you meet this question…">${esc(text)}</textarea>
      <div class="row" style="margin-top:6px"><span class="mute">Only you see this. Questions with notes are kept in your Saved questions.</span>
        <span>${q.my_note ? `<button class="btn" data-act="note-delete" ${n.busy ? "disabled" : ""}>Delete</button> ` : ""}<button class="btn pri" data-act="note-save" ${n.busy ? "disabled" : ""}>${n.busy ? "Saving…" : "Save note"}</button></span></div>
      ${n.err ? `<div class="fb bad">${esc(n.err)}</div>` : ""}${n.msg ? `<div class="fb ok">${esc(n.msg)}</div>` : ""}</div>`;
  }

  function commentBox(q) {
    const c = cm[q.id] || {};
    const button = `<button class="btn" data-act="cmtoggle">💬 Comment${c.open ? " ▴" : ""}</button>`;
    if (!c.open) return button;
    return `${button}<div class="cmtbox"><textarea class="cmt" data-comment-for="${q.id}" rows="3" maxlength="2000" placeholder="Spotted a mistake or something unclear? Tell the admins…">${esc(c.text || "")}</textarea>
      <div class="row" style="margin-top:6px"><span class="mute">Private — only the admins see this.</span>
        <button class="btn pri" data-act="cmsend">Send</button></div>
      ${c.err ? `<div class="fb bad">${esc(c.err)}</div>` : ""}${c.msg ? `<div class="fb ok">${esc(c.msg)}</div>` : ""}</div>`;
  }

  function aiSettings() {
    try {
      const saved = JSON.parse(localStorage.getItem(`gd:settings:${D.userId}`) || "{}");
      return {language: saved.aiLanguage === "ar" ? "ar" : "en", site: AI_SITES[saved.aiSite] ? saved.aiSite : "chatgpt",
              depth: saved.aiDepth === "deep" ? "deep" : "regular"};
    } catch (e) {
      return {language: "en", site: "chatgpt", depth: "regular"};
    }
  }

  // The same "AI ↗" button as the quiz screen, with the answer key and what you picked.
  function aiLink(q) {
    const {language, site, depth} = aiSettings();
    const ai = AI_SITES[site];
    const key = S.key || {}, mine = S.me.answer || {};
    const lines = [
      "I am studying medicine with a self-study quiz. Help me understand the underlying topic, not just memorize the answer.",
      "Do not mention, identify, or promote the website or quiz platform this question came from.",
      "Explain clearly and educationally. If the supplied explanation is incomplete or appears inaccurate, point that out and give the accurate reasoning.",
      language === "ar" ? "Write your entire response in Arabic. Keep medical terms in English in parentheses where useful."
        : "Write your entire response in English.",
      "Start your reply by writing out the question in full (with its options, statements or labels) exactly as given below, then give your explanation.",
      depth === "deep"
        ? "Give a deep explanation. First say which answer is correct and why, and why each other answer is wrong. Then dive deeper into the topic: the underlying mechanism, anatomy or physiology, related concepts, clinical relevance, and the high-yield points and common exam traps around it."
        : "Keep the explanation focused: say which answer is correct and why, and briefly why each other answer is wrong.",
    ];
    if (q.kind === "desc") {
      lines.push("Question type: Descriptive.", `Question: ${q.stem}`,
        `Suggested answer: ${key.answer || "No written answer was provided."}`, `Quiz explanation: ${key.explanation || "None provided."}`);
    } else if (q.kind === "tf") {
      lines.push("Question type: True/False statements.", `Question: ${q.stem}`,
        "For each statement, explain why it is true or false. For false statements, briefly give a corrected version.",
        "Statements and answer key:",
        ...q.options.map(o => {
          const id = String(o.id), picked = (mine.answers || {})[id];
          return `${o.letter}. ${o.text} — Correct answer: ${(key.answer_key || {})[id] ? "TRUE" : "FALSE"}${picked === undefined ? "" : ` (I selected ${picked ? "True" : "False"})`}`;
        }),
        `Quiz explanation: ${key.explanation || "None provided."}`);
    } else {
      lines.push("Question type: Multiple choice.", `Question: ${q.stem}`,
        "Explain why each option is correct or incorrect, and teach the concept being tested.", "Options and answer key:",
        ...q.options.map(o => `${o.letter}. ${o.text} — ${(key.correct_ids || []).includes(o.id) ? "CORRECT" : "INCORRECT"}${mine.selected === o.id ? " (I selected this option)" : ""}`),
        `Quiz explanation: ${key.explanation || "None provided."}`);
    }
    if (key.note) lines.push(`Quiz note: ${key.note}`);
    if (q.image_url) lines.push(`The question's image is at ${new URL(q.image_url, location.href).href}. Use it for visual details; if you can't open it, ask me to paste it instead of guessing.`);
    lines.push("This is for learning only, not for personal diagnosis or treatment.");
    const prompt = lines.join("\n\n");
    if (site === "chatgpt") {
      const chatUrl = new URL(ai.url);
      chatUrl.searchParams.set("q", prompt);
      return `<a class="btn" href="${esc(chatUrl.href)}" target="_blank" rel="noopener noreferrer">AI ↗</a>`;
    }
    return `<a class="btn" href="${esc(ai.url)}" target="_blank" rel="noopener noreferrer" data-copy-prompt="${esc(prompt)}">AI ↗</a>
      <p class="mute image-chatgpt-hint">The question is copied when you open ${esc(ai.name)}: paste it there with Ctrl+V (or long-press → Paste).</p>`;
  }

  const editUrl = id => {
    if (!D.edit) return "";
    const target = new URL(D.edit.replace("/0/", "/" + id + "/"), location.href);
    target.searchParams.set("next", location.pathname);
    return target.pathname + target.search;
  };

  // ------------------------------------------------------------ drawing
  const typing = () => {
    const el = document.activeElement;
    return el && root.contains(el) && el.tagName === "TEXTAREA";
  };

  function render(force) {
    if (!S) return;
    if (!force && typing()) { pendingRender = true; return; }
    pendingRender = false;
    const current = Q[S.position] || {};
    root.dataset.questionType = !S.ended && current.kind === "desc" ? "descriptive" : "mcq_tf";
    root.innerHTML = header() + (error ? `<div class="fb bad" role="alert">${esc(error)}</div>` : "")
      + (S.ended ? summary() : S.results_block !== null && S.results_block !== undefined ? blockReport() : card()) + people();
    tickTimer();
  }

  function header() {
    const leader = S.members.find(m => m.leader);
    const role = S.ended ? "Finished"
      : !leader ? "" : leader.id === ME ? "You're leading" : `Following ${esc(leader.name)}`;
    const marks = S.marks || {};
    const b = S.results_block ?? blockOf(S.position);
    const {ok, bad} = blockTally(b);
    const total = blockEnd(b) - blockStart(b) || 1;
    const left = total - ok - bad;
    const scope = blockCount() > 1 ? `Block ${b + 1}/${blockCount()} · ` : "";
    const nav = blockPositions(b).map((p, i) =>
      `<button class="${marks[p] || "new"} ${p === S.position ? "cur" : ""}" ${S.me.leader && !S.ended ? `data-act="go" data-p="${p}"` : "disabled"} aria-label="Question ${i + 1}" title="Question ${i + 1}">${i + 1}</button>`).join("");
    return `${S.ended ? "" : blockNav()}<div class="row quiz-meta"><a class="btn quiz-back-link" href="${esc(D.back)}">← Back</a>
        <span class="quiz-meta-copy"><span class="quiz-meta-title"><span class="live-pill${S.ended ? " is-ended" : ""}">${S.ended ? "ENDED" : "● LIVE"}</span> ${esc(S.title)}</span>
          <span class="quiz-meta-stats">${scope}${ok} correct · ${bad} missed · ${left} left · Code <b class="live-code">${esc(S.code)}</b> · ${role}</span></span>
        <span class="live-conn" data-conn hidden>Reconnecting…</span></div>
      <div class="bar quiz-progress"><i class="g" style="width:${ok / total * 100}%"></i><i class="r" style="width:${bad / total * 100}%"></i></div>
      ${S.ended ? "" : `<div class="nav-wrap quiz-navigation"><button class="btn nav-toggle" data-act="toggle-nav" aria-expanded="${navOpen}" title="${navOpen ? "Hide question list" : "Show question list"}">${navOpen ? "▾" : "▸"}</button><div class="nav ${navOpen ? "" : "closed"}">${navOpen ? nav : ""}</div></div>`}
      <div class="live-people" aria-label="People in this quiz">${S.members.map(m => `
        <span class="live-person${m.online ? " is-on" : ""}${m.answered && !S.revealed && !S.ended ? " has-answered" : ""}"
              title="${esc(m.name)}${m.leader ? " · leader" : ""}${m.online ? "" : " · not connected"}${m.answered && !S.revealed ? " · answered" : ""}">
          <span class="live-face">${avatar(m)}${m.leader ? '<i class="live-crown" aria-hidden="true">👑</i>' : ""}${m.answered && !S.revealed && !S.ended ? '<i class="live-tick" aria-hidden="true">✓</i>' : ""}</span>
          <small>${esc(firstName(m))}</small>
        </span>`).join("")}</div>`;
  }

  function blockNav() {
    if (blockCount() <= 1) return "";
    const active = S.results_block ?? blockOf(S.position);
    const lead = S.me.leader;
    const toggle = `<button class="btn nav-toggle" data-act="toggle-block-nav" aria-expanded="${blockNavOpen}" title="${blockNavOpen ? "Hide block list" : "Show block list"}">${blockNavOpen ? "▾" : "▸"}</button>`;
    const summary = `<button class="btn pri" ${lead ? `data-act="go" data-p="${blockStart(active)}"` : "disabled"}>Block ${active + 1}</button>`;
    const buttons = Array.from({length: blockCount()}, (_, b) => {
      const t = blockTally(b), size = blockEnd(b) - blockStart(b);
      const done = blockPositions(b).every(p => (S.marks || {})[p]);
      return `<button class="btn ${b === active ? "pri" : ""}" ${lead ? `data-act="go" data-p="${blockStart(b)}"` : "disabled"}>
        Block ${b + 1}<span class="mute"> · ${size}</span>${done ? " ✓" : ""}
        <span class="mute"> (${t.ok}/${size})</span></button>`;
    }).join(" ");
    return `<div class="row quiz-block-nav"><span class="mute">Blocks:</span>${toggle}${blockNavOpen ? buttons : summary}</div>`;
  }

  // The quiz screen's block report, for everyone in the room at once, with the group's scores added.
  function blockReport() {
    const b = S.results_block, positions = blockPositions(b), marks = S.marks || {};
    const t = blockTally(b), size = positions.length;
    const wrong = positions.filter(p => marks[p] === "bad");
    const left = positions.filter(p => !marks[p]);
    const pct = size ? Math.round(t.ok / size * 100) : 0;
    const local = p => String(p - blockStart(b) + 1).padStart(2, "0");
    // Only the leader moves the room, so only the leader gets links to a question.
    const jumpTo = (p, label) => S.me.leader ? `<a href="#" data-act="go" data-p="${p}" aria-label="Question ${local(p)}">${label}</a>` : `<b>${label}</b>`;
    const li = (arr, title) => arr.length ? `<h3 class="quiz-report-list-title">${title} (${arr.length})</h3><ul class="w quiz-report-list">`
      + arr.map(p => `<li>${jumpTo(p, `Q${local(p)}`)}<span>${esc(String((Q[p] || {}).stem || "").slice(0, 120))}</span></li>`).join("") + "</ul>" : "";
    const s = S.block_summary || {};
    const board = (s.board || []).map((row, i) => `<li class="live-score"><span class="live-rank">${i + 1}</span><b>${esc(row.name)}</b>
        <span class="live-score-bar" aria-hidden="true"><i style="width:${row.pct}%"></i></span>
        <span class="live-score-num">${row.right}/${row.total} · ${row.pct}%</span></li>`).join("");
    const hard = (s.hard || []).map(h => `<li>${jumpTo(h.position, `Q${local(h.position)}`)}<span>${esc(String((Q[h.position] || {}).stem || h.stem).slice(0, 120))} <small class="mute">· ${h.wrong} of ${h.total} missed</small></span></li>`).join("");
    const lastBlock = b >= blockCount() - 1;
    const leaderActions = S.me.leader
      ? `<button class="btn" data-act="block-back">← Back to block</button>
         <div class="result-action-group quiz-report-action-group">
           ${S.can_save && s.hard_count && !s.hard_saved ? `<button class="btn" data-act="save-hard">★ Save the ${s.hard_count} the group missed, for everyone</button>` : ""}
           ${lastBlock ? '<button class="btn pri" data-act="end" data-confirm="Finish the quiz for everyone?">Finish quiz</button>'
             : `<button class="btn pri" data-act="go" data-p="${blockStart(b + 1)}">Next block →</button>`}</div>`
      : `<p class="mute">${lastBlock ? "The leader will finish the quiz." : "The leader will start the next block."}</p>`;
    return `<div class="card quiz-report"><div class="quiz-report-summary"><div class="big quiz-score">${pct}%</div>
      <div><h2 class="quiz-report-title">Block ${b + 1} report</h2>
      <div class="quiz-report-stats">${t.ok} correct · ${t.bad} wrong · ${left.length} unanswered / revealed</div></div>
      </div><div class="quiz-report-content">
      <div class="bar quiz-report-progress"><i class="g" style="width:${t.ok / (size || 1) * 100}%"></i><i class="r" style="width:${t.bad / (size || 1) * 100}%"></i></div>
      ${li(wrong, "You missed")}
      ${board ? `<h3 class="quiz-report-list-title">The group in this block${s.accuracy !== null && s.accuracy !== undefined ? ` · ${s.accuracy}%` : ""}</h3><ol class="live-board">${board}</ol>` : ""}
      ${hard ? `<h3 class="quiz-report-list-title">The ones the group found hard (${s.hard_count})</h3><ul class="w quiz-report-list">${hard}</ul>` : ""}
      ${s.hard_saved ? '<p><span class="exam-badge is-source">★ Missed questions saved for everyone</span></p>' : ""}
      <div class="result-actions quiz-report-actions">${leaderActions}</div></div></div>`;
  }

  function figure(q) {
    if (!q.image_url) return "";
    const ratio = q.image_width > 0 && q.image_height > 0
      ? ` style="--image-aspect-ratio:${q.image_width}/${q.image_height};--image-max-width:${78 * q.image_width / q.image_height}vh;--image-w-per-h:${q.image_width / q.image_height}"` : "";
    return `<figure class="quiz-image-figure"><div class="image-stage${ratio ? " is-calibrated" : ""}"${ratio}>
      <img src="${esc(q.image_url)}" alt="Image for this question" loading="eager"></div>
      <figcaption><a href="${esc(q.image_url)}" target="_blank" rel="noopener">Open original image</a></figcaption></figure>`;
  }

  function questionBody(q) {
    const me = S.me, rev = S.revealed, key = S.key || {}, picks = S.picks || {};
    if (q.kind === "mcq") {
      const mine = me.answer ? me.answer.selected : null;
      return `<div class="stem">${esc(q.stem)}</div><div class="quiz-answer-list">${q.options.map(o => {
        let cl = "";
        if (rev) cl = (key.correct_ids || []).includes(o.id) ? "ok" : mine === o.id ? "bad" : "";
        else if (mine === o.id) cl = "sel";
        const who = rev ? picks[String(o.id)] : null;
        return `<button class="opt ${cl}" ${rev || busy ? "disabled" : ""} data-act="pick" data-id="${o.id}">
          <b>${o.letter}</b><span>${esc(o.text)}${who && who.length ? `<small class="live-who">${names(who)}</small>` : ""}</span></button>`;
      }).join("")}</div>`;
    }
    if (q.kind === "tf") {
      const sent = (me.answer && me.answer.answers) || {};
      return `<div class="stem">${esc(q.stem)}</div><div class="quiz-answer-list quiz-answer-list-tf">${q.options.map(o => {
        const id = String(o.id);
        const chosen = id in tfDraft ? tfDraft[id] : sent[id];
        const button = (value, label) => {
          let cl = "";
          if (rev) cl = (key.answer_key || {})[id] === value ? "ok" : sent[id] === value ? "bad" : "";
          else if (chosen === value) cl = "sel";
          return `<button class="${cl}" ${rev ? "disabled" : ""} data-act="tf" data-id="${id}" data-v="${value}">${label}</button>`;
        };
        const t = rev ? picks[id + ":T"] || [] : [], f = rev ? picks[id + ":F"] || [] : [];
        const who = t.length || f.length
          ? `<small class="live-who">${t.length ? "True: " + names(t) : ""}${t.length && f.length ? " · " : ""}${f.length ? "False: " + names(f) : ""}</small>` : "";
        return `<div class="tf"><div class="t"><b>${o.letter}</b>${esc(o.text)}${who}</div>
          <div class="tfb">${button(true, "True")}${button(false, "False")}</div></div>`;
      }).join("")}</div>`;
    }
    if (q.kind === "desc") {
      return `<section class="flash-study"><div class="flash-prompt"><div class="stem">${esc(q.stem)}</div></div>
        ${rev ? `<div class="flash-answer"><b>Answer</b><div>${esc(key.answer || "")}</div>${key.answer_image_url
          ? `<figure class="answer-image-figure"><img src="${esc(key.answer_image_url)}" alt="Answer image"></figure>` : ""}</div>`
        : `<div class="flash-answer-placeholder"><span class="flash-seal" aria-hidden="true">✦</span><strong>Answer held back</strong>
            <span>Think of your answer. ${me.leader ? "Reveal it when everyone's ready." : "The leader will reveal it."}</span></div>`}</section>`;
    }
    return `<div class="stem">${esc(q.stem || "")}</div><p class="mute">This question can't be played live.</p>`;
  }

  function feedback(q) {
    const me = S.me, key = S.key || {};
    const explain = (key.explanation ? `<p>${esc(key.explanation)}</p>` : "") + (key.note ? `<p class="note">⚠ ${esc(key.note)}</p>` : "");
    if (q.kind === "desc") {
      const grade = me.answer && me.answer.grade;
      const picks = S.picks || {};
      const tally = (picks.known || []).length || (picks.missed || []).length
        ? `<p class="live-tally">${picks.known ? `✓ Knew it: ${names(picks.known)}` : ""}${picks.known && picks.missed ? " · " : ""}${picks.missed ? `✗ Missed it: ${names(picks.missed)}` : ""}</p>` : "";
      return `<div class="fb info">${explain}<div class="ai-question-actions">${aiLink(q)}</div></div>
        <div class="live-grade">${grade ? `<span class="tag">${grade === "known" ? "You marked: knew it" : "You marked: missed it"}</span>` : '<span class="mute">Did you know it?</span>'}
          <button class="btn ${grade === "known" ? "pri" : ""}" data-act="grade" data-grade="known">I knew it</button>
          <button class="btn ${grade === "missed" ? "pri" : ""}" data-act="grade" data-grade="missed">I missed it</button></div>${tally}`;
    }
    let head = me.correct === true ? '<div class="fb ok"><b>✓ You got it.</b>'
      : me.correct === false ? '<div class="fb bad"><b>✗ Not this time.</b>' : '<div class="fb info"><b>You didn\'t answer.</b>';
    head += q.kind === "tf"
      ? " Key: " + q.options.map(o => o.letter + "=" + ((key.answer_key || {})[String(o.id)] ? "T" : "F")).join(", ") + "."
      : " Correct: " + q.options.filter(o => (key.correct_ids || []).includes(o.id)).map(o => o.letter).join(" and ") + ".";
    const right = S.right || [], wrong = S.wrong || [];
    const tally = right.length || wrong.length
      ? `<p class="live-tally">${right.length ? `✓ ${names(right)}` : ""}${right.length && wrong.length ? " · " : ""}${wrong.length ? `✗ ${names(wrong)}` : ""}</p>` : "";
    return head + explain + `<div class="ai-question-actions">${aiLink(q)}</div></div>` + tally;
  }

  function card() {
    const q = Q[S.position] || {missing: true};
    const me = S.me, rev = S.revealed;
    const joined = S.members.filter(m => m.joined);
    const answered = S.members.filter(m => m.answered).length;
    const b = blockOf(S.position);
    const n = S.position - blockStart(b) + 1, blockSize = blockEnd(b) - blockStart(b);
    let content;
    if (q.missing) content = '<p class="mute">This question was removed from the bank since the quiz started.</p>';
    else {
      content = questionBody(q);
      if (!rev && q.kind === "tf") {
        const sent = (me.answer && me.answer.answers) || {};
        const merged = {...sent, ...tfDraft};
        const allSet = q.options.every(o => String(o.id) in merged);
        const changed = Object.keys(tfDraft).some(k => tfDraft[k] !== sent[k]);
        if (!me.answer || changed) {
          content += `<div class="live-lock"><button class="btn pri" data-act="tf-lock" ${allSet && !busy ? "" : "disabled"}>${me.answer ? "Update my answers" : "Lock in my answers"}</button></div>`;
        } else content += '<p class="mute live-locked">Answers locked in ✓ You can change them until the reveal.</p>';
      }
      if (rev) content += feedback(q);
    }
    const status = rev ? '<span class="live-status-text">Answer shown</span>'
      : `<span class="live-status-text"><b>${answered}</b> of ${joined.length} answered</span><span class="live-timer" data-timer></span>`;
    const tools = [];
    if (S.can_save && !q.missing) {
      if (D.edit) tools.push(`<a class="btn" href="${esc(editUrl(q.id))}">✎ Edit</a>`);
      tools.push(`<button class="star ${savedShown(q) ? "on" : ""}" data-act="my-save" title="Save this question to your account">${savedShown(q) ? "★ Saved" : "☆ Save"}</button>`);
      if (me.leader) tools.push(`<button class="star live-group-star ${S.group_saved ? "on" : ""}" data-act="group-save" title="Save this question for everyone who follows saves">${S.group_saved ? "★ Saved for everyone" : "☆ Save for everyone"}</button>`);
    }
    const last = S.position >= S.total - 1;
    const blockLast = !last && S.position === blockEnd(b) - 1;
    const controls = me.leader
      ? `<div class="ctl quiz-controls live-controls">
          <button class="btn" data-act="go" data-p="${S.position - 1}" ${S.position === 0 || busy ? "disabled" : ""}>← Prev</button>
          ${!rev ? `<button class="btn pri" data-act="reveal" ${busy ? "disabled" : ""}>Reveal answer</button>`
            : last && blockCount() > 1 ? `<button class="btn pri" data-act="finish-block">Finish block →</button>`
            : last ? `<button class="btn pri" data-act="end" data-confirm="Finish the quiz for everyone?">Finish quiz</button>`
            : blockLast ? `<button class="btn pri" data-act="finish-block">Finish block →</button>`
            : `<button class="btn pri" data-act="go" data-p="${S.position + 1}" ${busy ? "disabled" : ""}>Next →</button>`}
        </div>
        <div class="live-leader-tools">
          <label class="live-timer-pick"><span>⏱ Timer</span><select data-change="timer" aria-label="Answer timer">${TIMERS.map(s =>
            `<option value="${s}" ${S.timer === s ? "selected" : ""}>${s ? s + " seconds" : "Off"}</option>`).join("")}</select></label>
          ${last ? "" : '<button class="exam-link-btn is-danger" data-act="end" data-confirm="End the quiz for everyone now?">End quiz</button>'}
        </div>`
      : `<p class="live-wait mute">${rev ? "The leader will move on to the next question." : "Pick your answer. The leader reveals it for everyone."}${S.group_saved ? ' <span class="tag">★ The leader saved this</span>' : ""}</p>`;
    return `<div class="card quiz-question live-card${q.kind === "desc" ? " flashcard descriptive-question" + (rev ? " is-revealed" : "") : ""}${q.image_url ? " quiz-question-has-image" : ""}">
      <div class="qh">
        <div class="quiz-question-context"><span class="qn quiz-position" aria-label="Question ${n} of ${blockSize}"><span>${String(n).padStart(2, "0")}</span><span class="quiz-position-divider">/</span><span class="quiz-position-total">${String(blockSize).padStart(2, "0")}</span></span>
          ${q.label ? `<span class="tag quiz-source-badge">${esc(String(q.label).replace(/_/g, " "))}</span>` : ""}
          ${q.multi ? '<span class="tag">more than one defensible</span>' : ""}
          ${q.kind === "tf" ? '<span class="tag">True / False</span>' : ""}</div>
        <div class="quiz-question-tools quiz-question-tools-solo">${tools.join("")}</div>
      </div>
      <div class="live-status">${status}</div>
      ${q.image_url ? `<div class="image-question-layout">${figure(q)}<div class="image-question-side">${content}</div></div>` : content}
      ${controls}
      ${S.can_save && !q.missing ? `<div class="cmtwrap quiz-comments">${commentBox(q)} ${noteBox(q)}</div>` : ""}
    </div>`;
  }

  function people() {
    const me = S.me;
    const inRoom = new Set(S.members.map(m => m.id));
    const pals = friendsList.filter(p => !inRoom.has(p.id));
    const rows = S.members.map(m => `<li class="live-member">
        ${avatar(m)}<span class="live-member-text"><b>${esc(m.name)}${m.id === ME ? " (you)" : ""}</b>
          <small class="mute">${m.leader ? "👑 Leader · " : ""}${m.online ? "Connected" : m.joined ? "Not connected" : "Invited, hasn't opened it"}${S.can_save ? (m.follow ? " · follows saves" : " · not following saves") : ""}</small></span>
        ${me.leader && !S.ended && m.id !== ME && m.joined ? `<button class="exam-link-btn" data-act="pass" data-user="${m.id}" data-confirm="Make ${esc(m.name)} the leader?">Make leader</button>` : ""}
      </li>`).join("");
    const invite = S.ended ? "" : S.full
      ? `<p class="mute">The room is full (10 people).</p>`
      : pals.length ? `<div class="live-invite"><span class="exam-chips-label">Invite friends</span>
          <div class="myspace-friends">${pals.map(p => `<label class="myspace-friend"><input type="checkbox" data-invite="${p.id}" ${inviteSel.has(p.id) ? "checked" : ""}>${avatar(p)}<span>${esc(p.name)}</span></label>`).join("")}</div>
          <button class="exam-card-start" data-act="invite" ${inviteSel.size && !busy ? "" : "disabled"}>Invite <span aria-hidden="true">→</span></button></div>`
      : `<p class="mute">All your friends are already here. <a href="${esc(D.friendsPage)}">Add friends</a></p>`;
    const follow = S.can_save && !S.ended ? `<label class="settings-check live-follow"><input type="checkbox" data-change="follow" ${me.follow ? "checked" : ""}>
        <span><b>Save what the leader saves to my account</b><small class="mute">Questions the leader saves go straight into your Saved list.</small></span></label>` : "";
    return `<details class="topic-manual live-room-panel" ${panelOpen ? "open" : ""} data-panel>
      <summary><span class="topic-manual-icon" aria-hidden="true">👥</span>
        <span><b>People · ${S.members.length}/10</b><small>${S.ended ? "Who took part" : `Friends next to you can join with code <b>${esc(S.code)}</b> in Messages`}</small></span>
        <span class="topic-manual-chevron" aria-hidden="true">⌄</span></summary>
      <div class="live-room-body">
        <ul class="live-members">${rows}</ul>
        ${follow}
        ${invite}
        ${S.ended ? "" : '<button class="exam-link-btn is-danger" data-act="leave" data-confirm="Leave this live quiz?">Leave the live quiz</button>'}
      </div>
    </details>`;
  }

  function summary() {
    const s = S.summary || {};
    const board = (s.board || []).map((row, i) => `<li class="live-score">
        <span class="live-rank">${i + 1}</span><b>${esc(row.name)}</b>
        <span class="live-score-bar" aria-hidden="true"><i style="width:${row.pct}%"></i></span>
        <span class="live-score-num">${row.right}/${row.total} · ${row.pct}%</span></li>`).join("");
    const hard = (s.hard || []).map(h => `<li><b>Q${h.position + 1}</b> ${esc(h.stem)} <small class="mute">${h.wrong} of ${h.total} missed</small></li>`).join("");
    const saveHard = S.can_save && s.hard_count
      ? S.me.leader && !s.hard_saved
        ? `<button class="exam-card-start" data-act="save-hard">★ Save the ${s.hard_count} the group missed, for everyone</button>`
        : s.hard_saved ? '<span class="exam-badge is-source">★ Missed questions saved for everyone</span>' : ""
      : "";
    return `<div class="card live-card live-summary">
      <h2>Quiz finished 🎉</h2>
      <p class="exam-card-meta"><span><b>${s.covered || 0}</b> question${s.covered === 1 ? "" : "s"} covered</span>
        ${s.accuracy !== null && s.accuracy !== undefined ? `<span>Group score <b>${s.accuracy}%</b></span>` : ""}</p>
      ${board ? `<h3>Scores</h3><ol class="live-board">${board}</ol>` : '<p class="mute">Nobody answered a question.</p>'}
      ${hard ? `<h3>The ones the group found hard</h3><ul class="live-hard">${hard}</ul>` : ""}
      <div class="exam-card-actions">${saveHard}
        <a class="exam-card-start is-quiet" href="${esc(D.back)}">Back to the quiz</a>
        ${S.can_save ? `<a class="exam-link-btn" href="${esc(D.saved)}">Open Saved</a>` : ""}</div>
    </div>`;
  }

  function showConnection() {
    const el = root.querySelector("[data-conn]");
    if (el) el.hidden = !offline;
  }

  function tickTimer() {
    const el = root.querySelector("[data-timer]");
    if (!el) return;
    if (!deadline || !S || S.revealed) { el.textContent = ""; return; }
    const left = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
    el.textContent = `⏱ ${left}s`;
    el.classList.toggle("is-low", left <= 5);
    if (left === 0 && !timerFired) { timerFired = true; setTimeout(poll, 700); }
  }

  function gone() {
    clearTimeout(pollTimer);
    root.innerHTML = `<div class="card live-card"><h2>You're not in this live quiz</h2>
      <p class="mute">You left it, or it was closed.</p><a class="exam-card-start" href="${esc(D.messages)}">Go to Messages</a></div>`;
  }

  // ------------------------------------------------------------ input
  root.addEventListener("click", e => {
    const summary = e.target.closest("[data-panel] > summary");
    if (summary) { e.preventDefault(); panelOpen = !panelOpen; summary.parentElement.open = panelOpen; return; }
    const b = e.target.closest("[data-act]");
    if (!b || b.disabled) return;
    e.preventDefault();
    const d = b.dataset;
    if (d.confirm && !confirm(d.confirm)) return;
    const q = Q[S.position] || {};
    switch (d.act) {
      case "pick": act("answer", {position: S.position, selected: +d.id}); break;   // stay put: the "answered" count is up here
      case "tf": tfDraft[d.id] = d.v === "true"; render(); break;
      case "tf-lock": {
        const answers = {...((S.me.answer && S.me.answer.answers) || {}), ...tfDraft};
        act("answer", {position: S.position, answers}).then(ok => { if (ok) { tfDraft = {}; render(); } });
        break;
      }
      case "grade": act("answer", {position: S.position, grade: d.grade}).then(ok => { if (ok) scrollToAnswerAction(); }); break;
      case "reveal": act("reveal"); break;
      case "toggle-nav": navOpen = !navOpen; render(true); break;
      case "toggle-block-nav": blockNavOpen = !blockNavOpen; render(true); break;
      case "finish-block": act("finish_block").then(ok => { if (ok) window.scrollTo({top: 0, behavior: "smooth"}); }); break;
      case "block-back": act("block_back"); break;
      case "go": act("go", {position: +d.p}); break;
      case "end": act("end"); break;
      case "group-save": act("save"); break;
      case "save-hard": act("save_hard"); break;
      case "my-save":
        if (savedShown(q) && q.my_note && !confirm("This question has a note. Removing it from Saved also deletes the note. Continue?")) break;
        toggleMySave(q);
        break;
      case "note-toggle": { const n = nt[q.id] || (nt[q.id] = {}); n.open = !n.open; if (n.open) n.text = q.my_note || ""; render(true); break; }
      case "note-save": saveNote(q, (nt[q.id] && nt[q.id].text) || ""); break;
      case "note-delete": if (confirm("Delete your note on this question?")) saveNote(q, ""); break;
      case "cmtoggle": { const c = cm[q.id] || (cm[q.id] = {}); c.open = !c.open; if (c.open) c.msg = ""; render(true); break; }
      case "cmsend": sendComment(q); break;
      case "pass": act("pass", {user: +d.user}); break;
      case "invite": act("invite", {friends: [...inviteSel]}).then(ok => { if (ok) { inviteSel.clear(); render(); } }); break;
      case "leave": act("leave"); break;
    }
  });

  root.addEventListener("input", e => {
    const t = e.target;
    if (t.dataset.noteFor) (nt[t.dataset.noteFor] || (nt[t.dataset.noteFor] = {})).text = t.value;
    if (t.dataset.commentFor) (cm[t.dataset.commentFor] || (cm[t.dataset.commentFor] = {})).text = t.value;
  });
  // Catch up on what happened while you were typing.
  root.addEventListener("focusout", () => setTimeout(() => { if (pendingRender && !typing()) render(); }, 0));
  // Other AI sites can't take the question in the link: copy it for pasting.
  root.addEventListener("click", e => {
    const link = e.target.closest("[data-copy-prompt]");
    if (link && navigator.clipboard) navigator.clipboard.writeText(link.dataset.copyPrompt).catch(() => {});
  });

  root.addEventListener("change", e => {
    const t = e.target;
    if (t.dataset.change === "timer") act("timer", {seconds: +t.value});
    else if (t.dataset.change === "follow") act("follow", {on: t.checked});
    else if (t.dataset.invite) {
      t.checked ? inviteSel.add(+t.dataset.invite) : inviteSel.delete(+t.dataset.invite);
      const button = root.querySelector('[data-act="invite"]');
      if (button) button.disabled = !inviteSel.size;
    }
  });

  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  setInterval(tickTimer, 250);

  loadQuestions().then(poll).catch(() => {
    root.innerHTML = '<div class="card"><h2>Couldn\'t load the questions</h2><p class="mute">Check your connection and reload the page.</p></div>';
  });
})();
