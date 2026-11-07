"""Stream a run's work into its Slack thread as it happens.

Claude's tool calls become Slack task cards (in progress, then done or failed) and the text it writes
between them streams in above them, through chat.startStream / appendStream / stopStream. The answer
itself is still posted as its own message once the run ends.

Slack ends a stream 10 minutes after it starts and caps it at 12,000 characters of text and 50 tasks,
so a long run moves on to a new stream message before it reaches any of them.
"""

import logging
import posixpath
import re
import threading
import time
import urllib.parse

from slack_sdk.errors import SlackApiError

log = logging.getLogger("claude-relay")

FLUSH_SECONDS = 1.0  # appendStream allows 100+ calls a minute; one a second keeps well under it
ROTATE_SECONDS = 9 * 60  # Slack finalizes a stream 10 minutes after it starts
MAX_TEXT = 11000  # of the 12,000 markdown characters a stream takes
MAX_TASKS = 45  # of the 50 tasks a stream shows
TITLE_LIMIT = 80

# Long opaque strings in a command line are most likely credentials: never send them to Slack.
SECRET = re.compile(r"\b(?:xox[a-z]-[\w-]+|gh[pousr]_\w+|sk-[\w-]{10,}|(?=[A-Za-z_\-+=]*\d)[A-Za-z0-9_\-+=]{32,})")


def redact(text: str) -> str:
    return SECRET.sub("[redacted]", text)


def short(text: str, limit: int = TITLE_LIMIT) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def task_title(name: str, args: dict) -> str:
    """What a tool call does, in a few words for its task card."""
    args = args if isinstance(args, dict) else {}
    path = posixpath.basename(str(args.get("file_path") or args.get("notebook_path") or args.get("path") or ""))
    if name == "Bash":
        return short(redact(args.get("description") or f"Run `{short(args.get('command', ''), 60)}`"))
    if name == "Read":
        return f"Read {path}" if path else "Read a file"
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return f"Edit {path}" if path else "Edit a file"
    if name in ("Grep", "Glob"):
        return short(redact(f"Search for `{short(args.get('pattern', ''), 50)}`"))
    if name == "WebFetch":
        return f"Read {urllib.parse.urlparse(str(args.get('url', ''))).netloc or 'a web page'}"
    if name == "WebSearch":
        return short(f"Search the web: {args.get('query', '')}")
    if name in ("Task", "Agent"):
        return short(f"Delegate: {args.get('description') or 'a subtask'}")
    if name.startswith("mcp__"):
        server, _, tool = name[5:].partition("__")
        return short(f"{server}: {tool.replace('_', ' ')}")
    return short(name)


MAX_STEP_LINES = 3  # latest steps the shimmer status rotates through


def step_titles(event: dict) -> list:
    """Task titles of the tool calls in one stream-json event, for the shimmer status (plain text, so no
    backticks). Subagent events and bookkeeping tools give none."""
    if event.get("type") != "assistant" or event.get("parent_tool_use_id"):
        return []
    content = (event.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [task_title(c.get("name", ""), c.get("input")).replace("`", "") for c in content
            if c.get("type") == "tool_use" and c.get("name") not in HIDDEN_TOOLS]


STREAM_MESSAGES = set()  # ts of every progress message: their message events are not answers
HIDDEN_TOOLS = {"TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "ToolSearch"}


class Progress:
    """The live progress of one run, as stream messages in its thread. Feed it Claude's stream-json
    events with on_event(); close() ends the stream. Slack errors are logged and turn it off."""

    def __init__(self, client, channel: str, thread_ts: str, recipient_user: str, recipient_team: str,
                 clock=time.monotonic):
        self.client, self.channel, self.thread_ts = client, channel, thread_ts
        self.recipient = {"recipient_user_id": recipient_user, "recipient_team_id": recipient_team}
        self.clock = clock
        self.lock = threading.Lock()
        self.ts = None  # the current stream message
        self.messages = []  # every stream message this run posted, oldest first
        self.started = 0.0
        self.text_chars = 0
        self.stream_tasks = set()  # tasks the current stream message shows
        self.tasks = {}  # tool_use id -> {"title": str, "status": str}
        self.queue = []  # chunks not sent yet
        self.broken = False
        self.done = threading.Event()
        self.flusher = None

    def start(self) -> None:
        self.flusher = threading.Thread(target=self._flush_loop, daemon=True)
        self.flusher.start()

    def on_event(self, event: dict) -> None:
        """One stream-json event: assistant turns open tasks and add text, tool results close tasks.
        Events of subagents (parent_tool_use_id) stay inside their parent's task."""
        if event.get("parent_tool_use_id"):
            return
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            return
        with self.lock:
            if event.get("type") == "assistant":
                calls = [c for c in content if c.get("type") == "tool_use" and c.get("name") not in HIDDEN_TOOLS]
                text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text").strip()
                # A turn without tool calls may be the answer, which gets its own message.
                if text and any(c.get("type") == "tool_use" for c in content):
                    self.queue.append({"type": "markdown_text", "text": text + "\n\n"})
                for call in calls:
                    self.tasks[call["id"]] = {"title": task_title(call.get("name", ""), call.get("input")),
                                              "status": "in_progress"}
                    self.queue.append(self._task(call["id"]))
            elif event.get("type") == "user":
                for c in content:
                    task = self.tasks.get(c.get("tool_use_id")) if c.get("type") == "tool_result" else None
                    if task:
                        task["status"] = "error" if c.get("is_error") else "complete"
                        self.queue.append(self._task(c["tool_use_id"]))

    def _task(self, task_id: str) -> dict:
        task = self.tasks[task_id]
        return {"type": "task_update", "id": task_id, "title": task["title"], "status": task["status"]}

    def _flush_loop(self) -> None:
        while not self.done.wait(FLUSH_SECONDS):
            self.flush()

    def flush(self, final: bool = False) -> None:
        with self.lock:
            if self.broken:
                return
            chunks, self.queue = self._dedupe(self.queue), []
            try:
                if self.ts and not final and (self.clock() - self.started > ROTATE_SECONDS or self._too_big(chunks)):
                    self._stop([])
                    open_tasks = [t for t in self.tasks if self.tasks[t]["status"] == "in_progress"]
                    chunks = self._dedupe([self._task(t) for t in open_tasks] + chunks)
                if final:
                    if self.ts:
                        self._stop(chunks)
                    return
                if not chunks:
                    return
                if not self.ts:
                    response = self.client.chat_startStream(channel=self.channel, thread_ts=self.thread_ts,
                                                            task_display_mode="timeline", chunks=chunks,
                                                            **self.recipient)
                    self.ts, self.started, self.text_chars, self.stream_tasks = response["ts"], self.clock(), 0, set()
                    STREAM_MESSAGES.add(self.ts)
                    self.messages.append(self.ts)
                else:
                    self.client.chat_appendStream(channel=self.channel, ts=self.ts, chunks=chunks)
                self._count(chunks)
            except SlackApiError as error:
                log.warning("progress stream in %s failed: %s", self.thread_ts, error.response.get("error"))
                self.broken = True

    def _stop(self, chunks: list) -> None:
        kwargs = {"chunks": chunks} if chunks else {}
        try:
            self.client.chat_stopStream(channel=self.channel, ts=self.ts, **kwargs)
        finally:
            self.ts = None

    @staticmethod
    def _dedupe(chunks: list) -> list:
        """Keep each task's latest update only, where its first update was."""
        latest = {c["id"]: c for c in chunks if c["type"] == "task_update"}
        out, seen = [], set()
        for c in chunks:
            if c["type"] != "task_update":
                out.append(c)
            elif c["id"] not in seen:
                seen.add(c["id"])
                out.append(latest[c["id"]])
        return out

    def _too_big(self, chunks: list) -> bool:
        text = sum(len(c["text"]) for c in chunks if c["type"] == "markdown_text")
        tasks = self.stream_tasks | {c["id"] for c in chunks if c["type"] == "task_update"}
        return self.text_chars + text > MAX_TEXT or len(tasks) > MAX_TASKS

    def _count(self, chunks: list) -> None:
        self.text_chars += sum(len(c["text"]) for c in chunks if c["type"] == "markdown_text")
        self.stream_tasks |= {c["id"] for c in chunks if c["type"] == "task_update"}

    def close(self, ok: bool = True) -> None:
        """End the stream. Tasks still open did not finish: they end as failed unless the run went well."""
        self.done.set()
        if self.flusher and self.flusher.is_alive():
            self.flusher.join(timeout=10)
        with self.lock:
            for task_id, task in self.tasks.items():
                if task["status"] == "in_progress":
                    task["status"] = "complete" if ok else "error"
                    self.queue.append(self._task(task_id))
        self.flush(final=True)

    def delete(self) -> None:
        """Delete the run's progress messages: the answer is posted and the steps are no longer needed.
        A message Slack will not delete stays; the others still go."""
        for ts in self.messages:
            try:
                self.client.chat_delete(channel=self.channel, ts=ts)
            except SlackApiError as error:
                log.warning("deleting progress %s in %s failed: %s", ts, self.thread_ts, error.response.get("error"))
        self.messages = []
