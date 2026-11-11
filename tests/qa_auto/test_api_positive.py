"""Positive API scenarios: valid requests to every endpoint return the expected data."""
import pytest
import requests

from conftest import BASE_URL

pytestmark = [pytest.mark.positive, pytest.mark.usefixtures("server_is_up")]


def make_payload(prefix):
    return {"source": f"{prefix} Source", "heading": f"{prefix} Heading", "body": f"{prefix} Body",
            "source_vec": [0.1] * 10, "heading_vec": [0.2] * 10, "body_vec": [0.3] * 10}


@pytest.fixture
def chunk(admin_headers):
    """A temporary chunk, deleted after the test."""
    r = requests.post(f"{BASE_URL}/kb/", json=make_payload("QA"), headers=admin_headers)
    assert r.status_code == 201, r.text
    chunk_id = r.json()["id"]
    yield chunk_id
    requests.delete(f"{BASE_URL}/kb/{chunk_id}", headers=admin_headers)


def test_stats_describe_the_knowledge_base():
    s = requests.get(f"{BASE_URL}/kb/stats").json()
    assert s["chunks"] > 0 and s["vectorized"] == s["chunks"]
    assert s["documents"] > 0
    assert s["dimensions"] == 768
    assert "langfuse" in s


def test_list_chunks():
    r = requests.get(f"{BASE_URL}/kb/")
    assert r.status_code == 200
    assert isinstance(r.json(), list) and r.json()


def test_create_and_read_chunk(chunk):
    data = requests.get(f"{BASE_URL}/kb/{chunk}").json()
    assert data["id"] == chunk
    assert data["heading"] == "QA Heading"
    assert len(data["body_vec"]) > 0


def test_update_chunk(chunk, admin_headers):
    r = requests.put(f"{BASE_URL}/kb/{chunk}", json=make_payload("Updated"), headers=admin_headers)
    assert r.status_code == 200
    assert requests.get(f"{BASE_URL}/kb/{chunk}").json()["body"] == "Updated Body"


def test_delete_chunk(admin_headers):
    r = requests.post(f"{BASE_URL}/kb/", json=make_payload("ToDelete"), headers=admin_headers)
    chunk_id = r.json()["id"]
    assert requests.delete(f"{BASE_URL}/kb/{chunk_id}", headers=admin_headers).status_code == 200
    assert requests.get(f"{BASE_URL}/kb/{chunk_id}").status_code == 404


def test_search_returns_chunks():
    r = requests.get(f"{BASE_URL}/kb/search", params={"query": "QA and testing services"})
    assert r.status_code == 200 and len(r.json()) > 0


def test_retrieve_returns_sorted_scores_and_map_point():
    data = requests.get(f"{BASE_URL}/kb/retrieve", params={"query": "penetration testing", "limit": 3}).json()
    assert len(data["sources"]) == 3
    scores = [s["score"] for s in data["sources"]]
    assert scores == sorted(scores, reverse=True)
    assert "security" in [s["source"] for s in data["sources"]]
    assert 0 <= data["point"]["x"] <= 1.1


def test_knowledge_map_has_a_point_per_chunk():
    points = requests.get(f"{BASE_URL}/kb/map", timeout=120).json()["points"]
    assert len(points) == requests.get(f"{BASE_URL}/kb/stats").json()["vectorized"]


def test_models_endpoint_lists_default_model_and_tones():
    data = requests.get(f"{BASE_URL}/kb/models").json()
    assert data["default"] in data["models"]
    assert {"formal", "friendly", "brief", "simple"} <= set(data["tones"])


def test_documents_list_and_chunks():
    docs = requests.get(f"{BASE_URL}/kb/documents").json()
    names = [d["source"] for d in docs]
    assert "aboutus" in names
    chunks = requests.get(f"{BASE_URL}/kb/documents/aboutus").json()
    assert chunks and all("heading" in c for c in chunks.get("chunks", chunks))


@pytest.mark.llm
def test_upload_search_and_delete_document(admin_headers):
    md = b"# Unicorn policy\nEvery tester gets a purple unicorn sticker after 100 closed bugs.\n"
    r = requests.post(f"{BASE_URL}/kb/upload", files={"file": ("pytest-unicorns.md", md)}, headers=admin_headers)
    assert r.status_code == 201 and r.json()["chunks"] == 1
    try:
        hit = requests.get(f"{BASE_URL}/kb/retrieve", params={"query": "purple unicorn sticker"}).json()
        assert hit["sources"][0]["source"] == "pytest-unicorns"
    finally:
        assert requests.delete(f"{BASE_URL}/kb/documents/pytest-unicorns",
                               headers=admin_headers).status_code == 200


def test_reembed_document(admin_headers):
    r = requests.post(f"{BASE_URL}/kb/documents/security/reembed", timeout=120, headers=admin_headers)
    assert r.status_code == 200 and r.json()["chunks"] > 0


@pytest.mark.llm
def test_answer_endpoint_with_history():
    r = requests.post(f"{BASE_URL}/kb/answer", json=["What services do you offer?"], timeout=300)
    assert r.status_code == 200 and r.json()["answer"]


@pytest.mark.llm
def test_chat_stream_event_order():
    from conftest import chat
    res = chat([{"role": "user", "content": "What services do you offer?"}])
    assert res.events[0] == "sources"
    assert "token" in res.events
    assert res.events[-1] == "done"
    done = res.data["done"]
    assert done["query_id"] and done["timings"]["total_ms"] > 0


@pytest.mark.llm
def test_feedback_is_saved():
    from conftest import chat
    qid = chat([{"role": "user", "content": "What is PgMex?"}]).data["done"]["query_id"]
    for rating in (1, -1):
        r = requests.post(f"{BASE_URL}/kb/feedback/{qid}", json={"rating": rating})
        assert r.status_code == 200 and r.json() == {"id": qid, "rating": rating}
