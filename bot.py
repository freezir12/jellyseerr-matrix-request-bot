#!/usr/bin/env python3
"""Interactive Jellyseerr request bot for Matrix. Companion to jellyseerr-matrix-bot
(the webhook notifier) - this one listens in the same E2EE room and lets users
search and request media via `!request <title>` plus a short text confirmation
(Matrix has no Telegram-style inline buttons to build a real one on)."""
import asyncio
import html
import io
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from uuid import uuid4

import aiohttp
from aiohttp import web
from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from nio import (
    Api,
    AsyncClient,
    InviteMemberEvent,
    MatrixRoom,
    RoomMessageText,
    RoomSendResponse,
    SyncResponse,
    UploadResponse,
)
from nio.exceptions import LocalProtocolError
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, generate_latest
from pydantic import BaseModel, Field, SecretStr

log = logging.getLogger("jellyseerr-matrix-request-bot")

ERRORS = Counter("bot_errors_total", "Errors (log.error/log.exception) anywhere in the bot")
REQUESTS = Counter("bot_requests_total", "!request commands handled", ["outcome"])
CONFIRMATIONS = Counter("bot_confirmations_total", "Confirmation replies handled", ["outcome"])
LAST_SYNC = Gauge("bot_last_sync_timestamp", "Unix time of the last /sync from the homeserver")

IMAGE_TMDB_URL = "https://image.tmdb.org/t/p/w600_and_h900_bestv2"
POSTER_MAX_BYTES = 10 * 1024 * 1024  # sanity limit; posters are ~200 KB

JELLYSEERR_API_URL = os.getenv("JELLYSEERR_API_URL", "").rstrip("/")
JELLYSEERR_API_KEY = os.getenv("JELLYSEERR_API_KEY", "")


class ErrorCounter(logging.Handler):
    """Counts every log.error/log.exception - attached to the root logger so nio
    errors count too, without touching individual code paths."""

    def emit(self, record):
        ERRORS.inc()


# ponytail: two languages = two dicts; switch to a locale framework at 3+.
# Mirrors jellyseerr-matrix-bot/bot.py's STRINGS/set_lang pattern.
STRINGS = {
    "en": {
        "usage": "Usage: !request <title> [year] [season]",
        "candidates_header": "Found:",
        "confirm_hint": "Reply with the number to request it, or 'no' to cancel. (Expires in {minutes} min.)",
        "ambiguous": "Reply 1 or 2?",
        "cancelled": "Cancelled.",
        "error": "Sorry, something went wrong processing your request.",
        "status": {"Available": "Available", "Requested": "Requested", "Not Requested": "Not requested"},
        "seasons": "Seasons",
    },
    "de": {
        "usage": "Nutzung: !request <Titel> [Jahr] [Staffel]",
        "candidates_header": "Gefunden:",
        "confirm_hint": "Antworte mit der Nummer, um anzufragen, oder mit 'nein' zum Abbrechen. (Läuft in {minutes} Min. ab.)",
        "ambiguous": "Antworte mit 1 oder 2?",
        "cancelled": "Abgebrochen.",
        "error": "Entschuldigung, bei der Verarbeitung ist ein Fehler aufgetreten.",
        "status": {"Available": "Verfügbar", "Requested": "Angefragt", "Not Requested": "Nicht angefragt"},
        "seasons": "Staffeln",
    },
}

S = STRINGS["en"]


def set_lang(code: str):
    global S
    if code not in STRINGS:
        log.warning("Unknown BOT_LANG %r, falling back to en", code)
        code = "en"
    S = STRINGS[code]


YES_WORDS = {"ja", "j", "yes", "y"}
NO_WORDS = {"nein", "n", "no", "cancel", "abbrechen", "stop"}


# --- Matrix E2EE send path -------------------------------------------------
# Ported verbatim from jellyseerr-matrix-bot/bot.py (the webhook notifier).
# Any fix to the key-sharing timing or the cleartext-mentions trick belongs in
# BOTH files - this bot only sends, it never needed its own copy of this logic
# to diverge, so we keep it byte-for-byte identical to the sibling project.


def encrypt_with_cleartext_mentions(encrypt_fn, room_id, inner, mentions):
    """Like nio does for m.relates_to: put m.mentions in the CLEARTEXT of the
    m.room.encrypted envelope too, so Synapse can evaluate .m.rule.is_user_mention
    and push despite a room mute."""
    _type, enc = encrypt_fn(room_id, "m.room.message", inner)
    if mentions:
        enc["m.mentions"] = {"user_ids": mentions}
    return enc


def image_content(uri, keys, size, mimetype, body, formatted, mentions):
    """m.image with caption (MSC2530): body/formatted_body carry the text card,
    filename holds the real file name -> clients render body as the caption."""
    return {
        "msgtype": "m.image",
        "body": body,
        "filename": "poster.jpg",
        "format": "org.matrix.custom.html",
        "formatted_body": formatted,
        "info": {"mimetype": mimetype, "size": size},
        "file": {
            "url": uri,
            "key": keys["key"],
            "iv": keys["iv"],
            "hashes": keys["hashes"],
            "v": keys["v"],
        },
        "m.mentions": {"user_ids": mentions} if mentions else {},
    }


def text_content(body, formatted, mentions):
    return {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": formatted,
        "m.mentions": {"user_ids": mentions} if mentions else {},
    }


async def upload_poster(client: AsyncClient, url: str):
    """Fetch the poster + upload it E2EE-encrypted to the media repo.
    -> (mxc, keys, size, mimetype). Raises on any failure; the caller catches
    and falls back to text."""
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            mimetype = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
            data = await r.read()
    if not data or len(data) > POSTER_MAX_BYTES:
        raise ValueError(f"Poster {len(data)} B outside the limit")
    resp, keys = await client.upload(
        io.BytesIO(data), content_type=mimetype, filename="poster.jpg",
        filesize=len(data), encrypt=True,
    )
    if not isinstance(resp, UploadResponse):
        raise RuntimeError(f"Upload failed: {resp}")
    return resp.content_uri, keys, len(data), mimetype


async def send(
    client: AsyncClient, room_id: str, body: str, formatted: str,
    mentions: list[str], poster_url: str | None = None,
):
    if client.should_query_keys:
        await client.keys_query()
    try:
        await client.share_group_session(room_id, ignore_unverified_devices=True)
        await asyncio.sleep(6)
    except LocalProtocolError:
        pass

    room = client.rooms.get(room_id)
    if room and not room.members_synced:
        await client.joined_members(room_id)

    inner = None
    if poster_url:
        try:
            uri, keys, size, mimetype = await upload_poster(client, poster_url)
            inner = image_content(uri, keys, size, mimetype, body, formatted, mentions)
        except Exception:
            log.exception("Poster upload failed, sending text only")
    if inner is None:
        inner = text_content(body, formatted, mentions)
    enc = encrypt_with_cleartext_mentions(client.encrypt, room_id, inner, mentions)
    method, path, data = Api.room_send(
        client.access_token, room_id, "m.room.encrypted", enc, uuid4()
    )
    resp = await client._send(RoomSendResponse, method, path, data, (room_id,))
    if not isinstance(resp, RoomSendResponse):
        log.error("Matrix send failed: %s", resp)


# --- Jellyseerr API ---------------------------------------------------------
# Ported from teleseerr-py/telegram_bot.py's search_overseerr/request_overseerr,
# rewritten as async aiohttp calls: this process also runs client.sync_forever()
# on the same event loop, so a blocking httpx/requests call here would freeze
# every other user's messages and the Matrix sync itself for the round trip.


@tool
async def search_jellyseerr(query: str, media_type: str) -> str:
    """Searches Jellyseerr for movies or TV shows and checks their request status.

    Args:
        query: The search query (e.g., movie or TV show title).
        media_type: The type of media to search for ('movie' or 'tv').

    Returns:
        A JSON-encoded list of up to 2 results, or an empty JSON list if nothing
        is found or an error occurs. Each result includes title, year, media_id,
        media_type, overview, status ('Available', 'Requested', 'Not Requested'),
        and poster_url.
    """
    log.info("Searching Jellyseerr for query=%r media_type=%r", query, media_type)
    url = f"{JELLYSEERR_API_URL}/search"
    headers = {"X-Api-Key": JELLYSEERR_API_KEY, "Content-Type": "application/json"}
    params = {"query": query}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status >= 400:
                text = await resp.text()
                log.error("Jellyseerr search failed: %s - %s", resp.status, text)
                return json.dumps([])
            data = await resp.json()

    results = data.get("results", [])
    if media_type:
        results = [r for r in results if r.get("mediaType") == media_type]

    processed = []
    for r in results[:2]:
        media_info = r.get("mediaInfo")
        status = "Not Requested"
        if media_info:
            # Status codes: 1: Unknown, 2: Pending, 3: Processing, 4: Partially Available, 5: Available
            media_status = media_info.get("status")
            media_status_4k = media_info.get("status4k")
            if media_status == 5 or media_status_4k == 5:
                status = "Available"
            elif media_status in (2, 3, 4) or media_status_4k in (2, 3, 4):
                status = "Requested"

        poster_path = r.get("posterPath")
        poster_url = f"{IMAGE_TMDB_URL}{poster_path}" if poster_path else None

        processed.append(
            {
                "title": r.get("title") or r.get("name"),
                "year": (r.get("releaseDate") or "")[:4],
                "media_id": r.get("id"),
                "media_type": r.get("mediaType"),
                "overview": r.get("overview"),
                "status": status,
                "poster_url": poster_url,
            }
        )
    return json.dumps(processed)


async def request_jellyseerr(media_id: int, media_type: str, seasons: list[int] | None = None) -> str:
    """Sends a request to Jellyseerr to add a movie or TV show, optionally
    specifying seasons for TV shows. Not an agent tool - called directly once
    the user confirms, same as teleseerr's request_overseerr."""
    url = f"{JELLYSEERR_API_URL}/request"
    headers = {"X-Api-Key": JELLYSEERR_API_KEY, "Content-Type": "application/json"}
    data: dict[str, int | str | list[int]] = {"mediaId": media_id, "mediaType": media_type}
    if media_type == "tv" and seasons:
        data["seasons"] = seasons
        log.info("Requesting specific seasons for TV show %s: %s", media_id, seasons)

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as resp:
            if resp.status < 400:
                season_text = f" (Seasons: {', '.join(map(str, seasons))})" if seasons else ""
                return f"Successfully requested {media_type} with ID {media_id}{season_text}."
            error_message = f"Status: {resp.status}"
            try:
                body = await resp.json()
                if body.get("message"):
                    error_message = body["message"]
            except Exception:
                pass
            log.error("Jellyseerr request failed: %s - %s", resp.status, error_message)
            return f"Failed to request {media_type} with ID {media_id}. {error_message}"


# --- LLM agent ---------------------------------------------------------------
# Ported from teleseerr-py/telegram_bot.py's OverseerrResponse/system_prompt/
# agent wiring, renamed for Jellyseerr branding, plus an explicit
# reply-in-the-user's-language instruction (teleseerr's original prompt never
# needed one; a bilingual Matrix room does).

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-nano")


class JellyseerrResponse(BaseModel):
    """Structured response from the Jellyseerr assistant."""

    answer: str = Field(description="The natural language response to the user.")
    action: str | None = Field(
        None, description="Set to 'offer_request' if the user should be prompted to request the item."
    )
    media_id: int | None = Field(
        None, description="The media ID to be requested, only if action is 'offer_request'."
    )
    media_type: str | None = Field(
        None, description="The media type ('movie' or 'tv') to be requested, only if action is 'offer_request'."
    )
    seasons: list[int] | None = Field(
        None,
        description="List of specific season numbers requested by the user (e.g., [1, 3, 5]). "
        "Only applicable if media_type is 'tv'. Null or empty if all seasons or it's a movie.",
    )
    poster_url: str | None = Field(
        None, description="The URL of the poster image for the media item, if available."
    )


system_prompt = (
    "You are a helpful assistant interacting with the Jellyseerr API. "
    "Your primary function is searching for media (movies or TV shows) using the search_jellyseerr tool. "
    "Analyze the user's request for media titles, potentially a specific release year, and specific season numbers (e.g., 'show title 2023', 'movie title s03', 'tv show ss5', 'show season 5 and 6'). Extract the title, year (if mentioned), and season numbers (if mentioned for TV shows, recognizing sX, ssX, season X patterns). "
    "Use the search_jellyseerr tool with the extracted title and media type ('movie' or 'tv'). "
    "Based on the search results from the tool: "
    "If the first search returns no results: Try calling `search_jellyseerr` exactly one more time. For this second attempt, use a simplified query, focusing only on the core title and removing any year or season specifiers identified in the user's original request. If this second search also returns no results, inform the user that you couldn't find anything matching their query. Set action, media_id, media_type, seasons, and poster_url to null in the response."
    "If results are found (either on the first or second attempt):"
    "   - If the user specified a year in their original request, try to find a result matching that year among the results found. If a match is found, prioritize that result. If no exact year match is found among the results, mention this and proceed with the top result overall."
    "   - Focus on the selected result (year-matched or top result)."
    "   - If the selected result is 'Available' or 'Requested', inform the user of its status (e.g., 'The movie Title (Year) is already available/requested.'). Include the year, an overview, and the poster_url in the response if available. Do not ask to request it again. Set action, media_id, media_type, and seasons to null in the response."
    "   - If the selected result is 'Not Requested':"
    "       - Clearly state the title, year, status, and provide an overview."
    "       - If it's a TV show and the user specified season numbers (e.g., 's5', 'ss3', 'season 3') in their original request, identify these numbers. Ask the user if they want to request *those specific seasons* (e.g., '... Would you like to request season 3 and 5?'). Populate the `seasons` field in the response schema with the identified season numbers (as integers). "
    "       - If it's a movie, or a TV show where the user did *not* specify seasons, ask if they want to request the item (e.g., '... Would you like to request this movie/show?'). Leave the `seasons` field null or empty."
    "       - Crucially, set action='offer_request', and populate media_id, media_type, and poster_url (if available) with the correct values from the search result in the response schema."
    "If multiple results were returned by the tool, mention that you found multiple results and are presenting the most relevant one (either the year-matched one or the top one), then proceed with the logic described above for that result."
    "Do not make up information. Only use the provided search_jellyseerr tool and its results."
    "Always answer in the same language the user wrote their request in."
    "Always structure your final response using the JellyseerrResponse schema."
)

_agent_executor = None


def get_agent_executor():
    """Built lazily, not at import time: constructing ChatOpenAI requires a
    non-empty API key in current langchain_openai/openai versions, which would
    make --selfcheck (network-free, no credentials needed) fail on import."""
    global _agent_executor
    if _agent_executor is None:
        llm = ChatOpenAI(model=OPENAI_MODEL, temperature=0, api_key=SecretStr(OPENAI_API_KEY))
        _agent_executor = create_react_agent(
            llm,
            [search_jellyseerr],
            prompt=SystemMessage(system_prompt),
            response_format=JellyseerrResponse,
        )
    return _agent_executor


def extract_candidates(messages: list, structured: JellyseerrResponse | None) -> list[dict]:
    """Recovers the up-to-2 candidates the agent saw from the search_jellyseerr
    ToolMessage, with the offered one (if any) moved to index 0 and its
    requested seasons (if any) attached. Falls back to a synthetic single-entry
    list built from the structured response if the tool output can't be matched
    (should not normally happen, but the agent's structured fields are the
    source of truth for what gets requested)."""
    candidates: list[dict] = []
    for m in messages:
        if isinstance(m, ToolMessage) and getattr(m, "name", None) == "search_jellyseerr":
            try:
                candidates = json.loads(m.content)
            except (json.JSONDecodeError, TypeError):
                candidates = []

    if not structured or structured.media_id is None:
        return candidates

    def matches(c):
        return c.get("media_id") == structured.media_id and c.get("media_type") == structured.media_type

    offered = next((c for c in candidates if matches(c)), None)
    if offered is None:
        offered = {
            "title": None,
            "year": None,
            "media_id": structured.media_id,
            "media_type": structured.media_type,
            "overview": None,
            "status": "Not Requested",
            "poster_url": structured.poster_url,
        }
        rest = candidates
    else:
        rest = [c for c in candidates if c is not offered]

    offered = dict(offered)
    if structured.seasons:
        offered["seasons"] = structured.seasons
    return [offered] + rest


@dataclass
class AgentResult:
    answer: str
    candidates: list[dict]
    offered: dict | None
    poster_url: str | None


async def run_agent(query: str) -> AgentResult:
    response = await get_agent_executor().ainvoke({"messages": [("user", query)]})
    messages = response.get("messages", [])
    structured: JellyseerrResponse | None = response.get("structured_response")
    answer = messages[-1].content if messages else ""
    candidates = extract_candidates(messages, structured)
    offered = None
    if structured and structured.action == "offer_request" and candidates:
        offered = candidates[0]
    poster_url = structured.poster_url if structured else None
    return AgentResult(answer=answer, candidates=candidates, offered=offered, poster_url=poster_url)


# --- Message parsing (pure, covered by --selfcheck) -------------------------


def parse_command(text: str, prefix: str = "!request") -> str | None:
    """-> the query text (possibly empty) if `text` is a trigger command, else None."""
    pattern = re.compile(rf"^{re.escape(prefix)}(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
    m = pattern.match(text.strip())
    if not m:
        return None
    return (m.group(1) or "").strip()


def classify_reply(text: str, num_candidates: int) -> tuple[str, int | None]:
    """-> (kind, index). kind is one of 'confirm' (index is the 1-based
    candidate chosen), 'ambiguous' (multiple candidates, plain 'yes'),
    'cancel', or 'ignore' (not a recognized reply - leave state untouched)."""
    t = text.strip().lower()
    if t in NO_WORDS:
        return "cancel", None
    if t.isdigit():
        idx = int(t)
        if 1 <= idx <= num_candidates:
            return "confirm", idx
        return "ignore", None
    if t in YES_WORDS:
        if num_candidates == 1:
            return "confirm", 1
        return "ambiguous", None
    return "ignore", None


def should_handle_event(sender: str, server_timestamp_ms: float, own_user_id: str, startup_ts_ms: float) -> bool:
    """False for our own messages (echo) and for anything from before this
    process started (the initial full-state sync must not re-trigger a real
    Jellyseerr request on restart)."""
    if sender == own_user_id:
        return False
    if server_timestamp_ms < startup_ts_ms:
        return False
    return True


def render_candidates_text(
    candidates: list[dict], strings: dict, timeout_seconds: int, jellyseerr_url: str = ""
) -> tuple[str, str]:
    lines = [strings["candidates_header"]]
    html_lines = [f"<b>{html.escape(strings['candidates_header'])}</b>"]
    for i, c in enumerate(candidates, start=1):
        title = c.get("title") or "?"
        year = f" ({c['year']})" if c.get("year") else ""
        status = strings["status"].get(c.get("status") or "", c.get("status") or "")
        seasons = c.get("seasons")
        season_suffix = f" [{strings['seasons']}: {', '.join(map(str, seasons))}]" if seasons else ""
        line = f"{i}. {title}{year} — {status}{season_suffix}"
        lines.append(line)

        title_html = html.escape(f"{title}{year}")
        media_type, media_id = c.get("media_type"), c.get("media_id")
        if jellyseerr_url and media_id and media_type in ("movie", "tv"):
            href = f"{jellyseerr_url}/{media_type}/{media_id}"
            title_html = f'<a href="{html.escape(href)}">{title_html}</a>'
        html_lines.append(
            f"{i}. {title_html} — {html.escape(status)}{html.escape(season_suffix)}"
        )
    minutes = max(1, timeout_seconds // 60)
    hint = strings["confirm_hint"].format(minutes=minutes)
    lines.append(hint)
    html_lines.append(f"<i>{html.escape(hint)}</i>")
    return "\n".join(lines), "<br>".join(html_lines)


def request_result_text(result: str) -> tuple[str, str]:
    return result, html.escape(result)


# --- Pending confirmation state ----------------------------------------------
# Keyed by (room_id, sender_mxid) - a shared room needs per-sender state, not
# per-room, since more than one person can search at the same time. Sweep-then-
# check idiom mirrors jellyseerr-matrix-bot/bot.py's dedup_seen().


@dataclass
class Pending:
    candidates: list[dict]
    expires_at: float


def pending_sweep(store: dict, now: float) -> None:
    for key, p in list(store.items()):
        if p.expires_at <= now:
            del store[key]


def pending_get(store: dict, key: tuple, now: float) -> Pending | None:
    pending_sweep(store, now)
    return store.get(key)


def pending_set(store: dict, key: tuple, candidates: list[dict], now: float, ttl: int) -> None:
    pending_sweep(store, now)
    store[key] = Pending(candidates, now + ttl)


def pending_pop(store: dict, key: tuple, now: float) -> Pending | None:
    pending_sweep(store, now)
    return store.pop(key, None)


def pending_refresh(store: dict, key: tuple, now: float, ttl: int) -> None:
    p = store.get(key)
    if p:
        p.expires_at = now + ttl


# --- Bot wiring ---------------------------------------------------------------


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger().addHandler(ErrorCounter(level=logging.ERROR))
    set_lang(os.environ.get("BOT_LANG") or "en")
    for outcome in ("ok", "not_found", "error"):
        REQUESTS.labels(outcome)
    for outcome in ("confirmed", "cancelled", "ambiguous", "ignored"):
        CONFIRMATIONS.labels(outcome)

    homeserver = os.environ["MATRIX_URL"]
    user_id = os.environ["MATRIX_USER_ID"]
    device_id = os.environ["MATRIX_DEVICE_ID"]
    room_id = os.environ["MATRIX_ROOM_ID"]

    global JELLYSEERR_API_URL, JELLYSEERR_API_KEY
    JELLYSEERR_API_URL = os.environ["JELLYSEERR_API_URL"].rstrip("/")
    JELLYSEERR_API_KEY = os.environ["JELLYSEERR_API_KEY"]
    jellyseerr_url = (os.environ.get("JELLYSEERR_URL") or "").rstrip("/")
    trigger_prefix = os.environ.get("REQUEST_TRIGGER_PREFIX") or "!request"
    timeout_seconds = int(os.environ.get("REQUEST_CONFIRM_TIMEOUT_SECONDS") or 300)
    store = "/data/store"

    os.makedirs(store, exist_ok=True)
    client = AsyncClient(homeserver, user_id, device_id=device_id, store_path=store)
    client.access_token = os.environ["MATRIX_TOKEN"]
    client.user_id = user_id
    client.load_store()
    log.info(
        "Starting as %s / device %s / trigger %r / timeout %ss",
        user_id, client.device_id, trigger_prefix, timeout_seconds,
    )

    async def on_invite(room: MatrixRoom, event: InviteMemberEvent):
        if event.state_key == client.user_id and room.room_id == room_id:
            log.info("Invited to %s -> joining", room.room_id)
            await client.join(room.room_id)

    client.add_event_callback(on_invite, InviteMemberEvent)

    async def on_sync(_resp: SyncResponse):
        LAST_SYNC.set(time.time())

    client.add_response_callback(on_sync, SyncResponse)

    if client.should_upload_keys:
        await client.keys_upload()
    await client.sync(timeout=30000, full_state=True)
    await client.join(room_id)

    # Registered only after the initial full-state sync has been processed, so
    # room backlog never reaches this callback; should_handle_event's timestamp
    # check is a second, independent guard against the same failure mode.
    startup_ts = time.time() * 1000
    pending: dict[tuple, Pending] = {}
    lock = asyncio.Lock()

    async def reply(body: str, formatted: str | None = None, poster_url: str | None = None):
        async with lock:  # ponytail: one room, one sender - a global lock is enough
            await send(client, room_id, body, formatted or html.escape(body), [], poster_url=poster_url)

    async def handle_command(sender: str, query: str):
        if not query:
            await reply(S["usage"])
            return
        try:
            result = await run_agent(query)
        except Exception:
            log.exception("Agent invocation failed")
            REQUESTS.labels("error").inc()
            await reply(S["error"])
            return

        if result.offered:
            key = (room_id, sender)
            pending_set(pending, key, result.candidates, time.monotonic(), timeout_seconds)
            body, formatted = render_candidates_text(result.candidates, S, timeout_seconds, jellyseerr_url)
            poster = result.candidates[0].get("poster_url")
            await reply(body, formatted, poster_url=poster)
            REQUESTS.labels("ok").inc()
        else:
            await reply(result.answer, html.escape(result.answer), poster_url=result.poster_url)
            REQUESTS.labels("not_found").inc()

    async def handle_reply(key: tuple, entry: Pending, text: str):
        kind, idx = classify_reply(text, len(entry.candidates))
        now = time.monotonic()
        if kind == "cancel":
            pending_pop(pending, key, now)
            await reply(S["cancelled"])
            CONFIRMATIONS.labels("cancelled").inc()
        elif kind == "confirm":
            pending_pop(pending, key, now)
            candidate = entry.candidates[idx - 1]
            try:
                result = await request_jellyseerr(
                    candidate["media_id"], candidate["media_type"], seasons=candidate.get("seasons")
                )
            except Exception:
                log.exception("Request to Jellyseerr failed")
                result = S["error"]
            body, formatted = request_result_text(result)
            await reply(body, formatted)
            CONFIRMATIONS.labels("confirmed").inc()
        elif kind == "ambiguous":
            pending_refresh(pending, key, now, timeout_seconds)
            await reply(S["ambiguous"])
            CONFIRMATIONS.labels("ambiguous").inc()
        else:
            CONFIRMATIONS.labels("ignored").inc()

    async def on_message(room: MatrixRoom, event: RoomMessageText):
        if room.room_id != room_id:
            return
        if not should_handle_event(event.sender, event.server_timestamp, client.user_id, startup_ts):
            return
        text = event.body or ""
        query = parse_command(text, trigger_prefix)
        if query is not None:
            await handle_command(event.sender, query)
            return
        key = (room_id, event.sender)
        entry = pending_get(pending, key, time.monotonic())
        if entry:
            await handle_reply(key, entry, text)

    client.add_event_callback(on_message, RoomMessageText)

    async def metrics(_req: web.Request) -> web.Response:
        return web.Response(body=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})

    async def healthz(_req: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/metrics", metrics)
    app.router.add_get("/healthz", healthz)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8080).start()
    log.info("Metrics on :8080/metrics, health on :8080/healthz")

    await client.sync_forever(timeout=30000)


def selfcheck():
    # parse_command: with/without query, whitespace, non-matching text, custom prefix.
    assert parse_command("!request Dune 2021") == "Dune 2021"
    assert parse_command("!request   ") == ""
    assert parse_command("!request") == ""
    assert parse_command("hello there") is None
    assert parse_command("!req Dune", prefix="!req") == "Dune"
    assert parse_command("!requestfoo") is None  # no boundary -> not a match

    # classify_reply: synonyms in both languages, case/whitespace, unknown text,
    # digit bounds, ambiguous 'yes' with 2 candidates.
    assert classify_reply("1", 2) == ("confirm", 1)
    assert classify_reply(" 2 ", 2) == ("confirm", 2)
    assert classify_reply("3", 2) == ("ignore", None)
    assert classify_reply("0", 2) == ("ignore", None)
    assert classify_reply("ja", 1) == ("confirm", 1)
    assert classify_reply("YES", 1) == ("confirm", 1)
    assert classify_reply("ja", 2) == ("ambiguous", None)
    assert classify_reply("Yes", 2) == ("ambiguous", None)
    assert classify_reply("nein", 2) == ("cancel", None)
    assert classify_reply("Cancel", 1) == ("cancel", None)
    assert classify_reply("abbrechen", 2) == ("cancel", None)
    assert classify_reply("whatever", 2) == ("ignore", None)
    assert classify_reply("", 2) == ("ignore", None)

    # pending store: sweep-then-check idiom, expiry boundary with injected now.
    store: dict = {}
    key = ("!room", "@frodo:example.org")
    pending_set(store, key, [{"title": "X"}], 1000.0, ttl=300)
    assert pending_get(store, key, 1000.0) is not None
    assert pending_get(store, key, 1299.9) is not None
    assert pending_get(store, key, 1300.0) is None  # expired, swept
    assert key not in store

    pending_set(store, key, [{"title": "X"}], 2000.0, ttl=300)
    popped = pending_pop(store, key, 2100.0)
    assert popped is not None and popped.candidates == [{"title": "X"}]
    assert key not in store  # popped, not just read
    assert pending_pop(store, key, 2200.0) is None  # already gone

    pending_set(store, key, [{"title": "X"}], 3000.0, ttl=300)
    pending_refresh(store, key, 3299.0, ttl=300)
    assert pending_get(store, key, 3500.0) is not None  # would have expired without the refresh

    # should_handle_event: own message (echo), pre-startup timestamp, otherwise ok.
    assert should_handle_event("@bot:example.org", 5000, "@bot:example.org", 1000) is False
    assert should_handle_event("@frodo:example.org", 500, "@bot:example.org", 1000) is False
    assert should_handle_event("@frodo:example.org", 1500, "@bot:example.org", 1000) is True

    # extract_candidates: match in tool output moves to front, seasons attached;
    # fallback when the agent's pick isn't in the tool output at all.
    tool_json = json.dumps(
        [
            {"title": "A", "media_id": 1, "media_type": "movie", "status": "Not Requested"},
            {"title": "B", "media_id": 2, "media_type": "movie", "status": "Not Requested"},
        ]
    )
    msgs = [ToolMessage(content=tool_json, name="search_jellyseerr", tool_call_id="x")]

    structured = JellyseerrResponse(answer="ok", action="offer_request", media_id=2, media_type="movie")
    cands = extract_candidates(msgs, structured)
    assert cands[0]["title"] == "B" and cands[1]["title"] == "A", cands

    structured_seasons = JellyseerrResponse(
        answer="ok", action="offer_request", media_id=1, media_type="movie", seasons=[1, 3]
    )
    cands = extract_candidates(msgs, structured_seasons)
    assert cands[0]["title"] == "A" and cands[0]["seasons"] == [1, 3], cands

    structured_nomatch = JellyseerrResponse(answer="ok", action="offer_request", media_id=99, media_type="tv")
    cands = extract_candidates(msgs, structured_nomatch)
    assert len(cands) == 3 and cands[0]["media_id"] == 99, cands

    assert extract_candidates(msgs, None) == json.loads(tool_json)

    # render_candidates_text: both languages, season suffix, HTML escaping of a
    # hostile title (trust boundary: titles come from Jellyseerr's own upstream
    # TMDB data, but must not be trusted to be safe HTML).
    hostile = [{"title": "<img src=x onerror=1>", "year": "2020", "status": "Not Requested"}]
    body, fmt = render_candidates_text(hostile, STRINGS["en"], 300)
    assert "<img" not in fmt, fmt
    assert "1. <img src=x onerror=1> (2020)" in body, body

    body, fmt = render_candidates_text(
        [{"title": "Show", "year": "2021", "status": "Not Requested", "seasons": [1, 2]}], STRINGS["de"], 90
    )
    assert "Staffeln: 1, 2" in body, body
    assert "Läuft in 1 Min. ab" in body, body  # floor, no ceil: 90s -> max(1, 90//60) = 1

    # Deep link only when jellyseerr_url + media_id + a known media_type are present.
    linked = [{"title": "Dune", "year": "2021", "status": "Not Requested", "media_type": "movie", "media_id": 42}]
    _, fmt = render_candidates_text(linked, STRINGS["en"], 300, jellyseerr_url="https://jf.example")
    assert '<a href="https://jf.example/movie/42">Dune (2021)</a>' in fmt, fmt
    _, fmt = render_candidates_text(linked, STRINGS["en"], 300)  # no base URL -> no link
    assert "<a href" not in fmt, fmt
    # Hostile media_id from the Jellyseerr response must not break out of the attribute.
    hostile_id = [{"title": "X", "media_type": "movie", "media_id": '"><script>'}]
    _, fmt = render_candidates_text(hostile_id, STRINGS["en"], 300, jellyseerr_url="https://jf.example")
    assert "<script>" not in fmt and 'href="https://jf.example/movie/&quot;' in fmt, fmt

    # request_result_text: escapes a hostile Jellyseerr error message.
    body, fmt = request_result_text("Failed: <script>alert(1)</script>")
    assert "<script>" not in fmt, fmt
    assert body == "Failed: <script>alert(1)</script>"

    # i18n coverage: both languages define the same keys.
    assert STRINGS["en"].keys() == STRINGS["de"].keys()
    assert STRINGS["en"]["status"].keys() == STRINGS["de"]["status"].keys()

    # Error counter, same pattern as jellyseerr-matrix-bot/bot.py.
    logging.getLogger().addHandler(ErrorCounter(level=logging.ERROR))
    before = REGISTRY.get_sample_value("bot_errors_total") or 0
    logging.getLogger("nio.something").error("boom")
    logging.getLogger().warning("just a warning")
    after = REGISTRY.get_sample_value("bot_errors_total") or 0
    assert after == before + 1, (before, after)

    print("selfcheck ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
    else:
        asyncio.run(main())
