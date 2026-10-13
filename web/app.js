// Live session screen for Bee Call. Plain JS, no build step.
// Routes: #/ follows the newest call, #/calls lists all calls, #/s/<date>/<time> opens one call.
// A call is rendered from its events.jsonl lines, which arrive over Server-Sent Events.
(() => {
  "use strict";

  const API = "api/sessions";
  const LIST_POLL_MS = 5000;
  const STALE_S = 120; // no new line for this long and no end marker: the call is not live (server uses the same)
  const IMAGE_RE = /\.(png|jpe?g|gif|webp|svg)$/i;
  const TEXT_RE = /\.(md|txt|log|csv|tsv|json|jsonl|html?|xml|ya?ml|toml|py|js|ts|sh|sql)$/i; // shown as text
  const STATE_TEXT = {
    initializing: "Getting ready", idle: "Idle", listening: "Listening", thinking: "Thinking", speaking: "Speaking",
  };
  const BEE_KIND = {
    fact_confirm: "Confirm fact", fact_update: "Update fact", todo_create: "New to-do", todo_accept: "Accept to-do",
  };

  const $ = (id) => document.getElementById(id);
  const view = $("view");
  const ui = {
    back: $("back"), title: $("title"), status: $("status"),
    statusText: $("status-text"), statusTime: $("status-time"), jump: $("jump"),
  };

  // ---------- small helpers ----------

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function icon(name) {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(ns, "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  }

  function clock(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = String(s % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${r}` : `${m}:${r}`;
  }

  function parseDate(value) {
    if (typeof value !== "string" || !value) return null;
    const d = new Date(value); // "2026-10-11T19:30:04" (no zone) is read as this device's local time
    return isNaN(d) ? null : d;
  }

  function dateFromId(id) {
    const m = /^(\d{4})-(\d{2})-(\d{2})\/(\d{2})(\d{2})/.exec(id);
    return m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) : null;
  }

  function hhmm(date) {
    return date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  }

  function dayLabel(date) {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const day = new Date(date);
    day.setHours(0, 0, 0, 0);
    const diff = Math.round((today - day) / 86400000);
    if (diff === 0) return "Today";
    if (diff === 1) return "Yesterday";
    const sameYear = day.getFullYear() === today.getFullYear();
    return date.toLocaleDateString("en-US", {
      weekday: "short", month: "short", day: "numeric", year: sameYear ? undefined : "numeric",
    });
  }

  function titleFor(id, startedAt) {
    const start = startedAt || dateFromId(id);
    return start ? `${dayLabel(start)} · ${hhmm(start)}` : "Call";
  }

  function humanize(value) {
    const s = String(value || "").replace(/_/g, " ").trim();
    return s ? s[0].toUpperCase() + s.slice(1) : "";
  }

  // One of the AI's extra Bee reads before a call, recorded as "tool {json arguments}".
  function readLabel(read) {
    if (typeof read !== "string" || !read.trim()) return null;
    const space = read.indexOf(" ");
    const name = space < 0 ? read : read.slice(0, space);
    let args = {};
    try { args = JSON.parse(read.slice(space + 1)) || {}; } catch (e) { /* no arguments to show */ }
    if (name === "search_bee") return `Searched for “${args.query || "…"}”`;
    if (name === "bee_conversation") return `Read a conversation in full (#${args.conversation_id ?? "?"})`;
    if (name === "bee_day") return `Read the day summary for ${args.date || "a day"}`;
    return humanize(name);
  }

  function speakerName(speaker) {
    if (!speaker) return "Someone";
    if (speaker === "me") return "You";
    const m = /^speaker_(\d+)$/.exec(speaker);
    return m ? `Speaker ${m[1]}` : String(speaker);
  }

  function atLabel(at) {
    if (typeof at !== "string" || !at) return "";
    if (/^\d{1,2}:\d{2}$/.test(at)) return at;
    const d = parseDate(at);
    return d ? hhmm(d) : at;
  }

  function closeReason(reason) {
    const s = String(reason || "").replace(/^CloseReason\./, "");
    return s ? s.replace(/_/g, " ").toLowerCase() : "";
  }

  function fileUrl(sessionId, path) {
    return `${API}/${sessionId}/files/${String(path).split("/").map(encodeURIComponent).join("/")}`;
  }

  function nearBottom() {
    const doc = document.documentElement;
    return window.innerHeight + window.scrollY >= doc.scrollHeight - 120;
  }

  function scrollToBottom(smooth) {
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  }

  function readPref(key) {
    try { return localStorage.getItem(`beecall.${key}`); } catch { return null; }
  }

  function writePref(key, value) {
    try { localStorage.setItem(`beecall.${key}`, value); } catch { /* private mode: the setting just is not kept */ }
  }

  async function fetchSessions() {
    try {
      const response = await fetch(API, { cache: "no-store" });
      if (!response.ok) return null;
      const data = await response.json();
      return Array.isArray(data) ? data : null;
    } catch {
      return null;
    }
  }

  function setHeader(title, back) {
    ui.title.textContent = title;
    ui.back.hidden = !back;
    document.title = title === "Bee Call" ? "Bee Call" : `${title} · Bee Call`;
  }

  function setStatus(status) {
    if (!status) {
      ui.status.hidden = true;
      return;
    }
    ui.status.hidden = false;
    ui.status.dataset.state = status.state;
    if (ui.statusText.textContent !== status.text) ui.statusText.textContent = status.text;
    ui.statusTime.textContent = status.time ? `· ${status.time}` : "";
  }

  function showNote(text) {
    view.replaceChildren(el("p", "empty", text));
  }

  const UNREACHABLE = "Can't reach the session server on the Mac. Retrying…";
  const NO_CALLS = "No calls yet. When the AI calls you, the call shows up here.";

  // ---------- file viewer (keeps the iPhone web view on this page) ----------

  const viewer = {
    dialog: $("viewer"),
    open(name, url, node) {
      $("viewer-title").textContent = name;
      $("viewer-raw").href = url;
      $("viewer-body").replaceChildren(node);
      if (typeof this.dialog.showModal === "function") this.dialog.showModal();
      else window.open(url, "_blank");
    },
    image(url, name) {
      const img = el("img");
      img.src = url;
      img.alt = name;
      this.open(name, url, img);
    },
    async text(url, name) {
      const pre = el("pre", "viewer-text", "Loading…");
      this.open(name, url, pre);
      try {
        const response = await fetch(url, { cache: "no-store" });
        pre.textContent = response.ok ? await response.text() : `Could not open this file (HTTP ${response.status}).`;
      } catch {
        pre.textContent = "Could not open this file.";
      }
    },
  };
  $("viewer-close").addEventListener("click", () => viewer.dialog.close());
  viewer.dialog.addEventListener("click", (e) => { if (e.target === viewer.dialog) viewer.dialog.close(); });
  ui.jump.addEventListener("click", () => { ui.jump.hidden = true; scrollToBottom(true); });

  // ---------- list of calls ----------

  class ListPage {
    constructor() {
      setHeader("Calls", false);
      setStatus(null);
      this.shown = "";
      this.load();
      this.timer = setInterval(() => this.load(), LIST_POLL_MS);
    }

    async load() {
      const sessions = await fetchSessions();
      if (this.closed) return;
      const key = JSON.stringify(sessions);
      if (key === this.shown) return;
      this.shown = key;
      if (sessions === null) showNote(UNREACHABLE);
      else if (!sessions.length) showNote(NO_CALLS);
      else this.render(sessions);
    }

    render(sessions) {
      const frag = document.createDocumentFragment();
      let group = null;
      let label = null;
      for (const s of sessions) {
        const start = parseDate(s.started_at) || dateFromId(s.id) || new Date();
        const day = dayLabel(start);
        if (day !== label) {
          label = day;
          group = el("ul", "calls");
          frag.append(el("h2", "day", day), group);
        }
        const row = el("a", "call-row");
        row.href = `#/s/${s.id}`;
        const preview = s.preview || (s.live ? "Call in progress" : "No conversation");
        const side = el("span", "call-side");
        if (s.live) side.append(el("span", "pill live", "Live"));
        else side.textContent = clock(s.duration);
        row.append(el("span", "call-time", hhmm(start)), el("span", `call-preview${s.preview ? "" : " faint"}`, preview), side);
        const item = el("li");
        item.append(row);
        group.append(item);
      }
      view.replaceChildren(frag);
    }

    close() {
      this.closed = true;
      clearInterval(this.timer);
    }
  }

  // ---------- following the newest call (the default page) ----------

  class FollowPage {
    constructor() {
      this.session = null;
      setHeader("Bee Call", true);
      setStatus(null);
      showNote("Looking for the latest call…");
      this.tick();
      this.timer = setInterval(() => this.tick(), LIST_POLL_MS);
    }

    async tick() {
      const sessions = await fetchSessions();
      if (this.closed) return;
      if (sessions === null || !sessions.length) {
        if (!this.session) showNote(sessions === null ? UNREACHABLE : NO_CALLS);
        return;
      }
      if (this.session && this.session.id === sessions[0].id) return;
      if (this.session) this.session.close();
      this.session = new SessionPage(sessions[0].id);
    }

    close() {
      this.closed = true;
      clearInterval(this.timer);
      if (this.session) this.session.close();
    }
  }

  // ---------- one call ----------

  class SessionPage {
    constructor(id) {
      this.id = id;
      this.debug = readPref("debug") === "1";
      this.events = [];
      this.ready = false; // the backlog has arrived
      this.reconnecting = false;
      this.lastAt = performance.now(); // when the newest line was written, on this device's clock
      setHeader(titleFor(id, null), true);
      this.build();
      this.reset();
      window.scrollTo(0, 0);
      this.connect();
      this.ticker = setInterval(() => this.renderStatus(), 1000);
      this.onScroll = () => { if (nearBottom()) ui.jump.hidden = true; };
      window.addEventListener("scroll", this.onScroll, { passive: true });
      this.renderStatus();
    }

    build() {
      this.root = el("section", "session");
      this.whySlot = el("div", "why-slot");
      this.prepSlot = el("div", "prep-slot"); // what the AI looked up in Bee before it picked up
      this.timeline = el("ol", "timeline");
      this.pendingEl = el("li", "turn user pending");
      this.dock = el("ol", "timeline dock"); // running jobs stay at the bottom until they finish
      this.beeSlot = el("div", "bee-slot");
      const foot = el("footer", "session-foot");
      this.footNote = el("span", "foot-note");
      this.debugButton = el("button", "link-button");
      this.debugButton.type = "button";
      this.debugButton.addEventListener("click", () => this.toggleDebug());
      foot.append(this.footNote, this.debugButton);
      this.root.append(this.whySlot, this.prepSlot, this.timeline, this.dock, this.beeSlot, foot);
      view.replaceChildren(this.root);
    }

    reset() {
      this.whySlot.replaceChildren();
      this.prepSlot.replaceChildren();
      this.direction = null;
      this.pickedUp = null;
      this.prepEvent = null;
      this.pendingEl.replaceChildren();
      this.pendingEl.hidden = true;
      this.timeline.replaceChildren(this.pendingEl); // the unconfirmed user line always sits last
      this.dock.replaceChildren();
      this.beeSlot.replaceChildren();
      this.jobs = new Map();
      this.bee = null;
      this.pending = [];
      this.agentState = null;
      this.endT = null;
      this.callEnded = false;
      this.endShown = false;
      this.lastT = 0;
      this.debugButton.textContent = this.debug ? "Hide debug events" : "Show debug events";
    }

    connect() {
      this.source = new EventSource(`${API}/${this.id}/events`);
      this.source.onmessage = (e) => this.onLine(e.data);
      this.source.addEventListener("ready", (e) => this.onReady(e.data));
      this.source.onerror = () => this.onError();
    }

    onLine(data) {
      let ev;
      try { ev = JSON.parse(data); } catch { return; }
      if (!ev || typeof ev !== "object" || Array.isArray(ev)) return;
      this.events.push(ev);
      this.reconnecting = false;
      this.lastAt = performance.now();
      if (!this.ready) {
        this.apply(ev); // backlog: no scrolling work per line
        return;
      }
      const stick = nearBottom();
      const before = document.documentElement.scrollHeight;
      this.apply(ev);
      if (stick) scrollToBottom(false);
      else if (document.documentElement.scrollHeight > before) ui.jump.hidden = false;
      this.renderStatus();
    }

    onReady(data) {
      let info = {};
      try { info = JSON.parse(data) || {}; } catch { /* keep defaults */ }
      const idle = Number(info.idle_s) || 0;
      this.lastAt = performance.now() - idle * 1000;
      this.reconnecting = false;
      if (!this.ready) {
        this.ready = true;
        if (this.endT === null && idle < STALE_S) scrollToBottom(false); // live: open at the newest turn
      }
      this.renderStatus();
    }

    onError() {
      if (this.closed) return;
      if (this.callEnded || this.source.readyState === EventSource.CLOSED) {
        this.source.close(); // the stream ends at call_ended; do not reconnect
        if (!this.events.length) {
          setStatus(null);
          view.replaceChildren(el("p", "empty", "This call could not be loaded."));
        }
        return;
      }
      this.reconnecting = true; // EventSource retries and resumes after the last line it got
      this.renderStatus();
    }

    renderStatus() {
      if (this.closed) return;
      const now = performance.now();
      const idle = (now - this.lastAt) / 1000;
      if (this.endT !== null) {
        setStatus({ state: "ended", text: "Call ended", time: clock(this.endT) });
      } else if (!this.ready && !this.events.length) {
        setStatus({ state: "wait", text: this.reconnecting ? "Reconnecting…" : "Loading…" });
      } else if (this.ready && idle > STALE_S) {
        setStatus({ state: "ended", text: "Not live", time: clock(this.lastT) });
      } else if (this.reconnecting) {
        setStatus({ state: "wait", text: "Reconnecting…", time: clock(this.lastT + idle) });
      } else {
        const state = this.agentState || "initializing";
        setStatus({ state, text: STATE_TEXT[state] || humanize(state), time: clock(this.lastT + idle) });
      }
    }

    toggleDebug() {
      this.debug = !this.debug;
      writePref("debug", this.debug ? "1" : "0");
      const before = this.debugButton.getBoundingClientRect().top;
      this.reset();
      for (const ev of this.events) this.apply(ev);
      window.scrollBy(0, this.debugButton.getBoundingClientRect().top - before); // keep the button in place
      this.renderStatus();
    }

    close() {
      this.closed = true;
      if (this.source) this.source.close();
      clearInterval(this.ticker);
      window.removeEventListener("scroll", this.onScroll);
      ui.jump.hidden = true;
      setStatus(null);
    }

    // ---------- events -> page ----------

    apply(ev) {
      const t = typeof ev.t === "number" ? ev.t : this.lastT;
      this.lastT = Math.max(this.lastT, t);
      switch (ev.type) {
        case "call_started": {
          const start = parseDate(ev.started_at);
          setHeader(titleFor(this.id, start), true);
          this.addSystem("Call started", start ? hhmm(start) : "");
          this.footNote.textContent = ev.room ? `Room ${ev.room}` : "";
          break;
        }
        case "why_calling": this.renderWhy(ev); break;
        case "call":
          this.direction = ev.direction;
          this.addDebug(ev, t);
          break;
        case "picked_up": // when the AI's first words played (a call the user started rang until then)
          this.pickedUp = ev;
          if (this.prepEvent) this.renderPrep(this.prepEvent);
          this.addDebug(ev, t);
          break;
        case "call_prep":
          this.renderPrep(ev);
          this.addDebug(ev, t);
          break;
        case "agent_state":
          if (typeof ev.new === "string") this.agentState = ev.new;
          this.addDebug(ev, t, `agent ${ev.old} → ${ev.new}`);
          break;
        case "user_transcript": this.addPending(ev.text); break;
        case "message":
          if (ev.role === "user" || ev.role === "assistant") this.addMessage(ev, t);
          else this.addDebug(ev, t);
          break;
        case "job_started": case "job_progress": case "job_done": case "job_failed": case "relay":
          this.updateJob(ev, t);
          break;
        case "bee_write_queued": case "bee_write_done": this.updateBee(ev); break;
        case "error": this.addError(ev, t); break;
        case "session_closed": case "call_ended": this.end(ev, t); break;
        case "screen_signal": break; // meant for the AI, nothing to show
        default: this.addDebug(ev, t); // metrics, item, user_state and anything this page does not know
      }
    }

    add(node) {
      this.timeline.insertBefore(node, this.pendingEl);
    }

    addSystem(text, detail) {
      const row = el("li", "row system");
      row.append(icon("phone"), el("span", null, detail ? `${text} · ${detail}` : text));
      this.add(row);
    }

    addError(ev, t) {
      const row = el("li", "row error");
      const source = ev.source && ev.source !== "NoneType" ? `${humanize(ev.source)}: ` : "";
      const text = el("span", "row-text", `${source}${ev.error || "something went wrong"}`);
      text.title = "Tap to show the whole error";
      text.addEventListener("click", () => text.classList.toggle("open")); // long errors start clamped
      row.append(icon("alert"), text, el("span", "row-time", clock(t)));
      this.add(row);
    }

    addDebug(ev, t, label) {
      if (!this.debug) return;
      const row = el("li", "row debug");
      const details = el("details");
      const summary = el("summary");
      summary.append(el("span", "row-time", clock(t)), ` ${label || debugLabel(ev)}`);
      details.append(summary, el("pre", "json", JSON.stringify(ev, null, 2)));
      row.append(details);
      this.add(row);
    }

    addMessage(ev, t) {
      const user = ev.role === "user";
      if (user) this.clearPending();
      const turn = el("li", `turn ${user ? "user" : "ai"}${ev.interrupted ? " interrupted" : ""}`);
      const body = el("div", user ? "bubble" : "speech");
      body.append(el("span", "sr-only", user ? "You said: " : "AI said: "));
      const text = typeof ev.text === "string" ? ev.text.trim() : "";
      body.append(text || "…");
      if (ev.interrupted) body.append(" ", el("span", "cut", "interrupted"));
      turn.append(body, el("div", "meta", clock(t)));
      if (this.debug && ev.metrics) {
        const details = el("details", "msg-metrics");
        const raw = typeof ev.metrics === "string" ? ev.metrics : JSON.stringify(ev.metrics, null, 2);
        details.append(el("summary", null, "metrics"), el("pre", "json", raw));
        turn.append(details);
      }
      this.add(turn);
    }

    // user_transcript is final speech that the matching user `message` confirms a moment later.
    addPending(text) {
      if (typeof text !== "string" || !text.trim()) return;
      this.pending.push(text.trim());
      const bubble = el("div", "bubble");
      bubble.append(el("span", "sr-only", "You are saying: "), this.pending.join(" "));
      this.pendingEl.replaceChildren(bubble);
      this.pendingEl.hidden = false;
    }

    clearPending() {
      this.pending = [];
      this.pendingEl.replaceChildren();
      this.pendingEl.hidden = true;
    }

    end(ev, t) {
      if (this.pending.length) this.addMessage({ role: "user", text: this.pending.join(" ") }, t);
      this.clearPending();
      if (this.endT === null) this.endT = t;
      if (ev.type === "call_ended") this.callEnded = true;
      if (!this.endShown) {
        this.endShown = true;
        const reason = ev.type === "session_closed" ? closeReason(ev.reason) : "";
        this.addSystem("Call ended", reason ? `${clock(t)}, ${reason}` : clock(t));
      }
    }

    renderWhy(ev) {
      const card = el("section", "card why");
      const head = el("div", "card-head");
      head.append(icon("chat"), el("span", null, "Why I'm calling"));
      card.append(head);
      if (typeof ev.summary === "string" && ev.summary.trim()) card.append(el("p", "why-summary", ev.summary.trim()));
      const utterances = Array.isArray(ev.utterances) ? ev.utterances.filter((u) => u && u.text) : [];
      if (utterances.length) {
        const list = el("ul", "quotes");
        for (const u of utterances) {
          const quote = el("li", "quote");
          const who = el("div", "quote-who");
          who.append(el("span", "who", speakerName(u.speaker)));
          const at = atLabel(u.at);
          if (at) who.append(el("span", "at", at));
          quote.append(who, el("p", null, u.text));
          list.append(quote);
        }
        card.append(el("p", "quotes-label", "From your Bee conversation"), list);
      }
      const facts = Array.isArray(ev.pending_fact_ids) ? ev.pending_fact_ids.length : 0;
      if (facts) {
        card.append(el("p", "card-note", `${facts} unconfirmed ${facts === 1 ? "fact" : "facts"} in Bee to check with you`));
      }
      this.whySlot.replaceChildren(card);
    }

    // The note the AI wrote from Bee before it picked up (voice/prep.py), and the extra reads it chose.
    renderPrep(ev) {
      this.prepEvent = ev;
      const card = el("section", "card prep");
      const head = el("div", "card-head");
      head.append(icon("search"), el("span", null, "Before picking up"));
      card.append(head);
      const note = typeof ev.note === "string" ? ev.note.trim() : "";
      if (note) {
        card.append(el("p", "prep-note", note));
      } else {
        const why = ev.error ? ` (${ev.error})` : "";
        card.append(el("p", "card-note", `No prep note${why}, so the call started with the plain Bee context.`));
      }
      const reads = Array.isArray(ev.reads) ? ev.reads.map(readLabel).filter(Boolean) : [];
      if (reads.length) {
        const list = el("ul", "prep-reads");
        for (const read of reads) list.append(el("li", null, read));
        card.append(el("p", "quotes-label", "Looked up in Bee"), list);
      }
      card.append(el("p", "card-note", this.prepWhen(ev)));
      this.prepSlot.replaceChildren(card);
    }

    prepWhen(ev) {
      const seconds = (n) => `${Number(n).toFixed(1)} s`;
      if (this.direction === "outbound") {
        return typeof ev.seconds === "number" ? `Prepared before your phone rang, in ${seconds(ev.seconds)}`
          : "Prepared before your phone rang";
      }
      const after = this.pickedUp && this.pickedUp.after_s;
      return typeof after === "number" ? `Read while your phone rang · picked up after ${seconds(after)}`
        : "Read while your phone rang";
    }

    // ---------- background jobs ----------

    job(id) {
      let job = this.jobs.get(id);
      if (job) return job;
      const card = el("li", "card job running");
      const head = el("div", "card-head");
      const pill = el("span", "pill running");
      pill.append(el("span", "spinner"), "Running");
      head.append(icon("bolt"), el("span", null, `Background job ${id}`), pill);
      const title = el("p", "card-title", "Working on it");
      title.title = "Tap to show the whole task";
      title.addEventListener("click", () => title.classList.toggle("open"));
      job = {
        el: card, pill, start: null, title,
        steps: el("ol", "steps"),
        relay: el("blockquote", "relay"),
        summary: el("p", "job-summary"),
        files: el("div", "files"),
        error: el("p", "job-error"),
      };
      for (const part of [job.steps, job.relay, job.summary, job.files, job.error]) part.hidden = true;
      card.append(head, job.title, job.steps, job.relay, job.summary, job.files, job.error);
      this.jobs.set(id, job);
      this.dock.append(card);
      return job;
    }

    updateJob(ev, t) {
      const job = this.job(ev.job_id !== undefined && ev.job_id !== null ? String(ev.job_id) : "?");
      if (ev.type === "job_started") {
        job.start = t;
        if (ev.task) job.title.textContent = String(ev.task);
      } else if (ev.type === "job_progress") {
        for (const step of job.steps.children) step.classList.add("finished");
        const step = el("li", "step");
        const mark = el("span", "step-mark");
        mark.append(icon("check"));
        step.append(mark, el("span", "step-text", ev.text || ""), el("span", "step-time", clock(t)));
        job.steps.append(step);
        job.steps.hidden = false;
      } else if (ev.type === "relay") {
        const label = el("span", "relay-label");
        label.append(icon("speaker"), "Message for you");
        job.relay.replaceChildren(label, String(ev.text || ""));
        job.relay.hidden = false;
      } else if (ev.type === "job_done") {
        this.finishJob(job, "done", "Done", t);
        if (ev.summary) {
          job.summary.textContent = String(ev.summary);
          job.summary.hidden = false;
        }
        this.renderFiles(job, Array.isArray(ev.files) ? ev.files : []);
      } else if (ev.type === "job_failed") {
        const cancelled = ev.error === "cancelled";
        this.finishJob(job, "failed", cancelled ? "Cancelled" : "Failed", t);
        if (!cancelled) {
          job.error.textContent = String(ev.error || "The job failed.");
          job.error.hidden = false;
        }
      }
    }

    finishJob(job, state, label, t) {
      job.el.classList.remove("running");
      job.el.classList.add(state);
      job.pill.className = `pill ${state}`;
      job.pill.replaceChildren(icon(state === "done" ? "check" : "x"), label);
      if (job.start !== null) job.pill.append(el("span", "pill-time", clock(t - job.start)));
      for (const step of job.steps.children) step.classList.add("finished");
      this.add(job.el); // a finished job settles into the timeline where it finished
    }

    renderFiles(job, files) {
      const chips = el("div", "chips");
      job.files.replaceChildren();
      for (const path of files) {
        if (typeof path !== "string" || !path) continue;
        const url = fileUrl(this.id, path);
        const name = path.split("/").pop();
        if (IMAGE_RE.test(name)) {
          const link = el("a", "thumb");
          link.href = url;
          const img = el("img");
          img.src = url;
          img.alt = name;
          img.loading = "lazy";
          link.append(img);
          link.addEventListener("click", (e) => { e.preventDefault(); viewer.image(url, name); });
          job.files.append(link);
        } else {
          const link = el("a", "file-chip");
          link.href = url;
          link.append(icon("file"), el("span", null, name));
          link.addEventListener("click", (e) => { // a plain link would replace the page inside the iPhone app
            e.preventDefault();
            if (TEXT_RE.test(name)) viewer.text(url, name);
            else viewer.open(name, url, el("p", "viewer-note", "No preview for this file type. Raw opens the file itself."));
          });
          chips.append(link);
        }
      }
      if (chips.children.length) job.files.append(chips);
      job.files.hidden = !job.files.children.length;
    }

    // ---------- things to save in Bee ----------

    updateBee(ev) {
      if (!this.bee) {
        const card = el("section", "card bee");
        const head = el("div", "card-head");
        const count = el("span", "pill");
        head.append(icon("bookmark"), el("span", null, "To save in Bee"), count);
        const list = el("ul", "bee-list");
        card.append(head, list);
        this.beeSlot.replaceChildren(card);
        this.bee = { count, list, items: [] };
      }
      const kind = String(ev.kind || "");
      const text = beeText(ev);
      const key = beeKey(ev);
      let item = null;
      if (ev.type === "bee_write_done") {
        // Confirm and accept writes carry no text, and a done line may lack the item ids,
        // so fall back to the oldest queued write of the same kind.
        const queued = this.bee.items.filter((i) => i.state === "queued" && i.kind === kind);
        item = queued.find((i) => i.key === key) || (ownText(ev) ? queued.find((i) => i.text === text) : queued[0]) || null;
      }
      if (!item) {
        const row = el("li", "bee-item");
        const status = el("span", "bee-status");
        row.append(el("span", "bee-kind", BEE_KIND[kind] || humanize(kind) || "Item"),
          el("span", "bee-text", text || "(no text)"), status);
        this.bee.list.append(row);
        item = { kind, text, key, row, status, state: "queued" };
        this.bee.items.push(item);
      }
      if (ev.type === "bee_write_done") item.state = ev.ok === false ? "failed" : "saved";
      item.row.dataset.state = item.state;
      item.status.replaceChildren();
      if (item.state === "saved") item.status.append(icon("check"), "Saved");
      else if (item.state === "failed") item.status.append(icon("x"), "Not saved");
      else item.status.append("Queued");
      const saved = this.bee.items.filter((i) => i.state === "saved").length;
      const total = this.bee.items.length;
      this.bee.count.textContent = saved ? `${saved} of ${total} saved` : `${total} queued`;
    }
  }

  function ownText(ev) {
    return typeof ev.text === "string" ? ev.text.trim() : "";
  }

  function beeItem(ev) {
    return ev.item && typeof ev.item === "object" ? ev.item : {};
  }

  function beeText(ev) {
    const item = beeItem(ev);
    if (ownText(ev)) return ownText(ev);
    if (item.fact_id !== undefined) return `Fact #${item.fact_id}`;
    if (item.suggestion_id !== undefined) return `Suggestion #${item.suggestion_id}`;
    return "";
  }

  function beeKey(ev) {
    const item = beeItem(ev);
    return [ev.kind, ownText(ev), item.fact_id ?? "", item.suggestion_id ?? ""].join("|");
  }

  function debugLabel(ev) {
    if (ev.type === "metrics") {
      const data = ev.data && typeof ev.data === "object" ? ev.data : {};
      const keys = ["ttft", "ttfb", "end_of_utterance_delay", "transcription_delay", "duration"];
      const parts = keys.filter((k) => typeof data[k] === "number").slice(0, 2).map((k) => `${k} ${data[k].toFixed(2)}s`);
      return [ev.kind || "metrics", ...parts].join(" · ");
    }
    if (ev.type === "user_state") return `user ${ev.old} → ${ev.new}`;
    if (ev.type === "item") return `item ${ev.kind || ""}`;
    if (ev.type === "tool") return `tool ${ev.name || ""}`;
    return String(ev.type || "event");
  }

  // ---------- routing ----------

  let page = null;

  function route() {
    if (page) page.close();
    const hash = location.hash.replace(/^#/, "");
    const m = /^\/s\/(\d{4}-\d{2}-\d{2})\/(\d{4}(?:-\d+)?)$/.exec(hash);
    if (hash === "/calls") page = new ListPage();
    else if (m) page = new SessionPage(`${m[1]}/${m[2]}`);
    else page = new FollowPage();
  }

  window.addEventListener("hashchange", route);
  route();
})();
