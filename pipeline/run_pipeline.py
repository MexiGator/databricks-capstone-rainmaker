"""
Rainmaker -- end-to-end pipeline runner.

Runs every stage in order, verifies each one before moving on, and prints what
it found. Designed for a Databricks notebook: paste one cell, watch it work, and
if it stops you get told which stage and why rather than a stack trace.

    import os; os.environ["LAKEBASE_URL"] = "postgresql://..."
    import run_pipeline; run_pipeline.main()

Stages are individually callable if you need to re-run just one:

    run_pipeline.stage_weather()
"""

from __future__ import annotations

import os as _os
import sys as _sys
import traceback

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "db"), _os.path.join(_ROOT, "pipeline"), _os.path.join(_ROOT, "agent")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

BAR = "=" * 68


def _head(n: int, title: str) -> None:
    print(f"\n{BAR}\nSTAGE {n} — {title}\n{BAR}")


def _ok(msg: str) -> None:
    print(f"  ok    {msg}")


def _warn(msg: str) -> None:
    print(f"  warn  {msg}")


def _fail(msg: str, fix: str) -> None:
    print(f"  FAIL  {msg}\n        fix: {fix}")


# ---------------------------------------------------------------------
def stage_connection() -> bool:
    _head(0, "Connection")
    if not _os.environ.get("LAKEBASE_URL"):
        _fail("LAKEBASE_URL is not set",
              "os.environ['LAKEBASE_URL'] = '<full postgres URL from the Lakebase instance>'")
        return False
    try:
        import lakebase
        with lakebase.cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]
        _ok(f"connected — {version.split(',')[0]}")
        return True
    except Exception as exc:
        _fail(f"could not connect: {exc}",
              "check the URL includes the password, and that the instance is running (not paused)")
        return False


def stage_schema() -> bool:
    _head(1, "Schema")
    try:
        import lakebase
        lakebase.ensure_schema()
        counts = lakebase.table_counts()
        _ok(f"{len(counts)} tables present")
        return True
    except Exception as exc:
        msg = str(exc)
        if "pgvector" in msg or "vector" in msg.lower():
            _fail(msg, "turn on 'Enable Lakebase Search' in the instance settings, then re-run")
        else:
            _fail(msg, "read the Postgres error above — it names the failing statement")
        return False


def stage_seed() -> bool:
    _head(2, "Seed data")
    try:
        import lakebase
        import seed
        seed.run()
        counts = lakebase.table_counts()
        if counts["customers"] < 50:
            _warn(f"only {counts['customers']} customers — expected 61")
        else:
            _ok(f"{counts['customers']} customers, {counts['outreach_templates']} campaigns")
        return counts["customers"] > 0
    except Exception as exc:
        _fail(str(exc), "check that stage 1 completed — seed needs the tables to exist")
        return False


def stage_weather() -> bool:
    _head(3, "Weather poll (live NWS)")
    try:
        import lakebase
        import poll_weather
        n = poll_weather.run()
        if n == 0:
            _warn("NWS returned no matching active alerts")
            print("        This is normal on a calm day, not a bug.")
            print("        For a demo, widen coverage:")
            print("          poll_weather.run(event_types=['Heat Advisory','Flood Watch',")
            print("                                        'Special Weather Statement'])")
            with lakebase.cursor() as cur:
                cur.execute("SELECT count(*) FROM weather_events")
                existing = cur.fetchone()[0]
            if existing:
                _ok(f"{existing} events already stored from an earlier poll — continuing")
                return True
            return False
        _ok(f"{n} active alerts stored")
        return True
    except Exception as exc:
        msg = str(exc)
        if "403" in msg:
            _fail("NWS returned 403", "the User-Agent header is required — check weather_client.USER_AGENT")
        else:
            _fail(msg, "check network egress from the workspace to api.weather.gov")
        return False


def stage_score() -> bool:
    _head(4, "Match & Score (Spark)")
    try:
        import lakebase
        import match_and_score
        n = match_and_score.run()
        if n == 0:
            _warn("no opportunities cleared the cutoff")
            print("        Weather is live but nobody is exposed. Either the alerts are")
            print("        far from your seeded cities, or every score fell below")
            print(f"        scoring.QUEUE_CUTOFF ({__import__('scoring').QUEUE_CUTOFF}).")
            return False
        with lakebase.cursor() as cur:
            cur.execute("SELECT priority, count(*) FROM opportunities GROUP BY 1 ORDER BY 1")
            for priority, count in cur.fetchall():
                print(f"        {priority:<10} {count}")
        _ok(f"{n} opportunities scored")
        return True
    except Exception as exc:
        msg = str(exc)
        if "ModuleNotFoundError" in msg:
            _fail(msg, "a Spark worker cannot import a driver-only module — "
                       "scoring must stay as column expressions, not UDFs")
        elif "CANNOT_DETERMINE_TYPE" in msg:
            _fail(msg, "an all-null column broke type inference — use the explicit schemas")
        else:
            _fail(msg, traceback.format_exc().splitlines()[-1])
        return False


def stage_embed() -> bool:
    _head(5, "Embed corpus")
    try:
        import lakebase
        import embed_corpus
        n = embed_corpus.run()
        if n == 0:
            _warn("nothing embedded — needs templates seeded and weather polled")
            return False
        with lakebase.cursor() as cur:
            cur.execute("SELECT source_type, count(*) FROM corpus_embeddings GROUP BY 1")
            for source, count in cur.fetchall():
                print(f"        {source:<10} {count} chunks")
        _ok(f"{n} chunks embedded")
        return True
    except Exception as exc:
        msg = str(exc)
        if "No module named 'sentence_transformers'" in msg:
            _fail("sentence-transformers is not installed on this cluster",
                  "run in a cell ABOVE this one, then re-run:\n"
                  "          %pip install sentence-transformers\n"
                  "          dbutils.library.restartPython()")
        elif "Permission" in msg or "Read-only" in msg or "cache" in msg.lower():
            _fail(msg, "serverless HOME is read-only — HF_HOME must point at /tmp/hf "
                       "(embed_corpus sets this at import; make sure it is imported first)")
        else:
            _fail(msg, "check that sentence-transformers installed and the model downloaded")
        return False


def stage_retrieval() -> bool:
    _head(6, "Retrieval smoke test")
    try:
        import retrieval
        hits = retrieval.search("hail damage roof inspection", top_k=3)
        if not hits:
            _warn("no chunks cleared the similarity floor")
            print(f"        floor is {retrieval.SIMILARITY_FLOOR} — check the corpus embedded")
            return False
        for h in hits:
            print(f"        {h.similarity:.3f}  [{h.source_type}]  {h.title[:44]}")
        _ok(f"{len(hits)} chunks retrieved")
        return True
    except Exception as exc:
        _fail(str(exc), "check pgvector is enabled and corpus_embeddings has rows")
        return False


def stage_agent() -> bool:
    _head(7, "Agent dry run")
    try:
        import lakebase
        import tools
        with lakebase.cursor() as cur:
            cur.execute("""
                SELECT opportunity_id FROM opportunities
                WHERE status = 'identified' ORDER BY exposure_score DESC LIMIT 1
            """)
            row = cur.fetchone()
        if not row:
            _warn("no opportunity in 'identified' state to test with")
            return False
        opp = row[0]

        notice = tools.send_safety_notice(opp)
        if notice.get("held"):
            _ok(f"safety notice correctly held — {notice['reason']}")
        elif notice.get("ok"):
            _ok(f"safety notice sent ({notice.get('tone')} tone, {notice.get('chars')} chars)")
        else:
            _fail(str(notice.get("error")), "check the opportunity joins to a customer and event")
            return False

        draft = tools.draft_outreach(opp)
        if draft.get("blocked"):
            _ok(f"commercial gate working — {draft['reason']}")
            return True
        if not draft.get("ok"):
            _fail(str(draft.get("error")), "check the model serving endpoint name in agent/llm.py")
            return False
        _ok(f"drafted, grounded on: {draft.get('grounded_on') or 'no strong match'}")
        return True
    except Exception as exc:
        _fail(str(exc), traceback.format_exc().splitlines()[-1])
        return False


def stage_rollup() -> bool:
    _head(8, "Gold rollup (Delta CDF)")
    try:
        import gold_rollup
        result = gold_rollup.run()
        _ok(f"{result['audited']} change records, {result['grains']} rollup rows, "
            f"version {result['version']}")
        return True
    except Exception as exc:
        msg = str(exc)
        if "DELTA_MERGE_UNRESOLVED_EXPRESSION" in msg:
            _fail(msg, "the CDF state table has a stale schema — drop it and re-run:\n"
                       "          spark.sql('DROP TABLE IF EXISTS "
                       "workspace.default.rainmaker_opportunity_state')")
        else:
            _fail(msg, "needs opportunities in Lakebase — run stage 4 first")
        return False


STAGES = [
    ("Connection", stage_connection, True),
    ("Schema", stage_schema, True),
    ("Seed", stage_seed, True),
    ("Weather", stage_weather, True),
    ("Score", stage_score, True),
    ("Embed", stage_embed, False),
    ("Retrieval", stage_retrieval, False),
    ("Agent", stage_agent, False),
    ("Rollup", stage_rollup, False),
]


def main(stop_on_failure: bool = True) -> dict[str, bool]:
    """Run every stage. `required` stages halt the run; optional ones report
    and continue, so one slow model endpoint does not hide a later problem."""
    results: dict[str, bool] = {}
    for name, fn, required in STAGES:
        try:
            passed = fn()
        except Exception as exc:  # noqa: BLE001 - the runner must never itself crash
            print(f"  FAIL  unexpected: {exc}")
            traceback.print_exc()
            passed = False
        results[name] = passed
        if not passed and required and stop_on_failure:
            print(f"\n{BAR}\nStopped at {name}. Fix the above, then re-run.\n{BAR}")
            return results

    print(f"\n{BAR}\nSUMMARY\n{BAR}")
    for name, passed in results.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    if all(results.values()):
        print("\nPipeline is green. Open the app and start the demo.")
    return results


if __name__ == "__main__":
    main()
