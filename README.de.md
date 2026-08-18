# jellyseerr-matrix-request-bot

Filme/Serien in einem Matrix-Raum per Satz suchen und anfragen, und
Jellyseerr-Webhook-Benachrichtigungen im selben Raum empfangen. Das ist ein
einziger Bot, der beide Richtungen vereint:

- **Request-Seite** (wie [teleseerr](../teleseerr)): `!request The Matrix`
  postet ein Ergebnis mit Poster und Beschreibung, und du navigierst/fragst
  per Emoji-Reaktion an.
- **Notifier-Seite** (wie der alte `jellyseerr-matrix-bot`): ein `/webhook`-
  Endpoint macht aus Jellyseerr-Benachrichtigungen Matrix-Nachrichten.

Vorbild ist [teleseerr](../teleseerr) (die Telegram-Version derselben Idee):
Die Suche geht direkt an die Jellyseerr-API (kein LLM), Ergebnisse werden
einzeln mit Poster und Beschreibung angezeigt, und man kann durchblättern. Da
Matrix keine Telegram-artigen Inline-Buttons hat, läuft die Navigation über
Reaktionen auf der Bot-Nachricht: **◀️** zurück, **➕** anfragen, **➡️** weiter.

## Warum ein Bot statt zwei?

Das alte Setup teilte das in `jellyseerr-matrix-bot` (Webhook-Notifier) und
diesen Request-Bot. Beide teilen denselben E2EE-Sendepfad, denselben Raum und
dasselbe Matrix-Account-Setup - als ein Prozess entfallen der doppelte
Crypto-Store, der zweite Container und die Webhook-Routing-Verwirrung (ein
Webhook, der an den falschen Bot ging). Die Notifier-Seite ist optional:
`WEBHOOK_SECRET` leer lassen und der Bot läuft als reiner Request-Bot.

## Voraussetzungen

- Ein Matrix-Account (eigenes Device, eigener Crypto-Store - siehe
  [jellyseerr-matrix-bot/docs/setup.md](../jellyseerr-matrix-bot/docs/setup.md)
  für die Token-Erzeugung, hier gleiches Vorgehen).
- Bot in den Raum einladen, er tritt selbstständig bei.
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

## Webhook-Benachrichtigungen (optional)

Um auch Jellyseerr-Benachrichtigungen im Raum zu empfangen, `WEBHOOK_SECRET`
in `config.env` setzen und den Jellyseerr-Webhook auf den Bot zeigen lassen:

- URL: `http://<host>:8082/webhook` (der in `docker-compose.yml` freigegebene Port)
- Authorization-Header: dein `WEBHOOK_SECRET`-Wert

Optional `USER_MAP` (Jellyseerr-Benutzername -> Matrix-ID, um den betroffenen
Nutzer zu pingen) und `ADMIN_IDS` (kommagetrennte Team-Matrix-IDs, gepingt bei
neuen Anfragen/Problemen) setzen. `WEBHOOK_SECRET` leer lassen, um als reiner
Request-Bot zu laufen.

## Contributing

Vor dem Push `python bot.py --selfcheck` laufen lassen (netzwerkfrei - deckt
die Parsing-/State-Machine-Logik ab, nicht die echten Jellyseerr-Calls).
Commits im Conventional-Stil (`feat: ...`, `fix: ...`), bitte auf Englisch.

MIT-Lizenz.

English version: [README.md](README.md)
