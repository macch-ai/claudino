import type { PairingDO } from "./pairing";
import type { ThreadDO } from "./thread";
import type { UserDO } from "./user";

export interface Env {
  USERS: DurableObjectNamespace<UserDO>;
  THREADS: DurableObjectNamespace<ThreadDO>;
  PAIRING: DurableObjectNamespace<PairingDO>;
  ALERT_CHANNEL_ID: string;
  SENTRY_USER_ID: string;
  CLAUDE_USER_ID: string;
  RELAY_APP_ID: string;
  ALLOWED_USERS: string; // comma separated
  AGENT_APP_ID: string; // another agent's app Machine AI delegates to (claudino)
  AGENT_USER_ID: string; // that agent's bot user
  ROUTINE_FIRE_URL: string;
  SLACK_BOT_TOKEN: string;
  SLACK_SIGNING_SECRET: string;
  SLACK_CLIENT_ID?: string;
  SLACK_CLIENT_SECRET?: string;
  ROUTINE_TOKEN?: string;
}

export type Request_ = [ts: string, user: string]; // one Slack message asking claudino for something

export function allowedUsers(env: Env): Set<string> {
  return new Set(env.ALLOWED_USERS.split(",").map((u) => u.trim()).filter(Boolean));
}

export function userStub(env: Env, user: string) {
  return env.USERS.get(env.USERS.idFromName(user));
}

export function threadStub(env: Env, key: string) {
  return env.THREADS.get(env.THREADS.idFromName(key));
}

export function pairingStub(env: Env) {
  return env.PAIRING.get(env.PAIRING.idFromName("pairing"));
}
