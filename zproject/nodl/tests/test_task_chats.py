"""Phase 3 server half: task-stream display metadata + unified-inbox identity.

Covers:
- "Task chats" folder get-or-create (per realm, no creator, idempotent)
- sync_task_stream: description = task title, folder assignment, no force-mute
- title rename reconciles the description WITHOUT a notification-bot message
- backfill_task_chats / unmute_task_streams management commands
- list_dm_conversations: nodl_user_id exposure + normalized unread counts
"""

import json
import uuid
from unittest.mock import patch

from django.core.management import call_command
from django.http import HttpRequest
from django.test import RequestFactory

from nodl.api.views.messages import list_dm_conversations
from nodl.api.views.task_streams import sync_task_stream
from nodl.extensions.mapping import record_realm_user_mapping
from nodl.extensions.models import NodlRealmExtension, NodlTaskStreamExtension, SyncStatus
from nodl.extensions.task_chats import (
    TASK_CHATS_FOLDER_NAME,
    get_or_create_task_chats_folder,
    task_stream_description,
)
from zerver.actions.create_realm import do_create_realm
from zerver.actions.create_user import do_create_user
from zerver.lib.streams import create_stream_if_needed
from zerver.lib.test_classes import ZulipTestCase
from zerver.models import ChannelFolder, Message, Realm, Stream, Subscription, UserProfile


class TaskChatsFixtureMixin(ZulipTestCase):
    def make_workspace_realm(self, name: str) -> tuple[Realm, uuid.UUID]:
        ws_uuid = uuid.uuid4()
        realm = do_create_realm(
            string_id=str(ws_uuid)[:20].lower(),
            name=name,
            description="",
            org_type=Realm.ORG_TYPES["business"]["id"],
            create_zulip_discussion_channel=False,
        )
        NodlRealmExtension.objects.create(
            zulip_realm=realm,
            nodl_workspace_id=ws_uuid,
            sync_status=SyncStatus.SYNCED,
        )
        return realm, ws_uuid

    def make_user(self, realm: Realm, email: str, full_name: str) -> UserProfile:
        return do_create_user(
            email=email,
            password=None,
            realm=realm,
            full_name=full_name,
            acting_user=None,
        )

    def sync_request(self, payload: dict) -> HttpRequest:
        request = RequestFactory().post(
            "/api/v1/internal/task-streams/sync",
            data=json.dumps(payload),
            content_type="application/json",
        )
        request.is_service_request = True
        return request


class TaskChatsFolderTest(TaskChatsFixtureMixin):
    def test_get_or_create_is_idempotent_and_creatorless(self) -> None:
        realm, _ = self.make_workspace_realm("Folder WS")
        folder1 = get_or_create_task_chats_folder(realm)
        folder2 = get_or_create_task_chats_folder(realm)
        self.assertEqual(folder1.id, folder2.id)
        self.assertEqual(folder1.name, TASK_CHATS_FOLDER_NAME)
        self.assertIsNone(folder1.creator_id)
        self.assertEqual(
            ChannelFolder.objects.filter(realm=realm, is_archived=False).count(), 1
        )

    def test_folders_are_per_realm(self) -> None:
        realm_a, _ = self.make_workspace_realm("WS A")
        realm_b, _ = self.make_workspace_realm("WS B")
        folder_a = get_or_create_task_chats_folder(realm_a)
        folder_b = get_or_create_task_chats_folder(realm_b)
        self.assertNotEqual(folder_a.id, folder_b.id)
        self.assertEqual(folder_a.realm_id, realm_a.id)
        self.assertEqual(folder_b.realm_id, realm_b.id)


class TaskStreamDisplayTest(TaskChatsFixtureMixin):
    def setUp(self) -> None:
        super().setUp()
        self.realm, self.ws_uuid = self.make_workspace_realm("Display WS")
        self.task_id = str(uuid.uuid4())
        self.member = {
            "supabase_user_id": str(uuid.uuid4()),
            "email": "worker@example.com",
            "full_name": "Worker",
            "avatar_url": None,
            "role": "assignee",
        }

    def _sync(self, task_title: str | None) -> Stream:
        payload = {
            "workspace_id": str(self.ws_uuid),
            "task_id": self.task_id,
            "stream_name": f"task-{self.task_id}",
            "task_title": task_title,
            "members": [self.member],
        }
        response = sync_task_stream(self.sync_request(payload))
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        return Stream.objects.get(id=data["zulip_stream_id"])

    def test_create_sets_title_description_folder_and_no_mute(self) -> None:
        stream = self._sync("Install kitchen cabinets")

        self.assertEqual(stream.description, "Install kitchen cabinets")
        folder = ChannelFolder.objects.get(realm=self.realm, name=TASK_CHATS_FOLDER_NAME)
        self.assertEqual(stream.folder_id, folder.id)

        sub = Subscription.objects.get(
            recipient=stream.recipient,
            user_profile__delivery_email="worker@example.com",
        )
        self.assertFalse(sub.is_muted)

    def test_create_without_title_keeps_legacy_placeholder(self) -> None:
        stream = self._sync(None)
        self.assertEqual(stream.description, f"Task discussion {self.task_id}")

    def test_rename_updates_description_without_notification_message(self) -> None:
        stream = self._sync("Old title")
        messages_before = Message.objects.filter(recipient=stream.recipient).count()

        stream = self._sync("New title after rename")

        self.assertEqual(stream.description, "New title after rename")
        extension = NodlTaskStreamExtension.objects.get(nodl_task_id=uuid.UUID(self.task_id))
        self.assertEqual(extension.task_title, "New title after rename")
        # No "changed the description" bot message in the stream.
        self.assertEqual(
            Message.objects.filter(recipient=stream.recipient).count(), messages_before
        )

    def test_resync_selfheals_legacy_stream_display(self) -> None:
        # A pre-Phase-3 stream: placeholder description, no folder.
        stream, _ = create_stream_if_needed(
            self.realm,
            f"task-{self.task_id}",
            invite_only=True,
            stream_description=f"Task discussion {self.task_id}",
            history_public_to_subscribers=False,
            acting_user=None,
        )
        NodlTaskStreamExtension.objects.create(
            zulip_realm=self.realm,
            zulip_stream=stream,
            nodl_workspace_id=self.ws_uuid,
            nodl_task_id=uuid.UUID(self.task_id),
            task_title="",
        )
        self.assertIsNone(stream.folder_id)

        synced = self._sync("Pour the foundation")

        self.assertEqual(synced.id, stream.id)
        self.assertEqual(synced.description, "Pour the foundation")
        folder = ChannelFolder.objects.get(realm=self.realm, name=TASK_CHATS_FOLDER_NAME)
        self.assertEqual(synced.folder_id, folder.id)

    def test_description_is_single_line_and_bounded(self) -> None:
        self.assertEqual(
            task_stream_description("line one\nline two\t end"), "line one line two end"
        )
        self.assertLessEqual(
            len(task_stream_description("x" * 5000)), Stream.MAX_DESCRIPTION_LENGTH
        )


class TaskChatsBackfillTest(TaskChatsFixtureMixin):
    def setUp(self) -> None:
        super().setUp()
        self.realm, self.ws_uuid = self.make_workspace_realm("Backfill WS")
        self.user = self.make_user(self.realm, "member@example.com", "Member")
        self.task_id = uuid.uuid4()
        self.stream, _ = create_stream_if_needed(
            self.realm,
            f"task-{self.task_id}",
            invite_only=True,
            stream_description=f"Task discussion {self.task_id}",
            history_public_to_subscribers=False,
            acting_user=None,
        )
        NodlTaskStreamExtension.objects.create(
            zulip_realm=self.realm,
            zulip_stream=self.stream,
            nodl_workspace_id=self.ws_uuid,
            nodl_task_id=self.task_id,
            task_title="Fit the windows",
        )
        self.subscribe(self.user, self.stream.name)
        Subscription.objects.filter(
            recipient=self.stream.recipient, user_profile=self.user
        ).update(is_muted=True)

    def test_backfill_task_chats_dry_run_changes_nothing(self) -> None:
        call_command("backfill_task_chats")
        self.stream.refresh_from_db()
        self.assertEqual(self.stream.description, f"Task discussion {self.task_id}")
        self.assertIsNone(self.stream.folder_id)

    def test_backfill_task_chats_commit_sets_description_and_folder(self) -> None:
        call_command("backfill_task_chats", "--commit")
        self.stream.refresh_from_db()
        self.assertEqual(self.stream.description, "Fit the windows")
        folder = ChannelFolder.objects.get(realm=self.realm, name=TASK_CHATS_FOLDER_NAME)
        self.assertEqual(self.stream.folder_id, folder.id)

        # Idempotent: a second run changes nothing further.
        call_command("backfill_task_chats", "--commit")
        self.stream.refresh_from_db()
        self.assertEqual(self.stream.description, "Fit the windows")

    def test_unmute_task_streams_commit_unmutes(self) -> None:
        call_command("unmute_task_streams")
        sub = Subscription.objects.get(
            recipient=self.stream.recipient, user_profile=self.user
        )
        self.assertTrue(sub.is_muted)  # dry run: untouched

        call_command("unmute_task_streams", "--commit")
        sub.refresh_from_db()
        self.assertFalse(sub.is_muted)


class ListDmConversationsIdentityTest(TaskChatsFixtureMixin):
    """nodl_user_id exposure and normalized unread counts for the unified inbox."""

    def setUp(self) -> None:
        super().setUp()
        self.realm, _ = self.make_workspace_realm("DM WS")
        self.alice = self.make_user(self.realm, "alice@example.com", "Alice")
        self.bob = self.make_user(self.realm, "bob@example.com", "Bob")
        self.carol = self.make_user(self.realm, "carol@example.com", "Carol")
        self.alice_supabase_id = uuid.uuid4()
        record_realm_user_mapping(self.realm, self.alice, self.alice_supabase_id)

    def _conversations_for(self, user: UserProfile) -> list[dict]:
        request = RequestFactory().get("/api/v1/dm/conversations")
        request.user_profile = user
        with patch(
            "nodl.api.views.messages.MessagesRateLimitedObject.rate_limit_request"
        ):
            response = list_dm_conversations(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "success")
        return data["conversations"]

    def test_nodl_user_id_present_for_mapped_counterpart(self) -> None:
        self.send_personal_message(self.alice, self.bob, "hi bob")

        conversations = self._conversations_for(self.bob)
        one_on_one = [c for c in conversations if c["user_ids"] == [self.alice.id]]
        self.assert_length(one_on_one, 1)
        (alice_entry,) = one_on_one[0]["users"]
        self.assertEqual(alice_entry["nodl_user_id"], str(self.alice_supabase_id))

    def test_nodl_user_id_null_for_unmapped_counterpart(self) -> None:
        self.send_personal_message(self.carol, self.bob, "hi from carol")

        conversations = self._conversations_for(self.bob)
        one_on_one = [c for c in conversations if c["user_ids"] == [self.carol.id]]
        self.assert_length(one_on_one, 1)
        (carol_entry,) = one_on_one[0]["users"]
        self.assertIsNone(carol_entry["nodl_user_id"])

    def test_unread_counts_are_per_conversation_not_per_sender(self) -> None:
        """Group-DM unreads must not leak 1:1 messages from the same sender.

        The pre-Phase-3 implementation counted a group conversation's unreads
        as 'unread messages TO me FROM its participants', double-counting
        every 1:1 message from the same humans.
        """
        self.send_personal_message(self.alice, self.bob, "one")
        self.send_personal_message(self.alice, self.bob, "two")
        self.send_group_direct_message(self.alice, [self.bob, self.carol], "group hello")

        conversations = self._conversations_for(self.bob)
        by_ids = {tuple(c["user_ids"]): c for c in conversations}

        self.assertEqual(by_ids[(self.alice.id,)]["unread_count"], 2)
        group_key = tuple(sorted([self.alice.id, self.carol.id]))
        self.assertEqual(by_ids[group_key]["unread_count"], 1)

    def test_read_messages_are_not_counted(self) -> None:
        message_id = self.send_personal_message(self.alice, self.bob, "seen")
        from zerver.actions.message_flags import do_update_message_flags

        do_update_message_flags(self.bob, "add", "read", [message_id])

        conversations = self._conversations_for(self.bob)
        one_on_one = [c for c in conversations if c["user_ids"] == [self.alice.id]]
        self.assert_length(one_on_one, 1)
        self.assertEqual(one_on_one[0]["unread_count"], 0)

    def test_deactivated_counterpart_included_and_flagged(self) -> None:
        """A 1:1 with a deactivated user keeps its row (and unreads) — dropping
        it would orphan the unread count client-side."""
        from zerver.actions.users import do_deactivate_user

        self.send_personal_message(self.alice, self.bob, "before leaving")
        do_deactivate_user(self.alice, acting_user=None)

        conversations = self._conversations_for(self.bob)
        one_on_one = [c for c in conversations if c["user_ids"] == [self.alice.id]]
        self.assert_length(one_on_one, 1)
        (alice_entry,) = one_on_one[0]["users"]
        self.assertFalse(alice_entry["is_active"])
        self.assertEqual(one_on_one[0]["unread_count"], 1)

    def test_active_counterpart_flagged_active(self) -> None:
        self.send_personal_message(self.alice, self.bob, "hello")
        conversations = self._conversations_for(self.bob)
        (alice_entry,) = conversations[0]["users"]
        self.assertTrue(alice_entry["is_active"])

    def test_bot_only_conversation_excluded(self) -> None:
        bot = do_create_user(
            email="helper-bot@example.com",
            password=None,
            realm=self.realm,
            full_name="Helper Bot",
            bot_type=UserProfile.DEFAULT_BOT,
            bot_owner=self.bob,
            acting_user=None,
        )
        self.send_personal_message(bot, self.bob, "beep")

        conversations = self._conversations_for(self.bob)
        self.assertNotIn([bot.id], [c["user_ids"] for c in conversations])
        # Per-row counts only — no aggregate that could leak the bot's unreads.
        for c in conversations:
            self.assertEqual(
                set(c.keys()),
                {"user_ids", "users", "last_message", "last_message_id", "unread_count", "muted"},
            )

    def test_self_dm_excluded(self) -> None:
        self.send_personal_message(self.bob, self.bob, "note to self")
        conversations = self._conversations_for(self.bob)
        self.assertNotIn([], [c["user_ids"] for c in conversations])
        self.assertNotIn([self.bob.id], [c["user_ids"] for c in conversations])

    def test_ordering_by_last_message_id_desc(self) -> None:
        """Back-to-back messages share a truncated-seconds timestamp; ordering
        must come from monotonic message ids, not the timestamp."""
        self.send_personal_message(self.alice, self.bob, "first conversation")
        self.send_personal_message(self.carol, self.bob, "second conversation")
        self.send_group_direct_message(self.alice, [self.bob, self.carol], "third conversation")

        conversations = self._conversations_for(self.bob)
        ids = [c["last_message_id"] for c in conversations]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(conversations[0]["user_ids"], sorted([self.alice.id, self.carol.id]))
        for c in conversations:
            self.assertEqual(c["last_message_id"], c["last_message"]["id"])

    def test_deleted_last_message_drops_row_for_this_response(self) -> None:
        """If the newest message vanishes between enumeration and fetch, the
        row is dropped for this response instead of sorting to the bottom with
        a blank preview."""
        self.send_personal_message(self.alice, self.bob, "real message")
        real = self._conversations_for(self.bob)
        self.assert_length([c for c in real if c["user_ids"] == [self.alice.id]], 1)

        with patch(
            "nodl.api.views.messages.get_recent_private_conversations",
            return_value={
                self.alice.recipient_id: {
                    "user_ids": [self.alice.id],
                    "max_message_id": 999_999_999,  # no such message
                }
            },
        ):
            conversations = self._conversations_for(self.bob)
        self.assertEqual(conversations, [])

    def test_preview_is_stripped_ellipsized_with_preview_message_id(self) -> None:
        long_tail = "word " * 40
        message_id = self.send_personal_message(
            self.alice, self.bob, f"**bold** [a link](https://example.com/x) {long_tail}"
        )

        conversations = self._conversations_for(self.bob)
        one_on_one = [c for c in conversations if c["user_ids"] == [self.alice.id]]
        self.assert_length(one_on_one, 1)
        last = one_on_one[0]["last_message"]

        preview = last["content"]
        self.assertNotIn("<", preview)
        self.assertNotIn(">", preview)
        self.assertNotIn("**", preview)  # markdown source not leaked
        self.assertIn("bold", preview)
        self.assertIn("a link", preview)
        self.assertTrue(preview.endswith("…"))
        self.assertLessEqual(len(preview), 101)
        self.assertEqual(last["preview_message_id"], message_id)
        self.assertEqual(last["id"], message_id)
        self.assertEqual(one_on_one[0]["last_message_id"], message_id)

    def test_muted_flag(self) -> None:
        from django.utils.timezone import now

        from zerver.actions.muted_users import do_mute_user

        self.send_personal_message(self.alice, self.bob, "from alice")
        self.send_personal_message(self.carol, self.bob, "from carol")
        self.send_group_direct_message(self.alice, [self.bob, self.carol], "group")
        do_mute_user(self.bob, self.alice, now())

        conversations = self._conversations_for(self.bob)
        by_ids = {tuple(c["user_ids"]): c for c in conversations}
        self.assertTrue(by_ids[(self.alice.id,)]["muted"])
        self.assertFalse(by_ids[(self.carol.id,)]["muted"])
        group_key = tuple(sorted([self.alice.id, self.carol.id]))
        self.assertFalse(by_ids[group_key]["muted"])  # carol not muted

    def test_rate_limiter_fails_open_on_backend_error(self) -> None:
        from zerver.lib.exceptions import RateLimitedError

        self.send_personal_message(self.alice, self.bob, "hi")
        request = RequestFactory().get("/api/v1/dm/conversations")
        request.user_profile = self.bob

        with patch(
            "nodl.api.views.messages.MessagesRateLimitedObject.rate_limit_request",
            side_effect=Exception("redis down"),
        ):
            response = list_dm_conversations(request)
        self.assertEqual(response.status_code, 200)

        with patch(
            "nodl.api.views.messages.MessagesRateLimitedObject.rate_limit_request",
            side_effect=RateLimitedError(30),
        ):
            response = list_dm_conversations(request)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(json.loads(response.content)["retry_after"], 30)

    def test_unread_counts_match_get_raw_unread_data(self) -> None:
        """Invariant: each returned unread_count equals the pm_dict/huddle_dict
        grouping from Zulip's own get_raw_unread_data for that conversation."""
        from zerver.actions.users import do_deactivate_user
        from zerver.lib.message import get_raw_unread_data

        self.send_personal_message(self.alice, self.bob, "one")
        self.send_personal_message(self.alice, self.bob, "two")
        self.send_personal_message(self.carol, self.bob, "three")
        self.send_group_direct_message(self.alice, [self.bob, self.carol], "group one")
        self.send_group_direct_message(self.carol, [self.bob, self.alice], "group two")
        do_deactivate_user(self.carol, acting_user=None)

        raw = get_raw_unread_data(self.bob)
        expected_one_on_one: dict[int, int] = {}
        for _mid, info in raw.pm_dict.items():
            other = info["other_user_id"]
            expected_one_on_one[other] = expected_one_on_one.get(other, 0) + 1
        expected_group: dict[frozenset[int], int] = {}
        for _mid, info in raw.huddle_dict.items():
            ids = frozenset(int(x) for x in info["user_ids_string"].split(",")) - {self.bob.id}
            expected_group[ids] = expected_group.get(ids, 0) + 1

        conversations = self._conversations_for(self.bob)
        for c in conversations:
            ids = c["user_ids"]
            if len(ids) == 1:
                self.assertEqual(
                    c["unread_count"],
                    expected_one_on_one.get(ids[0], 0),
                    f"1:1 with {ids[0]}",
                )
            else:
                self.assertEqual(
                    c["unread_count"],
                    expected_group.get(frozenset(ids), 0),
                    f"group {ids}",
                )
