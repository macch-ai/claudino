"""Hub for claudino desktop apps: runs each Slack request on the requester's own Mac.

Each teammate's desktop app opens a WebSocket to wss://claudino.macch.ai (a Cloudflare
Tunnel to this hub on hermes' localhost) and says hello with its Slack user ID and token.
The relay then sends that teammate's runs to their app, which runs Claude Code locally
with their own Claude Team seat.

Protocol (JSON text frames):
  app -> hub  {"type": "hello", "user": "U...", "token": "..."}
  hub -> app  {"type": "welcome"} or {"type": "denied"} (then closes)
  app -> hub  {"type": "unpair"}  (forget this teammate's token; the hub closes)

Pairing (an app with no token yet):
  app -> hub  {"type": "pair", "device": "MacBook de Salvador"}
  hub -> app  {"type": "pair_code", "code": "4F7-K2Q", "expires_in": 600, "login_url": "https://…/login?code=…"}
  the teammate signs in with Slack at login_url (or writes "@claudino vincular 4F7-K2Q" in Slack)
  hub -> app  {"type": "paired", "user": "U...", "token": "...", "name": "...", "handle": "...", "avatar": "..."}

HTTP on the same port (through the tunnel): GET /health, and the browser login
/login?code=… (confirm the device) -> /login/slack -> Slack -> /login/callback.
  hub -> app  {"type": "run", "id": "...", "key": "C...:ts", "prompt": "...", "system": "..."}
  hub -> app  {"type": "automation", "id": "...", "name": "..."}  (run one of the app's automations)
  app -> hub  {"type": "result", "id": "...", "result": "...", "is_error": false}
  app -> hub  {"type": "failed", "id": "...", "error": "..."}
  app -> hub  {"type": "post", "channel": "C...", "name": "...", "text": "..."}  (scheduled automation result)
"""

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

import slack_login

HOST = os.environ.get("DESKTOP_HUB_HOST", "127.0.0.1")  # only cloudflared reaches it, from the same host
PORT = int(os.environ.get("DESKTOP_HUB_PORT", "8766"))
TOKENS_FILE = Path.home() / ".config/claude-relay/desktop-tokens.json"  # {"U...": "token"}
HELLO_TIMEOUT_SECONDS = 10
PAIR_TTL_SECONDS = 10 * 60
MAX_PENDING_PAIRS = 20
PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

log = logging.getLogger("claude-relay.hub")


class Offline(Exception):
    """The requester has no desktop app connected."""


class RunFailed(RuntimeError):
    """The desktop app could not run Claude, or disconnected mid-run."""


def _tokens() -> dict:
    return json.loads(TOKENS_FILE.read_text()) if TOKENS_FILE.exists() else {}


def _save_tokens(tokens: dict) -> None:
    TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKENS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens, indent=1))
    tmp.chmod(0o600)
    tmp.replace(TOKENS_FILE)


def authorized(user: str, token: str) -> bool:
    expected = _tokens().get(user)
    return bool(expected) and hmac.compare_digest(expected, token or "")


def normalize_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def new_code() -> str:
    """Six characters without look-alikes (0/O, 1/I/L), shown as XXX-XXX."""
    return "".join(secrets.choice(PAIR_ALPHABET) for _ in range(6))


PAGE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>MACCH · claudino</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#161616;color:#f2f2f2;
font:15px -apple-system,BlinkMacSystemFont,sans-serif}}main{{max-width:420px;padding:32px;background:#1e1e1e;
border:1px solid #2e2e2e;border-radius:16px}}h1{{font-size:19px;margin:0 0 12px}}p{{color:#9a9a9a;line-height:1.5}}
a.btn{{display:inline-block;margin-top:8px;padding:10px 16px;border-radius:9px;background:#d97757;color:#fff;
text-decoration:none}}b{{color:#f2f2f2}}</style></head><body><main><h1>{title}</h1>{body}</main></body></html>"""


def page(status: int, title: str, body: str) -> Response:
    html = PAGE.format(title=html_escape(title), body=body).encode()
    return Response(status, "OK" if status == 200 else "Error",
                    Headers({"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
                             "Content-Length": str(len(html))}), html)


def redirect(url: str) -> Response:
    return Response(302, "Found", Headers({"Location": url, "Cache-Control": "no-store", "Content-Length": "0"}), b"")


def html_escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


class Pending:
    """An unpaired app waiting for its teammate to confirm the code."""

    def __init__(self, ws: ServerConnection, device: str) -> None:
        self.ws, self.device = ws, device[:80]
        self.expires = time.time() + PAIR_TTL_SECONDS
        self.nonce = secrets.token_urlsafe(16)


class Hub:
    def __init__(self, on_post=None, on_connect=None, allowed_users=(), team_id: str = "") -> None:
        """on_post(user, channel, name, text) publishes an automation result in Slack; on_connect(user) runs
        when a teammate's app connects (both called off the loop). Only allowed_users of team_id can pair."""
        self.on_post, self.on_connect = on_post, on_connect
        self.allowed_users, self.team_id = set(allowed_users), team_id
        self.loop = asyncio.new_event_loop()
        self.apps: dict[str, ServerConnection] = {}  # user -> latest connection
        self.waiting: dict[str, tuple[ServerConnection, asyncio.Future]] = {}  # run id -> (app connection, future)
        self.pairing: dict[str, Pending] = {}  # code -> unpaired app

    def start(self, host: str = HOST, port: int = PORT) -> None:
        ready = threading.Event()

        async def main() -> None:
            async with serve(self._handle, host, port, process_request=self._http):
                log.info("desktop hub listening on %s:%s", host, port)
                ready.set()
                await asyncio.Future()

        threading.Thread(target=self.loop.run_until_complete, args=(main(),), daemon=True).start()
        ready.wait(10)

    def connected(self, user: str) -> bool:
        return user in self.apps

    async def _handle(self, ws: ServerConnection) -> None:
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), HELLO_TIMEOUT_SECONDS))
        except (asyncio.TimeoutError, ConnectionClosed, ValueError):
            return
        if hello.get("type") == "pair":
            return await self._pair(ws, str(hello.get("device") or "una Mac"))
        user = hello.get("user", "")
        if hello.get("type") != "hello" or not authorized(user, hello.get("token", "")):
            log.warning("desktop app refused for %r", user)
            await ws.send(json.dumps({"type": "denied"}))
            return
        old = self.apps.get(user)
        self.apps[user] = ws
        if old:
            await old.close()  # one app per teammate: the newest wins
        await ws.send(json.dumps({"type": "welcome"}))
        log.info("desktop app connected for %s", user)
        if self.on_connect:
            self.loop.run_in_executor(None, self.on_connect, user)
        try:
            async for raw in ws:
                self._on_message(user, json.loads(raw))
        except (ConnectionClosed, ValueError):
            pass
        finally:
            if self.apps.get(user) is ws:
                del self.apps[user]
                log.info("desktop app disconnected for %s", user)
            for conn, future in list(self.waiting.values()):
                if conn is ws and not future.done():
                    future.set_exception(RunFailed("the desktop app disconnected during the run"))

    async def _pair(self, ws: ServerConnection, device: str) -> None:
        """An unpaired app asks for a code; it waits here until its teammate confirms it, or it expires."""
        now = time.time()
        for code, pending in list(self.pairing.items()):
            if pending.expires < now:
                del self.pairing[code]
        if len(self.pairing) >= MAX_PENDING_PAIRS:
            await ws.send(json.dumps({"type": "pair_unavailable"}))
            return
        code = new_code()
        self.pairing[code] = Pending(ws, device)
        msg = {"type": "pair_code", "code": f"{code[:3]}-{code[3:]}", "expires_in": PAIR_TTL_SECONDS}
        if slack_login.configured():
            msg["login_url"] = f"{slack_login.PUBLIC_URL}/login?code={code}"
        await ws.send(json.dumps(msg))
        try:
            await asyncio.wait_for(ws.wait_closed(), PAIR_TTL_SECONDS)
        except asyncio.TimeoutError:
            await ws.close()
        finally:
            pending = self.pairing.get(code)
            if pending and pending.ws is ws:
                del self.pairing[code]

    def _pending(self, code: str) -> tuple[str, Pending | None]:
        code = normalize_code(code)
        pending = self.pairing.get(code)
        return code, pending if pending and pending.expires >= time.time() else None

    async def _complete_pair(self, code: str, user: str, profile: dict) -> bool:
        code, pending = self._pending(code)
        if not pending:
            return False
        del self.pairing[code]
        ws = pending.ws
        token = secrets.token_urlsafe(32)
        tokens = _tokens()
        tokens[user] = token  # replaces any older Mac's token
        _save_tokens(tokens)
        try:
            await ws.send(json.dumps({"type": "paired", "user": user, "token": token, **profile}))
        except ConnectionClosed:
            return False
        await ws.close()
        log.info("desktop app paired for %s", user)
        return True

    def complete_pair(self, code: str, user: str, profile: dict | None = None) -> bool:
        """A teammate sent "vincular <code>" in Slack: give that waiting app a token for them (blocking)."""
        future = asyncio.run_coroutine_threadsafe(self._complete_pair(code, user, profile or {}), self.loop)
        return future.result(10)

    async def _http(self, connection: ServerConnection, request: Request) -> Response | None:
        """Plain HTTP requests (not WebSocket upgrades): health check and the browser login."""
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        url = urllib.parse.urlsplit(request.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        if url.path == "/health":
            return connection.respond(200, "ok\n")
        if not url.path.startswith("/login") or not slack_login.configured():
            return page(404, "No encontrado", "<p>Esta dirección no existe.</p>")
        expired = page(410, "El código venció", "<p>Volvé a la app de MACCH y pedí uno nuevo.</p>")
        if url.path == "/login":
            code, pending = self._pending(query.get("code", ""))
            if not pending:
                return expired
            # Confirm before Slack: someone could send you *their* link to get *their* Mac linked to your user.
            return page(200, "Vincular esta Mac", (
                f"<p>Vas a vincular <b>{html_escape(pending.device)}</b> con tu cuenta de Slack. Los pedidos que le "
                "hagas a @claudino van a correr en esa Mac, con su cuenta de Claude.</p>"
                "<p>Si no acabás de abrir la app de MACCH en tu Mac, cerrá esta página.</p>"
                f'<a class="btn" href="/login/slack?code={code}">Continuar con Slack</a>'))
        if url.path == "/login/slack":
            code, pending = self._pending(query.get("code", ""))
            if not pending:
                return expired
            return redirect(slack_login.authorize_url(code, pending.nonce, self.team_id))
        if url.path == "/login/callback":
            code, pending = self._pending(query.get("state", ""))
            if not pending:
                return expired
            if query.get("error") or not query.get("code"):
                return page(400, "No se vinculó", "<p>Slack no confirmó el inicio de sesión. Probá de nuevo desde la app.</p>")
            try:
                who = await self.loop.run_in_executor(None, slack_login.profile, query["code"])
            except Exception as error:
                log.warning("slack login failed: %s", error)
                return page(502, "No se vinculó", "<p>No pude confirmar tu cuenta con Slack. Probá de nuevo desde la app.</p>")
            if (self.team_id and who["team"] != self.team_id) or (self.allowed_users and who["user"] not in self.allowed_users):
                log.warning("slack login refused for %s (team %s)", who["user"], who["team"])
                return page(403, "Sin acceso", "<p>Tu cuenta de Slack no tiene acceso a claudino.</p>")
            if not await self._complete_pair(code, who["user"], {"name": who["name"], "avatar": who["avatar"]}):
                return expired
            return page(200, "Listo", f"<p>Tu Mac quedó vinculada, {html_escape(who['name'])}. Ya podés volver a la app.</p>")
        return page(404, "No encontrado", "<p>Esta dirección no existe.</p>")

    async def _unpair(self, user: str, ws: ServerConnection) -> None:
        tokens = _tokens()
        tokens.pop(user, None)
        _save_tokens(tokens)
        log.info("desktop app unpaired for %s", user)
        await ws.close()

    def _on_message(self, user: str, msg: dict) -> None:
        if msg.get("type") == "unpair":
            self.loop.create_task(self._unpair(user, self.apps[user]))
            return
        if msg.get("type") == "post":
            if self.on_post and msg.get("channel") and msg.get("text"):
                self.loop.run_in_executor(None, self.on_post, user, msg["channel"], msg.get("name") or "", msg["text"])
            return
        entry = self.waiting.get(msg.get("id", ""))
        if not entry or entry[1].done():
            return
        future = entry[1]
        if msg.get("type") == "result":
            future.set_result((msg.get("result") or "", bool(msg.get("is_error"))))
        elif msg.get("type") == "failed":
            future.set_exception(RunFailed(msg.get("error") or "the desktop app failed"))

    async def _request(self, user: str, payload: dict) -> tuple[str, bool]:
        ws = self.apps.get(user)
        if not ws:
            raise Offline(user)
        run_id = uuid.uuid4().hex
        future = self.loop.create_future()
        self.waiting[run_id] = (ws, future)
        try:
            await ws.send(json.dumps({**payload, "id": run_id}))
            return await future
        except ConnectionClosed:
            raise RunFailed("the desktop app disconnected before the run started")
        finally:
            self.waiting.pop(run_id, None)

    def _call(self, user: str, payload: dict, timeout: float) -> tuple[str, bool]:
        future = asyncio.run_coroutine_threadsafe(self._request(user, payload), self.loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise RunFailed(f"no answer from the desktop app after {timeout:.0f}s")

    def run(self, user: str, key: str, prompt: str, system: str, timeout: float) -> tuple[str, bool]:
        """Run prompt on user's Mac (blocking, from any thread). Returns (answer, is_error)."""
        return self._call(user, {"type": "run", "key": key, "prompt": prompt, "system": system}, timeout)

    def run_automation(self, user: str, name: str, timeout: float) -> tuple[str, bool]:
        """Run the automation called name on user's Mac (blocking). Returns (answer, is_error)."""
        return self._call(user, {"type": "automation", "name": name}, timeout)
