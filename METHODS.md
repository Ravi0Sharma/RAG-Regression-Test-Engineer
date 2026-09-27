# Metoder och kodkarta

`dataset` separerar tillåtna fält och referensfix. `prepare` hämtar kod för exakt
`base_commit`, utan Git-historik eller symlänkar. `run` använder endast publika fält
och indexet. `evaluate` läser fixen först efter att testartefakten frysts och hashats.

| Del | Implementation | Funktion |
| --- | --- | --- |
| Dataset | `data.py` | Explicit fältlista, reproducerbart urval, separat oracle. |
| Koduppdelning | `retrieval.py:chunks_for_file` | AST för funktioner, klasser och klassmetoder med kvalificerade symbolnamn. Stora symboler delas i högst 100 rader/ungefär 6 KB. Imports/modulkod får egna bitar; äldre syntax använder radfönster. |
| Index | `retrieval.py:build_index` | Python, Markdown, reST, text, YAML och SQL i lokalt JSON-index. YAML/SQL under testkataloger routas som tester. Dolda filer, filer över 500 KB och enstaka rader över 6 KB hoppas över. |
| Multi-Query | `model.py:PLAN` | Högst tre kompletterande sökformuleringar. |
| Routing/Query Construction | `Retriever.search` | Agenten väljer kod/tester/docs och filglob. Repository och commit låses av programmet. |
| RAG Fusion | `retrieval.py:rrf` | Summan av `1 / (60 + rank)`. Dubbletter räknas en gång per lista. |
| Hybrid retrieval | `Retriever.search` | BM25 och cosine similarity fusioneras inom frågan, sedan fusioneras frågornas rankningar. |
| Testmönster och setup | `Retriever.context_ids`, `workflow.select_sources` | Reserverar 1–2 av `top_k` platser för rankade Python-testfunktioner. Ett valt exempel följer med imports, faktisk klassdeklaration och högst en entydigt importerad lokal basklass före valfria helpers. Relativa importer stannar inom paketet; saknade, tvetydiga och kvalificerade baser lämnas olösta. Nya primära träffar får första `top_k` platserna, sedan kommer testpaketet och äldre underlag rangordnat mot buggrapporten inom `2 * top_k`. Ankaret byts bara när modellen läser ett annat test-ID. `bundle_*`, `retained_ids` och trunkering loggas. |
| CRAG | `model.py:ASSESS`, `workflow.py` | Bedöm relevans, täckning, luckor och motsägelser. Riktad läsning förbrukar en sökomgång. Upprepade källor/luckor ger tidigt stopp först om nästa föreslagna verktygsbegäran redan körts. Oprövade sökningar tillåts inom taket på sex omgångar. Exakt normalisering upptäcker inte alla semantiska upprepningar. |
| Generation | `model.py:TEST`, `workflow.py` | Modellen väljer katalog och kod. Programmet låser basename till `test_rag_regression.py`, loggar namnjustering och validerar att inget skrivs över. Testkod ändras inte. Ingen shell-sträng från modellen körs. |
| Återkoppling | `workflow.py`, CLI-förare `agent.py` | En agent väljer sökning, ändring, kandidat eller stopp efter observerat resultat. |
| Verktygsargument | `model.py:validate_source_request`, `agent.py`, `handoff.py` | Käll-ID:n kontrolleras före verktygskörning. Högst en modellrättning per bedömning, inom total modellbudget; avvisade svar sparas. Gäller båda jämförelsemetoderna. |
| Körning | `runner.py`, `runtime.py` | Ny Docker-container, avstängt nätverk, inga host-mounts/nycklar. Faktisk exekvering registreras. |
| Utvärdering | `evaluation.py` | Identiskt test före/efter fixen, matchande test-ID:n och mänsklig relevansbedömning. |

CRAG här är en projektspecifik korrigerande loop, inte en full reproduktion av artikelns
evaluator/webbsökning. Baslinjen använder samma routing/fusion/bedömning men ingen iteration;
den genererar ett test även vid svagt underlag. Jämförelsen mäter hela återkopplingsflödet,
inte varje metods isolerade effekt. Budgetskillnader redovisas.

Handoff och API-CLI kontrollerar före modellarbete att testbackenden kan importeras
i en nätverkslös container. `runtime-check.json` sparar versioner eller fel.
Kontrollen ersätter inte task-specifika beroenden, fixtures eller korrekt setup.
`runner_setup` beskriver dessa gränser för modellen.

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
SQLFluffs runtime använder `refresh_package_metadata: true`: efter `git clean` körs
`setup.py egg_info` offline från base commit, före fixpatch och genererat test. Detta
återskapar plugins som annars försvinner med editable-installationens metadata.
Metadatafel avbryter som `environment_error`; de räknas inte som reproducerad bugg.

## Verifierat lokalt

- 78 automatiska tester passerade med Studio-/testberoenden. I basmiljön passerade 70 och 8 hoppades över. Offline-demot passerade.
- SWE-bench Lite-urvalet har 5 dev- och 10 testuppgifter utan överlapp. Ett manuellt pvlib-kontrolltest gav NaN före fixen och 0 efteråt med samma frysta kod och pinnade Docker-image.
- En manuell Luna-överlämning gav ett oförändrat, modellgenererat fail-to-pass-test i Docker. Relevansbedömning återstår. Extern användning av tokens och pengar är okänd; se [HANDOFF.md](HANDOFF.md).
- Tidiga självständiga försök stoppade ibland utan test på evidensluckor. Sök-, käll-ID- och setup-rättningar följdes av nya körningar.
- En senare lokal dev-omgång gav 10/10 tekniska fail-to-pass-övergångar: 5 baseline och 5 agentic, med 50 modellsvar och 30 Docker-körningar. Oberoende mänsklig relevansgranskning återstår. Detta är inte en generell modellpoäng.

Råa testloggar, körresultat och experimentprotokoll bevaras lokalt under Git-ignorerade
`runs/` och `experiments/`. Sammanfattningar ovan skiljer modellförsök från manuellt
skrivna kontrolltester. Studio-demot har separat facitgraf; se [STUDIO.md](STUDIO.md).

## Källor

Egna implementationer med [RAG From Scratch](https://github.com/langchain-ai/rag-from-scratch)
som pedagogisk referens; ingen notebookkod kopierad.
API: [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
Image-namn: [SWE-bench v3.0.15 TestSpec](https://github.com/SWE-bench/SWE-bench/blob/v3.0.15/swebench/harness/test_spec/test_spec.py).
Upstream v5 har en ny byggväg; se [aktuell README](https://github.com/SWE-bench/SWE-bench).
Dataset: [Hugging Face rows API](https://huggingface.co/docs/dataset-viewer/rows).
