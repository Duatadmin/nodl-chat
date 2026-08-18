import datetime
import logging
import os
import threading

from asgiref.sync import async_to_sync
from livekit import api

logger = logging.getLogger(__name__)

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")

TOKEN_TTL = datetime.timedelta(hours=1)

# 1-to-1 voice call room shape. Also embedded in every access token so that
# if a participant's join auto-creates the room (create_room runs off the
# request path, so a fast client can win the race), the room still gets the
# call semantics — in particular the 35s empty_timeout that drives the
# room_finished → missed-call webhook.
CALL_ROOM_MAX_PARTICIPANTS = 2
CALL_ROOM_EMPTY_TIMEOUT = 35


def generate_token(identity: str, room_name: str) -> str:
    """Generate a LiveKit access token for the given identity and room.

    Args:
        identity: Participant identity (user email or ID string).
        room_name: LiveKit room name to grant access to.

    Returns:
        JWT access token string.

    Raises:
        ValueError: If LiveKit credentials are not configured.
    """
    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise ValueError("LiveKit API credentials not configured")

    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_ttl(TOKEN_TTL)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
            )
        )
        .with_room_config(
            api.RoomConfiguration(
                max_participants=CALL_ROOM_MAX_PARTICIPANTS,
                empty_timeout=CALL_ROOM_EMPTY_TIMEOUT,
            )
        )
    )
    return token.to_jwt()


async def create_room(
    room_name: str,
    max_participants: int = 2,
    empty_timeout: int = 35,
) -> dict:
    """Create a LiveKit room via the Room Service API (async).

    Args:
        room_name: Unique room name.
        max_participants: Max participants allowed (default 2 for 1-to-1 calls).
        empty_timeout: Seconds before empty room is closed (default 35 — server timeout).

    Returns:
        Dict with room details (name, sid).

    Raises:
        ValueError: If LiveKit credentials are not configured.
    """
    if not LIVEKIT_URL or not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise ValueError("LiveKit credentials not configured")

    lkapi = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    try:
        room = await lkapi.room.create_room(
            api.CreateRoomRequest(
                name=room_name,
                max_participants=max_participants,
                empty_timeout=empty_timeout,
            )
        )
        return {"name": room.name, "sid": room.sid}
    finally:
        await lkapi.aclose()


def create_room_sync(
    room_name: str,
    max_participants: int = 2,
    empty_timeout: int = 35,
) -> dict:
    """Sync wrapper for create_room(), safe to call from Django sync views.

    Uses asgiref.sync.async_to_sync which handles event loop management
    correctly in Django's sync context.
    """
    return async_to_sync(create_room)(room_name, max_participants, empty_timeout)


async def delete_room(room_name: str) -> None:
    """Delete a LiveKit room, disconnecting any joined participants.

    Raises:
        ValueError: If LiveKit credentials are not configured.
    """
    if not LIVEKIT_URL or not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise ValueError("LiveKit credentials not configured")

    lkapi = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    try:
        await lkapi.room.delete_room(api.DeleteRoomRequest(room=room_name))
    finally:
        await lkapi.aclose()


def delete_room_async(room_name: str) -> None:
    """Fire-and-forget best-effort room deletion (daemon thread).

    Used when a call dies before connecting (cancel/decline): without this
    the room stays joinable for empty_timeout more seconds, so a stale
    accept can land a participant alone in a dead room. Errors are logged,
    never raised — the empty_timeout auto-close remains the backstop.
    """

    def _run() -> None:
        try:
            async_to_sync(delete_room)(room_name)
        except Exception as e:
            logger.warning("LiveKit room deletion failed for %s: %s", room_name, e)

    threading.Thread(target=_run, daemon=True).start()
