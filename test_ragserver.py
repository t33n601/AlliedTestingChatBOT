"""Integration (black-box) tests: they talk to the running API over HTTP only.
Start the DB, Ollama and `uvicorn ragserver:app --port 8091` first."""
import requests

BASE_URL = "http://localhost:8091"


def make_payload(prefix):
    return {
        "source": f"{prefix} Source", "heading": f"{prefix} Heading",
        "body": f"{prefix} Body",
        "source_vec": [0.1] * 10, "heading_vec": [0.2] * 10,
        "body_vec": [0.3] * 10,
    }


def create(prefix):
    response = requests.post(f"{BASE_URL}/kb/", json=make_payload(prefix))
    assert response.status_code == 201
    return response.json()["id"]


def test_list_chunks():
    response = requests.get(f"{BASE_URL}/kb/")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_create_chunk():
    payload = make_payload("Test")
    response = requests.post(f"{BASE_URL}/kb/", json=payload)
    assert response.status_code == 201
    new_id = response.json()["id"]

    response = requests.get(f"{BASE_URL}/kb/{new_id}")
    assert response.status_code == 200
    assert response.json()["heading"] == payload["heading"]

    requests.delete(f"{BASE_URL}/kb/{new_id}")


def test_reject_empty_fields():
    payload = make_payload("Bad")
    payload["heading"] = "   "
    response = requests.post(f"{BASE_URL}/kb/", json=payload)
    assert response.status_code == 400


def test_reject_missing_fields():
    response = requests.post(f"{BASE_URL}/kb/", json={"source": "only source"})
    assert response.status_code == 422


def test_update_chunk():
    new_id = create("Old")
    payload = make_payload("New")
    response = requests.put(f"{BASE_URL}/kb/{new_id}", json=payload)
    assert response.status_code == 200

    data = requests.get(f"{BASE_URL}/kb/{new_id}").json()
    assert data["body"] == payload["body"]

    requests.delete(f"{BASE_URL}/kb/{new_id}")


def test_delete_chunk():
    new_id = create("ToDelete")
    response = requests.delete(f"{BASE_URL}/kb/{new_id}")
    assert response.status_code == 200
    assert requests.get(f"{BASE_URL}/kb/{new_id}").status_code == 404


def test_get_chunk_by_id():
    new_id = create("ById")
    data = requests.get(f"{BASE_URL}/kb/{new_id}").json()
    assert data["id"] == new_id
    assert data["source"] == "ById Source"
    assert len(data["body_vec"]) > 0
    requests.delete(f"{BASE_URL}/kb/{new_id}")


def test_missing_chunk_404():
    assert requests.get(f"{BASE_URL}/kb/999999999").status_code == 404
    assert requests.delete(f"{BASE_URL}/kb/999999999").status_code == 404


def test_search_returns_chunks():
    response = requests.get(f"{BASE_URL}/kb/search", params={"query": "QA and testing services"})
    assert response.status_code == 200
    assert len(response.json()) > 0


def test_answer_with_history():
    response = requests.post(f"{BASE_URL}/kb/answer",
                             json=["What services do you offer?"], timeout=300)
    assert response.status_code == 200
    assert response.json()["answer"]


# ---------------------------------------------------------------- new features

def test_retrieve_returns_scores_and_map_point():
    data = requests.get(f"{BASE_URL}/kb/retrieve", params={"query": "penetration testing", "limit": 3}).json()
    assert len(data["sources"]) == 3
    scores = [s["score"] for s in data["sources"]]
    assert scores == sorted(scores, reverse=True)
    assert 0 <= data["point"]["x"] <= 1.1


def test_knowledge_map_has_point_per_chunk():
    points = requests.get(f"{BASE_URL}/kb/map", timeout=60).json()["points"]
    stats = requests.get(f"{BASE_URL}/kb/stats").json()
    assert len(points) == stats["vectorized"]


def test_upload_search_and_delete_document():
    md = b"# Unicorn policy\nEvery tester gets a purple unicorn sticker after 100 closed bugs.\n"
    r = requests.post(f"{BASE_URL}/kb/upload", files={"file": ("pytest-unicorns.md", md)})
    assert r.status_code == 201 and r.json()["chunks"] == 1
    try:
        hit = requests.get(f"{BASE_URL}/kb/retrieve", params={"query": "purple unicorn sticker"}).json()
        assert hit["sources"][0]["source"] == "pytest-unicorns"
        dup = requests.post(f"{BASE_URL}/kb/upload", files={"file": ("pytest-unicorns.md", md)})
        assert dup.status_code == 409
    finally:
        assert requests.delete(f"{BASE_URL}/kb/documents/pytest-unicorns").status_code == 200


def test_upload_rejects_unknown_type():
    r = requests.post(f"{BASE_URL}/kb/upload", files={"file": ("virus.exe", b"MZ")})
    assert r.status_code == 400


def test_chat_stream_event_order():
    r = requests.post(f"{BASE_URL}/kb/chat/stream", stream=True, timeout=300, json={
        "messages": [{"role": "user", "content": "What services do you offer?"}], "think": False})
    events = [line[7:] for line in r.iter_lines(decode_unicode=True) if line.startswith("event: ")]
    assert events[0] == "sources"
    assert "token" in events
    assert events[-1] == "done"


def test_chat_stream_respects_top_k():
    r = requests.post(f"{BASE_URL}/kb/chat/stream", stream=True, timeout=300, json={
        "messages": [{"role": "user", "content": "QA services"}], "think": False,
        "top_k": 2, "tone": "brief"})
    lines = r.iter_lines(decode_unicode=True)
    for line in lines:
        if line == "event: sources":
            import json
            data = json.loads(next(lines)[6:])
            assert len(data["sources"]) <= 2
            break
    r.close()


def test_chat_stream_refuses_off_topic_question():
    r = requests.post(f"{BASE_URL}/kb/chat/stream", stream=True, timeout=300, json={
        "messages": [{"role": "user", "content": "How do I bake a chocolate cake?"}], "think": False})
    events = [line[7:] for line in r.iter_lines(decode_unicode=True) if line.startswith("event: ")]
    assert "no_answer" in events
    assert "token" not in events
    assert events[-1] == "done"


def test_chat_stream_rejects_unknown_model():
    r = requests.post(f"{BASE_URL}/kb/chat/stream", json={
        "messages": [{"role": "user", "content": "hi"}], "model": "no-such-model:1b"})
    assert r.status_code == 400


def test_models_endpoint_lists_default():
    data = requests.get(f"{BASE_URL}/kb/models").json()
    assert data["default"] in data["models"]
    assert "formal" in data["tones"]


def test_reembed_document():
    r = requests.post(f"{BASE_URL}/kb/documents/security/reembed", timeout=120)
    assert r.status_code == 200 and r.json()["chunks"] > 0
    assert requests.post(f"{BASE_URL}/kb/documents/no-such-doc/reembed").status_code == 404
