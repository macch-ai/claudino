// Runs Claude Code headless on this Mac, with the user's own claude.ai login (Claude Team seat).
import { execFile } from "node:child_process";
import os from "node:os";
import path from "node:path";

const EXTRA_PATH = [path.join(os.homedir(), ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin"];
const TIMEOUT_MS = 50 * 60 * 1000;
export const WORKDIR = path.join(os.homedir(), "code");

export interface ClaudeResult {
  result: string;
  isError: boolean;
  sessionId?: string;
}

export interface AskOptions {
  sessionId?: string;
  system?: string; // appended to Claude Code's system prompt
  cwd?: string;
  permissionMode?: "auto" | "default";
}

// Apps launched from Finder get a minimal PATH, so add where `claude` is usually installed.
// ANTHROPIC_API_KEY is dropped so the CLI never bills an API key instead of the Team seat.
function env(): NodeJS.ProcessEnv {
  const { ANTHROPIC_API_KEY, ...rest } = process.env;
  return { ...rest, PATH: [...EXTRA_PATH, rest.PATH ?? "/usr/bin:/bin"].join(":") };
}

function run(args: string[], cwd = os.homedir()): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile("claude", args, { cwd, env: env(), timeout: TIMEOUT_MS, maxBuffer: 32 * 1024 * 1024 }, (err, stdout, stderr) => {
      if (err && !stdout) reject(new Error(`claude failed: ${(stderr || err.message).slice(-500)}`));
      else resolve(stdout);
    });
  });
}

export async function authStatus(): Promise<Record<string, unknown>> {
  return JSON.parse(await run(["auth", "status"]));
}

export async function logout(): Promise<void> {
  await run(["auth", "logout"]);
}

export async function ask(prompt: string, opts: AskOptions = {}): Promise<ClaudeResult> {
  const args = ["-p", prompt, "--output-format", "json"];
  if (opts.permissionMode) args.push("--permission-mode", opts.permissionMode);
  if (opts.system) args.push("--append-system-prompt", opts.system);
  if (opts.sessionId) args.push("--resume", opts.sessionId);
  const out = await run(args, opts.cwd);
  let data: Record<string, unknown>;
  try {
    data = JSON.parse(out);
  } catch {
    throw new Error(`claude returned no JSON: ${out.slice(-500)}`);
  }
  return {
    result: String(data.result ?? ""),
    isError: Boolean(data.is_error),
    sessionId: typeof data.session_id === "string" ? data.session_id : undefined,
  };
}
