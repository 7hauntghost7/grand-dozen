/* My Space · "Write it myself": one card per question, kept in this browser until it's saved. */
(() => {
  const root = document.getElementById("builder");
  if (!root) return;
  const KEY = root.dataset.key;
  const listEl = root.querySelector("[data-list]");
  const titleEl = root.querySelector("[data-title]");
  const errorsEl = root.querySelector("[data-errors]");
  const saveBtn = root.querySelector("[data-save]");
  const L = "ABCDEFGH";
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const csrf = () => (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
  const blank = kind => kind === "desc"
    ? {kind, stem: "", answer: "", explanation: ""}
    : {kind, stem: "", options: ["", "", "", "", ""], correct: [0, 1, 2, 3, 4].map(() => kind === "tf"), explanation: ""};

  let S = {title: "", items: []};
  const initial = root.dataset.initial && document.getElementById(root.dataset.initial);
  if (initial) {
    S = {title: root.dataset.title || "", items: JSON.parse(initial.textContent).map(q => ({
      kind: q.kind, stem: q.stem || "", explanation: q.explanation || "", answer: q.answer || "",
      ...(q.kind === "desc" ? {} : {options: (q.options || []).slice(), correct: (q.correct || []).map(Boolean)}),
    }))};
  } else if (KEY) {
    try { S = JSON.parse(localStorage.getItem(KEY)) || S; } catch (e) {}
  }
  const LOCKED = root.dataset.locked === "1";      // taken as a challenge: no adding, removing or moving
  if (!Array.isArray(S.items) || !S.items.length) S.items = [blank("mcq")];
  const filled = q => q.stem.trim() || (q.options || []).some(o => o.trim()) || (q.answer || "").trim();
  const count = () => {
    const n = S.items.filter(filled).length;
    root.querySelector("[data-count]").textContent = `${n} question${n === 1 ? "" : "s"}`;
  };
  let dirty = false;
  const keep = () => { dirty = true; if (KEY) { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) {} } count(); };
  window.addEventListener("beforeunload", e => { if (!KEY && dirty) { e.preventDefault(); e.returnValue = ""; } });
  titleEl.value = S.title || "";
  titleEl.addEventListener("input", () => { S.title = titleEl.value; keep(); });

  function card(q, i) {
    const kinds = [["mcq", "MCQ"], ["tf", "True / False"], ["desc", "Written"]].map(([k, label]) =>
      `<button type="button" class="mb-kind${q.kind === k ? " is-on" : ""}" data-kind="${k}" aria-pressed="${q.kind === k}">${label}</button>`).join("");
    let body;
    if (q.kind === "mcq") {
      body = `<span class="mb-label">Options <small>tap a letter to mark the correct answer</small></span>
        ${q.options.map((o, j) => `<div class="mb-opt${q.correct[j] ? " is-correct" : ""}">
          <button type="button" class="mb-letter" data-correct="${j}" aria-pressed="${!!q.correct[j]}" aria-label="Mark ${L[j]} as the correct answer">${q.correct[j] ? "✓" : L[j]}</button>
          <input type="text" dir="auto" maxlength="1000" data-opt="${j}" value="${esc(o)}" placeholder="Option ${L[j]}${j > 1 ? " (optional)" : ""}" aria-label="Option ${L[j]}"></div>`).join("")}
        ${q.options.length < 8 && !LOCKED ? '<button type="button" class="exam-link-btn mb-more" data-more>＋ Another option</button>' : ""}`;
    } else if (q.kind === "tf") {
      body = `<span class="mb-label">Statements <small>set each one True or False</small></span>
        ${q.options.map((o, j) => `<div class="mb-opt mb-opt-tf">
          <span class="mb-letter is-plain" aria-hidden="true">${L[j]}</span>
          <input type="text" dir="auto" maxlength="1000" data-opt="${j}" value="${esc(o)}" placeholder="Statement ${L[j]}" aria-label="Statement ${L[j]}">
          <span class="mb-tf" role="group" aria-label="Statement ${L[j]} is">
            <button type="button" class="${q.correct[j] ? "is-t" : ""}" data-tf="${j}" data-v="1" aria-pressed="${!!q.correct[j]}">T</button><button type="button" class="${q.correct[j] ? "" : "is-f"}" data-tf="${j}" data-v="0" aria-pressed="${!q.correct[j]}">F</button></span></div>`).join("")}
        ${q.options.length < 8 && !LOCKED ? '<button type="button" class="exam-link-btn mb-more" data-more>＋ Another statement</button>' : ""}`;
    } else {
      body = `<label class="mb-field"><span class="mb-label">Answer</span>
        <textarea rows="3" dir="auto" maxlength="4000" data-f="answer" placeholder="The model answer">${esc(q.answer)}</textarea></label>`;
    }
    return `<article class="mb-card" data-i="${i}">
      <header class="mb-head"><span class="mb-num">Q${i + 1}</span>
        ${LOCKED ? `<span class="tag">${{mcq: "MCQ", tf: "True / False", desc: "Written"}[q.kind]}</span>` : `<span class="mb-kinds" role="group" aria-label="Question type">${kinds}</span>
        <span class="mb-tools">${i > 0 ? '<button type="button" class="mb-icon" data-move aria-label="Move up">↑</button>' : ""}
          <button type="button" class="mb-icon" data-dup aria-label="Duplicate question">⧉</button>
          <button type="button" class="mb-icon is-danger" data-remove aria-label="Delete question">✕</button></span>`}</header>
      <label class="mb-field"><span class="mb-label">Question</span>
        <textarea rows="2" dir="auto" maxlength="4000" data-f="stem" placeholder="Type the question">${esc(q.stem)}</textarea></label>
      ${body}
      <label class="mb-field"><span class="mb-label">Explanation <small>optional</small></span>
        <textarea rows="2" dir="auto" maxlength="4000" data-f="explanation" placeholder="Why the answer is right">${esc(q.explanation)}</textarea></label>
    </article>`;
  }
  const render = () => { listEl.innerHTML = S.items.map(card).join(""); count(); };

  listEl.addEventListener("input", e => {
    const c = e.target.closest("[data-i]");
    if (!c) return;
    const q = S.items[+c.dataset.i];
    if (e.target.dataset.f) q[e.target.dataset.f] = e.target.value;
    if (e.target.dataset.opt !== undefined) q.options[+e.target.dataset.opt] = e.target.value;
    keep();
  });
  listEl.addEventListener("click", e => {
    const c = e.target.closest("[data-i]"), b = e.target.closest("button");
    if (!c || !b) return;
    const i = +c.dataset.i, q = S.items[i];
    if (b.dataset.kind) {
      if (b.dataset.kind === q.kind) return;
      const next = blank(b.dataset.kind);
      next.stem = q.stem;
      next.explanation = q.explanation;
      if (q.options && next.options) {
        next.options = q.options.slice();
        next.correct = next.options.map(() => next.kind === "tf");
      }
      S.items[i] = next;
    } else if (b.dataset.correct !== undefined) {
      q.correct = q.options.map((_, j) => j === +b.dataset.correct);
    } else if (b.dataset.tf !== undefined) {
      q.correct[+b.dataset.tf] = b.dataset.v === "1";
    } else if (b.hasAttribute("data-more")) {
      q.options.push("");
      q.correct.push(q.kind === "tf");
    } else if (b.hasAttribute("data-remove")) {
      if (filled(q) && !confirm("Delete this question?")) return;
      S.items.splice(i, 1);
      if (!S.items.length) S.items.push(blank("mcq"));
    } else if (b.hasAttribute("data-dup")) {
      S.items.splice(i + 1, 0, JSON.parse(JSON.stringify(q)));
    } else if (b.hasAttribute("data-move")) {
      const [moved] = S.items.splice(i, 1);
      S.items.splice(i - 1, 0, moved);
    } else return;
    keep();
    render();
  });
  root.querySelectorAll("[data-add]").forEach(b => b.addEventListener("click", () => {
    S.items.push(blank(b.dataset.add));
    keep();
    render();
    const last = listEl.lastElementChild;
    last?.scrollIntoView({behavior: "smooth", block: "center"});
    last?.querySelector("textarea")?.focus({preventScroll: true});
  }));
  root.querySelector("[data-clear]")?.addEventListener("click", () => {
    if (!confirm("Clear every question you wrote here?")) return;
    S = {title: "", items: [blank("mcq")]};
    titleEl.value = "";
    keep();
    render();
  });
  const showErrors = html => {
    errorsEl.hidden = false;
    errorsEl.innerHTML = html;
    errorsEl.scrollIntoView({behavior: "smooth", block: "center"});
  };
  saveBtn.addEventListener("click", async () => {
    const items = S.items.filter(filled);
    errorsEl.hidden = true;
    if (!items.length) return showErrors("<p>Write at least one question first.</p>");
    saveBtn.disabled = true;
    try {
      const r = await fetch(root.dataset.api, {
        method: "POST", headers: {"Content-Type": "application/json", "X-CSRFToken": csrf()},
        body: JSON.stringify({title: S.title, questions: items}),
      });
      const j = await r.json().catch(() => ({}));
      if (r.ok && j.url) {
        dirty = false;
        if (KEY) { try { localStorage.removeItem(KEY); } catch (e) {} }
        location.href = j.url;
        return;
      }
      showErrors(`<p><b>Fix these first:</b></p><ul class="myspace-problems">${(j.errors || ["Couldn't save. Try again."])
        .map(x => `<li>${esc(x)}</li>`).join("")}</ul>`);
    } catch (e) {
      showErrors("<p>No connection. Your questions are kept here; try again.</p>");
    }
    saveBtn.disabled = false;
  });
  render();
})();
