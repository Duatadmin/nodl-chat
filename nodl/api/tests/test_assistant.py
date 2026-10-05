"""Unit tests for the internal assistant endpoints (Epic 2, Story 2.1).

Also covers the work-verification additions: the task-stream attachments
read and the verification card schemas. Scoping, the image filter and
deletion run against real rows (the query is the thing under test);
validation paths stay mocked.
"""

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.handlers.wsgi import WSGIRequest
from django.test import RequestFactory, TestCase, override_settings
from pydantic import ValidationError

from nodl.api.views.assistant import (
    ensure_assistant_bot,
    get_task_stream_attachments,
    get_task_stream_messages,
    send_assistant_message,
)
from nodl.api.views.card_schemas import (
    CARD_SCHEMAS,
    NODL_CARD_PREFIX,
    UnknownCardTypeError,
    VerificationRequestCard,
    VerificationResultCard,
    build_card_message_content,
    encode_card,
    validate_card_payload,
)
from nodl.extensions.models import (
    NodlRealmExtension,
    NodlRealmUserExtension,
    NodlTaskStreamExtension,
    SyncStatus,
)
from zerver.actions.create_realm import do_create_realm
from zerver.actions.create_user import do_create_user
from zerver.actions.message_delete import do_delete_messages
from zerver.actions.message_send import check_send_message
from zerver.actions.streams import bulk_add_subscriptions
from zerver.lib.cache import bounce_key_prefix_for_testing
from zerver.lib.streams import create_stream_if_needed
from zerver.models import Attachment, Message, Realm, Stream, UserProfile
from zerver.models.clients import get_client


def _decode_card(content: str) -> dict[str, Any]:
    marker = content.split("\n", 1)[0]
    b64 = marker[len(NODL_CARD_PREFIX) : -len(" -->")]
    envelope: dict[str, Any] = json.loads(base64.urlsafe_b64decode(b64.encode("ascii")))
    return envelope


def _verification_request_payload(**overrides: Any) -> dict[str, Any]:
    return {
        "session_id": str(uuid.uuid4()),
        "task_id": str(uuid.uuid4()),
        "workspace_id": str(uuid.uuid4()),
        "status": "collecting",
        "shots": [
            {"id": "s1", "kind": "overview", "instruction": "Whole wall, from the door"},
        ],
        **overrides,
    }


def _verification_result_payload(**overrides: Any) -> dict[str, Any]:
    return {
        "session_id": str(uuid.uuid4()),
        "task_id": str(uuid.uuid4()),
        "workspace_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "verdict": "PARTIAL",
        "verdict_label": "Partly done",
        "state": "awaiting_manager",
        "routed_by": "ai_partial",
        "checks": [
            {
                "id": "c1",
                "question": "Are all tiles grouted?",
                "severity": "major",
                "status": "partly_seen",
                "note": "Left corner not visible",
                "photo_message_ids": [101, 102],
            }
        ],
        "brief": {
            "headline": "Grout mostly done",
            "summary": "Two of three walls are grouted.",
            "issues": ["Left corner not shown"],
            "recommended_action": "ask_for_photo",
            "prefill": {"ask_for_photo": "Photo of the left corner", "send_back_reason": None},
            "in_person": [],
            "plan_context": ["Waterproofing passed on 10-01"],
        },
        "submitted_by": str(uuid.uuid4()),
        **overrides,
    }


class TestCardEncoding(TestCase):
    def test_encode_round_trip(self) -> None:
        payload = {
            "task_id": str(uuid.uuid4()),
            "mode": "assessment",
            "answer": "On track overall.",
            "risks": ["Tile delivery is late"],
        }

        encoded = encode_card("ask_ai_answer", payload)

        self.assertTrue(encoded.startswith(NODL_CARD_PREFIX))
        self.assertTrue(encoded.endswith(" -->"))
        b64 = encoded[len(NODL_CARD_PREFIX) : -len(" -->")]
        envelope = json.loads(base64.urlsafe_b64decode(b64.encode("ascii")))
        self.assertEqual(envelope["card_type"], "ask_ai_answer")
        self.assertEqual(envelope["payload"]["answer"], "On track overall.")
        self.assertEqual(envelope["payload"]["risks"], ["Tile delivery is late"])

    def test_unknown_card_type_rejected(self) -> None:
        with self.assertRaises(UnknownCardTypeError):
            validate_card_payload("not_a_card", {})

    def test_invalid_payload_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            validate_card_payload("checkin", {"checkin_id": "x"})  # missing fields

    def test_message_content_keeps_markdown_fallback(self) -> None:
        content = build_card_message_content(
            "checkin",
            {
                "checkin_id": str(uuid.uuid4()),
                "task_id": str(uuid.uuid4()),
                "workspace_id": str(uuid.uuid4()),
                "rule": "deadline_approaching",
                "question": "Is this task on track?",
            },
            "**Check-in**: Is this task on track?",
        )

        self.assertTrue(content.startswith(NODL_CARD_PREFIX))
        self.assertIn("**Check-in**: Is this task on track?", content)


class TestAssistantAuth(TestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()

    def test_read_requires_service_auth(self) -> None:
        request = self.factory.get(f"/api/v1/internal/task-streams/{uuid.uuid4()}/messages")
        request.is_service_request = False

        response = get_task_stream_messages(request, uuid.uuid4())

        self.assertEqual(response.status_code, 401)

    def test_send_requires_service_auth(self) -> None:
        request = self.factory.post("/api/v1/internal/messages/send")
        request.is_service_request = False

        response = send_assistant_message(request)

        self.assertEqual(response.status_code, 401)


class TestReadTaskStreamMessages(TestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.task_id = uuid.uuid4()
        self.workspace_id = str(uuid.uuid4())

    def _request(self, query: str):
        request = self.factory.get(f"/api/v1/internal/task-streams/{self.task_id}/messages?{query}")
        request.is_service_request = True
        return request

    def test_workspace_id_required(self) -> None:
        response = get_task_stream_messages(self._request(""), self.task_id)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["code"], "VALIDATION_ERROR")

    @patch("nodl.api.views.assistant._get_task_stream_extension", return_value=None)
    def test_unknown_task_is_404(self, _mock_ext: MagicMock) -> None:
        response = get_task_stream_messages(
            self._request(f"workspace_id={self.workspace_id}"), self.task_id
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.content)["code"], "TASK_STREAM_NOT_FOUND")

    @patch("nodl.api.views.assistant.NodlRealmUserExtension.objects")
    @patch("nodl.api.views.assistant.Message.objects")
    @patch("nodl.api.views.assistant._get_task_stream_extension")
    def test_returns_messages_with_human_anchor_metadata(
        self,
        mock_get_extension: MagicMock,
        mock_message_objects: MagicMock,
        mock_realm_user_objects: MagicMock,
    ) -> None:
        extension = MagicMock(zulip_realm_id=1)
        extension.zulip_stream = MagicMock(recipient_id=7, deactivated=True)
        mock_get_extension.return_value = extension

        supabase_id = uuid.uuid4()
        human = MagicMock(
            id=10,
            sender_id=11,
            date_sent=datetime(2026, 6, 11, 12, 0, tzinfo=UTC),
            content="tiles arrived",
        )
        human.sender.full_name = "Person"
        human.sender.is_bot = False
        human.topic_name.return_value = "task"
        bot = MagicMock(
            id=12,
            sender_id=99,
            date_sent=datetime(2026, 6, 11, 12, 5, tzinfo=UTC),
            content="<!-- nodl-card:v1:abc -->\n\nAssessment",
        )
        bot.sender.full_name = "Nodle Assistant"
        bot.sender.is_bot = True
        bot.topic_name.return_value = "task"

        stream_messages = mock_message_objects.filter.return_value
        page_query = (
            stream_messages.filter.return_value.select_related.return_value.order_by.return_value
        )
        page_query.__getitem__.return_value = [human, bot]
        # AD-10: bot message id 12 is newest, but the human anchor stays at 10.
        # Both aggregates run over human messages only (anchor, then edit time).
        stream_messages.exclude.return_value.aggregate.side_effect = [
            {"id__max": 10},
            {"last_edit_time__max": None},
        ]
        mock_realm_user_objects.filter.return_value.values_list.return_value = [(11, supabase_id)]

        response = get_task_stream_messages(
            self._request(f"workspace_id={self.workspace_id}&anchor=0"), self.task_id
        )

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(len(data["messages"]), 2)
        self.assertEqual(data["messages"][0]["sender"]["supabase_user_id"], str(supabase_id))
        self.assertFalse(data["messages"][0]["sender"]["is_bot"])
        self.assertTrue(data["messages"][1]["sender"]["is_bot"])
        self.assertEqual(data["latest_human_anchor"], 10)
        self.assertIsNone(data["last_edit_time"])
        self.assertTrue(data["found_newest"])
        self.assertTrue(data["stream_archived"])


class TestSendAssistantMessage(TestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.payload = {
            "workspace_id": str(uuid.uuid4()),
            "task_id": str(uuid.uuid4()),
            "topic": "task",
            "content": "Assessment: on track.",
        }

    def _request(self, payload: dict):
        request = self.factory.post(
            "/api/v1/internal/messages/send",
            data=json.dumps(payload),
            content_type="application/json",
        )
        request.is_service_request = True
        return request

    @patch("nodl.api.views.assistant._get_task_stream_extension", return_value=None)
    def test_unknown_task_is_404(self, _mock_ext: MagicMock) -> None:
        response = send_assistant_message(self._request(self.payload))

        self.assertEqual(response.status_code, 404)

    @patch("nodl.api.views.assistant._get_task_stream_extension")
    def test_archived_stream_is_409(self, mock_get_extension: MagicMock) -> None:
        extension = MagicMock()
        extension.zulip_stream.deactivated = True
        mock_get_extension.return_value = extension

        response = send_assistant_message(self._request(self.payload))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.content)["code"], "STREAM_ARCHIVED")

    @patch("nodl.api.views.assistant._get_task_stream_extension")
    def test_invalid_card_payload_is_400(self, mock_get_extension: MagicMock) -> None:
        extension = MagicMock()
        extension.zulip_stream.deactivated = False
        mock_get_extension.return_value = extension

        payload = {**self.payload, "card_type": "checkin", "card_payload": {"bad": True}}
        response = send_assistant_message(self._request(payload))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["code"], "INVALID_CARD")

    @patch("nodl.api.views.assistant.get_client")
    @patch("nodl.api.views.assistant.check_send_message")
    @patch("nodl.api.views.assistant.bulk_add_subscriptions")
    @patch("nodl.api.views.assistant.Subscription.objects")
    @patch("nodl.api.views.assistant.ensure_assistant_bot")
    @patch("nodl.api.views.assistant.NodlRealmExtension.objects")
    @patch("nodl.api.views.assistant._get_task_stream_extension")
    def test_sends_card_as_bot_and_subscribes_if_needed(
        self,
        mock_get_extension: MagicMock,
        mock_realm_ext_objects: MagicMock,
        mock_ensure_bot: MagicMock,
        mock_subscription_objects: MagicMock,
        mock_subscribe: MagicMock,
        mock_send: MagicMock,
        _mock_get_client: MagicMock,
    ) -> None:
        extension = MagicMock()
        extension.zulip_stream = MagicMock(id=42, deactivated=False)
        mock_get_extension.return_value = extension
        bot = MagicMock(id=99)
        mock_ensure_bot.return_value = bot
        mock_subscription_objects.filter.return_value.exists.return_value = False
        mock_send.return_value = MagicMock(message_id=123)

        payload = {
            **self.payload,
            "card_type": "ask_ai_answer",
            "card_payload": {
                "task_id": self.payload["task_id"],
                "mode": "assessment",
                "answer": "On track.",
            },
        }
        response = send_assistant_message(self._request(payload))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["message_id"], 123)
        mock_subscribe.assert_called_once()
        sent_content = mock_send.call_args.kwargs["message_content"]
        self.assertTrue(sent_content.startswith("<!-- nodl-card:v1:"))
        self.assertIn("Assessment: on track.", sent_content)
        self.assertEqual(mock_send.call_args.kwargs["sender"], bot)
        self.assertEqual(mock_send.call_args.kwargs["message_to"], [42])


class TestUpdateAssistantCard(TestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.payload = {
            "workspace_id": str(uuid.uuid4()),
            "task_id": str(uuid.uuid4()),
            "content": "**Check-in**: answered",
            "card_type": "checkin",
            "card_payload": {
                "checkin_id": str(uuid.uuid4()),
                "task_id": str(uuid.uuid4()),
                "workspace_id": str(uuid.uuid4()),
                "rule": "deadline_approaching",
                "question": "Is this task on track?",
                "status": "responded",
                "response": "blocked",
            },
        }

    def _request(self, payload: dict, message_id: int = 77):
        request = self.factory.post(
            f"/api/v1/internal/messages/{message_id}/update-card",
            data=json.dumps(payload),
            content_type="application/json",
        )
        request.is_service_request = True
        return request

    def test_requires_service_auth(self) -> None:
        from nodl.api.views.assistant import update_assistant_card

        request = self.factory.post("/api/v1/internal/messages/77/update-card")
        request.is_service_request = False

        self.assertEqual(update_assistant_card(request, 77).status_code, 401)

    @patch("nodl.api.views.assistant._get_task_stream_extension", return_value=None)
    def test_unknown_task_is_404(self, _mock_ext: MagicMock) -> None:
        from nodl.api.views.assistant import update_assistant_card

        response = update_assistant_card(self._request(self.payload), 77)

        self.assertEqual(response.status_code, 404)

    @patch("nodl.api.views.assistant.do_update_message")
    @patch("nodl.api.views.assistant.MentionData")
    @patch("nodl.api.views.assistant.MentionBackend")
    @patch("nodl.api.views.assistant.render_message_markdown")
    @patch("nodl.api.views.assistant.Message.objects")
    @patch("nodl.api.views.assistant.ensure_assistant_bot")
    @patch("nodl.api.views.assistant.NodlRealmExtension.objects")
    @patch("nodl.api.views.assistant.transaction.atomic")
    @patch("nodl.api.views.assistant._get_task_stream_extension")
    def test_rewrites_card_as_bot(
        self,
        mock_get_extension: MagicMock,
        mock_atomic: MagicMock,
        mock_realm_ext_objects: MagicMock,
        mock_ensure_bot: MagicMock,
        mock_message_objects: MagicMock,
        mock_render: MagicMock,
        _mock_backend: MagicMock,
        _mock_mention: MagicMock,
        mock_update: MagicMock,
    ) -> None:
        from nodl.api.views.assistant import update_assistant_card

        mock_atomic.return_value.__enter__ = MagicMock()
        mock_atomic.return_value.__exit__ = MagicMock(return_value=False)
        extension = MagicMock()
        extension.zulip_stream = MagicMock(recipient_id=7)
        mock_get_extension.return_value = extension
        bot = MagicMock(id=99)
        mock_ensure_bot.return_value = bot
        message = MagicMock(sender_id=99, content="old")
        message.topic_name.return_value = "task"
        locked_qs = mock_message_objects.select_for_update.return_value
        lookup = locked_qs.select_related.return_value.filter.return_value
        lookup.first.return_value = message

        response = update_assistant_card(self._request(self.payload), 77)

        self.assertEqual(response.status_code, 200, response.content)
        sent_content = mock_update.call_args.kwargs["message_edit_request"].content
        self.assertTrue(sent_content.startswith("<!-- nodl-card:v1:"))
        self.assertIn("**Check-in**: answered", sent_content)
        self.assertEqual(mock_update.call_args.kwargs["user_profile"], bot)

    @patch("nodl.api.views.assistant.transaction.atomic")
    @patch("nodl.api.views.assistant.NodlRealmExtension.objects")
    @patch("nodl.api.views.assistant.ensure_assistant_bot")
    @patch("nodl.api.views.assistant.Message.objects")
    @patch("nodl.api.views.assistant._get_task_stream_extension")
    def test_rejects_non_bot_messages(
        self,
        mock_get_extension: MagicMock,
        mock_message_objects: MagicMock,
        mock_ensure_bot: MagicMock,
        _mock_realm_ext: MagicMock,
        mock_atomic: MagicMock,
    ) -> None:
        from nodl.api.views.assistant import update_assistant_card

        mock_atomic.return_value.__enter__ = MagicMock()
        mock_atomic.return_value.__exit__ = MagicMock(return_value=False)
        extension = MagicMock()
        extension.zulip_stream = MagicMock(recipient_id=7)
        mock_get_extension.return_value = extension
        mock_ensure_bot.return_value = MagicMock(id=99)
        human_message = MagicMock(sender_id=11)
        locked_qs = mock_message_objects.select_for_update.return_value
        lookup = locked_qs.select_related.return_value.filter.return_value
        lookup.first.return_value = human_message

        response = update_assistant_card(self._request(self.payload), 77)

        self.assertEqual(response.status_code, 403)


class TestVerificationCardSchemas(TestCase):
    def test_both_verification_cards_are_registered(self) -> None:
        self.assertIs(CARD_SCHEMAS["verification_request"], VerificationRequestCard)
        self.assertIs(CARD_SCHEMAS["verification_result"], VerificationResultCard)

    def test_request_card_defaults_and_round_trip(self) -> None:
        payload = _verification_request_payload(status="preparing", shots=[])

        envelope = _decode_card(encode_card("verification_request", payload))

        self.assertEqual(envelope["card_type"], "verification_request")
        self.assertEqual(envelope["payload"]["shots"], [])
        self.assertEqual(envelope["payload"]["requested_shots"], [])
        self.assertFalse(envelope["payload"]["fallback_list"])
        self.assertIsNone(envelope["payload"]["note"])

    def test_request_card_keeps_nested_shots(self) -> None:
        envelope = _decode_card(
            encode_card("verification_request", _verification_request_payload())
        )

        self.assertEqual(
            envelope["payload"]["shots"],
            [{"id": "s1", "kind": "overview", "instruction": "Whole wall, from the door"}],
        )

    def test_request_card_rejects_unknown_status(self) -> None:
        with self.assertRaises(ValidationError):
            validate_card_payload(
                "verification_request", _verification_request_payload(status="pending")
            )

    def test_request_card_requires_shots(self) -> None:
        payload = _verification_request_payload()
        del payload["shots"]

        with self.assertRaises(ValidationError):
            validate_card_payload("verification_request", payload)

    def test_result_card_round_trips_nested_objects(self) -> None:
        review = {
            "decision": "request_more",
            "by_user_id": str(uuid.uuid4()),
            "at": "2026-10-05T12:00:00Z",
            "comment": None,
        }
        payload = _verification_result_payload(review=review, photo_message_ids=[101, 102])

        envelope = _decode_card(encode_card("verification_result", payload))

        card = envelope["payload"]
        self.assertEqual(card["checks"][0]["status"], "partly_seen")
        self.assertEqual(card["checks"][0]["photo_message_ids"], [101, 102])
        self.assertEqual(card["brief"]["prefill"]["ask_for_photo"], "Photo of the left corner")
        self.assertIsNone(card["brief"]["prefill"]["send_back_reason"])
        self.assertEqual(card["review"], review)
        self.assertEqual(card["requested_shots"], [])
        self.assertEqual(card["not_shown"], [])
        self.assertEqual(card["reviewer_user_ids"], [])
        self.assertIsNone(card["worker_comment"])

    def test_result_card_allows_unavailable_ai(self) -> None:
        payload = _verification_result_payload(
            run_id=None, verdict=None, routed_by="ai_unavailable", checks=[]
        )

        card = validate_card_payload("verification_result", payload)

        assert isinstance(card, VerificationResultCard)
        self.assertIsNone(card.verdict)
        self.assertIsNone(card.review)

    def test_result_card_rejects_bad_check_status(self) -> None:
        payload = _verification_result_payload()
        payload["checks"][0]["status"] = "unclear"

        with self.assertRaises(ValidationError):
            validate_card_payload("verification_result", payload)

    def test_result_card_requires_brief_and_nullable_fields(self) -> None:
        for missing in ("brief", "run_id", "verdict", "routed_by", "submitted_by"):
            payload = _verification_result_payload()
            del payload[missing]
            with self.subTest(missing=missing), self.assertRaises(ValidationError):
                validate_card_payload("verification_result", payload)


class TestTaskStreamAttachmentsValidation(TestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.task_id = uuid.uuid4()
        self.workspace_id = str(uuid.uuid4())

    def _request(self, query: str) -> WSGIRequest:
        request = self.factory.get(
            f"/api/v1/internal/task-streams/{self.task_id}/attachments?{query}"
        )
        request.is_service_request = True
        return request

    def test_requires_service_auth(self) -> None:
        request = self.factory.get(f"/api/v1/internal/task-streams/{self.task_id}/attachments")
        request.is_service_request = False

        self.assertEqual(get_task_stream_attachments(request, self.task_id).status_code, 401)

    def test_get_only(self) -> None:
        request = self.factory.post(f"/api/v1/internal/task-streams/{self.task_id}/attachments")
        request.is_service_request = True

        self.assertEqual(get_task_stream_attachments(request, self.task_id).status_code, 405)

    def test_workspace_id_must_be_a_uuid(self) -> None:
        for query in ("", "workspace_id=", "workspace_id=not-a-uuid"):
            with self.subTest(query=query):
                response = get_task_stream_attachments(self._request(query), self.task_id)

                self.assertEqual(response.status_code, 400)
                self.assertEqual(json.loads(response.content)["code"], "VALIDATION_ERROR")

    def test_bad_ints_are_400(self) -> None:
        for query in ("after_message_id=abc", "limit=1.5"):
            with self.subTest(query=query):
                response = get_task_stream_attachments(
                    self._request(f"workspace_id={self.workspace_id}&{query}"), self.task_id
                )

                self.assertEqual(response.status_code, 400)
                self.assertEqual(json.loads(response.content)["code"], "INVALID_PARAMS")

    @patch("nodl.api.views.assistant._get_task_stream_extension", return_value=None)
    def test_unknown_task_is_404(self, _mock_ext: MagicMock) -> None:
        response = get_task_stream_attachments(
            self._request(f"workspace_id={self.workspace_id}"), self.task_id
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.content)["code"], "TASK_STREAM_NOT_FOUND")


class TaskStreamTestCase(TestCase):
    """Real realm, users and private task stream, built the way task-stream sync does."""

    def setUp(self) -> None:
        super().setUp()
        # Per-test cache keys, as ZulipTestCase does: cached rows such as the
        # sending Client must not outlive the transaction that created them.
        bounce_key_prefix_for_testing(self.id())

    def _make_realm(self) -> tuple[Realm, uuid.UUID]:
        workspace_id = uuid.uuid4()
        realm = do_create_realm(
            string_id=str(workspace_id)[:20].lower(),
            name="Evidence Test",
            description="",
            org_type=Realm.ORG_TYPES["business"]["id"],
            create_zulip_discussion_channel=False,
        )
        NodlRealmExtension.objects.create(
            zulip_realm=realm,
            nodl_workspace_id=workspace_id,
            sync_status=SyncStatus.SYNCED,
        )
        return realm, workspace_id

    def _make_user(self, realm: Realm, full_name: str) -> tuple[UserProfile, uuid.UUID]:
        user = do_create_user(
            email=f"{uuid.uuid4().hex[:12]}@example.com",
            password=None,
            realm=realm,
            full_name=full_name,
            acting_user=None,
        )
        supabase_user_id = uuid.uuid4()
        NodlRealmUserExtension.objects.create(
            zulip_realm=realm, zulip_user=user, supabase_user_id=supabase_user_id
        )
        return user, supabase_user_id

    def _make_task_stream(
        self, realm: Realm, workspace_id: uuid.UUID, subscribers: list[UserProfile]
    ) -> tuple[Stream, uuid.UUID]:
        task_id = uuid.uuid4()
        stream, _created = create_stream_if_needed(
            realm,
            f"task-{task_id}",
            invite_only=True,
            history_public_to_subscribers=False,
            acting_user=None,
        )
        NodlTaskStreamExtension.objects.create(
            zulip_realm=realm,
            zulip_stream=stream,
            nodl_workspace_id=workspace_id,
            nodl_task_id=task_id,
        )
        bulk_add_subscriptions(
            realm, [stream], subscribers, from_user_creation=True, acting_user=None
        )
        return stream, task_id

    def _post(
        self, sender: UserProfile, stream: Stream, files: list[tuple[str, str | None]]
    ) -> int:
        """Post a message linking freshly uploaded files; Zulip claims them on send."""
        links = []
        for file_name, content_type in files:
            path_id = f"{sender.realm_id}/ab/{uuid.uuid4().hex[:24]}/{file_name}"
            Attachment.objects.create(
                file_name=file_name,
                path_id=path_id,
                owner=sender,
                realm=sender.realm,
                size=2048,
                content_type=content_type,
            )
            links.append(f"[{file_name}](/user_uploads/{path_id})")
        result = check_send_message(
            sender=sender,
            client=get_client("nodl-test"),
            recipient_type_name="stream",
            message_to=[stream.id],
            topic_name="task",
            message_content="\n".join(links) or "On it.",
            realm=sender.realm,
        )
        return result.message_id


class TestTaskStreamAttachments(TaskStreamTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.factory = RequestFactory()
        self.realm, self.workspace_id = self._make_realm()
        self.worker, self.worker_supabase_id = self._make_user(self.realm, "Wayan Worker")
        self.stream, self.task_id = self._make_task_stream(
            self.realm, self.workspace_id, [self.worker]
        )

    def _get(self, **params: Any) -> dict[str, Any]:
        query = "&".join(
            f"{key}={value}" for key, value in {"workspace_id": self.workspace_id, **params}.items()
        )
        request = self.factory.get(
            f"/api/v1/internal/task-streams/{self.task_id}/attachments?{query}"
        )
        request.is_service_request = True
        response = get_task_stream_attachments(request, self.task_id)
        self.assertEqual(response.status_code, 200, response.content)
        data: dict[str, Any] = json.loads(response.content)
        return data

    def test_lists_photos_after_anchor_in_order_with_senders(self) -> None:
        anchor = self._post(self.worker, self.stream, [("before.jpg", "image/jpeg")])
        first = self._post(
            self.worker, self.stream, [("wall.jpg", "image/jpeg"), ("spec.pdf", "application/pdf")]
        )
        self._post(self.worker, self.stream, [])  # text only
        bot = ensure_assistant_bot(
            self.realm, NodlRealmExtension.objects.get(zulip_realm=self.realm)
        )
        bulk_add_subscriptions(
            self.realm, [self.stream], [bot], from_user_creation=True, acting_user=None
        )
        second = self._post(bot, self.stream, [("card.png", "image/png")])

        data = self._get(after_message_id=anchor)

        self.assertFalse(data["stream_archived"])
        self.assertTrue(data["found_newest"])
        rows = data["attachments"]
        self.assertEqual(
            [(row["message_id"], row["file_name"]) for row in rows],
            [(first, "wall.jpg"), (second, "card.png")],
        )
        wall = rows[0]
        message = Message.objects.get(id=first)
        self.assertEqual(wall["timestamp"], int(message.date_sent.timestamp()))
        self.assertEqual(
            wall["sender"],
            {
                "zulip_user_id": self.worker.id,
                "supabase_user_id": str(self.worker_supabase_id),
                "full_name": "Wayan Worker",
                "is_bot": False,
            },
        )
        self.assertTrue(wall["path_id"].endswith("/wall.jpg"))
        self.assertEqual(wall["content_type"], "image/jpeg")
        self.assertEqual(wall["size"], 2048)
        # Dev/test settings use local uploads: nothing to presign.
        self.assertIsNotNone(settings.LOCAL_UPLOADS_DIR)
        self.assertIsNone(wall["presigned_url"])
        self.assertTrue(rows[1]["sender"]["is_bot"])
        self.assertIsNone(rows[1]["sender"]["supabase_user_id"])

    def test_image_filter_uses_type_then_extension_for_untyped_uploads(self) -> None:
        self._post(
            self.worker,
            self.stream,
            [
                ("a.jpg", "image/jpeg"),
                ("b.HEIC", "application/octet-stream"),
                ("c.png", None),
                ("d.webp", "image/webp"),
                ("e.pdf", "application/pdf"),
                ("f.txt", None),
                ("g.bin", "application/octet-stream"),
                ("h.jpg", "video/mp4"),  # a typed non-image is trusted over its name
            ],
        )

        data = self._get()

        self.assertEqual(
            [row["file_name"] for row in data["attachments"]],
            ["a.jpg", "b.HEIC", "c.png", "d.webp"],
        )

    def test_scoped_to_this_task_stream(self) -> None:
        other_stream, _other_task = self._make_task_stream(
            self.realm, self.workspace_id, [self.worker]
        )
        self._post(self.worker, other_stream, [("other.jpg", "image/jpeg")])
        mine = self._post(self.worker, self.stream, [("mine.jpg", "image/jpeg")])

        data = self._get()

        self.assertEqual([row["message_id"] for row in data["attachments"]], [mine])

    def test_deleted_message_photos_are_not_listed(self) -> None:
        kept = self._post(self.worker, self.stream, [("kept.jpg", "image/jpeg")])
        deleted = self._post(self.worker, self.stream, [("deleted.jpg", "image/jpeg")])
        do_delete_messages(self.realm, [Message.objects.get(id=deleted)], acting_user=None)

        data = self._get()

        self.assertEqual([row["message_id"] for row in data["attachments"]], [kept])

    def test_archived_stream_is_still_readable(self) -> None:
        posted = self._post(self.worker, self.stream, [("done.jpg", "image/jpeg")])
        self.stream.deactivated = True
        self.stream.save(update_fields=["deactivated"])

        data = self._get()

        self.assertTrue(data["stream_archived"])
        self.assertEqual([row["message_id"] for row in data["attachments"]], [posted])

    def test_workspace_mismatch_is_404(self) -> None:
        request = self.factory.get(
            f"/api/v1/internal/task-streams/{self.task_id}/attachments?workspace_id={uuid.uuid4()}"
        )
        request.is_service_request = True

        response = get_task_stream_attachments(request, self.task_id)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.content)["code"], "TASK_STREAM_NOT_FOUND")

    def test_pages_never_split_a_message(self) -> None:
        first = self._post(self.worker, self.stream, [("1a.jpg", None), ("1b.jpg", None)])
        second = self._post(self.worker, self.stream, [("2a.jpg", None), ("2b.jpg", None)])

        page = self._get(limit=3)

        self.assertFalse(page["found_newest"])
        self.assertEqual([row["message_id"] for row in page["attachments"]], [first, first])

        rest = self._get(limit=3, after_message_id=first)

        self.assertTrue(rest["found_newest"])
        self.assertEqual([row["message_id"] for row in rest["attachments"]], [second, second])

    def test_message_larger_than_limit_still_splits(self) -> None:
        first = self._post(self.worker, self.stream, [("1a.jpg", None), ("1b.jpg", None)])

        page = self._get(limit=1)

        self.assertFalse(page["found_newest"])
        self.assertEqual([row["message_id"] for row in page["attachments"]], [first])

    @override_settings(LOCAL_UPLOADS_DIR=None)
    @patch("nodl.api.views.assistant.presign_attachment", return_value="https://r2/signed")
    def test_presigns_with_the_evidence_ttl_on_s3(self, mock_presign: MagicMock) -> None:
        self._post(self.worker, self.stream, [("wall.jpg", "image/jpeg")])

        data = self._get()

        [row] = data["attachments"]
        self.assertEqual(row["presigned_url"], "https://r2/signed")
        mock_presign.assert_called_once_with(row["path_id"], settings.NODL_EVIDENCE_PRESIGN_TTL)
        self.assertEqual(settings.NODL_EVIDENCE_PRESIGN_TTL, 600)
