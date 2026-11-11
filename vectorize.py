import os

import psycopg2
import ollama

DB_DSN = os.getenv("KB_DB_DSN", "host=localhost port=5435 dbname=knowledge_db user=kbuser password=kbpass")
EMBED_MODEL = os.getenv("KB_EMBED_MODEL", "nomic-embed-text")


def make_vector(text, model=EMBED_MODEL):
    """Embed text. Text longer than the embedding model's context window is cut in half
    until it fits, instead of failing with "input length exceeds the context length"."""
    while True:
        try:
            response = ollama.embeddings(model=model, prompt=text)
            return response["embedding"]
        except ollama.ResponseError as e:
            if "context length" not in str(e) or len(text) < 200:
                raise
            text = text[: len(text) // 2]


# nomic-embed-text is trained with task prefixes: documents and questions are embedded
# differently, which ranks the right chunk first far more often.
PREFIXED = "nomic" in EMBED_MODEL
DOC_PREFIX = "search_document: " if PREFIXED else ""
QUERY_PREFIX = "search_query: " if PREFIXED else ""


def embed_chunk(heading, body):
    """Vector used for search (body_vec). The heading is included because it often holds the
    very words people ask about ("Managing Director", "Alliedium", "Learning Center")."""
    return make_vector(f"{DOC_PREFIX}{heading}\n\n{body}")


def embed_query(text):
    return make_vector(f"{QUERY_PREFIX}{text}")


def fill_vectors(all_rows=False):
    conn = cur = None
    try:
        conn = psycopg2.connect(DB_DSN)
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute("""
            SELECT id, source, heading, body FROM kb_chunks
            WHERE %s OR source_vec IS NULL OR heading_vec IS NULL OR body_vec IS NULL
        """, (all_rows,))
        rows = cur.fetchall()
        print(f"Vectorizing {len(rows)} rows")

        for row_id, source, heading, body in rows:
            source_vec = make_vector(source)
            heading_vec = make_vector(heading)
            body_vec = embed_chunk(heading, body)
            cur.execute("""
                UPDATE kb_chunks
                SET source_vec = %s, heading_vec = %s, body_vec = %s
                WHERE id = %s
            """, (source_vec, heading_vec, body_vec, row_id))
            print(f"Vectorized #{row_id}")

    except Exception as e:
        print("Error:", e)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# MAIN
if __name__ == "__main__":
    import sys
    fill_vectors(all_rows="--all" in sys.argv)   # --all: re-embed every chunk, e.g. after changing the scheme
