import json
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

import numpy as np
import psycopg2
import requests
from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from chunking import file_to_chunks
from vectorize import DB_DSN, EMBED_MODEL, make_vector

CHAT_MODEL = os.getenv("KB_CHAT_MODEL", "qwen3:1.7b")
OLLAMA_URL = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
ADMIN_TOKEN = os.getenv("KB_ADMIN_TOKEN", "")      # empty = write endpoints are open (local dev)
MAX_DISTANCE = 0.5
TOP_K = 5
MIN_SCORE = float(os.getenv("KB_MIN_SCORE", "0.55"))   # best match below this = "not in the knowledge base"
COLUMNS = "id, source, heading, body, source_vec, heading_vec, body_vec"
WEB_DIR = Path(__file__).parent / "web"
UI_DIR = Path(__file__).parent / "ui"

SYSTEM_RULES = (
    "You are a helpful and formal assistant. "
    "Use the following context to answer the user's question. "
    "Be concise and professional.\n\n"
    "Use only the following context as factual information:\n{context}"
)

# The streaming chat numbers each chunk so the model can cite it as [1], [2] ...
CITED_RULES = (
    "You are KB Helper, the knowledge assistant of Allied Testing.\n"
    "Answer ONLY from the numbered context below. After each fact add the number of the "
    "context block it came from in square brackets, for example [1] or [2][3].\n"
    "If the context does not contain the answer, say honestly that the knowledge base "
    "has no information about it. Never invent facts.\n"
    "{tone} "
    "Always answer in the same language as the user's last message.\n\n"
    "Context:\n{context}"
)


def get_conn():
    return psycopg2.connect(DB_DSN)


def close_all(cur, conn):
    if cur:
        cur.close()
    if conn:
        conn.close()


def init_db():
    """Idempotent: HNSW index for fast cosine search + a log table for analytics."""
    conn = cur = None
    try:
        conn = get_conn()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""CREATE INDEX IF NOT EXISTS kb_chunks_body_hnsw
                       ON kb_chunks USING hnsw (body_vec vector_cosine_ops)""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kb_queries (
              id          SERIAL PRIMARY KEY,
              asked_at    TIMESTAMPTZ DEFAULT now(),
              question    TEXT,
              answer      TEXT,
              sources     JSONB,
              top_score   REAL,
              retrieve_ms INTEGER,
              first_ms    INTEGER,
              total_ms    INTEGER,
              tokens      INTEGER,
              rating      SMALLINT
            )""")
    except Exception as e:
        print(f"[init_db] skipped: {e}")
    finally:
        close_all(cur, conn)


@asynccontextmanager
async def lifespan(_app):
    init_db()
    threading.Thread(target=lambda: _safe(kmap.get), daemon=True).start()   # warm the map
    yield


def _safe(fn):
    try:
        fn()
    except Exception as e:
        print(f"[warmup] {e}")


app = FastAPI(title="KB RAG server", lifespan=lifespan)


@app.middleware("http")
async def no_stale_ui(request, call_next):
    """The browser revalidates the UI files on every load, so a refresh always shows the latest version."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def require_admin(x_admin_token: Optional[str] = Header(default=None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Admin token required")


class ChunkInfo(BaseModel):
    source: str
    heading: str
    body: str
    source_vec: List[float] = []
    heading_vec: List[float] = []
    body_vec: List[float] = []


class ChatMessage(BaseModel):
    role: str
    content: str


TONES = {   # course exercise: "change the assistant's tone in the system prompt"
    "formal": "Be concise and professional, use short paragraphs or bullet lists.",
    "friendly": "Be warm, upbeat and conversational, like a helpful colleague. Short paragraphs, an emoji is fine.",
    "brief": "Be ultra-brief: at most three short sentences or three bullets.",
    "simple": "Explain it so a 12-year-old would understand: plain words, one everyday analogy.",
}


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    think: bool = True
    top_k: int = TOP_K
    tone: str = "formal"
    model: Optional[str] = None


def row_to_dict(row):
    return {
        "id": row[0], "source": row[1], "heading": row[2], "body": row[3],
        "source_vec": row[4], "heading_vec": row[5], "body_vec": row[6],
    }


def check_not_empty(info: ChunkInfo):
    if not info.source.strip() or not info.heading.strip() or not info.body.strip():
        raise HTTPException(status_code=400, detail="Fields must not be empty")


def retrieve(cur, vec, limit=TOP_K, max_distance=MAX_DISTANCE):
    """Retrieve: chunks closest in meaning to the query vector (cosine distance <=>)."""
    cur.execute("""
        SELECT id, source, heading, body, body_vec <=> %s::vector AS distance
        FROM kb_chunks
        WHERE body_vec IS NOT NULL AND body_vec <=> %s::vector < %s
        ORDER BY body_vec <=> %s::vector
        LIMIT %s
    """, (vec, vec, max_distance, vec, limit))
    return [{"id": r[0], "source": r[1], "heading": r[2], "body": r[3],
             "score": round(1 - float(r[4]), 4)} for r in cur.fetchall()]


def nearest_bodies(cur, text, limit=TOP_K):
    return [c["body"] for c in retrieve(cur, make_vector(text), limit)]


def ask_model(chunks, chat_messages):
    """Augment + generate (non-streaming): chunks go into the system prompt."""
    context = "\n".join(f"Chunk: {c}" for c in chunks)
    r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=600, json={
        "model": CHAT_MODEL, "stream": False, "think": False,
        "messages": [{"role": "system", "content": SYSTEM_RULES.format(context=context)}]
        + chat_messages,
    })
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


# ---------------------------------------------------------------- knowledge map

def tsne(X, perplexity=15.0, iters=900, seed=7):
    """Exact t-SNE (van der Maaten & Hinton, 2008) in plain numpy. Fine for a few hundred
    points; unlike PCA it keeps neighbourhoods, so topics show up as visible clusters."""
    n = X.shape[0]
    rng = np.random.default_rng(seed)
    D = np.maximum(2 - 2 * (X @ X.T), 0)                # squared distance of unit vectors
    P = np.zeros((n, n))
    target = np.log(min(perplexity, (n - 1) / 3))
    for i in range(n):                                  # binary search sigma for each point
        d = np.delete(D[i], i)
        lo, hi, beta = 0.0, np.inf, 1.0
        for _ in range(60):
            p = np.exp(-(d - d.min()) * beta)
            sp = p.sum()
            h = np.log(sp) + beta * (d - d.min()) @ p / sp
            if abs(h - target) < 1e-4:
                break
            if h > target:
                lo, beta = beta, beta * 2 if hi == np.inf else (beta + hi) / 2
            else:
                hi, beta = beta, (beta + lo) / 2
        P[i, np.arange(n) != i] = p / sp
    P = np.maximum((P + P.T) / (2 * n), 1e-12)
    Y = rng.normal(0, 1e-4, (n, 2))
    vel, gains = np.zeros_like(Y), np.ones_like(Y)
    for it in range(iters):
        exag = 12.0 if it < 250 else 1.0
        sq = (Y ** 2).sum(1)
        num = 1 / (1 + sq[:, None] + sq[None, :] - 2 * Y @ Y.T)
        np.fill_diagonal(num, 0)
        Q = np.maximum(num / num.sum(), 1e-12)
        W = (exag * P - Q) * num
        grad = 4 * ((np.diag(W.sum(1)) - W) @ Y)
        gains = np.where(np.sign(grad) != np.sign(vel), gains + 0.2, gains * 0.8).clip(0.01)
        vel = (0.5 if it < 250 else 0.8) * vel - max(n / 12, 10) * gains * grad
        Y += vel
        Y -= Y.mean(0)
    return Y


class KnowledgeMap:
    """2D layout of every chunk's body vector (t-SNE in cosine space). A question is
    placed among the chunks most similar to it, so the UI can draw it next to the
    passages it retrieved. Rebuilt lazily whenever the table changes."""

    def __init__(self):
        self.lock = threading.Lock()
        self.version = 1          # bumped on every write; the map is stale while built != version
        self.built = 0
        self.points = []
        self.X = self.P = None

    def invalidate(self):
        self.version += 1

    def _build(self):
        conn = cur = None
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("SELECT id, source, heading, body_vec::text FROM kb_chunks "
                        "WHERE body_vec IS NOT NULL ORDER BY id")
            rows = cur.fetchall()
        finally:
            close_all(cur, conn)
        if len(rows) < 5:
            self.points, self.X = [], None
            return
        X = np.array([json.loads(r[3]) for r in rows], dtype=np.float64)
        X /= np.linalg.norm(X, axis=1, keepdims=True)
        Y = tsne(X)
        lo, span = Y.min(axis=0), np.ptp(Y, axis=0) + 1e-9
        self.X, self.P = X, (Y - lo) / span
        self.points = [{"id": r[0], "source": r[1], "heading": r[2],
                        "x": round(float(p[0]), 4), "y": round(float(p[1]), 4)}
                       for r, p in zip(rows, self.P)]

    def get(self):
        with self.lock:
            if self.built != self.version:
                t0 = time.perf_counter()
                target = self.version
                self._build()
                self.built = target
                print(f"[map] {len(self.points)} points in {time.perf_counter() - t0:.2f}s")
            return self.points

    def project(self, vec, k=6):
        """Similarity-weighted centre of the k nearest chunks."""
        self.get()
        if self.X is None:
            return None
        v = np.asarray(vec, dtype=np.float64)
        v /= np.linalg.norm(v)
        sims = self.X @ v
        top = np.argsort(-sims)[:k]
        w = np.exp((sims[top] - sims[top].max()) * 25)
        p = (self.P[top] * w[:, None]).sum(0) / w.sum()
        return {"x": round(float(p[0]), 4), "y": round(float(p[1]), 4)}


kmap = KnowledgeMap()


# ---------------------------------------------------------------- web UI

@app.get("/", include_in_schema=False)
def index():
    """index.html with ?v=<mtime> on style.css/app.js: Cloudflare caches /static/ for hours,
    so a new URL after every edit makes phones load the fresh files instead of a stale copy."""
    from fastapi.responses import HTMLResponse
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("style.css", "app.js"):
        v = int((WEB_DIR / name).stat().st_mtime)
        html = html.replace(f'/static/{name}"', f'/static/{name}?v={v}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
app.mount("/brand", StaticFiles(directory=UI_DIR), name="brand")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(UI_DIR / "favicon-64.png", media_type="image/png")


@app.get("/motif.svg", include_in_schema=False)
def motif():
    from ui.motif import motif_svg
    from fastapi.responses import Response
    return Response(motif_svg(), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600"})


# --- stats, search & answer go BEFORE /kb/{chunk_id}, otherwise "search" is parsed as an id

@app.get("/kb/stats")
def kb_stats():
    conn = cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT count(*), count(body_vec), count(DISTINCT source) FROM kb_chunks")
        chunks, vectorized, documents = cur.fetchone()
        questions = 0
        try:
            cur.execute("SELECT count(*) FROM kb_queries")
            questions = cur.fetchone()[0]
        except Exception:
            conn.rollback()
        return {"chunks": chunks, "vectorized": vectorized, "documents": documents,
                "questions": questions, "chat_model": CHAT_MODEL, "embed_model": EMBED_MODEL,
                "dimensions": 768, "admin_required": bool(ADMIN_TOKEN)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        close_all(cur, conn)


@app.get("/kb/search")
def search_chunks(query: str = Query(...), limit: int = 5):
    if not query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")
    conn = cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        return nearest_bodies(cur, query, limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        close_all(cur, conn)


@app.get("/kb/retrieve")
def retrieve_chunks(query: str = Query(...), limit: int = Query(TOP_K, ge=1, le=20)):
    """Semantic search with scores and the query's position on the knowledge map."""
    if not query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")
    conn = cur = None
    try:
        t0 = time.perf_counter()
        vec = make_vector(query)
        conn = get_conn()
        cur = conn.cursor()
        sources = retrieve(cur, vec, limit, max_distance=2)
        return {"query": query, "sources": sources, "point": kmap.project(vec),
                "ms": int((time.perf_counter() - t0) * 1000)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        close_all(cur, conn)


def chat_models():
    """Chat-capable models installed in Ollama (embedding models are filtered out)."""
    r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
    r.raise_for_status()
    return [m["name"] for m in r.json().get("models", [])
            if "bert" not in (m.get("details", {}).get("family") or "") and "embed" not in m["name"]]


@app.get("/kb/models")
def list_models():
    try:
        return {"default": CHAT_MODEL, "models": chat_models(), "tones": list(TONES)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/kb/map")
def knowledge_map():
    try:
        return {"points": kmap.get()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/kb/answer")
def answer_question(question: str = Query(..., description="User's question")):
    conn = cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        chunks = nearest_bodies(cur, question)
        if not chunks:
            raise HTTPException(status_code=404, detail="No similar context found")
        answer = ask_model(chunks, [{"role": "user", "content": question}])
        return {"question": question, "answer": answer}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        close_all(cur, conn)


@app.post("/kb/answer")
def answer_with_history(messages: List[str] = Body(...)):
    if not messages or not messages[-1].strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")
    conn = cur = None
    try:
        history = messages[:-1]
        question = messages[-1]
        conn = get_conn()
        cur = conn.cursor()
        chunks = nearest_bodies(cur, question)
        if not chunks:
            raise HTTPException(status_code=404, detail="No similar context found")

        # even index = user, odd index = assistant
        chat = [{"role": "user" if i % 2 == 0 else "assistant", "content": msg}
                for i, msg in enumerate(history)]
        chat.append({"role": "user", "content": question})

        return {"question": question, "answer": ask_model(chunks, chat)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        close_all(cur, conn)


# ---------------------------------------------------------------- streaming chat

def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def log_query(question, answer, sources, timings, tokens):
    conn = cur = None
    try:
        conn = get_conn()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO kb_queries (question, answer, sources, top_score,
                                    retrieve_ms, first_ms, total_ms, tokens)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (question, answer,
              json.dumps([{"id": s["id"], "source": s["source"], "score": s["score"]}
                          for s in sources]),
              sources[0]["score"] if sources else None,
              timings.get("retrieve_ms"), timings.get("first_token_ms"),
              timings.get("total_ms"), tokens))
        return cur.fetchone()[0]
    except Exception as e:
        print(f"[log_query] {e}")
        return None
    finally:
        close_all(cur, conn)


def is_cyrillic(text):
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum("\u0400" <= c <= "\u04ff" for c in letters) / len(letters) > 0.3


def needs_translation(text):
    """The embedding model is English-centric: search non-English questions in English."""
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isascii() for c in letters) / len(letters) < 0.7


def rewrite_query(msgs):
    """Turn the last message (+ recent history) into a standalone English search query,
    so follow-ups like "and how much does it cost?" and Russian questions retrieve well."""
    history = "\n".join(f"{m.role}: {m.content[:400]}" for m in msgs[-5:-1])
    prompt = ("Rewrite the user's LAST message as one short standalone search query in English. "
              "Resolve pronouns using the conversation. Output only the query, nothing else.\n\n"
              f"Conversation:\n{history or '(none)'}\n\nLast message: {msgs[-1].content}")
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=60, json={
            "model": CHAT_MODEL, "stream": False, "think": False,
            "options": {"temperature": 0}, "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        q = r.json()["message"]["content"].strip().strip('"').splitlines()[0].strip()
        return q[:300] or msgs[-1].content
    except Exception as e:
        print(f"[rewrite_query] {e}")
        return msgs[-1].content


@app.post("/kb/chat/stream")
def chat_stream(req: ChatRequest):
    """Server-Sent Events: sources -> think* -> token* -> done (or error).
    Off-topic questions get sources -> no_answer -> done, without calling the model."""
    msgs = [m for m in req.messages if m.content.strip()]
    if not msgs or msgs[-1].role != "user":
        raise HTTPException(status_code=400, detail="Last message must be a non-empty user message")
    question = msgs[-1].content.strip()
    top_k = max(1, min(req.top_k, 10))
    model = req.model or CHAT_MODEL
    if model != CHAT_MODEL and model not in chat_models():
        raise HTTPException(status_code=400, detail=f"Model '{model}' is not installed in Ollama")

    def events():
        t0 = time.perf_counter()
        ms = lambda: int((time.perf_counter() - t0) * 1000)
        timings, answer, tokens = {}, [], 0
        sources = []
        try:
            query = question
            if len(msgs) > 1 or needs_translation(question):
                query = rewrite_query(msgs)
                timings["rewrite_ms"] = ms()
                if query != question:
                    yield sse("rewrite", {"query": query, "ms": timings["rewrite_ms"]})
            vec = make_vector(query)
            conn = get_conn()
            cur = conn.cursor()
            try:
                sources = retrieve(cur, vec, top_k)
                nearest = sources[:1] or retrieve(cur, vec, 1, max_distance=2)
            finally:
                close_all(cur, conn)
            timings["retrieve_ms"] = ms()
            best = nearest[0]["score"] if nearest else 0.0

            if best < MIN_SCORE:
                # Nothing in the knowledge base is close enough: don't let the model guess.
                yield sse("sources", {"sources": [], "point": kmap.project(vec),
                                      "retrieve_ms": timings["retrieve_ms"]})
                timings["total_ms"] = ms()
                qid = log_query(question, "", [], timings, 0)
                yield sse("no_answer", {"best_score": round(best, 4), "min_score": MIN_SCORE})
                yield sse("done", {"timings": timings, "tokens": 0, "query_id": qid, "model": model})
                return

            yield sse("sources", {"sources": sources, "point": kmap.project(vec),
                                  "retrieve_ms": timings["retrieve_ms"]})

            context = "\n\n".join(f"[{i}] ({s['source']} / {s['heading']})\n{s['body']}"
                                  for i, s in enumerate(sources, 1)) or "(no relevant context found)"
            tone = TONES.get(req.tone, TONES["formal"])
            chat = [{"role": "system", "content": CITED_RULES.format(context=context, tone=tone)}]
            chat += [{"role": m.role if m.role in ("user", "assistant") else "user",
                      "content": m.content} for m in msgs[-8:]]
            if sources:   # small models follow a reminder next to the question far better
                chat[-1]["content"] += ("\n\n(Answer from the context and cite blocks like "
                                        "[1], [2] after each fact.)")
            if is_cyrillic(question):
                chat[-1]["content"] += "\n(Отвечай на русском языке.)"

            with requests.post(f"{OLLAMA_URL}/api/chat", stream=True, timeout=600, json={
                "model": model, "messages": chat, "stream": True, "think": req.think,
                "options": {"temperature": 0.3},
            }) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    d = json.loads(line)
                    m = d.get("message") or {}
                    if m.get("thinking"):
                        timings.setdefault("first_think_ms", ms())
                        yield sse("think", {"t": m["thinking"]})
                    if m.get("content"):
                        timings.setdefault("first_token_ms", ms())
                        answer.append(m["content"])
                        yield sse("token", {"t": m["content"]})
                    if d.get("done"):
                        tokens = d.get("eval_count") or 0
                        dur = (d.get("eval_duration") or 0) / 1e9
                        timings["tokens_per_sec"] = round(tokens / dur, 1) if dur else None
            timings["total_ms"] = ms()
            qid = log_query(question, "".join(answer), sources, timings, tokens)
            yield sse("done", {"timings": timings, "tokens": tokens, "query_id": qid,
                               "model": model})
        except Exception as e:
            yield sse("error", {"detail": str(e)})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/kb/feedback/{query_id}")
def feedback(query_id: int, rating: int = Body(..., embed=True)):
    if rating not in (-1, 1):
        raise HTTPException(status_code=400, detail="rating must be 1 or -1")
    conn = cur = None
    try:
        conn = get_conn()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("UPDATE kb_queries SET rating = %s WHERE id = %s", (rating, query_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Query not found")
        return {"id": query_id, "rating": rating}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        close_all(cur, conn)


# ---------------------------------------------------------------- documents

@app.get("/kb/documents")
def list_documents():
    conn = cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""SELECT source, count(*), min(id), sum(length(body))
                       FROM kb_chunks GROUP BY source ORDER BY min(id)""")
        return [{"source": r[0], "chunks": r[1], "first_id": r[2], "chars": int(r[3] or 0)}
                for r in cur.fetchall()]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        close_all(cur, conn)


@app.get("/kb/documents/{source}")
def document_chunks(source: str):
    conn = cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, heading, body FROM kb_chunks WHERE source = %s ORDER BY id", (source,))
        rows = cur.fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="Document not found")
        return {"source": source,
                "chunks": [{"id": r[0], "heading": r[1], "body": r[2]} for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        close_all(cur, conn)


@app.delete("/kb/documents/{source}", dependencies=[Depends(require_admin)])
def delete_document(source: str):
    conn = cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM kb_chunks WHERE source = %s", (source,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Document not found")
        conn.commit()
        kmap.invalidate()
        return {"message": "Deleted", "source": source, "chunks": cur.rowcount}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        close_all(cur, conn)


@app.post("/kb/documents/{source}/reembed", dependencies=[Depends(require_admin)])
def reembed_document(source: str):
    """Recompute all three vectors for every chunk of one document (e.g. after editing text
    directly in the DB or switching the embedding model)."""
    conn = cur = None
    try:
        t0 = time.perf_counter()
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, heading, body FROM kb_chunks WHERE source = %s ORDER BY id", (source,))
        rows = cur.fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="Document not found")
        source_vec = make_vector(source)
        for chunk_id, heading, body in rows:
            cur.execute("""UPDATE kb_chunks SET source_vec = %s, heading_vec = %s, body_vec = %s
                           WHERE id = %s""", (source_vec, make_vector(heading), make_vector(body), chunk_id))
        conn.commit()
        kmap.invalidate()
        return {"message": "Re-embedded", "source": source, "chunks": len(rows),
                "ms": int((time.perf_counter() - t0) * 1000)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        close_all(cur, conn)


@app.post("/kb/upload", status_code=201, dependencies=[Depends(require_admin)])
async def upload_document(file: UploadFile = File(...), source: Optional[str] = Form(None)):
    data = await file.read()
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File is larger than 15 MB")
    try:
        chunks = file_to_chunks(file.filename or "upload.txt", data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read file: {e}")
    if not chunks:
        raise HTTPException(status_code=400, detail="No text found in the file")

    name = (source or Path(file.filename or "upload").stem).strip()[:80] or "upload"
    conn = cur = None
    try:
        t0 = time.perf_counter()
        source_vec = make_vector(name)
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM kb_chunks WHERE source = %s", (name,))
        if cur.fetchone()[0]:
            raise HTTPException(status_code=409, detail=f"Document '{name}' already exists")
        ids = []
        for heading, body in chunks:
            cur.execute("""
                INSERT INTO kb_chunks (source, heading, body, source_vec, heading_vec, body_vec)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """, (name, heading[:300], body, source_vec, make_vector(heading), make_vector(body)))
            ids.append(cur.fetchone()[0])
        conn.commit()
        kmap.invalidate()
        return {"message": "Uploaded", "source": name, "chunks": len(ids), "ids": ids,
                "ms": int((time.perf_counter() - t0) * 1000)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        close_all(cur, conn)


# ---------------------------------------------------------------- CRUD

@app.get("/kb/")
def list_chunks():
    conn = cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(f"SELECT {COLUMNS} FROM kb_chunks ORDER BY id")
        return [row_to_dict(row) for row in cur.fetchall()]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        close_all(cur, conn)


@app.get("/kb/{chunk_id}")
def get_chunk(chunk_id: int):
    conn = cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(f"SELECT {COLUMNS} FROM kb_chunks WHERE id = %s", (chunk_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Record not found")
        return row_to_dict(row)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        close_all(cur, conn)


@app.post("/kb/", status_code=201, dependencies=[Depends(require_admin)])
def create_chunk(info: ChunkInfo):
    check_not_empty(info)
    conn = cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO kb_chunks (source, heading, body, source_vec, heading_vec, body_vec)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            info.source.strip(), info.heading.strip(), info.body.strip(),
            make_vector(info.source), make_vector(info.heading), make_vector(info.body),
        ))
        new_id = cur.fetchone()[0]
        conn.commit()
        kmap.invalidate()
        return {"message": "Added", "id": new_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        close_all(cur, conn)


@app.put("/kb/{chunk_id}", dependencies=[Depends(require_admin)])
def update_chunk(chunk_id: int, info: ChunkInfo):
    check_not_empty(info)
    conn = cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            UPDATE kb_chunks
            SET source = %s, heading = %s, body = %s,
                source_vec = %s, heading_vec = %s, body_vec = %s
            WHERE id = %s
        """, (
            info.source.strip(), info.heading.strip(), info.body.strip(),
            make_vector(info.source), make_vector(info.heading), make_vector(info.body),
            chunk_id,
        ))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Data not found")
        conn.commit()
        kmap.invalidate()
        return {"message": "Updated", "id": chunk_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        close_all(cur, conn)


@app.delete("/kb/{chunk_id}", dependencies=[Depends(require_admin)])
def delete_chunk(chunk_id: int):
    conn = cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM kb_chunks WHERE id = %s", (chunk_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Data not found")
        conn.commit()
        kmap.invalidate()
        return {"message": "Deleted", "id": chunk_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        close_all(cur, conn)
