"""Negative API scenarios: invalid input, missing objects and missing rights are refused
with the right status code, and nothing is changed."""
import pytest
import requests

from conftest import BASE_URL

pytestmark = [pytest.mark.negative, pytest.mark.usefixtures("server_is_up")]

PAYLOAD = {"source": "Neg Source", "heading": "Neg Heading", "body": "Neg Body",
           "source_vec": [0.1] * 10, "heading_vec": [0.2] * 10, "body_vec": [0.3] * 10}


@pytest.fixture
def needs_admin(admin_required):
    if not admin_required:
        pytest.skip("server runs without KB_ADMIN_TOKEN: write endpoints are open")


@pytest.mark.parametrize("method, path", [
    ("post", "/kb/"),
    ("put", "/kb/1"),
    ("delete", "/kb/1"),
    ("delete", "/kb/documents/aboutus"),
    ("post", "/kb/documents/aboutus/reembed"),
])
@pytest.mark.parametrize("token", [None, "wrong-token"], ids=["no-token", "wrong-token"])
def test_write_endpoints_require_admin_token(needs_admin, method, path, token):
    headers = {"X-Admin-Token": token} if token else {}
    r = getattr(requests, method)(f"{BASE_URL}{path}", json=PAYLOAD, headers=headers)
    assert r.status_code == 401


def test_upload_requires_admin_token(needs_admin):
    r = requests.post(f"{BASE_URL}/kb/upload", files={"file": ("x.md", b"# x\nhello")})
    assert r.status_code == 401
    docs = [d["source"] for d in requests.get(f"{BASE_URL}/kb/documents").json()]
    assert "x" not in docs


def test_documents_survive_unauthorized_delete(needs_admin):
    before = requests.get(f"{BASE_URL}/kb/stats").json()["chunks"]
    requests.delete(f"{BASE_URL}/kb/documents/aboutus")
    assert requests.get(f"{BASE_URL}/kb/stats").json()["chunks"] == before


@pytest.mark.parametrize("field", ["source", "heading", "body"])
def test_reject_blank_fields(admin_headers, field):
    r = requests.post(f"{BASE_URL}/kb/", json={**PAYLOAD, field: "   "}, headers=admin_headers)
    assert r.status_code == 400


def test_reject_missing_fields(admin_headers):
    r = requests.post(f"{BASE_URL}/kb/", json={"source": "only source"}, headers=admin_headers)
    assert r.status_code == 422


def test_reject_wrong_types(admin_headers):
    r = requests.post(f"{BASE_URL}/kb/", json={**PAYLOAD, "body_vec": "not a list"}, headers=admin_headers)
    assert r.status_code == 422


def test_missing_chunk_is_404(admin_headers):
    assert requests.get(f"{BASE_URL}/kb/999999999").status_code == 404
    assert requests.delete(f"{BASE_URL}/kb/999999999", headers=admin_headers).status_code == 404
    assert requests.put(f"{BASE_URL}/kb/999999999", json=PAYLOAD, headers=admin_headers).status_code == 404


def test_non_numeric_chunk_id_is_422():
    assert requests.get(f"{BASE_URL}/kb/abc").status_code == 422


def test_missing_document_is_404(admin_headers):
    assert requests.get(f"{BASE_URL}/kb/documents/no-such-doc").status_code == 404
    assert requests.post(f"{BASE_URL}/kb/documents/no-such-doc/reembed",
                         headers=admin_headers).status_code == 404


@pytest.mark.parametrize("name, content", [
    ("virus.exe", b"MZ\x90\x00"),
    ("script.js", b"alert(1)"),
    ("image.png", b"\x89PNG\r\n"),
])
def test_upload_rejects_unsupported_types(admin_headers, name, content):
    r = requests.post(f"{BASE_URL}/kb/upload", files={"file": (name, content)}, headers=admin_headers)
    assert r.status_code == 400


def test_upload_rejects_duplicate_document(admin_headers):
    md = b"# Duplicate check\nThis document exists only during the duplicate upload test.\n"
    first = requests.post(f"{BASE_URL}/kb/upload", files={"file": ("pytest-dup.md", md)}, headers=admin_headers)
    assert first.status_code == 201
    try:
        dup = requests.post(f"{BASE_URL}/kb/upload", files={"file": ("pytest-dup.md", md)}, headers=admin_headers)
        assert dup.status_code == 409
    finally:
        requests.delete(f"{BASE_URL}/kb/documents/pytest-dup", headers=admin_headers)


def test_upload_rejects_empty_file(admin_headers):
    r = requests.post(f"{BASE_URL}/kb/upload", files={"file": ("empty.md", b"")}, headers=admin_headers)
    assert r.status_code == 400


@pytest.mark.parametrize("rating", [0, 2, -5])
def test_feedback_rejects_invalid_rating(rating):
    assert requests.post(f"{BASE_URL}/kb/feedback/1", json={"rating": rating}).status_code == 400


def test_feedback_for_unknown_query_is_404():
    assert requests.post(f"{BASE_URL}/kb/feedback/999999999", json={"rating": 1}).status_code == 404


@pytest.mark.parametrize("params", [{}, {"query": "qa", "limit": 0}, {"query": "qa", "limit": 999}])
def test_retrieve_validates_parameters(params):
    assert requests.get(f"{BASE_URL}/kb/retrieve", params=params).status_code == 422


def test_chat_stream_rejects_malformed_json():
    r = requests.post(f"{BASE_URL}/kb/chat/stream", data="{not json",
                      headers={"Content-Type": "application/json"})
    assert r.status_code == 422


def test_unknown_endpoint_is_404():
    assert requests.get(f"{BASE_URL}/kb/no/such/endpoint/here").status_code == 404
