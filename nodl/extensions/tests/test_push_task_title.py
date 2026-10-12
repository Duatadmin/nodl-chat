"""Task-chat push titles (W0): the task title, never `#task-<uuid>`.

get_message_stream_name_from_database feeds the payload's stream (legacy) /
channel_name (new) field; iOS builds the alert title from it server-side and
Android builds its title from the same field, so both platforms change with
no app release. Folder membership identifies a task chat, as on mobile.
"""

import uuid

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from nodl.extensions.models import NodlTaskStreamExtension
from nodl.extensions.task_chats import (
    TASK_CHAT_PUSH_FALLBACK_TITLE,
    TASK_CHAT_PUSH_TITLE_MAX_LENGTH,
    get_or_create_task_chats_folder,
    push_channel_display_name,
)
from zerver.actions.create_realm import do_create_realm
from zerver.actions.create_user import do_create_user
from zerver.actions.message_send import check_send_message
from zerver.actions.streams import bulk_add_subscriptions
from zerver.lib.cache import bounce_key_prefix_for_testing
from zerver.lib.push_notifications import (
    get_message_payload,
    get_message_payload_apns,
    get_message_payload_gcm,
    get_message_stream_name_from_database,
)
from zerver.lib.streams import create_stream_if_needed
from zerver.models import ChannelFolder, Message, Realm, Stream, UserProfile
from zerver.models.clients import get_client
from zerver.models.scheduled_jobs import NotificationTriggers

TITLE = "Pour slab B — level 2"


class PushTaskTitleTest(TestCase):
    def setUp(self) -> None:
        super().setUp()
        bounce_key_prefix_for_testing(self.id())
        self.realm = do_create_realm(
            string_id=uuid.uuid4().hex[:20],
            name="Push Title",
            description="",
            org_type=Realm.ORG_TYPES["business"]["id"],
            create_zulip_discussion_channel=False,
        )
        self.sender = self._make_user("Made Manager")
        self.reader = self._make_user("Wayan Worker")

    def _make_user(self, full_name: str) -> UserProfile:
        return do_create_user(
            email=f"{uuid.uuid4().hex[:12]}@example.com",
            password=None,
            realm=self.realm,
            full_name=full_name,
            acting_user=None,
        )

    def _task_stream(self, description: str) -> Stream:
        """A task stream as sync_task_stream builds it: private, filed in the folder."""
        task_id = uuid.uuid4()
        stream, _created = create_stream_if_needed(
            self.realm,
            f"task-{task_id}",
            invite_only=True,
            history_public_to_subscribers=False,
            stream_description=description,
            folder=get_or_create_task_chats_folder(self.realm),
            acting_user=None,
        )
        NodlTaskStreamExtension.objects.create(
            zulip_realm=self.realm,
            zulip_stream=stream,
            nodl_workspace_id=uuid.uuid4(),
            nodl_task_id=task_id,
        )
        bulk_add_subscriptions(self.realm, [stream], [self.sender, self.reader], acting_user=None)
        return stream

    def _ordinary_stream(self) -> Stream:
        stream, _created = create_stream_if_needed(
            self.realm,
            f"site-{uuid.uuid4().hex[:8]}",
            stream_description="Site-wide announcements",
            acting_user=None,
        )
        bulk_add_subscriptions(self.realm, [stream], [self.sender, self.reader], acting_user=None)
        return stream

    def _message(self, stream: Stream) -> Message:
        """Post, then load the message the way handle_push_notification does."""
        message_id = check_send_message(
            sender=self.sender,
            client=get_client("nodl-test"),
            recipient_type_name="stream",
            message_to=[stream.id],
            topic_name="task",
            message_content="Rebar is in, pour at 7",
            realm=self.realm,
        ).message_id
        return Message.objects.select_related("sender", "realm", "recipient").get(id=message_id)

    def test_legacy_payload_carries_the_task_title(self) -> None:
        stream = self._task_stream(TITLE)
        payload = get_message_payload(self.reader, self._message(stream))

        self.assertEqual(payload["stream"], TITLE)
        self.assertEqual(payload["stream_id"], stream.id)

    def test_new_payload_carries_the_task_title(self) -> None:
        stream = self._task_stream(TITLE)
        payload = get_message_payload(self.reader, self._message(stream), for_legacy_clients=False)

        self.assertEqual(payload["channel_name"], TITLE)
        self.assertEqual(payload["channel_id"], stream.id)

    def test_apns_alert_title_uses_the_task_title(self) -> None:
        message = self._message(self._task_stream(TITLE))
        apns = get_message_payload_apns(
            get_message_payload(self.reader, message),
            self.reader,
            message,
            NotificationTriggers.STREAM_PUSH,
        )

        self.assertEqual(apns["alert"]["title"], f"#{TITLE} > task")
        self.assertEqual(apns["custom"]["zulip"]["stream"], TITLE)

    def test_gcm_data_carries_the_task_title(self) -> None:
        message = self._message(self._task_stream(TITLE))
        legacy, _options = get_message_payload_gcm(
            get_message_payload(self.reader, message), self.reader, message
        )
        new, _options = get_message_payload_gcm(
            get_message_payload(self.reader, message, for_legacy_clients=False),
            self.reader,
            message,
            for_legacy_clients=False,
        )

        self.assertEqual(legacy["stream"], TITLE)
        self.assertEqual(new["channel_name"], TITLE)

    def test_untitled_task_shows_task_never_the_uuid(self) -> None:
        for description in (f"Task discussion {uuid.uuid4()}", ""):
            with self.subTest(description=description):
                stream = self._task_stream(description)
                payload = get_message_payload(self.reader, self._message(stream))

                self.assertEqual(payload["stream"], TASK_CHAT_PUSH_FALLBACK_TITLE)
                self.assertNotIn(stream.name, str(payload))

    def test_ordinary_stream_keeps_its_name(self) -> None:
        stream = self._ordinary_stream()
        payload = get_message_payload(self.reader, self._message(stream))

        self.assertEqual(payload["stream"], stream.name)

    def test_long_title_is_cut_to_one_line(self) -> None:
        stream = self._task_stream("Install   waterproofing\nmembrane " + "x" * 300)

        name = get_message_payload(self.reader, self._message(stream))["stream"]

        self.assertEqual(len(name), TASK_CHAT_PUSH_TITLE_MAX_LENGTH)
        self.assertTrue(name.startswith("Install waterproofing membrane x"))
        self.assertTrue(name.endswith("…"))

    def test_query_count_is_unchanged(self) -> None:
        # Upstream ran exactly one query here (Stream by id); the folder must
        # ride that query, for task and ordinary streams alike.
        for stream in (self._task_stream(TITLE), self._ordinary_stream()):
            with self.subTest(stream=stream.name):
                message = self._message(stream)
                with CaptureQueriesContext(connection) as queries:
                    get_message_stream_name_from_database(message)
                self.assertEqual(len(queries), 1)

    def test_only_the_task_chats_folder_counts(self) -> None:
        folder = ChannelFolder(realm=self.realm, name="task chats")
        self.assertEqual(
            push_channel_display_name(Stream(name="task-x", description=TITLE, folder=folder)),
            TITLE,
        )
        for other in (
            ChannelFolder(realm=self.realm, name="Site chats"),
            ChannelFolder(realm=self.realm, name="Task chats", is_archived=True),
            None,
        ):
            with self.subTest(folder=other):
                self.assertEqual(
                    push_channel_display_name(
                        Stream(name="task-x", description=TITLE, folder=other)
                    ),
                    "task-x",
                )
