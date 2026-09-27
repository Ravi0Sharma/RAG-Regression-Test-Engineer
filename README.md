# RAG Regression Test Engineer

One agent. A SWE-bench bug report. A regression test that fails before the fix and passes after it.

Built to learn retrieval, test generation and feedback loops. The reference fix stays outside generation.

## Flow

[![Retrieval, test generation and separate evaluation](assets/architecture.svg)](assets/architecture.svg)

`plan → search → assess → generate → execute → decide → freeze`

- **Search:** Multi-Query, routing, BM25 and RAG Fusion. Hybrid mode adds embeddings.
- **Assess:** check evidence and gaps; search again when needed. CRAG-inspired, adapted to the pinned repository.
- **Generate / execute:** write `test_rag_regression.py`; run in Docker. Use failures to revise or search again.
- **Freeze / evaluate:** lock the test; run identical code before and after the reference fix.

Agentic mode can loop. Baseline gets one search and one test version. Limits and models live in [config.json](config.json).

## Run

Python 3.9+, Git, Docker. Default: Luna high, hybrid retrieval, `text-embedding-3-small`. API runs need `OPENAI_API_KEY` in local `.env` or the environment.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m rag_regression doctor
python -m rag_regression dataset --split dev --count 5 --out data/dev
```

Pick `INSTANCE_ID` from `data/dev/selection.json`:

```sh
TASK=data/dev/public/INSTANCE_ID
python -m rag_regression prepare "$TASK"
python -m rag_regression image "$TASK"
python -m rag_regression index "$TASK"
python -m rag_regression run "$TASK" --mode agentic --out runs/pilot
python -m rag_regression evaluate runs/pilot --oracle data/dev/oracle/INSTANCE_ID.json
```

Use a new output path per run. `image "$TASK" --image IMAGE` accepts a custom image. Put `--config FILE.json` before the subcommand. Use `--mode baseline` for one pass.

Read the test and `evaluation/before/output.log` / `evaluation/after/output.log`. Then record your verdict:

```sh
python -m rag_regression review runs/pilot --verdict relevant --reason "Why this failure matches the bug"
```

Offline demo, no Docker or model API: `python -m rag_regression demo --out runs/demo-new`.
Tests: `python -m unittest discover -s tests -v`.

## Studio

Use a Python **3.11+** environment. Configure local `.env` for LangSmith tracing: `LANGSMITH_TRACING`, `LANGSMITH_ENDPOINT`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`. EU endpoint: `https://eu.api.smith.langchain.com`. Set tracing to `false` to disable trace uploads.

```sh
python -m pip install -e '.[studio,test]'
langgraph dev --studio-url https://eu.smith.langchain.com
```

Open the printed URL. Keep the server running.

- `regression_demo`: submit `{"mode":"agentic"}`. Inspect `sources`, `assessment`, `proposal`, `result`, `next_node`.
- `evaluate_demo`: submit `{"run_id":"..."}` from the generation output, not Studio's thread ID.
- `swe_handoff` / `swe_evaluate`: prepared SWE-bench tasks, manual model responses, Docker execution.

Demo responses are fixed; no OpenAI key needed. “Failed to fetch” while localhost works: check browser permission for local network/device access. Evaluate each frozen run once.

<details>
<summary>Manual model handoff</summary>

Prepare a task in `data/dev/selection.json`. Set `retrieval` to `lexical` before indexing to avoid embedding calls.

```sh
python -m rag_regression handoff start INSTANCE_ID
```

Give the external model the current request only. Return `answer` according to its schema. All envelope fields are required:

```json
{
  "request_id": "ID_FROM_CURRENT_REQUEST",
  "answer": {},
  "provenance": {
    "provider": "provider-name",
    "model": "model-name",
    "manual_edits": false,
    "note": ""
  }
}
```

Manual edits: set `manual_edits=true` and explain in `note`.

```sh
python -m rag_regression handoff respond RUN_ID --file response.json
python -m rag_regression handoff advance RUN_ID
python -m rag_regression handoff status RUN_ID
```

Repeat until `next_node=end`. `respond` imports; `advance` executes. After a Docker preflight failure, restore access and retry `advance` on the same run.

```sh
python -m rag_regression evaluate runs/handoff/RUN_ID/generation --oracle data/dev/oracle/INSTANCE_ID.json
```

External agents are started separately. Their tokens and cost are not metered by this application.

</details>

## Files

- `rag_regression/`: retrieval, workflow, runner, evaluator, Studio adapters.
- `tests/`: automated checks.
- `data/`: local source, index, runtime and separate reference fixes.
- `runs/`: model packets, test attempts, raw logs and frozen results.

Data, runs, experiments, local notes and `.env` stay outside Git.

## Example: Django `sqlmigrate`

**Bug:** `sqlmigrate` emitted `BEGIN` / `COMMIT` even when the database could not roll back DDL. Task: `django__django-11039`.

**How Luna handled the fresh run:**

1. Found `sqlmigrate` and existing migration tests in one search.
2. Judged the evidence sufficient. Generated a test using `TransactionTestCase`.
3. Registered the app before database setup. Kept `MIGRATION_MODULES` on the test method.
4. Called the real command with `can_rollback_ddl=False` mocked. Asserted that transaction wrappers were absent.
5. Saw the `BEGIN;` assertion fail. Selected a candidate. The test was frozen, then evaluated unchanged before and after the reference fix.

| Check | Result |
| --- | --- |
| Before fix | Fails on unwanted `BEGIN;` |
| After fix | Same test passes |
| Search / test versions / model packets | **1 / 1 / 4** |

## References

[SWE-bench Lite](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite)