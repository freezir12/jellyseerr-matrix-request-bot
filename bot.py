#!/usr/bin/env python3
"""Interactive Jellyseerr request bot for Matrix, modeled on teleseerr (the
Telegram bot). Companion to jellyseerr-matrix-bot (the webhook notifier) - this
one listens in the same E2EE room and lets users search and request media.

Unlike Telegram, Matrix has no inline buttons, so navigation is done with
emoji reactions on the bot's own message: ◀️ / ➕ / ➡️. The bot posts a result
with poster + caption (like teleseerr's template), and the user reacts to
navigate through results, load more (pagination), or fire the request.

Search goes straight to the Jellyseerr API (no LLM), exactly like teleseerr.
"""
import asyncio
import html
import io
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import quote
from uuid import uuid4

import aiohttp
from aiohttp import web
from nio import (
    Api,
    AsyncClient,
    InviteMemberEvent,
    MatrixRoom,
    MegolmEvent,
    ReactionEvent,
    RoomKeyRequest,
    RoomMessageText,
    RoomSendResponse,
    SyncResponse,
    UploadResponse,
)
from nio.exceptions import LocalProtocolError
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, generate_latest

log = logging.getLogger("jellyseerr-matrix-request-bot")

ERRORS = Counter("bot_errors_total", "Errors (log.error/log.exception) anywhere in the bot")
REQUESTS = Counter("bot_requests_total", "!request commands handled", ["outcome"])
NAVIGATION = Counter("bot_navigation_total", "Reaction navigation events", ["action"])
LAST_SYNC = Gauge("bot_last_sync_timestamp", "Unix time of the last /sync from the homeserver")

IMAGE_TMDB_URL = "https://image.tmdb.org/t/p/w600_and_h900_bestv2"
POSTER_MAX_BYTES = 10 * 1024 * 1024  # sanity limit; posters are ~200 KB

# Reaction keys, mirroring teleseerr's inline keyboard (◀️ / ➕ Request / ➡️).
REACT_PREV = "◀️"
REACT_REQUEST = "➕"
REACT_NEXT = "➡️"
REACTION_KEYS = {REACT_PREV, REACT_REQUEST, REACT_NEXT}

JELLYSEERR_API_URL = os.getenv("JELLYSEERR_API_URL", "").rstrip("/")
JELLYSEERR_API_KEY = os.getenv("JELLYSEERR_API_KEY", "")


class ErrorCounter(logging.Handler):
    """Counts every log.error/log.exception - attached to the root logger so nio
    errors count too, without touching individual code paths."""

    def emit(self, record):
        ERRORS.inc()


# ponytail: two languages = two dicts; switch to a locale framework at 3+.
STRINGS = {
    "en": {
        "usage": "Usage: !request <title>",
        "no_results": "No results found for '{query}'.",
        "error": "Sorry, something went wrong processing your request.",
        "requested": "✅ Requested successfully!",
        "request_failed": "❌ Something went wrong with the request.",
        "request_failed_detail": "❌ Request failed: {detail}",
        "already_available": "Already available.",
        "already_requested": "Already requested.",
        "not_requested": "Not requested",
        "react_hint": "React with ◀️ / ➕ / ➡️ to navigate or request.",
        "no_more_prev": "You cannot go back more!",
        "no_more_next": "No more results.",
        "media_type": {"movie": "Movie", "tv": "TV Show"},
    },
    "de": {
        "usage": "Nutzung: !request <Titel>",
        "no_results": "Keine Ergebnisse für '{query}' gefunden.",
        "error": "Entschuldigung, bei der Verarbeitung ist ein Fehler aufgetreten.",
        "requested": "✅ Erfolgreich angefragt!",
        "request_failed": "❌ Bei der Anfrage ist etwas schiefgelaufen.",
        "request_failed_detail": "❌ Anfrage fehlgeschlagen: {detail}",
        "already_available": "Bereits verfügbar.",
        "already_requested": "Bereits angefragt.",
        "not_requested": "Nicht angefragt",
        "react_hint": "Reagiere mit ◀️ / ➕ / ➡️ zum Navigieren oder Anfragen.",
        "no_more_prev": "Du kannst nicht weiter zurück!",
        "no_more_next": "Keine weiteren Ergebnisse.",
        "media_type": {"movie": "Film", "tv": "Serie"},
    },
}

S = STRINGS["en"]


def set_lang(code: str):
    global S
    if code not in STRINGS:
        log.warning("Unknown BOT_LANG %r, falling back to en", code)
        code = "en"
    S = STRINGS[code]


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


async def _prepare_room(client: AsyncClient, room_id: str) -> None:
    """Ensure keys are queried, the group session is shared and members are
    synced before sending an encrypted message."""
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


async def send(
    client: AsyncClient, room_id: str, body: str, formatted: str,
    mentions: list[str], poster_url: str | None = None,
) -> RoomSendResponse | None:
    """Send a (possibly poster-carrying) message to the room. Returns the
    RoomSendResponse so the caller can grab the event_id for reactions."""
    await _prepare_room(client, room_id)

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
        return None
    return resp


async def send_reaction(client: AsyncClient, room_id: str, target_event_id: str, key: str) -> None:
    """Send an m.reaction to a target event. Uses client.room_send so nio
    encrypts it automatically (reactions carry no mentions we need in cleartext)."""
    content = {
        "m.relates_to": {
            "rel_type": "m.annotation",
            "event_id": target_event_id,
            "key": key,
        }
    }
    resp = await client.room_send(room_id, "m.reaction", content, uuid4())
    if not isinstance(resp, RoomSendResponse):
        log.error("Reaction send failed: %s", resp)


async def edit_message(
    client: AsyncClient, room_id: str, target_event_id: str,
    body: str, formatted: str, poster_url: str | None = None,
) -> None:
    """Edit an existing message (m.replace). Rebuilds the content - image if a
    poster is available, else text - and sends it as a new m.room.message with
    the m.replace relation, encrypted via client.room_send."""
    await _prepare_room(client, room_id)

    inner = None
    if poster_url:
        try:
            uri, keys, size, mimetype = await upload_poster(client, poster_url)
            inner = image_content(uri, keys, size, mimetype, body, formatted, [])
        except Exception:
            log.exception("Poster upload failed during edit, editing text only")
    if inner is None:
        inner = text_content(body, formatted, [])

    content = dict(inner)
    content["m.new_content"] = inner
    content["m.relates_to"] = {"rel_type": "m.replace", "event_id": target_event_id}

    resp = await client.room_send(room_id, "m.room.message", content, uuid4())
    if not isinstance(resp, RoomSendResponse):
        log.error("Edit send failed: %s", resp)


# --- Jellyseerr API ---------------------------------------------------------
# Direct API calls, mirroring teleseerr's search()/request() in
# teleseerr/src/jellyseerr.ts - no LLM involved.


def _status_from_media_info(media_info: dict | None) -> str:
    """Map Jellyseerr mediaInfo status codes to a human status.
    1: Unknown, 2: Pending, 3: Processing, 4: Partially Available, 5: Available."""
    if not media_info:
        return "Not Requested"
    status = media_info.get("status")
    status_4k = media_info.get("status4k")
    if status == 5 or status_4k == 5:
        return "Available"
    if status in (2, 3, 4) or status_4k in (2, 3, 4):
        return "Requested"
    return "Not Requested"


async def search_jellyseerr(query: str, page: int = 1) -> list[dict]:
    """Search Jellyseerr for movies/TV shows. Returns a list of result dicts
    (title, overview, release_date, poster_url, media_type, media_id, status),
    filtering out 'person' results like teleseerr does.

    The query is URL-encoded TWICE: Jellyseerr rejects a raw multi-word query
    ("The Lion King" -> 400 'must be url encoded'), and aiohttp encodes the
    params dict once more on the wire. teleseerr does the same via
    `searchParams.append(key, encodeURIComponent(val))` - the double encoding
    is what Jellyseerr actually expects."""
    url = f"{JELLYSEERR_API_URL}/search"
    headers = {"X-Api-Key": JELLYSEERR_API_KEY, "Content-Type": "application/json"}
    params = {"query": quote(query), "page": str(page)}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status >= 400:
                text = await resp.text()
                log.error("Jellyseerr search failed: %s - %s", resp.status, text)
                return []
            data = await resp.json()

    processed = []
    for r in data.get("results", []):
        if r.get("mediaType") == "person":
            continue
        poster_path = r.get("posterPath")
        processed.append(
            {
                "title": r.get("title") or r.get("name"),
                "overview": r.get("overview"),
                "release_date": r.get("releaseDate") or r.get("firstAirDate") or "",
                "poster_url": f"{IMAGE_TMDB_URL}{poster_path}" if poster_path else None,
                "media_type": r.get("mediaType"),
                "media_id": r.get("id"),
                "status": _status_from_media_info(r.get("mediaInfo")),
            }
        )
    return processed


async def request_jellyseerr(media_id: int, media_type: str) -> str:
    """Send a request to Jellyseerr. For TV shows, requests all seasons by
    default (seasons: 'all'), exactly like teleseerr. Returns a human message."""
    url = f"{JELLYSEERR_API_URL}/request"
    headers = {"X-Api-Key": JELLYSEERR_API_KEY, "Content-Type": "application/json"}
    data: dict[str, int | str] = {"mediaId": media_id, "mediaType": media_type}
    if media_type == "tv":
        data["seasons"] = "all"

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as resp:
            if resp.status < 400:
                return S["requested"]
            error_message = f"Status: {resp.status}"
            try:
                body = await resp.json()
                if body.get("message"):
                    error_message = body["message"]
            except Exception:
                pass
            log.error("Jellyseerr request failed: %s - %s", resp.status, error_message)
            return S["request_failed_detail"].format(detail=error_message)


# --- Result template ---------------------------------------------------------
# Mirrors teleseerr's createTemplate(): title, overview, release date, type.


def render_result(result: dict, index: int, total: int) -> tuple[str, str]:
    """Build the (plain, html) caption for a single result, like teleseerr's
    template plus a position indicator and the reaction hint."""
    title = result.get("title") or "?"
    overview = (result.get("overview") or "").strip()
    release_date = result.get("release_date") or ""
    media_type = result.get("media_type") or ""
    status = result.get("status") or "Not Requested"

    media_type_label = S["media_type"].get(media_type, media_type)
    status_label = {
        "Available": S["already_available"],
        "Requested": S["already_requested"],
        "Not Requested": S["not_requested"],
    }.get(status, status)

    parts = [f"<b>{html.escape(title)}</b>"]
    if overview:
        parts.append(html.escape(overview))
    if release_date:
        parts.append(f"<i>release date: {html.escape(release_date)}</i>")
    parts.append(f"{html.escape(media_type_label)} — {html.escape(status_label)}")
    parts.append(f"<i>{index}/{total} · {html.escape(S['react_hint'])}</i>")

    plain = "\n\n".join(
        [
            title,
            overview,
            f"release date: {release_date}" if release_date else "",
            f"{media_type_label} — {status_label}",
            f"{index}/{total} · {S['react_hint']}",
        ]
    )
    # Collapse empty lines from missing overview/release date.
    plain = "\n".join(line for line in plain.split("\n") if line.strip())
    return plain, "<br>".join(parts)


# --- Message parsing (pure, covered by --selfcheck) -------------------------


def parse_command(text: str, prefix: str = "!request") -> str | None:
    """-> the query text (possibly empty) if `text` is a trigger command, else None."""
    pattern = re.compile(rf"^{re.escape(prefix)}(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
    m = pattern.match(text.strip())
    if not m:
        return None
    return (m.group(1) or "").strip()


def should_handle_event(sender: str, server_timestamp_ms: float, own_user_id: str, startup_ts_ms: float) -> bool:
    """False for our own messages (echo) and for anything from before this
    process started (the initial full-state sync must not re-trigger a real
    Jellyseerr request on restart)."""
    if sender == own_user_id:
        return False
    if server_timestamp_ms < startup_ts_ms:
        return False
    return True


def classify_reaction(key: str) -> str | None:
    """-> 'prev', 'request', 'next' for a known reaction key, else None."""
    if key == REACT_PREV:
        return "prev"
    if key == REACT_REQUEST:
        return "request"
    if key == REACT_NEXT:
        return "next"
    return None


# --- Search session state ----------------------------------------------------
# Keyed by the event_id of the bot's result message. Each session tracks the
# accumulated results, the current index, the query and the current page, so
# reactions on that message can navigate/request. Sweep-then-check idiom
# mirrors jellyseerr-matrix-bot/bot.py's dedup_seen().


@dataclass
class Session:
    query: str
    results: list[dict] = field(default_factory=list)
    index: int = 0
    page: int = 1
    expires_at: float = 0.0


def session_sweep(store: dict, now: float) -> None:
    for key, s in list(store.items()):
        if s.expires_at <= now:
            del store[key]


def session_get(store: dict, key: str, now: float) -> Session | None:
    session_sweep(store, now)
    return store.get(key)


def session_set(store: dict, key: str, session: Session, now: float, ttl: int) -> None:
    session_sweep(store, now)
    session.expires_at = now + ttl
    store[key] = session


def session_pop(store: dict, key: str, now: float) -> Session | None:
    session_sweep(store, now)
    return store.pop(key, None)


# --- Bot wiring ---------------------------------------------------------------


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger().addHandler(ErrorCounter(level=logging.ERROR))
    set_lang(os.environ.get("BOT_LANG") or "en")
    for outcome in ("ok", "no_results", "error"):
        REQUESTS.labels(outcome)
    for action in ("prev", "request", "next"):
        NAVIGATION.labels(action)

    homeserver = os.environ["MATRIX_URL"]
    user_id = os.environ["MATRIX_USER_ID"]
    device_id = os.environ["MATRIX_DEVICE_ID"]
    room_id = os.environ["MATRIX_ROOM_ID"]

    global JELLYSEERR_API_URL, JELLYSEERR_API_KEY
    JELLYSEERR_API_URL = os.environ["JELLYSEERR_API_URL"].rstrip("/")
    JELLYSEERR_API_KEY = os.environ["JELLYSEERR_API_KEY"]
    trigger_prefix = os.environ.get("REQUEST_TRIGGER_PREFIX") or "!request"
    session_ttl = int(os.environ.get("REQUEST_SESSION_TIMEOUT_SECONDS") or 600)
    store = "/data/store"

    os.makedirs(store, exist_ok=True)
    client = AsyncClient(homeserver, user_id, device_id=device_id, store_path=store)
    client.access_token = os.environ["MATRIX_TOKEN"]
    client.user_id = user_id
    client.load_store()
    log.info(
        "Starting as %s / device %s / trigger %r / session ttl %ss",
        user_id, client.device_id, trigger_prefix, session_ttl,
    )

    async def on_invite(room: MatrixRoom, event: InviteMemberEvent):
        if event.state_key == client.user_id and room.room_id == room_id:
            log.info("Invited to %s -> joining", room.room_id)
            await client.join(room.room_id)

    client.add_event_callback(on_invite, InviteMemberEvent)

    async def on_sync(_resp: SyncResponse):
        LAST_SYNC.set(time.time())

    client.add_response_callback(on_sync, SyncResponse)

    # E2EE key plumbing. nio does NOT automatically ask for missing Megolm
    # session keys, and it does not auto-answer incoming RoomKeyRequests when
    # the device is unverified. Both must be wired explicitly or the bot sits
    # in an encrypted room seeing only undecryptable MegolmEvents - which is
    # exactly the "no session found" spam in the logs. Without these handlers
    # the bot can never decrypt !request messages from other room members.
    async def on_undecryptable(room: MatrixRoom, event: MegolmEvent):
        if room.room_id != room_id:
            return
        if event.session_id in client.outgoing_key_requests:
            return  # a request is already in flight for this session
        try:
            resp = await client.request_room_key(event)
        except LocalProtocolError:
            pass  # already requested / not logged in yet - fine
        except Exception:
            log.exception("Requesting room key for session %s failed", event.session_id)
        else:
            log.info("Requested room key for session %s (sender %s)", event.session_id, event.sender)

    client.add_event_callback(on_undecryptable, MegolmEvent)

    async def on_key_request(event: RoomKeyRequest):
        # A device asks us for a room key. If it's untrusted we'd normally have
        # to verify it first (continue_key_share); here we share blindly so the
        # very first message from a fresh device starts decrypting immediately.
        # The key-sharing themselves stays safe: we only share what that device
        # is entitled to see anyway (the room it already has access to).
        try:
            device = client.device_store[event.sender][event.requesting_device_id]
            await client.continue_key_share(event)
            log.info("Shared room key %s with %s/%s", event.session_id, event.sender, event.requesting_device_id)
        except Exception:
            log.exception("Could not share room key %s with %s/%s", event.session_id, event.sender, event.requesting_device_id)

    client.add_to_device_callback(on_key_request, RoomKeyRequest)

    if client.should_upload_keys:
        await client.keys_upload()
    await client.sync(timeout=30000, full_state=True)
    await client.join(room_id)

    # Registered only after the initial full-state sync has been processed, so
    # room backlog never reaches this callback; should_handle_event's timestamp
    # check is a second, independent guard against the same failure mode.
    startup_ts = time.time() * 1000
    sessions: dict[str, Session] = {}
    lock = asyncio.Lock()

    async def reply(body: str, formatted: str | None = None, poster_url: str | None = None):
        async with lock:  # ponytail: one room, one sender - a global lock is enough
            return await send(client, room_id, body, formatted or html.escape(body), [], poster_url=poster_url)

    async def set_typing(typing: bool):
        """Show/hide the bot's typing indicator in the room. This is Matrix's
        equivalent of Telegram's 'typing...' - clients render it as an animated
        indicator next to the bot's name while it is working."""
        try:
            await client.room_typing(room_id, typing_state=typing, timeout=30000)
        except Exception:
            log.debug("Could not set typing state to %s", typing)

    async def show_result(session: Session, target_event_id: str | None = None):
        """Render the current result and either send a new message (with
        reactions) or edit the existing one. Returns the event_id of the
        message that carries the result (the new one on first send, the target
        on edit) so the caller can key its session on it."""
        result = session.results[session.index]
        body, formatted = render_result(result, session.index + 1, len(session.results))
        if target_event_id is None:
            resp = await reply(body, formatted, poster_url=result.get("poster_url"))
            if resp is not None:
                for key in (REACT_PREV, REACT_REQUEST, REACT_NEXT):
                    await send_reaction(client, room_id, resp.event_id, key)
            return resp.event_id if resp is not None else None
        await edit_message(client, room_id, target_event_id, body, formatted, result.get("poster_url"))
        return target_event_id

    async def handle_command(sender: str, query: str):
        if not query:
            await reply(S["usage"])
            return
        await set_typing(True)
        try:
            results = await search_jellyseerr(query, page=1)
        except Exception:
            log.exception("Jellyseerr search failed")
            REQUESTS.labels("error").inc()
            await set_typing(False)
            await reply(S["error"])
            return
        await set_typing(False)

        if not results:
            await reply(S["no_results"].format(query=query))
            REQUESTS.labels("no_results").inc()
            return

        session = Session(query=query, results=results, index=0, page=1)
        # Key the session on the event_id of the result message we are about to
        # send: reactions on that message arrive with that event_id, and
        # handle_reaction looks the session up by it. (Storing under a fixed
        # "pending" key would make every reaction look up a missing session.)
        event_id = await show_result(session)
        if event_id is not None:
            session_set(sessions, event_id, session, time.monotonic(), session_ttl)
        REQUESTS.labels("ok").inc()

    async def handle_reaction(sender: str, target_event_id: str, key: str):
        action = classify_reaction(key)
        if action is None:
            return
        now = time.monotonic()
        session = session_get(sessions, target_event_id, now)
        if session is None:
            return  # stale/unknown message - ignore

        if action == "request":
            session_pop(sessions, target_event_id, now)
            result = session.results[session.index]
            await set_typing(True)
            try:
                message = await request_jellyseerr(result["media_id"], result["media_type"])
            except Exception:
                log.exception("Request to Jellyseerr failed")
                message = S["request_failed"]
            await set_typing(False)
            await reply(message, html.escape(message))
            NAVIGATION.labels("request").inc()
            return

        if action == "prev":
            if session.index <= 0:
                await reply(S["no_more_prev"])
                NAVIGATION.labels("prev").inc()
                return
            session.index -= 1
        elif action == "next":
            if session.index >= len(session.results) - 1:
                # Pagination: fetch the next page and append, like teleseerr.
                session.page += 1
                await set_typing(True)
                try:
                    more = await search_jellyseerr(session.query, page=session.page)
                except Exception:
                    log.exception("Jellyseerr pagination search failed")
                    more = []
                await set_typing(False)
                if not more:
                    await reply(S["no_more_next"])
                    NAVIGATION.labels("next").inc()
                    return
                session.results.extend(more)
            session.index += 1

        session_set(sessions, target_event_id, session, now, session_ttl)
        # Editing the result message (poster upload + m.replace) is a network
        # round trip too, so show the typing indicator while it runs.
        await set_typing(True)
        await show_result(session, target_event_id=target_event_id)
        await set_typing(False)
        NAVIGATION.labels(action).inc()

    async def on_message(room: MatrixRoom, event: RoomMessageText):
        if room.room_id != room_id:
            return
        if not should_handle_event(event.sender, event.server_timestamp, client.user_id, startup_ts):
            return
        text = event.body or ""
        query = parse_command(text, trigger_prefix)
        if query is not None:
            await handle_command(event.sender, query)

    client.add_event_callback(on_message, RoomMessageText)

    async def on_reaction(room: MatrixRoom, event: ReactionEvent):
        if room.room_id != room_id:
            return
        if not should_handle_event(event.sender, event.server_timestamp, client.user_id, startup_ts):
            return
        await handle_reaction(event.sender, event.reacts_to, event.key)

    client.add_event_callback(on_reaction, ReactionEvent)

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

    # classify_reaction: known keys map to actions, unknown keys are ignored.
    assert classify_reaction(REACT_PREV) == "prev"
    assert classify_reaction(REACT_REQUEST) == "request"
    assert classify_reaction(REACT_NEXT) == "next"
    assert classify_reaction("👍") is None
    assert classify_reaction("") is None

    # should_handle_event: own message (echo), pre-startup timestamp, otherwise ok.
    assert should_handle_event("@bot:example.org", 5000, "@bot:example.org", 1000) is False
    assert should_handle_event("@frodo:example.org", 500, "@bot:example.org", 1000) is False
    assert should_handle_event("@frodo:example.org", 1500, "@bot:example.org", 1000) is True

    # session store: sweep-then-check idiom, expiry boundary with injected now.
    store: dict = {}
    key = "$event:example.org"
    session_set(store, key, Session(query="Dune"), 1000.0, ttl=600)
    assert session_get(store, key, 1000.0) is not None
    assert session_get(store, key, 1599.9) is not None
    assert session_get(store, key, 1600.0) is None  # expired, swept
    assert key not in store

    session_set(store, key, Session(query="Dune"), 2000.0, ttl=600)
    popped = session_pop(store, key, 2100.0)
    assert popped is not None and popped.query == "Dune"
    assert key not in store  # popped, not just read
    assert session_pop(store, key, 2200.0) is None  # already gone

    # _status_from_media_info: status code mapping.
    assert _status_from_media_info(None) == "Not Requested"
    assert _status_from_media_info({"status": 5}) == "Available"
    assert _status_from_media_info({"status": 4}) == "Requested"
    assert _status_from_media_info({"status": 1}) == "Not Requested"
    assert _status_from_media_info({"status": 1, "status4k": 5}) == "Available"

    # render_result: both languages, HTML escaping of a hostile title (trust
    # boundary: titles come from Jellyseerr's own upstream TMDB data, but must
    # not be trusted to be safe HTML).
    set_lang("en")
    hostile = {"title": "<img src=x onerror=1>", "overview": "o", "release_date": "2020", "media_type": "movie", "status": "Not Requested"}
    body, fmt = render_result(hostile, 1, 3)
    assert "<img" not in fmt, fmt
    assert "1/3" in body, body

    set_lang("de")
    body, fmt = render_result(
        {"title": "Show", "overview": "", "release_date": "", "media_type": "tv", "status": "Requested"},
        2, 2,
    )
    assert "Serie" in body, body  # German media type label
    assert "Bereits angefragt" in body, body  # German status label

    # i18n coverage: both languages define the same keys.
    set_lang("en")
    assert STRINGS["en"].keys() == STRINGS["de"].keys()
    assert STRINGS["en"]["media_type"].keys() == STRINGS["de"]["media_type"].keys()

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
