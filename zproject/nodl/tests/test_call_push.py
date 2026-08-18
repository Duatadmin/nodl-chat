import base64
import json
import threading
import uuid
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import TestCase, override_settings

from nodl.extensions.mapping import record_realm_user_mapping, resolve_human_profile_ids
from nodl.extensions.models import NodlRealmExtension, SyncStatus
from zerver.actions.create_realm import do_create_realm
from zerver.actions.create_user import do_create_user
from zerver.lib.test_classes import ZulipTestCase
from zerver.models import Realm, UserProfile
from zproject.nodl.models import CallRecord, DeviceVoipToken
from zproject.nodl.services.call_push_service import (
    dispatch_call_push,
    dispatch_call_push_async,
)


def default_recipient_kwargs(callee_id: int, workspace_id: str = "") -> dict[str, str]:
    """The recipient-identity kwargs dispatch passes to every send call."""
    return {
        "recipient_user_id": str(callee_id),
        "recipient_realm_url": settings.ROOT_DOMAIN_URI,
        "recipient_workspace_id": workspace_id,
    }


# ===== VoIP Token Endpoint Tests =====


class VoipTokenEndpointTest(ZulipTestCase):
    """Tests for POST /nodl/devices/voip-token and DELETE /nodl/devices/voip-token/unregister."""

    def setUp(self) -> None:
        super().setUp()
        self.user = self.example_user("hamlet")

    def _auth_headers(self, user: UserProfile | None = None) -> dict[str, str]:
        u = user or self.user
        cred = base64.b64encode(f"{u.delivery_email}:{u.api_key}".encode()).decode()
        return {"HTTP_AUTHORIZATION": f"Basic {cred}"}

    # --- Registration (POST) ---

    def test_register_ios_token(self) -> None:
        result = self.client_post(
            "/nodl/devices/voip-token",
            json.dumps({
                "platform": "ios",
                "device_id": "iphone-001",
                "voip_token": "apns-token-abc",
            }),
            content_type="application/json",
            **self._auth_headers(),
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["result"], "success")
        self.assertEqual(data["device_id"], "iphone-001")
        self.assertTrue(data["created"])

        # Verify DB
        token = DeviceVoipToken.objects.get(user=self.user, device_id="iphone-001")
        self.assertEqual(token.platform, "ios")
        self.assertEqual(token.voip_token, "apns-token-abc")
        self.assertIsNone(token.fcm_token)
        self.assertTrue(token.is_active)

    def test_register_android_token(self) -> None:
        result = self.client_post(
            "/nodl/devices/voip-token",
            json.dumps({
                "platform": "android",
                "device_id": "pixel-001",
                "fcm_token": "fcm-token-xyz",
            }),
            content_type="application/json",
            **self._auth_headers(),
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["result"], "success")
        self.assertTrue(data["created"])

        token = DeviceVoipToken.objects.get(user=self.user, device_id="pixel-001")
        self.assertEqual(token.platform, "android")
        self.assertEqual(token.fcm_token, "fcm-token-xyz")
        self.assertIsNone(token.voip_token)

    def test_register_upsert_updates_existing(self) -> None:
        """Re-registering same device_id updates the token (upsert)."""
        DeviceVoipToken.objects.create(
            user=self.user,
            platform="ios",
            device_id="iphone-001",
            voip_token="old-token",
            is_active=False,
        )

        result = self.client_post(
            "/nodl/devices/voip-token",
            json.dumps({
                "platform": "ios",
                "device_id": "iphone-001",
                "voip_token": "new-token",
            }),
            content_type="application/json",
            **self._auth_headers(),
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertFalse(data["created"])  # Updated, not created

        token = DeviceVoipToken.objects.get(user=self.user, device_id="iphone-001")
        self.assertEqual(token.voip_token, "new-token")
        self.assertTrue(token.is_active)  # Re-activated

    def test_register_ios_missing_voip_token(self) -> None:
        """iOS registration without voip_token returns 400."""
        result = self.client_post(
            "/nodl/devices/voip-token",
            json.dumps({
                "platform": "ios",
                "device_id": "iphone-001",
            }),
            content_type="application/json",
            **self._auth_headers(),
        )
        self.assertEqual(result.status_code, 400)
        self.assertIn("voip_token", result.json()["msg"])

    def test_register_android_missing_fcm_token(self) -> None:
        """Android registration without fcm_token returns 400."""
        result = self.client_post(
            "/nodl/devices/voip-token",
            json.dumps({
                "platform": "android",
                "device_id": "pixel-001",
            }),
            content_type="application/json",
            **self._auth_headers(),
        )
        self.assertEqual(result.status_code, 400)
        self.assertIn("fcm_token", result.json()["msg"])

    def test_register_missing_platform(self) -> None:
        result = self.client_post(
            "/nodl/devices/voip-token",
            json.dumps({"device_id": "dev-001", "voip_token": "t"}),
            content_type="application/json",
            **self._auth_headers(),
        )
        self.assertEqual(result.status_code, 400)
        self.assertIn("platform", result.json()["msg"])

    def test_register_invalid_platform(self) -> None:
        result = self.client_post(
            "/nodl/devices/voip-token",
            json.dumps({
                "platform": "windows",
                "device_id": "dev-001",
                "fcm_token": "t",
            }),
            content_type="application/json",
            **self._auth_headers(),
        )
        self.assertEqual(result.status_code, 400)
        self.assertIn("ios", result.json()["msg"])

    def test_register_missing_device_id(self) -> None:
        result = self.client_post(
            "/nodl/devices/voip-token",
            json.dumps({"platform": "ios", "voip_token": "t"}),
            content_type="application/json",
            **self._auth_headers(),
        )
        self.assertEqual(result.status_code, 400)
        self.assertIn("device_id", result.json()["msg"])

    # --- Unregistration (DELETE) ---

    def test_unregister_existing_token(self) -> None:
        DeviceVoipToken.objects.create(
            user=self.user,
            platform="ios",
            device_id="iphone-001",
            voip_token="token-abc",
        )

        result = self.client_delete(
            "/nodl/devices/voip-token/unregister",
            json.dumps({"device_id": "iphone-001"}),
            content_type="application/json",
            **self._auth_headers(),
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["result"], "success")
        self.assertTrue(data["deactivated"])

        token = DeviceVoipToken.objects.get(user=self.user, device_id="iphone-001")
        self.assertFalse(token.is_active)

    def test_unregister_nonexistent_token(self) -> None:
        """Unregistering a device that doesn't exist returns success (idempotent)."""
        result = self.client_delete(
            "/nodl/devices/voip-token/unregister",
            json.dumps({"device_id": "nonexistent-device"}),
            content_type="application/json",
            **self._auth_headers(),
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["result"], "success")
        self.assertFalse(data["deactivated"])

    def test_unregister_missing_device_id(self) -> None:
        result = self.client_delete(
            "/nodl/devices/voip-token/unregister",
            json.dumps({}),
            content_type="application/json",
            **self._auth_headers(),
        )
        self.assertEqual(result.status_code, 400)
        self.assertIn("device_id", result.json()["msg"])

    def test_unregister_only_own_tokens(self) -> None:
        """User can only unregister their own tokens."""
        other_user = self.example_user("othello")
        DeviceVoipToken.objects.create(
            user=other_user,
            platform="ios",
            device_id="othello-phone",
            voip_token="other-token",
        )

        result = self.client_delete(
            "/nodl/devices/voip-token/unregister",
            json.dumps({"device_id": "othello-phone"}),
            content_type="application/json",
            **self._auth_headers(),  # hamlet's auth
        )
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["deactivated"])

        # Other user's token is still active
        token = DeviceVoipToken.objects.get(user=other_user, device_id="othello-phone")
        self.assertTrue(token.is_active)


# ===== Push Dispatch Service Tests =====


class DispatchCallPushTest(TestCase):
    """Tests for dispatch_call_push and individual push senders."""

    def setUp(self) -> None:
        users = UserProfile.objects.filter(is_active=True)[:2]
        assert len(users) >= 2
        self.caller = users[0]
        self.callee = users[1]
        self.call_id = str(uuid.uuid4())
        self.room_name = f"call-{uuid.uuid4()}"

    @patch("zproject.nodl.services.call_push_service.send_voip_push_ios")
    @patch("zproject.nodl.services.call_push_service.send_fcm_call_data")
    def test_dispatch_ios_only(self, mock_fcm: MagicMock, mock_ios: MagicMock) -> None:
        """Dispatch sends VoIP push to iOS device."""
        DeviceVoipToken.objects.create(
            user=self.callee,
            platform="ios",
            device_id="iphone-001",
            voip_token="apns-token",
        )

        dispatch_call_push(
            self.callee.id, self.call_id, self.room_name, "Caller", "https://avatar.url"
        )

        mock_ios.assert_called_once_with(
            "apns-token", self.call_id, self.room_name, "Caller", "https://avatar.url",
            **default_recipient_kwargs(self.callee.id),
        )
        mock_fcm.assert_not_called()

    @patch("zproject.nodl.services.call_push_service.send_voip_push_ios")
    @patch("zproject.nodl.services.call_push_service.send_fcm_call_data")
    def test_dispatch_android_only(self, mock_fcm: MagicMock, mock_ios: MagicMock) -> None:
        """Dispatch sends FCM data message to Android device."""
        DeviceVoipToken.objects.create(
            user=self.callee,
            platform="android",
            device_id="pixel-001",
            fcm_token="fcm-token",
        )

        dispatch_call_push(
            self.callee.id, self.call_id, self.room_name, "Caller", ""
        )

        mock_fcm.assert_called_once_with(
            "fcm-token", self.call_id, self.room_name, "Caller", "",
            **default_recipient_kwargs(self.callee.id),
        )
        mock_ios.assert_not_called()

    @patch("zproject.nodl.services.call_push_service.send_voip_push_ios")
    @patch("zproject.nodl.services.call_push_service.send_fcm_call_data")
    def test_dispatch_multi_device(self, mock_fcm: MagicMock, mock_ios: MagicMock) -> None:
        """Dispatch sends to all active devices (iOS + Android)."""
        DeviceVoipToken.objects.create(
            user=self.callee,
            platform="ios",
            device_id="iphone-001",
            voip_token="apns-token-1",
        )
        DeviceVoipToken.objects.create(
            user=self.callee,
            platform="android",
            device_id="pixel-001",
            fcm_token="fcm-token-1",
        )

        dispatch_call_push(
            self.callee.id, self.call_id, self.room_name, "Caller", ""
        )

        self.assertEqual(mock_ios.call_count, 1)
        self.assertEqual(mock_fcm.call_count, 1)

    @patch("zproject.nodl.services.call_push_service.send_voip_push_ios")
    @patch("zproject.nodl.services.call_push_service.send_fcm_call_data")
    def test_dispatch_skips_inactive_tokens(self, mock_fcm: MagicMock, mock_ios: MagicMock) -> None:
        """Inactive tokens are not dispatched to."""
        DeviceVoipToken.objects.create(
            user=self.callee,
            platform="ios",
            device_id="iphone-old",
            voip_token="old-token",
            is_active=False,
        )

        dispatch_call_push(
            self.callee.id, self.call_id, self.room_name, "Caller", ""
        )

        mock_ios.assert_not_called()
        mock_fcm.assert_not_called()

    @patch("zproject.nodl.services.call_push_service.send_voip_push_ios")
    @patch("zproject.nodl.services.call_push_service.send_fcm_call_data")
    def test_dispatch_no_tokens(self, mock_fcm: MagicMock, mock_ios: MagicMock) -> None:
        """No tokens for callee — dispatch logs and returns silently."""
        dispatch_call_push(
            self.callee.id, self.call_id, self.room_name, "Caller", ""
        )

        mock_ios.assert_not_called()
        mock_fcm.assert_not_called()

    @patch("zproject.nodl.services.call_push_service.send_voip_push_ios")
    @patch("zproject.nodl.services.call_push_service.send_fcm_call_data")
    def test_dispatch_skips_ios_with_missing_voip_token(
        self, mock_fcm: MagicMock, mock_ios: MagicMock
    ) -> None:
        """iOS device with null voip_token is skipped (ledger advisory)."""
        DeviceVoipToken.objects.create(
            user=self.callee,
            platform="ios",
            device_id="iphone-broken",
            voip_token=None,
        )

        dispatch_call_push(
            self.callee.id, self.call_id, self.room_name, "Caller", ""
        )

        mock_ios.assert_not_called()

    @patch("zproject.nodl.services.call_push_service.send_voip_push_ios")
    @patch("zproject.nodl.services.call_push_service.send_fcm_call_data")
    def test_dispatch_skips_android_with_missing_fcm_token(
        self, mock_fcm: MagicMock, mock_ios: MagicMock
    ) -> None:
        """Android device with null fcm_token is skipped (ledger advisory)."""
        DeviceVoipToken.objects.create(
            user=self.callee,
            platform="android",
            device_id="pixel-broken",
            fcm_token=None,
        )

        dispatch_call_push(
            self.callee.id, self.call_id, self.room_name, "Caller", ""
        )

        mock_fcm.assert_not_called()


class DispatchCallPushFanOutTest(ZulipTestCase):
    """Cross-workspace fan-out: one human, N sibling profiles, dedup by token.

    A device may register its push token under any of the human's per-realm
    profiles; dispatch must find it regardless of which profile is being
    called, and one physical device must ring exactly once.
    """

    def setUp(self) -> None:
        super().setUp()
        self.supabase_id = uuid.uuid4()
        self.realm_a, self.ws_a = self._make_workspace_realm("Workspace A")
        self.realm_b, self.ws_b = self._make_workspace_realm("Workspace B")
        self.profile_a = self._make_mapped_profile(
            self.realm_a, "human-a@example.com", self.supabase_id
        )
        self.profile_b = self._make_mapped_profile(
            self.realm_b, "human-b@example.com", self.supabase_id
        )
        self.call_id = str(uuid.uuid4())
        self.room_name = f"call-{uuid.uuid4()}"

    def _make_workspace_realm(self, name: str) -> tuple[Realm, uuid.UUID]:
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

    def _make_mapped_profile(
        self, realm: Realm, email: str, supabase_id: uuid.UUID
    ) -> UserProfile:
        profile = do_create_user(
            email=email,
            password=None,
            realm=realm,
            full_name="Fan-out Test Human",
            acting_user=None,
        )
        assert record_realm_user_mapping(realm, profile, supabase_id) is not None
        return profile

    def test_resolve_human_profile_ids(self) -> None:
        ids = resolve_human_profile_ids(self.profile_a.id)
        self.assertEqual(sorted(ids), sorted([self.profile_a.id, self.profile_b.id]))

    def test_resolve_without_mapping_returns_self(self) -> None:
        unmapped = self.example_user("hamlet")
        self.assertEqual(resolve_human_profile_ids(unmapped.id), [unmapped.id])

    @patch("zproject.nodl.services.call_push_service.send_voip_push_ios")
    @patch("zproject.nodl.services.call_push_service.send_fcm_call_data")
    def test_token_under_sibling_profile_is_found(
        self, mock_fcm: MagicMock, mock_ios: MagicMock
    ) -> None:
        """Device registered under profile B still rings when profile A is called."""
        DeviceVoipToken.objects.create(
            user=self.profile_b,
            platform="ios",
            device_id="iphone-001",
            voip_token="apns-token-b",
        )

        dispatch_call_push(
            self.profile_a.id, self.call_id, self.room_name, "Caller", ""
        )

        mock_ios.assert_called_once_with(
            "apns-token-b", self.call_id, self.room_name, "Caller", "",
            **default_recipient_kwargs(self.profile_a.id, str(self.ws_a)),
        )

    @patch("zproject.nodl.services.call_push_service.send_voip_push_ios")
    @patch("zproject.nodl.services.call_push_service.send_fcm_call_data")
    def test_same_token_under_both_profiles_rings_once(
        self, mock_fcm: MagicMock, mock_ios: MagicMock
    ) -> None:
        """One device registered under both sibling profiles → exactly one push."""
        for profile in (self.profile_a, self.profile_b):
            DeviceVoipToken.objects.create(
                user=profile,
                platform="ios",
                device_id="iphone-001",
                voip_token="apns-token-shared",
            )

        dispatch_call_push(
            self.profile_a.id, self.call_id, self.room_name, "Caller", ""
        )

        self.assertEqual(mock_ios.call_count, 1)

    @patch("zproject.nodl.services.call_push_service.send_voip_push_ios")
    @patch("zproject.nodl.services.call_push_service.send_fcm_call_data")
    def test_two_devices_across_profiles_ring_both(
        self, mock_fcm: MagicMock, mock_ios: MagicMock
    ) -> None:
        DeviceVoipToken.objects.create(
            user=self.profile_a,
            platform="ios",
            device_id="iphone-001",
            voip_token="apns-token-1",
        )
        DeviceVoipToken.objects.create(
            user=self.profile_b,
            platform="android",
            device_id="pixel-001",
            fcm_token="fcm-token-2",
        )
        mock_fcm.return_value = "sent"

        dispatch_call_push(
            self.profile_a.id, self.call_id, self.room_name, "Caller", ""
        )

        self.assertEqual(mock_ios.call_count, 1)
        self.assertEqual(mock_fcm.call_count, 1)

    @patch("zproject.nodl.services.call_push_service.send_voip_push_ios")
    @patch("zproject.nodl.services.call_push_service.send_fcm_call_data")
    def test_rotated_token_rows_both_tried(
        self, mock_fcm: MagicMock, mock_ios: MagicMock
    ) -> None:
        """Same device_id, different token under each profile → both are tried."""
        DeviceVoipToken.objects.create(
            user=self.profile_a,
            platform="android",
            device_id="pixel-001",
            fcm_token="fcm-token-old",
        )
        DeviceVoipToken.objects.create(
            user=self.profile_b,
            platform="android",
            device_id="pixel-001",
            fcm_token="fcm-token-new",
        )
        mock_fcm.return_value = "sent"

        dispatch_call_push(
            self.profile_a.id, self.call_id, self.room_name, "Caller", ""
        )

        self.assertEqual(mock_fcm.call_count, 2)

    @patch("zproject.nodl.services.call_push_service.send_voip_push_ios")
    @patch("zproject.nodl.services.call_push_service.send_fcm_call_data")
    def test_unregistered_token_deactivated_across_profiles(
        self, mock_fcm: MagicMock, mock_ios: MagicMock
    ) -> None:
        """A stale FCM token is deactivated on every sibling row carrying it."""
        for profile in (self.profile_a, self.profile_b):
            DeviceVoipToken.objects.create(
                user=profile,
                platform="android",
                device_id="pixel-001",
                fcm_token="fcm-token-stale",
            )
        mock_fcm.return_value = "unregistered"

        dispatch_call_push(
            self.profile_a.id, self.call_id, self.room_name, "Caller", ""
        )

        self.assertEqual(
            DeviceVoipToken.objects.filter(
                fcm_token="fcm-token-stale", is_active=True
            ).count(),
            0,
        )

    @patch("zproject.nodl.services.call_push_service.send_voip_push_ios")
    @patch("zproject.nodl.services.call_push_service.send_fcm_call_data")
    def test_push_device_token_fallback_spans_profiles(
        self, mock_fcm: MagicMock, mock_ios: MagicMock
    ) -> None:
        """PushDeviceToken fallback considers sibling profiles too."""
        from zerver.models import PushDeviceToken

        PushDeviceToken.objects.create(
            user=self.profile_b,
            kind=PushDeviceToken.FCM,
            token="zulip-fcm-token-b",
        )
        mock_fcm.return_value = "sent"

        dispatch_call_push(
            self.profile_a.id, self.call_id, self.room_name, "Caller", ""
        )

        mock_fcm.assert_called_once_with(
            "zulip-fcm-token-b", self.call_id, self.room_name, "Caller", "",
            **default_recipient_kwargs(self.profile_a.id, str(self.ws_a)),
        )

    @patch("zproject.nodl.services.call_push_service.send_voip_push_ios")
    @patch("zproject.nodl.services.call_push_service.send_fcm_call_data")
    def test_fallback_skips_token_already_tried(
        self, mock_fcm: MagicMock, mock_ios: MagicMock
    ) -> None:
        """A token that failed via DeviceVoipToken is not retried via fallback."""
        from zerver.models import PushDeviceToken

        DeviceVoipToken.objects.create(
            user=self.profile_a,
            platform="android",
            device_id="pixel-001",
            fcm_token="fcm-token-dup",
        )
        PushDeviceToken.objects.create(
            user=self.profile_a,
            kind=PushDeviceToken.FCM,
            token="fcm-token-dup",
        )
        mock_fcm.return_value = "error"

        dispatch_call_push(
            self.profile_a.id, self.call_id, self.room_name, "Caller", ""
        )

        self.assertEqual(mock_fcm.call_count, 1)


class DispatchCallPushAsyncTest(TestCase):
    """Tests for fire-and-forget async dispatch."""

    def setUp(self) -> None:
        users = UserProfile.objects.filter(is_active=True)[:2]
        assert len(users) >= 2
        self.caller = users[0]
        self.callee = users[1]

    @patch("zproject.nodl.services.call_push_service.dispatch_call_push")
    def test_async_dispatch_spawns_thread(self, mock_dispatch: MagicMock) -> None:
        """dispatch_call_push_async spawns a daemon thread."""
        call_id = str(uuid.uuid4())
        room_name = f"call-{uuid.uuid4()}"

        dispatch_call_push_async(
            self.callee.id, call_id, room_name, "Caller", ""
        )

        # Wait for thread to complete
        for t in threading.enumerate():
            if t.daemon and t.is_alive():
                t.join(timeout=2)

        mock_dispatch.assert_called_once_with(
            self.callee.id, call_id, room_name, "Caller", ""
        )


class SendVoipPushIosTest(TestCase):
    """Tests for send_voip_push_ios."""

    FAKE_PEM = "-----BEGIN PRIVATE KEY-----\nfake-key-material\n-----END PRIVATE KEY-----\n"

    def _apns_config(self, **overrides: object) -> "patch._patch":
        """Patch the module-level APNs config constants (read at import time,
        so os.environ patching does nothing)."""
        import zproject.nodl.services.call_push_service as cps

        config = {
            "APNS_KEY_ID": "ABC123DEFG",
            "APNS_TEAM_ID": "8Z2F9BY77D",
            "APNS_BUNDLE_ID": "tech.nodle.mobile",
            "APNS_AUTH_KEY_B64": base64.b64encode(self.FAKE_PEM.encode()).decode(),
            "APNS_AUTH_KEY_PATH": "",
            **overrides,
        }
        return patch.multiple(cps, **config)

    def test_missing_apns_credentials_returns_false(self) -> None:
        """Returns False when APNs credentials are not configured."""
        from zproject.nodl.services.call_push_service import send_voip_push_ios

        with patch.dict("os.environ", {}, clear=False):
            result = send_voip_push_ios(
                "token", "call-id", "room", "Caller", ""
            )
            self.assertFalse(result)

    def test_b64_key_sends_voip_push_with_required_headers(self) -> None:
        """APNS_AUTH_KEY_B64 (no file) is decoded and the push carries the
        VoIP essentials: .voip topic, apns-push-type voip, priority 10, TTL."""
        from unittest.mock import AsyncMock

        from aioapns import PushType

        import zproject.nodl.services.call_push_service as cps

        with self._apns_config(), \
                patch("aioapns.APNs") as mock_apns_cls, \
                patch("aioapns.NotificationRequest") as mock_request_cls:
            mock_apns_cls.return_value.send_notification = AsyncMock(
                return_value=MagicMock(is_successful=True)
            )

            result = cps.send_voip_push_ios(
                "voip-token", "call-id", "room", "Caller", "",
                recipient_user_id="57",
            )

        self.assertTrue(result)
        client_kwargs = mock_apns_cls.call_args.kwargs
        self.assertEqual(client_kwargs["key"], self.FAKE_PEM)
        self.assertEqual(client_kwargs["key_id"], "ABC123DEFG")
        self.assertEqual(client_kwargs["team_id"], "8Z2F9BY77D")
        self.assertEqual(client_kwargs["topic"], "tech.nodle.mobile.voip")
        request_kwargs = mock_request_cls.call_args.kwargs
        self.assertEqual(request_kwargs["push_type"], PushType.VOIP)
        self.assertEqual(request_kwargs["priority"], 10)
        self.assertEqual(request_kwargs["time_to_live"], cps.APNS_VOIP_TTL_SECONDS)
        self.assertEqual(request_kwargs["message"]["recipient_user_id"], "57")

    def test_send_works_from_loopless_thread(self) -> None:
        """Regression: dispatch runs in a fire-and-forget thread with NO
        asyncio event loop; aioapns grabs the loop at client construction,
        which raised 'There is no current event loop in thread' in prod
        (2026-08-18). The send must succeed from a bare worker thread."""
        from unittest.mock import AsyncMock

        import zproject.nodl.services.call_push_service as cps

        results: list[bool] = []
        with self._apns_config(), \
                patch("aioapns.APNs") as mock_apns_cls, \
                patch("aioapns.NotificationRequest"):
            mock_apns_cls.return_value.send_notification = AsyncMock(
                return_value=MagicMock(is_successful=True)
            )
            t = threading.Thread(target=lambda: results.append(
                cps.send_voip_push_ios("voip-token", "call-id", "room", "C", "")
            ))
            t.start()
            t.join(timeout=10)

        self.assertEqual(results, [True])

    def test_bad_device_token_retries_other_environment(self) -> None:
        """Env mismatch (dev-signed = sandbox token, TestFlight = production
        token): production BadDeviceToken → one sandbox retry, and vice versa."""
        from unittest.mock import AsyncMock

        import zproject.nodl.services.call_push_service as cps

        bad = MagicMock(is_successful=False, description="BadDeviceToken")
        good = MagicMock(is_successful=True)
        with self._apns_config(APNS_USE_SANDBOX=False), \
                patch("aioapns.APNs") as mock_apns_cls, \
                patch("aioapns.NotificationRequest"):
            mock_apns_cls.return_value.send_notification = AsyncMock(
                side_effect=[bad, good]
            )
            result = cps.send_voip_push_ios(
                "voip-token", "call-id", "room", "C", ""
            )

        self.assertTrue(result)
        envs = [c.kwargs["use_sandbox"] for c in mock_apns_cls.call_args_list]
        self.assertEqual(envs, [False, True])

    def test_non_token_failure_does_not_retry(self) -> None:
        """Only BadDeviceToken triggers the environment retry."""
        from unittest.mock import AsyncMock

        import zproject.nodl.services.call_push_service as cps

        bad = MagicMock(is_successful=False, description="TooManyRequests")
        with self._apns_config(APNS_USE_SANDBOX=False), \
                patch("aioapns.APNs") as mock_apns_cls, \
                patch("aioapns.NotificationRequest"):
            mock_apns_cls.return_value.send_notification = AsyncMock(
                return_value=bad
            )
            result = cps.send_voip_push_ios(
                "voip-token", "call-id", "room", "C", ""
            )

        self.assertFalse(result)
        self.assertEqual(mock_apns_cls.call_count, 1)

    def test_invalid_b64_key_returns_false(self) -> None:
        """Garbage APNS_AUTH_KEY_B64 fails soft (skip, never raise)."""
        import zproject.nodl.services.call_push_service as cps

        with self._apns_config(APNS_AUTH_KEY_B64="!!!not-base64!!!"):
            result = cps.send_voip_push_ios(
                "voip-token", "call-id", "room", "Caller", ""
            )
        self.assertFalse(result)

    def test_file_path_fallback_still_works(self) -> None:
        """Without B64, the key is read from APNS_AUTH_KEY_PATH as before."""
        import tempfile
        from unittest.mock import AsyncMock

        import zproject.nodl.services.call_push_service as cps

        with tempfile.NamedTemporaryFile("w", suffix=".p8") as f:
            f.write(self.FAKE_PEM)
            f.flush()
            with self._apns_config(APNS_AUTH_KEY_B64="", APNS_AUTH_KEY_PATH=f.name), \
                    patch("aioapns.APNs") as mock_apns_cls, \
                    patch("aioapns.NotificationRequest"):
                mock_apns_cls.return_value.send_notification = AsyncMock(
                    return_value=MagicMock(is_successful=True)
                )
                result = cps.send_voip_push_ios(
                    "voip-token", "call-id", "room", "Caller", ""
                )

        self.assertTrue(result)
        self.assertEqual(mock_apns_cls.call_args.kwargs["key"], self.FAKE_PEM)


class SendFcmCallDataTest(TestCase):
    """Tests for send_fcm_call_data."""

    @patch("zproject.nodl.services.call_push_service._ensure_firebase_initialized", return_value=False)
    def test_firebase_not_initialized_returns_false(self, mock_init: MagicMock) -> None:
        """Returns False when Firebase is not initialized."""
        from zproject.nodl.services.call_push_service import send_fcm_call_data

        result = send_fcm_call_data("token", "call-id", "room", "Caller", "")
        self.assertFalse(result)

    @patch("zproject.nodl.services.call_push_service._ensure_firebase_initialized", return_value=True)
    @patch("zproject.nodl.services.call_push_service.messaging")
    def test_fcm_send_success(self, mock_messaging: MagicMock, mock_init: MagicMock) -> None:
        """Sends FCM data message with correct payload."""
        from zproject.nodl.services.call_push_service import send_fcm_call_data

        mock_messaging.send.return_value = "projects/test/messages/123"

        result = send_fcm_call_data(
            "fcm-token-123", "call-id-abc", "call-room", "Hamlet", "https://avatar"
        )
        self.assertTrue(result)

        mock_messaging.Message.assert_called_once()
        call_kwargs = mock_messaging.Message.call_args
        msg_data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data")
        self.assertEqual(msg_data["type"], "incoming_call")
        self.assertEqual(msg_data["call_id"], "call-id-abc")
        self.assertEqual(msg_data["room_name"], "call-room")
        self.assertEqual(msg_data["caller_name"], "Hamlet")
        self.assertEqual(msg_data["caller_avatar_url"], "https://avatar")
        # Recipient identity keys are always present (empty without kwargs)
        self.assertEqual(msg_data["recipient_user_id"], "")
        self.assertEqual(msg_data["recipient_realm_url"], "")
        self.assertEqual(msg_data["recipient_workspace_id"], "")

    @patch(
        "zproject.nodl.services.call_push_service._ensure_firebase_initialized",
        return_value=True,
    )
    @patch("zproject.nodl.services.call_push_service.messaging")
    def test_fcm_send_includes_recipient_identity(
        self, mock_messaging: MagicMock, mock_init: MagicMock
    ) -> None:
        """Recipient identity kwargs land in the FCM data payload."""
        from zproject.nodl.services.call_push_service import send_fcm_call_data

        mock_messaging.send.return_value = "projects/test/messages/123"

        send_fcm_call_data(
            "fcm-token-123", "call-id-abc", "call-room", "Hamlet", "",
            recipient_user_id="57",
            recipient_realm_url="https://chat.example.com",
            recipient_workspace_id="80e9f594-0000-0000-0000-000000000000",
        )

        call_kwargs = mock_messaging.Message.call_args
        msg_data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data")
        self.assertEqual(msg_data["recipient_user_id"], "57")
        self.assertEqual(msg_data["recipient_realm_url"], "https://chat.example.com")
        self.assertEqual(
            msg_data["recipient_workspace_id"], "80e9f594-0000-0000-0000-000000000000"
        )

    @patch("zproject.nodl.services.call_push_service._ensure_firebase_initialized", return_value=True)
    @patch("zproject.nodl.services.call_push_service.messaging")
    def test_fcm_send_exception_returns_false(
        self, mock_messaging: MagicMock, mock_init: MagicMock
    ) -> None:
        """FCM exception is caught and returns False."""
        from zproject.nodl.services.call_push_service import send_fcm_call_data

        mock_messaging.send.side_effect = Exception("FCM error")

        result = send_fcm_call_data("token", "call-id", "room", "Caller", "")
        self.assertFalse(result)


# ===== Integration: initiate_call triggers push dispatch =====


MOCK_LIVEKIT_ENV = {
    "LIVEKIT_URL": "wss://test.livekit.cloud",
    "LIVEKIT_API_KEY": "test-api-key",
    "LIVEKIT_API_SECRET": "test-api-secret-that-is-long-enough-for-hs256-algorithm",
}


class InitiateCallPushIntegrationTest(ZulipTestCase):
    """Test that initiate_call triggers fire-and-forget push dispatch."""

    def setUp(self) -> None:
        super().setUp()
        self.caller = self.example_user("hamlet")
        self.callee = self.example_user("othello")

    def _auth_headers(self, user: UserProfile | None = None) -> dict[str, str]:
        u = user or self.caller
        cred = base64.b64encode(f"{u.delivery_email}:{u.api_key}".encode()).decode()
        return {"HTTP_AUTHORIZATION": f"Basic {cred}"}

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
    @patch("zproject.nodl.views.calls.create_room_sync")
    @patch("zproject.nodl.views.calls.dispatch_call_push_async")
    def test_initiate_triggers_push_dispatch(
        self, mock_dispatch: MagicMock, mock_create_room: MagicMock
    ) -> None:
        """initiate_call calls dispatch_call_push_async with correct args."""
        mock_create_room.return_value = {"name": "room", "sid": "sid"}

        result = self.client_post(
            "/nodl/calls/initiate",
            json.dumps({"callee_id": self.callee.id}),
            content_type="application/json",
            **self._auth_headers(),
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["result"], "success")

        # Verify dispatch was called
        mock_dispatch.assert_called_once()
        call_args = mock_dispatch.call_args
        self.assertEqual(call_args.kwargs["callee_id"], self.callee.id)
        self.assertEqual(call_args.kwargs["room_name"], result.json()["room_name"])
        self.assertEqual(call_args.kwargs["caller_name"], self.caller.full_name)

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
    @patch("zproject.nodl.views.calls.create_room_sync")
    @patch("zproject.nodl.views.calls.dispatch_call_push_async")
    def test_initiate_returns_before_push_completes(
        self, mock_dispatch: MagicMock, mock_create_room: MagicMock
    ) -> None:
        """Endpoint returns immediately — dispatch is fire-and-forget."""
        mock_create_room.return_value = {"name": "room", "sid": "sid"}

        # Make dispatch block to prove endpoint doesn't wait
        event = threading.Event()
        original_dispatch = mock_dispatch.side_effect

        def slow_dispatch(**kwargs: object) -> None:
            event.wait(timeout=5)

        mock_dispatch.side_effect = slow_dispatch

        result = self.client_post(
            "/nodl/calls/initiate",
            json.dumps({"callee_id": self.callee.id}),
            content_type="application/json",
            **self._auth_headers(),
        )

        # Endpoint returned 200 even though dispatch hasn't completed
        self.assertEqual(result.status_code, 200)
        event.set()  # Unblock
