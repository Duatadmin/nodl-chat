"""Read-access regression tests for the chat read endpoints.

1. Narrow-less GET /api/v1/messages (the combined feed): with no narrow (or
   ``narrow=[]``, what the mobile CombinedFeedNarrow sends) and no stream_id,
   list_messages used to return every message in the realm — other users' DMs
   and private task streams included. Both request shapes must return only
   messages the requester can read, with upstream's combined-feed semantics
   (UserMessage rows + current stream access).
2. The archived-task-stream fallback (messages + streams views) accepted any
   Subscription row, so a member removed from a task stream (row deactivated,
   not deleted) kept reading it. Only active subscriptions count; archiving
   keeps subscriptions active, so subscribers still read archived task streams.
"""

import json
import uuid
from typing import Any
from unittest.mock import patch

from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.utils import timezone

from nodl.api.views.messages import list_messages
from nodl.api.views.streams import get_stream, get_stream_topics
from nodl.extensions.models import NodlTaskStreamExtension
from zerver.actions.create_realm import do_create_realm
from zerver.actions.create_user import do_create_user
from zerver.actions.message_send import check_send_message
from zerver.actions.streams import bulk_add_subscriptions, bulk_remove_subscriptions
from zerver.lib.cache import bounce_key_prefix_for_testing
from zerver.lib.message import bulk_access_messages
from zerver.lib.streams import create_stream_if_needed
from zerver.models import Message, Realm, Stream, UserProfile
from zerver.models.clients import get_client

# The two request shapes that reach the combined feed.
NARROWLESS_QUERIES = {"no narrow": "", "empty narrow": "narrow=[]"}


class ReadAccessTestCase(TestCase):
    """Real realm and users; private streams are task streams built like sync_task_stream."""

    def setUp(self) -> None:
        super().setUp()
        # Per-test cache keys, as ZulipTestCase does.
        bounce_key_prefix_for_testing(self.id())
        self.factory = RequestFactory()
        self.workspace_id = uuid.uuid4()
        self.realm = do_create_realm(
            string_id=uuid.uuid4().hex[:20],
            name="Access Test",
            description="",
            org_type=Realm.ORG_TYPES["business"]["id"],
            create_zulip_discussion_channel=False,
        )
        self.alice = self._make_user("Alice")
        self.bob = self._make_user("Bob")
        self.carol = self._make_user("Carol")
        # External crew join workspaces as guests.
        self.crew = self._make_user("Crew", role=UserProfile.ROLE_GUEST)

    def _make_user(self, full_name: str, role: int | None = None) -> UserProfile:
        return do_create_user(
            email=f"{uuid.uuid4().hex[:12]}@example.com",
            password=None,
            realm=self.realm,
            full_name=full_name,
            role=role,
            acting_user=None,
        )

    def _make_stream(self, subscribers: list[UserProfile], *, private: bool) -> Stream:
        stream, _created = create_stream_if_needed(
            self.realm,
            f"stream-{uuid.uuid4().hex[:12]}",
            invite_only=private,
            history_public_to_subscribers=False if private else None,
            acting_user=None,
        )
        if private:
            NodlTaskStreamExtension.objects.create(
                zulip_realm=self.realm,
                zulip_stream=stream,
                nodl_workspace_id=self.workspace_id,
                nodl_task_id=uuid.uuid4(),
            )
        bulk_add_subscriptions(
            self.realm, [stream], subscribers, from_user_creation=True, acting_user=None
        )
        return stream

    def _archive(self, stream: Stream) -> None:
        # What archive_task_stream leaves behind; subscriptions stay active.
        stream.deactivated = True
        stream.save(update_fields=["deactivated"])
        NodlTaskStreamExtension.objects.filter(zulip_stream=stream).update(
            archived_at=timezone.now()
        )

    def _dm(self, sender: UserProfile, recipients: list[UserProfile]) -> int:
        return check_send_message(
            sender=sender,
            client=get_client("nodl-test"),
            recipient_type_name="private",
            message_to=[user.id for user in recipients],
            topic_name="",
            message_content=f"dm from {sender.full_name}",
            realm=self.realm,
        ).message_id

    def _post(self, sender: UserProfile, stream: Stream) -> int:
        return check_send_message(
            sender=sender,
            client=get_client("nodl-test"),
            recipient_type_name="stream",
            message_to=[stream.id],
            topic_name="task",
            message_content=f"post from {sender.full_name}",
            realm=self.realm,
        ).message_id

    def _get_messages(self, user: UserProfile, query: str) -> HttpResponse:
        request = self.factory.get(f"/api/v1/messages?{query}")
        request.user_profile = user
        response: HttpResponse = list_messages(request)
        return response

    def _list(self, user: UserProfile, query: str) -> dict[str, Any]:
        response = self._get_messages(user, query)
        self.assertEqual(response.status_code, 200, response.content)
        data: dict[str, Any] = json.loads(response.content)
        return data


class TestNarrowlessListMessagesAccess(ReadAccessTestCase):
    def _feed_ids(self, user: UserProfile, query: str) -> list[int]:
        """Message ids in the user's feed; every one must pass upstream's access check."""
        data = self._list(user, "&".join(filter(None, [query, "num_before=200"])))
        ids = [message["id"] for message in data["messages"]]
        accessible = bulk_access_messages(
            user,
            list(Message.objects.filter(id__in=ids).select_related("recipient")),
            is_modifying_message=False,
        )
        self.assertEqual(sorted(ids), sorted(message.id for message in accessible))
        return ids

    def test_member_sees_only_their_own_conversations(self) -> None:
        alice_bob = self._dm(self.alice, [self.bob])
        bob_carol = self._dm(self.bob, [self.carol])
        group = self._dm(self.alice, [self.bob, self.carol])
        alice_task = self._post(self.bob, self._make_stream([self.alice, self.bob], private=True))
        other_task = self._post(self.bob, self._make_stream([self.bob, self.carol], private=True))
        public = self._post(self.bob, self._make_stream([self.alice, self.bob], private=False))

        for label, query in NARROWLESS_QUERIES.items():
            with self.subTest(label):
                ids = self._feed_ids(self.alice, query)
                for mine in (alice_bob, group, alice_task, public):
                    self.assertIn(mine, ids)
                for foreign in (bob_carol, other_task):
                    self.assertNotIn(foreign, ids)

    def test_guest_crew_sees_only_their_task_stream_and_dms(self) -> None:
        crew_task = self._post(self.bob, self._make_stream([self.bob, self.crew], private=True))
        other_task = self._post(self.bob, self._make_stream([self.alice, self.bob], private=True))
        public = self._post(self.bob, self._make_stream([self.alice, self.bob], private=False))
        bob_crew = self._dm(self.bob, [self.crew])
        alice_bob = self._dm(self.alice, [self.bob])

        for label, query in NARROWLESS_QUERIES.items():
            with self.subTest(label):
                ids = self._feed_ids(self.crew, query)
                self.assertIn(crew_task, ids)
                self.assertIn(bob_crew, ids)
                for foreign in (other_task, public, alice_bob):
                    self.assertNotIn(foreign, ids)

    def test_unsubscribed_public_history_is_left_to_the_stream_narrow(self) -> None:
        stream = self._make_stream([self.bob], private=False)
        public = self._post(self.bob, stream)

        for label, query in NARROWLESS_QUERIES.items():
            with self.subTest(label):
                self.assertNotIn(public, self._feed_ids(self.alice, query))
        # Upstream semantics: the history stays readable through the channel.
        narrow = json.dumps([{"operator": "channel", "operand": stream.id}])
        data = self._list(self.alice, f"narrow={narrow}")
        self.assertIn(public, [message["id"] for message in data["messages"]])

    def test_removed_subscriber_loses_the_stream_from_the_feed(self) -> None:
        stream = self._make_stream([self.alice, self.bob], private=True)
        posted = self._post(self.bob, stream)
        self.assertIn(posted, self._feed_ids(self.alice, ""))

        bulk_remove_subscriptions(self.realm, [self.alice], [stream], acting_user=None)

        # Alice keeps a leftover UserMessage row; it must not grant access.
        for label, query in NARROWLESS_QUERIES.items():
            with self.subTest(label):
                self.assertNotIn(posted, self._feed_ids(self.alice, query))

    def test_archived_task_stream_stays_in_subscribers_feed(self) -> None:
        stream = self._make_stream([self.alice, self.bob], private=True)
        posted = self._post(self.bob, stream)
        self._archive(stream)

        for label, query in NARROWLESS_QUERIES.items():
            with self.subTest(label):
                self.assertIn(posted, self._feed_ids(self.alice, query))
                self.assertNotIn(posted, self._feed_ids(self.carol, query))

    def test_anchor_on_a_foreign_message_reveals_nothing(self) -> None:
        bob_carol = self._dm(self.bob, [self.carol])

        for label, query in NARROWLESS_QUERIES.items():
            with self.subTest(label):
                data = self._list(
                    self.alice,
                    "&".join(filter(None, [query, f"anchor={bob_carol}&num_after=0"])),
                )
                self.assertFalse(data["found_anchor"])
                self.assertNotIn(bob_carol, [message["id"] for message in data["messages"]])

    def test_pagination_windows_over_accessible_messages_only(self) -> None:
        self._dm(self.alice, [self.bob])
        second = self._dm(self.bob, [self.alice])
        third = self._dm(self.alice, [self.bob])
        self._dm(self.bob, [self.carol])  # newest overall, not Alice's

        for label, query in NARROWLESS_QUERIES.items():
            with self.subTest(label):
                data = self._list(
                    self.alice, "&".join(filter(None, [query, "anchor=newest&num_before=1"]))
                )
                self.assertEqual([message["id"] for message in data["messages"]], [second, third])
                self.assertEqual(data["anchor"], third)
                self.assertTrue(data["found_newest"])
                self.assertFalse(data["found_oldest"])


@patch("nodl.api.views.streams.StreamsRateLimitedObject.rate_limit_request")
class TestRemovedTaskStreamMemberAccess(ReadAccessTestCase):
    def _stream_queries(self, stream: Stream) -> dict[str, str]:
        narrow = json.dumps([{"operator": "channel", "operand": stream.id}])
        return {"stream_id": f"stream_id={stream.id}", "channel narrow": f"narrow={narrow}"}

    def _stream_views(self, user: UserProfile, stream: Stream) -> dict[str, HttpResponse]:
        responses = {}
        for name, view in (("get_stream", get_stream), ("get_stream_topics", get_stream_topics)):
            request = self.factory.get(f"/api/v1/streams/{stream.id}")
            request.user_profile = user
            responses[name] = view(request, stream.id)
        return responses

    def test_removed_member_cannot_read_the_task_stream(self, _rate_limit: Any) -> None:
        stream = self._make_stream([self.alice, self.bob], private=True)
        self._post(self.bob, stream)
        bulk_remove_subscriptions(self.realm, [self.alice], [stream], acting_user=None)
        self._post(self.bob, stream)  # sent after the removal

        for label, query in self._stream_queries(stream).items():
            with self.subTest(label):
                response = self._get_messages(self.alice, query)
                self.assertEqual(response.status_code, 404, response.content)
        for name, response in self._stream_views(self.alice, stream).items():
            with self.subTest(name):
                self.assertEqual(response.status_code, 404, response.content)

    def test_removed_member_cannot_read_the_archived_task_stream(self, _rate_limit: Any) -> None:
        stream = self._make_stream([self.alice, self.bob], private=True)
        self._post(self.bob, stream)
        bulk_remove_subscriptions(self.realm, [self.alice], [stream], acting_user=None)
        self._archive(stream)

        for label, query in self._stream_queries(stream).items():
            with self.subTest(label):
                response = self._get_messages(self.alice, query)
                self.assertEqual(response.status_code, 404, response.content)
        for name, response in self._stream_views(self.alice, stream).items():
            with self.subTest(name):
                self.assertEqual(response.status_code, 404, response.content)

    def test_subscriber_still_reads_the_archived_task_stream(self, _rate_limit: Any) -> None:
        stream = self._make_stream([self.alice, self.bob], private=True)
        posted = self._post(self.bob, stream)
        self._archive(stream)

        for label, query in self._stream_queries(stream).items():
            with self.subTest(label):
                data = self._list(self.alice, query)
                self.assertEqual([message["id"] for message in data["messages"]], [posted])
                self.assertEqual(self._get_messages(self.carol, query).status_code, 404)
        for name, response in self._stream_views(self.alice, stream).items():
            with self.subTest(name):
                self.assertEqual(response.status_code, 200, response.content)
