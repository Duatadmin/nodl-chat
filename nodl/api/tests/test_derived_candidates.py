"""Internal translation-candidates read (W0 back-fill + sweep).

Mode A must return exactly the reader's own chats — subscribed streams plus
DMs they received — after upstream's access check, never a realm-wide set.
Mode B is realm-scoped by id. Real realm, users, streams and messages: the
queries are the thing under test.
"""

import base64
import json
import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.utils.timezone import now as timezone_now

from nodl.api.views.derived_candidates import list_translation_candidates
from nodl.extensions.models import (
    NodlRealmExtension,
    NodlRealmUserExtension,
    NodlUserExtension,
    SyncStatus,
)
from zerver.actions.create_realm import do_create_realm
from zerver.actions.create_user import do_create_user
from zerver.actions.message_delete import do_delete_messages
from zerver.actions.message_edit import check_update_message
from zerver.actions.message_send import check_send_message
from zerver.actions.streams import bulk_add_subscriptions
from zerver.actions.users import do_deactivate_user
from zerver.lib.cache import bounce_key_prefix_for_testing
from zerver.lib.streams import create_stream_if_needed
from zerver.models import Attachment, Message, Realm, Stream, UserProfile
from zerver.models.clients import get_client


def _card(card_type: str, payload: dict[str, Any], fallback: str) -> str:
    raw = json.dumps({"card_type": card_type, "payload": payload})
    encoded = base64.urlsafe_b64encode(raw.encode()).decode("ascii")
    return f"<!-- nodl-card:v1:{encoded} -->\n\n{fallback}"


class CandidatesTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        bounce_key_prefix_for_testing(self.id())
        self.factory = RequestFactory()
        self.realm, self.workspace_id = self._make_realm("Candidates")
        self.reader, self.reader_supabase_id = self._make_user(self.realm, "Reader")
        self.alice, self.alice_supabase_id = self._make_user(self.realm, "Alice")
        self.bob, _ = self._make_user(self.realm, "Bob")
        self.carol, _ = self._make_user(self.realm, "Carol")
        self.since = timezone_now() - timedelta(days=7)

    def _make_realm(self, name: str) -> tuple[Realm, uuid.UUID]:
        workspace_id = uuid.uuid4()
        realm = do_create_realm(
            string_id=uuid.uuid4().hex[:20],
            name=name,
            description="",
            org_type=Realm.ORG_TYPES["business"]["id"],
            create_zulip_discussion_channel=False,
        )
        NodlRealmExtension.objects.create(
            zulip_realm=realm, nodl_workspace_id=workspace_id, sync_status=SyncStatus.SYNCED
        )
        return realm, workspace_id

    def _make_user(
        self, realm: Realm, full_name: str, bot_type: int | None = None
    ) -> tuple[UserProfile, uuid.UUID]:
        user = do_create_user(
            email=f"{uuid.uuid4().hex[:12]}@example.com",
            password=None,
            realm=realm,
            full_name=full_name,
            bot_type=bot_type,
            acting_user=None,
        )
        supabase_user_id = uuid.uuid4()
        if bot_type is None:
            NodlRealmUserExtension.objects.create(
                zulip_realm=realm, zulip_user=user, supabase_user_id=supabase_user_id
            )
        return user, supabase_user_id

    def _stream(
        self,
        subscribers: list[UserProfile],
        *,
        private: bool = True,
        shared_history: bool = False,
    ) -> Stream:
        # Task streams: private, history not public to subscribers.
        stream, _created = create_stream_if_needed(
            self.realm,
            f"s-{uuid.uuid4().hex[:12]}",
            invite_only=private,
            history_public_to_subscribers=shared_history if private else None,
            acting_user=None,
        )
        if subscribers:
            self._subscribe(stream, subscribers)
        return stream

    def _subscribe(self, stream: Stream, users: list[UserProfile]) -> None:
        bulk_add_subscriptions(self.realm, [stream], users, acting_user=None)

    def _post(self, sender: UserProfile, stream: Stream, content: str = "") -> int:
        return check_send_message(
            sender=sender,
            client=get_client("nodl-test"),
            recipient_type_name="stream",
            message_to=[stream.id],
            topic_name="task",
            message_content=content or f"post from {sender.full_name} {uuid.uuid4().hex[:6]}",
            realm=self.realm,
        ).message_id

    def _dm(self, sender: UserProfile, recipients: list[UserProfile], content: str = "") -> int:
        return check_send_message(
            sender=sender,
            client=get_client("nodl-test"),
            recipient_type_name="private",
            message_to=[user.id for user in recipients],
            topic_name="",
            message_content=content or f"dm from {sender.full_name} {uuid.uuid4().hex[:6]}",
            realm=self.realm,
        ).message_id

    def _upload_link(self, sender: UserProfile, file_name: str, content_type: str) -> str:
        path_id = f"{self.realm.id}/ab/{uuid.uuid4().hex[:24]}/{file_name}"
        Attachment.objects.create(
            file_name=file_name,
            path_id=path_id,
            owner=sender,
            realm=self.realm,
            size=2048,
            content_type=content_type,
        )
        return f"[{file_name}](/user_uploads/{path_id})"

    def _call(self, body: dict[str, Any], *, service: bool = True) -> HttpResponse:
        request = self.factory.post(
            "/api/v1/internal/derived/candidates",
            data=json.dumps(body, default=str),
            content_type="application/json",
        )
        request.is_service_request = service
        response: HttpResponse = list_translation_candidates(request)
        return response

    def _backfill(self, **overrides: Any) -> dict[str, Any]:
        body = {
            "workspace_id": str(self.workspace_id),
            "supabase_user_id": str(self.reader_supabase_id),
            "since": self.since.isoformat(),
            **overrides,
        }
        response = self._call(body)
        self.assertEqual(response.status_code, 200, response.content)
        data: dict[str, Any] = json.loads(response.content)
        return data

    def _backfill_ids(self, **overrides: Any) -> list[int]:
        return [item["message_id"] for item in self._backfill(**overrides)["items"]]

    def _sweep(self, message_ids: list[int]) -> dict[int, dict[str, Any]]:
        response = self._call({"workspace_id": str(self.workspace_id), "message_ids": message_ids})
        self.assertEqual(response.status_code, 200, response.content)
        return {item["message_id"]: item for item in json.loads(response.content)["items"]}


class TestCandidatesRequest(CandidatesTestCase):
    def test_requires_service_auth(self) -> None:
        response = self._call({"workspace_id": str(self.workspace_id)}, service=False)
        self.assertEqual(response.status_code, 401)

    def test_post_only(self) -> None:
        request = self.factory.get("/api/v1/internal/derived/candidates")
        request.is_service_request = True
        self.assertEqual(list_translation_candidates(request).status_code, 405)

    def test_exactly_one_mode(self) -> None:
        workspace = {"workspace_id": str(self.workspace_id)}
        for body in (
            workspace,
            {**workspace, "supabase_user_id": str(self.reader_supabase_id)},  # no since
            {
                **workspace,
                "supabase_user_id": str(self.reader_supabase_id),
                "since": self.since.isoformat(),
                "message_ids": [1],
            },
            {**workspace, "message_ids": list(range(1, 202))},  # over 200
            {"workspace_id": "not-a-uuid", "message_ids": [1]},
        ):
            with self.subTest(body=body):
                response = self._call(body)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(json.loads(response.content)["code"], "VALIDATION_ERROR")

    def test_unknown_workspace_or_user_is_404(self) -> None:
        other_realm, other_workspace_id = self._make_realm("Other")
        _outsider, outsider_supabase_id = self._make_user(other_realm, "Outsider")
        deactivated, deactivated_supabase_id = self._make_user(self.realm, "Gone")
        do_deactivate_user(deactivated, acting_user=None)
        since = self.since.isoformat()

        for label, body in {
            "unknown workspace": {
                "workspace_id": str(uuid.uuid4()),
                "supabase_user_id": str(self.reader_supabase_id),
                "since": since,
            },
            "user of another realm": {
                "workspace_id": str(self.workspace_id),
                "supabase_user_id": str(outsider_supabase_id),
                "since": since,
            },
            "reader asked in another realm": {
                "workspace_id": str(other_workspace_id),
                "supabase_user_id": str(self.reader_supabase_id),
                "since": since,
            },
            "unmapped user": {
                "workspace_id": str(self.workspace_id),
                "supabase_user_id": str(uuid.uuid4()),
                "since": since,
            },
            "deactivated user": {
                "workspace_id": str(self.workspace_id),
                "supabase_user_id": str(deactivated_supabase_id),
                "since": since,
            },
            "sweep of an unknown workspace": {
                "workspace_id": str(uuid.uuid4()),
                "message_ids": [1],
            },
        }.items():
            with self.subTest(label):
                self.assertEqual(self._call(body).status_code, 404)

    def test_legacy_mapping_resolves_only_inside_the_realm(self) -> None:
        # Users provisioned before realm-scoped mappings have only the global
        # NodlUserExtension row; it counts when its user lives in this realm.
        legacy_reader = do_create_user(
            email=f"{uuid.uuid4().hex[:12]}@example.com",
            password=None,
            realm=self.realm,
            full_name="Legacy Reader",
            acting_user=None,
        )
        legacy_supabase_id = uuid.uuid4()
        NodlUserExtension.objects.create(
            zulip_user=legacy_reader, supabase_user_id=legacy_supabase_id
        )
        stream = self._stream([legacy_reader, self.alice])
        posted = self._post(self.alice, stream)
        other_realm, other_workspace_id = self._make_realm("Other")

        ids = self._backfill_ids(supabase_user_id=str(legacy_supabase_id))
        elsewhere = self._call(
            {
                "workspace_id": str(other_workspace_id),
                "supabase_user_id": str(legacy_supabase_id),
                "since": self.since.isoformat(),
            }
        )

        self.assertEqual(ids, [posted])
        self.assertEqual(elsewhere.status_code, 404)


class TestBackfillScope(CandidatesTestCase):
    def test_returns_the_readers_chats_only(self) -> None:
        mine = self._stream([self.reader, self.alice])
        public_mine = self._stream([self.reader, self.alice], private=False)
        foreign = self._stream([self.alice, self.bob])
        public_unjoined = self._stream([self.alice], private=False)

        in_task = self._post(self.alice, mine, "Rebar is in")
        in_public = self._post(self.alice, public_mine)
        dm_received = self._dm(self.alice, [self.reader], "Can you check the slab?")
        group_dm = self._dm(self.bob, [self.reader, self.alice])
        not_subscribed = self._post(self.alice, foreign)
        unjoined_public = self._post(self.alice, public_unjoined)
        dm_not_received = self._dm(self.bob, [self.carol])

        ids = self._backfill_ids()

        for included in (in_task, in_public, dm_received, group_dm):
            self.assertIn(included, ids)
        for excluded in (not_subscribed, unjoined_public, dm_not_received):
            self.assertNotIn(excluded, ids)

    def test_excludes_own_bot_card_attachment_and_deleted_messages(self) -> None:
        # A meeting message stays in: its marker is stripped, its text goes.
        stream = self._stream([self.reader, self.alice])
        bot, _ = self._make_user(self.realm, "Nodle Assistant", bot_type=UserProfile.DEFAULT_BOT)
        self._subscribe(stream, [bot])

        kept = self._post(self.alice, stream, "Pour at 7")
        own = self._post(self.reader, stream, "On my way")
        from_bot = self._post(bot, stream, "Status check-in")
        card = self._dm(
            self.alice,
            [self.reader],
            _card(
                "call_event",
                {"call_id": "c", "status": "missed", "caller_id": 1, "callee_id": 2},
                "Missed call",
            ),
        )
        meeting = self._post(self.alice, stream, '<!-- meeting:{"room":"r"} -->\nJoin at 9')
        photo = self._post(self.alice, stream, self._upload_link(self.alice, "a.jpg", "image/jpeg"))
        deleted = self._post(self.alice, stream, "wrong chat")
        do_delete_messages(self.realm, list(Message.objects.filter(id=deleted)), acting_user=None)

        items = self._backfill()["items"]

        self.assertEqual([item["message_id"] for item in items], [meeting, kept])
        self.assertEqual(items[0]["text"], "Join at 9")
        for excluded in (own, from_bot, card, photo, deleted):
            self.assertNotIn(excluded, [item["message_id"] for item in items])

    def test_items_carry_kind_text_sender_and_date(self) -> None:
        stream = self._stream([self.reader, self.alice])
        text_id = self._post(self.alice, stream, "  Бетон привезли  ")
        voice_id = self._post(
            self.alice, stream, self._upload_link(self.alice, "voice-1723500000.m4a", "audio/mp4")
        )

        items = {item["message_id"]: item for item in self._backfill()["items"]}

        self.assertEqual(items[text_id]["kind"], "text_translation")
        self.assertEqual(items[text_id]["text"], "Бетон привезли")
        self.assertEqual(items[text_id]["sender_supabase_user_id"], str(self.alice_supabase_id))
        sent = Message.objects.get(id=text_id).date_sent
        self.assertEqual(items[text_id]["date_sent"], sent.isoformat())
        self.assertEqual(items[voice_id]["kind"], "voice_transcript")
        self.assertIsNone(items[voice_id]["text"])

    def test_an_empty_page_can_carry_a_cursor(self) -> None:
        # Every scanned row is a photo: no items, but more rows remain.
        stream = self._stream([self.reader, self.alice])
        text = self._post(self.alice, stream, "Pour at 7")
        for _ in range(2):
            self._post(self.alice, stream, self._upload_link(self.alice, "a.jpg", "image/jpeg"))

        empty = self._backfill(limit=1)
        last = self._backfill(limit=1, before_message_id=empty["next_before_message_id"])

        self.assertEqual(empty["items"], [])
        self.assertIsNotNone(empty["next_before_message_id"])
        self.assertEqual([item["message_id"] for item in last["items"]], [text])

    def test_messages_before_since_are_out(self) -> None:
        stream = self._stream([self.reader, self.alice])
        old = self._post(self.alice, stream)
        recent = self._post(self.alice, stream)
        Message.objects.filter(id=old).update(date_sent=timezone_now() - timedelta(days=8))

        ids = self._backfill_ids()

        self.assertEqual(ids, [recent])

    def test_protected_history_joined_late_excludes_earlier_messages(self) -> None:
        stream = self._stream([self.alice], shared_history=False)
        before = self._post(self.alice, stream)
        self._subscribe(stream, [self.reader])
        after = self._post(self.alice, stream)

        ids = self._backfill_ids()

        self.assertIn(after, ids)
        self.assertNotIn(before, ids)

    def test_shared_history_joined_late_includes_earlier_messages(self) -> None:
        stream = self._stream([self.alice], shared_history=True)
        before = self._post(self.alice, stream)
        self._subscribe(stream, [self.reader])
        after = self._post(self.alice, stream)

        ids = self._backfill_ids()

        self.assertIn(before, ids)
        self.assertIn(after, ids)


class TestBackfillPaging(CandidatesTestCase):
    def setUp(self) -> None:
        super().setUp()
        stream = self._stream([self.reader, self.alice])
        self.posted = [self._post(self.alice, stream) for _ in range(5)]

    def test_limit_and_cursor_walk_newest_first(self) -> None:
        newest_first = sorted(self.posted, reverse=True)

        first = self._backfill(limit=2)
        self.assertEqual([item["message_id"] for item in first["items"]], newest_first[:2])
        second = self._backfill(limit=2, before_message_id=first["next_before_message_id"])
        self.assertEqual([item["message_id"] for item in second["items"]], newest_first[2:4])
        third = self._backfill(limit=2, before_message_id=second["next_before_message_id"])
        self.assertEqual([item["message_id"] for item in third["items"]], newest_first[4:])
        self.assertIsNone(third["next_before_message_id"])

    def test_limit_is_capped_at_300(self) -> None:
        self.assertEqual(len(self._backfill(limit=5000)["items"]), 5)

    def test_text_cap_ends_the_page_and_the_next_page_resumes(self) -> None:
        newest_first = sorted(self.posted, reverse=True)
        one_text = len(Message.objects.get(id=newest_first[0]).content.encode())

        with patch("nodl.api.views.derived_candidates.MAX_PAGE_TEXT_BYTES", one_text * 2):
            first = self._backfill()
            second = self._backfill(before_message_id=first["next_before_message_id"])

        self.assertEqual([item["message_id"] for item in first["items"]], newest_first[:2])
        self.assertEqual([item["message_id"] for item in second["items"]], newest_first[2:4])


class TestSweep(CandidatesTestCase):
    def test_returns_current_kind_and_text_by_id(self) -> None:
        stream = self._stream([self.reader, self.alice])
        edited = self._post(self.alice, stream, "Pour at 7")
        check_update_message(self.alice, edited, content="Pour at 8")
        voice = self._post(
            self.alice, stream, self._upload_link(self.alice, "voice-1723500000.m4a", "audio/mp4")
        )
        now_a_card = self._post(self.alice, stream, "Calling you")
        check_update_message(
            self.alice, now_a_card, content=_card("call_event", {"call_id": "c"}, "Missed call")
        )

        items = self._sweep([edited, voice, now_a_card])

        self.assertEqual(
            (items[edited]["kind"], items[edited]["text"]), ("text_translation", "Pour at 8")
        )
        self.assertEqual(items[edited]["sender_supabase_user_id"], str(self.alice_supabase_id))
        self.assertEqual((items[voice]["kind"], items[voice]["text"]), ("voice_transcript", None))
        self.assertEqual((items[now_a_card]["kind"], items[now_a_card]["text"]), (None, None))

    def test_deleted_and_foreign_realm_ids_are_absent(self) -> None:
        stream = self._stream([self.reader, self.alice])
        kept = self._post(self.alice, stream)
        deleted = self._post(self.alice, stream)
        do_delete_messages(self.realm, list(Message.objects.filter(id=deleted)), acting_user=None)
        other_realm, _ = self._make_realm("Other")
        outsider, _ = self._make_user(other_realm, "Outsider")
        insider, _ = self._make_user(other_realm, "Insider")
        foreign = check_send_message(
            sender=outsider,
            client=get_client("nodl-test"),
            recipient_type_name="private",
            message_to=[insider.id],
            topic_name="",
            message_content="not yours",
            realm=other_realm,
        ).message_id

        self.assertEqual(list(self._sweep([kept, deleted, foreign, 10**9])), [kept])

    def test_text_cap_truncates_and_says_so(self) -> None:
        stream = self._stream([self.reader, self.alice])
        posted = [self._post(self.alice, stream) for _ in range(3)]
        one_text = len(Message.objects.get(id=posted[0]).content.encode())
        body = {"workspace_id": str(self.workspace_id), "message_ids": posted}

        with patch("nodl.api.views.derived_candidates.MAX_PAGE_TEXT_BYTES", one_text * 2):
            capped = json.loads(self._call(body).content)
        whole = json.loads(self._call(body).content)

        self.assertEqual([item["message_id"] for item in capped["items"]], posted[:2])
        self.assertIs(capped["truncated"], True)
        self.assertEqual([item["message_id"] for item in whole["items"]], posted)
        self.assertIs(whole["truncated"], False)
