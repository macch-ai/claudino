// Connection to the claudino Worker on Cloudflare: receives this teammate's Slack runs and runs them locally.
// Protocol: see worker/src/user.ts (runs) and worker/src/pairing.ts (pairing) in the repository root.
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { ask, WORKDIR } from "./claude";

const CONFIG_DIR = path.join(os.homedir(), ".config/claudino");
const CONFIG_FILE = path.join(CONFIG_DIR, "desktop.json"); // written by pairing: {url, user, token, name, handle, avatar}
const SESSIONS_FILE = path.join(CONFIG_DIR, "sessions.json"); // Slack thread key -> Claude session id
const FILES_DIR = path.join(CONFIG_DIR, "files"); // Slack files of the threads this Mac worked on
const MAX_BACKOFF_MS = 60_000;
const CONNECT_TIMEOUT_MS = 8_000;
const PING_MS = 30_000; // Cloudflare closes idle WebSockets; "ping" is answered without waking the Worker
const MAX_TASKS = 50;
export const DEFAULT_URL = process.env.CLAUDINO_HUB_URL || "wss://claudino.macch-claudino.workers.dev"; // the Worker

export type HubStatus = "no-config" | "connecting" | "connected" | "denied" | "offline";

export interface Config {
  url: string;
  user: string; // Slack user ID, vouched for by Slack when pairing
  token: string;
  name?: string; // Slack profile, sent by the Worker when pairing
  handle?: string;
  avatar?: string;
}

/** Pairing in progress: write "@Machine AI vincular <code>" in Slack before expiresAt. */
export interface Pairing {
  state: "requesting" | "waiting" | "expired" | "unreachable" | "busy";
  code?: string;
  expiresAt?: number;
  loginUrl?: string; // "Iniciar sesión con Slack" in the browser, when the Worker has Sign in with Slack configured
}

/** How this Mac is named on the browser confirmation page ("MacBook-Pro-de-Salvador"). */
function deviceName(): string {
  return os.hostname().replace(/\.local$/, "");
}

export interface Task {
  id: string;
  label: string; // "Pedido de Slack" or the automation's name
  key?: string; // "<channel>:<thread_ts>" for Slack requests
  startedAt: number;
  endedAt?: number;
  state: "running" | "done" | "error" | "failed";
  summary?: string;
}

interface FileRef {
  id: string;
  ts: string;
  name: string;
  mimetype: string;
}

interface RunMessage {
  type: "run";
  id: string;
  key: string;
  prompt: string; // has {{file:<id>}} where each of `files` goes
  system: string;
  files?: FileRef[];
}

interface EmojiMessage {
  type: "emoji";
  id: string;
  prompt: string;
}

/** Download the thread's Slack files through the Worker and put their local paths in the prompt. */
async function withFiles(prompt: string, files: FileRef[], config: Config): Promise<string> {
  const base = config.url.replace(/^ws/, "http");
  for (const f of files) {
    const safe = f.name.replace(/[^A-Za-z0-9._-]/g, "_");
    const dest = path.join(FILES_DIR, `${f.ts}_${f.id}_${safe}`);
    let note: string;
    try {
      if (!fs.existsSync(dest)) {
        const resp = await fetch(`${base}/files/${encodeURIComponent(f.id)}`, {
          headers: { Authorization: `Bearer ${config.user}:${config.token}` },
        });
        if (!resp.ok) throw new Error(`${resp.status} ${(await resp.text()).slice(0, 200)}`);
        fs.mkdirSync(FILES_DIR, { recursive: true });
        fs.writeFileSync(dest, Buffer.from(await resp.arrayBuffer()));
      }
      note = `[attached file ${f.name} (${f.mimetype}) saved at ${dest}; open it with the Read tool]`;
    } catch (err) {
      note = `[file ${f.name}: download failed (${(err as Error).message})]`;
    }
    prompt = prompt.split(`{{file:${f.id}}}`).join(note);
  }
  return prompt;
}

interface AutomationMessage {
  type: "automation";
  id: string;
  name: string;
}

/** Runs the automation called name (from Slack); rejects when there is none. */
export type AutomationRunner = (name: string) => Promise<{ result: string; isError: boolean }>;

function readJson<T>(file: string, fallback: T): T {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8")) as T;
  } catch {
    return fallback;
  }
}

function writeJson(file: string, data: unknown, mode?: number): void {
  fs.mkdirSync(CONFIG_DIR, { recursive: true });
  fs.writeFileSync(file, JSON.stringify(data, null, 1), { mode });
}

export function loadConfig(): Partial<Config> {
  return readJson<Partial<Config>>(CONFIG_FILE, {});
}

export function saveConfig(config: Config): void {
  writeJson(CONFIG_FILE, config, 0o600); // holds the hub token
}

function clearConfig(): void {
  const { url } = loadConfig();
  if (url && url !== DEFAULT_URL) writeJson(CONFIG_FILE, { url }, 0o600); // keep a custom Worker URL
  else fs.rmSync(CONFIG_FILE, { force: true });
}

function setSession(key: string, sessionId: string | undefined): void {
  const sessions = readJson<Record<string, string>>(SESSIONS_FILE, {});
  if (sessionId) sessions[key] = sessionId;
  else delete sessions[key];
  writeJson(SESSIONS_FILE, sessions);
}

function firstLine(text: string): string {
  return text.trim().split("\n")[0].slice(0, 160);
}

export class HubClient {
  status: HubStatus = "connecting";
  tasks: Task[] = [];
  pairing: Pairing | null = null;
  private outbox: object[] = []; // automation results waiting for the connection
  private ws: WebSocket | null = null;
  private pairWs: WebSocket | null = null;
  private backoff = 1000;
  private retry: NodeJS.Timeout | null = null;

  constructor(private onChange: () => void, private runAutomation: AutomationRunner) {}

  /** Show a run in the Tasks list until it settles. */
  track(label: string, work: Promise<{ result: string; isError: boolean }>, key?: string): void {
    const task: Task = { id: crypto.randomUUID(), label, key, startedAt: Date.now(), state: "running" };
    this.tasks = [task, ...this.tasks].slice(0, MAX_TASKS);
    this.onChange();
    work.then(
      (r) => Object.assign(task, { state: r.isError ? "error" : "done", summary: firstLine(r.result) }),
      (err) => Object.assign(task, { state: "failed", summary: firstLine((err as Error).message) }),
    ).finally(() => {
      task.endedAt = Date.now();
      this.onChange();
    });
  }

  /** Ask the Worker to send a scheduled automation's result by DM and/or to a channel; kept until connected. */
  post(target: { dm: boolean; channel?: string }, name: string, text: string): void {
    this.outbox.push({ type: "post", dm: target.dm, channel: target.channel, name, text });
    this.flush();
  }

  private flush(): void {
    while (this.outbox.length && this.status === "connected" && this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(this.outbox.shift()));
    }
  }

  get running(): number {
    return this.tasks.filter((t) => t.state === "running").length;
  }

  /** Ask the Worker for a pairing code and wait until the teammate confirms it (or it expires). */
  pair(): void {
    this.pairWs?.close();
    const url = loadConfig().url || DEFAULT_URL;
    const ws = new WebSocket(`${url}/pair`);
    this.pairWs = ws;
    let paired = false;
    this.setPairing({ state: "requesting" });
    // An address that does not answer can leave the socket CONNECTING for minutes: give up sooner.
    const timer = setTimeout(() => {
      if (this.pairWs !== ws || this.pairing?.state !== "requesting") return;
      this.pairWs = null;
      ws.close();
      this.setPairing({ state: "unreachable" });
    }, CONNECT_TIMEOUT_MS);
    ws.onopen = () => ws.send(JSON.stringify({ type: "pair", device: deviceName() }));
    ws.onmessage = (event) => {
      const msg = JSON.parse(String(event.data));
      if (msg.type === "pair_code") {
        clearTimeout(timer);
        this.setPairing({ state: "waiting", code: msg.code, expiresAt: Date.now() + msg.expires_in * 1000,
                          loginUrl: msg.login_url });
      } else if (msg.type === "pair_unavailable") {
        this.setPairing({ state: "busy" });
      } else if (msg.type === "paired") {
        paired = true;
        saveConfig({ url, user: msg.user, token: msg.token, name: msg.name, handle: msg.handle, avatar: msg.avatar });
        this.setPairing(null);
        this.start();
      }
    };
    ws.onclose = () => {
      clearTimeout(timer);
      if (this.pairWs !== ws || paired) return;
      this.pairWs = null;
      this.setPairing({ state: this.pairing?.state === "waiting" ? "expired" : "unreachable" });
    };
  }

  /** Forget this Mac's link: the Worker drops the token and the app goes back to pairing. */
  unpair(): void {
    if (this.status === "connected") this.ws?.send(JSON.stringify({ type: "unpair" }));
    clearConfig();
    this.start();
    this.pair();
  }

  private setPairing(pairing: Pairing | null): void {
    this.pairing = pairing;
    this.onChange();
  }

  /** (Re)connect with the saved config; call again after saving a new one. */
  start(): void {
    if (this.retry) clearTimeout(this.retry);
    const old = this.ws;
    this.ws = null;
    old?.close();
    this.backoff = 1000;
    const config = loadConfig();
    if (!config.url || !config.user || !config.token) return this.set("no-config");
    this.connect(config as Config);
  }

  private set(status: HubStatus): void {
    this.status = status;
    this.onChange();
  }

  private connect(config: Config): void {
    this.set("connecting");
    const ws = new WebSocket(`${config.url}/ws?user=${encodeURIComponent(config.user)}`);
    this.ws = ws;
    let ping: NodeJS.Timeout | null = null;
    const retryLater = () => {
      this.set("offline");
      this.retry = setTimeout(() => this.connect(config), this.backoff);
      this.backoff = Math.min(this.backoff * 2, MAX_BACKOFF_MS);
    };
    // Same as pairing: an unanswered address would leave us "connecting" for minutes.
    const timer = setTimeout(() => {
      if (this.ws !== ws || this.status !== "connecting") return;
      this.ws = null;
      ws.close();
      retryLater();
    }, CONNECT_TIMEOUT_MS);
    ws.onopen = () => {
      ws.send(JSON.stringify({ type: "hello", user: config.user, token: config.token }));
      ping = setInterval(() => ws.readyState === WebSocket.OPEN && ws.send("ping"), PING_MS);
    };
    ws.onmessage = (event) => {
      if (event.data === "pong") return;
      const msg = JSON.parse(String(event.data));
      if (msg.type === "welcome") {
        clearTimeout(timer);
        this.backoff = 1000;
        this.set("connected");
        this.flush();
      } else if (msg.type === "denied") {
        this.set("denied");
      } else if (msg.type === "run") {
        void this.handleRun(ws, msg as RunMessage);
      } else if (msg.type === "automation") {
        void this.handleAutomation(ws, msg as AutomationMessage);
      } else if (msg.type === "emoji") {
        void this.handleEmoji(ws, msg as EmojiMessage);
      }
    };
    ws.onclose = () => {
      clearTimeout(timer);
      if (ping) clearInterval(ping);
      if (this.ws !== ws || this.status === "denied") return; // replaced, or a wrong token: never retry
      retryLater();
    };
  }

  private async handleRun(ws: WebSocket, msg: RunMessage): Promise<void> {
    const sessionId = readJson<Record<string, string>>(SESSIONS_FILE, {})[msg.key];
    const work = withFiles(msg.prompt, msg.files ?? [], loadConfig() as Config)
      .then((prompt) => ask(prompt, { sessionId, system: msg.system, cwd: WORKDIR, permissionMode: "auto" }));
    this.track("Pedido de Slack", work, msg.key);
    try {
      const r = await work;
      if (r.sessionId) setSession(msg.key, r.sessionId);
      ws.send(JSON.stringify({ type: "result", id: msg.id, result: r.result, is_error: r.isError }));
    } catch (err) {
      setSession(msg.key, undefined); // the relay retries once with the whole thread in a new session
      ws.send(JSON.stringify({ type: "failed", id: msg.id, error: (err as Error).message }));
    }
  }

  /** The Worker reacts on each request with the emoji Haiku picks here (it falls back to :eyes:). */
  private async handleEmoji(ws: WebSocket, msg: EmojiMessage): Promise<void> {
    let name = "";
    try {
      name = (await ask(msg.prompt, { model: "haiku", noTools: true, cwd: os.tmpdir() })).result;
    } catch {
      // the Worker uses :eyes:
    }
    ws.send(JSON.stringify({ type: "emoji_result", id: msg.id, name }));
  }

  private async handleAutomation(ws: WebSocket, msg: AutomationMessage): Promise<void> {
    try {
      const r = await this.runAutomation(msg.name); // tracked by the scheduler's onRun
      ws.send(JSON.stringify({ type: "result", id: msg.id, result: r.result, is_error: r.isError }));
    } catch (err) {
      ws.send(JSON.stringify({ type: "failed", id: msg.id, error: (err as Error).message }));
    }
  }
}
