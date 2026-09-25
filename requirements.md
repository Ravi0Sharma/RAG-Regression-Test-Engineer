# Kravspecifikation — RAG Regression Test Engineer

Status: första utkast baserat på våra beslut. Öppna frågor i avsnitt 10 ska
beslutas tillsammans innan berörda delar implementeras.

## 1. Syfte

Bygga ett läranderepo för att förstå, implementera och utvärdera hur en agent
med RAG kan generera regressionstester för buggar från SWE-bench.

Ett lyckat regressionstest ska köras på versionen före fixen och misslyckas
på grund av den rapporterade buggen, och sedan passera på versionen med fixen.
Testet ska vara identiskt vid dessa två körningar.

Användaren tar arkitektur- och implementationsbesluten. Assistenten förklarar
alternativ, hjälper till med felsökning och kan lägga in exempelkod och
anpassningar efter användarens godkännande.

Referens för RAG-delarna:
[LangChain RAG From Scratch](https://github.com/langchain-ai/rag-from-scratch).
Referensrepot finns redan länkat i projektets `readme`.

## 2. Beslutad omfattning

- Uppgifterna och underlaget för före/efter-utvärdering ska baseras på SWE-bench.
- Systemet ska använda en agent med flera verktyg och tydliga beslutspunkter.
- Följande metoder ska ingå: Generation, Multi-Query, RAG Fusion, Routing,
  Query Construction och CRAG.
- Agenten ska kunna söka igen både när hämtat underlag är otillräckligt och när
  testkörningen visar att ytterligare information behövs.
- Testgenerering och utvärdering med fixen ska hållas separerade.
- En jämförelse mellan RAG med en sökomgång och RAG med återkoppling ska ingå.
- Arbetet ska byggas stegvis och dokumenteras så att metodernas roller går att
  förstå och deras resultat går att granska.

ColBERT, RAPTOR och Multi Representation är möjliga senare experiment, inte
krav för att projektet ska vara klart. Flera agentroller ingår inte i denna version.

## 3. Indata och resultat

### Indata till agenten

- En vald SWE-bench-uppgift med uppgifts-ID och buggrapport.
- Identifierat repository och exakt version före fixen.
- Tillåten kod, befintliga tester och dokumentation från den versionen.
- Konfiguration för sökning, modell, testkörning och budgetar.

### Indata som endast den separata utvärderaren får använda

- Den tillhörande fixen och uppgifter som behövs för att skapa versionen med fixen.
- Eventuellt övrigt facitmaterial, exempelvis tester som tillhör fixen.

### Resultat per uppgift

- Genererat test som en sparad fil eller patch, om ett test har skapats.
- Status: kandidat för utvärdering eller avslutat utan kandidat.
- Körlogg med sökningar, källor, handlingar, testresultat och stopporsak.
- Antal försök, tidsåtgång, tokenförbrukning och kostnad när den kan beräknas.
- Separat utvärderingsresultat för det frysta testet före och efter fixen.

Exakta filformat och gränssnitt är öppna beslut.

## 4. Funktionella krav

| ID | Krav | Hur kravet kontrolleras |
| --- | --- | --- |
| F01 | Läsa en vald SWE-bench-uppgift och koppla den till rätt repositoryversion. | Uppgifts-ID, repository och commit framgår av körningen. |
| F02 | Göra tillåtet material före fixen sökbart. | Hämtade träffar kan kopplas till den valda versionen. |
| F03 | Dela kod med hänsyn till funktioner och klasser och behålla sammanhang där det är praktiskt möjligt. | Exempel på kodbitar visar symbolgränser; uppdelning av stora symboler dokumenteras. |
| F04 | Bevara repository, commit, fil, symbol när tillämpligt och radnummer för hämtat material. | Varje träff går att spåra tillbaka till sin källa. |
| F05 | Identifiera informationsbehov från buggrapporten: berörd funktion, utlösande indata, förväntat beteende och testmönster. | Körloggen innehåller en kort arbetsplan och markerar saknad information. |
| F06 | Använda Multi-Query för att skapa flera sökformuleringar och RAG Fusion för att kombinera träfflistor. | Formuleringarna och den kombinerade rankningen går att granska. |
| F07 | Använda Routing för att välja relevant material och Query Construction för sökfrågor med strukturerade filter. | Vald källa och använda filter framgår; sökningen kan inte lämna tillåten repositoryversion. |
| F08 | Använda ett CRAG-steg för att bedöma relevans, täckning, saknade detaljer och möjliga motsägelser. | Bedömningen kan leda till en riktad följdsökning innan testgenerering. |
| F09 | Generera test med relevant kod och befintliga tester som underlag. | Testet använder projektets anropssätt, testmönster och fixtures där det är relevant. |
| F10 | Köra testet på den buggiga versionen och kontrollera att det faktiskt kördes. | Körkommando, utfall och information om insamling och exekvering sparas. Ett överhoppat eller ej hittat test räknas inte som passerat. |
| F11 | Skilja mellan miljöfel, syntax/importfel, passerat test, möjligt relevant buggfel och oklart/annat fel. | Klassificeringen stöds av det observerade körresultatet. |
| F12 | Välja nästa handling utifrån sökbedömning och testresultat. | En körning kan demonstrera en riktad sökning eller teständring som följer av en observerad brist. |
| F13 | Avsluta vid en kandidat, förbrukad budget eller utebliven progression. | Varje körning får en explicit stopporsak och redovisar återstående osäkerhet. |
| F14 | Frysa testet inför separat utvärdering. | Samma testartefakt används före och efter fixen och ändras inte utifrån resultat från den fixade versionen. |
| F15 | Utvärdera om det frysta testet misslyckas av rätt anledning före fixen och passerar efteråt. | Båda körresultaten och underlaget för bedömningen sparas. |
| F16 | Jämföra en sökomgång med återkopplingsloopen. | En rapport visar resultat, antal försök och resursåtgång för samma uppgiftsurval. |

## 5. Agentens arbetsflöde

1. Läs buggrapporten och identifiera vad som behöver undersökas.
2. Välj material och konstruera sökningar med tillåtna filter.
3. Hämta och kombinera sökresultat med Multi-Query och RAG Fusion.
4. Bedöm underlaget med CRAG-steget.
5. Sök riktat igen om underlaget är otillräckligt och budgeten tillåter det.
6. Generera ett test när underlaget bedöms räcka.
7. Kör testet på versionen före fixen och tolka resultatet.
8. Välj mellan ny sökning, ändring av testet eller avslut.
9. Spara en kandidat eller redovisa varför ingen kandidat kunde tas fram.

Arbetsflödet får återbesöka tidigare steg. Det ska inte kräva samma antal
sökningar eller teständringar för varje uppgift.

Exempel på beslut:

- Saknad fixture → sök efter fixturedefinition och befintlig användning.
- Fel import → sök hur befintliga tester importerar funktionen.
- Testet passerar → undersök om utlösande indata och assertion verkligen prövar buggen.
- Möjligt relevant buggfel → spara testet som kandidat för separat utvärdering.
- Upprepade försök utan ny information → avsluta enligt stoppreglerna.

En misslyckad assertion eller exception är inte automatiskt ett reproducerat
buggfel. Kopplingen till buggrapporten måste bedömas. En oväntad exception kan
vara rätt fel om det är just det beteende som rapporten beskriver.

## 6. Gränser mellan agent och utvärderare

- Agentens sökningar får endast använda det tillåtna materialet före fixen.
- Fixpatchen, tester från fixen och facit får inte ingå i agentens underlag.
- CRAG:s nya sökningar begränsas till detta material; fri webbsökning ingår inte.
- Agenten får ändra det genererade testet, men inte reparera produktionskoden.
- Resultat från den fixade versionen får inte återföras till agenten för att
  förbättra testet under utvärderingen av samma uppgift.
- Utvärderaren ska använda samma frysta test och jämförbara körvillkor före
  och efter fixen. Miljöproblem och ofullständiga körningar redovisas separat.

Den tekniska mekanismen för separation och isolerad testkörning ska beslutas
innan testkörningsdelen implementeras.

## 7. Loggning och utvärdering

Varje agentsteg ska logga vald handling, en kort motivering, källreferenser
och observerat resultat. Loggen ska visa beslutens grund utan att kräva en
fullständig intern resonemangstext.

Jämförelsen ska använda samma uppgifter och modell samt dokumenterade budgetar.
Konfigurationerna ska visa vilka delar som skiljer försöken åt. Exakt vilka
sökmetoder baslinjen använder är ett öppet beslut.

Följande ska redovisas:

- Antal uppgifter som ingår och antal som faktiskt kunde utvärderas.
- Antal och andel lyckade regressionstester: relevant fel före fixen och
  godkänt test efter fixen.
- Misslyckanden fördelade på observerad feltyp och stopporsak.
- Antal sökningar, testgenereringar och testkörningar.
- Tidsåtgång, tokenförbrukning och kostnad där uppgifter finns tillgängliga.

Nämnaren för varje andel och eventuella exkluderingar ska vara tydliga.
Återkopplingsloopen behöver inte överträffa baslinjen för att lärandeprojektet
ska vara klart; jämförelsen ska vara genomförd och resultaten förklarade.

## 8. Lärande och genomförande

- Varje metod ska dokumenteras med syfte, indata, utdata och roll i projektet.
- Återanvänd exempelkod från referensrepot efter användarens godkännande;
  dokumentera källa och anpassningar samt kontrollera tillämpliga licensvillkor.
- Dokumentera modell, beroenden, konfiguration och kommandon som behövs för
  att återskapa körningarna. Modellgenererade resultat kan variera.
- Granska varje byggsteg tillsammans innan nästa större del påbörjas.

Föreslagen byggordning, ännu inte beslutad:

1. Välj SWE-bench-urval och få en kontrollerad före/efter-körning att fungera.
2. Bygg indexering, enkel sökning och Generation som baslinje.
3. Lägg till Multi-Query, RAG Fusion, Routing och Query Construction.
4. Lägg till CRAG-bedömning, agentens återkoppling och stoppregler.
5. Kör den separata utvärderingen och sammanställ jämförelsen.

## 9. Klart-kriterier

- [ ] Beslut som krävs för implementation och utvärdering i avsnitt 10 är fattade.
- [ ] Alla funktionella krav F01–F16 har demonstrerats och dokumenterats.
- [ ] Minst en dokumenterad körning visar att agenten väljer en följdhandling
      utifrån saknat underlag eller ett testresultat.
- [ ] Ett kontrollerat exempel verifierar att utvärderaren kan skilja ett
      relevant före/efter-resultat från importfel, miljöfel och ej körda tester.
- [ ] Separationen mellan agentens material och fix/facit har kontrollerats.
- [ ] Budgetar och stoppvillkor fungerar och loggar en tydlig stopporsak.
- [ ] Det beslutade SWE-bench-urvalet har körts med båda jämförelsemetoderna;
      hinder och exkluderingar är redovisade.
- [ ] Resultat, kostnad/resursåtgång och begränsningar finns i en jämförelserapport.
- [ ] README beskriver installation, körning, metoder och hur resultaten läses.

Inget numeriskt mål för lyckade tester är ännu beslutat. Ett sådant mål ska
inte anges som ett godkänt krav innan vi har tagit ställning till det.

## 10. Öppna beslut

| Beslut | Vad vi behöver bestämma |
| --- | --- |
| SWE-bench-variant och urval | Vilken variant, vilka uppgifter/repon, hur många och hur de väljs. Ett repo och 10–20 buggar var tidigare förslag, inte fattade beslut. |
| Utvecklingsmiljö | Språk, Pythonversion om tillämpligt, pakethantering och projektstruktur. |
| Körmiljö | Lokal eller extern körning, tillgänglig maskin, isolering och hur uppgifternas beroenden hanteras. |
| Modeller | Modell för generering/bedömning och embeddingmodell samt budget. |
| Sökimplementation | Index/vector store, uppdelning av stora symboler, antal träffar och fusionens inställningar. |
| Agent och verktyg | Ramverk om något, exakt verktygsuppsättning och vilket tillstånd agenten sparar. |
| CRAG-bedömning | Format och kriterier för relevans, tillräcklighet, motsägelser och följdsökningar. |
| Stoppgränser | Max antal sökningar och testförsök, tids-/token-/kostnadsgränser och definition av utebliven progression. Tre försök är ett tidigare förslag. |
| Utvärderingsbedömning | Hur vi avgör att felet motsvarar buggen, inklusive manuell granskning och oklara fall. |
| Baslinje | Vilka sökmetoder en sökomgång innehåller och vilka budgetar som gör jämförelsen begriplig. |
| Gränssnitt och artefakter | Notebook, kommandorad eller annat, samt format för tester, loggar och rapporter. |
| Framgångsmål | Om projektet ska ha ett numeriskt resultatmål utöver genomförd och dokumenterad utvärdering. |

## 11. Referenser

- [RAG From Scratch](https://github.com/langchain-ai/rag-from-scratch)
- [SWE-bench](https://github.com/SWE-bench/SWE-bench)
- [From Static to Smart: Agentic RAG for Enterprise AI](https://unstructured.io/insights/from-static-to-smart-agentic-rag-for-enterprise-ai)

Metodnamnen beskriver vad vi ska lära oss och implementera. Exakta anpassningar
från referensmaterialet till kodsökning och regressionstester dokumenteras i projektet.
