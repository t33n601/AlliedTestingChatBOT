import os

import psycopg2
import ollama

DB_DSN = os.getenv("KB_DB_DSN", "host=localhost port=5435 dbname=knowledge_db user=kbuser password=kbpass")
EMBED_MODEL = os.getenv("KB_EMBED_MODEL", "nomic-embed-text")


def make_vector(text, model=EMBED_MODEL):
    response = ollama.embeddings(
        model=model,
        prompt=text,
    )
    return response["embedding"]


def fill_vectors():
    conn = cur = None
    try:
        conn = psycopg2.connect(DB_DSN)
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute("""
            SELECT id, source, heading, body FROM kb_chunks
            WHERE source_vec IS NULL OR heading_vec IS NULL OR body_vec IS NULL
        """)
        rows = cur.fetchall()
        print(f"Vectorizing {len(rows)} rows")

        for row_id, source, heading, body in rows:
            source_vec = make_vector(source)
            heading_vec = make_vector(heading)
            body_vec = make_vector(body)
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
    fill_vectors()
