"""Shared helpers for the automated QA suite.

The tests are black-box: they talk to a running KB Helper over HTTP only.
    KB_BASE_URL      server to test (default http://localhost:8091)
    KB_ADMIN_TOKEN   admin token for write endpoints (default: read from .env.local)
"""
import os
from pathlib import Path

import pytest
import requests

import qa_eval
from qa_eval import ChatResult, is_cyrillic, load_dataset, long_gibberish, looks_romanian  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
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


def chat(messages, timeout=300, **extra):
    """POST /kb/chat/stream to the server under test and collect the Server-Sent Events."""
    return qa_eval.chat(BASE_URL, messages, timeout=timeout, headers=QA_HEADERS, **extra)


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

def check_case(case):
    """Run one dataset case against the API and assert every check of it (see qa_eval.evaluate).
    The Quality page in the UI runs exactly the same checks."""
    messages, extra = qa_eval.build_request(case)
    res = chat(messages, **extra)
    failed = [c for c in qa_eval.evaluate(case, res, ADMIN_TOKEN) if not c["ok"]]
    assert not failed, f"{case['id']}: " + "; ".join(f"{c['name']}: {c['detail']}" for c in failed)
    return res
