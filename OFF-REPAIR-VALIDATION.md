# Lokalna naprawa OFF po unresolved accepted intent

## Zakres i przyczyna

Baza: `d235fa9`, manifest 0.8.9. Bez publikacji, push, połączeń z chmurą,
wywołań usług HA ani zmian produkcji. Wersji manifestu nie zmieniono.

Produkcyjny `command_py` z `installed-hon-command-source.json` jest identyczny
z bazowym checkoutem. Trzy rzeczywiste katalogi AC mają w `stopProgram`
pełne pola mutable: machMode, tempSel, windSpeed, louvres, presety itd.
Dotychczasowy warunek `neutral_off` odrzucał taki zwykły OFF przy unresolved
SET/START. Istniejący test neutralnego OFF zastępował katalog jednopolowym.
`climate.async_turn_off` ignorował `send=False`, więc zakończone wywołanie usługi
nie dowodziło nawet wysłania POST. Dodatkowo predykat dopuszczał niektóre
sterujące pola `fixed` (np. rapidMode), mimo że nie są neutralne.

## Zmiana

- Tylko zwykły AC `stop_command()` / `stop_command({onOffStatus: 0})`,
  z zadeklarowanym fixed power=0 i fixed operationName=`grSetDAC`, otrzymuje
  izolowany draft `{onOffStatus: 0, operationName: grSetDAC}` bez ancillary.
  Nie seeduje mutable settings ani nie modyfikuje źródłowego katalogu.
- Explicit dodatkowe pola zachowują pełny, walidowany i fenced draft.
  Nie ma ogólnego wyjątku „onOffStatus=0 oznacza zgodę na stare parametry”.
- Predykat neutralności dopuszcza tylko power, echo i znany fixed opcode;
  dodatkowe pola i ancillary wykluczają wyjątek nawet dla typologii fixed.
  Istniejący transport quiet nadal dodaje echoStatus=1, jeżeli opcja jest włączona.
- Native climate.turn_off i set_hvac_mode=off zgłaszają HomeAssistantError,
  jeżeli komenda została withheld. Nie zamieniają ACK na fizyczne potwierdzenie.
- Zachowano istniejące łączenie fence przy zaakceptowanym OFF: nadpisuje tylko
  oczekiwane power, nie kasuje wcześniejszych nierozstrzygniętych pól.

Nie ma funkcji `_await_command_context` ani `_is_neutral_off` w tej bazie:
readback jest w `HonCommand._send`, a predykat inline w `send`.
`load_context` już publikuje każdy poprawny rzeczywisty snapshot przed oceną
zgodności pending. To zachowanie pozostaje bez zmian i ma nowe regresje:
późniejszy GET power=0 publikuje OFF przy niezgodnych starych nastawach;
niezgodne nastawy nadal blokują nowe nie-neutralne komendy. ACK, błędny GET
ani stary GET nigdy nie tworzą optymistycznego OFF. Dopiero rzeczywisty GET
zgodny z całym fence usuwa go naturalnie. Nie użyto kasowania fence ani reload.

## Dowody TDD (lokalnie `.git/hon-off-audit/`)

Uruchomienia: checkout `.venv/bin/python -m pytest`; fixture HTTP jest
fail-closed; shadow snapshots są reprezentatywne, nie produkcyjne.
Pełne katalogi stop skopiowano z archiwalnego read-only
`actual-program-catalog.json`; sprawdzono równość całej projekcji 3 schematów.

- Baseline: `baseline.log/xml`: **467 passed**, 228 ostrzeżeń deprecation.
- RED1: `red1.log/xml`: **6 failed**, tylko 1 POST zamiast 2, OFF withheld;
  3 rzeczywiste katalogi × unresolved SETTINGS/START.
- GREEN1: **17 passed** (nowe + istniejące authorization).
- RED2: `red2-valid.log/xml`: **2 failed**, DID NOT RAISE HomeAssistantError;
  native turn_off oraz set_hvac_mode. Pierwsza próba `red2.log` zawiera też
  niepoprawny fixture set_temperature bez temperature, odrzucony przez HA;
  nie jest dowodem behavioral RED i usunięto go przed poprawką produkcyjną.
- GREEN2: **8 passed**.
- RED3: `red3.log/xml`: **4 failed**, fixed rapid/mute/health/unknown wysyłane
  mimo fence zamiast odmowy. GREEN3: **23 passed** z authorization.
- Dodatkowe characterization/controls: późny i failed/stale readback,
  superseding OFF ze starej generacji, walidacja explicit fields, pełny fence,
  brak replay, quiet on/off, niezmienność katalogów.
- Final `full.log/xml`: **493 passed**, 265 ostrzeżeń tych samych dwóch
  typów deprecation (HA aiohttp subclass oraz istniejące datetime.utcnow).
- compileall, git diff --check: exit 0. `uv pip check --python .venv/bin/python`:
  110 pakietów zgodnych. Skan dodanych linii produkcyjnych: brak trafień.

Niezależny review rodzica dla dokładnego drzewa/commitu jest nadal wymagany
przed release/wdrożeniem. Nie oznaczać commitu jako independently verified.

## Kontrolowane wdrożenie — instrukcja, NIE wykonano

1. Uzyskać niezależny review exact SHA, osobną zgodę na produkcję i spełnić
   aktywny profilowy `runbooks/hon-fork-rollout.md` (okno lub jawny wyjątek,
   blokady writerów, aktualny capture intencji/poboru/stanu i wąski rollback).
   Publikacja ewentualnej 0.8.10 jest odrębną czynnością; ten commit to nie release.
2. Zachować containment OFF oraz istniejące cele/presety/tożsamości konta.
   Nie kasować pending, nie reloadować w celu zdjęcia fence. Instalacja kodu
   wymaga standardowego kontrolowanego cyklu uruchomienia, ale reset procesu
   nie jest dowodem usunięcia błędu ani testem unresolved-intent.
3. Przygotować niezmienny, przejrzany artefakt; zweryfikować źródła i SHA256,
   quiet oraz konta/encje; wykonać jedną kontrolowaną instalację według runbooka.
4. Próbę funkcjonalną zacząć na jednym AC przy bezpiecznym otoczeniu i osobnej
   zgodzie. Użyć zwykłego climate.turn_off; nie używać low-level hon.turn_off
   jako dowodu tej poprawki. Zapisać rzeczywisty connector POST stopProgram
   i payload power+opcode (echo=1 przy quiet), brak mutable/ancillary oraz
   zdalny resultCode="0". HTTP200 usługi nie wystarcza.
5. Zbierać wyłącznie odczyty kontekstu i HA przez do 90 s oraz niezależny pobór.
   Domyślny bezpośredni readback ma nadal budżet 4 s, a późniejsze zwykłe poll
   mogą potwierdzić OFF później. Nie replayować zaakceptowanego POST.
   Wymagać rzeczywistego power=0 i stanu HA OFF; przy pomiarze wspólnego obwodu
   uwzględnić inne odbiorniki. ACK bez OFF to NIE zaliczona próba.
6. Jeżeli pojawi się naturalny unresolved SET/START, wymagać działania zwykłego
   OFF mimo fence i późnej publikacji faktycznego OFF; nowa nie-neutralna intencja
   ma nadal zostać withheld aż do prawdziwego pełnego potwierdzenia. Nie tworzyć
   ryzykownego ogrzewania tylko po to, by wywołać błąd; bez takiej okazji ten
   warunek ma dowód offline, nie produkcyjny. Odmowa ma być widoczna jako błąd.
7. Jeżeli chmura odrzuci zredukowany stop payload albo urządzenie nie wyłączy się,
   przerwać próbę, zachować dowody i zastosować wcześniej zatwierdzony containment
   oraz rollback. Nie dodawać starego machMode/fan/target „żeby przeszło”.
   Dopiero po sukcesie i osobnej zgodzie rozszerzyć kontrolę na pozostałe AC;
   fasady przywrócić na końcu z aktualnych zachowanych intencji.

Ograniczenie: archiwalny katalog oznacza mutable pola jako mandatory.
Zredukowany draft jest wąską komendą stop z opcode tego katalogu, nie pełnym
settings; testy offline dowodzą autoryzacji, payloadu i publikacji odczytu,
**nie dowodzą przyjęcia tego wariantu przez prawdziwy cloud/firmware**.
Operacyjne containment przez publiczne hon.turn_off wysyłało inną komendę
(power0 + machMode1 przez async_set), więc nie waliduje tego payloadu.
