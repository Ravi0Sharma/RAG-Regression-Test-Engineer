# Metoder och kodkarta

`dataset` separerar tillåtna fält och referensfix. `prepare` hämtar kod för exakt
`base_commit`, utan Git-historik eller symlänkar. `run` använder endast publika fält
och indexet. `evaluate` läser fixen först efter att testartefakten frysts och hashats.

| Del | Implementation | Funktion |
| --- | --- | --- |
| Dataset | `data.py` | Explicit fältlista, reproducerbart urval, separat oracle. |
| Koduppdelning | `retrieval.py:chunks_for_file` | AST för funktioner/klasser. Stora symboler delas i högst 100 rader/ungefär 6 KB. Imports/modulkod får egna bitar; äldre syntax använder radfönster. |
| Index | `retrieval.py:build_index` | Lokalt JSON-index. Dolda filer, filer över 500 KB och enstaka rader över 6 KB hoppas över. |
| Multi-Query | `model.py:PLAN` | Högst tre kompletterande sökformuleringar. |
| Routing/Query Construction | `Retriever.search` | Agenten väljer kod/tester/docs och filglob. Repository och commit låses av programmet. |
| RAG Fusion | `retrieval.py:rrf` | Summan av `1 / (60 + rank)`. Dubbletter räknas en gång per lista. |
| Hybrid retrieval | `Retriever.search` | BM25 och cosine similarity fusioneras inom frågan, sedan fusioneras frågornas rankningar. |
| CRAG | `model.py:ASSESS`, `agent.py` | Bedöm relevans, täckning, luckor och motsägelser. Följdsök vid otillräckligt underlag. |
| Generation | `model.py:TEST` | Structured Outputs ger testfil, kod och förväntat fel. Ingen shell-sträng från modellen körs. |
| Återkoppling | `agent.py` | En agent väljer sökning, ändring, kandidat eller stopp efter observerat resultat. |
| Körning | `runner.py`, `runtime.py` | Ny Docker-container, avstängt nätverk, inga host-mounts/nycklar. Faktisk exekvering registreras. |
| Utvärdering | `evaluation.py` | Identiskt test före/efter fixen, matchande test-ID:n och mänsklig relevansbedömning. |

CRAG här är en projektspecifik korrigerande loop, inte en full reproduktion av artikelns
evaluator/webbsökning. Baslinjen använder samma routing/fusion/bedömning men ingen iteration;
den genererar ett test även vid svagt underlag. Jämförelsen mäter hela återkopplingsflödet,
inte varje metods isolerade effekt. Budgetskillnader redovisas.

## Artefakter och begränsningar

- `data/*/public/`: buggrapport, source, index, runtime. Agenten har inga fria filsystemverktyg.
- `data/*/oracle/`: referensfix, endast läst av evaluator.
- `runs/*/events.jsonl`: handlingar, korta motiveringar, källor och API-usage.
- `attempt-*/`: fullständiga loggar, exekveringsbevis och klassificering.
- `frozen.json`: testkod, placering, provenance och runtime med integritetshash.
- `evaluation/`: före/efter-körningar och granskningsbeslut.

Hashar skyddar mot oavsiktliga ändringar, inte en illvillig operatör. Containrar begränsar
påverkan på hosten men gör inte genererad kod till pålitligt facit. Granska testets semantik
och möjlig manipulation. Repo-specifik installation/fixtures kan ge miljöfel.
Fixar som kräver omkompilering behöver en anpassad körmiljö; git-apply räcker inte då.

## Verifierat lokalt

Studio: en separat pedagogisk LangGraph-graf återanvänder demo, sökning och testkörare.
Facit läses i en andra graf först efter frysning. Se [STUDIO.md](STUDIO.md) för körning och begränsningar.

- 34 automatiska tester med Studio-tillägget, inklusive riktig pytest-exekvering, det syntetiska agentflödet och separat grafutvärdering.
- SWE-bench Lite hämtat: 5 dev/10 test utan överlapp; pvlib-koden indexerad på exakt base commit.
- `pvlib__pvlib-python-1154`: ett manuellt skrivet kontrolltest ger NaN före fixen och 0 efteråt.
  Samma frysta kod, samma pinnade Docker-image. Loggar: `runs/swe-smoke-numpy1/evaluation/`.
- Upstream-image behövde NumPy 1.26.4; reproducerbar justering finns i `docker/pvlib-0.8.Dockerfile`.
- Ingen modell-/embedding-API-körning gjord. Resultaten ovan mäter inte Luna eller RAG-kvalitet.

## Källor

Egna implementationer med [RAG From Scratch](https://github.com/langchain-ai/rag-from-scratch)
som pedagogisk referens; ingen notebookkod kopierad.
API: [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
Image-namn: [SWE-bench v3.0.15 TestSpec](https://github.com/SWE-bench/SWE-bench/blob/v3.0.15/swebench/harness/test_spec/test_spec.py).
Upstream v5 har en ny byggväg; se [aktuell README](https://github.com/SWE-bench/SWE-bench).
Dataset: [Hugging Face rows API](https://huggingface.co/docs/dataset-viewer/rows).
