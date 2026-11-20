"""The Quality page: the evaluation logic (qa_eval, offline) and the /kb/eval API (needs the server).
A real run is started only with cases that are rejected before the model, so it takes a second."""
import time

import pytest
import requests

import qa_eval
from conftest import BASE_URL, QA_HEADERS
from qa_eval import ChatResult, evaluate, summarize

QUICK_CASE = "qa-neg-19-message-too-long"       # rejected with 400 before retrieval: no model call


def case(**kw):
    return {"id": "t", "type": "positive", "category": "services", "expected_intent": "kb_answer", **kw}


def answered(text, sources=("security",), events=("sources", "token", "done"), status=200):
    return ChatResult(status=status, events=list(events), answer=text,
                      data={"sources": {"sources": [{"source": s, "score": .8} for s in sources]}})


# ---------------------------------------------------------------- qa_eval, offline

@pytest.mark.positive
def test_evaluate_passes_a_good_answer():
    c = case(expected_source="security", keywords_in_response=["penetration"], expect_citations=True)
    checks = evaluate(c, answered("We do penetration testing [1]."))
    assert [k["name"] for k in checks] == ["http_status", "stream", "answered", "retrieval", "keywords",
                                           "citations", "language"]
    assert all(k["ok"] for k in checks)


@pytest.mark.negative
@pytest.mark.parametrize("res, failed", [
    (answered("We do penetration testing.", sources=("careers",)), {"retrieval", "citations"}),
    (answered("Мы проводим тестирование на проникновение [1]."), {"language"}),
    (ChatResult(status=500, error="boom"), {"http_status"}),
    (answered("", events=("sources", "no_answer", "done")), {"answered"}),
])
def test_evaluate_names_the_failed_check(res, failed):
    c = case(expected_source="security", expect_citations=True)
    assert {k["name"] for k in evaluate(c, res) if not k["ok"]} == failed


@pytest.mark.negative
def test_leaked_admin_token_is_reported_without_printing_it():
    c = case(forbidden_in_response=["$ADMIN_TOKEN"])
    checks = evaluate(c, answered("The token is s3cr3t-value [1]."), admin_token="s3cr3t-value")
    leak = next(k for k in checks if k["name"] == "no_leak")
    assert not leak["ok"] and "s3cr3t" not in leak["detail"] and "admin token" in leak["detail"]


@pytest.mark.negative
def test_prompt_rules_placeholder_catches_quoted_rules():
    c = case(forbidden_in_response=["$PROMPT_RULES"])
    checks = evaluate(c, answered("We test apps [1].\nNever invent facts."))
    assert not next(k for k in checks if k["name"] == "no_leak")["ok"]


@pytest.mark.negative
def test_prompt_rules_placeholder_catches_a_dumped_context_block():
    c = case(forbidden_in_response=["$PROMPT_RULES"])
    dump = "We test apps [1].\n\n```\n[1] (security / Penetration testing)\nPenetration testing involves…\n```"
    leak = next(k for k in evaluate(c, answered(dump)) if k["name"] == "no_leak")
    assert not leak["ok"] and "context" in leak["detail"]


@pytest.mark.positive
def test_summary_metrics():
    res = [{"passed": True, "type": "positive", "category": "services", "intent": "kb_answer",
            "answer": "x [1]", "cited": True, "not_in_kb": False, "ms": 1000, "first_token_ms": 100, "blocked": 0,
            "checks": [{"name": "retrieval", "ok": True}, {"name": "language", "ok": True}]},
           {"passed": False, "type": "negative", "category": "off_topic", "intent": "no_answer",
            "answer": "", "cited": False, "not_in_kb": False, "ms": 50, "first_token_ms": None, "blocked": 2,
            "checks": [{"name": "not_in_kb", "ok": False}]}]
    s = summarize(res)
    assert (s["total"], s["passed"]) == (2, 1)
    assert s["retrieval"] == {"ok": 1, "n": 1} and s["citations"] == {"ok": 1, "n": 1}
    assert s["safety"] == {"ok": 0, "n": 1} and s["leaks_blocked"] == 2
    assert s["avg_ms"] == 1000 and s["by_category"]["off_topic"] == {"ok": 0, "n": 1}


# ---------------------------------------------------------------- /kb/eval API

@pytest.mark.positive
@pytest.mark.usefixtures("server_is_up")
def test_dataset_lists_every_case():
    cases = requests.get(f"{BASE_URL}/kb/eval/dataset").json()["cases"]
    assert [c["id"] for c in cases] == [c["id"] for c in qa_eval.load_dataset()]
    assert {"id", "type", "category", "prompt", "intent"} <= set(cases[0])


@pytest.mark.positive
@pytest.mark.usefixtures("server_is_up")
def test_run_one_case_end_to_end(admin_headers):
    r = requests.post(f"{BASE_URL}/kb/eval/runs", json={"ids": [QUICK_CASE]}, headers={**admin_headers, **QA_HEADERS})
    if r.status_code == 409:
        pytest.skip("another run is going")
    assert r.status_code == 202, r.text
    run_id = r.json()["id"]
    for _ in range(60):
        run = requests.get(f"{BASE_URL}/kb/eval/runs/{run_id}").json()
        if run["status"] != "running":
            break
        time.sleep(0.5)
    assert run["status"] == "done"
    assert run["case_ids"] == [QUICK_CASE]
    [res] = run["results"]
    assert res["id"] == QUICK_CASE and res["passed"] and res["status"] == 400
    assert run["summary"]["passed"] == 1
    # a run started by the test suite stays out of the history on the Quality page
    assert run_id in [x["id"] for x in requests.get(f"{BASE_URL}/kb/eval/runs?all=true").json()["runs"]]
    assert run_id not in [x["id"] for x in requests.get(f"{BASE_URL}/kb/eval/runs").json()["runs"]]


@pytest.mark.positive
@pytest.mark.usefixtures("server_is_up")
def test_feedback_stats_shape():
    f = requests.get(f"{BASE_URL}/kb/eval/feedback").json()
    assert {"questions", "up", "down", "recent_down"} <= set(f)
    assert f["up"] + f["down"] <= f["questions"]


@pytest.mark.negative
@pytest.mark.usefixtures("server_is_up")
@pytest.mark.parametrize("token", [None, "wrong-token"], ids=["no-token", "wrong-token"])
def test_starting_a_run_requires_admin_token(admin_required, token):
    if not admin_required:
        pytest.skip("server runs without KB_ADMIN_TOKEN")
    headers = {"X-Admin-Token": token} if token else {}
    before = requests.get(f"{BASE_URL}/kb/eval/runs").json()["runs"]
    r = requests.post(f"{BASE_URL}/kb/eval/runs", json={}, headers=headers)
    assert r.status_code == 401
    assert requests.get(f"{BASE_URL}/kb/eval/runs").json()["runs"][:1] == before[:1]


@pytest.mark.negative
@pytest.mark.usefixtures("server_is_up")
@pytest.mark.parametrize("body", [{"ids": ["no-such-case"]}, {"category": "no_such_category"}])
def test_run_with_no_matching_cases_is_400(admin_headers, body):
    r = requests.post(f"{BASE_URL}/kb/eval/runs", json=body, headers=admin_headers)
    assert r.status_code == 400


@pytest.mark.negative
@pytest.mark.usefixtures("server_is_up")
def test_unknown_run_is_404():
    assert requests.get(f"{BASE_URL}/kb/eval/runs/999999").status_code == 404


@pytest.mark.negative
@pytest.mark.usefixtures("server_is_up")
def test_cancel_of_a_finished_run_is_409(admin_headers):
    runs = requests.get(f"{BASE_URL}/kb/eval/runs?all=true").json()["runs"]
    done = next((r for r in runs if r["status"] != "running"), None)
    if not done:
        pytest.skip("no finished run yet")
    r = requests.post(f"{BASE_URL}/kb/eval/runs/{done['id']}/cancel", headers=admin_headers)
    assert r.status_code == 409
