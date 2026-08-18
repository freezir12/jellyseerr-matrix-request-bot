# jellyseerr-matrix-request-bot

Filme/Serien in einem Matrix-Raum per Satz suchen und anfragen. Begleiter zu
[jellyseerr-matrix-bot](../jellyseerr-matrix-bot), das die andere Richtung
übernimmt (Jellyseerr-Webhook -> Matrix-Benachrichtigung). Dieser Bot hört
zusätzlich zu: `!request The Matrix` postet ein Ergebnis mit Poster und
Beschreibung, und du navigierst/fragst per Emoji-Reaktion an.

Vorbild ist [teleseerr](../teleseerr) (die Telegram-Version derselben Idee):
Die Suche geht direkt an die Jellyseerr-API (kein LLM), Ergebnisse werden
einzeln mit Poster und Beschreibung angezeigt, und man kann durchblättern. Da
Matrix keine Telegram-artigen Inline-Buttons hat, läuft die Navigation über
Reaktionen auf der Bot-Nachricht: **◀️** zurück, **➕** anfragen, **➡️** weiter.

## Warum ein eigener Bot statt Erweiterung von jellyseerr-matrix-bot?

jellyseerr-matrix-bot ist bewusst abhängigkeitsarm (nio + aiohttp +
prometheus, kein LLM) und rein ausgehend. Dieser Bot bringt eine zweite
Jellyseerr-API-Oberfläche (Suche + Anfrage, nicht nur Webhooks) und eingehende
Nachrichtenverarbeitung mit eigenen Fehlerfällen mit. Als eigener Prozess
bleiben Zuverlässigkeit und Kostenprofil des Notifiers unberührt, und jeder
Bot bleibt eine Datei, die man von vorne bis hinten lesen kann.

## Voraussetzungen

- Ein Matrix-Account **getrennt von** dem des Notifiers (eigenes Device,
  eigener Crypto-Store - siehe [jellyseerr-matrix-bot/docs/setup.md](../jellyseerr-matrix-bot/docs/setup.md)
  für die Token-Erzeugung, hier gleiches Vorgehen).
- Bot in den gleichen Raum wie die Benachrichtigungen einladen (oder einen
  anderen - eure Entscheidung), er tritt selbstständig bei.
- Ein Jellyseerr-API-Key (Einstellungen -> Allgemein).

`config.env` aus `config.env.example` befüllen - dort stehen alle Variablen
mit Default-Werten.

## Nutzung

Im Raum:

```
!request The Matrix
!request Stranger Things
!request Dune
```

Der Bot sucht in Jellyseerr und postet das erste Ergebnis mit Poster und
Beschreibung (Titel, Übersicht, Erscheinungsdatum, Typ, Anfrage-Status) plus
den Reaktionen **◀️ ➕ ➡️**:

```
Dune

A mythic and emotionally charged hero's journey...

release date: 2021-10-22

Film — Nicht angefragt

1/3 · Reagiere mit ◀️ / ➕ / ➡️ zum Navigieren oder Anfragen.
```

- **➡️** zeigt das nächste Ergebnis (lädt automatisch weitere Seiten nach,
  wenn du am Ende angekommen bist, wie die Pagination von teleseerr).
- **◀️** geht zum vorherigen Ergebnis zurück.
- **➕** fragt das aktuell angezeigte Element an (Serien fragen standardmäßig
  alle Staffeln an, genau wie teleseerr).

Die Such-Sitzung läuft von selbst ab (`REQUEST_SESSION_TIMEOUT_SECONDS`,
Default 10 Min.), damit eine alte Reaktion Tage später nicht den falschen
Titel anfragt.

## Deployment

```sh
docker compose up -d --build
```

Nutzt `uv` (siehe `pyproject.toml`/`uv.lock`). Nach
Abhängigkeitsänderungen `uv lock` ausführen und neu bauen.

## Contributing

Vor dem Push `python bot.py --selfcheck` laufen lassen (netzwerkfrei - deckt
die Parsing-/State-Machine-Logik ab, nicht die echten Jellyseerr-Calls).
Commits im Conventional-Stil (`feat: ...`, `fix: ...`), bitte auf Englisch.

MIT-Lizenz.

English version: [README.md](README.md)
