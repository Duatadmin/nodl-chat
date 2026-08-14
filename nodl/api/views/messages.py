"""API views for message endpoints.

Implements REST API for chat messages with JWT authentication.
CSRF protection is disabled for state-changing endpoints as they use
Bearer token (JWT) authentication, not browser session cookies.
"""

import json
import logging
import re
from collections import defaultdict
from collections.abc import Callable
from contextlib import suppress
from functools import wraps
from typing import Any

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from pydantic import ValidationError

from nodl.api.serializers.messages import (
    MessageCreatePayload,
    MessageSerializer,
    ReactionSerializer,
)
from nodl.extensions.models import NodlRealmUserExtension, NodlTaskStreamExtension
from zerver.actions.message_flags import do_update_message_flags
from zerver.actions.message_send import check_send_message
from zerver.actions.muted_users import do_mute_user, do_unmute_user
from zerver.lib.exceptions import JsonableError, RateLimitedError
from zerver.lib.message import access_message, get_recent_private_conversations, messages_for_ids
from zerver.lib.muted_users import get_mute_object
from zerver.lib.rate_limiter import RateLimitedObject, RedisRateLimiterBackend
from zerver.lib.response import json_response_from_error
from zerver.lib.streams import access_stream_by_id
from zerver.lib.users import access_user_by_id_including_cross_realm
from zerver.models import Message, MutedUser, Reaction, Subscription, UserMessage, UserProfile
from zerver.models.clients import get_client

logger = logging.getLogger(__name__)


# Rate limiting configuration
MESSAGES_READ_LIMIT = 300  # requests per minute
MESSAGES_WRITE_LIMIT = 60  # messages per minute
FLAGS_WRITE_LIMIT = 300  # flag updates per minute (mark-read is bursty by design)
RATE_LIMIT_WINDOW = 60  # seconds


def _access_stream_or_archived_task_stream(user: UserProfile, stream_id: int):
    try:
        return access_stream_by_id(user, stream_id)[0]
    except Exception:
        task_extension = (
            NodlTaskStreamExtension.objects.select_related("zulip_stream")
            .filter(
                zulip_realm_id=user.realm_id,
                zulip_stream_id=stream_id,
            )
            .first()
        )
        if (
            not task_extension
            or not Subscription.objects.filter(
                user_profile=user,
                recipient=task_extension.zulip_stream.recipient,
            ).exists()
        ):
            raise
        return task_extension.zulip_stream


class MessagesRateLimitedObject(RateLimitedObject):
    """Rate limiter for messages API endpoints."""

    def __init__(self, user_id: int, key_prefix: str, limit: int, window: int) -> None:
        super().__init__(RedisRateLimiterBackend)
        self.user_id = user_id
        self.key_prefix = key_prefix
        self.limit = limit
        self.window = window

    def key(self) -> str:
        return f"{self.key_prefix}:{self.user_id}"

    def rules(self) -> list[tuple[int, int]]:
        return [(self.window, self.limit)]


def rate_limit(key_prefix: str, limit: int, window: int = RATE_LIMIT_WINDOW) -> Callable:
    """Decorator for rate limiting API endpoints."""

    def decorator(view_func: Callable) -> Callable:
        @wraps(view_func)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            user = getattr(request, "user_profile", None)
            if user is None:
                return JsonResponse(
                    {"result": "error", "code": "UNAUTHORIZED", "msg": "Authentication required"},
                    status=401,
                )

            rate_limiter = MessagesRateLimitedObject(user.id, key_prefix, limit, window)
            try:
                rate_limiter.rate_limit_request(request)
            except RateLimitedError as e:
                return JsonResponse(
                    {
                        "result": "error",
                        "code": "RATE_LIMITED",
                        "msg": "Too many requests. Please wait before sending more messages.",
                        "retry_after": int(e.secs_to_freedom) if e.secs_to_freedom else window,
                    },
                    status=429,
                )
            except Exception:
                # Fail open on limiter-infrastructure failures (e.g. Redis
                # unreachable): a backend blip must not turn every request —
                # including the mobile inbox poll — into a 429. Tradeoff:
                # throttling (reads and writes alike) is disabled while the
                # limiter backend is down.
                logger.exception("Rate limiter backend failure; failing open")

            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator


def require_jwt_auth(view_func: Callable) -> Callable:
    """Decorator to require JWT authentication.

    Expects that authentication middleware has already validated the JWT
    and set request.user_profile.
    """

    @wraps(view_func)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        user = getattr(request, "user_profile", None)
        if user is None or not user.is_authenticated:
            return JsonResponse(
                {"result": "error", "code": "UNAUTHORIZED", "msg": "Authentication required"},
                status=401,
            )
        return view_func(request, *args, **kwargs)

    return wrapper


def _get_reactions_for_message(message_id: int) -> list[ReactionSerializer]:
    """Get reactions for a message, grouped by emoji."""
    reactions_data = Reaction.get_raw_db_rows([message_id])

    # Group reactions by emoji
    emoji_users: dict[tuple[str, str], list[int]] = defaultdict(list)
    for reaction in reactions_data:
        key = (reaction["emoji_name"], reaction["emoji_code"])
        emoji_users[key].append(reaction["user_profile_id"])

    return [
        ReactionSerializer(
            emoji_name=emoji_name,
            emoji_code=emoji_code,
            user_ids=user_ids,
        )
        for (emoji_name, emoji_code), user_ids in emoji_users.items()
    ]


def _get_message_flags(user: UserProfile, message_id: int) -> list[str]:
    """Get message flags for a user (read, starred, etc.)."""
    try:
        user_message = UserMessage.objects.get(user_profile=user, message_id=message_id)
        flags = []
        if user_message.flags.read:
            flags.append("read")
        if user_message.flags.starred:
            flags.append("starred")
        if user_message.flags.mentioned:
            flags.append("mentioned")
        return flags
    except UserMessage.DoesNotExist:
        return []


def _build_dm_recipient_query(user: UserProfile, user_ids: list[int]):
    """Build a query for DM messages to/from specific users.

    For 1:1 DMs using Recipient.PERSONAL, we need a BIDIRECTIONAL query because:
    - When User A sends to User B: recipient_id = User B's personal recipient
    - When User B sends to User A: recipient_id = User A's personal recipient

    For group DMs (Recipient.DIRECT_MESSAGE_GROUP), a simple recipient filter works
    because all messages share the same recipient_id.
    """
    from django.db.models import Q

    from zerver.lib.recipient_users import recipient_for_user_profiles
    from zerver.models import DirectMessageGroup, Recipient

    # Include current user in the lookup
    all_user_ids = sorted(set(user_ids + [user.id]))

    # Get recipient for this exact set of users
    try:
        user_profiles = list(
            UserProfile.objects.filter(
                id__in=all_user_ids,
                realm=user.realm,
            )
        )
        if len(user_profiles) != len(all_user_ids):
            return None  # Some users not found

        recipient = recipient_for_user_profiles(
            user_profiles=user_profiles,
            forwarded_mirror_message=False,
            forwarder_user_profile=None,
            sender=user,
            create=False,  # Don't create if doesn't exist
        )

        # Group DM (DIRECT_MESSAGE_GROUP) - simple query by recipient_id
        # All messages in a group DM share the same recipient
        if recipient.type == Recipient.DIRECT_MESSAGE_GROUP:
            return Message.objects.filter(
                realm_id=user.realm_id,
                recipient_id=recipient.id,
            )

        # 1:1 DM using PERSONAL recipient - need bidirectional query
        # Find the other participant
        other_participant = None
        for profile in user_profiles:
            if profile.id != user.id:
                other_participant = profile
                break

        if other_participant:
            # Bidirectional query: messages in BOTH directions
            # 1. Messages sent BY the other person TO me (recipient = my personal recipient)
            # 2. Messages sent BY me TO the other person (recipient = their personal recipient)
            return Message.objects.filter(
                realm_id=user.realm_id,
            ).filter(
                Q(sender_id=other_participant.id, recipient_id=user.recipient_id)
                | Q(sender_id=user.id, recipient_id=other_participant.recipient_id)
            )
        else:
            # Self DM (messaging yourself)
            return Message.objects.filter(
                realm_id=user.realm_id,
                sender_id=user.id,
                recipient_id=user.recipient_id,
            )

    except DirectMessageGroup.DoesNotExist:
        # No existing DirectMessageGroup, fall back to personal recipients for 1:1 DM
        try:
            # Get the other user (excluding current user from the list)
            other_user_ids = [uid for uid in user_ids if uid != user.id]
            if not other_user_ids:
                # Self DM case
                return Message.objects.filter(
                    realm_id=user.realm_id,
                    sender_id=user.id,
                    recipient_id=user.recipient_id,
                )

            other_user = UserProfile.objects.get(id=other_user_ids[0], realm=user.realm)

            # Bidirectional query using personal recipients
            return Message.objects.filter(
                realm_id=user.realm_id,
            ).filter(
                Q(sender_id=other_user.id, recipient_id=user.recipient_id)
                | Q(sender_id=user.id, recipient_id=other_user.recipient_id)
            )
        except (IndexError, UserProfile.DoesNotExist):
            return None
    except Exception:
        return None


@csrf_exempt
def messages_dispatch(request: HttpRequest) -> HttpResponse:
    """Dispatch /api/v1/messages by HTTP method: GET → list, POST → send."""
    if request.method == "GET":
        return list_messages(request)
    elif request.method == "POST":
        return send_message(request)
    else:
        return JsonResponse(
            {"result": "error", "msg": "Method not allowed"},
            status=405,
        )


@require_jwt_auth
@rate_limit(key_prefix="messages_read", limit=MESSAGES_READ_LIMIT)
def list_messages(request: HttpRequest) -> HttpResponse:
    """Fetch messages with anchor-based pagination.

    GET /api/v1/messages

    Query parameters (choose one approach):

    For stream messages (legacy):
    - stream_id: Filter by stream (required)
    - topic: Filter by topic (optional)

    For DM messages (using narrow):
    - narrow: JSON array of filter operators, e.g.:
      - [{"operator":"dm","operand":[9]}] - DMs with user 9
      - [{"operator":"dm","operand":[9,12]}] - Group DM with users 9 and 12

    Common parameters:
    - anchor: 'newest', 'oldest', or message_id (default: 'newest')
    - num_before: Messages before anchor (default: 50)
    - num_after: Messages after anchor (default: 0)

    Response:
    {
        "result": "success",
        "messages": [...],
        "found_anchor": true,
        "found_oldest": false,
        "found_newest": true
    }
    """
    if request.method != "GET":
        return JsonResponse(
            {"result": "error", "code": "METHOD_NOT_ALLOWED", "msg": "GET required"},
            status=405,
        )

    user: UserProfile = request.user_profile  # type: ignore[attr-defined]

    # Parse common query parameters
    anchor = request.GET.get("anchor", "newest")

    try:
        num_before = int(request.GET.get("num_before", 50))
        num_after = int(request.GET.get("num_after", 0))
    except ValueError:
        return JsonResponse(
            {"result": "error", "code": "INVALID_PARAMS", "msg": "Invalid pagination parameters"},
            status=400,
        )

    # Validate limits
    num_before = min(max(0, num_before), 200)
    num_after = min(max(0, num_after), 200)

    # Honor the client's apply_markdown (stock Zulip default: true). The client
    # JSON-encodes the bool, so it arrives as the literal string "true"/"false".
    # When false, messages_for_ids returns raw Markdown source with
    # content_type 'text/x-markdown' instead of rendered HTML.
    apply_markdown = request.GET.get("apply_markdown", "true").lower() != "false"

    # Check for narrow parameter (DM queries)
    narrow_str = request.GET.get("narrow")
    stream_id_str = request.GET.get("stream_id")

    base_query = None

    if narrow_str:
        # Parse narrow parameter
        try:
            narrow_terms = json.loads(narrow_str)
        except json.JSONDecodeError:
            return JsonResponse(
                {"result": "error", "code": "INVALID_PARAMS", "msg": "Invalid narrow JSON"},
                status=400,
            )

        if not isinstance(narrow_terms, list):
            return JsonResponse(
                {
                    "result": "error",
                    "code": "INVALID_PARAMS",
                    "msg": "narrow must be an array",
                },
                status=400,
            )

        # Empty narrow = all messages in user's realm
        if len(narrow_terms) == 0:
            base_query = Message.objects.filter(realm_id=user.realm_id)
        else:
            # Parse the narrow operator
            term = narrow_terms[0]
            operator = term.get("operator", "")
            operand = term.get("operand")

            if operator == "dm":
                # DM with specific users
                if not isinstance(operand, list) or len(operand) == 0:
                    return JsonResponse(
                        {
                            "result": "error",
                            "code": "INVALID_PARAMS",
                            "msg": "dm operand must be a list of user IDs",
                        },
                        status=400,
                    )

                # Validate operand contains integers
                try:
                    user_ids = [int(uid) for uid in operand]
                except (ValueError, TypeError):
                    return JsonResponse(
                        {
                            "result": "error",
                            "code": "INVALID_PARAMS",
                            "msg": "dm operand must contain valid user IDs",
                        },
                        status=400,
                    )

                base_query = _build_dm_recipient_query(user, user_ids)
                if base_query is None:
                    return JsonResponse(
                        {
                            "result": "error",
                            "code": "NOT_FOUND",
                            "msg": "DM conversation not found",
                        },
                        status=404,
                    )
                # Filter out bot messages from DMs (e.g., Zulip's "Welcome Bot")
                base_query = base_query.exclude(sender__is_bot=True)

            elif operator in ("channel", "stream"):
                # Channel/stream narrow - operand is the stream ID (int)
                if not isinstance(operand, int):
                    try:
                        operand = int(operand)
                    except (ValueError, TypeError):
                        return JsonResponse(
                            {
                                "result": "error",
                                "code": "INVALID_PARAMS",
                                "msg": "channel operand must be a valid stream ID",
                            },
                            status=400,
                        )

                try:
                    stream = _access_stream_or_archived_task_stream(user, operand)
                except Exception:
                    return JsonResponse(
                        {
                            "result": "error",
                            "code": "NOT_FOUND",
                            "msg": "Stream not found or access denied",
                        },
                        status=404,
                    )

                base_query = Message.objects.filter(
                    realm_id=user.realm_id,
                    recipient_id=stream.recipient_id,
                )

                # Check for topic narrow term in remaining narrow terms
                for t in narrow_terms[1:]:
                    if t.get("operator") == "topic" and t.get("operand"):
                        base_query = base_query.filter(subject__iexact=t["operand"])
                        break

            else:
                return JsonResponse(
                    {
                        "result": "error",
                        "code": "INVALID_PARAMS",
                        "msg": f"Unsupported narrow operator: {operator}",
                    },
                    status=400,
                )

    elif stream_id_str:
        # Legacy stream-based query
        try:
            stream_id = int(stream_id_str)
        except ValueError:
            return JsonResponse(
                {"result": "error", "code": "INVALID_PARAMS", "msg": "Invalid stream_id"},
                status=400,
            )

        topic = request.GET.get("topic")

        # Verify user has access to the stream
        try:
            stream = _access_stream_or_archived_task_stream(user, stream_id)
        except Exception:
            return JsonResponse(
                {
                    "result": "error",
                    "code": "NOT_FOUND",
                    "msg": "Stream not found or access denied",
                },
                status=404,
            )

        base_query = Message.objects.filter(
            realm_id=user.realm_id,
            recipient_id=stream.recipient_id,
        )

        # Filter by topic if specified
        if topic:
            base_query = base_query.filter(subject__iexact=topic)
    else:
        # No narrow and no stream_id — return all messages in user's realm
        base_query = Message.objects.filter(realm_id=user.realm_id)

    # Apply anchor-based pagination to get message IDs
    anchor_message_id = None
    found_anchor = True
    message_ids: list[int] = []
    before_ids: list[int] = []
    after_ids: list[int] = []

    if anchor == "newest":
        # Get newest message IDs
        message_ids = list(
            base_query.order_by("-id").values_list("id", flat=True)[: num_before + 1]
        )
        message_ids.reverse()  # Put in chronological order
    elif anchor == "oldest":
        # Get oldest message IDs
        message_ids = list(base_query.order_by("id").values_list("id", flat=True)[: num_after + 1])
    elif anchor == "first_unread":
        # MVP: treat as newest — full unread tracking not implemented yet
        message_ids = list(
            base_query.order_by("-id").values_list("id", flat=True)[: num_before + 1]
        )
        message_ids.reverse()
    else:
        # Anchor is a message ID
        try:
            anchor_message_id = int(anchor)
        except ValueError:
            return JsonResponse(
                {"result": "error", "code": "INVALID_PARAMS", "msg": "Invalid anchor value"},
                status=400,
            )

        # Get message IDs before anchor
        before_ids = list(
            base_query.filter(id__lt=anchor_message_id)
            .order_by("-id")
            .values_list("id", flat=True)[:num_before]
        )
        before_ids.reverse()

        # Check if anchor message exists
        anchor_exists = base_query.filter(id=anchor_message_id).exists()
        if anchor_exists:
            anchor_ids = [anchor_message_id]
        else:
            anchor_ids = []
            found_anchor = False

        # Get message IDs after anchor
        after_ids = list(
            base_query.filter(id__gt=anchor_message_id)
            .order_by("id")
            .values_list("id", flat=True)[:num_after]
        )

        message_ids = before_ids + anchor_ids + after_ids

    # Fetch messages from cache using Zulip's cache layer
    if message_ids:
        message_dicts = messages_for_ids(
            message_ids=message_ids,
            user_message_flags={mid: [] for mid in message_ids},
            search_fields={},
            apply_markdown=apply_markdown,
            client_gravatar=False,
            allow_empty_topic_name=False,
            message_edit_history_visibility_policy=1,  # UserProfile.POLICY_ALLOW_ANYONE
            user_profile=user,
            realm=user.realm,
        )
        # Pass through raw Zulip message dicts — Flutter expects exact Zulip format
        message_data = message_dicts
    else:
        message_data = []

    # Determine pagination state efficiently
    # Instead of extra queries, use the count of returned messages vs requested
    found_oldest = False
    found_newest = False

    if message_ids:
        # If we got fewer messages than requested, we've hit a boundary
        if anchor in ("newest", "first_unread"):
            # For newest/first_unread anchor, we're at the newest if we got messages
            found_newest = True
            # We're at oldest if we got fewer than requested
            found_oldest = len(message_ids) < num_before + 1
        elif anchor == "oldest":
            # For oldest anchor, we're at the oldest
            found_oldest = True
            # We're at newest if we got fewer than requested
            found_newest = len(message_ids) < num_after + 1
        else:
            # For specific anchor, check both directions
            # This is an approximation - frontend can refine if needed
            found_oldest = len(before_ids) < num_before
            found_newest = len(after_ids) < num_after

    # Compute anchor value for response (Zulip returns the resolved anchor as int)
    if anchor in ("newest", "first_unread"):
        anchor_value = message_ids[-1] if message_ids else 0
    elif anchor == "oldest":
        anchor_value = message_ids[0] if message_ids else 0
    else:
        anchor_value = anchor_message_id or 0

    return JsonResponse(
        {
            "result": "success",
            "msg": "",
            "messages": message_data,
            "anchor": anchor_value,
            "found_anchor": found_anchor,
            "found_oldest": found_oldest,
            "found_newest": found_newest,
            "history_limited": False,
        }
    )


@csrf_exempt
@require_jwt_auth
@rate_limit(key_prefix="messages_send", limit=MESSAGES_WRITE_LIMIT)
def send_message(request: HttpRequest) -> HttpResponse:
    """Send a message to a stream or direct message.

    POST /api/v1/messages

    For stream messages:
    {
        "type": "stream",  // or omit for default
        "stream_id": 42,
        "topic": "Project Updates",
        "content": "Hello **world**!"
    }

    For direct messages:
    {
        "type": "direct",
        "to": [9, 12],  // Array of recipient user IDs
        "content": "Hello!"
    }

    Response:
    {
        "result": "success",
        "id": 12345,
        "message": {...}
    }
    """
    if request.method != "POST":
        return JsonResponse(
            {"result": "error", "code": "METHOD_NOT_ALLOWED", "msg": "POST required"},
            status=405,
        )

    user: UserProfile = request.user_profile  # type: ignore[attr-defined]

    try:
        # Try JSON first, fall back to form-encoded data (Flutter Zulip client)
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            body = dict(request.POST.items())
            # Parse JSON-encoded values in form data
            for key in ("to", "stream_id"):
                if key in body and isinstance(body[key], str):
                    with suppress(json.JSONDecodeError, ValueError):
                        body[key] = json.loads(body[key])
        # Extract Zulip client fields not in MessageCreatePayload
        local_id = body.pop("local_id", None)
        queue_id = body.pop("queue_id", None)
        read_by_sender = body.pop("read_by_sender", None)
        if isinstance(read_by_sender, str):
            read_by_sender = read_by_sender.lower() in ("true", "1")
        # Zulip API compat: mobile sends 'to' as stream_id (int) for stream messages
        msg_type = body.get("type", "stream")
        if msg_type == "stream" and "to" in body and isinstance(body["to"], int):
            body.setdefault("stream_id", body.pop("to"))
        payload = MessageCreatePayload(**body)
    except ValidationError as e:
        return JsonResponse(
            {"result": "error", "code": "VALIDATION_ERROR", "msg": str(e)},
            status=400,
        )

    client = get_client("nodl-api")

    # Handle direct messages
    if payload.type == "direct":
        if not payload.to or len(payload.to) == 0:
            return JsonResponse(
                {
                    "result": "error",
                    "code": "INVALID_PARAMS",
                    "msg": "Missing 'to' recipients for direct message",
                },
                status=400,
            )

        try:
            # Get recipient user profiles
            recipient_users = list(
                UserProfile.objects.filter(
                    id__in=payload.to,
                    realm=user.realm,
                    is_active=True,
                )
            )

            if len(recipient_users) != len(payload.to):
                return JsonResponse(
                    {
                        "result": "error",
                        "code": "NOT_FOUND",
                        "msg": "One or more recipients not found",
                    },
                    status=404,
                )

            # Send using Zulip's check_send_message with "private" type
            result = check_send_message(
                sender=user,
                client=client,
                recipient_type_name="private",
                message_to=payload.to,
                topic_name="",  # DMs don't have topics
                message_content=payload.content,
                realm=user.realm,
                local_id=local_id,
                sender_queue_id=queue_id,
                read_by_sender=read_by_sender if read_by_sender is not None else True,
            )

            # Fetch the created message
            message = Message.objects.select_related("sender", "recipient").get(
                id=result.message_id
            )

            # Include sender in recipients for display_recipient
            all_users = recipient_users + [user] if user.id not in payload.to else recipient_users
            serializer = MessageSerializer.from_message(message, recipient_users=all_users)

            return JsonResponse(
                {
                    "result": "success",
                    "msg": "",
                    "id": message.id,
                    "message": serializer.model_dump(),
                },
                status=200,
            )

        except JsonableError as e:
            return JsonResponse(
                {"result": "error", "code": "SEND_FAILED", "msg": str(e)},
                status=400,
            )
        except Exception as e:
            logger.exception("Failed to send direct message")
            return JsonResponse(
                {"result": "error", "code": "SEND_FAILED", "msg": str(e)},
                status=500,
            )

    # Handle stream messages (default)
    if not payload.stream_id:
        return JsonResponse(
            {
                "result": "error",
                "code": "INVALID_PARAMS",
                "msg": "stream_id is required for stream messages",
            },
            status=400,
        )

    # Default topic to "general" if not provided
    topic = payload.topic or "general"

    # Verify user has access to the stream
    try:
        stream, _ = access_stream_by_id(user, payload.stream_id)
    except Exception:
        return JsonResponse(
            {"result": "error", "code": "NOT_FOUND", "msg": "Stream not found or access denied"},
            status=404,
        )

    # Send the message using Zulip's check_send_message
    try:
        result = check_send_message(
            sender=user,
            client=client,
            recipient_type_name="stream",
            message_to=[stream.id],
            topic_name=topic,
            message_content=payload.content,
            realm=user.realm,
            local_id=local_id,
            sender_queue_id=queue_id,
            read_by_sender=read_by_sender if read_by_sender is not None else True,
        )

        # Fetch the created message to return full details
        # Include recipient to avoid N+1 query during serialization
        message = Message.objects.select_related("sender", "recipient").get(id=result.message_id)
        serializer = MessageSerializer.from_message(message)

        return JsonResponse(
            {
                "result": "success",
                "msg": "",
                "id": message.id,
                "message": serializer.model_dump(),
            },
            status=200,
        )

    except JsonableError as e:
        return JsonResponse(
            {"result": "error", "code": "SEND_FAILED", "msg": str(e)},
            status=400,
        )
    except Exception as e:
        logger.exception("Failed to send message")
        return JsonResponse(
            {"result": "error", "code": "SEND_FAILED", "msg": str(e)},
            status=500,
        )


@require_jwt_auth
@rate_limit(key_prefix="messages_read", limit=MESSAGES_READ_LIMIT)
def get_message(request: HttpRequest, message_id: int) -> HttpResponse:
    """Get a single message with reactions.

    GET /api/v1/messages/{id}

    Response:
    {
        "result": "success",
        "message": {
            "id": 12345,
            "sender_id": 678,
            "reactions": [...],
            ...
        }
    }
    """
    if request.method != "GET":
        return JsonResponse(
            {"result": "error", "code": "METHOD_NOT_ALLOWED", "msg": "GET required"},
            status=405,
        )

    user: UserProfile = request.user_profile  # type: ignore[attr-defined]

    # Access the message (verifies user has permission)
    try:
        message = access_message(user, message_id, is_modifying_message=False)
    except JsonableError:
        return JsonResponse(
            {"result": "error", "code": "NOT_FOUND", "msg": "Message not found or access denied"},
            status=404,
        )

    # Get reactions and flags
    reactions = _get_reactions_for_message(message_id)
    flags = _get_message_flags(user, message_id)

    serializer = MessageSerializer.from_message(message, reactions=reactions, flags=flags)

    return JsonResponse(
        {
            "result": "success",
            "message": serializer.model_dump(),
            # Stock-Zulip parity: expose the raw Markdown source at the top level
            # (the deprecated-but-supported `raw_content` field). Clients that
            # need the source to re-send a message (e.g. forward) read this.
            "raw_content": message.content,
        }
    )


def _seed_zulip_request_notes(request: HttpRequest) -> None:
    """Prepare RequestNotes for delegating to a stock Zulip view.

    JWT/Basic-authed nodl requests bypass Zulip's rest_dispatch, so the notes
    upstream views rely on (client, log_data) may be unset.
    """
    from zerver.lib.request import RequestNotes

    notes = RequestNotes.get_notes(request)
    if notes.client is None:
        notes.client = get_client("nodl-api")
    if notes.log_data is None:
        notes.log_data = {}


# Params update_message_backend accepts; JSON bodies are re-encoded into
# request.POST under these keys for @typed_endpoint.
_EDIT_MESSAGE_PARAMS = (
    "content",
    "topic",
    "propagate_mode",
    "prev_content_sha256",
    "send_notification_to_old_thread",
    "send_notification_to_new_thread",
    "stream_id",
)


@csrf_exempt
def message_detail_dispatch(request: HttpRequest, message_id: int) -> HttpResponse:
    """Dispatch /api/v1/messages/{id} by method: GET → fetch, PATCH → edit, DELETE → delete.

    This URL shadows Zulip's rest_path (nodl patterns register first), so
    every verb the Flutter client uses on it must be served here — before
    this dispatcher existed, PATCH/DELETE got a 405 from the GET-only view
    and mobile edit/delete could never reach the backend.
    """
    if request.method == "GET":
        return get_message(request, message_id)
    elif request.method == "PATCH":
        return edit_message(request, message_id)
    elif request.method == "DELETE":
        return delete_message(request, message_id)
    return JsonResponse(
        {"result": "error", "code": "METHOD_NOT_ALLOWED", "msg": "GET, PATCH or DELETE required"},
        status=405,
    )


@csrf_exempt
@require_jwt_auth
@rate_limit(key_prefix="messages_write", limit=MESSAGES_WRITE_LIMIT)
def edit_message(request: HttpRequest, message_id: int) -> HttpResponse:
    """Edit a message — thin wrapper over Zulip's update_message_backend.

    PATCH /api/v1/messages/{id}        (canonical; Flutter client, form-encoded)
    PATCH /api/v1/messages/{id}/edit   (legacy; web client, JSON {"content"})

    Same delegation idiom as update_flags: seed RequestNotes, normalize the
    body into request.POST for @typed_endpoint, delegate. This inherits ALL
    stock edit rules — sender-only editing, realm allow_message_editing, the
    realm edit time limit (+20s grace), prev_content_sha256 conflict
    detection, edit-history recording — and fixes DM editing: the previous
    hand-rolled StreamMessageEditRequest assumed a stream recipient and
    404'd on every direct message.

    The legacy /edit path responds with {"message": {...}} (the web client
    updates its caches from it); the canonical path returns Zulip's own
    response shape.
    """
    if request.method != "PATCH":
        return JsonResponse(
            {"result": "error", "code": "METHOD_NOT_ALLOWED", "msg": "PATCH required"},
            status=405,
        )

    user: UserProfile = request.user_profile  # type: ignore[attr-defined]
    _seed_zulip_request_notes(request)

    # JSON bodies (web client) are injected into request.POST; form-encoded
    # bodies (Flutter client) are parsed by process_as_post below, exactly as
    # Zulip's rest_dispatch does for PATCH.
    if request.content_type == "application/json" and request.body:
        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse(
                {"result": "error", "code": "INVALID_JSON", "msg": "Invalid JSON body"},
                status=400,
            )
        request.POST = request.POST.copy()
        for key in _EDIT_MESSAGE_PARAMS:
            if key in body and key not in request.POST:
                val = body[key]
                request.POST[key] = (
                    json.dumps(val) if isinstance(val, list | dict | bool) else str(val)
                )

    from zerver.decorator import process_as_post
    from zerver.views.message_edit import update_message_backend

    try:
        response = process_as_post(update_message_backend)(request, user, message_id=message_id)
    except JsonableError as e:
        return json_response_from_error(e)
    except Exception as e:
        logger.exception("Failed to edit message")
        return JsonResponse(
            {"result": "error", "code": "EDIT_FAILED", "msg": str(e)},
            status=500,
        )

    if request.path.endswith("/edit"):
        # Legacy web contract: return the updated message object.
        message = access_message(user, message_id, is_modifying_message=False)
        reactions = _get_reactions_for_message(message_id)
        flags = _get_message_flags(user, message_id)
        serializer = MessageSerializer.from_message(message, reactions=reactions, flags=flags)
        return JsonResponse({"result": "success", "message": serializer.model_dump()})

    return response


@csrf_exempt
@require_jwt_auth
@rate_limit(key_prefix="messages_write", limit=MESSAGES_WRITE_LIMIT)
def delete_message(request: HttpRequest, message_id: int) -> HttpResponse:
    """Delete a message — thin wrapper over Zulip's delete_message_backend.

    DELETE /api/v1/messages/{id}          (canonical; Flutter client)
    DELETE /api/v1/messages/{id}/delete   (legacy; web client)

    Inherits the stock permission rules (can_delete_any/own_message groups,
    channel-level overrides, realm delete time limit) and stock semantics:
    the message is archived (soft delete — restorable via `manage.py
    restore_messages`), never hard-deleted, and the row lock serializes a
    concurrent edit against the delete. Replaces a hand-rolled
    owner-or-admin check that ignored every realm setting.
    """
    if request.method != "DELETE":
        return JsonResponse(
            {"result": "error", "code": "METHOD_NOT_ALLOWED", "msg": "DELETE required"},
            status=405,
        )

    user: UserProfile = request.user_profile  # type: ignore[attr-defined]
    _seed_zulip_request_notes(request)

    from zerver.views.message_edit import delete_message_backend

    try:
        return delete_message_backend(request, user, message_id=message_id)
    except JsonableError as e:
        return json_response_from_error(e)
    except Exception as e:
        logger.exception("Failed to delete message")
        return JsonResponse(
            {"result": "error", "code": "DELETE_FAILED", "msg": str(e)},
            status=500,
        )


DM_PREVIEW_MAX_CHARS = 100

# The voice-note filename discriminator (`voice-<epoch>.m4a`) as it appears
# in preview text extracted from rendered_content.
_VOICE_FILENAME_RE = re.compile(r"voice-\d+\.m4a")


def _dm_preview_text(message: Message) -> str:
    """Plain-text preview of a message, matching how clients render it.

    Uses Zulip's push-notification HTML->text converter on rendered_content
    (handles emoji spans, image alt text, blockquotes, KaTeX, spoilers), so
    the polled preview agrees with a client that strips the rendered HTML —
    instead of leaking raw Markdown source. Whitespace is collapsed and the
    result is truncated with an ellipsis.
    """
    # Local import: don't pay the push-notifications import at module load.
    from zerver.lib.push_notifications import get_mobile_push_content

    text = message.content
    if message.rendered_content:
        try:
            text = get_mobile_push_content(message.rendered_content)
        except Exception:
            # An lxml parse failure must not 500 the inbox; raw content is
            # an acceptable degraded preview.
            logger.exception("Failed to render DM preview for message %d", message.id)
    # Voice notes (V2.11): never leak the raw `voice-….m4a` filename into the
    # preview — show `🎤 <transcript>` once the derived transcript is attached
    # to rendered_content, or a bare `🎤 Voice message` before it. Mirrors
    # the mobile client's messagePreviewText so polled and live-derived
    # previews render identically.
    if _VOICE_FILENAME_RE.search(text):
        text = _VOICE_FILENAME_RE.sub("", text)
        text = " ".join(text.split())
        text = f"🎤 {text}" if text else "🎤 Voice message"
    text = " ".join(text.split())
    if len(text) > DM_PREVIEW_MAX_CHARS:
        text = text[:DM_PREVIEW_MAX_CHARS] + "…"
    return text


@require_jwt_auth
@rate_limit(key_prefix="messages_read", limit=MESSAGES_READ_LIMIT)
def list_dm_conversations(request: HttpRequest) -> HttpResponse:
    """List DM conversations for the current user.

    GET /api/v1/dm/conversations

    Uses Zulip's get_recent_private_conversations() which correctly queries
    UserMessage with is_private flag instead of Subscription table.

    Structural limit: enumeration covers the user's most recent 1000 DM
    UserMessage rows (RECENT_CONVERSATIONS_LIMIT); a conversation whose
    entire history falls outside that window is absent from the response,
    including its unread count.

    Invariants:
    - `user_ids` = ALL counterparts (including bots); `users[]` = human
      participants only, active or not (deactivated entries carry
      `is_active: false`). Clients key conversations by `user_ids` and
      render from `users[]`.
    - `unread_count` counts only messages with id <= `last_message_id`, so
      the count and the preview always describe the same snapshot.
    - Rows are sorted by `last_message_id` descending.

    Response:
    {
        "result": "success",
        "conversations": [
            {
                "user_ids": [9],
                "users": [
                    {
                        "id": 9,
                        "full_name": "Alice",
                        "email": "alice@example.com",
                        "avatar_url": "/avatar/9",
                        "nodl_user_id": "<supabase uuid or null>",
                        "is_active": true,
                    }
                ],
                "last_message": {
                    "id": 12345,
                    "content": "Hello!",          # plain text, <=100 chars + ellipsis
                    "preview_message_id": 12345,  # the message `content` belongs to
                    "sender_id": 9,
                    "sender_full_name": "Alice",
                    "timestamp": 1234567890
                },
                "last_message_id": 12345,
                "unread_count": 2,
                "muted": false
            }
        ]
    }
    """

    if request.method != "GET":
        return JsonResponse(
            {"result": "error", "code": "METHOD_NOT_ALLOWED", "msg": "GET required"},
            status=405,
        )

    user: UserProfile = request.user_profile  # type: ignore[attr-defined]

    try:
        # Use Zulip's proven function that queries UserMessage with is_private flag
        recipient_map = get_recent_private_conversations(user)

        # Batch all per-conversation lookups up front; this endpoint is polled
        # once per workspace on every mobile app-open.
        all_participant_ids = {
            user_id for data in recipient_map.values() for user_id in data["user_ids"]
        }
        # Deactivated humans are INCLUDED (flagged is_active below): dropping
        # them would make their conversations vanish while the unread messages
        # persist — an orphaned count the client can never reconcile.
        profiles_by_id = {
            u["id"]: u
            for u in UserProfile.objects.filter(
                id__in=all_participant_ids,
            ).values("id", "full_name", "delivery_email", "avatar_source", "is_bot", "is_active")
        }
        muted_user_ids = set(
            MutedUser.objects.filter(user_profile=user).values_list("muted_user_id", flat=True)
        )
        # Cross-realm human identity: the mobile unified inbox groups
        # counterparts by supabase id (email alone is not a reliable join key).
        supabase_id_by_user_id = {
            zulip_user_id: str(supabase_user_id)
            for zulip_user_id, supabase_user_id in NodlRealmUserExtension.objects.filter(
                zulip_user_id__in=all_participant_ids
            ).values_list("zulip_user_id", "supabase_user_id")
        }
        last_messages_by_id = {
            m.id: m
            for m in Message.objects.select_related("sender").filter(
                id__in=[data["max_message_id"] for data in recipient_map.values()]
            )
        }

        # Unread DM counts, keyed the same way get_recent_private_conversations
        # keys conversations: incoming 1:1 messages (recipient = my personal
        # recipient) count under the sender's personal recipient id; everything
        # else (group DMs, modern direct-message-group model) under the
        # message's own recipient id.
        # Message ids are kept so each conversation's count can be clamped to
        # its enumeration-time max_message_id: the enumeration, the message
        # fetch, and this query are separate READ COMMITTED statements, so a
        # message arriving mid-request would otherwise increment the count
        # while the preview still shows the older message. Clamping makes
        # count and preview describe the same snapshot by construction; the
        # next poll picks up the newer message. (transaction.atomic would NOT
        # fix this — each statement still gets a fresh snapshot.)
        unread_ids_by_recipient: dict[int, list[int]] = defaultdict(list)
        unread_rows = UserMessage.objects.filter(
            user_profile=user,
            flags__andnz=UserMessage.flags.is_private.mask,
            flags__andz=UserMessage.flags.read.mask,
        ).values_list("message_id", "message__recipient_id", "message__sender__recipient_id")
        for message_id, message_recipient_id, sender_recipient_id in unread_rows:
            if user.recipient_id is not None and message_recipient_id == user.recipient_id:
                unread_ids_by_recipient[sender_recipient_id].append(message_id)
            else:
                unread_ids_by_recipient[message_recipient_id].append(message_id)

        conversations = []

        for recipient_id, data in recipient_map.items():
            participant_ids = data["user_ids"]
            if not participant_ids:
                continue

            # Filter out bot users from participants
            non_bot_users = [
                profiles_by_id[user_id]
                for user_id in participant_ids
                if user_id in profiles_by_id and not profiles_by_id[user_id].get("is_bot")
            ]

            # Skip conversations where all participants are bots (e.g., Welcome Bot)
            if not non_bot_users:
                continue

            users_data = [
                {
                    "id": u["id"],
                    "full_name": u["full_name"],
                    "email": u["delivery_email"],
                    "avatar_url": f"/avatar/{u['id']}" if u.get("avatar_source") else None,
                    "nodl_user_id": supabase_id_by_user_id.get(u["id"]),
                    "is_active": u["is_active"],
                }
                for u in non_bot_users
            ]

            last_message = last_messages_by_id.get(data["max_message_id"])
            if last_message is None:
                # The newest message was deleted between enumeration and the
                # message fetch. Drop the row for this response — the next
                # poll's enumeration re-derives the next-newest message.
                # (Emitting it would sort a blank-preview row to the bottom.)
                continue

            last_message_data = {
                "id": last_message.id,
                "content": _dm_preview_text(last_message),
                "preview_message_id": last_message.id,
                "sender_id": last_message.sender_id,
                "sender_full_name": last_message.sender.full_name,
                "timestamp": int(last_message.date_sent.timestamp()),
            }

            max_message_id = data["max_message_id"]
            conversations.append(
                {
                    "user_ids": participant_ids,
                    "users": users_data,
                    "last_message": last_message_data,
                    "last_message_id": max_message_id,
                    "unread_count": sum(
                        1
                        for mid in unread_ids_by_recipient.get(recipient_id, ())
                        if mid <= max_message_id
                    ),
                    # A 1:1 with a muted counterpart — or a group where every
                    # human counterpart is muted — is flagged, never hidden
                    # (hiding would orphan its unread count client-side).
                    "muted": all(u["id"] in muted_user_ids for u in non_bot_users),
                }
            )

        # Most recent first. Message ids are monotonic on this server, so the
        # sort is stable and tie-free (second-granularity timestamps are not).
        conversations.sort(key=lambda c: c["last_message_id"], reverse=True)

        return JsonResponse(
            {
                "result": "success",
                "conversations": conversations,
            }
        )

    except Exception:
        logger.exception("Failed to list DM conversations")
        return JsonResponse(
            {"result": "error", "code": "FETCH_FAILED", "msg": "Failed to list conversations"},
            status=500,
        )


@csrf_exempt
@require_jwt_auth
@rate_limit(key_prefix="messages_write", limit=MESSAGES_WRITE_LIMIT)
def mark_messages_as_read(request: HttpRequest) -> HttpResponse:
    """Mark messages as read.

    POST /api/v1/messages/read

    Request body (exactly one of):
    {
        "stream_id": 42,          // Mark all in stream
        "topic": "welcome",       // Optional: restrict to topic (requires stream_id)
        "dm_user_ids": [9] | [9,12],  // Mark an ENTIRE DM conversation (1:1 or group)
        "message_ids": [1,2,3]    // Mark specific messages
    }

    Response:
    {
        "result": "success",
        "messages_marked": 5
    }
    """
    if request.method != "POST":
        return JsonResponse(
            {"result": "error", "code": "METHOD_NOT_ALLOWED", "msg": "POST required"},
            status=405,
        )

    user: UserProfile = request.user_profile  # type: ignore[attr-defined]

    try:
        body = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse(
            {"result": "error", "code": "INVALID_JSON", "msg": "Invalid JSON body"},
            status=400,
        )

    stream_id = body.get("stream_id")
    topic = body.get("topic")
    message_ids = body.get("message_ids")
    dm_user_ids = body.get("dm_user_ids")

    from zerver.models import Stream

    try:
        if stream_id:
            # Mark all messages in a stream (optionally filtered by topic)
            from zerver.actions.message_flags import do_mark_stream_messages_as_read

            stream = Stream.objects.get(id=stream_id, realm=user.realm)
            count = do_mark_stream_messages_as_read(user, stream.recipient_id, topic)
        elif dm_user_ids:
            # Mark an entire DM conversation (1:1 or group) as read —
            # conversation-scoped, not limited to whatever page the client
            # has loaded. Reuses the same bidirectional recipient query as
            # message listing so 1:1 traffic in both directions is covered.
            try:
                dm_ids = [int(uid) for uid in dm_user_ids]
            except (ValueError, TypeError):
                return JsonResponse(
                    {
                        "result": "error",
                        "code": "INVALID_PARAMS",
                        "msg": "dm_user_ids must be a list of user IDs",
                    },
                    status=400,
                )
            base_query = _build_dm_recipient_query(user, dm_ids)
            if base_query is None:
                return JsonResponse(
                    {"result": "error", "code": "NOT_FOUND", "msg": "DM conversation not found"},
                    status=404,
                )
            unread_ids = list(
                UserMessage.objects.filter(
                    user_profile=user,
                    flags__andz=UserMessage.flags.read.mask,
                    message__in=base_query,
                ).values_list("message_id", flat=True)
            )
            count = 0
            # Chunked: do_update_message_flags bulk-updates and emits one
            # event per call; keep each call bounded like upstream's
            # MAX_MESSAGES_PER_UPDATE-style limits.
            for i in range(0, len(unread_ids), 1000):
                chunk_count, _ = do_update_message_flags(
                    user, "add", "read", unread_ids[i : i + 1000]
                )
                count += chunk_count
        elif message_ids:
            # Mark specific messages as read
            count, _ = do_update_message_flags(user, "add", "read", message_ids)
        else:
            return JsonResponse(
                {
                    "result": "error",
                    "code": "INVALID_PARAMS",
                    "msg": "Either stream_id, dm_user_ids or message_ids required",
                },
                status=400,
            )

        return JsonResponse(
            {
                "result": "success",
                "messages_marked": count,
            }
        )
    except Stream.DoesNotExist:
        return JsonResponse(
            {"result": "error", "code": "NOT_FOUND", "msg": "Stream not found"},
            status=404,
        )
    except Exception as e:
        logger.exception("Failed to mark messages as read")
        return JsonResponse(
            {"result": "error", "code": "MARK_READ_FAILED", "msg": str(e)},
            status=500,
        )


@csrf_exempt
@require_jwt_auth
@rate_limit(key_prefix="dm_write", limit=MESSAGES_WRITE_LIMIT)
def mute_dm_user(request: HttpRequest, user_id: int) -> HttpResponse:
    """Mute a user.

    Muted counterparts stay in the conversation list but are flagged
    `muted: true` there (clients style/deprioritize; hiding rows would
    orphan their unread counts). New messages from muted users arrive
    pre-read per Zulip semantics.

    POST /api/v1/dm/{user_id}/mute

    Response:
    {
        "result": "success",
        "msg": "User muted",
        "muted_user_id": 123
    }
    """
    if request.method != "POST":
        return JsonResponse(
            {"result": "error", "code": "METHOD_NOT_ALLOWED", "msg": "POST required"},
            status=405,
        )

    user: UserProfile = request.user_profile  # type: ignore[attr-defined]

    if user.id == user_id:
        return JsonResponse(
            {"result": "error", "code": "INVALID_PARAMS", "msg": "Cannot mute yourself"},
            status=400,
        )

    try:
        muted_user = access_user_by_id_including_cross_realm(
            user, user_id, allow_bots=True, allow_deactivated=True, for_admin=False
        )
    except JsonableError:
        return JsonResponse(
            {"result": "error", "code": "NOT_FOUND", "msg": "User not found"},
            status=404,
        )

    from django.db import IntegrityError
    from django.utils.timezone import now as timezone_now

    try:
        date_muted = timezone_now()
        do_mute_user(user, muted_user, date_muted)
        return JsonResponse(
            {
                "result": "success",
                "msg": "User muted",
                "muted_user_id": user_id,
            }
        )
    except IntegrityError:
        # Already muted - idempotent success
        return JsonResponse(
            {
                "result": "success",
                "msg": "User already muted",
                "muted_user_id": user_id,
            }
        )
    except Exception as e:
        logger.exception("Failed to mute user")
        return JsonResponse(
            {"result": "error", "code": "MUTE_FAILED", "msg": str(e)},
            status=500,
        )


@csrf_exempt
@require_jwt_auth
@rate_limit(key_prefix="dm_write", limit=MESSAGES_WRITE_LIMIT)
def unmute_dm_user(request: HttpRequest, user_id: int) -> HttpResponse:
    """Unmute a user (show their DMs in conversation list again).

    POST /api/v1/dm/{user_id}/unmute

    Response:
    {
        "result": "success",
        "msg": "User unmuted",
        "muted_user_id": 123
    }
    """
    if request.method != "POST":
        return JsonResponse(
            {"result": "error", "code": "METHOD_NOT_ALLOWED", "msg": "POST required"},
            status=405,
        )

    user: UserProfile = request.user_profile  # type: ignore[attr-defined]

    try:
        muted_user = access_user_by_id_including_cross_realm(
            user, user_id, allow_bots=True, allow_deactivated=True, for_admin=False
        )
    except JsonableError:
        return JsonResponse(
            {"result": "error", "code": "NOT_FOUND", "msg": "User not found"},
            status=404,
        )

    mute_object = get_mute_object(user, muted_user)

    if mute_object is None:
        # Already unmuted - idempotent success
        return JsonResponse(
            {
                "result": "success",
                "msg": "User not muted",
                "muted_user_id": user_id,
            }
        )

    try:
        do_unmute_user(mute_object)
        return JsonResponse(
            {
                "result": "success",
                "msg": "User unmuted",
                "muted_user_id": user_id,
            }
        )
    except Exception as e:
        logger.exception("Failed to unmute user")
        return JsonResponse(
            {"result": "error", "code": "UNMUTE_FAILED", "msg": str(e)},
            status=500,
        )


@require_jwt_auth
@rate_limit(key_prefix="messages_read", limit=MESSAGES_READ_LIMIT)
def get_unread_counts(request: HttpRequest) -> HttpResponse:
    """Get unread message counts for the current user.

    GET /api/v1/unread

    Response:
    {
        "result": "success",
        "unread_counts": {
            "stream:123": 5,                // per-stream total (all topics)
            "stream:123:topic:welcome": 2,  // per (stream, topic)
            "dm:9": 3,                      // 1:1 DM, keyed by counterpart user id
            "huddle:9,12,13": 1             // group DM, keyed by user_ids_string
        }
    }

    The key scheme is the web client's canonical `stream:`-prefixed format
    (useUnreadCounts). Counts are raw per-row numbers — mute state does not
    change them; any mute/aggregate policy is applied client-side. Topics
    that differ only by case are one entry (Zulip groups them
    case-insensitively, first-seen casing wins).
    """
    if request.method != "GET":
        return JsonResponse(
            {"result": "error", "code": "METHOD_NOT_ALLOWED", "msg": "GET required"},
            status=405,
        )

    user: UserProfile = request.user_profile  # type: ignore[attr-defined]

    try:
        from zerver.lib.message import aggregate_unread_data, get_raw_unread_data

        aggregated = aggregate_unread_data(get_raw_unread_data(user), allow_empty_topic_name=True)

        unread_counts: dict[str, int] = {}

        stream_totals: dict[int, int] = defaultdict(int)
        for stream_info in aggregated["streams"]:
            stream_id = stream_info["stream_id"]
            count = len(stream_info["unread_message_ids"])
            stream_totals[stream_id] += count
            unread_counts[f"stream:{stream_id}:topic:{stream_info['topic']}"] = count
        for stream_id, total in stream_totals.items():
            unread_counts[f"stream:{stream_id}"] = total

        for pm_info in aggregated["pms"]:
            unread_counts[f"dm:{pm_info['other_user_id']}"] = len(pm_info["unread_message_ids"])
        for group_info in aggregated["huddles"]:
            unread_counts[f"huddle:{group_info['user_ids_string']}"] = len(
                group_info["unread_message_ids"]
            )

        return JsonResponse(
            {
                "result": "success",
                "unread_counts": unread_counts,
            }
        )
    except Exception:
        # A failure here must be VISIBLE: the previous implementation
        # swallowed a guaranteed TypeError into {"result": "success",
        # "unread_counts": {}}, which hid the endpoint being broken for
        # every user with unreads.
        logger.exception("Failed to get unread counts")
        return JsonResponse(
            {"result": "error", "code": "UNREAD_FAILED", "msg": "Failed to get unread counts"},
            status=500,
        )


@csrf_exempt
@require_jwt_auth
@rate_limit(key_prefix="flags_write", limit=FLAGS_WRITE_LIMIT)
def update_flags(request: HttpRequest) -> HttpResponse:
    """POST /api/v1/messages/flags - Update message flags (read, starred, etc.).

    Thin wrapper over Zulip's own update_message_flags view (same idiom as
    update_flags_narrow below): seeds RequestNotes for JWT-authed requests,
    injects JSON bodies into request.POST so @typed_endpoint can parse them,
    then delegates. Form-encoded bodies (what the Flutter client sends) are
    already in request.POST and pass straight through. Delegating instead of
    reimplementing keeps validation, response shape, and the log_data summary
    identical to upstream.
    """
    if request.method != "POST":
        return JsonResponse(
            {"result": "error", "msg": "Method not allowed"},
            status=405,
        )

    user: UserProfile = request.user_profile  # type: ignore[attr-defined]

    # Ensure RequestNotes has a client set (required by Zulip views)
    from zerver.lib.request import RequestNotes

    notes = RequestNotes.get_notes(request)
    if notes.client is None:
        notes.client = get_client("nodl-web")
    if notes.log_data is None:
        # Direct view calls (tests) bypass the LogRequests middleware; the
        # upstream view asserts log_data exists before writing its summary.
        notes.log_data = {}

    # Parse JSON body and inject into request.POST for @typed_endpoint
    try:
        body = json.loads(request.body) if request.body else {}
    except (json.JSONDecodeError, ValueError):
        body = {}

    if body:
        request.POST = request.POST.copy()
        for key in ("flag", "messages", "op"):
            if key in body and key not in request.POST:
                val = body[key]
                request.POST[key] = (
                    json.dumps(val) if isinstance(val, list | dict | bool) else str(val)
                )

    from zerver.views.message_flags import update_message_flags

    try:
        return update_message_flags(request, user)
    except Exception as e:
        logger.warning("[nodl-flags] Error updating flags: %s", e)
        return JsonResponse(
            {"result": "error", "msg": str(e)},
            status=400,
        )


@csrf_exempt
@require_jwt_auth
@rate_limit(key_prefix="flags_write", limit=FLAGS_WRITE_LIMIT)
def update_flags_narrow(request: HttpRequest) -> HttpResponse:
    """POST /api/v1/messages/flags/narrow - Update flags for messages matching a narrow.

    Proxies to Zulip's update_message_flags_for_narrow with JWT auth.
    Injects body params into request.POST so the @typed_endpoint decorator can parse them.
    """
    if request.method != "POST":
        return JsonResponse(
            {"result": "error", "msg": "Method not allowed"},
            status=405,
        )

    user: UserProfile = request.user_profile  # type: ignore[attr-defined]

    # Ensure RequestNotes has a client set (required by Zulip views)
    from zerver.lib.request import RequestNotes

    notes = RequestNotes.get_notes(request)
    if notes.client is None:
        notes.client = get_client("nodl-web")

    # Parse JSON body and inject into request.POST for @typed_endpoint
    try:
        body = json.loads(request.body) if request.body else {}
    except (json.JSONDecodeError, ValueError):
        body = {}

    if body:
        request.POST = request.POST.copy()
        for key in ("anchor", "flag", "include_anchor", "narrow", "num_after", "num_before", "op"):
            if key in body and key not in request.POST:
                val = body[key]
                request.POST[key] = (
                    json.dumps(val) if isinstance(val, list | dict | bool) else str(val)
                )

    from zerver.views.message_flags import update_message_flags_for_narrow

    try:
        return update_message_flags_for_narrow(request, user)
    except Exception as e:
        logger.warning("[nodl-flags-narrow] Error updating flags: %s", e)
        return JsonResponse(
            {"result": "error", "msg": str(e)},
            status=400,
        )
