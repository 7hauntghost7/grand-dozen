/* Quiz runner. Questions come from the API WITHOUT answers; each answer is checked
   by the server. Progress is kept in localStorage. Large decks are split into
   blocks of 100 questions, each with its own report. */
(() => {
  const root = document.getElementById("quiz");
  if (!root) return;
  const D = root.dataset;
  const KEY = "gd:" + D.key;
  const AUTH = D.auth === "1";
  const BLOCK_SIZE = 100;
  const esc = s => String(s).replace(/[&<>\"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const csrf = () => (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
  const url = (tpl, id) => tpl.replace("/0/", "/" + id + "/");

  let S = {a: {}, order: null};
  try { S = JSON.parse(localStorage.getItem(KEY)) || S; } catch (e) {}
  if (!S.a) S.a = {};
  if (!S.order) S.order = null;
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) {} };

  let Q = [], byId = {}, list = [], pos = 0, view = "quiz", busy = false, navOpen = true, blockNavOpen = true;
  let resultBlock = 0, showUnanswered = false;
  const tfPending = {};
  const cm = {};
  const notice = {};

  const blockCount = () => Math.ceil(list.length / BLOCK_SIZE);
  const blockStart = b => b * BLOCK_SIZE;
  const blockEnd = b => Math.min(blockStart(b) + BLOCK_SIZE, list.length);
  const currentBlock = () => Math.floor(pos / BLOCK_SIZE);
  const blockList = b => list.slice(blockStart(b), blockEnd(b));

  const status = id => {
    const a = S.a[id];
    if (!a) return "new";
    if (a.revealed) return "rev";
    return a.res.correct ? "ok" : "bad";
  };

  const tally = ids => {
    const t = {ok: 0, bad: 0, rev: 0, new: 0};
    (ids || list).forEach(id => t[status(id)]++);
    return t;
  };

  const blockComplete = b => blockList(b).every(id => status(id) !== "new");

  async function post(u, payload) {
    const r = await fetch(u, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRFToken": csrf()},
      body: JSON.stringify(payload || {}),
    });
    let j = {};
    try { j = await r.json(); } catch (e) {}
    return {status: r.status, json: j};
  }

  async function check(id, payload) {
    if (busy) return;
    busy = true;
    try {
      const r = await post(url(D.check, id), payload);
      S.a[id] = {payload, res: r.json, revealed: !!payload.reveal};
      save();
    } finally { busy = false; render(); }
  }

  async function toggleSave(id) {
    if (!AUTH) { notice[id] = "login"; render(); return; }
    const r = await post(url(D.save, id), {});
    if (r.status === 401) notice[id] = "login";
    else if (r.json && typeof r.json.saved === "boolean") byId[id].saved = r.json.saved;
    render();
  }

  async function sendComment(id) {
    const c = cm[id] || (cm[id] = {});
    const text = (c.text || "").trim();
    if (!text) { c.err = "Please write a comment first."; render(); return; }
    const r = await post(url(D.comment, id), {text});
    if (r.status === 401) notice[id] = "login";
    else if (r.json && r.json.ok) { c.msg = "✓ Sent to the admins — thank you!"; c.text = ""; c.err = ""; }
    else c.err = (r.json && r.json.error) || "Could not send the comment.";
    render();
  }

  function blockNav() {
    if (blockCount() <= 1) return "";
    const activeBlock = view === "results" ? resultBlock : currentBlock();
    const toggle = `<button class="btn nav-toggle" data-act="toggle-block-nav" aria-expanded="${blockNavOpen}" title="${blockNavOpen ? "Hide block list" : "Show block list"}">${blockNavOpen ? "▾" : "▸"}</button>`;
    const summary = `<button class="btn ${activeBlock === currentBlock() ? "pri" : ""}" data-act="block" data-block="${activeBlock}">Block ${activeBlock + 1}</button>`;
    const resetBlock = `<button class="btn" data-act="reset-block" title="Reset this block progress">Reset block</button>`;
    const buttons = Array.from({length: blockCount()}, (_, b) => {
      const ids = blockList(b), t = tally(ids), active = b === activeBlock;
      const done = blockComplete(b);
      return `<button class="btn ${active ? "pri" : ""}" data-act="block" data-block="${b}">
        Block ${b + 1}<span class="mute"> · ${ids.length}</span>${done ? " ✓" : ""}
        <span class="mute"> (${t.ok}/${ids.length})</span></button>`;
    }).join(" ");
    return `<div class="row" style="margin:0 0 10px;justify-content:flex-start;gap:6px;flex-wrap:wrap">
      <span class="mute">Blocks:</span>${toggle}${blockNavOpen ? buttons : summary}${resetBlock}</div>`;
  }

  function render() {
    root.innerHTML = view === "results" ? results() : question();
    if (view === "quiz") {
      const ta = root.querySelector("textarea.cmt");
      if (ta) ta.addEventListener("input", e => { (cm[list[pos]] || (cm[list[pos]] = {})).text = e.target.value; });
    }
  }

  function feedback(q, a) {
    const r = a.res;
    let head;
    if (a.revealed) head = '<div class="fb info"><b>Answer shown.</b>';
    else if (r.correct) head = '<div class="fb ok"><b>✓ Correct.</b>';
    else head = '<div class="fb bad"><b>✗ Not quite.</b>';
    if (q.kind === "tf") {
      head += " Key: " + q.options.map(o => o.letter + "=" + (r.key[o.id] ? "T" : "F")).join(", ") + ".";
    } else {
      head += " Correct: " + q.options.filter(o => r.correct_ids.includes(o.id)).map(o => o.letter).join(" and ") + ".";
    }
    if (r.explanation) head += "<p>" + esc(r.explanation) + "</p>";
    if (r.note) head += '<p class="note">⚠ ' + esc(r.note) + "</p>";
    return head + "</div>";
  }

  function commentBox(id) {
    const c = cm[id] || {};
    const finish = `<button class="btn" data-act="finish" title="See your results for this block now">End block</button>`;
    const btn = `<button class="btn" data-act="cmtoggle">💬 Comment${c.open ? " ▴" : ""}</button>`;
    if (!c.open) return `${finish} ${btn}`;
    let inner;
    if (!AUTH) {
      inner = `<p class="mute"><a href="${esc(D.login)}">Log in</a> to send a comment to the admins.</p>`;
    } else {
      inner = `<textarea class="cmt" rows="3" maxlength="2000" placeholder="Spotted a mistake or something unclear? Tell the admins…">${esc(c.text || "")}</textarea>
        <div class="row" style="margin-top:6px"><span class="mute">Private — only the admins see this.</span>
        <button class="btn pri" data-act="cmsend">Send to admins</button></div>
        ${c.err ? `<div class="fb bad">${esc(c.err)}</div>` : ""}${c.msg ? `<div class="fb ok">${esc(c.msg)}</div>` : ""}`;
    }
    return `${finish} ${btn}<div class="cmtbox">${inner}</div>`;
  }

  function question() {
    const id = list[pos], q = byId[id], a = S.a[id], st = status(id), b = currentBlock();
    const ids = blockList(b), t = tally(ids);
    const localPos = pos - blockStart(b);
    const nav = ids.map((j, i) =>
      `<button class="${status(j)} ${j === id ? "cur" : ""}" data-act="go" data-p="${blockStart(b) + i}">${esc(byId[j].number)}</button>`).join("");
    const navToggle = `<button class="btn nav-toggle" data-act="toggle-nav" aria-expanded="${navOpen}" title="${navOpen ? "Hide question list" : "Show question list"}">${navOpen ? "▾" : "▸"}</button>`;
    let body = "";
    if (q.kind === "mcq") {
      body = q.options.map(o => {
        let cl = "";
        if (a) {
          if (a.res.correct_ids.includes(o.id)) cl = "ok";
          else if (a.payload.selected === o.id) cl = "bad";
        }
        return `<button class="opt ${cl}" ${a ? "disabled" : ""} data-act="pick" data-id="${o.id}"><b>${o.letter}</b><span>${esc(o.text)}</span></button>`;
      }).join("");
    } else {
      const pend = tfPending[id] || {};
      body = q.options.map(o => {
        const chosen = a && !a.revealed ? a.payload.answers[o.id] : pend[o.id];
        const btn = (v, lbl) => {
          let cl = "";
          if (a) {
            if (a.res.key[o.id] === v) cl = "ok";
            else if (!a.revealed && chosen === v) cl = "bad";
          } else if (chosen === v) cl = "sel";
          return `<button class="${cl}" ${a ? "disabled" : ""} data-act="tf" data-id="${o.id}" data-v="${v}">${lbl}</button>`;
        };
        const mark = a && !a.revealed ? (a.res.per[o.id] ? " ✓" : " ✗") : "";
        return `<div class="tf"><div class="t"><b>${o.letter}</b>${esc(o.text)}<span class="mute">${mark}</span></div>
                <div class="tfb">${btn(true, "True")}${btn(false, "False")}</div></div>`;
      }).join("");
    }
    const allSet = q.kind === "tf" && q.options.every(o => (tfPending[id] || {})[o.id] !== undefined);
    const extra = st === "new"
      ? (q.kind === "tf" ? `<button class="btn pri" ${allSet ? "" : "disabled"} data-act="tfcheck">Check answers</button> ` : "") +
        '<button class="btn" data-act="reveal">Show answer</button> ' : "";
    const firstInBlock = localPos === 0;
    const lastInBlock = localPos === ids.length - 1;
    const star = `<button class="star ${q.saved ? "on" : ""}" data-act="save" title="Save this question">${q.saved ? "★ Saved" : "☆ Save"}</button>`;
    const edit = D.edit ? `<a class="btn" href="${esc(url(D.edit, id))}">✎ Edit</a>` : "";
    const loginNote = notice[id] === "login"
      ? `<div class="fb info"><a href="${esc(D.login)}">Log in</a> to save questions and send comments.</div>` : "";
    const finishLabel = lastInBlock ? (b === blockCount() - 1 ? "Finish block" : "Finish block") : "";
    const next = lastInBlock
      ? `<button class="btn pri" data-act="finish">${finishLabel} →</button>`
      : `<button class="btn pri" data-act="go" data-p="${pos + 1}">Next →</button>`;
    const prev = firstInBlock && b > 0
      ? `<button class="btn" data-act="block" data-block="${b - 1}">← Previous block</button>`
      : `<button class="btn" ${pos === 0 ? "disabled" : ""} data-act="go" data-p="${pos - 1}">← Prev</button>`;
    return `${blockNav()}
      <div class="row" style="margin:0 0 6px"><a class="btn" href="${esc(D.back)}">← Back</a>
      <span class="mute">${esc(D.title)} · Block ${b + 1}/${blockCount()} · ${t.ok} ✓ · ${t.bad} ✗ · ${t.new + t.rev} left</span></div>
      <div class="bar"><i class="g" style="width:${t.ok / ids.length * 100}%"></i><i class="r" style="width:${t.bad / ids.length * 100}%"></i></div>
      <div class="nav-wrap">${navToggle}<div class="nav ${navOpen ? "" : "closed"}">${navOpen ? nav : ""}</div></div>
      <div class="card"><div class="qh"><span class="qn">Q${esc(q.number)}</span>
        <span class="mute">${localPos + 1} of ${ids.length}</span>
        ${q.multi ? '<span class="tag">more than one defensible</span>' : ""}
        ${q.kind === "tf" ? '<span class="tag">True / False</span>' : ""}
        <span class="grow"></span>${edit}${star}</div>
        ${loginNote}
        <div class="stem">${esc(q.stem)}</div>${body}${a ? feedback(q, a) : ""}
        <div class="ctl">${prev}<span>${extra}${next}</span></div>
        <div class="cmtwrap">${commentBox(id)}</div></div>`;
  }

  function results() {
    const ids = blockList(resultBlock), t = tally(ids), answered = t.ok + t.bad;
    const wrong = ids.filter(i => status(i) === "bad");
    const left = ids.filter(i => ["new", "rev"].includes(status(i)));
    const pct = ids.length ? Math.round(t.ok / ids.length * 100) : 0;
    const li = (arr, title) => arr.length ? `<h3 style="margin:16px 0 0">${title} (${arr.length})</h3><ul class="w">` +
      arr.map(i => `<li><a href="#" data-act="jump" data-id="${i}">Q${esc(byId[i].number)}</a><span>${esc(byId[i].stem.slice(0, 120))}</span></li>`).join("") + "</ul>" : "";
    const leftList = left.length ? `<div class="row" style="margin-top:12px;justify-content:space-between;gap:8px;align-items:center">
        <h3 style="margin:0">Unanswered or revealed (${left.length})</h3>
        <button class="btn" data-act="toggle-results-list">${showUnanswered ? "Hide unanswered / revealed" : "Show unanswered / revealed"}</button>
      </div>
      ${showUnanswered ? `<ul class="w">` + left.map(i => `<li><a href="#" data-act="jump" data-id="${i}">Q${esc(byId[i].number)}</a><span>${esc(byId[i].stem.slice(0, 120))}</span></li>`).join("") + "</ul>" : ""}` : "";
    const nextBlock = resultBlock < blockCount() - 1
      ? `<button class="btn pri" data-act="block" data-block="${resultBlock + 1}">Next block →</button>` : "";
    return `${blockNav()}<div class="card"><div class="big">${pct}%</div>
      <h2 style="margin:0 0 6px">Block ${resultBlock + 1} report</h2>
      <div class="mute">${t.ok} correct · ${t.bad} wrong · ${left.length} unanswered / revealed</div>
      <div class="bar" style="margin-top:10px"><i class="g" style="width:${t.ok / ids.length * 100}%"></i><i class="r" style="width:${t.bad / ids.length * 100}%"></i></div>
      ${li(wrong, "Missed")}${leftList}
      <div class="ctl"><button class="btn" data-act="back">← Back to block</button>
      <span>${wrong.length + left.length ? '<button class="btn pri" data-act="retry">Retry missed &amp; unanswered</button> ' : ""}
      ${nextBlock} <button class="btn" data-act="reset">Reset progress</button></span></div></div>`;
  }

  root.addEventListener("click", e => {
    const b = e.target.closest("[data-act]");
    if (!b || b.disabled) return;
    e.preventDefault();
    const id = list[pos], d = b.dataset;
    switch (d.act) {
      case "go": pos = +d.p; view = "quiz"; render(); window.scrollTo(0, 0); break;
      case "block": {
        const target = +d.block;
        const ids = blockList(target);
        const first = ids.findIndex(i => status(i) === "new");
        pos = blockStart(target) + (first < 0 ? 0 : first);
        view = "quiz"; render(); window.scrollTo(0, 0); break;
      }
      case "toggle-nav": navOpen = !navOpen; render(); break;
      case "toggle-block-nav": blockNavOpen = !blockNavOpen; render(); break;
      case "toggle-results-list": showUnanswered = !showUnanswered; render(); break;
      case "reset-block": {
        const target = view === "results" ? resultBlock : currentBlock();
        if (confirm("Clear progress for this block?")) {
          blockList(target).forEach(i => { delete S.a[i]; delete tfPending[i]; });
          save();
          pos = blockStart(target);
          view = "quiz";
          render();
        }
        break;
      }
      case "pick": check(id, {selected: +d.id}); break;
      case "tf": (tfPending[id] = tfPending[id] || {})[d.id] = d.v === "true"; render(); break;
      case "tfcheck": check(id, {answers: tfPending[id]}); break;
      case "reveal": check(id, {reveal: true}); break;
      case "save": toggleSave(id); break;
      case "cmtoggle": (cm[id] = cm[id] || {}).open = !(cm[id] || {}).open; if (cm[id].open) cm[id].msg = ""; render(); break;
      case "cmsend": sendComment(id); break;
      case "finish": resultBlock = currentBlock(); view = "results"; render(); window.scrollTo(0, 0); break;
      case "back": view = "quiz"; pos = blockStart(resultBlock) + Math.max(0, blockList(resultBlock).findIndex(i => status(i) === "new")); render(); break;
      case "jump": pos = list.indexOf(+d.id); view = "quiz"; render(); window.scrollTo(0, 0); break;
      case "retry": {
        const ids = blockList(resultBlock);
        ids.filter(i => status(i) !== "ok").forEach(i => { delete S.a[i]; delete tfPending[i]; });
        save();
        pos = blockStart(resultBlock) + 0;
        view = "quiz"; render(); break;
      }
      case "reset":
        if (confirm("Clear your progress for this quiz?")) { S = {a: {}, order: null}; save(); location.reload(); }
        break;
    }
  });

  document.addEventListener("keydown", e => {
    if (view !== "quiz" || !list.length || e.metaKey || e.ctrlKey || e.altKey) return;
    if (/^(TEXTAREA|INPUT|SELECT)$/.test((e.target.tagName || ""))) return;
    const b = currentBlock(), start = blockStart(b), end = blockEnd(b) - 1;
    if (e.key === "ArrowRight" && pos < end) { pos++; render(); }
    else if (e.key === "ArrowLeft" && pos > start) { pos--; render(); }
  });

  fetch(D.api).then(r => r.json()).then(data => {
    Q = data.questions;
    if (!Q.length) { root.innerHTML = '<div class="card">There are no questions here yet.</div>'; return; }
    Q.forEach(q => byId[q.id] = q);
    const ids = Q.map(q => q.id);
    const valid = S.order && S.order.length === ids.length && S.order.every(i => byId[i]);
    if (!valid) {
      S.order = ids.slice();
      if (D.shuffle === "1") for (let i = S.order.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [S.order[i], S.order[j]] = [S.order[j], S.order[i]];
      }
      S.a = {};
      save();
    }
    list = S.order.slice();
    const first = list.findIndex(i => status(i) === "new");
    pos = first < 0 ? 0 : first;
    render();
  }).catch(() => { root.innerHTML = '<div class="fb error">Could not load questions.</div>'; });
})();
