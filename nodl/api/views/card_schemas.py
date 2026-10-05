"""Structured assistant card payloads and the nodl-card:v1 encoding (AD-12).

Cards are embedded in message content as
``<!-- nodl-card:v1:{base64url-json} -->`` followed by human-readable
markdown fallback. Base64url is used deliberately: Zulip renders message
content through markdown/HTML escaping, and raw JSON inside HTML comments
proved escape-fragile (the legacy ``<!-- meeting:{json} -->`` parser needs
three escaping variants). Base64url survives every escaping layer intact.

Unknown card types/versions must degrade to the markdown fallback on every
consumer — never hard-fail rendering.

Registered card types (``CARD_SCHEMAS``):

- ``ask_ai_answer`` — Ask AI result in a task stream.
- ``checkin`` — supervisor check-in with status buttons.
- ``call_event`` — voice-call lifecycle event in a DM.
- ``verification_request`` — work verification: the photo list a worker is
  asked to post in the task chat, updated in place as the check progresses.
- ``verification_result`` — work verification: one AI check run and the
  manager's review of it.
"""

import base64
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

NODL_CARD_VERSION = "v1"
NODL_CARD_PREFIX = f"<!-- nodl-card:{NODL_CARD_VERSION}:"
NODL_CARD_SUFFIX = " -->"


class AskAiAnswerCard(BaseModel):
    """Ask AI result posted back into the task stream (Story 2.3 AC5)."""

    task_id: str
    mode: Literal["assessment", "qa"]
    question: str | None = None
    answer: str
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    open_items: list[str] = Field(default_factory=list)
    covers_anchor: int | None = None
    generated_at: str | None = None


class CheckinCard(BaseModel):
    """Supervisor check-in card with status buttons (Story 2.5 AC2)."""

    checkin_id: str
    task_id: str
    workspace_id: str
    rule: str
    question: str
    due_date: str | None = None
    options: list[str] = Field(default_factory=lambda: ["on_track", "blocked", "needs_extension"])
    # Story 7.33: numeric one-tap remaining-duration chips (working days) on
    # statusing cards, and the recorded answer on the responded re-encode.
    remaining_options: list[int] = Field(default_factory=list)
    remaining_days: int | None = None
    status: Literal["pending", "responded"] = "pending"
    response: str | None = None


class CallEventCard(BaseModel):
    """Voice-call lifecycle event rendered inline in the caller↔callee DM.

    Posted by the call views/webhooks with the CALLER as the message sender,
    so it lands in the existing 1:1 thread (a bot sender would force a
    3-party group DM — see zerver.lib.recipient_users, which always adds the
    sender to the recipient set). Clients render direction/labels from
    caller_id vs. the viewing user.
    """

    call_id: str
    status: Literal["missed", "declined", "cancelled", "ended"]
    caller_id: int
    callee_id: int
    duration_seconds: int | None = None


class VerificationShot(BaseModel):
    """One photo the worker is asked to take."""

    id: str
    kind: str
    instruction: str


class VerificationRequestCard(BaseModel):
    """Work verification request, posted when a session opens.

    Updated in place as the session moves on; `shots` is empty while the
    photo list is still being prepared.
    """

    session_id: str
    task_id: str
    workspace_id: str
    status: Literal[
        "preparing",
        "collecting",
        "checking",
        "not_passed",
        "with_manager",
        "approved",
        "sent_back",
        "closed",
    ]
    shots: list[VerificationShot]
    # Extra photos the AI or the manager asked for.
    requested_shots: list[str] = Field(default_factory=list)
    # True when `shots` is the generic per-archetype list (scenario failed).
    fallback_list: bool = False
    note: str | None = None


class VerificationCheck(BaseModel):
    """One check question and what the AI saw for it."""

    id: str
    question: str
    severity: str
    status: Literal["pass", "partly_seen", "fail", "cannot_see"]
    note: str
    photo_message_ids: list[int]


class VerificationBriefPrefill(BaseModel):
    """Suggested text for the manager's ask-for-photo / send-back actions."""

    ask_for_photo: str | None
    send_back_reason: str | None


class VerificationBrief(BaseModel):
    """The manager's brief for one run."""

    headline: str
    summary: str
    issues: list[str]
    recommended_action: Literal[
        "approve",
        "approve_with_snag",
        "send_back",
        "ask_for_photo",
        "check_in_person",
        "review_photos",
    ]
    prefill: VerificationBriefPrefill
    in_person: list[str]
    plan_context: list[str]


class VerificationReview(BaseModel):
    """The manager's decision on a run."""

    decision: Literal["approve", "reject", "request_more"]
    by_user_id: str
    at: str
    comment: str | None


class VerificationResultCard(BaseModel):
    """Work verification result: one AI check run, then its review.

    Posted as a new message for every run (so a mention can ping someone);
    review actions update it in place.
    """

    session_id: str
    task_id: str
    workspace_id: str
    run_id: str | None
    # None when the AI check was unavailable.
    verdict: (
        Literal[
            "PASS",
            "PASS_WITH_NOTES",
            "PARTIAL",
            "FAIL",
            "NEED_MORE_EVIDENCE",
            "NOT_VERIFIABLE_FROM_PHOTO",
        ]
        | None
    )
    verdict_label: str
    # Session state after this run.
    state: str
    routed_by: (
        Literal[
            "ai_pass",
            "ai_partial",
            "worker_override",
            "not_photo_verifiable",
            "ai_unavailable",
        ]
        | None
    )
    checks: list[VerificationCheck]
    requested_shots: list[str] = Field(default_factory=list)
    # Check questions no photo showed.
    not_shown: list[str] = Field(default_factory=list)
    brief: VerificationBrief
    worker_comment: str | None = None
    submitted_by: str
    reviewer_user_ids: list[str] = Field(default_factory=list)
    photo_message_ids: list[int] = Field(default_factory=list)
    review: VerificationReview | None = None


CARD_SCHEMAS: dict[str, type[BaseModel]] = {
    "ask_ai_answer": AskAiAnswerCard,
    "checkin": CheckinCard,
    "call_event": CallEventCard,
    "verification_request": VerificationRequestCard,
    "verification_result": VerificationResultCard,
}


class UnknownCardTypeError(ValueError):
    """Raised when a card_type has no registered schema."""


def validate_card_payload(card_type: str, payload: dict[str, Any]) -> BaseModel:
    """Validate a card payload against its registered schema.

    Raises UnknownCardTypeError for unregistered types and pydantic
    ValidationError for schema violations.
    """
    schema = CARD_SCHEMAS.get(card_type)
    if schema is None:
        raise UnknownCardTypeError(f"Unknown card_type: {card_type}")
    return schema(**payload)


def encode_card(card_type: str, payload: dict[str, Any]) -> str:
    """Encode a validated card payload as a nodl-card:v1 HTML comment."""
    validated = validate_card_payload(card_type, payload)
    envelope = {"card_type": card_type, "payload": validated.model_dump()}
    raw = json.dumps(envelope, separators=(",", ":"), ensure_ascii=True)
    encoded = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    return f"{NODL_CARD_PREFIX}{encoded}{NODL_CARD_SUFFIX}"


def build_card_message_content(
    card_type: str, payload: dict[str, Any], fallback_markdown: str
) -> str:
    """Compose full message content: encoded card + markdown fallback (AD-12)."""
    return f"{encode_card(card_type, payload)}\n\n{fallback_markdown}"


__all__ = [
    "AskAiAnswerCard",
    "CallEventCard",
    "CheckinCard",
    "VerificationRequestCard",
    "VerificationResultCard",
    "CARD_SCHEMAS",
    "UnknownCardTypeError",
    "ValidationError",
    "validate_card_payload",
    "encode_card",
    "build_card_message_content",
]
