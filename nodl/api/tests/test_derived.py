"""Derived content across message edits (W0 P1, chat half).

- The edit hook tags its re-queued translation event `edited`, so the
  backend re-translates the new text instead of ignoring a duplicate.
- An edit that keeps a voice note keeps its derived block (transcript and
  translations) in both the stored render and the edit event.
- A text attach carrying `source_hash` is refused with 409 STALE_SOURCE once
  the message text has changed, under the attach's row lock.

Real realm, users and messages; the edits go through check_update_message,
the path every client edit takes.
"""

import base64
import json
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings

from nodl.api.views.derived import attach_derived_content
from nodl.derived_content import (
    DERIVED_BLOCK_RE,
    decode_derived_payload,
    translation_source_hash,
)
from nodl.extensions.models import NodlRealmExtension, SyncStatus
from zerver.actions.create_realm import do_create_realm
from zerver.actions.create_user import do_create_user
from zerver.actions.message_edit import check_update_message
from zerver.actions.message_send import check_send_message
from zerver.actions.streams import bulk_add_subscriptions
from zerver.lib.cache import bounce_key_prefix_for_testing
from zerver.lib.streams import create_stream_if_needed
from zerver.models import Attachment, Message, Realm, Stream, UserProfile
from zerver.models.clients import get_client
from zerver.worker.nodl_derived_content import NodlDerivedContentWorker

# A human-sent card (call_event is sent as the caller): marker first.
CARD_CONTENT = (
    "<!-- nodl-card:v1:"
    + base64.urlsafe_b64encode(
        json.dumps({"card_type": "call_event", "payload": {"call_id": "c"}}).encode()
    ).decode("ascii")
    + " -->\n\nMissed call"
)
MEETING_CONTENT = (
    '<!-- meeting:{"room":"site-standup","title":"Slab B pour review"} -->\n'
    "**Slab B pour review**\nJoin the video meeting: [Click to Join](https://meet.example/r)"
)


def _payload_of(rendered: str) -> dict[str, Any] | None:
    block = DERIVED_BLOCK_RE.search(rendered)
    if block is None:
        return None
    encoded = block.group(0).split("nodl-derived:v1:", 1)[1].split()[0]
    return decode_derived_payload(encoded)


class DerivedEditTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        bounce_key_prefix_for_testing(self.id())
        self.factory = RequestFactory()
        self.realm = do_create_realm(
            string_id=uuid.uuid4().hex[:20],
            name="Derived Edit",
            description="",
            org_type=Realm.ORG_TYPES["business"]["id"],
            create_zulip_discussion_channel=False,
        )
        self.sender = self._make_user("Wayan Worker")
        self.reader = self._make_user("Made Manager")
        self.stream, _created = create_stream_if_needed(
            self.realm,
            f"task-{uuid.uuid4()}",
            invite_only=True,
            history_public_to_subscribers=False,
            acting_user=None,
        )
        bulk_add_subscriptions(
            self.realm, [self.stream], [self.sender, self.reader], acting_user=None
        )

    def _make_user(self, full_name: str) -> UserProfile:
        return do_create_user(
            email=f"{uuid.uuid4().hex[:12]}@example.com",
            password=None,
            realm=self.realm,
            full_name=full_name,
            acting_user=None,
        )

    def _send(self, content: str, stream: Stream | None = None) -> int:
        return check_send_message(
            sender=self.sender,
            client=get_client("nodl-test"),
            recipient_type_name="stream",
            message_to=[(stream or self.stream).id],
            topic_name="task",
            message_content=content,
            realm=self.realm,
        ).message_id

    def _voice_link(self) -> str:
        """Upload a voice note (Zulip claims it on send) and return its link."""
        path_id = f"{self.realm.id}/ab/{uuid.uuid4().hex[:24]}/voice-1723500000.m4a"
        Attachment.objects.create(
            file_name="voice-1723500000.m4a",
            path_id=path_id,
            owner=self.sender,
            realm=self.realm,
            size=4096,
            content_type="audio/mp4",
        )
        return f"[voice-1723500000.m4a](/user_uploads/{path_id})"

    def _attach(self, message_id: int, body: dict[str, Any]) -> HttpResponse:
        request = self.factory.post(
            f"/api/v1/internal/messages/{message_id}/derived",
            data=json.dumps(body),
            content_type="application/json",
        )
        request.is_service_request = True
        response: HttpResponse = attach_derived_content(request, message_id)
        return response

    def _rendered(self, message_id: int) -> str:
        return Message.objects.get(id=message_id).rendered_content or ""


class TestEditRequeue(DerivedEditTestCase):
    @patch("zerver.actions.message_edit.queue_event_on_commit")
    def test_text_edit_queues_an_event_tagged_edited(self, mock_queue: MagicMock) -> None:
        message_id = self._send("Pour at 7")

        check_update_message(self.sender, message_id, content="Pour at 8")

        events = [
            call.args[1]
            for call in mock_queue.call_args_list
            if call.args[0] == "nodl_derived_content"
        ]
        self.assertEqual(
            events,
            [
                {
                    "message_id": message_id,
                    "message_realm_id": self.realm.id,
                    "path_ids": [],
                    "edited": True,
                }
            ],
        )

    @patch("zerver.actions.message_edit.queue_event_on_commit")
    def test_card_edit_queues_nothing(self, mock_queue: MagicMock) -> None:
        message_id = self._send("Pour at 7")

        check_update_message(self.sender, message_id, content=CARD_CONTENT)

        self.assertNotIn("nodl_derived_content", [c.args[0] for c in mock_queue.call_args_list])


class TestUnchangedTextEdit(DerivedEditTestCase):
    """An edit that leaves the translatable text as it was keeps the
    translation block and asks the backend for nothing."""

    TRANSLATIONS = {"ru": "Заливка в 7"}

    def setUp(self) -> None:
        super().setUp()
        self.message_id = self._send("Pour at 7")
        response = self._attach(
            self.message_id,
            {
                "kind": "text_translation",
                "source_lang": "en",
                "translations": self.TRANSLATIONS,
                "source_hash": translation_source_hash("Pour at 7"),
            },
        )
        self.assertEqual(response.status_code, 200, response.content)

    @patch("zerver.actions.message_edit.queue_event_on_commit")
    @patch("zerver.actions.message_edit.send_event_on_commit")
    def test_identical_edit_keeps_the_block_and_queues_nothing(
        self, mock_send_event: MagicMock, mock_queue: MagicMock
    ) -> None:
        for content in ("Pour at 7", "\n\nPour at 7   "):
            with self.subTest(content=content):
                check_update_message(self.sender, self.message_id, content=content)

                rendered = self._rendered(self.message_id)
                payload = _payload_of(rendered)
                assert payload is not None
                self.assertEqual(payload["translations"], self.TRANSLATIONS)
                self.assertEqual(len(DERIVED_BLOCK_RE.findall(rendered)), 1)
                edit_event = mock_send_event.call_args_list[-1].args[1]
                self.assertEqual(edit_event["rendered_content"], rendered)
        self.assertNotIn("nodl_derived_content", [c.args[0] for c in mock_queue.call_args_list])

    @patch("zerver.actions.message_edit.queue_event_on_commit")
    def test_real_edit_drops_the_block_and_queues_an_edit(self, mock_queue: MagicMock) -> None:
        check_update_message(self.sender, self.message_id, content="Pour at 8")

        self.assertIsNone(_payload_of(self._rendered(self.message_id)))
        events = [
            c.args[1] for c in mock_queue.call_args_list if c.args[0] == "nodl_derived_content"
        ]
        self.assertEqual(len(events), 1)
        self.assertIs(events[0]["edited"], True)


@override_settings(NODL_BACKEND_URL="http://backend.test", BACKEND_SERVICE_KEY="test-key")
class TestWorkerForwardsReason(DerivedEditTestCase):
    def _forwarded(self, event: dict[str, Any]) -> dict[str, Any]:
        NodlRealmExtension.objects.get_or_create(
            zulip_realm=self.realm,
            defaults={"nodl_workspace_id": uuid.uuid4(), "sync_status": SyncStatus.SYNCED},
        )
        with patch("zerver.worker.nodl_derived_content.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=202)
            NodlDerivedContentWorker().consume(event)
        mock_post.assert_called_once()
        body: dict[str, Any] = mock_post.call_args.kwargs["json"]
        return body

    def test_send_and_edit_events_carry_their_reason(self) -> None:
        message_id = self._send("Pour at 7")
        event = {"message_id": message_id, "message_realm_id": self.realm.id, "path_ids": []}

        self.assertEqual(self._forwarded(event)["reason"], "send")
        edited = self._forwarded({**event, "edited": True})
        self.assertEqual(edited["reason"], "edit")
        self.assertEqual(edited["text"], "Pour at 7")

    def test_meeting_message_forwards_its_text_without_the_marker(self) -> None:
        message_id = self._send(MEETING_CONTENT)
        body = self._forwarded(
            {"message_id": message_id, "message_realm_id": self.realm.id, "path_ids": []}
        )

        self.assertTrue(body["text"].startswith("**Slab B pour review**"))
        self.assertNotIn("<!--", body["text"])
        self.assertNotIn('"room"', body["text"])


class TestVoiceEditKeepsDerivedBlock(DerivedEditTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.voice_link = self._voice_link()
        self.message_id = self._send(self.voice_link)
        response = self._attach(
            self.message_id,
            {
                "kind": "voice_transcript",
                "transcript": "Бетон привезли",
                "source_lang": "ru",
                "translations": {"en": "The concrete arrived"},
            },
        )
        self.assertEqual(response.status_code, 200, response.content)

    @patch("zerver.actions.message_edit.queue_event_on_commit")
    @patch("zerver.actions.message_edit.send_event_on_commit")
    def test_block_survives_in_render_and_edit_event(
        self, mock_send_event: MagicMock, mock_queue: MagicMock
    ) -> None:
        check_update_message(self.sender, self.message_id, content=f"{self.voice_link} level 2")

        rendered = self._rendered(self.message_id)
        self.assertIn("level 2", rendered)
        payload = _payload_of(rendered)
        assert payload is not None
        self.assertEqual(payload["transcript"], "Бетон привезли")
        self.assertEqual(payload["translations"], {"en": "The concrete arrived"})
        self.assertIn("<p>Бетон привезли</p>", rendered)
        self.assertEqual(len(DERIVED_BLOCK_RE.findall(rendered)), 1)

        edit_events = [
            c.args[1]
            for c in mock_send_event.call_args_list
            if c.args[1].get("type") == "update_message"
        ]
        self.assertEqual(len(edit_events), 1)
        self.assertEqual(edit_events[0]["rendered_content"], rendered)
        # Voice is never re-queued: no re-transcription for unchanged audio.
        self.assertNotIn("nodl_derived_content", [c.args[0] for c in mock_queue.call_args_list])

    def test_block_dropped_when_the_voice_note_is_removed(self) -> None:
        check_update_message(self.sender, self.message_id, content="sent the wrong recording")

        self.assertIsNone(_payload_of(self._rendered(self.message_id)))


class TestAttachSourceHash(DerivedEditTestCase):
    TRANSLATIONS = {"ru": "Заливка в 7"}

    def _text_attach(self, message_id: int, source_hash: str | None) -> HttpResponse:
        body: dict[str, Any] = {
            "kind": "text_translation",
            "source_lang": "en",
            "translations": self.TRANSLATIONS,
        }
        if source_hash is not None:
            body["source_hash"] = source_hash
        return self._attach(message_id, body)

    def test_matching_hash_attaches(self) -> None:
        message_id = self._send("Pour at 7")

        response = self._text_attach(message_id, translation_source_hash("Pour at 7"))

        self.assertEqual(response.status_code, 200, response.content)
        payload = _payload_of(self._rendered(message_id))
        assert payload is not None
        self.assertEqual(payload["translations"], self.TRANSLATIONS)

    def test_uppercase_hash_matches(self) -> None:
        message_id = self._send("Pour at 7")

        response = self._text_attach(message_id, translation_source_hash("Pour at 7").upper())

        self.assertEqual(response.status_code, 200, response.content)

    def test_stale_hash_after_an_edit_is_409(self) -> None:
        message_id = self._send("Pour at 7")
        stale_hash = translation_source_hash("Pour at 7")
        check_update_message(self.sender, message_id, content="Pour at 8")

        response = self._text_attach(message_id, stale_hash)

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(json.loads(response.content)["code"], "STALE_SOURCE")
        self.assertIsNone(_payload_of(self._rendered(message_id)))

    def test_text_no_longer_translatable_is_409(self) -> None:
        message_id = self._send("Pour at 7")
        source_hash = translation_source_hash("Pour at 7")
        check_update_message(self.sender, message_id, content=CARD_CONTENT)

        response = self._text_attach(message_id, source_hash)

        self.assertEqual(response.status_code, 409, response.content)

    def test_marker_message_hashes_the_text_without_the_marker(self) -> None:
        # The worker forwards translation_text(), markers stripped; the
        # backend hashes that, and the attach check recomputes the same.
        message_id = self._send(MEETING_CONTENT)
        forwarded_text = "**Slab B pour review**\nJoin the video meeting: [Click to Join](https://meet.example/r)"

        stale = self._text_attach(message_id, translation_source_hash(MEETING_CONTENT))
        self.assertEqual(stale.status_code, 409, stale.content)
        response = self._text_attach(message_id, translation_source_hash(forwarded_text))
        self.assertEqual(response.status_code, 200, response.content)

    def test_without_hash_attaches_as_before(self) -> None:
        # Backends that predate source_hash keep today's behavior.
        message_id = self._send("Pour at 7")
        check_update_message(self.sender, message_id, content="Pour at 8")

        response = self._text_attach(message_id, None)

        self.assertEqual(response.status_code, 200, response.content)

    def test_voice_rows_are_never_checked(self) -> None:
        # A voice row's backend hash is of its transcript, not the content.
        message_id = self._send(self._voice_link())

        response = self._attach(
            message_id,
            {
                "kind": "voice_transcript",
                "transcript": "Бетон привезли",
                "source_hash": translation_source_hash("Бетон привезли"),
            },
        )

        self.assertEqual(response.status_code, 200, response.content)
