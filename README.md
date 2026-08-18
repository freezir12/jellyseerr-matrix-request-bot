# jellyseerr-matrix-request-bot

Search and request movies/TV shows in a Matrix room by typing a sentence.
Companion to [jellyseerr-matrix-bot](../jellyseerr-matrix-bot), which handles
the other direction (Jellyseerr webhook -> Matrix notification). This one
listens instead of only sending: `!request The Matrix` gets you a result and,
if it's not available yet, a plain-text confirmation ("reply 1" / "reply
yes") to fire the actual request - Matrix has no Telegram-style inline
buttons to build a real one on.

Ported from [teleseerr-py](../teleseerr-py) (the Telegram version of this same
idea): a LangGraph/OpenAI agent parses the free-text request (title, year,
season numbers), searches Jellyseerr, and reports status or offers to
request.

## Why a separate bot from jellyseerr-matrix-bot?

jellyseerr-matrix-bot is deliberately dependency-light (nio + aiohttp +
prometheus, no LLM) and outbound-only. This bot adds an LLM agent, a second
Jellyseerr API surface (search + request, not just webhooks), and inbound
message handling with its own failure modes (OpenAI down/rate-limited).
Keeping it a separate process means the notifier's reliability and cost
profile are unaffected, and each bot stays a single file you can read start to
finish.

## Requirements

- A Matrix account **distinct from** jellyseerr-matrix-bot's (own device, own
  crypto store - see [jellyseerr-matrix-bot/docs/setup.md](../jellyseerr-matrix-bot/docs/setup.md)
  for the token-generation steps, same procedure here).
- Invite the bot into the same room as the notifications (or a different one -
  your call), it joins on its own.
- A Jellyseerr API key (Settings -> General).
- An OpenAI API key.

Fill in `config.env` from `config.env.example` - see that file for every
variable and its default.

## Usage

In the room:

```
!request The Matrix
!request Stranger Things season 4
!request The Office s02 and s05
!request Dune 2021
```

The bot searches Jellyseerr, reports availability/request status, and - if
the item isn't requested yet - posts a numbered list and asks for
confirmation:

```
Found:
1. Dune (2021) — Not requested
Reply with the number to request it, or 'no' to cancel. (Expires in 5 min.)
```

Reply `1` (or `yes`, if there's only one candidate) to request it, `no` /
`cancel` to back out. The confirmation expires on its own
(`REQUEST_CONFIRM_TIMEOUT_SECONDS`, default 5 min) so a stale "yes" days later
can't fire against the wrong title. State is tracked per person, not per room,
so several people can search at the same time without cross-talk.

## Deployment

```sh
docker compose up -d --build
```

Uses `uv` (see `pyproject.toml`/`uv.lock`), same as teleseerr-py. Run
`uv lock` after changing dependencies, then rebuild.

## Contributing

Run `python bot.py --selfcheck` before pushing (no network required - it
covers the parsing/state-machine logic, not the live Jellyseerr/OpenAI calls).
Keep commits in conventional style (`feat: ...`, `fix: ...`), English please.

MIT licensed.

Deutsche Version: [README.de.md](README.de.md)
