"""Trigger control API — plan §4b + ADR-006 §4: "API surface is a
monitoring tool, not just CRUD". Two layers:

- `TriggerService` — the logic, offline-testable against any store with
  the `TriggerStore` shape (save/load_all/record_run/runs). Owns the
  in-memory trigger registry; `list` returns the `triggers.rollup` view
  so a client monitors background programs without opening run traces.
- A thin localhost HTTP binding over it (stdlib `http.server`, no web
  framework) — JSON in/out, `{"error": {"code", "message"}}` on non-2xx.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol
from urllib.parse import parse_qs, urlparse

from .trigger_events import FeedProtocolError, TriggerEventFeed
from .triggers import RunRecord, Trigger, rollup, trigger_from_record, trigger_to_record

type Records = list[dict]

# Client-writable definition fields (wire name → dataclass attribute).
# Everything else — id, the rollup, scheduling state — is service-owned.
_FIELD_ATTRS = {
    "name": "name", "kind": "kind", "spec": "spec", "program": "program",
    "args": "args", "owner": "owner", "enabled": "enabled", "limits": "limits",
    "maxConsecutiveFailures": "max_consecutive_failures",
}


class TriggerNotFound(KeyError):
    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


class Store(Protocol):
    """The `triggers.TriggerStore` surface (a fake in tests)."""

    def save(self, t: Trigger) -> None: ...
    def load_all(self) -> list[Trigger]: ...
    def record_run(self, t: Trigger, run: RunRecord, *, ts_ms: int) -> None: ...
    def runs(self, trigger_id: str, limit: int = 20) -> Records: ...


def _validate(t: Trigger) -> None:
    if t.kind not in ("cron", "event"):
        raise ValueError(f"kind must be 'cron' or 'event', got {t.kind!r}")
    if not t.program:
        raise ValueError("program is required")
    if t.kind == "cron" and "cron" not in t.spec and "every_s" not in t.spec:
        raise ValueError("cron trigger spec needs 'cron' or 'every_s'")
    if t.kind == "event" and "dataset" not in t.spec:
        raise ValueError("event trigger spec needs 'dataset'")


class TriggerService:
    """CRUD + monitoring over a trigger store. Create stamps `owner`
    (this instance by default — plan §4b single-owner create-flow) and
    persists; the registry is authoritative between `load_all` and
    saves. Thread-safe: the HTTP binding runs threaded."""

    def __init__(self, store: Store, *, owner: str,
                 registry: dict[str, Trigger] | None = None):
        """`registry` shares live state with a TriggerRuntime (pass
        `runtime.triggers` — live-caught: separate dicts meant control-API
        patches never reached the running scheduler). In-memory entries
        win over persisted ones; store rows fill the gaps."""
        self._store = store
        self._owner = owner
        self._lock = threading.Lock()
        self._triggers: dict[str, Trigger] = \
            registry if registry is not None else {}
        for t in store.load_all():
            self._triggers.setdefault(t.id, t)

    # --- CRUD ---------------------------------------------------------------
    def create(self, fields: dict) -> str:
        rec = self._check_fields(fields)
        with self._lock:
            rec["id"] = uuid.uuid4().hex
            if not rec.get("owner"):
                rec["owner"] = self._owner
            t = trigger_from_record(rec)
            _validate(t)
            self._store.save(t)
            self._triggers[t.id] = t
            return t.id

    def delete(self, trigger_id: str) -> None:
        with self._lock:
            self._get(trigger_id)
            del self._triggers[trigger_id]
            # TriggerStore has no delete yet — duck-typed so a store that
            # grows one is honored; until then the synced record merely
            # stops being served/scheduled by this instance.
            delete_fn = getattr(self._store, "delete", None)
            if callable(delete_fn):
                delete_fn(trigger_id)

    def patch(self, trigger_id: str, fields: dict) -> dict:
        upd = self._check_fields(fields)
        with self._lock:
            t = self._get(trigger_id)
            cand = replace(t, **{_FIELD_ATTRS[k]: v for k, v in upd.items()})
            if "spec" in upd or "kind" in upd:
                cand.next_due = None  # re-arm forward from now on the new schedule
            _validate(cand)
            self._triggers[trigger_id] = cand
            self._store.save(cand)
            return self._record(cand)

    def enable(self, trigger_id: str) -> dict:
        with self._lock:
            t = self._get(trigger_id)
            t.enabled = True
            t.consecutive_failures = 0  # manual re-enable resets the circuit breaker
            t.next_due = None           # cron re-arms forward, never backward
            self._store.save(t)
            return self._record(t)

    def disable(self, trigger_id: str) -> dict:
        with self._lock:
            t = self._get(trigger_id)
            t.enabled = False
            self._store.save(t)
            return self._record(t)

    # --- monitoring ---------------------------------------------------------
    def list(self) -> Records:
        with self._lock:
            return [rollup(t) for t in self._triggers.values()]

    def get(self, trigger_id: str) -> dict:
        with self._lock:
            return self._record(self._get(trigger_id))

    def runs(self, trigger_id: str, limit: int = 20) -> Records:
        with self._lock:
            self._get(trigger_id)
        return self._store.runs(trigger_id, limit)

    # --- internals ----------------------------------------------------------
    def _get(self, trigger_id: str) -> Trigger:
        t = self._triggers.get(trigger_id)
        if t is None:
            raise TriggerNotFound(f"trigger {trigger_id} not found")
        return t

    @staticmethod
    def _record(t: Trigger) -> dict:
        return {"id": t.id, **trigger_to_record(t)}

    @staticmethod
    def _check_fields(fields: dict) -> dict:
        unknown = set(fields) - set(_FIELD_ATTRS)
        if unknown:
            allowed = ", ".join(sorted(_FIELD_ATTRS))
            raise ValueError(f"unknown fields {sorted(unknown)}; allowed: {allowed}")
        return dict(fields)


class TriggerControlServer(ThreadingHTTPServer):
    """Localhost control server over a TriggerService. Port 0 = ephemeral
    (read the bound port from `server_address`). `feed_factory` (a fresh
    `TriggerEventFeed` per call) enables the live `GET /triggers/events`
    stream; without it the route answers 501."""

    daemon_threads = True

    def __init__(self, service: TriggerService, addr: tuple[str, int] = ("127.0.0.1", 0),
                 *, feed_factory: Callable[[], TriggerEventFeed] | None = None):
        super().__init__(addr, _Handler)
        self.service = service
        self.feed_factory = feed_factory


def start_server(service: TriggerService, *, host: str = "127.0.0.1", port: int = 0,
                 feed_factory: Callable[[], TriggerEventFeed] | None = None,
                 ) -> TriggerControlServer:
    """Start the control server on a daemon thread; `shutdown()` +
    `server_close()` to stop."""
    srv = TriggerControlServer(service, (host, port), feed_factory=feed_factory)
    threading.Thread(target=srv.serve_forever, name="trigger-control", daemon=True).start()
    return srv


class _Handler(BaseHTTPRequestHandler):
    # Routes: POST/GET /triggers · GET /triggers/events (SSE)
    # · GET/PATCH/DELETE /triggers/{id}
    # · POST /triggers/{id}/enable|disable · GET /triggers/{id}/runs?limit=N
    @property
    def _service(self) -> TriggerService:
        assert isinstance(self.server, TriggerControlServer)
        return self.server.service
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        try:
            self._route(method)
        except TriggerNotFound as e:
            self._json(404, {"error": {"code": "trigger.not_found", "message": str(e)}})
        except ValueError as e:
            self._json(400, {"error": {"code": "request.invalid", "message": str(e)}})

    def _route(self, method: str) -> None:
        url = urlparse(self.path)
        seg = [s for s in url.path.split("/") if s]
        svc = self._service
        if seg == ["triggers"]:
            if method == "POST":
                return self._json(201, {"id": svc.create(self._body())})
            if method == "GET":
                return self._json(200, svc.list())
        # static /triggers/events before the /triggers/{id} wildcard
        elif seg == ["triggers", "events"]:
            if method == "GET":
                return self._stream_runs()
        elif len(seg) == 2 and seg[0] == "triggers":
            tid = seg[1]
            if method == "GET":
                return self._json(200, svc.get(tid))
            if method == "PATCH":
                return self._json(200, svc.patch(tid, self._body()))
            if method == "DELETE":
                svc.delete(tid)
                return self._json(204, None)
        elif len(seg) == 3 and seg[0] == "triggers":
            tid, tail = seg[1], seg[2]
            if method == "POST" and tail == "enable":
                return self._json(200, svc.enable(tid))
            if method == "POST" and tail == "disable":
                return self._json(200, svc.disable(tid))
            if method == "GET" and tail == "runs":
                raw = parse_qs(url.query).get("limit", ["20"])[0]
                try:
                    limit = int(raw)
                except ValueError:
                    raise ValueError(f"limit must be an integer, got {raw!r}") from None
                return self._json(200, svc.runs(tid, limit))
        self._json(404, {"error": {"code": "route.not_found",
                                   "message": f"no route {method} {url.path}"}})

    def _stream_runs(self) -> None:
        """GET /triggers/events — bridge a live TriggerEventFeed onto the
        control API: one `run` frame per post-connect run, terminal
        `closed{reason}` (drop-snapshot semantics live in the feed)."""
        assert isinstance(self.server, TriggerControlServer)
        factory = self.server.feed_factory
        if factory is None:
            return self._json(501, {"error": {"code": "feed.unavailable",
                                              "message": "no run feed wired"}})
        feed = factory()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            try:
                for event in feed.events():
                    self._frame("run", event)
                reason = feed.closed_reason or "upstream_closed"
            except FeedProtocolError:
                reason = "upstream_error"
            self._frame("closed", {"reason": reason})
        except (BrokenPipeError, ConnectionResetError):
            pass  # monitor disconnected — the feed generator closes with us

    def _frame(self, event: str, data: dict) -> None:
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"bad JSON body: {e}") from None
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        return body

    def _json(self, status: int, obj: object | None) -> None:
        payload = b"" if obj is None else json.dumps(obj, indent=2).encode()
        self.send_response(status)
        if payload:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        pass  # control-plane chatter; the service layer is the observable surface
