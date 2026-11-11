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

Events go to the public ingestion API (POST /api/public/ingestion) from a background
thread, so tracing never slows down or breaks an answer.
"""
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
    return str(uuid.uuid4())


class Langfuse:
    """Minimal batched client for the Langfuse ingestion API."""

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

    # ------------------------------------------------------------ transport
    def _enqueue(self, kind, body):
        if not self.enabled:
            return
        body.setdefault("environment", self.environment)
        self._q.put({"id": new_id(), "type": kind, "timestamp": now_iso(), "body": body})
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

    def _send(self, batch):
        try:
            r = requests.post(f"{self.host}/api/public/ingestion", json={"batch": batch},
                              auth=(self.public_key, self.secret_key), timeout=10)
            if r.status_code >= 300:
                self.last_error = f"HTTP {r.status_code}: {r.text[:200]}"
            else:
                errors = (r.json() or {}).get("errors") or []
                self.last_error = f"{len(errors)} rejected: {errors[0]}" if errors else None
                self.sent += len(batch) - len(errors)
            if self.last_error:
                print(f"[langfuse] {self.last_error}")
        except Exception as e:
            self.last_error = str(e)
            print(f"[langfuse] {e}")

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
    def trace(self, trace_id, **fields):
        """Create or update (upsert by id) a trace."""
        self._enqueue("trace-create", {"id": trace_id, "timestamp": now_iso(), **_clean(fields)})

    def span(self, trace_id, name, start, end=None, **fields):
        self._enqueue("span-create", {"id": new_id(), "traceId": trace_id, "name": name,
                                      "startTime": start, "endTime": end or now_iso(), **_clean(fields)})

    def generation(self, trace_id, name, start, end=None, **fields):
        self._enqueue("generation-create", {"id": new_id(), "traceId": trace_id, "name": name,
                                            "startTime": start, "endTime": end or now_iso(),
                                            **_clean(fields)})

    def event(self, trace_id, name, **fields):
        self._enqueue("event-create", {"id": new_id(), "traceId": trace_id, "name": name,
                                       "startTime": now_iso(), **_clean(fields)})

    def score(self, trace_id, name, value, comment=None, data_type="NUMERIC", score_id=None):
        # a fixed id per (trace, name) means a changed 👍/👎 overwrites the old score
        self._enqueue("score-create", _clean({"id": score_id or f"{trace_id}-{name}", "traceId": trace_id,
                                              "name": name, "value": value, "dataType": data_type,
                                              "comment": comment}))


def _clean(d):
    return {k: v for k, v in d.items() if v is not None}


def usage(prompt_tokens, completion_tokens):
    if not (prompt_tokens or completion_tokens):
        return None
    p, c = prompt_tokens or 0, completion_tokens or 0
    return {"input": p, "output": c, "total": p + c, "unit": "TOKENS"}


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
    start = now_iso()
    lf.trace(tid, name="langfuse-check", input="ping", output="pong", tags=["check"])
    lf.span(tid, "check", start, input="ping", output="pong")
    lf.flush()
    print("error  : " + lf.last_error if lf.last_error else f"ok     : test trace sent -> {lf.host}/trace/{tid}")
