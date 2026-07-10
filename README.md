# TukkerScout 3.0

Lokale FC Twente-nieuwsmonitor voor TukkerTribune.

## Eerste installatie

1. Download of clone deze repository.
2. Dubbelklik op `installeren.bat`.
3. Dubbelklik op `start_tukkerscout.bat`.
4. Open `http://127.0.0.1:8765`.

## Pagina's

- Nieuws
- Personen
- Bronnen

## Personen

De eerste keer kun je de volledige spelersselectie plakken. Daarna gebruik je **Kleine wijziging** of **Selectie bijwerken**.

## Lokale bestanden

Deze worden niet naar GitHub gestuurd:

- `.venv/`
- `data/`
- `logs/`
- `.env`

## Updates

Na een echte Git-clone kun je `update_via_github.bat` gebruiken.


## Versie 3.1

- Geen automatische controles meer.
- Gebruik de knop **Update nieuws**.
- TukkerScout onthoudt alle gevonden artikelen lokaal.
- Bovenaan staan alleen berichten die bij de laatste handmatige update nieuw waren.
- Eerdere berichten blijven onder **Onthouden berichten** zichtbaar.
- Optionele X-integratie via de officiële X API.
- De X Bearer Token wordt uitsluitend lokaal opgeslagen in `data/x_config.json`.
