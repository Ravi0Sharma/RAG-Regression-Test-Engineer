# Krav — RAG Regression Test Engineer

Status: beslutad inriktning; teknikval och budgetar öppna. Läranderepo.
Användaren beslutar och godkänner implementation

## Mål och omfattning

En agent genererar regressionstester för SWE-bench med Generation, Multi-Query,
RAG Fusion, Routing, Query Construction och CRAG. Agenten söker, bedömer underlag,
genererar test, kör före fixen och väljer nästa handling inom en begränsad budget.

Lyckat test: körs och misslyckas på grund av rapporterad bugg före fixen, passerar
med fixen. Exakt samma frysta test används i båda utvärderingskörningarna.
Jämför en sökomgång med återkopplingsloopen. ColBERT, RAPTOR och Multi Representation
är valfria senare experiment. Flera agentroller ingår inte.

## Indata och resultat

Agenten får uppgifts-ID, buggrapport, repository, commit före fixen, tillåten kod,
tester och dokumentation samt körkonfiguration. Endast utvärderaren får fix och facit.

Spara testfil/patch om skapad, kandidatstatus, steglogg, stopporsak, källor,
körresultat, antal försök, tid, tokens och kostnad när tillgänglig.
Separat utvärderingsresultat beskriver båda körningarna. Filformat är öppet.

## Funktionella krav

| ID | Krav och kontroll |
| --- | --- |
| F01 | Läs SWE-bench-uppgift; logga uppgifts-ID, repository och exakt commit. |
| F02 | Indexera endast tillåtet material före fixen; träffar ska tillhöra vald version. |
| F03 | Dela kod efter funktioner/klasser där praktiskt; dokumentera uppdelning av stora symboler. |
| F04 | Spåra träffar till repository, commit, fil, radnummer och symbol när tillämpligt. |
| F05 | Identifiera berörd funktion, utlösande indata, förväntat beteende och testmönster; logga plan och informationsluckor. |
| F06 | Skapa flera sökformuleringar med Multi-Query och kombinera rankningar med RAG Fusion; spara båda. |
| F07 | Routing väljer material; Query Construction bygger sökfrågor och filter. Logga val; lås repository/version. |
| F08 | CRAG bedömer relevans, täckning, luckor och motsägelser; otillräckligt underlag kan utlösa riktad följdsökning. |
| F09 | Generera test från hämtat underlag med projektets anropssätt, testmönster och fixtures. |
| F10 | Kör före fixen; spara kommando, insamling, faktisk exekvering och utfall. Ej hittade/överhoppade tester räknas inte som passerade. |
| F11 | Skilj miljöfel, syntax/importfel, passerat test, möjligt relevant buggfel och oklart/annat fel med stöd i körresultatet. |
| F12 | Välj ny sökning, teständring eller avslut utifrån observationer; demonstrera minst en sådan följdhandling. |
| F13 | Stoppa vid kandidat, förbrukad budget eller utebliven progression; logga orsak och kvarstående osäkerhet. |
| F14 | Frys testet inför separat utvärdering; ingen ändring utifrån den fixade versionens resultat. |
| F15 | Kör identiskt test före/efter fixen; spara bevis för relevant buggfel före och godkänt resultat efter. |
| F16 | Jämför en sökomgång och återkopplingsloop på samma uppgifter/modell med dokumenterade budgetar och resultat. |

## Agentbeslut och gränser

Arbetsflöde: förstå rapport, välj källa/filter, sök/fusionera, bedöm med CRAG,
generera, kör, tolka och välj nästa handling. Steg kan återbesökas.
Saknad fixture/import ger riktad sökning. Passerande test kräver granskning av
utlösande indata/assertion. Möjligt buggfel ger kandidat; upprepning utan framsteg ger stopp.

- Assertion eller exception är inte automatiskt ett buggfel; kopplingen till rapporten måste bedömas.
- Agenten får ändra testet, inte reparera produktionskoden.
- Fixpatch, tester från fixen, facit och resultat från fixad version får inte påverka genereringen.
- CRAG:s följdsökningar använder endast tillåtet material före fixen; ingen fri webbsökning.
- Utvärderaren använder jämförbara körvillkor. Miljöproblem och ofullständiga körningar redovisas separat.
- Bestäm isolering och teknisk separation innan testkörningsdelen byggs. Bevara fullständiga testloggar.

## Utvärdering och lärande

Logga handling, kort motivering, källor och observation per steg; intern resonemangstext behövs inte.
Rapportera antal valda/utvärderade uppgifter, antal/andel lyckade tester, feltyper,
stopporsaker, sökningar, genereringar, testkörningar, tid, tokens och tillgänglig kostnad.
Ange nämnare och exkluderingar. Dokumentera exakt vad som skiljer jämförelsemetoderna.

Förklara varje metods syfte, indata, utdata och anpassningar. Återanvänd referenskod
efter godkännande; ange källa och kontrollera licens. Dokumentera installation,
modell, beroenden, konfiguration och körkommandon. Modellresultat kan variera.
Granska byggstegen tillsammans. Återkopplingsloopen behöver inte slå baslinjen för att projektet ska bli klart.

## Klart när

- [ ] Nödvändiga öppna beslut nedan är fattade.
- [ ] F01–F16 är demonstrerade, inklusive en observation som ändrar agentens nästa handling.
- [ ] Ett kontrollerat exempel skiljer relevant före/efter-resultat från importfel, miljöfel och ej körda tester.
- [ ] Separation från fix/facit samt budgetar och stoppvillkor är kontrollerade.
- [ ] Båda jämförelsemetoderna har körts på beslutat urval; hinder/exkluderingar är redovisade.
- [ ] Rapporten visar resultat, resursåtgång och begränsningar; README beskriver installation och användning.

## Öppna beslut

- SWE-bench-variant, uppgifter/repon, urvalsmetod och antal. Ett repo/10–20 buggar är endast tidigare förslag.
- Språk/version, pakethantering, struktur, gränssnitt och artefaktformat.
- Lokal/extern körmiljö, maskinresurser, isolering och uppgifternas beroenden.
- Genererings-/bedömningsmodell, embeddingmodell och budget.
- Index/vector store, koduppdelning, träffantal och fusionens inställningar.
- Agentramverk, verktyg och sparat tillstånd; CRAG:s bedömningsformat/kriterier.
- Gränser för sökningar, testförsök, tid, tokens och kostnad; definition av utebliven progression. Tre försök är endast förslag.
- Bedömning av relevant buggfel, manuell granskning och oklara fall.
- Baslinjens sökmetoder och jämförelsebudgetar; eventuellt numeriskt framgångsmål.

Föreslagen byggordning: före/efter-körning, enkel RAG-baslinje, sökmetoder,
CRAG/agentloop, jämförelse. Ännu inte beslutad.

## Referenser

- [RAG From Scratch](https://github.com/langchain-ai/rag-from-scratch)
- [SWE-bench](https://github.com/SWE-bench/SWE-bench)
- [Agentic RAG-artikeln](https://unstructured.io/insights/from-static-to-smart-agentic-rag-for-enterprise-ai)
