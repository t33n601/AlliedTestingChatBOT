"""Langfuse tracing (langfuse_tracing.py), tested offline against a fake ingestion server:
no keys and no internet needed."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import langfuse_tracing as lt  # noqa: E402


@pytest.fixture
def fake_langfuse():
    """A local HTTP server that records ingestion batches like Langfuse would."""
    received, status = [], {"code": 207}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append({"auth": self.headers.get("Authorization"), "batch": body["batch"]})
            self.send_response(status["code"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"successes": [], "errors": []}).encode())

        def do_GET(self):
            ok = self.headers.get("Authorization") is not None and status["code"] < 300
            self.send_response(200 if ok else 401)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{server.server_port}", "received": received, "status": status}
    server.shutdown()


def client(url, **cfg):
    return lt.Langfuse({"public_key": "pk-lf-test", "secret_key": "sk-lf-test", "host": url, **cfg},
                       flush_interval=0.05)


def events(fake):
    return [e for call in fake["received"] for e in call["batch"]]


# ---------------------------------------------------------------- positive

@pytest.mark.positive
def test_config_is_read_from_yaml(tmp_path, monkeypatch):
    for var in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST", "LANGFUSE_ENVIRONMENT"):
        monkeypatch.delenv(var, raising=False)
    f = tmp_path / "langfuse.yml"
    f.write_text('langfuse:\n  public_key: "pk-lf-1"\n  secret_key: "sk-lf-2"\n  host: "https://example.test/"\n')
    cfg = lt.load_config(f)
    assert cfg["public_key"] == "pk-lf-1" and cfg["secret_key"] == "sk-lf-2"
    assert cfg["host"] == "https://example.test"
    assert lt.Langfuse(cfg).enabled


@pytest.mark.positive
def test_environment_variables_override_yaml(tmp_path, monkeypatch):
    f = tmp_path / "langfuse.yml"
    f.write_text("public_key: pk-lf-file\nsecret_key: sk-lf-file\n")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-env")
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    cfg = lt.load_config(f)
    assert cfg["public_key"] == "pk-lf-env" and cfg["secret_key"] == "sk-lf-file"
    assert cfg["host"] == lt.DEFAULT_HOST


@pytest.mark.positive
def test_full_trace_is_sent_with_basic_auth(fake_langfuse):
    lf = client(fake_langfuse["url"])
    tid = lt.new_id()
    start = lt.now_iso()
    lf.trace(tid, name="kb-chat", input="What is PgMex?", sessionId="chat-1", tags=["lang:en"])
    lf.span(tid, "retrieve", start, input="what is pgmex", output=[{"source": "ourproducts"}])
    lf.generation(tid, "answer", start, model="qwen3:1.7b", output="A library [1].",
                  usage=lt.usage(120, 30))
    lf.event(tid, "no_answer", level="WARNING")
    lf.score(tid, "user_feedback", 1)
    lf.flush()

    got = events(fake_langfuse)
    assert [e["type"] for e in got] == ["trace-create", "span-create", "generation-create",
                                        "event-create", "score-create"]
    assert all(e["body"].get("traceId", e["body"]["id"]) == tid for e in got)
    assert fake_langfuse["received"][0]["auth"].startswith("Basic ")
    gen = got[2]["body"]
    assert gen["model"] == "qwen3:1.7b"
    assert gen["usage"] == {"input": 120, "output": 30, "total": 150, "unit": "TOKENS"}
    assert got[0]["body"]["sessionId"] == "chat-1"
    assert lf.sent == 5 and lf.last_error is None


@pytest.mark.positive
def test_changed_feedback_overwrites_the_same_score(fake_langfuse):
    lf = client(fake_langfuse["url"])
    lf.score("t-1", "user_feedback", 1)
    lf.score("t-1", "user_feedback", -1)
    lf.flush()
    ids = [e["body"]["id"] for e in events(fake_langfuse)]
    assert ids[0] == ids[1] == "t-1-user_feedback"


@pytest.mark.positive
def test_auth_check_accepts_valid_keys(fake_langfuse):
    assert client(fake_langfuse["url"]).auth_check()


# ---------------------------------------------------------------- negative

@pytest.mark.negative
@pytest.mark.parametrize("cfg", [
    {},
    {"public_key": "pk-lf-1"},
    {"secret_key": "sk-lf-1"},
    {"public_key": "pk-lf-xxxxxxxx", "secret_key": "sk-lf-xxxxxxxx"},      # template placeholders
    {"public_key": "pk-lf-1", "secret_key": "sk-lf-1", "enabled": False},
], ids=["no-keys", "no-secret", "no-public", "placeholders", "disabled"])
def test_tracing_is_off_without_valid_keys(cfg, fake_langfuse):
    lf = lt.Langfuse({**cfg, "host": fake_langfuse["url"]})
    assert not lf.enabled
    lf.trace("t", name="x")
    lf.score("t", "s", 1)
    lf.flush(timeout=0.3)
    assert fake_langfuse["received"] == []
    assert not lf.auth_check()


@pytest.mark.negative
def test_unreachable_langfuse_never_raises():
    lf = client("http://127.0.0.1:9")          # nothing listens on port 9
    lf.trace("t", name="kb-chat")              # must not raise in the request thread
    lf.flush(timeout=15)
    assert lf.last_error and lf.sent == 0


@pytest.mark.negative
def test_rejected_keys_are_reported(fake_langfuse):
    fake_langfuse["status"]["code"] = 401
    lf = client(fake_langfuse["url"])
    lf.trace("t", name="kb-chat")
    lf.flush()
    assert lf.last_error.startswith("HTTP 401")
    assert not lf.auth_check()


@pytest.mark.negative
def test_broken_yaml_does_not_crash(tmp_path, monkeypatch):
    for var in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    f = tmp_path / "langfuse.yml"
    f.write_text("langfuse: [unclosed\n  :::")
    assert not lt.Langfuse(lt.load_config(f)).enabled


@pytest.mark.negative
def test_missing_yaml_means_tracing_off(tmp_path, monkeypatch):
    for var in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert not lt.Langfuse(lt.load_config(tmp_path / "nope.yml")).enabled
