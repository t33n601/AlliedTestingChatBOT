-- Steps 9-10: enable pgvector and create the table
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS kb_chunks (
  id          SERIAL PRIMARY KEY,
  source      TEXT,
  heading     TEXT,
  body        TEXT,
  source_vec  vector(768),
  heading_vec vector(768),
  body_vec    vector(768)
);
