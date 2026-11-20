"""Quality page API: run the QA dataset against this server and keep the history of runs.

    GET  /kb/eval/dataset            the cases (id, type, category, prompt)
    GET  /kb/eval/runs               recent runs with their headline metrics
    GET  /kb/eval/runs/{id}          one run with every case and every check
    POST /kb/eval/runs               start a run (admin): {"ids": [...]} or {"category": "..."} or all cases
    POST /kb/eval/runs/{id}/cancel   stop a running run (admin)
    GET  /kb/eval/feedback           thumbs up / down from real users

A run is a background thread that sends each case to this server's own /kb/chat/stream over HTTP,
so the checks see exactly what a user would. The checks live in qa_eval.py and are the same ones
the pytest suite asserts.
"""
import json
import threading
import time

import psycopg2.extras
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request

import qa_eval

CLIENT = "qa-eval"                 # X-KB-Client of the run's requests: tag client:qa-eval in Langfuse
KEEP_RUNS = 50


def build_router(get_conn, close_all, require_admin, admin_token, chat_model):
    router = APIRouter(prefix="/kb/eval", tags=["quality"])
    lock = threading.Lock()
    state = {"run_id": None, "cancel": False}

    def db(sql, args=(), fetch="none"):
        conn = cur = None
        try:
            conn = get_conn()
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql, args)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
        finally:
            close_all(cur, conn)

    def init():
        db("""CREATE TABLE IF NOT EXISTS kb_eval_runs (
                id          SERIAL PRIMARY KEY,
                started_at  TIMESTAMPTZ DEFAULT now(),
                finished_at TIMESTAMPTZ,
                status      TEXT,
                scope       TEXT,
                model       TEXT,
                total       INTEGER,
                results     JSONB DEFAULT '[]',
                summary     JSONB
              )""")
        db("ALTER TABLE kb_eval_runs ADD COLUMN IF NOT EXISTS case_ids JSONB")
        db("ALTER TABLE kb_eval_runs ADD COLUMN IF NOT EXISTS started_by TEXT")
        # a run that was going when the server stopped will never finish
        db("UPDATE kb_eval_runs SET status = 'interrupted', finished_at = now() WHERE status = 'running'")

    def iso(row):
        for k in ("started_at", "finished_at"):
            if row.get(k):
                row[k] = row[k].isoformat()
        return row

    def worker(run_id, cases, base_url):
        results = []
        status = "done"
        try:
            for case in cases:
                if state["cancel"]:
                    status = "cancelled"
                    break
                results.append(qa_eval.run_case(case, base_url, headers={"X-KB-Client": CLIENT},
                                                admin_token=admin_token))
                db("UPDATE kb_eval_runs SET results = %s, summary = %s WHERE id = %s",
                   (json.dumps(results, ensure_ascii=False),
                    json.dumps(qa_eval.summarize(results)), run_id))
        except Exception as e:
            status = "failed"
            print(f"[eval] run {run_id} failed: {e}")
        finally:
            db("UPDATE kb_eval_runs SET status = %s, finished_at = now(), summary = %s WHERE id = %s",
               (status, json.dumps(qa_eval.summarize(results)), run_id))
            db("""DELETE FROM kb_eval_runs WHERE id NOT IN
                    (SELECT id FROM kb_eval_runs ORDER BY id DESC LIMIT %s)""", (KEEP_RUNS,))
            with lock:
                state["run_id"] = None

    @router.get("/dataset")
    def dataset():
        return {"cases": [{"id": c["id"], "type": c["type"], "category": c["category"],
                           "lang": c.get("lang", "en"), "intent": c["expected_intent"],
                           "prompt": qa_eval.display_prompt(c)} for c in qa_eval.load_dataset()]}

    @router.get("/runs")
    def runs(limit: int = 20, all: bool = False):
        """Runs started by the pytest suite (X-KB-Client: qa-…) are left out unless all=true."""
        rows = db("""SELECT id, started_at, finished_at, status, scope, model, total, summary
                     FROM kb_eval_runs WHERE %s OR COALESCE(started_by, 'web') NOT LIKE 'qa-%%'
                     ORDER BY id DESC LIMIT %s""", (all, max(1, min(limit, KEEP_RUNS))), fetch="all")
        return {"runs": [iso(dict(r)) for r in rows], "running": state["run_id"]}

    @router.get("/runs/{run_id}")
    def run(run_id: int):
        row = db("SELECT * FROM kb_eval_runs WHERE id = %s", (run_id,), fetch="one")
        if row is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return iso(dict(row))

    @router.post("/runs", status_code=202, dependencies=[Depends(require_admin)])
    def start(request: Request, ids: list = Body(default=None), category: str = Body(default=None),
              x_kb_client: Optional[str] = Header(default=None)):
        cases = qa_eval.load_dataset()
        scope = "all"
        if ids:
            cases = [c for c in cases if c["id"] in set(ids)]
            scope = cases[0]["id"] if len(cases) == 1 else f"{len(cases)} cases"
        elif category:
            cases = [c for c in cases if c["category"] == category]
            scope = category
        if not cases:
            raise HTTPException(status_code=400, detail="No cases match")
        with lock:
            if state["run_id"]:
                raise HTTPException(status_code=409, detail=f"Run {state['run_id']} is still going")
            row = db("""INSERT INTO kb_eval_runs (status, scope, model, total, summary, case_ids, started_by)
                        VALUES ('running', %s, %s, %s, %s, %s, %s) RETURNING id""",
                     (scope, chat_model, len(cases), json.dumps(qa_eval.summarize([])),
                      json.dumps([c["id"] for c in cases]), (x_kb_client or "web")[:20]), fetch="one")
            state.update(run_id=row["id"], cancel=False)
        # call this server directly on the address it listens on, not through the public tunnel
        host, port = request.scope.get("server") or ("127.0.0.1", 8091)
        host = "127.0.0.1" if host in ("0.0.0.0", "::", None) else host
        threading.Thread(target=worker, args=(row["id"], cases, f"http://{host}:{port}"),
                         daemon=True).start()
        return {"id": row["id"], "total": len(cases)}

    @router.post("/runs/{run_id}/cancel", dependencies=[Depends(require_admin)])
    def cancel(run_id: int):
        if state["run_id"] != run_id:
            raise HTTPException(status_code=409, detail="This run is not going")
        state["cancel"] = True
        return {"id": run_id, "cancelling": True}

    @router.get("/feedback")
    def feedback():
        """Ratings of real users' answers (test traffic is left out)."""
        real = "client IS NOT NULL AND client NOT LIKE %s"   # rows before the client column are mixed with tests
        row = db(f"""SELECT count(*) AS questions,
                            count(*) FILTER (WHERE rating = 1)  AS up,
                            count(*) FILTER (WHERE rating = -1) AS down,
                            avg(total_ms)::int AS avg_ms
                     FROM kb_queries WHERE {real}""", ("qa-%",), fetch="one")
        down = db(f"""SELECT question, asked_at FROM kb_queries
                      WHERE rating = -1 AND {real} ORDER BY id DESC LIMIT 5""", ("qa-%",), fetch="all")
        return {**dict(row), "recent_down": [iso_one(d) for d in down]}

    def iso_one(d):
        return {"question": (d["question"] or "")[:200], "asked_at": d["asked_at"].isoformat()}

    router.init = init
    return router
