import os
import psycopg2

from vectorize import DB_DSN


def save_chunk(source, heading, body):
    conn = cur = None
    try:
        conn = psycopg2.connect(DB_DSN)
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO kb_chunks (source, heading, body)
            VALUES (%s, %s, %s)
        """, (source, heading, body))

        print("Chunk saved!")

    except Exception as e:
        print("Error:", e)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def import_folder(folder="knowledge"):
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".md"):
            continue
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            text = f.read()

        source = name[:-3]                  # aboutus.md -> aboutus
        for part in text.split("\n#"):      # each heading starts a section
            lines = part.strip().lstrip("#").strip().split("\n", 1)
            if len(lines) < 2:              # heading without text: skip
                continue
            heading, body = lines
            save_chunk(source, heading.strip(), body.strip())


# MAIN
if __name__ == "__main__":
    # careful: running this twice inserts every chunk twice
    import_folder()
