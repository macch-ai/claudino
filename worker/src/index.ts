// claudino on Cloudflare Workers: Slack events in, each request out to the requester's own Mac.
//   POST /slack/events   Slack Events API (signed)          GET /ws?user=U…   desktop app (UserDO)
//   GET  /pair           unlinked desktop app (PairingDO)   GET /login…       Sign in with Slack pairing pages
//   GET  /files/:id      Slack file for a linked Mac        GET /health
import { allowedUsers, type Env, pairingStub, threadStub, userStub } from "./env";
import { addressed, automationCommand, emojiPrompt, isFromClaudino, isSentryAlert, pairCommand, type SlackMessage, threadKey } from "./logic";
import { normalizeCode } from "./pairing";
import { customEmojiNames, post, replies, slack, verifySlack } from "./slack";
import { ids } from "./thread";

export { PairingDO } from "./pairing";
export { ThreadDO } from "./thread";
export { UserDO } from "./user";

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const upgrade = request.headers.get("Upgrade")?.toLowerCase() === "websocket";
    if (url.pathname === "/health") return new Response("ok\n");
    if (url.pathname === "/slack/events" && request.method === "POST") return slackEvents(request, env, ctx);
    if (url.pathname === "/ws" && upgrade) {
      const user = url.searchParams.get("user") ?? "";
      if (!/^[UW][A-Z0-9]+$/.test(user)) return new Response("bad user", { status: 400 });
      return userStub(env, user).fetch(request);
    }
    if (url.pathname === "/pair" && upgrade) return pairingStub(env).fetch(request);
    if (url.pathname.startsWith("/login")) return login(url, env);
    if (url.pathname.startsWith("/files/")) return file(url.pathname.slice(7), request, env);
    return new Response("not found", { status: 404 });
  },
} satisfies ExportedHandler<Env>;

// ---- Slack events -----------------------------------------------------------------------------------------------

async function slackEvents(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  const body = await request.text();
  const ok = await verifySlack(env.SLACK_SIGNING_SECRET, request.headers.get("X-Slack-Request-Timestamp"),
                               request.headers.get("X-Slack-Signature"), body);
  if (!ok) return new Response("bad signature", { status: 401 });
  const payload = JSON.parse(body);
  if (payload.type === "url_verification") return Response.json({ challenge: payload.challenge });
  // Slack retries when we answer late; the first delivery is already being handled.
  if (request.headers.get("X-Slack-Retry-Num")) return new Response("ok");
  if (payload.type === "event_callback" && payload.event?.type === "message") {
    ctx.waitUntil(onMessage(env, payload.event).catch((error) => console.error(`event ${payload.event_id} failed: ${error}`)));
  } else {
    console.log(`ignored ${payload.type}/${payload.event?.type}`);
  }
  return new Response("ok");
}

export async function onMessage(env: Env, event: SlackMessage): Promise<void> {
  const i = await ids(env);
  const channel = event.channel ?? "";
  if (channel === i.alertChannel && isSentryAlert(event, i)) return handOver(env, event.ts);
  if (isFromClaudino(event, i)) return console.log(`skip ${event.ts}: from the bot itself (user=${event.user} app=${event.app_id})`);
  // The delegate agent (claudino) answering Machine AI: continues the request only if this thread is waiting for it.
  if (event.app_id === env.AGENT_APP_ID && event.thread_ts && (event.text ?? "").includes(`<@${i.bot}>`)
      && (!event.subtype || event.subtype === "bot_message")) {
    const taken = await threadStub(env, threadKey(channel, event.thread_ts)).agentReply(event.ts);
    return console.log(`agent reply ${event.ts}: ${taken ? "continues the request" : "not waited for, ignored"}`);
  }
  if (event.subtype || event.bot_id || !event.user) return console.log(`skip ${event.ts}: subtype=${event.subtype} bot=${event.bot_id}`);
  const threadTs = event.thread_ts ?? event.ts;
  let to = addressed(event, i);
  if (to === "thread") to = (await tookPart(env, channel, threadTs)) ? "yes" : "no";
  if (to !== "yes") return console.log(`skip ${event.ts}: not for the bot (${i.bot}); mentions: ${(event.text ?? "").match(/<@[A-Z0-9]+/g)}`);
  const user = event.user;
  if (!allowedUsers(env).has(user)) {
    console.log(`skip ${event.ts} from ${user}: not allowed`);
    return;
  }
  const text = event.text ?? "";
  const code = pairCommand(text);
  if (code) {
    const p = event.user_profile ?? {};
    const ok = await pairingStub(env).complete(code, user, {
      name: p.real_name || p.display_name || "", handle: p.display_name || p.name || "", avatar: p.image_72 || "",
    });
    return post(env, channel, threadTs, ok ? `<@${user}> listo: tu Mac quedó vinculada a Machine AI.`
      : `<@${user}> ese código no existe o venció: pedí uno nuevo en la app (Conexión).`);
  }
  await userStub(env, user).pickEmoji(crypto.randomUUID(), channel, event.ts, emojiPrompt(text, await customEmojiNames(env)));
  const name = automationCommand(text);
  if (name) {
    const sent = await userStub(env, user).runAutomation(crypto.randomUUID(), name, channel, threadTs);
    if (sent === "offline") await post(env, channel, threadTs, `<@${user}> tu Mac está apagada o sin la app de MACCH: prendela y volvé a pedir *${name}*.`);
    return;
  }
  await threadStub(env, threadKey(channel, threadTs)).request(channel, threadTs, [[event.ts, user]]);
}

/** claudino already took part in the thread: remembered, or found in Slack (it was mentioned or answered). */
async function tookPart(env: Env, channel: string, threadTs: string): Promise<boolean> {
  const thread = threadStub(env, threadKey(channel, threadTs));
  if (await thread.isActive()) return true;
  const i = await ids(env);
  try {
    const seen = (await replies(env, channel, threadTs)).some((m) => isFromClaudino(m, i) || (m.text ?? "").includes(`<@${i.bot}>`));
    if (seen) await thread.markActive(channel, threadTs);
    return seen;
  } catch (error) {
    console.warn(`read thread ${threadTs} failed: ${error}`);
    return false;
  }
}

/** A new Sentry alert: fire the Claude Code routine that investigates it and replies in its thread. */
async function handOver(env: Env, ts: string): Promise<void> {
  if (!env.ROUTINE_TOKEN) return console.warn(`alert ${ts}: no ROUTINE_TOKEN, not handed over`);
  await threadStub(env, threadKey(env.ALERT_CHANNEL_ID, ts)).markActive(env.ALERT_CHANNEL_ID, ts);
  const resp = await fetch(env.ROUTINE_FIRE_URL, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.ROUTINE_TOKEN}`, "anthropic-beta": "experimental-cc-routine-2026-04-01",
               "anthropic-version": "2023-06-01", "Content-Type": "application/json" },
    body: JSON.stringify({ text: `alert_ts=${ts}\nchannel=${env.ALERT_CHANNEL_ID}\nA new Sentry alert was posted at the top level `
      + "of #production-errors. Read it and its thread with the Slack connector." }),
  });
  console.log(`fired Claude task for ${ts}: ${resp.status}`);
}

// ---- Files: a linked Mac downloads a thread's Slack files through here (the bot token never leaves Cloudflare) ----

async function file(id: string, request: Request, env: Env): Promise<Response> {
  const [user, token] = (request.headers.get("Authorization") ?? "").replace(/^Bearer /, "").split(":");
  if (!user || !token || !(await userStub(env, user).checkToken(token))) return new Response("unauthorized", { status: 401 });
  try {
    const info = (await slack(env, "files.info", { file: id })).file;
    const src = info.url_private_download || info.url_private;
    const resp = await fetch(src, { headers: { Authorization: `Bearer ${env.SLACK_BOT_TOKEN}` } });
    if ((resp.headers.get("Content-Type") ?? "").includes("text/html")) return new Response("slack refused the download", { status: 502 });
    return new Response(resp.body, { headers: { "Content-Type": info.mimetype || "application/octet-stream" } });
  } catch (error) {
    return new Response(`file ${id}: ${error}`, { status: 502 });
  }
}

// ---- Sign in with Slack (OpenID Connect) for pairing ------------------------------------------------------------

const PAGE = (title: string, body: string) => `<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>MACCH · Machine AI</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#161616;color:#f2f2f2;
font:15px -apple-system,BlinkMacSystemFont,sans-serif}main{max-width:420px;padding:32px;background:#1e1e1e;
border:1px solid #2e2e2e;border-radius:16px}h1{font-size:19px;margin:0 0 12px}p{color:#9a9a9a;line-height:1.5}
a.btn{display:inline-block;margin-top:8px;padding:10px 16px;border-radius:9px;background:#d97757;color:#fff;
text-decoration:none}b{color:#f2f2f2}</style></head><body><main><h1>${escape(title)}</h1>${body}</main></body></html>`;

function page(status: number, title: string, body: string): Response {
  return new Response(PAGE(title, body), { status, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });
}

function escape(text: string): string {
  return (text ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

async function login(url: URL, env: Env): Promise<Response> {
  if (!env.SLACK_CLIENT_ID || !env.SLACK_CLIENT_SECRET) return page(404, "No encontrado", "<p>Esta dirección no existe.</p>");
  const expired = page(410, "El código venció", "<p>Volvé a la app de MACCH y pedí uno nuevo.</p>");
  const redirectUri = `${url.origin}/login/callback`;
  const q = url.searchParams;
  if (url.pathname === "/login") {
    const code = normalizeCode(q.get("code") ?? "");
    const pending = await pairingStub(env).pending(code);
    if (!pending) return expired;
    // Confirm before Slack: someone could send you *their* link to get *their* Mac linked to your user.
    return page(200, "Vincular esta Mac", `<p>Vas a vincular <b>${escape(pending.device)}</b> con tu cuenta de Slack. Los pedidos `
      + "que le hagas a @Machine AI van a correr en esa Mac, con su cuenta de Claude.</p>"
      + "<p>Si no acabás de abrir la app de MACCH en tu Mac, cerrá esta página.</p>"
      + `<a class="btn" href="/login/slack?code=${code}">Continuar con Slack</a>`);
  }
  if (url.pathname === "/login/slack") {
    const code = normalizeCode(q.get("code") ?? "");
    const pending = await pairingStub(env).pending(code);
    if (!pending) return expired;
    const team = (await slack(env, "auth.test", {})).team_id;
    const params = new URLSearchParams({ response_type: "code", scope: "openid profile", client_id: env.SLACK_CLIENT_ID,
                                         redirect_uri: redirectUri, state: code, nonce: pending.nonce, team });
    return Response.redirect(`https://slack.com/openid/connect/authorize?${params}`, 302);
  }
  if (url.pathname === "/login/callback") {
    const code = normalizeCode(q.get("state") ?? "");
    if (!(await pairingStub(env).pending(code))) return expired;
    if (q.get("error") || !q.get("code")) return page(400, "No se vinculó", "<p>Slack no confirmó el inicio de sesión. Probá de nuevo desde la app.</p>");
    let who: { user: string; team: string; name: string; avatar: string };
    try {
      who = await slackProfile(env, q.get("code")!, redirectUri);
    } catch (error) {
      console.warn(`slack login failed: ${error}`);
      return page(502, "No se vinculó", "<p>No pude confirmar tu cuenta con Slack. Probá de nuevo desde la app.</p>");
    }
    const team = (await slack(env, "auth.test", {})).team_id;
    if (who.team !== team || !allowedUsers(env).has(who.user)) {
      console.warn(`slack login refused for ${who.user} (team ${who.team})`);
      return page(403, "Sin acceso", "<p>Tu cuenta de Slack no tiene acceso a Machine AI.</p>");
    }
    if (!(await pairingStub(env).complete(code, who.user, { name: who.name, avatar: who.avatar }))) return expired;
    return page(200, "Listo", `<p>Tu Mac quedó vinculada, ${escape(who.name)}. Ya podés volver a la app.</p>`);
  }
  return page(404, "No encontrado", "<p>Esta dirección no existe.</p>");
}

async function slackForm(url: string, data: Record<string, string>, token = ""): Promise<any> {
  const headers: Record<string, string> = { "Content-Type": "application/x-www-form-urlencoded" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const body: any = await (await fetch(url, { method: "POST", headers, body: new URLSearchParams(data) })).json();
  if (!body.ok) throw new Error(body.error ?? "slack_error");
  return body;
}

async function slackProfile(env: Env, code: string, redirectUri: string) {
  const tokens = await slackForm("https://slack.com/api/openid.connect.token",
    { client_id: env.SLACK_CLIENT_ID!, client_secret: env.SLACK_CLIENT_SECRET!, code, redirect_uri: redirectUri });
  const info = await slackForm("https://slack.com/api/openid.connect.userInfo", {}, tokens.access_token);
  return {
    user: info["https://slack.com/user_id"] ?? "", team: info["https://slack.com/team_id"] ?? "",
    name: info.name || info.given_name || "", avatar: info["https://slack.com/user_image_72"] || info.picture || "",
  };
}
