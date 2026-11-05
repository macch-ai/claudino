// One Durable Object per Slack thread: one run at a time, requests that arrive meanwhile are queued and handled
// together by the next run, and each run goes to the first requester's Mac (ported from relay.py's work/reply_done).
import { DurableObject } from "cloudflare:workers";
import INSTRUCTIONS from "../../machine_ai_instructions.md";
import { type Env, type Request_, userStub } from "./env";
import { type Ids, threadPrompt } from "./logic";
import { botUserId, post, replies } from "./slack";

const BUSY_TIMEOUT_MS = 60 * 60 * 1000; // a run that never answered stops blocking its thread
const MAX_AGENT_TURNS = 3; // delegate-agent replies handled per human request (no endless agent ping-pong)

interface Busy {
  id: string;
  owner: string;
  since: number;
  requests: Request_[];
  attempt: number;
  lastSeen: string;
}

interface State {
  channel: string;
  threadTs: string;
  seenTs: string; // last message the owner's session already saw
  active: boolean; // claudino took part in this thread
  busy: Busy | null;
  pending: Request_[];
  // Machine AI's last answer mentioned the delegate agent (claudino): its reply continues this owner's request.
  waitingFor?: { owner: string } | null;
  agentTurns?: number; // delegate replies handled since the last human request
}

export async function ids(env: Env): Promise<Ids> {
  return { bot: await botUserId(env), appId: env.RELAY_APP_ID, claude: env.CLAUDE_USER_ID,
           sentry: env.SENTRY_USER_ID, alertChannel: env.ALERT_CHANNEL_ID };
}

function mentions(requests: Request_[]): string {
  return [...new Set(requests.map(([, u]) => `<@${u}>`))].sort().join(" ");
}

export class ThreadDO extends DurableObject<Env> {
  private async state(channel = "", threadTs = ""): Promise<State> {
    return (await this.ctx.storage.get<State>("state"))
      ?? { channel, threadTs, seenTs: "0", active: false, busy: null, pending: [] };
  }

  private async save(state: State): Promise<void> {
    await this.ctx.storage.put("state", state);
  }

  private key(s: State): string {
    return `${s.channel}:${s.threadTs}`;
  }

  async isActive(): Promise<boolean> {
    return (await this.state()).active;
  }

  async markActive(channel: string, threadTs: string): Promise<void> {
    const s = await this.state(channel, threadTs);
    s.active = true;
    await this.save(s);
  }

  /** A new request in this thread (from Slack) or requests that waited for the owner's Mac. */
  async request(channel: string, threadTs: string, requests: Request_[]): Promise<void> {
    const s = await this.state(channel, threadTs);
    s.waitingFor = null; // a human spoke: any delegation is over and the agent-turn budget restarts
    s.agentTurns = 0;
    await this.enqueueOrStart(s, requests);
  }

  /** The delegate agent answered in this thread: run its reply on the waiting owner's Mac, or ignore it. */
  async agentReply(ts: string): Promise<boolean> {
    const s = await this.state();
    if (!s.waitingFor || (s.agentTurns ?? 0) >= MAX_AGENT_TURNS) return false;
    const owner = s.waitingFor.owner;
    s.waitingFor = null;
    s.agentTurns = (s.agentTurns ?? 0) + 1;
    console.log(`thread ${this.key(s)}: agent reply ${ts} continues ${owner}'s request (turn ${s.agentTurns})`);
    await this.enqueueOrStart(s, [[ts, owner]]);
    return true;
  }

  private async enqueueOrStart(s: State, requests: Request_[]): Promise<void> {
    s.active = true;
    if (s.busy && Date.now() - s.busy.since < BUSY_TIMEOUT_MS) {
      s.pending.push(...requests.filter(([ts]) => !s.pending.some(([p]) => p === ts)));
      await this.save(s);
      return;
    }
    await this.start(s, requests, 0);
  }

  async resume(requests: Request_[]): Promise<void> {
    await this.enqueueOrStart(await this.state(), requests); // keeps any delegation state
  }

  private async start(s: State, requests: Request_[], attempt: number): Promise<void> {
    const owner = requests[0][1];
    const id = crypto.randomUUID();
    // Mark busy before any await, so a request arriving meanwhile is queued instead of starting a second run.
    s.busy = { id, owner, since: Date.now(), requests, attempt, lastSeen: s.seenTs };
    await this.save(s);
    try {
      const messages = await replies(this.env, s.channel, s.threadTs);
      const { prompt, lastSeen, files } = threadPrompt(s.channel, s.threadTs, messages, s.seenTs, requests, await ids(this.env));
      s.busy.lastSeen = lastSeen;
      await this.save(s);
      const sent = await userStub(this.env, owner).startRun(id, this.key(s), prompt, INSTRUCTIONS, files);
      if (sent === "sent") {
        await this.ctx.storage.setAlarm(Date.now() + BUSY_TIMEOUT_MS);
        return;
      }
      // The owner's Mac is off: the thread (and anything queued here) waits in their queue.
      const waiting = [...requests, ...s.pending];
      s.busy = null;
      s.pending = [];
      await this.save(s);
      if (await userStub(this.env, owner).enqueue(this.key(s), waiting)) {
        await post(this.env, s.channel, s.threadTs, `<@${owner}> tu Mac está apagada o sin la app de MACCH: lo hago apenas se conecte.`);
      }
    } catch (error) {
      console.error(`thread ${this.key(s)} failed to start: ${error}`);
      await post(this.env, s.channel, s.threadTs, `${mentions(requests)} no pude preparar el pedido: \`${String(error).slice(0, 300)}\``);
      await this.finish(await this.state(), id);
    }
  }

  /** The owner's Mac answered. */
  async complete(id: string, result: string, isError: boolean): Promise<void> {
    const s = await this.state();
    if (s.busy?.id !== id) return;
    s.seenTs = s.busy.lastSeen;
    await this.save(s);
    console.log(`thread ${this.key(s)} answered on ${s.busy.owner}'s Mac (error=${isError})`);
    // Delegated: the answer asks the delegate agent, so its reply (not a human) continues the request.
    s.waitingFor = !isError && result.includes(`<@${this.env.AGENT_USER_ID}>`) ? { owner: s.busy.owner } : null;
    await this.save(s);
    await post(this.env, s.channel, s.threadTs, result || `${mentions(s.busy.requests)} la sesión terminó sin respuesta.`);
    await this.finish(s, id);
  }

  /** The owner's Mac could not run it: retry once with the whole thread in a new session, then report. */
  async failed(id: string, error: string): Promise<void> {
    const s = await this.state();
    if (s.busy?.id !== id) return;
    const busy = s.busy;
    if (busy.attempt === 0) {
      console.warn(`thread ${this.key(s)} failed, retrying with a new session: ${error}`);
      s.seenTs = "0"; // the app drops a failed session
      await this.start(s, busy.requests, 1);
      return;
    }
    await post(this.env, s.channel, s.threadTs,
      `${mentions(busy.requests)} falló la sesión de Claude en la Mac de <@${busy.owner}>: \`${error.slice(0, 300)}\``);
    await this.finish(s, id);
  }

  /** The run is over: start the requests that arrived after what it saw. */
  private async finish(s: State, id: string): Promise<void> {
    if (s.busy && s.busy.id !== id) return;
    const seenUntil = Number(s.busy?.lastSeen ?? s.seenTs);
    const pending = s.pending.filter(([ts]) => Number(ts) > seenUntil);
    s.busy = null;
    s.pending = [];
    await this.save(s);
    await this.ctx.storage.deleteAlarm();
    if (pending.length) await this.start(s, pending, 0);
  }

  async alarm(): Promise<void> {
    const s = await this.state();
    if (s.busy && Date.now() - s.busy.since >= BUSY_TIMEOUT_MS) {
      console.warn(`thread ${this.key(s)}: run ${s.busy.id} never answered, unblocking`);
      await this.finish(s, s.busy.id);
    }
  }
}
