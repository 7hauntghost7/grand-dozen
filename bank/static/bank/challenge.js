/* Group challenge runner. Answers are checked by the server, so the page never holds the key ahead of time.
   reveal "each": the answer shows after each question. reveal "end": answers can be changed until Submit. */
(() => {
  const root = document.getElementById("quiz");
  if (!root || !root.classList.contains("challenge-page")) return;
  const D = root.dataset;
  const L = "ABCDEFGH";
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const csrf = () => (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
  let S = null, pos = 0, view = "intro", busy = false, error = "";
  const tfDraft = {};

  async function call(url, body) {
    const r = await fetch(url, body === undefined ? {} : {
      method: "POST", headers: {"Content-Type": "application/json", "X-CSRFToken": csrf()}, body: JSON.stringify(body),
    });
    let j = {};
    try { j = await r.json(); } catch (e) {}
    return {ok: r.ok, status: r.status, json: j};
  }
  async function load(start) {
    const r = await call(D.data + (start ? "?start=1" : ""));
    if (!r.ok) { root.innerHTML = '<div class="card live-card"><p>Couldn’t load the challenge. Refresh to try again.</p></div>'; return; }
    S = r.json;
    if (S.finished) view = view === "review" ? "review" : "done";
    else if (S.started) {
      if (view === "intro") pos = Math.max(0, S.questions.findIndex(q => !(String(q.n) in S.answers)));
      view = "quiz";
    } else view = "intro";
    render();
  }

  const answered = () => S.questions.filter(q => String(q.n) in S.answers).length;
  const shown = q => String(q.n) in S.results;
  const left = () => {
    if (!S.ends_at) return "";
    const ms = new Date(S.ends_at) - Date.now();
    if (ms <= 0) return "ended";
    const m = Math.floor(ms / 60000), h = Math.floor(m / 60), d = Math.floor(h / 24);
    return d >= 1 ? `${d}d ${h % 24}h left` : h >= 1 ? `${h}h ${m % 60}m left` : `${Math.max(1, m)} min left`;
  };
  const endsOn = () => S.ends_at ? new Date(S.ends_at).toLocaleString([], {weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"}) : "";

  // ------------------------------------------------------------------ pieces
  function body(q, locked) {
    const a = S.answers[String(q.n)] || null, res = S.results[String(q.n)] || null;
    if (q.kind === "mcq") {
      return `<div class="stem" dir="auto">${esc(q.stem)}</div><div class="quiz-answer-list">${q.options.map((text, j) => {
        let cl = "";
        if (res) cl = res.key[j] ? "ok" : a && a.selected === j ? "bad" : "";
        else if (a && a.selected === j) cl = "sel";
        return `<button class="opt ${cl}" ${res || locked || busy ? "disabled" : ""} data-act="pick" data-q="${q.n}" data-j="${j}">
          <b>${L[j]}</b><span dir="auto">${esc(text)}</span></button>`;
      }).join("")}</div>`;
    }
    if (q.kind === "tf") {
      const sent = (a && a.answers) || [];
      const draft = tfDraft[q.n] || {};
      return `<div class="stem" dir="auto">${esc(q.stem)}</div><div class="quiz-answer-list quiz-answer-list-tf">${q.options.map((text, j) => {
        const chosen = j in draft ? draft[j] : sent[j];
        const button = (value, label) => {
          let cl = "";
          if (res) cl = res.key[j] === value ? "ok" : sent[j] === value ? "bad" : "";
          else if (chosen === value) cl = "sel";
          return `<button class="${cl}" ${res || locked ? "disabled" : ""} data-act="tf" data-q="${q.n}" data-j="${j}" data-v="${value ? 1 : 0}">${label}</button>`;
        };
        const mark = res && sent.length ? (sent[j] === res.key[j] ? " ✓" : " ✗") : "";
        return `<div class="tf"><div class="t"><b>${L[j]}</b><span dir="auto">${esc(text)}</span><span class="mute">${mark}</span></div>
          <div class="tfb">${button(true, "True")}${button(false, "False")}</div></div>`;
      }).join("")}</div>`;
    }
    return `<section class="flash-study"><div class="flash-prompt"><div class="stem" dir="auto">${esc(q.stem)}</div></div>
      ${res ? `<div class="flash-answer"><b>Answer</b><div dir="auto">${esc(res.answer || "")}</div></div>`
      : `<div class="flash-answer-placeholder"><span class="flash-seal" aria-hidden="true">✦</span><strong>Answer held back</strong>
          <span>Think of your answer, then show it. Written questions aren’t scored.</span></div>`}</section>`;
  }

  function feedback(q) {
    const res = S.results[String(q.n)];
    if (!res) return "";
    const explain = res.explanation ? `<p dir="auto">${esc(res.explanation)}</p>` : "";
    if (q.kind === "desc") return explain ? `<div class="fb info">${explain}</div>` : "";
    const key = q.kind === "tf"
      ? " Key: " + q.options.map((_, j) => L[j] + "=" + (res.key[j] ? "T" : "F")).join(", ") + "."
      : " Correct: " + q.options.map((_, j) => res.key[j] ? L[j] : "").filter(Boolean).join(" and ") + ".";
    const head = res.correct === true ? '<div class="fb ok"><b>✓ Correct.</b>'
      : res.correct === false ? '<div class="fb bad"><b>✗ Not this one.</b>' : '<div class="fb info"><b>Not answered.</b>';
    return head + key + explain + "</div>";
  }

  function controls(q) {
    const res = S.results[String(q.n)], a = S.answers[String(q.n)];
    const out = [];
    if (q.kind === "tf" && !res) {
      const draft = tfDraft[q.n] || {}, sent = (a && a.answers) || [];
      const all = q.options.every((_, j) => j in draft || sent[j] !== undefined);
      const changed = Object.keys(draft).some(j => draft[j] !== sent[j]);
      if (S.reveal === "each") out.push(`<button class="btn pri" data-act="tf-send" data-q="${q.n}" ${all && !busy ? "" : "disabled"}>Check answers</button>`);
      else if (!a || changed) out.push(`<button class="btn pri" data-act="tf-send" data-q="${q.n}" ${all && !busy ? "" : "disabled"}>${a ? "Update my answers" : "Save my answers"}</button>`);
      else out.push('<span class="mute">Saved ✓ You can change them until you submit.</span>');
    }
    if (q.kind === "desc" && !res) out.push(`<button class="btn pri" data-act="show" data-q="${q.n}" ${busy ? "disabled" : ""}>Show answer</button>`);
    return out.join(" ");
  }

  function card(q, total, locked) {
    return `<div class="card quiz-question live-card${q.kind === "desc" ? " flashcard descriptive-question" + (shown(q) ? " is-revealed" : "") : ""}">
      <div class="qh"><div class="quiz-question-context">
        <span class="qn quiz-position" aria-label="Question ${q.n} of ${total}"><span>${String(q.n).padStart(2, "0")}</span><span class="quiz-position-divider">/</span><span class="quiz-position-total">${String(total).padStart(2, "0")}</span></span>
        ${q.kind === "tf" ? '<span class="tag">True / False</span>' : q.kind === "desc" ? '<span class="tag">Written · not scored</span>' : ""}</div></div>
      ${body(q, locked)}
      ${locked ? "" : `<div class="chal-actions">${controls(q)}</div>`}
      ${feedback(q)}
    </div>`;
  }

  function statusOf(q) {
    const res = S.results[String(q.n)];
    if (res) return q.kind === "desc" ? "rev" : res.correct ? "ok" : "bad";
    return String(q.n) in S.answers ? "sel" : "new";
  }

  // ------------------------------------------------------------------ screens
  function intro() {
    const ended = !S.open;
    const scored = +D.scored, count = +D.count;
    return `<div class="row quiz-meta"><a class="btn quiz-back-link" href="${esc(D.back)}">← Back</a>
      <span class="quiz-meta-copy"><span class="quiz-meta-title">🏆 ${esc(S.title)}</span><span class="quiz-meta-stats">${esc(D.group)}</span></span></div>
      <div class="card live-card chal-intro">
        <span class="chal-trophy" aria-hidden="true">🏆</span>
        <h2 dir="auto">${esc(S.title)}</h2>
        <p class="mute">${esc(D.group)} · challenge</p>
        <ul class="chal-facts">
          <li><b>${count}</b> question${count === 1 ? "" : "s"}${scored !== count ? ` · ${scored} scored` : ""}</li>
          <li>${S.reveal === "each" ? "👁 Answers show <b>after each question</b>" : "🔒 Answers show <b>after you submit</b>. Until then you can change them."}</li>
          <li>${ended ? "⏹ <b>This challenge has ended</b>" : `⏳ Ends <b>${esc(endsOn())}</b> · ${esc(left())}`}</li>
        </ul>
        <p class="chal-rules">${ended
          ? "You can still take it for practice. It won’t count on the leaderboard."
          : "Only your <b>first try</b> counts. The clock starts when you press Start, and if two people tie, whoever finished faster ranks higher."}</p>
        <button class="btn pri chal-start" data-act="start">${ended ? "Practise it" : "Start challenge"} →</button>
        ${D.results ? `<a class="exam-link-btn" href="${esc(D.results)}">See everyone’s results</a>` : ""}
      </div>`;
  }

  function quiz() {
    const total = S.questions.length;
    const q = S.questions[pos];
    const done = answered();
    const nav = S.questions.map((x, i) =>
      `<button class="${statusOf(x)} ${i === pos ? "cur" : ""}" data-act="go" data-p="${i}" aria-label="Question ${i + 1}">${i + 1}</button>`).join("");
    const okN = S.questions.filter(x => statusOf(x) === "ok").length, badN = S.questions.filter(x => statusOf(x) === "bad").length;
    const stats = S.reveal === "each" ? `${okN} correct · ${badN} missed · ${total - done} left` : `${done} of ${total} answered`;
    const last = pos === total - 1;
    const submitLabel = `Submit · ${done}/${total}`;
    return `<div class="row quiz-meta"><a class="btn quiz-back-link" href="${esc(D.back)}">← Back</a>
        <span class="quiz-meta-copy"><span class="quiz-meta-title">🏆 ${esc(S.title)}</span>
          <span class="quiz-meta-stats">${stats}${S.ranked && S.ends_at ? ` · ${esc(left())}` : " · practice"}</span></span>
        <button class="btn pri chal-submit-top" data-act="submit">${submitLabel}</button></div>
      <div class="bar quiz-progress">${S.reveal === "each"
        ? `<i class="g" style="width:${okN / total * 100}%"></i><i class="r" style="width:${badN / total * 100}%"></i>`
        : `<i class="g" style="width:${done / total * 100}%"></i>`}</div>
      <div class="nav-wrap quiz-navigation"><div class="nav chal-nav">${nav}</div></div>
      ${error ? `<div class="fb bad" role="alert">${esc(error)}</div>` : ""}
      ${card(q, total, false)}
      <div class="row quiz-controls chal-controls">
        <button class="btn" data-act="go" data-p="${pos - 1}" ${pos === 0 ? "disabled" : ""}>← Prev</button>
        ${last ? `<button class="btn pri" data-act="submit">${submitLabel} →</button>` : `<button class="btn pri" data-act="go" data-p="${pos + 1}">Next →</button>`}
      </div>`;
  }

  function done() {
    const pct = S.total ? Math.round(100 * S.score / S.total) : 0;
    const note = !S.ranked ? "This was a practice try: the challenge had ended, so it isn’t on the leaderboard."
      : S.published ? "The leaderboard is out."
      : D.official === "1" ? `Admins publish the leaderboard after it ends (${esc(endsOn())}).`
      : `The leaderboard comes out when the challenge ends (${esc(endsOn())}).`;
    const ring = `conic-gradient(var(--ok) ${pct * 3.6}deg, color-mix(in srgb, var(--mute) 22%, transparent) 0)`;
    return `<div class="row quiz-meta"><a class="btn quiz-back-link" href="${esc(D.back)}">← Back</a>
        <span class="quiz-meta-copy"><span class="quiz-meta-title">🏆 ${esc(S.title)}</span><span class="quiz-meta-stats">${esc(D.group)}</span></span></div>
      <div class="card live-card chal-done">
        <div class="chal-ring" style="background:${ring}"><span><b>${S.score}</b><small>of ${S.total}</small></span></div>
        <h2>${pct >= 90 ? "Outstanding! 🌟" : pct >= 75 ? "Great work! 💪" : pct >= 50 ? "Good effort 👍" : "Keep going, you’ll get there 🌱"}</h2>
        <p class="chal-stat"><span>${pct}%</span><span>⏱ ${esc(S.time || "")}</span></p>
        <p class="mute">${note}</p>
        <div class="chal-done-actions">
          <button class="btn pri" data-act="review">Review answers</button>
          <a class="btn" href="${esc(D.back)}">${S.published ? "See the leaderboard" : "Back to the group"}</a>
          ${D.results ? `<a class="exam-link-btn" href="${esc(D.results)}">Everyone’s results</a>` : ""}
        </div>
      </div>`;
  }

  function review() {
    const total = S.questions.length;
    return `<div class="row quiz-meta"><button class="btn quiz-back-link" data-act="back-done">← Score</button>
        <span class="quiz-meta-copy"><span class="quiz-meta-title">🏆 ${esc(S.title)}</span><span class="quiz-meta-stats">${S.score} of ${S.total} · review</span></span></div>
      <div class="chal-review">${S.questions.map(q => card(q, total, true)).join("")}</div>`;
  }

  function render() {
    root.innerHTML = view === "intro" ? intro() : view === "quiz" ? quiz() : view === "review" ? review() : done();
  }
  const focusCard = () => {
    const el = root.querySelector(".quiz-question");
    if (el) el.scrollIntoView({behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start"});
  };

  // ------------------------------------------------------------------ actions
  async function send(n, payload) {
    busy = true; error = ""; render();
    const r = await call(D.answer, {pos: n, payload});
    busy = false;
    if (r.ok) {
      if (r.json.result) S.results[String(n)] = r.json.result;
      S.answers[String(n)] = payload || {seen: true};
      delete tfDraft[n];
      render();
      return true;
    }
    if (r.status === 409) { alert(r.json.error || "This challenge has ended."); await load(); return false; }
    error = r.json.error || "Couldn’t save that answer. Check your connection and try again.";
    render();
    return false;
  }

  root.addEventListener("click", async e => {
    const b = e.target.closest("[data-act]");
    if (!b || b.disabled) return;
    const act = b.dataset.act, n = +b.dataset.q;
    if (act === "start") { await load(true); focusCard(); }
    else if (act === "go") { pos = Math.max(0, Math.min(S.questions.length - 1, +b.dataset.p)); error = ""; render(); focusCard(); }
    else if (act === "pick") {
      const ok = await send(n, {selected: +b.dataset.j});
      if (ok && S.reveal === "end" && pos < S.questions.length - 1) setTimeout(() => { pos++; render(); focusCard(); }, 280);
    }
    else if (act === "tf") { (tfDraft[n] = tfDraft[n] || {})[+b.dataset.j] = b.dataset.v === "1"; render(); }
    else if (act === "tf-send") {
      const q = S.questions[n - 1], sent = ((S.answers[String(n)] || {}).answers) || [], draft = tfDraft[n] || {};
      await send(n, {answers: q.options.map((_, j) => j in draft ? draft[j] : sent[j])});
    }
    else if (act === "show") await send(n, null);
    else if (act === "submit") {
      const missing = S.questions.length - answered();
      if (!answered()) { error = "Answer at least one question first."; render(); return; }
      if (!confirm(missing ? `${missing} question${missing === 1 ? " is" : "s are"} unanswered and will count as wrong. Submit anyway?`
                           : "Submit your answers? You can’t change them after this.")) return;
      b.disabled = true;
      const r = await call(D.submit, {});
      if (r.ok) { S = r.json; view = "done"; render(); window.scrollTo({top: 0}); }
      else { error = r.json.error || "Couldn’t submit. Try again."; render(); }
    }
    else if (act === "review") { view = "review"; render(); window.scrollTo({top: 0}); }
    else if (act === "back-done") { view = "done"; render(); window.scrollTo({top: 0}); }
  });

  // Keep the countdown fresh; when time runs out mid-try, the server hands it in.
  setInterval(() => {
    if (!S || !S.ends_at || view !== "quiz" && view !== "intro") return;
    if (S.ranked && !S.finished && new Date(S.ends_at) <= Date.now()) { load(); return; }
    const stats = root.querySelector(".quiz-meta-stats");
    if (stats && view === "quiz") render();
  }, 30000);
  load(false);
})();
