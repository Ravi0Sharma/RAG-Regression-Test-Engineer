# RAG Regression Test Engineer

Läranderepo: en agent söker kod och genererar regressionstester från SWE-bench Lite.
Modellen körs via OpenAI API; testerna körs i separata Docker-containrar.
Python 3.9+, Git och Docker. CLI:t använder bara standardbiblioteket.

## Se flödet i Studio

LangGraph-demot har synliga noder, in-/utdata och svenska förklaringar.
Separat graf utvärderar det frysta testet. Fasta modellsvar; ingen OpenAI-nyckel behövs.
Studio kräver Python 3.11+ och extra beroenden. [Starta och följ demot](STUDIO.md).

```sh
.venv/bin/langgraph dev --studio-url https://eu.smith.langchain.com
```

## Prova utan API

```sh
python3 -m rag_regression doctor
python3 -m unittest discover -s tests -v
python3 -m rag_regression demo --out runs/demo-new
```

Demot använder fasta svar och en egen liten bugg. Det verifierar flödet, inte modellkvalitet eller SWE-bench-resultat.
Rapport: `runs/demo-new/report/report.md`. Befintliga körningar skrivs inte över.
Valfritt: `python3 -m pip install -e '.[test]'` installerar CLI-kommandot och pytest för extra körartester.

## Kör SWE-bench

`.env` är lokal och git-ignorerad; Studio-inställningarna använder LangSmith. För modell-API-anrop: sätt `OPENAI_API_KEY` i din lokala
`.env` eller som miljövariabel. Nyckeln loggas aldrig. Starta Docker; förbered en uppgift i taget.

```sh
# Importera en gång. Dessa urval finns redan lokalt efter implementationen.
python3 -m rag_regression dataset --split dev --count 5 --out data/dev
python3 -m rag_regression dataset --split test --count 10 --out data/test

# Hoppa över prepare om source redan finns.
RAG_TASK=data/dev/public/pvlib__pvlib-python-1154
python3 -m rag_regression prepare "$RAG_TASK"
python3 -m rag_regression image "$RAG_TASK"
python3 -m rag_regression index "$RAG_TASK"
python3 -m rag_regression run "$RAG_TASK" --mode baseline --out runs/pilot/baseline/pvlib__pvlib-python-1154
python3 -m rag_regression run "$RAG_TASK" --mode agentic --out runs/pilot/agentic/pvlib__pvlib-python-1154
```

`config.json`: GPT-6 Luna, `text-embedding-3-small`, hybrid BM25/embeddings, 8 träffar,
högst 3 frågor/sökomgång. `retrieval: lexical` ger BM25 utan embedding-anrop.
Bygg om index vid byte till hybrid/embeddingmodell. Vid modellbyte: uppdatera även tokenpriser.

Agentbudget: 6 sökomgångar, 3 testversioner, 24 API-anrop, 100 000 tokens, 10 minuter,
120 sekunder/test och uppskattat $1 per uppgift. Indexering: separat $1, 5 miljoner tokens,
1 000 anrop, 30 minuter; justera via `index --help`. Priser är uppskattningar, inte fakturakontroll.
API-avbrott kan ge okänd debitering; ingen automatisk retry.

`image` hämtar en klassisk SWE-bench-image och pinnar image-ID. ARM-Mac använder amd64-emulering.
Saknas image: bygg en kompatibel image enligt upstream och använd `image --image IMAGE`.
Den behöver Git, `/testbed` och repo-beroenden i `/opt/miniconda3/envs/testbed/bin/python`.
Annan Python-sökväg kan anges i `runtime.json`. Pytest är standard; `--backend unittest`
eller `--backend django` väljer alternativ. Egna Django-appar/databaser och kompilerade fixar kan behöva en anpassad image.

Den valda pvlib-uppgiftens upstream-image har NumPy 2, som bryter gamla `np.Inf`-anrop.
En separat image rättar bara beroendet:

```sh
docker build --platform linux/amd64 -f docker/pvlib-0.8.Dockerfile -t rag-regression/pvlib-1154:numpy1 docker
python3 -m rag_regression image "$RAG_TASK" --image rag-regression/pvlib-1154:numpy1
```

Gör detta före generering/utvärdering; båda versionerna använder samma pinnade image.

## Utvärdera separat

```sh
RAG_RUN=runs/pilot/agentic/pvlib__pvlib-python-1154
python3 -m rag_regression evaluate "$RAG_RUN" --oracle data/dev/oracle/pvlib__pvlib-python-1154.json
# Läs fryst test och evaluation/{before,after}/output.log före bedömningen.
python3 -m rag_regression review "$RAG_RUN" --verdict relevant --reason "Beskriv kopplingen till buggrapporten"
python3 -m rag_regression report runs/pilot --selection data/dev/selection.json --out runs/pilot-report
```

Utvärdera även baslinjen. Fail-to-pass är preliminärt tills du granskat att testet provar buggen,
inte läser facit, ändrar produktionskod eller manipulerar köraren. `unrelated`/`unclear` räknas inte som lyckade.
Modellens bedömning räcker inte. Saknade körningar och miljöfel ligger kvar i nämnaren.

Efter förberedelse/indexering av alla valda uppgifter:

```sh
python3 -m rag_regression batch data/dev --out runs/batch-1 --max-total-usd 5
```

Batchbudgeten gäller generering; indexering/Docker ingår inte. Utvärdering sker efter generering
och återförs aldrig till agenten. Lås metod och konfiguration på dev innan `data/test` används.

- [Beslutskarta: varje steg och varför det finns](DECISIONS.md)
- [Krav och verifieringsstatus](requirements.md)
- [Metoder och kodkarta](METHODS.md)
- [Arbetssätt och skills](AGENTS.md)
- [RAG From Scratch](https://github.com/langchain-ai/rag-from-scratch)
- [SWE-bench](https://github.com/SWE-bench/SWE-bench)
