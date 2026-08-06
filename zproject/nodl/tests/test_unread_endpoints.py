"""Mark-as-read repair (S1): the unread/flags endpoint surface.

Covers:
- update_flags: the shadow of POST /api/v1/messages/flags that the Flutter
  client hits (form-encoded) — delegation to zerver's view, client seeding
- update_flags_narrow: delegation + narrow-scoped mark-read
- get_unread_counts: the /api/v1/unread transform (previously a guaranteed
  TypeError swallowed into an empty "success")
- get_stream_topics: per-topic unread counts (previously hardcoded 0)
- mark_messages_as_read: whole-conversation DM mark-read via dm_user_ids
"""

import json
from unittest.mock import patch
from urllib.parse import urlencode

from django.test import RequestFactory

from nodl.api.views.messages import (
    get_unread_counts,
    mark_messages_as_read,
    update_flags,
    update_flags_narrow,
)
from nodl.api.views.streams import get_stream_topics
from zerver.lib.message import get_raw_unread_data
from zerver.lib.request import RequestNotes
from zerver.models import UserMessage

from .test_task_chats import TaskChatsFixtureMixin


class UnreadEndpointsFixtureMixin(TaskChatsFixtureMixin):
    def setUp(self) -> None:
        super().setUp()
        self.realm, _ = self.make_workspace_realm("Unread WS")
        self.alice = self.make_user(self.realm, "alice@example.com", "Alice")
        self.bob = self.make_user(self.realm, "bob@example.com", "Bob")
        self.carol = self.make_user(self.realm, "carol@example.com", "Carol")

    def _call_messages_view(self, view, request):
        request.user_profile = self.bob
        with patch("nodl.api.views.messages.MessagesRateLimitedObject.rate_limit_request"):
            return view(request)

    def _is_unread(self, user, message_id: int) -> bool:
        um = UserMessage.objects.get(user_profile=user, message_id=message_id)
        return not um.flags.read


class UpdateFlagsShadowTest(UnreadEndpointsFixtureMixin):
    """The JWT shadow of POST /api/v1/messages/flags — mobile's mark-read path."""

    def test_form_encoded_marks_read(self) -> None:
        """Exactly what the Flutter client sends: urlencoded, messages as a
        JSON-string list."""
        m1 = self.send_personal_message(self.alice, self.bob, "one")
        m2 = self.send_personal_message(self.alice, self.bob, "two")
        self.assertTrue(self._is_unread(self.bob, m1))

        request = RequestFactory().post(
            "/api/v1/messages/flags",
            data=urlencode({"messages": json.dumps([m1, m2]), "op": "add", "flag": "read"}),
            content_type="application/x-www-form-urlencoded",
        )
        response = self._call_messages_view(update_flags, request)

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "success")
        self.assertEqual(sorted(data["messages"]), sorted([m1, m2]))
        self.assertIn("ignored_because_not_subscribed_channels", data)
        self.assertFalse(self._is_unread(self.bob, m1))
        self.assertFalse(self._is_unread(self.bob, m2))
        # Delegation contract: the wrapper must seed RequestNotes.client for
        # the upstream view (rest_dispatch normally does this).
        self.assertIsNotNone(RequestNotes.get_notes(request).client)

    def test_json_body_marks_read(self) -> None:
        m1 = self.send_personal_message(self.alice, self.bob, "json one")

        request = RequestFactory().post(
            "/api/v1/messages/flags",
            data=json.dumps({"messages": [m1], "op": "add", "flag": "read"}),
            content_type="application/json",
        )
        response = self._call_messages_view(update_flags, request)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self._is_unread(self.bob, m1))

    def test_remove_op_marks_unread_again(self) -> None:
        m1 = self.send_personal_message(self.alice, self.bob, "flip")
        for op in ("add", "remove"):
            request = RequestFactory().post(
                "/api/v1/messages/flags",
                data=json.dumps({"messages": [m1], "op": op, "flag": "read"}),
                content_type="application/json",
            )
            response = self._call_messages_view(update_flags, request)
            self.assertEqual(response.status_code, 200)
        self.assertTrue(self._is_unread(self.bob, m1))

    def test_invalid_params_are_client_errors(self) -> None:
        request = RequestFactory().post(
            "/api/v1/messages/flags",
            data=json.dumps({"messages": [1], "op": "bogus", "flag": "read"}),
            content_type="application/json",
        )
        response = self._call_messages_view(update_flags, request)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["result"], "error")


class UpdateFlagsNarrowShadowTest(UnreadEndpointsFixtureMixin):
    def test_dm_narrow_marks_conversation_read(self) -> None:
        m1 = self.send_personal_message(self.alice, self.bob, "narrow one")
        m2 = self.send_personal_message(self.alice, self.bob, "narrow two")
        other = self.send_personal_message(self.carol, self.bob, "unrelated")

        request = RequestFactory().post(
            "/api/v1/messages/flags/narrow",
            data=json.dumps(
                {
                    "narrow": [
                        {"operator": "dm", "operand": [self.alice.id]},
                        {"operator": "is", "operand": "unread"},
                    ],
                    "anchor": "oldest",
                    "include_anchor": True,
                    "num_before": 0,
                    "num_after": 1000,
                    "op": "add",
                    "flag": "read",
                }
            ),
            content_type="application/json",
        )
        response = self._call_messages_view(update_flags_narrow, request)

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "success")
        self.assertTrue(data["found_newest"])
        self.assertEqual(data["updated_count"], 2)
        self.assertFalse(self._is_unread(self.bob, m1))
        self.assertFalse(self._is_unread(self.bob, m2))
        # Other conversations untouched.
        self.assertTrue(self._is_unread(self.bob, other))


class GetUnreadCountsTest(UnreadEndpointsFixtureMixin):
    def test_full_shape_streams_topics_dms_huddles(self) -> None:
        stream = self.make_stream("builds", realm=self.realm)
        self.subscribe(self.alice, stream.name)
        self.subscribe(self.bob, stream.name)
        self.send_stream_message(self.alice, stream.name, "a1", topic_name="Alpha")
        self.send_stream_message(self.alice, stream.name, "a2", topic_name="Alpha")
        self.send_stream_message(self.alice, stream.name, "b1", topic_name="beta")
        self.send_personal_message(self.alice, self.bob, "dm hello")
        self.send_group_direct_message(self.alice, [self.bob, self.carol], "group hello")

        request = RequestFactory().get("/api/v1/unread")
        response = self._call_messages_view(get_unread_counts, request)

        self.assertEqual(response.status_code, 200)
        counts = json.loads(response.content)["unread_counts"]

        self.assertEqual(counts[f"stream:{stream.id}"], 3)
        self.assertEqual(counts[f"stream:{stream.id}:topic:Alpha"], 2)
        self.assertEqual(counts[f"stream:{stream.id}:topic:beta"], 1)
        self.assertEqual(counts[f"dm:{self.alice.id}"], 1)
        huddle_keys = [k for k in counts if k.startswith("huddle:")]
        self.assert_length(huddle_keys, 1)
        self.assertEqual(counts[huddle_keys[0]], 1)

        # Values must agree with Zulip's own unread model (filtered per key —
        # onboarding-bot DMs may also exist in pm_dict).
        raw = get_raw_unread_data(self.bob)
        self.assertEqual(
            counts[f"stream:{stream.id}"],
            sum(1 for info in raw["stream_dict"].values() if info["stream_id"] == stream.id),
        )
        self.assertEqual(
            counts[f"dm:{self.alice.id}"],
            sum(1 for info in raw["pm_dict"].values() if info["other_user_id"] == self.alice.id),
        )

    def test_failure_is_not_swallowed_as_success(self) -> None:
        """The old transform crashed on every user with stream unreads and
        returned {"result": "success", "unread_counts": {}} — hiding the
        breakage. Failures must be visible."""
        request = RequestFactory().get("/api/v1/unread")
        with patch("zerver.lib.message.get_raw_unread_data", side_effect=Exception("boom")):
            response = self._call_messages_view(get_unread_counts, request)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(json.loads(response.content)["result"], "error")


class GetStreamTopicsUnreadTest(UnreadEndpointsFixtureMixin):
    def _topics_for(self, stream_id: int) -> list[dict]:
        request = RequestFactory().get(f"/api/v1/streams/{stream_id}/topics")
        request.user_profile = self.bob
        with patch("nodl.api.views.streams.StreamsRateLimitedObject.rate_limit_request"):
            response = get_stream_topics(request, stream_id)
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content)["topics"]

    def test_per_topic_unread_counts(self) -> None:
        stream = self.make_stream("site-log", realm=self.realm)
        self.subscribe(self.alice, stream.name)
        self.subscribe(self.bob, stream.name)
        self.send_stream_message(self.alice, stream.name, "one", topic_name="Deliveries")
        self.send_stream_message(self.alice, stream.name, "two", topic_name="Deliveries")
        read_id = self.send_stream_message(self.alice, stream.name, "seen", topic_name="Safety")
        from zerver.actions.message_flags import do_update_message_flags

        do_update_message_flags(self.bob, "add", "read", [read_id])

        by_name = {t["name"]: t for t in self._topics_for(stream.id)}
        self.assertEqual(by_name["Deliveries"]["unread_count"], 2)
        self.assertEqual(by_name["Safety"]["unread_count"], 0)

    def test_topic_casing_mismatch_still_joins(self) -> None:
        """Topic history canonicalizes to the most recent casing; the unread
        aggregate keeps first-seen casing — the join must be case-insensitive."""
        stream = self.make_stream("casing", realm=self.realm)
        self.subscribe(self.alice, stream.name)
        self.subscribe(self.bob, stream.name)
        self.send_stream_message(self.alice, stream.name, "one", topic_name="Punch List")
        self.send_stream_message(self.alice, stream.name, "two", topic_name="punch list")

        topics = self._topics_for(stream.id)
        self.assert_length(topics, 1)
        self.assertEqual(topics[0]["name"].lower(), "punch list")
        self.assertEqual(topics[0]["unread_count"], 2)


class MarkMessagesAsReadDmTest(UnreadEndpointsFixtureMixin):
    def _mark(self, body: dict):
        request = RequestFactory().post(
            "/api/v1/messages/read",
            data=json.dumps(body),
            content_type="application/json",
        )
        return self._call_messages_view(mark_messages_as_read, request)

    def test_dm_user_ids_marks_whole_one_on_one(self) -> None:
        m1 = self.send_personal_message(self.alice, self.bob, "one")
        m2 = self.send_personal_message(self.alice, self.bob, "two")
        other = self.send_personal_message(self.carol, self.bob, "unrelated")

        response = self._mark({"dm_user_ids": [self.alice.id]})

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "success")
        self.assertEqual(data["messages_marked"], 2)
        self.assertFalse(self._is_unread(self.bob, m1))
        self.assertFalse(self._is_unread(self.bob, m2))
        self.assertTrue(self._is_unread(self.bob, other))

    def test_dm_user_ids_marks_group_conversation(self) -> None:
        group_id = self.send_group_direct_message(self.alice, [self.bob, self.carol], "group hello")
        one_on_one = self.send_personal_message(self.alice, self.bob, "direct")

        response = self._mark({"dm_user_ids": sorted([self.alice.id, self.carol.id])})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["messages_marked"], 1)
        self.assertFalse(self._is_unread(self.bob, group_id))
        # The 1:1 with alice is a different conversation — untouched.
        self.assertTrue(self._is_unread(self.bob, one_on_one))

    def test_dm_user_ids_idempotent_and_validated(self) -> None:
        self.send_personal_message(self.alice, self.bob, "once")
        first = self._mark({"dm_user_ids": [self.alice.id]})
        second = self._mark({"dm_user_ids": [self.alice.id]})
        self.assertEqual(json.loads(first.content)["messages_marked"], 1)
        self.assertEqual(json.loads(second.content)["messages_marked"], 0)

        bad = self._mark({"dm_user_ids": ["not-an-id"]})
        self.assertEqual(bad.status_code, 400)

        missing = self._mark({"dm_user_ids": [999_999]})
        self.assertEqual(missing.status_code, 404)
