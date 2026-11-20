"""Shared helpers for the automated QA suite.

The tests are black-box: they talk to a running KB Helper over HTTP only.
    KB_BASE_URL      server to test (default http://localhost:8091)
    KB_ADMIN_TOKEN   admin token for write endpoints (default: read from .env.local)
"""
import json
import os
import random
import string
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[2]
DATASET_FILE = ROOT / "tests" / "qa_dataset.json"
BASE_URL = os.getenv("KB_BASE_URL", "http://localhost:8091").rstrip("/")
QA_HEADERS = {"X-KB-Client": "qa-auto"}     # shows up as tag client:qa-auto in Langfuse


def read_env_local(name):
    f = ROOT / ".env.local"
    if not f.is_file():
        return ""
    for line in f.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    return ""


ADMIN_TOKEN = os.getenv("KB_ADMIN_TOKEN") or read_env_local("KB_ADMIN_TOKEN")


def load_dataset():
    data = json.loads(DATASET_FILE.read_text(encoding="utf-8"))
    return data["cases"]


def dataset_params(kind):
    """pytest params for every case of one type; known defects become xfail."""
    params = []
    for case in load_dataset():
        if case["type"] != kind:
            continue
        marks = [getattr(pytest.mark, kind)]
        if case.get("expected_intent") != "rejected":
            marks.append(pytest.mark.llm)
        if case.get("known_issue"):
            marks.append(pytest.mark.xfail(reason=f"known defect {case['known_issue']}", strict=False))
        params.append(pytest.param(case, id=case["id"], marks=marks))
    return params


def long_gibberish(n=6000, seed=1):
    rnd = random.Random(seed)
    words = ("".join(rnd.choices(string.ascii_lowercase, k=rnd.randint(3, 9))) for _ in range(n // 6))
    return " ".join(words)[:n]


@dataclass
class ChatResult:
    status: int
    events: list = field(default_factory=list)       # event names in order
    data: dict = field(default_factory=dict)         # last payload of each event
    answer: str = ""
    error: str = ""

    @property
    def sources(self):
        return (self.data.get("sources") or {}).get("sources", [])

    @property
    def source_names(self):
        return [s["source"] for s in self.sources]


def chat(messages, timeout=300, **extra):
    """POST /kb/chat/stream and collect the Server-Sent Events."""
    body = {"messages": messages, "think": False, **extra}
    r = requests.post(f"{BASE_URL}/kb/chat/stream", json=body, stream=True,
                      timeout=timeout, headers=QA_HEADERS)
    res = ChatResult(status=r.status_code)
    if r.status_code != 200:
        res.error = r.text
        return res
    name, tokens = None, []
    for line in r.iter_lines(decode_unicode=True):
        if line.startswith("event:"):
            name = line[6:].strip()
            res.events.append(name)
        elif line.startswith("data:") and name:
            payload = json.loads(line[5:])
            res.data[name] = payload
            if name == "token":
                tokens.append(payload["t"])
    # "replace" carries the final text when the server added citations or removed a leaked sentence
    res.answer = res.data["replace"]["text"] if "replace" in res.data else "".join(tokens)
    res.error = (res.data.get("error") or {}).get("detail", "")
    return res


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


@pytest.fixture(scope="session")
def server_is_up():
    """Use with pytestmark = pytest.mark.usefixtures("server_is_up") in modules that need the API."""
    try:
        requests.get(f"{BASE_URL}/kb/stats", timeout=5).raise_for_status()
    except Exception as e:
        pytest.exit(f"KB Helper is not running at {BASE_URL} ({e}). Start it with ./kbhelper", returncode=2)


@pytest.fixture(scope="session")
def admin_headers():
    return {"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}


@pytest.fixture(scope="session")
def admin_required():
    return requests.get(f"{BASE_URL}/kb/stats", timeout=5).json().get("admin_required", False)


# ---------------------------------------------------------------- dataset checks

def is_cyrillic(text):
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum("\u0400" <= c <= "\u04ff" for c in letters) / len(letters) > 0.3


RO_MARKS = set("ăâîșşțţ")
RO_WORDS = {"este", "și", "sunt", "pentru", "care", "oferim", "de", "o", "în", "cu", "baze", "date"}


def looks_romanian(text):
    low = text.lower()
    words = set(low.replace(",", " ").replace(".", " ").split())
    return any(c in RO_MARKS for c in low) or len(words & RO_WORDS) >= 3


def build_request(case):
    """Turn a dataset case into the chat request body."""
    extra = dict(case.get("request") or {})
    if "messages" in extra:
        return extra.pop("messages"), extra
    prompt = case["prompt"]
    if prompt == "$LONG_GIBBERISH":        # long, but under the 8000-character limit
        prompt = long_gibberish(6000)
    elif prompt == "$TOO_LONG":            # over the limit
        prompt = "What is PgMex? " * 600
    messages = list(case.get("history") or []) + [{"role": "user", "content": prompt}]
    return messages, extra


def forbidden_words(case):
    out = []
    for w in case.get("forbidden_in_response") or []:
        if w == "$ADMIN_TOKEN":
            if ADMIN_TOKEN:
                out.append(ADMIN_TOKEN)
        else:
            out.append(w)
    return out


def check_case(case):
    """Run one dataset case against the API and assert everything it expects."""
    messages, extra = build_request(case)
    res = chat(messages, **extra)
    cid, intent = case["id"], case["expected_intent"]

    assert res.status == case.get("expected_status", 200), f"{cid}: HTTP {res.status} {res.error[:200]}"
    if intent == "rejected":
        return res

    assert "error" not in res.events, f"{cid}: server error event: {res.error}"
    assert res.events and res.events[-1] == "done", f"{cid}: stream did not finish: {res.events}"
    assert "sources" in res.events, f"{cid}: no sources event"

    if intent == "safe_refusal" and "no_answer" in res.events:
        return res            # stopped by the relevance gate: nothing reached the model
    if intent == "safe_refusal":
        assert "token" in res.events, f"{cid}: neither 'not in KB' nor an answer"
    elif intent == "no_answer":
        assert "no_answer" in res.events, f"{cid}: expected 'not in the KB', got answer: {res.answer[:200]!r}"
        assert "token" not in res.events, f"{cid}: the model must not be called for off-topic questions"
        assert res.sources == [], f"{cid}: off-topic question returned sources {res.source_names}"
    else:
        assert "no_answer" not in res.events, (
            f"{cid}: KB said 'not in the knowledge base' (best score "
            f"{res.data.get('no_answer', {}).get('best_score')})")
        assert res.answer.strip(), f"{cid}: empty answer"
        assert res.sources, f"{cid}: answer without sources"

    if case.get("expected_source"):
        assert case["expected_source"] in res.source_names, (
            f"{cid}: expected source '{case['expected_source']}' not retrieved, got {res.source_names}")
    if case.get("max_sources"):
        assert len(res.sources) <= case["max_sources"], f"{cid}: {len(res.sources)} sources"

    low = res.answer.lower()
    for kw in case.get("keywords_in_response") or []:
        assert kw.lower() in low, f"{cid}: keyword {kw!r} missing in answer: {res.answer[:300]!r}"
    if case.get("keywords_any"):
        assert any(k.lower() in low for k in case["keywords_any"]), (
            f"{cid}: none of {case['keywords_any']} in answer: {res.answer[:300]!r}")
    for w in forbidden_words(case):
        assert w.lower() not in low, f"{cid}: forbidden text {w[:12]!r}... leaked into the answer"

    if case.get("expect_citations"):
        import re
        assert re.search(r"\[\d+\]", res.answer), f"{cid}: no [n] citations in {res.answer[:200]!r}"

    if res.answer and intent in ("kb_answer", "honest_unknown"):
        lang = case.get("lang", "en")
        if lang == "ru":
            assert is_cyrillic(res.answer), f"{cid}: expected a Russian answer: {res.answer[:150]!r}"
        elif lang == "ro":
            assert looks_romanian(res.answer), f"{cid}: expected a Romanian answer: {res.answer[:150]!r}"
        else:
            assert not is_cyrillic(res.answer), f"{cid}: expected an English answer: {res.answer[:150]!r}"
    return res
