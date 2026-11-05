// Pairing: an unlinked desktop app asks for a code and waits (hibernating WebSocket) until its teammate confirms it,
// with Sign in with Slack in the browser or "@Machine AI vincular <code>" in Slack.
//   app -> {"type":"pair","device":"MacBook de Salvador"}
//   <- {"type":"pair_code","code":"4F7-K2Q","expires_in":600,"login_url":"https://…/login?code=4F7K2Q"}
//   <- {"type":"paired","user","token","name","handle","avatar"}   (then closes)
import { DurableObject } from "cloudflare:workers";
import { type Env, userStub } from "./env";
import type { Profile } from "./user";

const TTL_MS = 10 * 60 * 1000;
const MAX_PENDING = 20;
const ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"; // no look-alikes (0/O, 1/I/L)

interface Pending {
  device: string;
  expires: number;
  nonce: string;
}

export function normalizeCode(code: string): string {
  return (code ?? "").toUpperCase().replace(/[^A-Z0-9]/g, "");
}

function randomString(alphabet: string, length: number): string {
  const bytes = crypto.getRandomValues(new Uint8Array(length));
  return [...bytes].map((b) => alphabet[b % alphabet.length]).join("");
}

export function newToken(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export class PairingDO extends DurableObject<Env> {
  private async codes(): Promise<Record<string, Pending>> {
    const all = (await this.ctx.storage.get<Record<string, Pending>>("codes")) ?? {};
    const now = Date.now();
    for (const [code, p] of Object.entries(all)) if (p.expires < now) delete all[code];
    return all;
  }

  async fetch(request: Request): Promise<Response> {
    const [client, server] = Object.values(new WebSocketPair());
    this.ctx.acceptWebSocket(server);
    server.serializeAttachment({ origin: new URL(request.url).origin });
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws: WebSocket, raw: string | ArrayBuffer): Promise<void> {
    let msg: any;
    try {
      msg = JSON.parse(typeof raw === "string" ? raw : new TextDecoder().decode(raw));
    } catch {
      return;
    }
    const attachment = ws.deserializeAttachment() ?? {};
    if (msg.type !== "pair" || attachment.code) return;
    const codes = await this.codes();
    if (Object.keys(codes).length >= MAX_PENDING) {
      ws.send(JSON.stringify({ type: "pair_unavailable" }));
      ws.close(1013, "busy");
      return;
    }
    const code = randomString(ALPHABET, 6);
    codes[code] = { device: String(msg.device || "una Mac").slice(0, 80), expires: Date.now() + TTL_MS, nonce: newToken() };
    await this.ctx.storage.put("codes", codes);
    ws.serializeAttachment({ ...attachment, code });
    const reply: Record<string, unknown> = { type: "pair_code", code: `${code.slice(0, 3)}-${code.slice(3)}`, expires_in: TTL_MS / 1000 };
    if (this.env.SLACK_CLIENT_ID && this.env.SLACK_CLIENT_SECRET) reply.login_url = `${attachment.origin}/login?code=${code}`;
    ws.send(JSON.stringify(reply));
    if ((await this.ctx.storage.getAlarm()) === null) await this.ctx.storage.setAlarm(Date.now() + TTL_MS);
  }

  async webSocketClose(ws: WebSocket): Promise<void> {
    const code = ws.deserializeAttachment()?.code;
    if (!code) return;
    const codes = await this.codes();
    delete codes[code];
    await this.ctx.storage.put("codes", codes);
  }

  /** Close the apps whose code expired. */
  async alarm(): Promise<void> {
    const codes = await this.codes();
    await this.ctx.storage.put("codes", codes);
    for (const ws of this.ctx.getWebSockets()) {
      const code = ws.deserializeAttachment()?.code;
      if (code && !codes[code]) ws.close(1000, "expired");
    }
    if (Object.keys(codes).length) await this.ctx.storage.setAlarm(Math.min(...Object.values(codes).map((p) => p.expires)) + 1000);
  }

  async pending(code: string): Promise<{ device: string; nonce: string } | null> {
    const p = (await this.codes())[normalizeCode(code)];
    return p ? { device: p.device, nonce: p.nonce } : null;
  }

  /** The teammate confirmed: give the waiting app a token for them. False when the code is unknown or expired. */
  async complete(code: string, user: string, profile: Profile): Promise<boolean> {
    code = normalizeCode(code);
    const codes = await this.codes();
    if (!codes[code]) return false;
    const ws = this.ctx.getWebSockets().find((w) => w.deserializeAttachment()?.code === code);
    delete codes[code];
    await this.ctx.storage.put("codes", codes);
    if (!ws) return false;
    const token = newToken();
    await userStub(this.env, user).setToken(user, token, profile);
    ws.send(JSON.stringify({ type: "paired", user, token, ...profile }));
    ws.close(1000, "paired");
    console.log(`desktop app paired for ${user}`);
    return true;
  }
}
