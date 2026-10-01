"""Test setup: every test gets a fresh database in a temp folder, and no real
API keys are ever loaded or used."""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="ev-tests-"))
os.environ["EV_DATA_DIR"] = str(_TMP / "data")
os.environ["EV_ENV_FILE"] = str(_TMP / "test.env")
os.environ["GITHUB_TOKEN"] = "ghp_from_the_shell_must_be_ignored"  # E.V. must only read its own .env

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend import config, db, providers  # noqa: E402
from backend.main import create_app  # noqa: E402
from backend.providers.base import Provider, ToolCall, TurnResult  # noqa: E402
from backend.services import settings  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ev.db")
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    (tmp_path / "uploads").mkdir()
    (tmp_path / "backups").mkdir()
    db.init()
    yield


@pytest.fixture
def app():
    return create_app(start_scheduler=False)


@pytest.fixture
def client(app):
    return TestClient(app, client=("127.0.0.1", 50000), headers={"X-EV-Request": "1"})


@pytest.fixture
def authed(client):
    r = client.post("/api/auth/setup", json={"passcode": "correct-horse", "name": "Tester"})
    assert r.status_code == 200, r.text
    return client


class FakeProvider(Provider):
    """Scripted provider. Each script item is (text, [ToolCall,...]) or an Exception."""

    id = "fake"
    label = "Fake AI"
    needs_key = None

    def __init__(self, script=None, pid="fake", label="Fake AI"):
        self.id, self.label = pid, label
        self.script = list(script or [])
        self.calls: list[dict] = []

    def configured(self) -> bool:
        return True

    async def list_models(self):
        return ["fake-1"]

    async def stream_turn(self, *, model, system, messages, tools, temperature=None):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages], "tools": [t.name for t in tools]})
        step = self.script.pop(0) if self.script else ("(script exhausted)", [])
        if isinstance(step, Exception):
            raise step
        text, calls = step
        for i in range(0, len(text), 7):
            yield {"type": "text", "text": text[i:i + 7]}
            await asyncio.sleep(0)
        yield {"type": "turn", "result": TurnResult(text=text, tool_calls=calls, model="fake-1",
                                                     finish_reason="tool_calls" if calls else "stop")}


@pytest.fixture
def fake_provider():
    def make(script, pid="fake", label="Fake AI", active=True):
        p = FakeProvider(script, pid, label)
        providers.register(p)
        if active:
            settings.put("llm.provider", pid)
        return p
    yield make
    for pid in ("fake", "fake2"):
        providers._REGISTRY.pop(pid, None)


def call(name: str, args: dict, cid: str = "call_1") -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args)
