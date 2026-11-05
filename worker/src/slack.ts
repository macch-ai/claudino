// Slack Web API calls and request signature checks.
import type { Env } from "./env";

const encoder = new TextEncoder();

async function hmacHex(secret: string, data: string): Promise<string> {
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, encoder.encode(data));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** Slack's v0 signature: only requests Slack signed in the last 5 minutes are accepted. */
export async function verifySlack(secret: string, timestamp: string | null, signature: string | null, body: string,
                                  now = Date.now()): Promise<boolean> {
  if (!timestamp || !signature || !secret) return false;
  if (Math.abs(now / 1000 - Number(timestamp)) > 300) return false;
  const expected = "v0=" + (await hmacHex(secret, `v0:${timestamp}:${body}`));
  return timingSafeEqual(expected, signature);
}

export class SlackError extends Error {}

export async function slack(env: Env, method: string, params: Record<string, unknown>): Promise<any> {
  const resp = await fetch(`https://slack.com/api/${method}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.SLACK_BOT_TOKEN}`, "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify(params),
  });
  const data: any = await resp.json();
  if (!data.ok) throw new SlackError(`${method}: ${data.error}`);
  return data;
}

let botUser: string | null = null;

/** claudino's own bot user ID (cached per isolate). */
export async function botUserId(env: Env): Promise<string> {
  if (!botUser) botUser = (await slack(env, "auth.test", {})).user_id as string;
  return botUser;
}

export async function post(env: Env, channel: string, threadTs: string | undefined, text: string): Promise<void> {
  try {
    await slack(env, "chat.postMessage", { channel, thread_ts: threadTs, text: text.slice(0, 39000), unfurl_links: false });
  } catch (error) {
    console.error(`post in ${channel}:${threadTs} failed: ${error}`);
  }
}

export async function react(env: Env, channel: string, ts: string, name: string): Promise<boolean> {
  try {
    await slack(env, "reactions.add", { channel, timestamp: ts, name });
    return true;
  } catch (error) {
    if (String(error).includes("already_reacted")) return true;
    console.warn(`reaction ${name} on ${ts} failed: ${error}`);
    return false;
  }
}

let customEmoji: { names: string[]; at: number } = { names: [], at: 0 };

/** The workspace's custom emoji names, refreshed at most once an hour. */
export async function customEmojiNames(env: Env): Promise<string[]> {
  if (Date.now() - customEmoji.at > 3600_000) {
    try {
      customEmoji = { names: Object.keys((await slack(env, "emoji.list", {})).emoji).sort(), at: Date.now() };
    } catch (error) {
      customEmoji = { ...customEmoji, at: Date.now() };
      console.warn(`emoji.list failed: ${error}`);
    }
  }
  return customEmoji.names;
}

export async function replies(env: Env, channel: string, ts: string, limit = 200): Promise<any[]> {
  const params = new URLSearchParams({ channel, ts, limit: String(limit) });
  const resp = await fetch(`https://slack.com/api/conversations.replies?${params}`, {
    headers: { Authorization: `Bearer ${env.SLACK_BOT_TOKEN}` },
  });
  const data: any = await resp.json();
  if (!data.ok) throw new SlackError(`conversations.replies: ${data.error}`);
  return data.messages;
}
