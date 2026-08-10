"""
Draft path — the LLM adapter and its graceful degradation.

Regression cover for the deployed-app bug where the draft tool raised
'ServingEndpointsAPI object has no attribute get_open_ai_client' (a
databricks-sdk version mismatch). Two guarantees:

  1. llm.complete() drives the SDK's NATIVE serving_endpoints.query(...) and
     returns the model text — no get_open_ai_client, so it can't depend on
     which SDK the runtime ships.
  2. draft_outreach() ALWAYS returns text: if the model/SDK call fails, it
     degrades to a deterministic templated draft instead of hard-failing, so
     draft -> send -> book keeps working during a live demo.

All network-free: the SDK is faked in sys.modules; the DB is monkeypatched.
"""
import sys
import types

import pytest


# --- 1. llm.complete uses the version-independent native query -------------- #
def _install_fake_sdk(monkeypatch, capture):
    """Register a minimal fake `databricks.sdk` so llm.complete() imports it
    instead of the real SDK (which need not be installed to run this test)."""
    class ChatMessageRole:
        SYSTEM = "system"
        USER = "user"

    class ChatMessage:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    class _Msg:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content):
            self.message = _Msg(content)

    class _Resp:
        def __init__(self, content):
            self.choices = [_Choice(content)]

    class _ServingEndpoints:
        def query(self, name, messages, max_tokens=None, temperature=None):
            capture.update(name=name, messages=messages,
                           max_tokens=max_tokens, temperature=temperature)
            return _Resp("Maria, worth a roof check after the storm. Reply YES.")

    class WorkspaceClient:
        def __init__(self):
            self.serving_endpoints = _ServingEndpoints()

    mod_databricks = types.ModuleType("databricks")
    mod_sdk = types.ModuleType("databricks.sdk")
    mod_service = types.ModuleType("databricks.sdk.service")
    mod_serving = types.ModuleType("databricks.sdk.service.serving")
    mod_sdk.WorkspaceClient = WorkspaceClient
    mod_serving.ChatMessage = ChatMessage
    mod_serving.ChatMessageRole = ChatMessageRole
    for name, mod in {
        "databricks": mod_databricks,
        "databricks.sdk": mod_sdk,
        "databricks.sdk.service": mod_service,
        "databricks.sdk.service.serving": mod_serving,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)


def test_complete_uses_native_query_and_returns_text(monkeypatch):
    from agent import llm

    capture = {}
    _install_fake_sdk(monkeypatch, capture)

    text = llm.complete("draft me a message", max_tokens=123, temperature=0.3)

    assert isinstance(text, str) and text.strip()
    # routed to the configured endpoint by name (not get_open_ai_client)
    assert capture["name"] == llm.DEFAULT_MODEL
    assert capture["max_tokens"] == 123
    # the single prompt is sent as a USER chat message
    assert capture["messages"][0].role == "user"
    assert capture["messages"][0].content == "draft me a message"


# --- 2. draft_outreach always returns text (fallback + happy path) ---------- #
_OPP = {
    "name": "Maria Lopez", "city": "Dallas", "state": "TX",
    "service_needed": "roofing", "event_type": "Severe Thunderstorm Warning",
    "headline": "a Severe Thunderstorm Warning", "severity": "Severe",
    "distance_km": 12.0, "is_prospect": False, "tier": "gold",
    "tenure_start": "2020-01-01", "safety_sent": True, "expires_at": None,
}


def _patch_draft_io(monkeypatch, tools):
    """Stub the DB + safety gate + retrieval so draft_outreach runs offline."""
    class _FakeCur:
        def execute(self, *a, **k):
            return None

        def fetchone(self):
            return (123,)

    class _FakeCM:
        def __enter__(self):
            return _FakeCur()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(tools, "_load_opportunity", lambda oid: dict(_OPP))
    monkeypatch.setattr(tools.safety, "commercial_allowed",
                        lambda **kw: (True, "ok"))
    monkeypatch.setattr(tools.retrieval, "best_template", lambda *a, **k: None)
    monkeypatch.setattr(tools.lakebase, "cursor", lambda *a, **k: _FakeCM())
    monkeypatch.setattr(tools, "_set_status", lambda *a, **k: None)


def test_draft_outreach_degrades_to_template_when_model_fails(monkeypatch):
    pytest.importorskip("psycopg2")  # agent.tools imports lakebase -> psycopg2
    from agent import tools

    _patch_draft_io(monkeypatch, tools)

    def _boom(*a, **k):
        raise RuntimeError("ServingEndpointsAPI object has no attribute get_open_ai_client")

    monkeypatch.setattr(tools.llm, "complete", _boom)

    res = tools.draft_outreach("opp-1")

    assert res["ok"] is True
    assert res["degraded"] is True
    assert res["message_text"] and "Maria" in res["message_text"]
    assert len(res["message_text"]) <= 480
    assert res["status"] == "drafted"


def test_draft_outreach_uses_model_text_on_happy_path(monkeypatch):
    pytest.importorskip("psycopg2")
    from agent import tools

    _patch_draft_io(monkeypatch, tools)
    monkeypatch.setattr(tools.llm, "complete",
                        lambda *a, **k: "Maria, worth a roof check. Reply YES.")

    res = tools.draft_outreach("opp-1")

    assert res["ok"] is True
    assert res["degraded"] is False
    assert "roof check" in res["message_text"]


def test_templated_draft_is_bounded_and_safe(monkeypatch):
    pytest.importorskip("psycopg2")
    from agent import tools

    msg = tools._templated_draft(_OPP, "sms")

    assert msg.startswith("Maria")
    assert len(msg) <= 480
    assert "!" not in msg                 # no exclamation marks (RULES)
    assert "YES" in msg                   # one clear ask
    assert msg.isascii()                  # SMS-safe, no emoji
