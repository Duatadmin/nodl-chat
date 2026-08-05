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
