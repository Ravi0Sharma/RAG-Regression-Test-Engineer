# Manuell SWE-bench-överlämning

`handoff` kör samma arbetssteg som API-agenten, men pausar vid varje modellfråga. Du skickar frågan till valfri modell utanför programmet och importerar dess strukturerade svar. Detta är för det frysta dev-urvalet. Externa tokens och kostnader är okända; körningen ingår inte i API-jämförelsen.

## Förbered

Välj ett ID i `data/dev/selection.json`. Kör `prepare`, `image` och `index` enligt [README](README.md). Handoff använder lokalt BM25-index och kräver en fungerande pinnad Docker-image.

```sh
python3 -m rag_regression handoff start INSTANCE_ID
```

Kommandot ger ett `run_id` och en `request` med `request_id`, systeminstruktion, kontext och svarsschema. Spara svaret i en lokal JSON-fil med exakt detta skal:

```json
{
  "request_id": "ID_FRÅN_REQUEST",
  "answer": {},
  "provenance": {
    "provider": "modellleverantör",
    "model": "modellnamn",
    "manual_edits": false,
    "note": ""
  }
}
```

Fyll `answer` enligt schemat i aktuell `request`. En manuell ändring kräver `manual_edits: true` och en förklaring i `note`. Programmet kontrollerar bland annat käll-ID:n och sparar avvisade svar.

```sh
python3 -m rag_regression handoff respond RUN_ID --file svar.json
python3 -m rag_regression handoff advance RUN_ID
python3 -m rag_regression handoff status RUN_ID
```

Upprepa `respond` och `advance` för varje ny fråga tills `next_node` är `end`. `respond` importerar bara svaret; `advance` utför nästa steg. Om Docker-åtkomst fallerar vid `advance`, rätta åtkomsten och kör samma `advance` igen. Skicka inte om samma svar. Session, frågor, svar, proveniens och fullständiga testloggar ligger i `runs/handoff/RUN_ID/`.

## Utvärdera separat

När `frozen.json` finns i `generation/`:

```sh
python3 -m rag_regression evaluate runs/handoff/RUN_ID/generation --oracle data/dev/oracle/INSTANCE_ID.json
```

Granska testet och loggarna före/efter. Registrera relevans med `review` enligt [README](README.md). I Studio motsvarar graferna `swe_handoff` och `swe_evaluate` samma separerade steg; se [STUDIO.md](STUDIO.md).
