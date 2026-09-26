# Se demot i LangGraph Studio

**Börja här:** se vad varje nod får, lämnar vidare och väljer som nästa steg.
Detta är ett körbart demo med fasta modellsvar och riktig lokal testexekvering.
Det anropar varken OpenAI eller embeddings. LangSmith får körspår när tracing är på.

## Starta

Från projektroten, med den installerade `.venv`:

```sh
.venv/bin/langgraph dev --studio-url https://eu.smith.langchain.com
```

Behåll terminalen öppen. Servern kör på `127.0.0.1:2024`; Ctrl+C stoppar den.
Öppna [Studio för den lokala servern](https://eu.smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024)
och logga in på samma LangSmith-konto som projektet `test-Eng`.
Studio är en webbsida, ingen separat app att installera.

## 1. Följ agentens försök

Välj grafen **regression_demo** och kör med denna JSON-indata:

```json
{"mode": "agentic"}
```

Flödet är `prepare → plan → search → assess → generate → execute → decide`.
`decide` kan gå tillbaka till sökning/generering eller vidare till `freeze`.

| Nod | Öppna dessa fält | Vad de visar |
| --- | --- | --- |
| `prepare` | `task`, `run_id` | Buggrapporten och ID för denna separata körning. |
| `plan` | `model_input`, `route` | Vad planeringen fick och vilka frågor/filter den föreslog. |
| `search` | `sources`, `search_trace` | De faktiska kodbitarna, källorna och träfflistorna. |
| `assess` | `model_input`, `assessment` | Underlaget och bedömningen av om det räcker. |
| `generate` | `model_input`, `proposal` | Underlaget och den föreslagna testkoden. |
| `execute` | `result` | Utförda testfall, feltyp och feltext. |
| `decide` | `decision`, `next_node` | Beslutet och vart flödet går. |
| `freeze` | `frozen`, `summary` | Det låsta testet och körningens sammanfattning. |

Varje nod ger även **`explanation`**, en kort svensk förklaring.
Nodens output visar ändringarna; state-vyn visar all data som hittills har samlats.

Förväntat förlopp:

1. Första testet har avsiktligt fel import: `syntax_import_error`.
2. Beslutet blir en ny sökning.
3. Andra testet har korrekt import och avslöjar mellanslagsbuggen: `test_failure`.
4. Testet fryses som **kandidat**, ännu inte bekräftad framgång.

Kör sedan en ny körning med `{"mode": "baseline"}`. Den stannar efter första testversionen.
Skillnaden är inbyggd i demot och säger inget om vilken AI-modell som är bäst.

## 2. Utvärdera med facit — separat graf

Kopiera **`run_id`** från genereringsgrafens resultat. Det är demots 32 tecken långa ID,
inte Studio-gränssnittets thread-ID eller serverns run-ID.
Välj grafen **evaluate_demo** och kör:

```json
{"run_id": "klistra-in-demots-run_id-har"}
```

Öppna `evaluation.before`, `evaluation.after` och `evaluation.fail_to_pass`.
För agentic-exemplet ska testet misslyckas före fixen och passera efteråt.
`pending_review` betyder att mänsklig relevansgranskning återstår; ingen automatisk godkännandestämpel ges.
Utvärderingen körs en gång per demo-ID för att bevara bevisen. Skapa en ny generering för att prova igen.
Det finns ingen övergång från facitgrafen tillbaka till genereringsgrafen.

Lokala filer ligger i `runs/studio/<run_id>/generation/`:
`events.jsonl`, `attempt-*/`, `frozen.json`, `summary.json` och efter utvärdering `evaluation/`.

## LangSmith-inställningar

Servern läser projektets lokala `.env`. Följande namn används; nyckeln ska bara finnas lokalt:

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com
LANGSMITH_API_KEY=<din-lokala-nyckel>
LANGSMITH_PROJECT=test-Eng
```

Inget `OPENAI_API_KEY` krävs för dessa två grafer. Tracing-vyn i LangSmith visar steg och in-/utdata.
Sätt `LANGSMITH_TRACING=false` om du vill stänga av uppladdningen av körspår.
Nycklar ingår inte i grafens state eller dess nodloggar.

## Installation på nytt

Python **3.11+** krävs för Studio-servern. På denna dator finns `python3.11`.
Från projektroten:

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[studio,test]'
```

De direkta Studio-beroendena är versionslåsta i `pyproject.toml`.
`langgraph.json` registrerar de två graferna. `.venv`, `.env`, `runs` och serverns `.langgraph_api` är Git-ignorerade.
LangChain/OpenAI-paketen från webbplatsens väderexempel behövs inte för vårt demo.

## Om något inte visas

- **Failed to fetch, men localhost visar `{"ok":true}`:** öppna webbplatsinformationen till vänster om adressen på Studio-fliken. Om **Apps on device** visar **Automatically blocked**, slå på behörigheten för `eu.smith.langchain.com` och ladda om Studio. I andra Chrome-versioner heter behörigheten **Local network access**. Den behövs för att webbsidan ska nå den lokala servern.
- **Kan inte ansluta:** kontrollera att startkommandot fortfarande kör och att porten är 2024. Använd länken som servern skriver ut om porten ändrats.
- **Safari blockerar localhost:** öppna samma Studio-länk i Chrome/Edge och tillåt lokal nätverksåtkomst om webbläsaren frågar.
- **Grafen finns men inga traces:** kontrollera tracing-inställningen, EU-endpointen, nyckelns behörighet och projektet `test-Eng`.
- **Evaluation already exists:** kör en ny generering och använd dess nya `run_id`.

## Avgränsning

Studio-versionen återanvänder demots uppgift, ScriptedModel, sökning, DemoRunner och evaluator.
Den visar en egen pedagogisk graf; SWE-bench-kommandot `run` använder fortfarande `agent.py`.
Studio-grafen demonstrerar sök-/testgränser, men är inte ersättaren för CLI-agentens fulla tids-, kostnads- och stagnationskontroller.
DemoRunner accepterar bara de två inbyggda teststrängarna, även om kod ändras i Studio.
En godtycklig Luna-genererad testfil ska senare gå genom Docker, inte denna lokala demokörare.

Nästa steg: gå igenom nodernas observationer tillsammans, därefter bygga den kontrollerade bryggan för Luna-svar via Codex.
Den bryggan och SWE-bench i Studio är ännu inte implementerade.

Källor: [LangGraph state/noder](https://docs.langchain.com/oss/python/langgraph/graph-api),
[lokal utvecklingsserver](https://docs.langchain.com/langsmith/local-dev-testing),
[Studio](https://docs.langchain.com/langsmith/quick-start-studio).
