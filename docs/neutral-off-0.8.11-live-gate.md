# Kandydat 0.8.11 — neutralne OFF i live gate

## Zakres i dowody offline

0.8.10 nie przeszło live gate: ACK lean `stopProgram` nie wyłączył urządzenia.
Ta poprawka nie szuka kolejnego minimalnego `stopProgram`. Zwykły, izolowany
AC OFF wybiera istniejące `HonConnection.async_set(mac, 'AC',
{'onOffStatus': '0', 'machMode': '1'})`, czyli transport używany przez
`hon.turn_off`. Koperta to `startProgram / PROGRAMS.AC.HOME_ASSISTANT`,
łącznie z dotychczasowymi attributes/ancillary/device i quiet helperem.

Wybór zachodzi **wewnątrz normalnego HonCommand.send()**, po wejściu w
transakcję; connector nadal posiada osobny lock POST. GET jest po zwolnieniu
locka POST, w dotychczasowym ograniczonym czasowo readback. Marker transportu
nie jest parametrem serwisu. Predykat neutralności i odmowa dla dodatkowych
pól pozostają ścisłe, również po mutacji draftu przed wysłaniem.

`machMode=1` jest stałym wymogiem konkretnej koperty generic OFF, **nie**
neutralną intencją zmiany trybu. Nie trafia do nowej intencji ani oczekiwań
GET. Aktualizujemy oczekiwaną moc do 0, zachowując nierozwiązane nonpower
z poprzedniego polecenia. Nie zmieniamy optymistycznie raportowanej telemetrii.
Explicit stop+settings nadal używa pełnej, fenced ścieżki `stopProgram`.

Fixture `tests/fixtures/generic_ac_off_request.json` jest reprezentatywne,
nie jest przechwyceniem produkcyjnym. Testy porównują całą wychodzącą kopertę
(z osobną walidacją timestamp/transactionId) ze znanym transportem generic
przez prawdziwy coordinator.async_set. Obie zwykłe usługi HA (`climate.turn_off`
i `climate.set_hvac_mode: off`) przechodzą prawdziwy dispatcher, HonCommand,
connector i GET; tylko serwer HTTP jest zastąpiony offline. To **nie dowodzi
wyłączenia sprzętu**.

## Live gate — tylko po osobnej autoryzacji

1. Najpierw niezależny review dokładnego drzewa/commita; brak push/release i
   brak automatycznej instalacji w ramach prac lokalnych. Zanotuj dokładny SHA,
   wersję, snapshot konfiguracji i aktualne zamiary pokoi. Ustal operatora,
   okno, plan odtworzenia i działający rollback 0.8.9. Salon pozostaje OFF
   do wyraźnej zgody na próbę; nie uruchamiaj innych pokoi przy okazji.
2. Osobno przygotuj potwierdzony HEAT, a następnie FAN_ONLY. W próbie FAN
   użyj prędkości zgodnej ze schematem (AUTO nie zawsze jest dozwolone).
   Przed każdym OFF zapisz aktualny, świeży kontekst oraz rzeczywisty stan AC.
3. Dla obu stanów źródłowych wykonaj osobno zwykłe `climate.turn_off` oraz
   `climate.set_hvac_mode` z `hvac_mode: off` (osobne uruchomienie i potwierdzenie
   trybu przed każdą próbą). Nie zastępuj ich fallbackiem `hon.turn_off`.
   Zapisz usługę/target/czas, rzeczywisty POST i ACK oraz następujące GET.
4. Wymagany outbound: jeden `startProgram`, program
   `PROGRAMS.AC.HOME_ASSISTANT`, parametry wyłącznie onOffStatus=0,
   machMode=1 oraz echoStatus=1 przy quiet. Brak starej temperatury, fan,
   louvre i presetów. Nie uznawaj HTTP200 ani ACK za OFF.
5. Wymagaj onOffStatus=0 ze świeżego kontekstu, natywnego climate=off,
   facade=off i niezależnego potwierdzenia urządzenia. Zapisuj oś czasu,
   nie tylko wynik usługi. Brak potwierdzenia w uzgodnionym oknie oznacza FAIL;
   nie ponawiaj automatycznie zaakceptowanego POST. Rozróżniaj krótki budżet
   bezpośredniego readback i późniejsze monitorowanie propagacji chmury.
6. Osobno sprawdź ścieżkę po nierozwiązanym nonpower intent: OFF nadal możliwe,
   pending nonpower zachowane, explicit stop+settings i zwykłe SETTINGS/start
   z błędnej podstawy nadal odmówione. Nie wywołuj celowo awarii sieci ani
   nie modyfikuj pending na produkcji bez osobnego zatwierdzenia; jeżeli tego
   scenariusza nie ma bezpiecznie, oznacz go jako zweryfikowany wyłącznie offline.
7. Przy FAIL najpierw zachowaj kompletny ślad nieudanej zwykłej ścieżki.
   Jeżeli potrzebne containment, zastosuj wyłącznie uprzednio zatwierdzony
   `hon.turn_off`, oznaczając go oddzielnie; OFF po fallbacku nie zalicza gate.
   Wróć do uzgodnionego rollbacku i potwierdź świeżym GET oraz sprzętem.
8. Po próbach przywróć zatwierdzone zamiary, Salon pozostaw OFF, jeżeli nie
   ustalono inaczej. Dowody testu i odtworzenia zapisuj osobno, append-only.
   Publikacja/wdrożenie wymaga osobnej decyzji nawet przy PASS.
