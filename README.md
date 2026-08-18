# jellyseerr-matrix-request-bot

Search and request movies/TV shows in a Matrix room by typing a sentence.
Companion to [jellyseerr-matrix-bot](../jellyseerr-matrix-bot), which handles
the other direction (Jellyseerr webhook -> Matrix notification). This one
listens instead of only sending: `!request The Matrix` posts a result with
poster + caption, and you navigate/request with emoji reactions.

Modeled on [teleseerr](../teleseerr) (the Telegram version of this same idea):
search goes straight to the Jellyseerr API (no LLM), results are shown one at a
time with a poster and caption, and you can page through them. Since Matrix has
no Telegram-style inline buttons, navigation uses reactions on the bot's own
message: **◀️** previous, **➕** request, **➡️** next.

## Why a separate bot from jellyseerr-matrix-bot?

jellyseerr-matrix-bot is deliberately dependency-light (nio + aiohttp +
prometheus, no LLM) and outbound-only. This bot adds a second Jellyseerr API
surface (search + request, not just webhooks) and inbound message handling with
its own failure modes. Keeping it a separate process means the notifier's
reliability and cost profile are unaffected, and each bot stays a single file
you can read start to finish.

## Requirements

- A Matrix account **distinct from** jellyseerr-matrix-bot's (own device, own
  crypto store - see [jellyseerr-matrix-bot/docs/setup.md](../jellyseerr-matrix-bot/docs/setup.md)
  for the token-generation steps, same procedure here).
- Invite the bot into the same room as the notifications (or a different one -
  your call), it joins on its own.
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

## Contributing

Run `python bot.py --selfcheck` before pushing (no network required - it
covers the parsing/state-machine logic, not the live Jellyseerr calls).
Keep commits in conventional style (`feat: ...`, `fix: ...`), English please.

MIT licensed.

Deutsche Version: [README.de.md](README.de.md)
