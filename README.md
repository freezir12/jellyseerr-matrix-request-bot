# jellyseerr-matrix-request-bot

Search and request movies/TV shows in a Matrix room by typing a sentence, and
get Jellyseerr webhook notifications in the same room. This is a single bot
that combines both directions:

- **Request side** (like [teleseerr](../teleseerr)): `!request The Matrix`
  posts a result with poster + caption, and you navigate/request with emoji
  reactions.
- **Notifier side** (like the old `jellyseerr-matrix-bot`): a `/webhook`
  endpoint turns Jellyseerr notifications into Matrix messages.

Modeled on [teleseerr](../teleseerr) (the Telegram version of this same idea):
search goes straight to the Jellyseerr API (no LLM), results are shown one at a
time with a poster and caption, and you can page through them. Since Matrix has
no Telegram-style inline buttons, navigation uses reactions on the bot's own
message: **◀️** previous, **➕** request, **➡️** next.

## Why one bot instead of two?

The old setup split this into `jellyseerr-matrix-bot` (webhook notifier) and
this request bot. Both share the same E2EE send path, the same room and the
same Matrix account setup, so running them as one process removes the duplicate
crypto store, the second container and the webhook-routing confusion (a webhook
sent to the wrong bot). The notifier side is optional: leave `WEBHOOK_SECRET`
empty and the bot runs as a pure request bot.

## Requirements

- A Matrix account (own device, own crypto store - see
  [jellyseerr-matrix-bot/docs/setup.md](../jellyseerr-matrix-bot/docs/setup.md)
  for the token-generation steps, same procedure here).
- Invite the bot into the room, it joins on its own.
- A Jellyseerr API key (Settings -> General).

Fill in `config.env` from `config.env.example` - see that file for every
variable and its default.

## Usage

In the room:

```
!request The Matrix
!request Stranger Things
!request Dune
```

The bot searches Jellyseerr and posts the first result with its poster and a
caption (title, overview, release date, type, request status), plus the
reactions **◀️ ➕ ➡️**:

```
Dune

A mythic and emotionally charged hero's journey...

release date: 2021-10-22

Movie — Not requested

1/3 · React with ◀️ / ➕ / ➡️ to navigate or request.
```

- **➡️** shows the next result (loading more pages automatically when you reach
  the end, like teleseerr's pagination).
- **◀️** goes back to the previous result.
- **➕** requests the currently shown item (TV shows request all seasons by
  default, exactly like teleseerr).

The search session expires on its own (`REQUEST_SESSION_TIMEOUT_SECONDS`,
default 10 min) so a stale reaction days later can't fire against the wrong
title.

## Deployment

```sh
docker compose up -d --build
```

Uses `uv` (see `pyproject.toml`/`uv.lock`). Run `uv lock` after changing
dependencies, then rebuild.

## Webhook notifications (optional)

To also receive Jellyseerr notifications in the room, set `WEBHOOK_SECRET` in
`config.env` and point Jellyseerr's webhook at the bot:

- URL: `http://<host>:8082/webhook` (the port exposed in `docker-compose.yml`)
- Authorization header: your `WEBHOOK_SECRET` value

Optionally set `USER_MAP` (Jellyseerr username -> Matrix ID, to ping the
affected user) and `ADMIN_IDS` (comma-separated team Matrix IDs, pinged on new
requests/issues). Leave `WEBHOOK_SECRET` empty to run as a pure request bot.

## Contributing

Run `python bot.py --selfcheck` before pushing (no network required - it
covers the parsing/state-machine logic, not the live Jellyseerr calls).
Keep commits in conventional style (`feat: ...`, `fix: ...`), English please.

MIT licensed.

Deutsche Version: [README.de.md](README.de.md)
