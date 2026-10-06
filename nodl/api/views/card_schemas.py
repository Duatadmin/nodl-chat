"""Structured assistant card payloads and the nodl-card:v1 encoding (AD-12).

Cards are embedded in message content as
``<!-- nodl-card:v1:{base64url-json} -->`` followed by human-readable
markdown fallback. Base64url is used deliberately: Zulip renders message
content through markdown/HTML escaping, and raw JSON inside HTML comments
proved escape-fragile (the legacy ``<!-- meeting:{json} -->`` parser needs
three escaping variants). Base64url survives every escaping layer intact.
The JSON is UTF-8 (not ASCII-escaped): a non-Latin character then costs
about 2.7 of Zulip's 10,000 message characters instead of 8. Every reader
decodes base64url → UTF-8 → JSON (mobile ``utf8.decode``, web
``TextDecoder``).

Unknown card types/versions must degrade to the markdown fallback on every
consumer — never hard-fail rendering.

Registered card types (``CARD_SCHEMAS``):

- ``ask_ai_answer`` — Ask AI result in a task stream.
- ``checkin`` — supervisor check-in with status buttons (``extra_options``:
  buttons only newer clients render, e.g. work verification's
  ``done_send_photos``; ``extra_response``: that answer on the answered
  card, whose ``response`` stays a key every client knows).
- ``call_event`` — voice-call lifecycle event in a DM.
- ``verification_request`` — work verification: the photo list a worker is
  asked to post in the task chat, updated in place as the check progresses.
- ``verification_result`` — work verification: one AI check run and the
  manager's review of it.

The verification cards follow contract v2 (nodl-backend
``docs/work-verification/M-PHASE-PLAN.md`` §3). Pydantic drops keys a schema
doesn't declare, so every field the backend sends is declared here.
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
    # Option keys only newer clients render (work verification:
    # "done_send_photos"). Kept apart from `options` because the clients
    # already shipped render every `options` key as a button.
    extra_options: list[str] = Field(default_factory=list)
    status: Literal["pending", "responded"] = "pending"
    response: str | None = None
    # The answer given with an `extra_options` key (e.g. "done_send_photos",
    # recorded as `response` "on_track"): newer clients show it instead.
    extra_response: str | None = None


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
    # A short name for the shot (newer scenario specs); clients fall back to
    # the instruction.
    title: str | None = None


class VerificationRequestedShot(BaseModel):
    """An extra photo someone asked for: the AI, or the manager (named)."""

    text: str
    by: Literal["ai", "manager"]
    # The manager who asked (None for the AI): clients say "you" to them.
    by_user_id: str | None = None
    by_name: str | None = None
    # "document" when the task can't be shown in photos (proof is asked for).
    kind: Literal["photo", "document"] = "photo"


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
    requested_shots: list[VerificationRequestedShot] = Field(default_factory=list)
    # True when `shots` is the generic per-archetype list (scenario failed).
    fallback_list: bool = False
    note: str | None = None
    # Why "Check my photos" is waiting: "no_new_photo" (tapped after the
    # manager's request with no new photo posted; cleared once a check finds
    # one). A plain string, so a reason added later never fails a card update.
    blocked_reason: str | None = None
    # Which photos the live-edge bar may count as new — exactly those a check
    # would take: human image attachments after this card, each photo (path
    # id) at the first message carrying it, new when that message is after
    # `photos_after_message_id`, sent at or after `photos_after_at` (epoch
    # seconds) and its path id is not in `checked_path_ids` (unset = no limit).
    photos_after_message_id: int | None = None
    photos_after_at: int | None = None
    checked_path_ids: list[str] = Field(default_factory=list)
    # The result card that shows where the check stands.
    result_message_id: int | None = None
    # Why a closed check closed: "closed_without_review", "task_archived",
    # "task_deleted" or "expired" (clients word it per language). A plain
    # string, so a reason added later never fails a card update.
    closed_reason: str | None = None
    # The backend shortened or emptied fields to fit a chat message; the full
    # check is in its session read.
    trimmed: bool = False


class VerificationCheck(BaseModel):
    """One check question and what the AI saw for it."""

    id: str
    question: str
    # A short statement of the check (newer scenario specs); clients fall
    # back to the question.
    label: str | None = None
    severity: str
    status: Literal["pass", "partly_seen", "fail", "cannot_see"]
    note: str
    photo_message_ids: list[int]
    # The photos that show it, by `VerificationPhoto.ref`.
    photo_refs: list[int] = Field(default_factory=list)


class VerificationPhoto(BaseModel):
    """One photo the AI saw, numbered from 1 in the order it saw them.

    Several photos of one composer send share a message id.
    """

    ref: int
    message_id: int
    path_id: str


class VerificationSkippedPhoto(BaseModel):
    """A photo the check could not read (too large, a format it can't read,
    a download that failed)."""

    message_id: int
    path_id: str
    # A plain string, so a reason added later never fails a card update.
    reason: str


class VerificationPerson(BaseModel):
    """Someone a card names."""

    id: str
    name: str


class VerificationInPerson(BaseModel):
    """Something photos can't confirm, and how to check it instead."""

    label: str
    how: str


class VerificationBriefPrefill(BaseModel):
    """Suggested text for the manager's ask-for-photo / send-back actions."""

    ask_for_photo: str | None
    send_back_reason: str | None


class VerificationBrief(BaseModel):
    """The manager's brief for one run."""

    headline: str
    # On a worker override ("send to manager anyway"): the first finding.
    override_reason: str | None = None
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
    in_person: list[VerificationInPerson]
    plan_context: list[str]


class VerificationReview(BaseModel):
    """The manager's decision on a run."""

    decision: Literal["approve", "reject", "request_more"]
    by_user_id: str
    by_name: str | None = None
    at: str
    # The reason, the photo asked for, or an approval's note.
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
    requested_shots: list[VerificationRequestedShot] = Field(default_factory=list)
    # Check questions no photo showed.
    not_shown: list[str] = Field(default_factory=list)
    brief: VerificationBrief
    # On a worker override: the note of whoever sent it on (`escalated_by`).
    worker_comment: str | None = None
    submitted_by: str
    submitted_by_name: str | None = None
    # On a worker override: who sent it on anyway — the worker or a crew-mate.
    escalated_by: str | None = None
    escalated_by_name: str | None = None
    reviewer_user_ids: list[str] = Field(default_factory=list)
    # The lead reviewer, when they have a name (`reviewer_user_ids` has all).
    reviewers: list[VerificationPerson] = Field(default_factory=list)
    # What "ask for more" asks for: proof on a task photos can't show.
    ask_kind: Literal["photo", "document"] = "photo"
    photos: list[VerificationPhoto] = Field(default_factory=list)
    photo_message_ids: list[int] = Field(default_factory=list)
    # Photos the check could not read; with no `photos`, none could be read.
    skipped_photos: list[VerificationSkippedPhoto] = Field(default_factory=list)
    review: VerificationReview | None = None
    # The backend shortened or emptied fields to fit a chat message; the full
    # check is in its session read.
    trimmed: bool = False


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
    raw = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
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
