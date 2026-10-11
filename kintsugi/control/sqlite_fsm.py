"""Reduced mode: the SQLite-WAL event-sourced state machine with leases (spec §5.4; ruling D6).

For edge deployments and the hackathon cut without Temporal. Durable and resumable through
event sourcing: an incident's state is a fold over its append-only ``transitions`` (checked
against ``states.py``), and every activity execution is a ``steps`` row with an idempotency key,
a lease and an attempt counter.

**Single writer.** One process owns the database (``FsmServer``); crew workers ask it for writes
over a local socket (``FsmClient``), authenticated with a 256-bit token, one JSON line per call.

**Resume** (on every start; a ``kill -9`` is just an unclean start):

    1. open the DB: SQLite replays the WAL, committed transactions are recovered
    2. for each incident not RESOLVED, ESCALATED or PARKED:
         a. fold its transitions to the current state
         b. LEASED steps whose lease has expired → abandoned (the next lease is attempt + 1)
         c. re-run the step through its idempotency key: a DONE result is returned as is; a
            detectable side effect (branch, commit nonce, PR) is adopted; otherwise the handler
            cleans up and re-executes (``SurgeonStep`` does both)
    3. every 5 minutes and on clean shutdown: PRAGMA wal_checkpoint(TRUNCATE)

``seen_hits`` lives in the senses store (Stage 4, amendment A-32) rather than here.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import json
import secrets
import socket
import socketserver
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kintsugi.control.states import TERMINAL, IncidentState, fold, next_state
from kintsugi.proof.ledger import entry_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
  id TEXT PRIMARY KEY, host TEXT NOT NULL, signature TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('OPEN','REPRODUCED','ATTRIBUTED','REPAIRING','SHIPPED',
                                       'CANARY','RESOLVED','ESCALATED','PARKED')),
  budget_usd REAL, spent_usd REAL DEFAULT 0, opened_at TEXT, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS transitions (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, incident TEXT REFERENCES incidents(id),
  from_state TEXT, to_state TEXT, cause TEXT, payload_json TEXT, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
  incident TEXT, step TEXT, idem_key TEXT UNIQUE,
  status TEXT CHECK (status IN ('LEASED','DONE','FAILED')),
  lease_owner TEXT, lease_expires_at TEXT, attempt INTEGER DEFAULT 0, result_json TEXT
);
CREATE TABLE IF NOT EXISTS antibodies (
  id TEXT, version TEXT, state TEXT, spec_sha TEXT, PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS ledger (
  seq INTEGER PRIMARY KEY, entry_json TEXT NOT NULL, prev_hash TEXT, hash TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS budgets (
  host TEXT, month TEXT, pool_usd REAL, spent_usd REAL, plan_sha TEXT, PRIMARY KEY (host, month)
);
"""
CHECKPOINT_EVERY_S = 300.0


class FsmError(RuntimeError):
    pass


class LeaseHeld(FsmError):
    """Another live owner holds this step's lease."""


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t % 1 * 1000):03d}Z"


@dataclass(frozen=True)
class Lease:
    idem_key: str
    owner: str
    attempt: int
    done: bool = False
    result: Any = None


class FsmStore:
    """The database itself. Only the single writer (``FsmServer``) should hold one."""

    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        self.path = str(path)
        self.clock = clock
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5.0, isolation_level=None,
                                  check_same_thread=False)  # fmt: skip
        self.db.row_factory = sqlite3.Row
        for pragma in ("journal_mode = WAL", "synchronous = NORMAL", "busy_timeout = 5000",
                       "foreign_keys = ON"):  # fmt: skip
            self.db.execute(f"PRAGMA {pragma}")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.genesis = "0" * 64

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    # ----------------------------------------------------------------------- incidents

    def open_incident(self, incident: str, host: str, signature: str, budget_usd: float) -> None:
        with self.tx() as db:
            db.execute("INSERT OR IGNORE INTO incidents (id, host, signature, state, budget_usd,"
                       " opened_at) VALUES (?, ?, ?, 'OPEN', ?, ?)",
                       (incident, host, signature, budget_usd, _iso(self.clock())))  # fmt: skip

    def events(self, incident: str) -> list[tuple[IncidentState, IncidentState, str]]:
        rows = self.db.execute("SELECT from_state, to_state, cause FROM transitions"
                               " WHERE incident = ? ORDER BY seq", (incident,))  # fmt: skip
        return [(IncidentState(r[0]), IncidentState(r[1]), str(r[2])) for r in rows]

    def state(self, incident: str) -> IncidentState:
        """The fold over the transition log (the source of truth), cross-checked."""
        folded = fold(self.events(incident))
        row = self.db.execute("SELECT state FROM incidents WHERE id = ?", (incident,)).fetchone()
        if row is None:
            raise FsmError(f"unknown incident {incident}")
        if IncidentState(row[0]) is not folded:
            raise FsmError(f"{incident}: incidents.state {row[0]} disagrees with the log {folded}")
        return folded

    def transition(
        self, incident: str, target: str, outcome: str, payload: Mapping[str, Any] | None = None
    ) -> IncidentState:
        with self.tx() as db:
            current = fold(self.events(incident))
            nxt = next_state(current, outcome)  # raises on an illegal transition
            if nxt is not IncidentState(target):
                raise FsmError(f"{current} --{outcome}--> {nxt}, not {target}")
            now = _iso(self.clock())
            db.execute("INSERT INTO transitions (incident, from_state, to_state, cause,"
                       " payload_json, at) VALUES (?, ?, ?, ?, ?, ?)",
                       (incident, str(current), str(nxt), outcome,
                        json.dumps(dict(payload or {}), sort_keys=True), now))  # fmt: skip
            closed = now if nxt in TERMINAL else None
            db.execute("UPDATE incidents SET state = ?, closed_at = COALESCE(?, closed_at)"
                       " WHERE id = ?", (str(nxt), closed, incident))  # fmt: skip
            return nxt

    def open_incidents(self) -> list[str]:
        terminal = tuple(str(s) for s in TERMINAL)
        rows = self.db.execute(
            f"SELECT id FROM incidents WHERE state NOT IN ({','.join('?' * len(terminal))})"
            " ORDER BY opened_at, id", terminal
        )  # fmt: skip
        return [r[0] for r in rows]

    # ----------------------------------------------------------------------- steps

    def lease(self, incident: str, step: str, idem_key: str, owner: str, ttl_s: float) -> Lease:
        now = self.clock()
        with self.tx() as db:
            row = db.execute("SELECT * FROM steps WHERE idem_key = ?", (idem_key,)).fetchone()
            expires = _iso(now + ttl_s)
            if row is None:
                db.execute("INSERT INTO steps (incident, step, idem_key, status, lease_owner,"
                           " lease_expires_at, attempt) VALUES (?, ?, ?, 'LEASED', ?, ?, 1)",
                           (incident, step, idem_key, owner, expires))  # fmt: skip
                return Lease(idem_key, owner, 1)
            if row["status"] == "DONE":
                return Lease(idem_key, row["lease_owner"], row["attempt"], True,
                             json.loads(row["result_json"] or "null"))  # fmt: skip
            live = row["status"] == "LEASED" and row["lease_expires_at"] > _iso(now)
            if live and row["lease_owner"] != owner:
                raise LeaseHeld(f"{idem_key} is leased by {row['lease_owner']}")
            attempt = row["attempt"] + (0 if live else 1)
            db.execute("UPDATE steps SET status = 'LEASED', lease_owner = ?, lease_expires_at = ?,"
                       " attempt = ? WHERE idem_key = ?",
                       (owner, expires, attempt, idem_key))  # fmt: skip
            return Lease(idem_key, owner, attempt)

    def renew(self, idem_key: str, owner: str, ttl_s: float) -> bool:
        query = ("UPDATE steps SET lease_expires_at = ? WHERE idem_key = ? AND lease_owner = ?"
                 " AND status = 'LEASED'")  # fmt: skip
        with self.tx() as db:
            changed = db.execute(query, (_iso(self.clock() + ttl_s), idem_key, owner)).rowcount
            return bool(changed)

    def complete(self, idem_key: str, owner: str, result: Any) -> None:
        with self.tx() as db:
            changed = db.execute("UPDATE steps SET status = 'DONE', result_json = ? WHERE"
                                 " idem_key = ? AND lease_owner = ? AND status = 'LEASED'",
                                 (json.dumps(result, sort_keys=True), idem_key,
                                  owner)).rowcount  # fmt: skip
            if not changed:
                raise LeaseHeld(f"{owner} no longer holds {idem_key}")

    def fail(self, idem_key: str, owner: str, error: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE steps SET status = 'FAILED', result_json = ? WHERE idem_key = ?"
                       " AND lease_owner = ?",
                       (json.dumps({"error": error}), idem_key, owner))  # fmt: skip

    def expire_stale(self) -> list[dict[str, Any]]:
        """§5.4 step 2b: expired leases → abandoned; the next lease is attempt + 1."""
        now = _iso(self.clock())
        with self.tx() as db:
            rows = db.execute("SELECT * FROM steps WHERE status = 'LEASED' AND"
                              " lease_expires_at < ?", (now,)).fetchall()  # fmt: skip
            for row in rows:
                note = {"abandoned": True, "attempt": row["attempt"], "owner": row["lease_owner"]}
                db.execute("UPDATE steps SET status = 'FAILED', result_json = ? WHERE"
                           " idem_key = ?", (json.dumps(note), row["idem_key"]))  # fmt: skip
        return [dict(row) for row in rows]

    def step(self, idem_key: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM steps WHERE idem_key = ?", (idem_key,)).fetchone()
        return dict(row) if row else None

    def unfinished_steps(self, incident: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM steps WHERE incident = ? AND status != 'DONE'"
                               " ORDER BY rowid", (incident,))  # fmt: skip
        return [dict(row) for row in rows]

    # ----------------------------------------------------------------------- ledger

    def ledger_append(self, entry: Mapping[str, Any]) -> str:
        """Append in ``kintsugi.proof.ledger``'s record format, so C1 verifies this table."""
        if any(key in entry for key in ("seq", "prev", "hash")):
            raise FsmError("the ledger assigns seq, prev and hash itself")
        body = {"at": _iso(self.clock()), **dict(entry)}
        if not body.get("type"):
            raise FsmError("a ledger entry needs a type")
        with self.tx() as db:
            row = db.execute("SELECT seq, hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
            seq, prev = (row[0] + 1, row[1]) if row else (1, self.genesis)
            record = {"seq": seq, **body, "prev": prev}
            record["hash"] = entry_hash(record, prev)
            db.execute("INSERT INTO ledger (seq, entry_json, prev_hash, hash) VALUES (?, ?, ?, ?)",
                       (seq, json.dumps(record, sort_keys=True), prev, record["hash"]))  # fmt: skip
            return record["hash"]

    def ledger_entries(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT entry_json FROM ledger ORDER BY seq")
        return [json.loads(row[0]) for row in rows]

    def recover(self) -> dict[str, Any]:
        """The writer's start-up half of §5.4: fold every open incident, expire stale leases."""
        incidents = {incident: str(self.state(incident)) for incident in self.open_incidents()}
        abandoned = self.expire_stale()
        self.checkpoint()
        return {"incidents": incidents, "abandoned": [row["idem_key"] for row in abandoned]}

    def checkpoint(self) -> tuple[int, int, int]:
        with self.lock:
            row = self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self.checkpoint()
        self.db.close()


# --------------------------------------------------------------------------- resume


class StepHandler:
    """How to re-run one kind of step after a crash (adoption is inside ``execute``)."""

    def execute(self, incident: str, idem_key: str, attempt: int, args: Mapping[str, Any]) -> Any:
        raise NotImplementedError


@dataclass
class ResumeReport:
    incidents: list[str]
    abandoned: list[dict[str, Any]]
    rerun: dict[str, Any]


def resume(
    store: FsmStore,
    handlers: Mapping[str, StepHandler],
    args_for: Callable[[dict[str, Any]], Mapping[str, Any]],
    *,
    owner: str,
    ttl_s: float = 60.0,
) -> ResumeReport:
    """§5.4's resume algorithm, run on every start."""
    abandoned = store.expire_stale()
    rerun: dict[str, Any] = {}
    incidents = store.open_incidents()
    for incident in incidents:
        store.state(incident)  # fold the log; raises if it is not a legal history
        for row in store.unfinished_steps(incident):
            handler = handlers.get(row["step"])
            if handler is None:
                continue
            try:
                lease = store.lease(incident, row["step"], row["idem_key"], owner, ttl_s)
            except LeaseHeld:  # a live worker is still on it
                continue
            if lease.done:
                rerun[row["idem_key"]] = lease.result
                continue
            result = handler.execute(incident, row["idem_key"], lease.attempt, args_for(row))
            store.complete(row["idem_key"], owner, result)
            rerun[row["idem_key"]] = result
    store.checkpoint()
    return ResumeReport(incidents, abandoned, rerun)


# --------------------------------------------------------------------------- the writer


OPS = ("open_incident", "transition", "state", "open_incidents", "lease", "renew", "complete",
       "fail", "step", "ledger_append", "ledger_entries", "expire_stale", "recover",
       "checkpoint")  # fmt: skip


class FsmServer:
    """The single writer: serves ``OPS`` on a local socket, one JSON line per request."""

    def __init__(self, store: FsmStore, token: str, *, host: str = "127.0.0.1", port: int = 0):
        if len(token) < 43:
            raise ValueError("the FSM token must carry at least 256 bits")
        self.store = store
        self.token = token
        server = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                for raw in self.rfile:
                    reply = server.dispatch(raw)
                    self.wfile.write((json.dumps(reply) + "\n").encode("utf-8"))
                    self.wfile.flush()

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.tcp = socketserver.ThreadingTCPServer((host, port), Handler)
        self.tcp.daemon_threads = True
        self.address = self.tcp.server_address
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def dispatch(self, raw: bytes) -> dict[str, Any]:
        try:
            request = json.loads(raw)
            if not hmac.compare_digest(str(request.get("token", "")).encode(), self.token.encode()):
                return {"ok": False, "error": "unauthorized"}
            op = request.get("op")
            if op not in OPS:
                return {"ok": False, "error": f"unknown op {op!r}"}
            result = getattr(self.store, op)(**request.get("args", {}))
            if isinstance(result, Lease):
                result = vars(result)
            elif isinstance(result, IncidentState):
                result = str(result)
            return {"ok": True, "result": result}
        except LeaseHeld as error:
            return {"ok": False, "error": str(error), "kind": "LeaseHeld"}
        except Exception as error:  # every failure goes back to the caller, never kills the writer
            return {"ok": False, "error": f"{type(error).__name__}: {error}"}

    def start(self) -> FsmServer:
        self._thread = threading.Thread(target=self.tcp.serve_forever, daemon=True)
        self._thread.start()
        threading.Thread(target=self._checkpoints, daemon=True).start()
        return self

    def _checkpoints(self) -> None:
        while not self._stop.wait(CHECKPOINT_EVERY_S):
            with contextlib.suppress(sqlite3.Error):
                self.store.checkpoint()

    def stop(self) -> None:
        self._stop.set()
        self.tcp.shutdown()
        self.tcp.server_close()
        self.store.close()


class FsmClient:
    """A crew worker's handle on the single writer."""

    def __init__(self, host: str, port: int, token: str, *, timeout_s: float = 10.0) -> None:
        self.host, self.port, self.token, self.timeout_s = host, port, token, timeout_s

    def call(self, op: str, **args: Any) -> Any:
        with socket.create_connection((self.host, self.port), timeout=self.timeout_s) as sock:
            request = {"token": self.token, "op": op, "args": args}
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    raise FsmError("the writer closed the connection")
                data += chunk
        reply = json.loads(data)
        if not reply["ok"]:
            if reply.get("kind") == "LeaseHeld":
                raise LeaseHeld(reply["error"])
            raise FsmError(reply["error"])
        return reply["result"]


def new_token() -> str:
    return secrets.token_urlsafe(32)


def read_token(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def write_token(path: Path) -> str:
    token = new_token()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return token


# --------------------------------------------------------------------------- a leased step


def run_leased(
    client: FsmClient,
    incident: str,
    step: str,
    idem_key: str,
    owner: str,
    work: Callable[[int], Any],
    *,
    ttl_s: float = 60.0,
) -> Any:
    """Lease a step, renew the lease while ``work(attempt)`` runs, record its result."""
    lease = client.call("lease", incident=incident, step=step, idem_key=idem_key, owner=owner,
                        ttl_s=ttl_s)  # fmt: skip
    if lease["done"]:
        return lease["result"]
    stop = threading.Event()

    def renew() -> None:
        while not stop.wait(ttl_s / 3):
            with contextlib.suppress(Exception):
                client.call("renew", idem_key=idem_key, owner=owner, ttl_s=ttl_s)

    threading.Thread(target=renew, daemon=True).start()
    try:
        result = work(int(lease["attempt"]))
    except Exception as error:
        stop.set()
        client.call("fail", idem_key=idem_key, owner=owner, error=str(error))
        raise
    stop.set()
    client.call("complete", idem_key=idem_key, owner=owner, result=result)
    return result


# --------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The reduced-mode single writer and helpers.")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the single writer")
    serve.add_argument("--db", type=Path, required=True)
    serve.add_argument("--token-file", type=Path, required=True)
    serve.add_argument("--port", type=int, default=0)
    step = sub.add_parser("run-step", help="lease and run one drill surgeon step")
    step.add_argument("--port", type=int, required=True)
    step.add_argument("--token-file", type=Path, required=True)
    step.add_argument("--card", required=True, help="JSON of a contracts.Card")
    step.add_argument("--idem", required=True)
    step.add_argument("--ttl", type=float, default=3.0)
    args = parser.parse_args(argv)
    if args.command == "serve":
        store = FsmStore(args.db)  # opening replays the WAL: committed transactions survive
        recovered = store.recover()
        server = FsmServer(store, read_token(args.token_file), port=args.port).start()
        print(f"listening {server.address[1]} {json.dumps(recovered)}", flush=True)
        with contextlib.suppress(KeyboardInterrupt):
            while True:
                time.sleep(3600)
        server.stop()
        return 0
    from kintsugi.control.drill import surgeon_drill_step

    client = FsmClient("127.0.0.1", args.port, read_token(args.token_file))
    card = json.loads(args.card)
    owner = f"executor-{card['hypothesis']}-{time.time():.0f}"

    def work(attempt: int) -> dict[str, Any]:
        return surgeon_drill_step(card, attempt)

    result = run_leased(client, card["incident_id"], "surgeon", args.idem, owner, work,
                        ttl_s=args.ttl)  # fmt: skip
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
