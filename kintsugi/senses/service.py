"""Wiring for ``kintsugi senses``: paths, the ingress secrets and the long-running parts.

State lives under ``.state/senses/`` (gitignored): the shared SQLite store, the receptor
manifest and ``ingress.secret.json``, which holds the 256-bit path secret and the webhook signing
secret. Both secrets are generated here, sent to neatlogs only inside the webhook registration,
and never printed. Raw deliveries are kept as evidence under ``evidence/ingress/``.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kintsugi.senses.detections import Manifest, Provisioner
from kintsugi.senses.ingress import IngressConfig, IngressState, create_app
from kintsugi.senses.neatlogs_api import Credentials, NeatlogsApi
from kintsugi.senses.poller import Poller
from kintsugi.senses.store import QueueSink, SenseStore

STATE_DIR = Path(".state") / "senses"
STORE_PATH = STATE_DIR / "senses.sqlite"
MANIFEST_PATH = STATE_DIR / "receptors.json"
SECRET_PATH = STATE_DIR / "ingress.secret.json"
EVIDENCE_DIR = Path("evidence")


@dataclass(frozen=True)
class IngressSecrets:
    path_secret: str
    signing_secret: str
    admin_token: str  # the Watchdog's chaos API switches the ingress with it (Stage 5)

    def __repr__(self) -> str:
        return "IngressSecrets(<redacted>)"


def _save(path: Path, made: IngressSecrets) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(vars(made)), encoding="utf-8", newline="\n")
    os.chmod(path, 0o600)


def ingress_secrets(repo: Path, *, create: bool = False) -> IngressSecrets:
    path = repo / SECRET_PATH
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        if "admin_token" not in data:  # written before Stage 5: add the admin token once
            data["admin_token"] = secrets.token_urlsafe(32)
            _save(path, IngressSecrets(**data))
        return IngressSecrets(data["path_secret"], data["signing_secret"], data["admin_token"])
    if not create:
        raise FileNotFoundError(f"{SECRET_PATH} is missing: run `kintsugi senses provision`")
    made = IngressSecrets(*(secrets.token_urlsafe(32) for _ in range(3)))
    _save(path, made)
    return made


def api_from_env(env: Mapping[str, str], **kwargs: Any) -> NeatlogsApi:
    return NeatlogsApi(Credentials.from_env(env), **kwargs)


def provision(repo: Path, api: NeatlogsApi, public_url: str | None) -> tuple[Manifest, list[str]]:
    """Ensure the receptor group, detections, alert rules and (with a URL) the webhook."""
    provisioner = Provisioner(api)
    webhook_id = None
    if public_url:
        keys = ingress_secrets(repo, create=True)
        url = f"{public_url.rstrip('/')}/hook/{keys.path_secret}"
        webhook_id = provisioner.webhook(url, keys.signing_secret)
    manifest = provisioner.ensure(webhook_id=webhook_id)
    manifest.save(repo / MANIFEST_PATH)
    created = [f"created {n}" for n in provisioner.created]
    return manifest, created + [f"deactivated {n}" for n in provisioner.deactivated]


def load_manifest(repo: Path) -> Manifest:
    return Manifest.load(repo / MANIFEST_PATH)


def ingress_app(repo: Path, store: SenseStore, state: IngressState | None = None) -> Any:
    keys = ingress_secrets(repo)
    manifest = load_manifest(repo) if (repo / MANIFEST_PATH).is_file() else None
    bindings = {k: vars(v) for k, v in (manifest.detections if manifest else {}).items()}
    config = IngressConfig(path_secret=keys.path_secret, signing_secret=keys.signing_secret,
                           evidence_dir=repo / EVIDENCE_DIR, bindings=bindings,
                           source_header="cf-connecting-ip",
                           admin_token=keys.admin_token)  # fmt: skip
    return create_app(config, store, state=state)


def poller(repo: Path, api: NeatlogsApi, store: SenseStore) -> Poller:
    manifest = load_manifest(repo)
    return Poller(api, store, QueueSink(), manifest.detections, manifest.metrics)


def serve(app: Any, *, host: str = "127.0.0.1", port: int = 8787) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="warning")


def poll_forever(poll: Poller, stop: threading.Event, interval_s: float) -> None:
    poll.run(stop, interval_s=interval_s)
