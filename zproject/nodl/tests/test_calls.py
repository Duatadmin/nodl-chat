import base64
import json
import uuid
from datetime import timedelta
from unittest.mock import ANY, MagicMock, patch
from urllib.parse import urlencode

from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone

from nodl.extensions.mapping import record_realm_user_mapping
from zerver.lib.test_classes import ZulipTestCase
from zerver.models import UserProfile
from zproject.nodl.models import CallRecord, DeviceVoipToken


class CallRecordModelTest(TestCase):
    """Tests for the CallRecord model."""

    def setUp(self) -> None:
        users = UserProfile.objects.filter(is_active=True)[:2]
        assert len(users) >= 2
        self.caller = users[0]
        self.callee = users[1]

    def test_create_call_record(self) -> None:
        call = CallRecord.objects.create(
            room_name="call-test-123",
            caller=self.caller,
            callee=self.callee,
            status="ringing",
        )
        self.assertIsNotNone(call.id)
        self.assertEqual(call.status, "ringing")
        self.assertEqual(call.room_name, "call-test-123")
        self.assertIsNotNone(call.initiated_at)
        self.assertIsNone(call.answered_at)
        self.assertIsNone(call.ended_at)
        self.assertIsNone(call.duration_seconds)
        self.assertIsNone(call.end_reason)

    def test_uuid_primary_key(self) -> None:
        call = CallRecord.objects.create(
            room_name="call-uuid-test",
            caller=self.caller,
            callee=self.callee,
        )
        self.assertIsInstance(call.id, uuid.UUID)

    def test_status_choices(self) -> None:
        for status in ["ringing", "connected", "ended", "missed", "declined", "cancelled"]:
            call = CallRecord.objects.create(
                room_name=f"call-{status}",
                caller=self.caller,
                callee=self.callee,
                status=status,
            )
            self.assertEqual(call.status, status)

    def test_ordering(self) -> None:
        call1 = CallRecord.objects.create(
            room_name="call-old",
            caller=self.caller,
            callee=self.callee,
        )
        call2 = CallRecord.objects.create(
            room_name="call-new",
            caller=self.caller,
            callee=self.callee,
        )
        calls = list(CallRecord.objects.all())
        self.assertEqual(calls[0].id, call2.id)
        self.assertEqual(calls[1].id, call1.id)


class DeviceVoipTokenModelTest(TestCase):
    """Tests for the DeviceVoipToken model (AC:2)."""

    def setUp(self) -> None:
        self.user = UserProfile.objects.filter(is_active=True).first()
        assert self.user is not None

    def test_create_token(self) -> None:
        token = DeviceVoipToken.objects.create(
            user=self.user,
            platform="ios",
            voip_token="test-voip-token-abc",
            device_id="device-001",
        )
        self.assertIsNotNone(token.id)
        self.assertIsInstance(token.id, uuid.UUID)
        self.assertEqual(token.platform, "ios")
        self.assertEqual(token.voip_token, "test-voip-token-abc")
        self.assertIsNone(token.fcm_token)
        self.assertTrue(token.is_active)
        self.assertIsNotNone(token.created_at)
        self.assertIsNotNone(token.updated_at)

    def test_create_android_token(self) -> None:
        token = DeviceVoipToken.objects.create(
            user=self.user,
            platform="android",
            fcm_token="test-fcm-token-xyz",
            device_id="device-002",
        )
        self.assertEqual(token.platform, "android")
        self.assertEqual(token.fcm_token, "test-fcm-token-xyz")
        self.assertIsNone(token.voip_token)

    def test_unique_user_device_constraint(self) -> None:
        DeviceVoipToken.objects.create(
            user=self.user,
            platform="ios",
            voip_token="token-1",
            device_id="device-unique",
        )
        with self.assertRaises(IntegrityError):
            DeviceVoipToken.objects.create(
                user=self.user,
                platform="ios",
                voip_token="token-2",
                device_id="device-unique",  # same device_id
            )

    def test_is_active_default_true(self) -> None:
        token = DeviceVoipToken.objects.create(
            user=self.user,
            platform="ios",
            device_id="device-active-test",
        )
        self.assertTrue(token.is_active)

    def test_soft_delete(self) -> None:
        token = DeviceVoipToken.objects.create(
            user=self.user,
            platform="android",
            fcm_token="token-soft",
            device_id="device-soft",
        )
        token.is_active = False
        token.save(update_fields=["is_active"])
        token.refresh_from_db()
        self.assertFalse(token.is_active)


MOCK_LIVEKIT_ENV = {
    "LIVEKIT_URL": "wss://test.livekit.cloud",
    "LIVEKIT_API_KEY": "test-api-key",
    "LIVEKIT_API_SECRET": "test-api-secret-that-is-long-enough-for-hs256-algorithm",
}


class CallViewsTest(ZulipTestCase):
    """Tests for call signaling API endpoints."""

    def setUp(self) -> None:
        super().setUp()
        self.caller = self.example_user("hamlet")
        self.callee = self.example_user("othello")
        # The views fire background threads for room provisioning + push
        # dispatch; real threads must not run against the test transaction.
        setup_patcher = patch("zproject.nodl.views.calls._start_call_setup_async")
        self.mock_call_setup = setup_patcher.start()
        self.addCleanup(setup_patcher.stop)
        event_patcher = patch("zproject.nodl.views.calls.dispatch_call_event_push_async")
        self.mock_event_push = event_patcher.start()
        self.addCleanup(event_patcher.stop)
        room_delete_patcher = patch("zproject.nodl.views.calls.delete_room_async")
        self.mock_room_delete = room_delete_patcher.start()
        self.addCleanup(room_delete_patcher.stop)

    def _auth_headers(self, user: UserProfile | None = None) -> dict[str, str]:
        u = user or self.caller
        cred = base64.b64encode(f"{u.delivery_email}:{u.api_key}".encode()).decode()
        return {"HTTP_AUTHORIZATION": f"Basic {cred}"}

    def _create_ringing_call(self) -> CallRecord:
        """Helper: create a call in ringing state."""
        return CallRecord.objects.create(
            room_name=f"call-{uuid.uuid4()}",
            caller=self.caller,
            callee=self.callee,
            status="ringing",
        )

    def _create_connected_call(self) -> CallRecord:
        """Helper: create a call in connected state."""
        return CallRecord.objects.create(
            room_name=f"call-{uuid.uuid4()}",
            caller=self.caller,
            callee=self.callee,
            status="connected",
            answered_at=timezone.now(),
        )

    # === Happy path: initiate → accept → end ===

    @patch.dict("os.environ", MOCK_LIVEKIT_ENV)
    @patch("zproject.nodl.services.livekit_service.LIVEKIT_URL", MOCK_LIVEKIT_ENV["LIVEKIT_URL"])
    @patch(
        "zproject.nodl.services.livekit_service.LIVEKIT_API_KEY",
        MOCK_LIVEKIT_ENV["LIVEKIT_API_KEY"],
    )
    @patch(
        "zproject.nodl.services.livekit_service.LIVEKIT_API_SECRET",
        MOCK_LIVEKIT_ENV["LIVEKIT_API_SECRET"],
    )
    def test_happy_path_initiate_accept_end(self) -> None:
        # Initiate
        result = self.client_post(
            "/nodl/calls/initiate",
            json.dumps({"callee_id": self.callee.id}),
            content_type="application/json",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["result"], "success")
        self.assertIn("call_id", data)
        self.assertIn("room_name", data)
        self.assertIn("livekit_url", data)
        self.assertIn("token", data)

        # Room provisioning + callee push run off the critical path.
        self.mock_call_setup.assert_called_once_with(
            callee_id=self.callee.id,
            call_id=data["call_id"],
            room_name=data["room_name"],
            caller_name=ANY,
            caller_avatar_url="",
            caller_id=self.caller.id,
        )

        call_id = data["call_id"]

        # Verify call record created
        call = CallRecord.objects.get(id=call_id)
        self.assertEqual(call.status, "ringing")
        self.assertEqual(call.caller_id, self.caller.id)
        self.assertEqual(call.callee_id, self.callee.id)

        # Accept (as callee)
        result = self.client_post(
            f"/nodl/calls/{call_id}/accept",
            content_type="application/json",
            **self._auth_headers(self.callee),
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["result"], "success")
        self.assertIn("token", data)
        self.assertIn("call_id", data)
        self.assertIn("room_name", data)
        self.assertIn("livekit_url", data)
        self.assertEqual(data["caller_id"], self.caller.id)
        self.assertEqual(data["callee_id"], self.callee.id)

        call.refresh_from_db()
        self.assertEqual(call.status, "connected")
        self.assertIsNotNone(call.answered_at)

        # End (as caller)
        result = self.client_post(
            f"/nodl/calls/{call_id}/end",
            content_type="application/json",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["result"], "success")

        call.refresh_from_db()
        self.assertEqual(call.status, "ended")
        self.assertIsNotNone(call.ended_at)
        self.assertIsNotNone(call.duration_seconds)
        self.assertEqual(call.end_reason, "caller_hangup")

    # === Decline flow ===

    @patch.dict("os.environ", MOCK_LIVEKIT_ENV)
    @patch("zproject.nodl.services.livekit_service.LIVEKIT_URL", MOCK_LIVEKIT_ENV["LIVEKIT_URL"])
    @patch(
        "zproject.nodl.services.livekit_service.LIVEKIT_API_KEY",
        MOCK_LIVEKIT_ENV["LIVEKIT_API_KEY"],
    )
    @patch(
        "zproject.nodl.services.livekit_service.LIVEKIT_API_SECRET",
        MOCK_LIVEKIT_ENV["LIVEKIT_API_SECRET"],
    )
    def test_decline_flow(self) -> None:
        call = self._create_ringing_call()

        result = self.client_post(
            f"/nodl/calls/{call.id}/decline",
            content_type="application/json",
            **self._auth_headers(self.callee),
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["result"], "success")

        call.refresh_from_db()
        self.assertEqual(call.status, "declined")
        self.assertIsNotNone(call.ended_at)
        self.assertEqual(call.end_reason, "callee_declined")

    # === Cancel flow ===

    def test_cancel_flow(self) -> None:
        call = self._create_ringing_call()

        result = self.client_post(
            f"/nodl/calls/{call.id}/cancel",
            content_type="application/json",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["result"], "success")

        call.refresh_from_db()
        self.assertEqual(call.status, "cancelled")
        self.assertIsNotNone(call.ended_at)
        self.assertEqual(call.end_reason, "caller_cancelled")

    def test_cancel_deletes_livekit_room(self) -> None:
        """Cancel closes the room so a stale accept can't join it."""
        call = self._create_ringing_call()
        result = self.client_post(
            f"/nodl/calls/{call.id}/cancel",
            content_type="application/json",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 200)
        self.mock_room_delete.assert_called_once_with(call.room_name)

    def test_decline_deletes_livekit_room(self) -> None:
        call = self._create_ringing_call()
        result = self.client_post(
            f"/nodl/calls/{call.id}/decline",
            content_type="application/json",
            **self._auth_headers(self.callee),
        )
        self.assertEqual(result.status_code, 200)
        self.mock_room_delete.assert_called_once_with(call.room_name)

    def test_failed_cancel_does_not_delete_room(self) -> None:
        """A cancel rejected by the state guard must leave the room alone."""
        call = self._create_connected_call()
        result = self.client_post(
            f"/nodl/calls/{call.id}/cancel",
            content_type="application/json",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 409)
        self.mock_room_delete.assert_not_called()

    # === Ring-status watchdog probe ===

    def test_ring_status_ringing(self) -> None:
        """Unauthenticated probe reports a ringing call as ringing."""
        call = self._create_ringing_call()
        result = self.client_get(f"/nodl/calls/{call.id}/ring-status")
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["result"], "success")
        self.assertTrue(data["ringing"])

    def test_ring_status_cancelled(self) -> None:
        call = self._create_ringing_call()
        call.status = "cancelled"
        call.save(update_fields=["status"])
        result = self.client_get(f"/nodl/calls/{call.id}/ring-status")
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["ringing"])

    def test_ring_status_connected_not_ringing(self) -> None:
        """Accepted (possibly on a sibling device) reads as not ringing."""
        call = self._create_connected_call()
        result = self.client_get(f"/nodl/calls/{call.id}/ring-status")
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["ringing"])

    def test_ring_status_unknown_call_404(self) -> None:
        result = self.client_get(f"/nodl/calls/{uuid.uuid4()}/ring-status")
        self.assertEqual(result.status_code, 404)

    def test_ring_status_invalid_id_400(self) -> None:
        result = self.client_get("/nodl/calls/not-a-uuid/ring-status")
        self.assertEqual(result.status_code, 400)

    def test_ring_status_post_not_allowed(self) -> None:
        call = self._create_ringing_call()
        result = self.client_post(
            f"/nodl/calls/{call.id}/ring-status",
            content_type="application/json",
        )
        self.assertEqual(result.status_code, 405)

    # === Race conditions ===

    @patch.dict("os.environ", MOCK_LIVEKIT_ENV)
    @patch("zproject.nodl.services.livekit_service.LIVEKIT_URL", MOCK_LIVEKIT_ENV["LIVEKIT_URL"])
    @patch(
        "zproject.nodl.services.livekit_service.LIVEKIT_API_KEY",
        MOCK_LIVEKIT_ENV["LIVEKIT_API_KEY"],
    )
    @patch(
        "zproject.nodl.services.livekit_service.LIVEKIT_API_SECRET",
        MOCK_LIVEKIT_ENV["LIVEKIT_API_SECRET"],
    )
    def test_accept_while_cancelled(self) -> None:
        """AC:9 — accept after cancel returns error."""
        call = self._create_ringing_call()

        # Cancel first
        self.client_post(
            f"/nodl/calls/{call.id}/cancel",
            content_type="application/json",
            **self._auth_headers(self.caller),
        )

        # Try to accept
        result = self.client_post(
            f"/nodl/calls/{call.id}/accept",
            content_type="application/json",
            **self._auth_headers(self.callee),
        )
        self.assertEqual(result.status_code, 409)
        data = result.json()
        self.assertEqual(data["result"], "error")
        self.assertIn("cancelled", data["msg"])

    def test_simultaneous_end_idempotent(self) -> None:
        """AC:8 — second /end returns 200 OK."""
        call = self._create_connected_call()

        # First end
        result = self.client_post(
            f"/nodl/calls/{call.id}/end",
            content_type="application/json",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 200)

        # Second end (idempotent)
        result = self.client_post(
            f"/nodl/calls/{call.id}/end",
            content_type="application/json",
            **self._auth_headers(self.callee),
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["result"], "success")

    @patch.dict("os.environ", MOCK_LIVEKIT_ENV)
    @patch("zproject.nodl.services.livekit_service.LIVEKIT_URL", MOCK_LIVEKIT_ENV["LIVEKIT_URL"])
    @patch(
        "zproject.nodl.services.livekit_service.LIVEKIT_API_KEY",
        MOCK_LIVEKIT_ENV["LIVEKIT_API_KEY"],
    )
    @patch(
        "zproject.nodl.services.livekit_service.LIVEKIT_API_SECRET",
        MOCK_LIVEKIT_ENV["LIVEKIT_API_SECRET"],
    )
    def test_multi_device_accept_first_wins(self) -> None:
        """AC:9 — multi-device accept: first wins, subsequent get error."""
        call = self._create_ringing_call()

        # First accept succeeds
        result = self.client_post(
            f"/nodl/calls/{call.id}/accept",
            content_type="application/json",
            **self._auth_headers(self.callee),
        )
        self.assertEqual(result.status_code, 200)

        # Second accept fails (status is now 'connected', not 'ringing')
        result = self.client_post(
            f"/nodl/calls/{call.id}/accept",
            content_type="application/json",
            **self._auth_headers(self.callee),
        )
        self.assertEqual(result.status_code, 409)

    # === History pagination ===

    def test_history_pagination(self) -> None:
        """AC:10 — paginated call records, newest first."""
        # Create 5 calls
        for i in range(5):
            CallRecord.objects.create(
                room_name=f"call-hist-{i}",
                caller=self.caller,
                callee=self.callee,
                status="ended",
            )

        result = self.client_get(
            "/nodl/calls/history?limit=3&offset=0",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["result"], "success")
        self.assertEqual(len(data["calls"]), 3)

        # Page 2
        result = self.client_get(
            "/nodl/calls/history?limit=3&offset=3",
            **self._auth_headers(self.caller),
        )
        data = result.json()
        self.assertEqual(len(data["calls"]), 2)

    def test_history_default_pagination(self) -> None:
        """Default limit=20, offset=0."""
        result = self.client_get(
            "/nodl/calls/history",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["result"], "success")
        self.assertIn("calls", data)

    def test_history_cursor_does_not_skip_after_new_insertion(self) -> None:
        base = timezone.now()
        original = [
            CallRecord.objects.create(
                room_name=f"call-cursor-{i}",
                caller=self.caller,
                callee=self.callee,
                status="ended",
                initiated_at=base - timedelta(minutes=i),
            )
            for i in range(4)
        ]
        first = self.client_get(
            "/nodl/calls/history?limit=2",
            **self._auth_headers(self.caller),
        ).json()["calls"]
        self.assertEqual([row["call_id"] for row in first], [str(c.id) for c in original[:2]])

        CallRecord.objects.create(
            room_name="call-cursor-inserted",
            caller=self.caller,
            callee=self.callee,
            status="ended",
            initiated_at=base + timedelta(minutes=1),
        )
        query = urlencode(
            {
                "limit": 2,
                "offset": 2,
                "before_initiated_at": first[-1]["initiated_at"],
                "before_call_id": first[-1]["call_id"],
            }
        )
        second = self.client_get(
            f"/nodl/calls/history?{query}",
            **self._auth_headers(self.caller),
        ).json()["calls"]

        self.assertEqual([row["call_id"] for row in second], [str(c.id) for c in original[2:]])

    def test_history_shows_both_directions(self) -> None:
        """User sees calls where they are caller OR callee."""
        # Call where user is caller
        CallRecord.objects.create(
            room_name="call-outgoing",
            caller=self.caller,
            callee=self.callee,
            status="ended",
        )
        # Call where user is callee
        CallRecord.objects.create(
            room_name="call-incoming",
            caller=self.callee,
            callee=self.caller,
            status="ended",
        )

        result = self.client_get(
            "/nodl/calls/history",
            **self._auth_headers(self.caller),
        )
        data = result.json()
        self.assertEqual(len(data["calls"]), 2)

    def test_history_includes_remote_global_identity_and_email(self) -> None:
        """History exposes identity hints without broadening realm auth."""
        nodl_user_id = uuid.uuid4()
        record_realm_user_mapping(self.callee.realm, self.callee, nodl_user_id)
        CallRecord.objects.create(
            room_name="call-identity",
            caller=self.caller,
            callee=self.callee,
            status="ended",
        )

        result = self.client_get(
            "/nodl/calls/history",
            **self._auth_headers(self.caller),
        )

        self.assertEqual(result.status_code, 200)
        call = result.json()["calls"][0]
        self.assertEqual(call["remote_nodl_user_id"], str(nodl_user_id))
        self.assertEqual(call["remote_email"], self.callee.delivery_email)

    def test_history_identity_is_nullable_for_unmapped_profiles(self) -> None:
        CallRecord.objects.create(
            room_name="call-legacy-identity",
            caller=self.caller,
            callee=self.callee,
            status="ended",
        )

        result = self.client_get(
            "/nodl/calls/history",
            **self._auth_headers(self.caller),
        )

        call = result.json()["calls"][0]
        self.assertIsNone(call["remote_nodl_user_id"])
        self.assertEqual(call["remote_email"], self.callee.delivery_email)

    # === Authorization ===

    def test_accept_only_callee(self) -> None:
        """Caller cannot accept their own call."""
        call = self._create_ringing_call()

        result = self.client_post(
            f"/nodl/calls/{call.id}/accept",
            content_type="application/json",
            **self._auth_headers(self.caller),  # caller trying to accept
        )
        self.assertEqual(result.status_code, 403)

    def test_cancel_only_caller(self) -> None:
        """Callee cannot cancel."""
        call = self._create_ringing_call()

        result = self.client_post(
            f"/nodl/calls/{call.id}/cancel",
            content_type="application/json",
            **self._auth_headers(self.callee),  # callee trying to cancel
        )
        self.assertEqual(result.status_code, 403)

    def test_decline_only_callee(self) -> None:
        """Caller cannot decline their own call."""
        call = self._create_ringing_call()

        result = self.client_post(
            f"/nodl/calls/{call.id}/decline",
            content_type="application/json",
            **self._auth_headers(self.caller),  # caller trying to decline
        )
        self.assertEqual(result.status_code, 403)

    def test_end_only_participants(self) -> None:
        """Third party cannot end a call."""
        call = self._create_connected_call()
        third_party = self.example_user("cordelia")

        result = self.client_post(
            f"/nodl/calls/{call.id}/end",
            content_type="application/json",
            **self._auth_headers(third_party),
        )
        self.assertEqual(result.status_code, 403)

    def test_call_detail_authorization(self) -> None:
        """AC:11 — only caller or callee can view."""
        call = self._create_ringing_call()
        third_party = self.example_user("cordelia")

        # Participant can view
        result = self.client_get(
            f"/nodl/calls/{call.id}",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 200)

        # Third party cannot view
        result = self.client_get(
            f"/nodl/calls/{call.id}",
            **self._auth_headers(third_party),
        )
        self.assertEqual(result.status_code, 403)

    def test_unauthorized_request(self) -> None:
        """Endpoints require authentication."""
        result = self.client_post("/nodl/calls/initiate")
        self.assertEqual(result.status_code, 401)

    # === Response format ===

    def test_response_format_zulip_wrapper(self) -> None:
        """AC:12 — responses use Zulip wrapper format."""
        call = self._create_ringing_call()

        # Success response
        result = self.client_post(
            f"/nodl/calls/{call.id}/cancel",
            content_type="application/json",
            **self._auth_headers(self.caller),
        )
        data = result.json()
        self.assertIn("result", data)
        self.assertIn("msg", data)
        self.assertEqual(data["result"], "success")
        self.assertEqual(data["msg"], "")

    def test_error_response_format(self) -> None:
        """Error responses also use Zulip wrapper."""
        result = self.client_post(
            "/nodl/calls/initiate",
            json.dumps({}),
            content_type="application/json",
            **self._auth_headers(self.caller),
        )
        data = result.json()
        self.assertIn("result", data)
        self.assertIn("msg", data)
        self.assertEqual(data["result"], "error")

    def test_call_detail_response_fields(self) -> None:
        """AC:11 — detail returns full record with snake_case."""
        call = self._create_connected_call()

        result = self.client_get(
            f"/nodl/calls/{call.id}",
            **self._auth_headers(self.caller),
        )
        data = result.json()
        call_data = data["call"]
        self.assertIn("call_id", call_data)
        self.assertIn("room_name", call_data)
        self.assertIn("caller_id", call_data)
        self.assertIn("callee_id", call_data)
        self.assertIn("status", call_data)
        self.assertIn("initiated_at", call_data)
        self.assertIn("answered_at", call_data)
        self.assertIn("ended_at", call_data)
        self.assertIn("duration_seconds", call_data)
        self.assertIn("end_reason", call_data)

    # === Edge cases ===

    @patch.dict("os.environ", MOCK_LIVEKIT_ENV)
    @patch("zproject.nodl.services.livekit_service.LIVEKIT_URL", MOCK_LIVEKIT_ENV["LIVEKIT_URL"])
    @patch(
        "zproject.nodl.services.livekit_service.LIVEKIT_API_KEY",
        MOCK_LIVEKIT_ENV["LIVEKIT_API_KEY"],
    )
    @patch(
        "zproject.nodl.services.livekit_service.LIVEKIT_API_SECRET",
        MOCK_LIVEKIT_ENV["LIVEKIT_API_SECRET"],
    )
    def test_initiate_invalid_callee(self) -> None:
        result = self.client_post(
            "/nodl/calls/initiate",
            json.dumps({"callee_id": 999999}),
            content_type="application/json",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 400)

    @patch.dict("os.environ", MOCK_LIVEKIT_ENV)
    @patch("zproject.nodl.services.livekit_service.LIVEKIT_URL", MOCK_LIVEKIT_ENV["LIVEKIT_URL"])
    @patch(
        "zproject.nodl.services.livekit_service.LIVEKIT_API_KEY",
        MOCK_LIVEKIT_ENV["LIVEKIT_API_KEY"],
    )
    @patch(
        "zproject.nodl.services.livekit_service.LIVEKIT_API_SECRET",
        MOCK_LIVEKIT_ENV["LIVEKIT_API_SECRET"],
    )
    def test_initiate_call_self(self) -> None:
        result = self.client_post(
            "/nodl/calls/initiate",
            json.dumps({"callee_id": self.caller.id}),
            content_type="application/json",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 400)
        self.assertIn("Cannot call yourself", result.json()["msg"])

    @patch.dict("os.environ", MOCK_LIVEKIT_ENV)
    @patch("zproject.nodl.services.livekit_service.LIVEKIT_URL", MOCK_LIVEKIT_ENV["LIVEKIT_URL"])
    @patch(
        "zproject.nodl.services.livekit_service.LIVEKIT_API_KEY",
        MOCK_LIVEKIT_ENV["LIVEKIT_API_KEY"],
    )
    @patch(
        "zproject.nodl.services.livekit_service.LIVEKIT_API_SECRET",
        MOCK_LIVEKIT_ENV["LIVEKIT_API_SECRET"],
    )
    def test_initiate_cross_realm_callee_rejected(self) -> None:
        """A callee in another realm is invisible — 400, no call record."""
        from zerver.actions.create_realm import do_create_realm
        from zerver.actions.create_user import do_create_user
        from zerver.models import Realm

        other_realm = do_create_realm(
            string_id="other-realm-calls",
            name="Other Realm",
            description="",
            org_type=Realm.ORG_TYPES["business"]["id"],
            create_zulip_discussion_channel=False,
        )
        stranger = do_create_user(
            email="stranger@example.com",
            password=None,
            realm=other_realm,
            full_name="Stranger",
            acting_user=None,
        )

        result = self.client_post(
            "/nodl/calls/initiate",
            json.dumps({"callee_id": stranger.id}),
            content_type="application/json",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 400)
        self.assertIn("Callee not found", result.json()["msg"])
        self.assertFalse(CallRecord.objects.filter(callee=stranger).exists())

    def test_call_not_found(self) -> None:
        fake_id = str(uuid.uuid4())
        result = self.client_post(
            f"/nodl/calls/{fake_id}/accept",
            content_type="application/json",
            **self._auth_headers(self.callee),
        )
        self.assertEqual(result.status_code, 404)

    def test_invalid_call_id_format(self) -> None:
        result = self.client_post(
            "/nodl/calls/not-a-uuid/accept",
            content_type="application/json",
            **self._auth_headers(self.callee),
        )
        self.assertEqual(result.status_code, 400)

    def test_callee_end_sets_callee_hangup(self) -> None:
        """End reason is callee_hangup when callee ends the call."""
        call = self._create_connected_call()

        self.client_post(
            f"/nodl/calls/{call.id}/end",
            content_type="application/json",
            **self._auth_headers(self.callee),
        )

        call.refresh_from_db()
        self.assertEqual(call.end_reason, "callee_hangup")

    def test_initiate_invalid_json(self) -> None:
        result = self.client_post(
            "/nodl/calls/initiate",
            "not-json",
            content_type="application/json",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 400)

    def test_initiate_missing_callee_id(self) -> None:
        result = self.client_post(
            "/nodl/calls/initiate",
            json.dumps({}),
            content_type="application/json",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 400)
        self.assertIn("callee_id", result.json()["msg"])

    def test_initiate_callee_id_string_type(self) -> None:
        """Fix #3: callee_id as non-integer string returns 400, not 500."""
        result = self.client_post(
            "/nodl/calls/initiate",
            json.dumps({"callee_id": "abc"}),
            content_type="application/json",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 400)
        self.assertIn("integer", result.json()["msg"])

    def test_initiate_callee_id_float_type(self) -> None:
        """callee_id as float is rejected with 400."""
        result = self.client_post(
            "/nodl/calls/initiate",
            json.dumps({"callee_id": 3.14}),
            content_type="application/json",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 400)
        self.assertIn("integer", result.json()["msg"])

    def test_initiate_callee_id_bool_type(self) -> None:
        """callee_id as bool is rejected with 400."""
        result = self.client_post(
            "/nodl/calls/initiate",
            json.dumps({"callee_id": True}),
            content_type="application/json",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 400)
        self.assertIn("integer", result.json()["msg"])


class CallBusyAndSignalingTest(ZulipTestCase):
    """Busy rejection, stale-call self-healing, and lifecycle event pushes."""

    def setUp(self) -> None:
        super().setUp()
        self.caller = self.example_user("hamlet")
        self.callee = self.example_user("othello")
        self.third = self.example_user("cordelia")
        setup_patcher = patch("zproject.nodl.views.calls._start_call_setup_async")
        self.mock_call_setup = setup_patcher.start()
        self.addCleanup(setup_patcher.stop)
        event_patcher = patch("zproject.nodl.views.calls.dispatch_call_event_push_async")
        self.mock_event_push = event_patcher.start()
        self.addCleanup(event_patcher.stop)
        room_delete_patcher = patch("zproject.nodl.views.calls.delete_room_async")
        self.mock_room_delete = room_delete_patcher.start()
        self.addCleanup(room_delete_patcher.stop)

    def _auth_headers(self, user: UserProfile) -> dict[str, str]:
        cred = base64.b64encode(f"{user.delivery_email}:{user.api_key}".encode()).decode()
        return {"HTTP_AUTHORIZATION": f"Basic {cred}"}

    def _initiate(self, caller: UserProfile, callee: UserProfile):
        return self.client_post(
            "/nodl/calls/initiate",
            json.dumps({"callee_id": callee.id}),
            content_type="application/json",
            **self._auth_headers(caller),
        )

    # === Busy handling ===

    def test_initiate_caller_busy(self) -> None:
        CallRecord.objects.create(
            room_name="call-busy-caller",
            caller=self.caller,
            callee=self.third,
            status="connected",
            answered_at=timezone.now(),
        )
        result = self._initiate(self.caller, self.callee)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["code"], "CALLER_BUSY")
        self.mock_call_setup.assert_not_called()

    def test_initiate_callee_busy(self) -> None:
        CallRecord.objects.create(
            room_name="call-busy-callee",
            caller=self.third,
            callee=self.callee,
            status="ringing",
        )
        result = self._initiate(self.caller, self.callee)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["code"], "CALLEE_BUSY")
        self.mock_call_setup.assert_not_called()

    def test_initiate_callee_busy_as_caller_elsewhere(self) -> None:
        """Callee who is themselves ringing someone else counts as busy."""
        CallRecord.objects.create(
            room_name="call-busy-callee-outgoing",
            caller=self.callee,
            callee=self.third,
            status="ringing",
        )
        result = self._initiate(self.caller, self.callee)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["code"], "CALLEE_BUSY")

    # === Stale-call self-healing ===

    def test_stale_ringing_call_does_not_block(self) -> None:
        """A ringing record from a crashed client expires instead of blocking."""
        stale = CallRecord.objects.create(
            room_name="call-stale-ringing",
            caller=self.third,
            callee=self.callee,
            status="ringing",
        )
        CallRecord.objects.filter(id=stale.id).update(
            initiated_at=timezone.now() - timezone.timedelta(minutes=2),
        )

        result = self._initiate(self.caller, self.callee)
        self.assertEqual(result.status_code, 200)

        stale.refresh_from_db()
        self.assertEqual(stale.status, "missed")
        self.assertEqual(stale.end_reason, "timeout")
        self.assertIsNotNone(stale.ended_at)

    def test_fresh_ringing_call_still_blocks(self) -> None:
        CallRecord.objects.create(
            room_name="call-fresh-ringing",
            caller=self.third,
            callee=self.callee,
            status="ringing",
        )
        result = self._initiate(self.caller, self.callee)
        self.assertEqual(result.status_code, 409)

    def test_stale_connected_call_does_not_block(self) -> None:
        stale = CallRecord.objects.create(
            room_name="call-stale-connected",
            caller=self.caller,
            callee=self.third,
            status="connected",
            answered_at=timezone.now() - timezone.timedelta(days=2),
        )
        result = self._initiate(self.caller, self.callee)
        self.assertEqual(result.status_code, 200)

        stale.refresh_from_db()
        self.assertEqual(stale.status, "ended")
        self.assertEqual(stale.end_reason, "error")

    # === Lifecycle event pushes ===

    def _create_ringing_call(self) -> CallRecord:
        return CallRecord.objects.create(
            room_name=f"call-{uuid.uuid4()}",
            caller=self.caller,
            callee=self.callee,
            status="ringing",
        )

    def test_cancel_pushes_call_cancelled_to_callee(self) -> None:
        call = self._create_ringing_call()
        result = self.client_post(
            f"/nodl/calls/{call.id}/cancel",
            content_type="application/json",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 200)
        self.mock_event_push.assert_called_once_with(
            self.callee.id, "call_cancelled", str(call.id))

    def test_decline_pushes_call_declined_to_caller(self) -> None:
        call = self._create_ringing_call()
        result = self.client_post(
            f"/nodl/calls/{call.id}/decline",
            content_type="application/json",
            **self._auth_headers(self.callee),
        )
        self.assertEqual(result.status_code, 200)
        self.mock_event_push.assert_called_once_with(
            self.caller.id, "call_declined", str(call.id))

    def test_end_pushes_call_ended_to_other_party(self) -> None:
        call = CallRecord.objects.create(
            room_name=f"call-{uuid.uuid4()}",
            caller=self.caller,
            callee=self.callee,
            status="connected",
            answered_at=timezone.now(),
        )
        result = self.client_post(
            f"/nodl/calls/{call.id}/end",
            content_type="application/json",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 200)
        self.mock_event_push.assert_called_once_with(
            self.callee.id, "call_ended", str(call.id))

    def test_failed_cancel_no_event_push(self) -> None:
        """A 409 cancel (wrong state) must not push a dismissal."""
        call = CallRecord.objects.create(
            room_name=f"call-{uuid.uuid4()}",
            caller=self.caller,
            callee=self.callee,
            status="connected",
            answered_at=timezone.now(),
        )
        result = self.client_post(
            f"/nodl/calls/{call.id}/cancel",
            content_type="application/json",
            **self._auth_headers(self.caller),
        )
        self.assertEqual(result.status_code, 409)
        self.mock_event_push.assert_not_called()


class RunCallSetupTest(TestCase):
    """Unit tests for the background call-setup worker."""

    @patch("zproject.nodl.views.calls.dispatch_call_push")
    @patch("zproject.nodl.views.calls.create_room_sync")
    def test_creates_room_then_dispatches_push(
        self, mock_create_room: MagicMock, mock_push: MagicMock
    ) -> None:
        from zproject.nodl.views.calls import _run_call_setup

        mock_create_room.return_value = {"name": "call-x", "sid": "RM_x"}
        _run_call_setup(
            callee_id=7,
            call_id="call-id-1",
            room_name="call-x",
            caller_name="Hamlet",
            caller_avatar_url="",
            caller_id=3,
        )
        mock_create_room.assert_called_once_with(
            "call-x", max_participants=2, empty_timeout=35)
        mock_push.assert_called_once_with(
            callee_id=7,
            call_id="call-id-1",
            room_name="call-x",
            caller_name="Hamlet",
            caller_avatar_url="",
            caller_id=3,
        )

    @patch("zproject.nodl.views.calls.dispatch_call_push")
    @patch("zproject.nodl.views.calls.create_room_sync")
    def test_room_creation_failure_still_pushes(
        self, mock_create_room: MagicMock, mock_push: MagicMock
    ) -> None:
        """Token-embedded room config makes room creation non-fatal — the
        callee must still be rung."""
        from zproject.nodl.views.calls import _run_call_setup

        mock_create_room.side_effect = RuntimeError("livekit down")
        _run_call_setup(
            callee_id=7,
            call_id="call-id-2",
            room_name="call-y",
            caller_name="Hamlet",
            caller_avatar_url="",
        )
        mock_push.assert_called_once()

    def _make_call(self, status: str) -> CallRecord:
        users = list(UserProfile.objects.filter(is_active=True)[:2])
        assert len(users) >= 2
        return CallRecord.objects.create(
            room_name=f"call-{uuid.uuid4()}",
            caller=users[0],
            callee=users[1],
            status=status,
        )

    @patch("zproject.nodl.views.calls.dispatch_call_push")
    @patch("zproject.nodl.views.calls.create_room_sync")
    def test_cancelled_call_skips_ring_push(
        self, mock_create_room: MagicMock, mock_push: MagicMock
    ) -> None:
        """A cancel that lands during room provisioning must stop the ring
        push — once the VoIP push is out, PushKit forces the phone to ring."""
        from zproject.nodl.views.calls import _run_call_setup

        call = self._make_call("cancelled")
        mock_create_room.return_value = {"name": call.room_name, "sid": "RM_z"}
        _run_call_setup(
            callee_id=call.callee_id,
            call_id=str(call.id),
            room_name=call.room_name,
            caller_name="Hamlet",
            caller_avatar_url="",
        )
        mock_push.assert_not_called()

    @patch("zproject.nodl.views.calls.dispatch_call_push")
    @patch("zproject.nodl.views.calls.create_room_sync")
    def test_still_ringing_call_pushes(
        self, mock_create_room: MagicMock, mock_push: MagicMock
    ) -> None:
        from zproject.nodl.views.calls import _run_call_setup

        call = self._make_call("ringing")
        mock_create_room.return_value = {"name": call.room_name, "sid": "RM_r"}
        _run_call_setup(
            callee_id=call.callee_id,
            call_id=str(call.id),
            room_name=call.room_name,
            caller_name="Hamlet",
            caller_avatar_url="",
        )
        mock_push.assert_called_once()

    @patch("zproject.nodl.views.calls.dispatch_call_push")
    @patch("zproject.nodl.views.calls.create_room_sync")
    def test_missing_record_fails_open_and_pushes(
        self, mock_create_room: MagicMock, mock_push: MagicMock
    ) -> None:
        """A guard lookup failure must never eat a real ring."""
        from zproject.nodl.views.calls import _run_call_setup

        mock_create_room.return_value = {"name": "call-q", "sid": "RM_q"}
        _run_call_setup(
            callee_id=7,
            call_id=str(uuid.uuid4()),
            room_name="call-q",
            caller_name="Hamlet",
            caller_avatar_url="",
        )
        mock_push.assert_called_once()
