# jellyseerr-matrix-request-bot

Filme/Serien in einem Matrix-Raum per Satz suchen und anfragen. Begleiter zu
[jellyseerr-matrix-bot](../jellyseerr-matrix-bot), das die andere Richtung
übernimmt (Jellyseerr-Webhook -> Matrix-Benachrichtigung). Dieser Bot hört
zusätzlich zu: `!request The Matrix` liefert ein Ergebnis, und falls der Titel
noch nicht verfügbar ist, eine Text-Bestätigung ("antworte mit 1" / "antworte
mit ja"), um die eigentliche Anfrage auszulösen - Matrix hat keine
Telegram-artigen Inline-Buttons, auf denen man das sonst bauen könnte.

Portiert aus [teleseerr-py](../teleseerr-py) (die Telegram-Version derselben
Idee): ein LangGraph/OpenAI-Agent parst die Freitext-Anfrage (Titel, Jahr,
Staffelnummern), sucht in Jellyseerr und meldet den Status oder bietet die
Anfrage an.

## Warum ein eigener Bot statt Erweiterung von jellyseerr-matrix-bot?

jellyseerr-matrix-bot ist bewusst abhängigkeitsarm (nio + aiohttp +
prometheus, kein LLM) und rein ausgehend. Dieser Bot bringt einen LLM-Agenten,
eine zweite Jellyseerr-API-Oberfläche (Suche + Anfrage, nicht nur Webhooks)
und eingehende Nachrichtenverarbeitung mit eigenen Fehlerfällen (OpenAI down/
rate-limited) mit. Als eigener Prozess bleiben Zuverlässigkeit und
Kostenprofil des Notifiers unberührt, und jeder Bot bleibt eine Datei, die man
von vorne bis hinten lesen kann.

## Voraussetzungen

- Ein Matrix-Account **getrennt von** dem des Notifiers (eigenes Device,
  eigener Crypto-Store - siehe [jellyseerr-matrix-bot/docs/setup.md](../jellyseerr-matrix-bot/docs/setup.md)
  für die Token-Erzeugung, hier gleiches Vorgehen).
- Bot in den gleichen Raum wie die Benachrichtigungen einladen (oder einen
  anderen - eure Entscheidung), er tritt selbstständig bei.
- Ein Jellyseerr-API-Key (Einstellungen -> Allgemein).
- Ein OpenAI-API-Key.

`config.env` aus `config.env.example` befüllen - dort stehen alle Variablen
mit Default-Werten.

## Nutzung

Im Raum:

```
!request The Matrix
!request Stranger Things season 4
!request The Office s02 and s05
!request Dune 2021
```

Der Bot sucht in Jellyseerr, meldet Verfügbarkeit/Anfrage-Status, und postet -
falls der Titel noch nicht angefragt ist - eine nummerierte Liste mit
Bestätigungsaufforderung:

```
Gefunden:
1. Dune (2021) — Nicht angefragt
Antworte mit der Nummer, um anzufragen, oder mit 'nein' zum Abbrechen. (Läuft in 5 Min. ab.)
```

Mit `1` antworten (oder `ja`, wenn nur ein Treffer da ist), um anzufragen,
`nein`/`abbrechen` zum Absagen. Die Bestätigung läuft von selbst ab
(`REQUEST_CONFIRM_TIMEOUT_SECONDS`, Default 5 Min.), damit ein "ja" Tage
später nicht den falschen Titel anfragt. Der Zustand wird pro Person
gehalten, nicht pro Raum - mehrere Leute können gleichzeitig suchen, ohne
sich zu stören.

## Deployment

```sh
docker compose up -d --build
```

Nutzt `uv` (siehe `pyproject.toml`/`uv.lock`), wie teleseerr-py. Nach
Abhängigkeitsänderungen `uv lock` ausführen und neu bauen.

## Contributing

Vor dem Push `python bot.py --selfcheck` laufen lassen (netzwerkfrei - deckt
die Parsing-/State-Machine-Logik ab, nicht die echten Jellyseerr-/OpenAI-
Calls). Commits im Conventional-Stil (`feat: ...`, `fix: ...`), bitte auf
Englisch.

MIT-Lizenz.

English version: [README.md](README.md)
