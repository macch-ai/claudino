// Pure rules ported from relay.py: who is talking to claudino, commands, and the prompt for a thread.
import type { Request_ } from "./env";

export interface SlackFile {
  id: string;
  name?: string;
  mimetype?: string;
}

export interface SlackMessage {
  ts: string;
  thread_ts?: string;
  user?: string;
  bot_id?: string;
  app_id?: string;
  subtype?: string;
  text?: string;
  channel?: string;
  files?: SlackFile[];
  attachments?: { author_name?: string; author_subname?: string; channel_name?: string; text?: string; fallback?: string; files?: SlackFile[] }[];
  user_profile?: { real_name?: string; display_name?: string; name?: string; image_72?: string };
}

/** A file the Mac downloads through the Worker before running; the prompt has {{file:<id>}} where it goes. */
export interface FileRef {
  id: string;
  ts: string;
  name: string;
  mimetype: string;
}

export interface Ids {
  bot: string; // claudino's bot user
  appId: string;
  claude: string; // Claude Tag
  sentry: string;
  alertChannel: string;
}

export function isSentryAlert(m: SlackMessage, ids: Ids): boolean {
  const isReply = m.thread_ts !== undefined && m.thread_ts !== m.ts;
  return m.user === ids.sentry && !isReply;
}

export function isFromClaudino(m: SlackMessage, ids: Ids): boolean {
  return m.user === ids.bot || m.app_id === ids.appId;
}

/** "yes" / "no", or "thread" when it depends on whether claudino already took part in the thread. */
export function addressed(m: SlackMessage, ids: Ids): "yes" | "no" | "thread" {
  const text = m.text ?? "";
  if (text.includes(`<@${ids.bot}>`)) return "yes";
  if (m.channel === ids.alertChannel && text.includes(`<@${ids.claude}>`)) return "yes";
  if (!m.thread_ts || m.thread_ts === m.ts) return "no";
  const others = [...text.matchAll(/<@([UW][A-Z0-9]+)/g)].map((x) => x[1]).filter((u) => u !== ids.bot && u !== ids.claude);
  if (others.length) return "no"; // talking to someone else ("Sent using @Claude" footers don't count)
  return "thread";
}

export function withoutMentions(text: string): string {
  return (text ?? "").replace(/<@[A-Z0-9]+(\|[^>]*)?>/g, "").trim();
}

export function pairCommand(text: string): string | null {
  const m = withoutMentions(text).match(/^vincular\s+`?([A-Za-z0-9]{3}-?[A-Za-z0-9]{3})`?\.?$/i);
  return m ? m[1] : null;
}

export function automationCommand(text: string): string | null {
  const m = withoutMentions(text).match(/^ejecut[aá]\s+([\s\S]+)$/i);
  if (!m) return null;
  return m[1].trim().replace(/^[\s`"'*_.]+|[\s`"'*_.]+$/g, "") || null;
}

function fileRef(f: SlackFile, ts: string): FileRef {
  return { id: f.id, ts, name: f.name || f.id, mimetype: f.mimetype || "?" };
}

/** A message's text plus its files (as placeholders) and forwarded/shared messages. */
export function render(m: SlackMessage, files: FileRef[]): string {
  const parts = [m.text ?? ""];
  for (const f of m.files ?? []) {
    files.push(fileRef(f, m.ts));
    parts.push(`{{file:${f.id}}}`);
  }
  for (const a of m.attachments ?? []) {
    const who = a.author_name || a.author_subname || a.channel_name || "";
    const body = a.text || a.fallback || "";
    if (body || who) parts.push(`[forwarded/shared message from ${who}]: ${body}`);
    for (const f of a.files ?? []) {
      files.push(fileRef(f, m.ts));
      parts.push(`{{file:${f.id}}}`);
    }
  }
  return parts.filter(Boolean).join("\n");
}

/** The thread messages the session has not seen yet, plus who is asking for what. */
export function threadPrompt(
  channel: string, threadTs: string, messages: SlackMessage[], seenTs: string, requests: Request_[], ids: Ids,
): { prompt: string; lastSeen: string; files: FileRef[] } {
  const fresh = seenTs === "0";
  const files: FileRef[] = [];
  const lines = messages
    .filter((m) => Number(m.ts) > Number(seenTs) && (fresh || !isFromClaudino(m, ids)))
    .map((m) => {
      const author = isFromClaudino(m, ids) ? "Machine AI (you)" : `<@${m.user || m.bot_id}>`;
      return `[${m.ts}] ${author}: ${render(m, files)}`;
    });
  const requesters = [...new Set(requests.map(([, u]) => `<@${u}>`))].sort().join(" ");
  const header = `Slack channel ${channel}, thread ${threadTs}. Requesters: ${requesters}. `
    + `Requests: the messages with ts ${requests.map(([ts]) => ts).join(", ")}.\n`
    + (fresh ? "Thread so far:\n" : "New messages in the thread since your last answer:\n");
  const lastSeen = [...messages.map((m) => m.ts), seenTs].reduce((a, b) => (Number(b) > Number(a) ? b : a));
  return { prompt: header + lines.join("\n"), lastSeen, files };
}

const EMOJI_PROMPT =
  "You are Machine AI, an engineering assistant in Macch's Slack. A teammate just sent you the message below. "
  + "Pick the one Slack emoji reaction a thoughtful teammate would add to show they saw it and how it lands "
  + "(for example: eyes, saluting_face, rocket, fire, rotating_light, thinking_face, mag, ok_hand, muscle, "
  + "white_check_mark, pray, joy, sweat_smile, hammer_and_wrench, chart_with_upwards_trend, coffee). "
  + "Any emoji from Slack's standard set is fine. Reply with only its short name, without colons.\n\nMessage:\n";

export function emojiPrompt(text: string, custom: string[]): string {
  const cleaned = (text ?? "").replace(/\s*\*?Sent using\*?\s*<@[A-Z0-9]+(\|[^>]*)?>\s*$/, "").trim().slice(0, 2000);
  const extra = custom.length
    ? `This workspace also has custom emoji you can use (prefer one when it fits well): ${custom.join(", ")}. `
    : "";
  return EMOJI_PROMPT.replace("Reply with only", extra + "Reply with only") + cleaned;
}

export function validEmoji(name: string): string | null {
  const n = (name ?? "").trim().replace(/^:|:$/g, "").toLowerCase();
  return /^[a-z0-9_+\-']{1,60}$/.test(n) ? n : null;
}

export function threadKey(channel: string, threadTs: string): string {
  return `${channel}:${threadTs}`;
}
