// One Durable Object per teammate: their Mac's WebSocket (hibernating), hub token, queued threads and runs in flight.
//
// Protocol with the desktop app (JSON text frames; "ping" is answered "pong" without waking the object):
//   app -> {"type":"hello","user":"U…","token":"…"}       <- {"type":"welcome"} | {"type":"denied"}
//   <- {"type":"run","id","key","prompt","system","files":[{id,ts,name,mimetype}]}   -> {"type":"result"|"failed",…}
//   <- {"type":"automation","id","name"}                   -> {"type":"result"|"failed",…}
//   <- {"type":"emoji","id","prompt"}                      -> {"type":"emoji_result","id","name"}
//   app -> {"type":"post","dm":true,"channel"?,"name","text"}  (scheduled automation result)   app -> {"type":"unpair"}
import { DurableObject } from "cloudflare:workers";
import { type Env, type Request_, threadStub } from "./env";
import { validEmoji } from "./logic";
import { post, react, slack } from "./slack";
import { timingSafeEqual } from "./slack";

type Inflight =
  | { kind: "run"; thread: string }
  | { kind: "automation"; channel: string; threadTs: string; name: string }
  | { kind: "emoji"; channel: string; ts: string };

export interface Profile {
  name?: string;
  handle?: string;
  avatar?: string;
}

export class UserDO extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
  }

  private async user(): Promise<string> {
    return (await this.ctx.storage.get<string>("user")) ?? "";
  }

  private authed(): WebSocket[] {
    return this.ctx.getWebSockets().filter((ws) => ws.deserializeAttachment()?.authed);
  }

  private async inflight(): Promise<Record<string, Inflight>> {
    return (await this.ctx.storage.get<Record<string, Inflight>>("inflight")) ?? {};
  }

  private async setInflight(id: string, entry: Inflight | null): Promise<void> {
    const all = await this.inflight();
    if (entry) all[id] = entry;
    else delete all[id];
    await this.ctx.storage.put("inflight", all);
  }

  /** WebSocket upgrade from /ws?user=U…: the app proves who it is with its first message. */
  async fetch(request: Request): Promise<Response> {
    const user = new URL(request.url).searchParams.get("user") ?? "";
    if (!(await this.user())) await this.ctx.storage.put("user", user);
    const [client, server] = Object.values(new WebSocketPair());
    this.ctx.acceptWebSocket(server);
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws: WebSocket, raw: string | ArrayBuffer): Promise<void> {
    let msg: any;
    try {
      msg = JSON.parse(typeof raw === "string" ? raw : new TextDecoder().decode(raw));
    } catch {
      return;
    }
    if (!ws.deserializeAttachment()?.authed) return this.hello(ws, msg);
    const user = await this.user();
    if (msg.type === "unpair") {
      await this.ctx.storage.delete("token");
      for (const w of this.ctx.getWebSockets()) w.close(1000, "unpaired");
      return;
    }
    if (msg.type === "post" && msg.text) {
      const name = msg.name || "Automatización";
      if (msg.dm) {
        try {
          const dm = (await slack(this.env, "conversations.open", { users: user })).channel.id;
          await post(this.env, dm, undefined, `*${name}*\n${msg.text}`);
        } catch (error) {
          console.error(`automation ${name}: DM to ${user} failed: ${error}`);
        }
      }
      if (msg.channel) await post(this.env, msg.channel, undefined, `*${name}* · automatización de <@${user}>\n${msg.text}`);
      return;
    }
    const entry = (await this.inflight())[msg.id];
    if (!entry) return;
    await this.setInflight(msg.id, null);
    if (entry.kind === "run") {
      const thread = threadStub(this.env, entry.thread);
      if (msg.type === "result") await thread.complete(msg.id, String(msg.result ?? ""), Boolean(msg.is_error));
      else await thread.failed(msg.id, String(msg.error ?? "the desktop app failed"));
    } else if (entry.kind === "automation") {
      const text = msg.type === "result"
        ? `<@${user}> *${entry.name}*:\n${msg.result || "terminó sin respuesta."}`
        : `<@${user}> falló la automatización *${entry.name}*: \`${String(msg.error ?? "").slice(0, 300)}\``;
      await post(this.env, entry.channel, entry.threadTs, text);
    } else if (entry.kind === "emoji") {
      const name = validEmoji(msg.name ?? "");
      if (!name || !(await react(this.env, entry.channel, entry.ts, name))) await react(this.env, entry.channel, entry.ts, "eyes");
    }
  }

  private async hello(ws: WebSocket, msg: any): Promise<void> {
    const token = await this.ctx.storage.get<string>("token");
    const ok = msg.type === "hello" && msg.user === (await this.user()) && token && timingSafeEqual(token, String(msg.token ?? ""));
    if (!ok) {
      ws.send(JSON.stringify({ type: "denied" }));
      ws.close(1008, "denied");
      return;
    }
    for (const old of this.authed()) old.close(1000, "replaced"); // one Mac per teammate: the newest wins
    ws.serializeAttachment({ authed: true });
    ws.send(JSON.stringify({ type: "welcome" }));
    // Threads that waited for this Mac.
    const queue = (await this.ctx.storage.get<Record<string, Request_[]>>("queue")) ?? {};
    await this.ctx.storage.delete("queue");
    for (const [key, requests] of Object.entries(queue)) await threadStub(this.env, key).resume(requests);
  }

  async webSocketClose(ws: WebSocket): Promise<void> {
    if (!ws.deserializeAttachment()?.authed || this.authed().some((w) => w !== ws)) return;
    // The Mac is gone: runs in flight will never answer.
    const all = await this.inflight();
    await this.ctx.storage.delete("inflight");
    for (const [id, entry] of Object.entries(all)) {
      if (entry.kind === "run") await threadStub(this.env, entry.thread).failed(id, "the desktop app disconnected during the run");
      else if (entry.kind === "automation") await post(this.env, entry.channel, entry.threadTs, `La Mac se desconectó durante *${entry.name}*.`);
      else await react(this.env, entry.channel, entry.ts, "eyes");
    }
  }

  async webSocketError(ws: WebSocket): Promise<void> {
    await this.webSocketClose(ws);
  }

  private send(payload: object): boolean {
    const ws = this.authed()[0];
    if (!ws) return false;
    ws.send(JSON.stringify(payload));
    return true;
  }

  // RPC from ThreadDO, PairingDO and the Worker.

  async startRun(id: string, thread: string, prompt: string, system: string, files: unknown[]): Promise<"sent" | "offline"> {
    if (!this.authed().length) return "offline";
    await this.setInflight(id, { kind: "run", thread });
    this.send({ type: "run", id, key: thread, prompt, system, files });
    return "sent";
  }

  async runAutomation(id: string, name: string, channel: string, threadTs: string): Promise<"sent" | "offline"> {
    if (!this.authed().length) return "offline";
    await this.setInflight(id, { kind: "automation", channel, threadTs, name });
    this.send({ type: "automation", id, name });
    return "sent";
  }

  /** React on a request with the emoji this teammate's Mac picks (Haiku); :eyes: when it is off. */
  async pickEmoji(id: string, channel: string, ts: string, prompt: string): Promise<void> {
    if (!this.authed().length) {
      await react(this.env, channel, ts, "eyes");
      return;
    }
    await this.setInflight(id, { kind: "emoji", channel, ts });
    this.send({ type: "emoji", id, prompt });
  }

  /** Keep a thread's requests until this Mac connects. True when the thread was not queued yet. */
  async enqueue(thread: string, requests: Request_[]): Promise<boolean> {
    const queue = (await this.ctx.storage.get<Record<string, Request_[]>>("queue")) ?? {};
    const isNew = !(thread in queue);
    const known = new Set((queue[thread] ?? []).map(([ts]) => ts));
    queue[thread] = [...(queue[thread] ?? []), ...requests.filter(([ts]) => !known.has(ts))];
    await this.ctx.storage.put("queue", queue);
    return isNew;
  }

  /** Pairing finished: this token now identifies the teammate's Mac (any older Mac is disconnected). */
  async setToken(user: string, token: string, profile: Profile): Promise<void> {
    await this.ctx.storage.put({ user, token, profile });
    for (const ws of this.authed()) ws.close(1000, "paired elsewhere");
  }

  async checkToken(token: string): Promise<boolean> {
    const expected = await this.ctx.storage.get<string>("token");
    return Boolean(expected) && timingSafeEqual(expected!, token);
  }

  async isConnected(): Promise<boolean> {
    return this.authed().length > 0;
  }
}
