"""Langfuse observability for KB Helper.

Every chat question becomes one Langfuse *trace*:

    kb-chat (trace)                     question -> answer, session = UI chat, tags = lang/tone/outcome
    ├── rewrite        (generation)     question + history -> English search query   (only when needed)
    ├── retrieve       (span)           search query -> top-k chunks with scores
    ├── no_answer      (event)          best score below the threshold, model not called
    └── answer         (generation)     prompt with numbered sources -> streamed answer, tokens, TTFT

and 👍/👎 in the UI arrives as a `user_feedback` score on that trace.

Keys are read from langfuse.yml (gitignored, see langfuse.example.yml) or from the
LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST environment variables.
Without keys tracing is off and every call here is a cheap no-op.

Spans go to the OpenTelemetry endpoint (POST /api/public/otel/v1/traces, OTLP/JSON) and
scores to POST /api/public/scores, from a background thread, so tracing never slows down
or breaks an answer. Langfuse Cloud no longer accepts traces through the old ingestion API.
"""
import hashlib
import json
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

CONFIG_FILE = Path(os.getenv("LANGFUSE_CONFIG", Path(__file__).parent / "langfuse.yml"))
DEFAULT_HOST = "https://cloud.langfuse.com"


def load_config(path=CONFIG_FILE):
    """langfuse.yml first, environment variables override it."""
    cfg = {}
    try:
        import yaml
        if Path(path).is_file():
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
            cfg = data.get("langfuse", data) if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[langfuse] cannot read {path}: {e}")
    env = {"public_key": "LANGFUSE_PUBLIC_KEY", "secret_key": "LANGFUSE_SECRET_KEY",
           "host": "LANGFUSE_HOST", "environment": "LANGFUSE_ENVIRONMENT"}
    for key, var in env.items():
        if os.getenv(var):
            cfg[key] = os.getenv(var)
    cfg["host"] = str(cfg.get("host") or DEFAULT_HOST).rstrip("/")
    return cfg


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id():
    """A trace id: 32 hex characters, as OpenTelemetry requires."""
    return uuid.uuid4().hex


def otel_trace_id(trace_id):
    """Old ids were UUIDs with dashes (still stored in kb_queries.trace_id)."""
    return str(trace_id).replace("-", "").lower()


def _span_id():
    return uuid.uuid4().hex[:16]


def _root_span_id(trace_id):
    return hashlib.sha256(trace_id.encode()).hexdigest()[:16]


def _ns(iso=None):
    if not iso:
        return time.time_ns()
    dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    return int(dt.timestamp() * 1e9)


def _text(v):
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)


def _attr(key, value):
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    elif isinstance(value, (list, tuple)) and all(isinstance(x, str) for x in value):
        v = {"arrayValue": {"values": [{"stringValue": x} for x in value]}}
    else:
        v = {"stringValue": _text(value)}
    return {"key": key, "value": v}


class Langfuse:
    """Minimal batched Langfuse client: OTLP/JSON spans + the scores API."""

    def __init__(self, cfg=None, flush_interval=1.0, batch_size=50):
        cfg = load_config() if cfg is None else cfg
        self.public_key = str(cfg.get("public_key") or "")
        self.secret_key = str(cfg.get("secret_key") or "")
        self.host = str(cfg.get("host") or DEFAULT_HOST).rstrip("/")
        self.environment = str(cfg.get("environment") or "default")
        self.enabled = (bool(self.public_key and self.secret_key)
                        and cfg.get("enabled", True) is not False
                        and not self.public_key.startswith("pk-lf-xxxx"))
        self.flush_interval = flush_interval
        self.batch_size = batch_size
        self.last_error = None
        self.sent = 0
        self._q = queue.Queue()
        self._worker = None
        self._lock = threading.Lock()
        self._traces = {}          # trace id -> {"start": ns, "fields": {...}} until the trace has an output

    # ------------------------------------------------------------ transport
    def _enqueue(self, kind, item):
        if not self.enabled:
            return
        self._q.put((kind, item))
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, name="langfuse", daemon=True)
                self._worker.start()

    def _run(self):
        while True:
            batch = [self._q.get()]
            deadline = time.monotonic() + self.flush_interval
            while len(batch) < self.batch_size:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                try:
                    batch.append(self._q.get(timeout=left))
                except queue.Empty:
                    break
            self._send(batch)
            for _ in batch:
                self._q.task_done()

    def _post(self, path, payload):
        try:
            r = requests.post(f"{self.host}{path}", json=payload,
                              auth=(self.public_key, self.secret_key), timeout=10)
            if r.status_code >= 300:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            return True
        except Exception as e:
            self.last_error = str(e)
            print(f"[langfuse] {path}: {e}")
            return False

    def _send(self, batch):
        self.last_error = None
        spans = [item for kind, item in batch if kind == "span"]
        if spans and self._post("/api/public/otel/v1/traces", {"resourceSpans": [{
                "resource": {"attributes": [_attr("service.name", "kb-helper"),
                                            _attr("deployment.environment", self.environment)]},
                "scopeSpans": [{"scope": {"name": "kb-helper"}, "spans": spans}]}]}):
            self.sent += len(spans)
        for kind, item in batch:
            if kind == "score" and self._post("/api/public/scores", item):
                self.sent += 1

    def flush(self, timeout=10.0):
        """Block until everything queued so far is sent (tests, shutdown)."""
        end = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < end:
            time.sleep(0.05)

    def auth_check(self):
        """True if the keys are accepted by the Langfuse project."""
        if not self.enabled:
            return False
        try:
            r = requests.get(f"{self.host}/api/public/projects",
                             auth=(self.public_key, self.secret_key), timeout=10)
            self.last_error = None if r.ok else f"HTTP {r.status_code}: {r.text[:200]}"
            return r.ok
        except Exception as e:
            self.last_error = str(e)
            return False

    # ------------------------------------------------------------ objects
    def _shared(self, fields):
        """Trace attributes repeated on every span, so sessions and tags filter observations too."""
        out = []
        if fields.get("sessionId"):
            out.append(_attr("langfuse.session.id", str(fields["sessionId"])))
        if fields.get("userId"):
            out.append(_attr("langfuse.user.id", str(fields["userId"])))
        if fields.get("tags"):
            out.append(_attr("langfuse.trace.tags", [str(x) for x in fields["tags"]]))
        return out

    def _span(self, trace_id, span_id, parent, name, start_ns, end_ns, attrs, error=False):
        attrs = [_attr("langfuse.environment", self.environment)] + attrs
        span = {"traceId": trace_id, "spanId": span_id, "name": name, "kind": 1,
                "startTimeUnixNano": str(start_ns), "endTimeUnixNano": str(max(end_ns, start_ns)),
                "attributes": attrs, "status": {"code": 2} if error else {}}
        if parent:
            span["parentSpanId"] = parent
        self._enqueue("span", span)

    def trace(self, trace_id, **fields):
        """Set trace fields. Calls merge; the trace (its root span) is sent once it has an output."""
        if not self.enabled:
            return
        tid = otel_trace_id(trace_id)
        with self._lock:
            t = self._traces.setdefault(tid, {"start": time.time_ns(), "fields": {}})
            t["fields"].update(_clean(fields))
            if "output" not in t["fields"] and len(self._traces) < 1000:
                return
            self._traces.pop(tid, None)
        f = t["fields"]
        attrs = [_attr("langfuse.trace.name", f.get("name", "trace"))]
        for key, attr in (("input", "langfuse.trace.input"), ("output", "langfuse.trace.output"),
                          ("input", "langfuse.observation.input"), ("output", "langfuse.observation.output")):
            if key in f:
                attrs.append(_attr(attr, _text(f[key])))
        attrs += self._shared(f)
        for k, v in (f.get("metadata") or {}).items():
            attrs.append(_attr(f"langfuse.trace.metadata.{k}", _text(v)))
        error = "error" in (f.get("tags") or [])
        self._span(tid, _root_span_id(tid), None, f.get("name", "trace"), t["start"], time.time_ns(),
                   attrs, error)

    def _observation(self, kind, trace_id, name, start, end, fields):
        if not self.enabled:
            return
        tid = otel_trace_id(trace_id)
        f = _clean(fields)
        with self._lock:
            pending = dict(self._traces.get(tid, {}).get("fields", {}))
        attrs = [_attr("langfuse.observation.type", kind)] + self._shared(pending)
        for key, attr in (("input", "langfuse.observation.input"), ("output", "langfuse.observation.output"),
                          ("model", "langfuse.observation.model.name"),
                          ("modelParameters", "langfuse.observation.model.parameters"),
                          ("usage", "langfuse.observation.usage_details"),
                          ("completionStartTime", "langfuse.observation.completion_start_time"),
                          ("level", "langfuse.observation.level"),
                          ("statusMessage", "langfuse.observation.status_message")):
            if key in f:
                attrs.append(_attr(attr, _text(f[key])))
        for k, v in (f.get("metadata") or {}).items():
            attrs.append(_attr(f"langfuse.observation.metadata.{k}", _text(v)))
        start_ns = _ns(start)
        self._span(tid, _span_id(), _root_span_id(tid), name, start_ns, _ns(end) if end else time.time_ns(),
                   attrs, f.get("level") == "ERROR")

    def span(self, trace_id, name, start, end=None, **fields):
        self._observation("span", trace_id, name, start, end, fields)

    def generation(self, trace_id, name, start, end=None, **fields):
        self._observation("generation", trace_id, name, start, end, fields)

    def event(self, trace_id, name, **fields):
        now = now_iso()
        self._observation("event", trace_id, name, now, now, fields)

    def score(self, trace_id, name, value, comment=None, data_type="NUMERIC", score_id=None):
        # a fixed id per (trace, name) means a changed 👍/👎 overwrites the old score
        tid = otel_trace_id(trace_id)
        self._enqueue("score", _clean({"id": score_id or f"{tid}-{name}", "traceId": tid, "name": name,
                                       "value": value, "dataType": data_type, "comment": comment,
                                       "environment": self.environment}))


def _clean(d):
    return {k: v for k, v in d.items() if v is not None}


def usage(prompt_tokens, completion_tokens):
    if not (prompt_tokens or completion_tokens):
        return None
    p, c = prompt_tokens or 0, completion_tokens or 0
    return {"input": p, "output": c, "total": p + c}


langfuse = Langfuse()


if __name__ == "__main__":
    # python langfuse_tracing.py  ->  checks the keys and sends one test trace
    lf = Langfuse()
    print(f"config : {CONFIG_FILE}")
    print(f"host   : {lf.host}")
    if not lf.enabled:
        raise SystemExit("off    : put public_key and secret_key into langfuse.yml (see langfuse.example.yml)")
    if not lf.auth_check():
        raise SystemExit(f"error  : keys rejected ({lf.last_error})")
    tid = new_id()
    lf.trace(tid, name="langfuse-check", input="ping", tags=["check"])
    lf.span(tid, "check", now_iso(), input="ping", output="pong")
    lf.trace(tid, output="pong")
    lf.score(tid, "check", 1, data_type="BOOLEAN")
    lf.flush()
    print("error  : " + lf.last_error if lf.last_error else f"ok     : test trace {tid} sent -> {lf.host} (Tracing -> Traces)")
