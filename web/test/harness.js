// Runs web/app.js in Node for tests (node --test web/test/), with no browser and no packages: a small stand-in
// for the parts of the DOM the page uses, an EventSource the test feeds with events.jsonl lines, and fetch and
// timers the test controls. The page itself stays plain JS with no build step.
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const WEB = path.resolve(__dirname, "..");
const REPO = path.resolve(WEB, "..");

// ---------- a small DOM ----------

class FakeNode {
  constructor(doc) {
    this.ownerDocument = doc;
    this.parentNode = null;
    this.childNodes = [];
  }

  get textContent() {
    return this.childNodes.map((node) => node.textContent).join("");
  }

  remove() {
    if (!this.parentNode) return;
    const siblings = this.parentNode.childNodes;
    siblings.splice(siblings.indexOf(this), 1);
    this.parentNode = null;
  }
}

class FakeText extends FakeNode {
  constructor(doc, data) {
    super(doc);
    this.data = String(data);
  }

  get textContent() {
    return this.data;
  }

  set textContent(value) {
    this.data = String(value);
  }
}

class FakeClassList {
  constructor(element) {
    this.element = element;
  }

  get names() {
    return this.element.className.split(/\s+/).filter(Boolean);
  }

  contains(name) {
    return this.names.includes(name);
  }

  add(...names) {
    this.element.className = [...new Set([...this.names, ...names])].join(" ");
  }

  remove(...names) {
    this.element.className = this.names.filter((name) => !names.includes(name)).join(" ");
  }

  toggle(name, force) {
    const on = force === undefined ? !this.contains(name) : Boolean(force);
    if (on) this.add(name);
    else this.remove(name);
    return on;
  }
}

class FakeElement extends FakeNode {
  constructor(doc, tag) {
    super(doc);
    this.localName = tag.toLowerCase();
    this.tagName = tag.toUpperCase();
    this.attributes = {};
    this.className = "";
    this.dataset = {};
    this.hidden = false;
    this.listeners = {};
    this.classList = new FakeClassList(this);
    this.open = false;
  }

  get id() {
    return this.attributes.id || "";
  }

  get children() {
    return this.childNodes.filter((node) => node instanceof FakeElement);
  }

  get childElementCount() {
    return this.children.length;
  }

  get textContent() {
    return super.textContent;
  }

  set textContent(value) {
    this.replaceChildren();
    if (value !== null && value !== undefined && value !== "") this.append(String(value));
  }

  setAttribute(name, value) {
    if (name === "class") this.className = String(value);
    else this.attributes[name] = String(value);
  }

  getAttribute(name) {
    if (name === "class") return this.className;
    return name in this.attributes ? this.attributes[name] : null;
  }

  append(...nodes) {
    for (const node of nodes) {
      this.appendChild(typeof node === "string" ? new FakeText(this.ownerDocument, node) : node);
    }
  }

  appendChild(node) {
    if (node instanceof FakeFragment) {
      for (const child of [...node.childNodes]) this.appendChild(child);
      return node;
    }
    node.remove();
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  insertBefore(node, before) {
    if (before === null || before === undefined) return this.appendChild(node);
    if (node instanceof FakeFragment) {
      for (const child of [...node.childNodes]) this.insertBefore(child, before);
      return node;
    }
    node.remove();
    const index = this.childNodes.indexOf(before);
    if (index < 0) throw new Error("insertBefore: the reference node is not a child");
    node.parentNode = this;
    this.childNodes.splice(index, 0, node);
    return node;
  }

  replaceChildren(...nodes) {
    for (const child of [...this.childNodes]) child.remove();
    this.append(...nodes);
  }

  addEventListener(type, listener) {
    (this.listeners[type] ||= []).push(listener);
  }

  removeEventListener(type, listener) {
    this.listeners[type] = (this.listeners[type] || []).filter((l) => l !== listener);
  }

  click() {
    const event = { type: "click", target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    for (const listener of this.listeners.click || []) listener(event);
    return event;
  }

  getBoundingClientRect() {
    return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 };
  }

  showModal() {
    this.open = true;
  }

  close() {
    this.open = false;
  }
}

class FakeFragment extends FakeElement {
  constructor(doc) {
    super(doc, "#fragment");
  }
}

class FakeDocument {
  constructor() {
    this.body = new FakeElement(this, "body");
    this.documentElement = { scrollHeight: 1000 };
    this.title = "";
  }

  createElement(tag) {
    return new FakeElement(this, tag);
  }

  createElementNS(_namespace, tag) {
    return new FakeElement(this, tag);
  }

  createTextNode(text) {
    return new FakeText(this, text);
  }

  createDocumentFragment() {
    return new FakeFragment(this);
  }

  getElementById(id) {
    return find(this.body, (node) => node.id === id) || null;
  }
}

function walk(root, visit) {
  for (const child of root.childNodes) {
    if (!(child instanceof FakeElement)) continue;
    visit(child);
    walk(child, visit);
  }
}

function find(root, test) {
  let found = null;
  walk(root, (node) => { if (!found && test(node)) found = node; });
  return found;
}

function findAll(root, test) {
  const found = [];
  walk(root, (node) => { if (test(node)) found.push(node); });
  return found;
}

// The elements index.html gives the page (every element with an id), so the page finds what it expects.
function bodyFromIndex(doc) {
  const html = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
  const tag = /<([a-z][a-z0-9]*)\b([^>]*)>/gi;
  for (const [, name, attrs] of html.matchAll(tag)) {
    const id = /\sid="([^"]+)"/.exec(attrs);
    if (!id) continue;
    const element = doc.createElement(name);
    element.setAttribute("id", id[1]);
    element.hidden = /\shidden(\s|$|=)/.test(attrs);
    doc.body.appendChild(element);
  }
}

// ---------- the page, loaded ----------

/**
 * Loads web/app.js on a fresh fake page.
 *   hash, search: the page's address (location.hash and location.search)
 *   fetch: async (url) => response; by default every request fails like an unreachable server
 *   bridge: true for the iPhone app's web view (window.webkit.messageHandlers.beeCall)
 *   prefs: what localStorage already holds, without the "beecall." prefix
 */
function loadPage({ hash = "#/", search = "", fetch, bridge = false, prefs = {} } = {}) {
  const doc = new FakeDocument();
  bodyFromIndex(doc);

  const streams = [];
  class FakeEventSource {
    constructor(url) {
      this.url = url;
      this.readyState = FakeEventSource.CONNECTING;
      this.listeners = {};
      this.onmessage = null;
      this.onerror = null;
      streams.push(this);
    }

    addEventListener(type, listener) {
      (this.listeners[type] ||= []).push(listener);
    }

    close() {
      this.readyState = FakeEventSource.CLOSED;
    }

    message(data) {
      this.readyState = FakeEventSource.OPEN;
      if (this.onmessage) this.onmessage({ data });
    }

    emit(type, data) {
      for (const listener of this.listeners[type] || []) listener({ data });
    }

    fail() {
      if (this.onerror) this.onerror({});
    }
  }
  FakeEventSource.CONNECTING = 0;
  FakeEventSource.OPEN = 1;
  FakeEventSource.CLOSED = 2;

  const timers = new Map();
  let nextTimer = 1;
  const addTimer = (fn) => { timers.set(nextTimer, fn); return nextTimer++; };
  const clock = { now: 0 };
  const store = new Map(Object.entries(prefs).map(([key, value]) => [`beecall.${key}`, String(value)]));
  const posted = [];
  const windowListeners = {};

  const sandbox = {
    document: doc,
    location: { hash, search },
    localStorage: {
      getItem: (key) => (store.has(key) ? store.get(key) : null),
      setItem: (key, value) => store.set(key, String(value)),
    },
    performance: { now: () => clock.now },
    EventSource: FakeEventSource,
    fetch: fetch || (async () => { throw new TypeError("Failed to fetch"); }),
    setInterval: (fn) => addTimer(fn),
    clearInterval: (id) => timers.delete(id),
    setTimeout: (fn) => addTimer(fn),
    clearTimeout: (id) => timers.delete(id),
    console,
    innerHeight: 800,
    scrollY: 0,
    scrollTo() {},
    scrollBy() {},
    addEventListener: (type, listener) => (windowListeners[type] ||= []).push(listener),
    removeEventListener: (type, listener) => {
      windowListeners[type] = (windowListeners[type] || []).filter((l) => l !== listener);
    },
  };
  // A message made inside the page's context is copied, so tests compare it with plain objects of their own.
  const keep = (message) => posted.push(JSON.parse(JSON.stringify(message)));
  if (bridge) sandbox.webkit = { messageHandlers: { beeCall: { postMessage: keep } } };
  sandbox.window = sandbox;

  const source = fs.readFileSync(path.join(WEB, "app.js"), "utf8");
  vm.runInContext(source, vm.createContext(sandbox), { filename: path.join(WEB, "app.js") });

  const page = {
    document: doc,
    streams,
    posted,
    store,
    clock,
    /** The event stream the page opened last. */
    get stream() {
      return streams[streams.length - 1];
    },
    /** Sends events.jsonl lines (strings or objects) as the server's messages, then the stream's "ready". */
    feed(lines, { idle = 0 } = {}) {
      for (const line of lines) page.stream.message(typeof line === "string" ? line : JSON.stringify(line));
      page.stream.emit("ready", JSON.stringify({ lines: lines.length, idle_s: idle }));
    },
    /** Sends lines after "ready", as a live call does. */
    live(lines) {
      for (const line of lines) page.stream.message(typeof line === "string" ? line : JSON.stringify(line));
    },
    /** Runs every timer the page has set once (the status ticker, the list's poll). */
    tick() {
      for (const fn of [...timers.values()]) fn();
    },
    /** Lets pending promises (fetch) finish. */
    async settle() {
      for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve));
    },
    all(className) {
      return findAll(doc.body, (node) => node.classList.contains(className));
    },
    one(className) {
      const found = page.all(className);
      if (found.length !== 1) throw new Error(`expected one .${className}, found ${found.length}`);
      return found[0];
    },
    has(className) {
      return page.all(className).length > 0;
    },
    byId(id) {
      return doc.getElementById(id);
    },
    /** All the text a person sees on the page (hidden elements left out). */
    visibleText(root = doc.body) {
      if (root.hidden) return "";
      return root.childNodes.map((node) => (node instanceof FakeElement ? page.visibleText(node) : node.textContent))
        .join("");
    },
    text(node) {
      return node.textContent.replace(/\s+/g, " ").trim();
    },
  };
  return page;
}

/** The lines of an events.jsonl file, by its path from the repository root. */
function fixture(relative) {
  return fs.readFileSync(path.join(REPO, relative), "utf8").split("\n").filter((line) => line.trim());
}

module.exports = { loadPage, fixture };
