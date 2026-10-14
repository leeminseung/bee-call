// Live session screen for Bee Call. Plain JS, no build step.
// Routes: #/ follows the newest call, #/calls lists all calls, #/s/<date>/<time> opens one call,
// #/meta shows what the meta agent noticed and decided between calls.
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
  // The debug toggle, and the debug events behind it, exist only with ?debug=1 in the address, which the iPhone
  // app adds in its Debug builds. A call record otherwise shows the conversation, why the AI called, and its jobs.
  const DEBUG_ALLOWED = /(?:^\?|&)debug=1(?:&|$)/.test(location.search);

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
    if (name === "bee_conversation") return "Read a conversation in full";
    if (name === "bee_day") return `Read the day summary for ${args.date || "a day"}`;
    return humanize(name);
  }

  // The prep note keeps Bee's ids for the AI ("(conversation 10785664)", "todo suggestion 11628862"); people
  // do not need them, so the page leaves them out. The note itself is unchanged.
  const ID_WORDS = "convs?|conversations?|todos?|todo suggestions?|suggestions?|facts?|unconfirmed facts?|ids?";
  const ID_GROUP = new RegExp(`\\s*\\((?:(?:${ID_WORDS})\\.?\\s*)?#?\\d{4,}(?:\\s*(?:,|and|&)\\s*#?\\d{4,})*\\)`, "gi");
  const ID_AFTER_WORD = new RegExp(`\\b(${ID_WORDS})\\s+#?\\d{4,}\\b`, "gi");
  function withoutIds(text) {
    return String(text || "")
      .replace(ID_GROUP, "")
      .replace(ID_AFTER_WORD, "$1")
      .replace(/(^|\s)#\d{4,}\b/g, "$1")
      .replace(/[ \t]+([,.;:])/g, "$1")
      .replace(/[ \t]{2,}/g, " ");
  }

  // The iPhone app's web view carries this bridge (CallRecordSheet.swift); a Mac browser does not.
  function appBridge() {
    const handlers = window.webkit && window.webkit.messageHandlers;
    return handlers && handlers.beeCall ? handlers.beeCall : null;
  }

  const INTERNAL_FILES = new Set(["usage.json"]); // the worker's own bookkeeping, shown only with debug events

  // What an error means for the person on the call; the raw text stays one tap away.
  function errorText(ev) {
    const source = String(ev.source || "");
    if (source === "bee_context") return "Some of your Bee data could not be read before the call.";
    if (source.startsWith("handler:")) return null; // the page's own bookkeeping: debug events only
    return "Something went wrong during the call.";
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

  const UNREACHABLE = "Can't reach the call records right now. Retrying…"; // the server is on the Mac or in the cloud
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

  function metaLink() {
    const link = el("a", "meta-link");
    link.href = "#/meta";
    link.append(el("span", null, "Between calls: what Bee Call noticed"), icon("chevron"));
    return link;
  }

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
      else if (!sessions.length) view.replaceChildren(metaLink(), el("p", "empty", NO_CALLS));
      else this.render(sessions);
    }

    render(sessions) {
      const frag = document.createDocumentFragment();
      frag.append(metaLink());
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

  // ---------- between calls: the meta agent's session ----------

  const EVENT_TEXT = {
    bee_change: (e) => `Bee saved something new (${e.inbox || "inbox"})`,
    call_ended: (e) => `A call ended (${e.call || ""})`,
    offer_answered: (e) => `You answered an offer about “${e.topic || ""}”: ${e.choice || ""}`,
    booked_soon: (e) => `A booked call is coming up at ${atLabel(e.call_at)}: “${e.topic || ""}”`,
  };

  class MetaPage {
    constructor() {
      setHeader("Between calls", true);
      setStatus(null);
      this.shown = "";
      this.load();
      this.timer = setInterval(() => this.load(), LIST_POLL_MS);
    }

    async load() {
      let data = null;
      try {
        const response = await fetch(`${API.replace(/\/sessions$/, "")}/meta`, { cache: "no-store" });
        if (response.ok) data = await response.json();
      } catch { /* shown below */ }
      if (this.closed) return;
      const key = JSON.stringify(data);
      if (key === this.shown) return;
      this.shown = key;
      if (data === null) showNote(UNREACHABLE);
      else this.render(data);
    }

    render(data) {
      const frag = document.createDocumentFragment();
      frag.append(el("p", "meta-intro", "Bee Call looks at your day between calls. It only proposes; nothing happens until you say yes. "
        + "Every time it looked, its own words are below."));
      if (data.state) {
        const card = el("section", "card meta-state");
        const head = el("div", "card-head");
        head.append(icon("bookmark"), el("span", null, "What it keeps track of"));
        card.append(head, el("p", "meta-state-text", data.state));
        frag.append(card);
      }
      const entries = Array.isArray(data.entries) ? data.entries : [];
      if (!entries.length) frag.append(el("p", "empty", "Nothing yet. Bee Call looks when Bee saves something new or a call ends."));
      for (const entry of entries) frag.append(this.entry(entry));
      view.replaceChildren(frag);
    }

    entry(entry) {
      const card = el("section", "card meta-entry");
      const when = parseDate(entry.at);
      const head = el("div", "card-head");
      head.append(icon("search"), el("span", null, when ? `${dayLabel(when)} · ${hhmm(when)}` : String(entry.at || "")));
      card.append(head);
      const events = Array.isArray(entry.events) ? entry.events : [];
      if (events.length) {
        const list = el("ul", "meta-events");
        for (const e of events) {
          const text = (EVENT_TEXT[e.kind] || ((x) => String(x.kind || "event")))(e);
          const item = el("li");
          if (e.kind === "call_ended" && e.call) {
            const link = el("a", null, text);
            link.href = `#/s/${e.call}`;
            item.append(link);
          } else item.textContent = text;
          list.append(item);
        }
        card.append(list);
      }
      if (entry.error) card.append(el("p", "job-error", `It could not finish: ${entry.error}`));
      if (entry.decision) card.append(el("p", "meta-decision", entry.decision));
      const actions = Array.isArray(entry.actions) ? entry.actions : [];
      for (const a of actions) {
        const text = a.tool === "offer_call" ? `Asked you about a call: “${a.topic}”. ${a.result || ""}`
          : a.tool === "write_state" ? "Updated what it keeps track of" : `${a.tool}: ${a.result || ""}`;
        card.append(el("p", "card-note", text));
      }
      const tools = Array.isArray(entry.tools) ? entry.tools : [];
      if (tools.length) {
        const details = el("details", "meta-tools");
        details.append(el("summary", null, `What it looked at (${tools.length})`));
        for (const t of tools) {
          const block = el("div", "meta-tool");
          block.append(el("p", "meta-tool-name", `${t.name} ${t.input && Object.keys(t.input).length ? JSON.stringify(t.input) : ""}`),
            el("pre", "json", String(t.result || "")));
          details.append(block);
        }
        card.append(details);
      }
      const foot = [entry.model, typeof entry.seconds === "number" ? `${entry.seconds.toFixed(1)} s` : null].filter(Boolean);
      if (foot.length) card.append(el("p", "card-note", foot.join(", ")));
      return card;
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
      this.debug = DEBUG_ALLOWED && readPref("debug") === "1";
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
      this.continueSlot = el("div", "continue-slot"); // "Continue this call", in the iPhone app once the call is over
      const foot = el("footer", "session-foot");
      this.footNote = el("span", "foot-note");
      this.debugButton = el("button", "link-button");
      this.debugButton.type = "button";
      this.debugButton.hidden = !DEBUG_ALLOWED;
      this.debugButton.addEventListener("click", () => this.toggleDebug());
      foot.append(this.footNote, this.debugButton);
      this.root.append(this.whySlot, this.prepSlot, this.timeline, this.dock, this.beeSlot, this.continueSlot, foot);
      view.replaceChildren(this.root);
    }

    reset() {
      this.whySlot.replaceChildren();
      this.prepSlot.replaceChildren();
      this.direction = null;
      this.callInfo = null; // the "call" event: which way the call went, and for an AI call its topic and reason
      this.beeContext = null; // the "why_calling" event: the Bee conversation and facts read for the call
      this.pickedUp = null;
      this.prepEvent = null;
      this.pendingEl.replaceChildren();
      this.pendingEl.hidden = true;
      this.timeline.replaceChildren(this.pendingEl); // the unconfirmed user line always sits last
      this.userTurn = null; // the person's bubble since the AI's last turn
      this.dock.replaceChildren();
      this.beeSlot.replaceChildren();
      this.continueSlot.replaceChildren();
      this.jobs = new Map();
      this.bee = null; // writes queued for after the call (records before 2026-10-15)
      this.writes = null; // writes made during the call
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
      if (this.endT !== null || (this.ready && idle > STALE_S)) this.renderContinue();
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
        case "why_calling":
          this.beeContext = ev;
          this.renderWhy();
          break;
        case "call":
          this.direction = ev.direction;
          this.callInfo = ev;
          this.renderWhy();
          if (typeof ev.continue_from === "string" && ev.continue_from) this.addContinued(ev.continue_from);
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
        case "bee_write": case "bee_write_undone": this.beeWrite(ev); break;
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

    // This call continues an earlier one (voice/continuation.py): link back to it.
    addContinued(id) {
      const row = el("li", "row system");
      const link = el("a", "row-link", `Continues your call from ${titleFor(id, null)}`);
      link.href = `#/s/${id}`;
      row.append(icon("phone"), link);
      this.add(row);
    }

    // A new call that picks up from this one. The app starts it; the page only asks.
    renderContinue() {
      const bridge = appBridge();
      if (!bridge || this.continueSlot.childElementCount) return;
      const button = el("button", "continue-call");
      button.type = "button";
      button.append(icon("phone"), el("span", null, "Continue this call"));
      button.addEventListener("click", () => bridge.postMessage({ continue: this.id }));
      this.continueSlot.replaceChildren(button, el("p", "card-note", "Bee Call picks up where this call left off."));
    }

    addError(ev, t) {
      const plain = errorText(ev);
      if (!this.debug && !plain) return;
      const row = el("li", "row error");
      const source = ev.source && ev.source !== "NoneType" ? `${humanize(ev.source)}: ` : "";
      const raw = `${source}${ev.error || "something went wrong"}`;
      const text = el("span", "row-text", this.debug ? raw : plain);
      if (!this.debug) {
        text.title = "Tap to show what went wrong";
        text.addEventListener("click", () => { text.textContent = text.textContent === plain ? raw : plain; });
      } else {
        text.title = "Tap to show the whole error";
        text.addEventListener("click", () => text.classList.toggle("open")); // long errors start clamped
      }
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

    // The person's turns before the AI's next turn read as one bubble: speech recognition cuts continuous speech
    // into several turns. `userTurn` is that open bubble, closed by the AI's next turn.
    addMessage(ev, t) {
      const user = ev.role === "user";
      const text = typeof ev.text === "string" ? ev.text.trim() : "";
      let turn;
      if (user) {
        this.pending = []; // this turn confirms the words shown so far
        if (this.userTurn) {
          this.userTurn.texts.push(text);
          this.userTurn.interrupted = Boolean(ev.interrupted);
        } else {
          this.userTurn = { ...this.newTurn("user", ev, t), texts: [text], interrupted: Boolean(ev.interrupted) };
        }
        turn = this.userTurn.turn;
      } else {
        if (this.userTurn) {
          const closed = this.userTurn;
          this.userTurn = null;
          this.renderUserTurn(closed); // words still unconfirmed move to a bubble of their own after this turn
        }
        const ai = this.newTurn("ai", ev, t);
        ai.body.append(el("span", "sr-only", "AI said: "), text || "…");
        if (ev.interrupted) ai.body.append(" ", el("span", "cut", "interrupted"));
        turn = ai.turn;
      }
      if (this.debug && ev.metrics) {
        const details = el("details", "msg-metrics");
        const raw = typeof ev.metrics === "string" ? ev.metrics : JSON.stringify(ev.metrics, null, 2);
        details.append(el("summary", null, "metrics"), el("pre", "json", raw));
        turn.append(details);
      }
      this.showPending();
    }

    newTurn(who, ev, t) {
      const turn = el("li", `turn ${who}${ev.interrupted ? " interrupted" : ""}`);
      const body = el("div", who === "user" ? "bubble" : "speech");
      turn.append(body, el("div", "meta", clock(t)));
      this.add(turn);
      return { turn, body };
    }

    renderUserTurn(turn) {
      turn.turn.classList.toggle("interrupted", turn.interrupted);
      const said = turn.texts.filter(Boolean).join(" ") || "…";
      const parts = [el("span", "sr-only", "You said: "), said];
      if (turn.interrupted) parts.push(" ", el("span", "cut", "interrupted"));
      if (turn === this.userTurn && this.pending.length) {
        parts.push(" ", el("span", "pending-text", this.pending.join(" ")));
      }
      turn.body.replaceChildren(...parts);
    }

    // user_transcript is final speech that the matching user `message` confirms a moment later. It shows in the
    // person's open bubble, or in a bubble of its own (the last in the timeline) when the AI spoke last.
    addPending(text) {
      if (typeof text !== "string" || !text.trim()) return;
      this.pending.push(text.trim());
      this.showPending();
    }

    clearPending() {
      this.pending = [];
      this.showPending();
    }

    showPending() {
      if (this.userTurn) this.renderUserTurn(this.userTurn);
      if (this.userTurn || !this.pending.length) {
        this.pendingEl.replaceChildren();
        this.pendingEl.hidden = true;
        return;
      }
      const bubble = el("div", "bubble");
      bubble.append(el("span", "sr-only", "You are saying: "), this.pending.join(" "));
      this.pendingEl.replaceChildren(bubble);
      this.pendingEl.hidden = false;
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

    // Why the AI called: every call the AI started gets the card, from its "call" event's topic and reason (also a
    // booked call, or a person without Bee), with the Bee conversation behind it when there is one. A call the
    // person started never gets it. A record from before the "call" event shows its Bee conversation as before.
    renderWhy() {
      const call = this.callInfo;
      const bee = this.beeContext;
      if ((call && call.direction !== "outbound") || (!call && !bee)) {
        this.whySlot.replaceChildren();
        return;
      }
      const card = el("section", "card why");
      const head = el("div", "card-head");
      head.append(icon("chat"), el("span", null, "Why I'm calling"));
      card.append(head);
      const said = (value) => (typeof value === "string" ? value.trim() : "");
      if (call) {
        if (said(call.topic)) card.append(el("p", "card-title", said(call.topic)));
        if (said(call.reason)) card.append(el("p", "why-summary", said(call.reason)));
        if (!said(call.topic) && !said(call.reason)) card.append(el("p", "why-summary", "You said yes to this call."));
      }
      if (bee) this.addBeeContext(card, bee, Boolean(call));
      this.whySlot.replaceChildren(card);
    }

    // The Bee conversation the call is about, and the unconfirmed facts it brings. Under an AI call's own reason,
    // Bee's summary of the conversation goes with the quotes.
    addBeeContext(card, ev, underReason) {
      const summary = typeof ev.summary === "string" ? ev.summary.trim() : "";
      if (summary && !underReason) card.append(el("p", "why-summary", summary));
      const utterances = Array.isArray(ev.utterances) ? ev.utterances.filter((u) => u && u.text) : [];
      if (utterances.length || (summary && underReason)) {
        card.append(el("p", "quotes-label", "From your Bee conversation"));
        if (summary && underReason) card.append(el("p", "quotes-summary", summary));
      }
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
        card.append(list);
      }
      const facts = Array.isArray(ev.pending_fact_ids) ? ev.pending_fact_ids.length : 0;
      if (facts) {
        card.append(el("p", "card-note", `${facts} unconfirmed ${facts === 1 ? "fact" : "facts"} in Bee to check with you`));
      }
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
        card.append(el("p", "prep-note", withoutIds(note)));
      } else {
        const why = ev.error ? ` (${ev.error})` : "";
        card.append(el("p", "card-note", `No prep note${why}, so the call started with the plain Bee context.`));
      }
      const reads = Array.isArray(ev.reads) ? ev.reads.map(readLabel).filter(Boolean) : [];
      if (reads.length) {
        const list = el("ul", "prep-reads");
        const counts = new Map();
        for (const read of reads) counts.set(read, (counts.get(read) || 0) + 1);
        for (const [read, n] of counts) list.append(el("li", null, n > 1 ? `${read} (${n} times)` : read));
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
        if (INTERNAL_FILES.has(name) && !this.debug) continue;
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
        this.beeSlot.append(card);
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

    // ---------- Bee writes during the call ----------

    // The AI asks ("Shall I put ... in Bee as a to-do?"), writes right after the person's yes, and takes the last
    // write out again when asked to (bee_write_undone). A write that failed is tried once more after the call,
    // and that result comes as another bee_write with "retried". The server names each write with a key, which
    // its undo and its retry carry too. Without a key, an undo names its write by kind and text (or takes out
    // the last saved write), and only a retry updates the failed write with the same kind and text.
    beeWrite(ev) {
      if (!this.writes) {
        const card = el("section", "card bee");
        const head = el("div", "card-head");
        const count = el("span", "pill");
        head.append(icon("bookmark"), el("span", null, "Saved in Bee"), count);
        const list = el("ul", "bee-list");
        card.append(head, list);
        this.beeSlot.append(card);
        this.writes = { count, list, items: [] };
      }
      const items = this.writes.items;
      const kind = String(ev.kind || "");
      const text = beeText(ev); // its own text, or the Bee item it confirms or accepts
      const key = typeof ev.key === "string" && ev.key ? ev.key : null;
      const sameKey = key ? items.find((i) => i.key === key) : null;
      if (ev.type === "bee_write_undone") {
        const named = (i) => (!kind || i.kind === kind) && (!text || i.text === text);
        const item = key ? sameKey : [...items].reverse().find((i) => i.state === "saved" && named(i));
        if (item) this.setWriteState(item, "undone");
      } else {
        const earlier = key ? sameKey
          : ev.retried ? items.find((i) => i.state === "failed" && i.kind === kind && i.text === text) : null;
        const item = earlier || this.addWrite(ev, kind, text, key);
        this.setWriteState(item, ev.result === "ok" ? "saved" : "failed", Boolean(ev.retried));
      }
      const kept = items.filter((i) => i.state !== "undone");
      const saved = kept.filter((i) => i.state === "saved").length;
      this.writes.count.textContent = !kept.length ? "Taken out"
        : saved === kept.length ? `${saved} saved` : `${saved} of ${kept.length} saved`;
    }

    addWrite(ev, kind, text, key) {
      const row = el("li", "bee-item");
      const status = el("span", "bee-status");
      row.append(el("span", "bee-kind", BEE_KIND[kind] || humanize(kind) || "Item"),
        el("span", "bee-text", text || "(no text)"), status);
      const question = ownText({ text: ev.question });
      const answer = ownText({ text: ev.answer });
      const asked = [question && `Asked: “${question}”`, answer && `You said: “${answer}”`].filter(Boolean).join(" · ");
      if (asked) row.append(el("p", "bee-asked", asked));
      const note = el("p", "bee-note");
      row.append(note);
      const item = { key, kind, text, row, status, note, state: null };
      this.writes.items.push(item);
      this.writes.list.append(row);
      return item;
    }

    // A write that failed in the call is tried once more after it; once that retry has failed too, nothing more
    // is promised.
    setWriteState(item, state, retried = false) {
      item.state = state;
      item.row.dataset.state = state;
      item.status.replaceChildren();
      if (state === "saved") item.status.append(icon("check"), "Saved");
      else if (state === "failed") item.status.append(icon("x"), retried ? "Not saved" : "Not saved yet");
      else item.status.append("Taken out");
      item.note.textContent = state !== "failed" ? ""
        : retried ? "Bee Call tried again after the call, and it did not go through."
          : "Bee Call tries again after the call.";
      item.note.hidden = state !== "failed";
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
    else if (hash === "/meta") page = new MetaPage();
    else if (m) page = new SessionPage(`${m[1]}/${m[2]}`);
    else page = new FollowPage();
  }

  window.addEventListener("hashchange", route);
  route();
})();
