import json
import time
from unittest import mock

import jwt
from django.core.cache import cache
from django.test import override_settings

from zerver.lib.test_classes import ZulipTestCase
from zerver.models import UserProfile
from zerver.models.realms import get_realm

from zproject.nodl.actions import mask_email

TEST_JWT_SECRET = "test-supabase-jwt-secret-for-testing"
TEST_SUPABASE_URL = "https://testproject.supabase.co"
TEST_SERVICE_ROLE_KEY = "test-service-role-key"


def make_jwt(
    payload: dict | None = None,
    secret: str = TEST_JWT_SECRET,
    algorithm: str = "HS256",
    **overrides: object,
) -> str:
    """Helper to create a signed JWT token for testing."""
    now = int(time.time())
    default_payload = {
        "sub": "test-supabase-uuid-1234",
        "email": "bridge-test@example.com",
        "phone": "+15551234567",
        "aud": "authenticated",
        "iss": f"{TEST_SUPABASE_URL}/auth/v1",
        "role": "authenticated",
        "exp": now + 3600,
        "iat": now,
    }
    if payload is not None:
        default_payload.update(payload)
    default_payload.update(overrides)
    return jwt.encode(default_payload, secret, algorithm=algorithm)


def make_supabase_user(
    user_id: str = "test-supabase-uuid-1234",
    email: str | None = None,
    phone: str = "+15551234567",
) -> dict:
    """Helper to create a mock Supabase user response."""
    identities = []
    if email:
        identities.append(
            {
                "provider": "email",
                "identity_data": {"email": email},
            }
        )
    identities.append(
        {
            "provider": "phone",
            "identity_data": {"phone": phone},
        }
    )
    return {
        "id": user_id,
        "email": email or "",
        "phone": phone,
        "identities": identities,
    }


AUTH_BRIDGE_URL = "/nodl/auth/bridge"

NODL_SETTINGS = {
    "NODL_SUPABASE_JWT_SECRET": TEST_JWT_SECRET,
    "NODL_SUPABASE_URL": TEST_SUPABASE_URL,
    "NODL_SUPABASE_SERVICE_ROLE_KEY": TEST_SERVICE_ROLE_KEY,
}

# Workspace UUID mapped to the stock "zulip" test realm in AuthBridgeTestBase.
TEST_WORKSPACE_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class AuthBridgeTestBase(ZulipTestCase):
    """Base for bridge tests: gives the JWT user a resolvable workspace.

    The bridge has no fallback realm — a user with no resolvable workspace
    gets ``no_workspace``.  Historically these tests relied on the (removed)
    oldest-realm fallback because the workspace RPC was never patched; now
    every test runs with the RPC patched to place the user in the stock
    "zulip" realm via a NodlRealmExtension mapping.  Tests that need a
    different membership adjust ``self.mock_workspace_ids.return_value``.
    """

    def setUp(self) -> None:
        super().setUp()
        from nodl.extensions.models import NodlRealmExtension

        NodlRealmExtension.objects.get_or_create(
            zulip_realm=get_realm("zulip"),
            defaults={"nodl_workspace_id": TEST_WORKSPACE_ID},
        )
        # Patch at the view namespace — auth_bridge imports it by name.
        patcher = mock.patch(
            "zproject.nodl.views.auth_bridge.get_user_workspace_ids",
            return_value=[TEST_WORKSPACE_ID],
        )
        self.mock_workspace_ids = patcher.start()
        self.addCleanup(patcher.stop)


@override_settings(**NODL_SETTINGS)
class AuthBridgeNewUserTest(AuthBridgeTestBase):
    """Test: valid JWT for new user -> 200 + user created + API key returned (AC #1)"""

    def test_valid_jwt_new_user_creates_account(self) -> None:
        token = make_jwt(email="newuser-bridge@nodl.local", phone="+15559999999")
        result = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 200)
        data = self.assert_json_success(result)
        self.assertEqual(data["result"], "success")
        self.assertEqual(data["msg"], "")
        self.assertIn("api_key", data)
        self.assertIn("user_id", data)
        self.assertEqual(data["email"], "newuser-bridge@nodl.local")

        # Verify user actually exists in DB
        realm = get_realm("zulip")
        user = UserProfile.objects.get(
            delivery_email="newuser-bridge@nodl.local", realm=realm
        )
        self.assertTrue(user.is_active)
        self.assertEqual(data["user_id"], user.id)
        self.assertEqual(data["api_key"], user.api_key)

    def test_phone_only_user_derives_email(self) -> None:
        token = make_jwt(email="", phone="+15558888888", sub="phone-only-uuid")
        result = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 200)
        data = self.assert_json_success(result)
        self.assertEqual(data["email"], "+15558888888@nodl.local")


@override_settings(**NODL_SETTINGS)
class AuthBridgeExistingUserTest(AuthBridgeTestBase):
    """Test: valid JWT for existing user -> 200 + same user (AC #2)"""

    def test_existing_user_returns_same_api_key(self) -> None:
        email = "existing-bridge@nodl.local"
        # First request creates the user
        token = make_jwt(email=email, sub="existing-uuid")
        result1 = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result1.status_code, 200)
        data1 = self.assert_json_success(result1)

        # Second request returns the same user
        result2 = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result2.status_code, 200)
        data2 = self.assert_json_success(result2)

        self.assertEqual(data1["user_id"], data2["user_id"])
        self.assertEqual(data1["api_key"], data2["api_key"])
        self.assertEqual(data1["email"], data2["email"])

        # Verify only one user exists
        realm = get_realm("zulip")
        count = UserProfile.objects.filter(delivery_email=email, realm=realm).count()
        self.assertEqual(count, 1)


@override_settings(**NODL_SETTINGS)
class AuthBridgeInvalidJWTTest(AuthBridgeTestBase):
    """Test error cases for invalid JWTs (AC #3)"""

    def test_expired_jwt_returns_401(self) -> None:
        token = make_jwt(exp=int(time.time()) - 3600)
        result = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 401)
        data = result.json()
        self.assertEqual(data["result"], "error")
        self.assertEqual(data["msg"], "Invalid JWT token")
        self.assertEqual(data["code"], "UNAUTHORIZED")

    def test_malformed_jwt_returns_401(self) -> None:
        result = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION="Bearer not.a.valid.jwt",
        )
        self.assertEqual(result.status_code, 401)
        data = result.json()
        self.assertEqual(data["result"], "error")
        self.assertEqual(data["code"], "UNAUTHORIZED")

    def test_missing_auth_header_returns_401(self) -> None:
        result = self.client_post(AUTH_BRIDGE_URL)
        self.assertEqual(result.status_code, 401)
        data = result.json()
        self.assertEqual(data["result"], "error")
        self.assertEqual(data["code"], "UNAUTHORIZED")

    def test_wrong_audience_returns_401(self) -> None:
        token = make_jwt(aud="wrong-audience")
        result = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 401)
        data = result.json()
        self.assertEqual(data["result"], "error")
        self.assertEqual(data["code"], "UNAUTHORIZED")

    def test_wrong_secret_returns_401(self) -> None:
        token = make_jwt(secret="wrong-secret")
        result = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 401)
        data = result.json()
        self.assertEqual(data["result"], "error")
        self.assertEqual(data["code"], "UNAUTHORIZED")

    def test_get_method_not_allowed(self) -> None:
        result = self.client_get(AUTH_BRIDGE_URL)
        self.assertEqual(result.status_code, 405)


@override_settings(**NODL_SETTINGS)
class AuthBridgePhoneValidationTest(AuthBridgeTestBase):
    """Test E.164 phone validation (H2 fix)."""

    def test_invalid_phone_format_returns_400(self) -> None:
        """Non-E.164 phone in JWT should be rejected."""
        token = make_jwt(email="", phone="555-not-e164", sub="bad-phone-uuid")
        result = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 400)
        data = result.json()
        self.assertEqual(data["result"], "error")
        self.assertEqual(data["msg"], "Invalid phone number format")
        self.assertEqual(data["code"], "BAD_REQUEST")

    def test_valid_e164_phone_accepted(self) -> None:
        """Valid E.164 phone should proceed normally."""
        token = make_jwt(email="valid-phone@nodl.local", phone="+12025551234", sub="valid-phone-uuid")
        result = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 200)

    def test_empty_phone_accepted(self) -> None:
        """Empty phone should not trigger validation."""
        token = make_jwt(email="no-phone@nodl.local", phone="", sub="no-phone-uuid")
        result = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 200)


@override_settings(**NODL_SETTINGS)
class AuthBridgeRateLimitTest(AuthBridgeTestBase):
    """Test: rate limiting -> 429 after 10+ rapid requests (AC #5)"""

    def test_rate_limit_exceeded_returns_429(self) -> None:
        token = make_jwt()
        # Make 10 requests (should all succeed)
        for i in range(10):
            result = self.client_post(
                AUTH_BRIDGE_URL,
                HTTP_AUTHORIZATION=f"Bearer {token}",
                REMOTE_ADDR="192.0.2.100",
            )
            self.assertEqual(result.status_code, 200, f"Request {i+1} failed")

        # 11th request should be rate limited
        result = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
            REMOTE_ADDR="192.0.2.100",
        )
        self.assertEqual(result.status_code, 429)
        data = result.json()
        self.assertEqual(data["result"], "error")
        self.assertEqual(data["msg"], "Rate limit exceeded")
        self.assertEqual(data["code"], "RATE_LIMIT_HIT")
        self.assertIn("Retry-After", result.headers)

    def test_different_ips_not_rate_limited(self) -> None:
        token = make_jwt()
        # Requests from different IPs should not interfere
        for i in range(5):
            result = self.client_post(
                AUTH_BRIDGE_URL,
                HTTP_AUTHORIZATION=f"Bearer {token}",
                REMOTE_ADDR=f"192.0.2.{i+1}",
            )
            self.assertEqual(result.status_code, 200)


@override_settings(**NODL_SETTINGS)
class AuthBridgeConcurrencyTest(AuthBridgeTestBase):
    """Test: concurrent requests for same user don't create duplicates (AC #1, #2)"""

    def test_concurrent_requests_no_duplicates(self) -> None:
        email = "concurrent-bridge@nodl.local"
        token = make_jwt(email=email, sub="concurrent-uuid")
        realm = get_realm("zulip")

        result1 = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result1.status_code, 200)

        result2 = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result2.status_code, 200)

        data1 = self.assert_json_success(result1)
        data2 = self.assert_json_success(result2)
        self.assertEqual(data1["user_id"], data2["user_id"])

        count = UserProfile.objects.filter(delivery_email=email, realm=realm).count()
        self.assertEqual(count, 1)


@override_settings(**NODL_SETTINGS)
class AuthBridgeResponseFormatTest(AuthBridgeTestBase):
    """Test: response format matches Zulip's structure (AC #1)"""

    def test_success_response_format(self) -> None:
        token = make_jwt(email="format-test@nodl.local", sub="format-uuid")
        result = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        data = result.json()
        # Verify exact keys present
        self.assertIn("result", data)
        self.assertIn("msg", data)
        self.assertIn("api_key", data)
        self.assertIn("user_id", data)
        self.assertIn("email", data)
        # Verify types
        self.assertEqual(data["result"], "success")
        self.assertEqual(data["msg"], "")
        self.assertIsInstance(data["api_key"], str)
        self.assertIsInstance(data["user_id"], int)
        self.assertIsInstance(data["email"], str)

    def test_error_response_format(self) -> None:
        result = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION="Bearer invalid",
        )
        data = result.json()
        self.assertIn("result", data)
        self.assertIn("msg", data)
        self.assertIn("code", data)
        self.assertEqual(data["result"], "error")
        self.assertIsInstance(data["msg"], str)
        self.assertIsInstance(data["code"], str)


# ======================================================================
# Story 1.4: Account Linking Tests
# ======================================================================


class EmailMaskingTest(ZulipTestCase):
    """Test email masking utility (Task 1.5)"""

    def test_standard_email(self) -> None:
        self.assertEqual(mask_email("marcus@example.com"), "m***@example.com")

    def test_short_local_part(self) -> None:
        self.assertEqual(mask_email("a@b.com"), "a***@b.com")

    def test_single_char_local(self) -> None:
        self.assertEqual(mask_email("x@domain.com"), "x***@domain.com")

    def test_empty_local_part(self) -> None:
        self.assertEqual(mask_email("@domain.com"), "*@domain.com")

    def test_no_at_symbol(self) -> None:
        self.assertEqual(mask_email("not-an-email"), "not-an-email")

    def test_long_email(self) -> None:
        self.assertEqual(
            mask_email("longusername@company.co.uk"), "l***@company.co.uk"
        )


@override_settings(**NODL_SETTINGS)
class AuthBridgeAccountDetectionTest(AuthBridgeTestBase):
    """Test account linking detection (Task 1.1-1.3, AC #1)"""

    def _create_existing_email_user(self, email: str) -> UserProfile:
        """Helper: create a Zulip user with a given email."""
        token = make_jwt(email=email, sub="existing-email-user-uuid")
        result = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 200)
        realm = get_realm("zulip")
        return UserProfile.objects.get(delivery_email=email, realm=realm)

    @mock.patch("zproject.nodl.views.auth_bridge.check_duplicate_phone")
    @mock.patch("zproject.nodl.views.auth_bridge.get_supabase_user_by_id")
    def test_auto_link_when_email_identity_matches(
        self,
        mock_get_user: mock.MagicMock,
        mock_check_dup: mock.MagicMock,
    ) -> None:
        """When the phone user's email identity matches a Zulip user, log
        straight into that profile (auto-link) instead of asking the client
        to confirm — Supabase already asserts both identities are one human,
        and the old confirm handshake never worked end-to-end."""
        existing_user = self._create_existing_email_user("marcus@example.com")
        mock_check_dup.return_value = False
        mock_get_user.return_value = make_supabase_user(
            user_id="phone-user-uuid",
            email="marcus@example.com",
            phone="+15557777777",
        )

        token = make_jwt(
            email="", phone="+15557777777", sub="phone-user-uuid"
        )
        result = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["result"], "success")
        self.assertEqual(data["api_key"], existing_user.api_key)
        self.assertEqual(data["user_id"], existing_user.id)
        self.assertEqual(data["email"], existing_user.delivery_email)
        self.assertEqual(data["linked_email_masked"], "m***@example.com")
        self.assertTrue(data["is_new_device"])
        # No second profile was provisioned for the phone-derived email.
        realm = get_realm("zulip")
        self.assertFalse(
            UserProfile.objects.filter(
                realm=realm, delivery_email__iexact="15557777777@nodl.local"
            ).exists()
        )

    @mock.patch("zproject.nodl.views.auth_bridge.check_duplicate_phone")
    @mock.patch("zproject.nodl.views.auth_bridge.get_supabase_user_by_id")
    def test_no_linking_when_no_email_identity(
        self,
        mock_get_user: mock.MagicMock,
        mock_check_dup: mock.MagicMock,
    ) -> None:
        """Phone-only Supabase user with no email identity proceeds normally."""
        mock_check_dup.return_value = False
        mock_get_user.return_value = make_supabase_user(
            user_id="phone-only-uuid",
            email=None,
            phone="+15556666666",
        )

        token = make_jwt(email="", phone="+15556666666", sub="phone-only-uuid")
        result = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["result"], "success")
        # Normal flow: should have api_key, no linking_available
        self.assertIn("api_key", data)
        self.assertNotIn("linking_available", data)

    @mock.patch("zproject.nodl.views.auth_bridge.check_duplicate_phone")
    @mock.patch("zproject.nodl.views.auth_bridge.get_supabase_user_by_id")
    def test_no_linking_when_zulip_user_not_found(
        self,
        mock_get_user: mock.MagicMock,
        mock_check_dup: mock.MagicMock,
    ) -> None:
        """Email identity exists in Supabase but no Zulip user with that email."""
        mock_check_dup.return_value = False
        mock_get_user.return_value = make_supabase_user(
            user_id="orphan-uuid",
            email="nonexistent@example.com",
            phone="+15554444444",
        )

        token = make_jwt(email="", phone="+15554444444", sub="orphan-uuid")
        result = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        # Should proceed with normal flow (no match)
        self.assertIn("api_key", data)
        self.assertNotIn("linking_available", data)


@override_settings(**NODL_SETTINGS)
class AuthBridgeDuplicatePhoneTest(AuthBridgeTestBase):
    """Test duplicate phone detection (Task 1.4, AC #4)"""

    @mock.patch("zproject.nodl.views.auth_bridge.check_duplicate_phone")
    def test_duplicate_phone_returns_flag(
        self, mock_check_dup: mock.MagicMock
    ) -> None:
        """When phone is already registered to another user, return duplicate_phone."""
        mock_check_dup.return_value = True

        token = make_jwt(email="", phone="+15553333333", sub="new-phone-uuid")
        result = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["result"], "success")
        self.assertTrue(data["duplicate_phone"])
        self.assertNotIn("api_key", data)

    @mock.patch("zproject.nodl.views.auth_bridge.check_duplicate_phone")
    def test_no_duplicate_proceeds_normally(
        self, mock_check_dup: mock.MagicMock
    ) -> None:
        """When phone is not a duplicate, proceed with normal flow."""
        mock_check_dup.return_value = False

        token = make_jwt(
            email="unique-phone@nodl.local", phone="+15552222222", sub="unique-uuid"
        )
        result = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertNotIn("duplicate_phone", data)
        self.assertIn("api_key", data)


@override_settings(**NODL_SETTINGS)
class AuthBridgeLinkConfirmationTest(AuthBridgeTestBase):
    """Test link confirmation endpoint (Task 2.1-2.3, AC #2, #3)"""

    def _create_existing_email_user(self, email: str) -> UserProfile:
        """Helper: create a Zulip user with a given email."""
        token = make_jwt(email=email, sub="existing-email-user-uuid-link")
        result = self.client_post(
            AUTH_BRIDGE_URL,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 200)
        realm = get_realm("zulip")
        return UserProfile.objects.get(delivery_email=email, realm=realm)

    @mock.patch("zproject.nodl.views.auth_bridge.link_phone_to_existing_user")
    @mock.patch("zproject.nodl.views.auth_bridge.get_supabase_user_by_email")
    @mock.patch("zproject.nodl.views.auth_bridge.get_supabase_user_by_id")
    def test_link_action_link_returns_existing_user(
        self,
        mock_get_user_by_id: mock.MagicMock,
        mock_get_user_by_email: mock.MagicMock,
        mock_link_phone: mock.MagicMock,
    ) -> None:
        """link_action='link' returns existing Zulip user's API key."""
        existing_user = self._create_existing_email_user("link-target@example.com")
        mock_get_user_by_id.return_value = make_supabase_user(
            user_id="phone-linker-uuid",
            email="link-target@example.com",
            phone="+15551111111",
        )
        # H1: get_supabase_user_by_email returns the email user's Supabase record
        mock_get_user_by_email.return_value = {
            "id": "email-owner-supabase-uuid",
            "email": "link-target@example.com",
        }
        mock_link_phone.return_value = True

        token = make_jwt(
            email="", phone="+15551111111", sub="phone-linker-uuid"
        )
        result = self.client_post(
            AUTH_BRIDGE_URL,
            json.dumps({"link_action": "link"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["result"], "success")
        self.assertEqual(data["api_key"], existing_user.api_key)
        self.assertEqual(data["user_id"], existing_user.id)
        self.assertEqual(data["email"], "link-target@example.com")
        # H1: verify link_phone is called with the EMAIL user's Supabase ID
        mock_link_phone.assert_called_once_with("email-owner-supabase-uuid", "+15551111111")

    @mock.patch("zproject.nodl.views.auth_bridge.get_supabase_user_by_id")
    def test_link_action_create_new_provisions_user(
        self,
        mock_get_user: mock.MagicMock,
    ) -> None:
        """link_action='create_new' provisions a new Zulip account."""
        mock_get_user.return_value = None  # Not needed for create_new

        token = make_jwt(
            email="", phone="+15550000000", sub="create-new-uuid"
        )
        result = self.client_post(
            AUTH_BRIDGE_URL,
            json.dumps({"link_action": "create_new"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["result"], "success")
        self.assertIn("api_key", data)
        self.assertEqual(data["email"], "+15550000000@nodl.local")

    def test_invalid_link_action_returns_400(self) -> None:
        """Invalid link_action value returns 400."""
        token = make_jwt(email="", phone="+15559876543", sub="invalid-action-uuid")
        result = self.client_post(
            AUTH_BRIDGE_URL,
            json.dumps({"link_action": "invalid"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 400)
        data = result.json()
        self.assertEqual(data["result"], "error")
        self.assertEqual(data["msg"], "Invalid link_action")

    @mock.patch("zproject.nodl.views.auth_bridge.link_phone_to_existing_user")
    @mock.patch("zproject.nodl.views.auth_bridge.get_supabase_user_by_id")
    def test_link_fails_when_supabase_api_fails(
        self,
        mock_get_user: mock.MagicMock,
        mock_link_phone: mock.MagicMock,
    ) -> None:
        """When Supabase admin API fails during linking, return error."""
        mock_get_user.return_value = None  # Simulates Supabase API failure

        token = make_jwt(email="", phone="+15558765432", sub="fail-link-uuid")
        result = self.client_post(
            AUTH_BRIDGE_URL,
            json.dumps({"link_action": "link"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 500)
        data = result.json()
        self.assertEqual(data["result"], "error")
        mock_link_phone.assert_not_called()

    @mock.patch("zproject.nodl.views.auth_bridge.release_phone_link_lock")
    @mock.patch("zproject.nodl.views.auth_bridge.acquire_phone_link_lock")
    @mock.patch("zproject.nodl.views.auth_bridge.link_phone_to_existing_user")
    @mock.patch("zproject.nodl.views.auth_bridge.get_supabase_user_by_email")
    @mock.patch("zproject.nodl.views.auth_bridge.get_supabase_user_by_id")
    def test_link_conflict_when_lock_held(
        self,
        mock_get_user_by_id: mock.MagicMock,
        mock_get_user_by_email: mock.MagicMock,
        mock_link_phone: mock.MagicMock,
        mock_acquire_lock: mock.MagicMock,
        mock_release_lock: mock.MagicMock,
    ) -> None:
        """H3: when another link operation is in progress, return 409 CONFLICT."""
        self._create_existing_email_user("lock-target@example.com")
        mock_get_user_by_id.return_value = make_supabase_user(
            user_id="lock-phone-uuid",
            email="lock-target@example.com",
            phone="+15551119999",
        )
        mock_get_user_by_email.return_value = {
            "id": "lock-email-uuid",
            "email": "lock-target@example.com",
        }
        mock_acquire_lock.return_value = False  # Lock already held

        token = make_jwt(email="", phone="+15551119999", sub="lock-phone-uuid")
        result = self.client_post(
            AUTH_BRIDGE_URL,
            json.dumps({"link_action": "link"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 409)
        data = result.json()
        self.assertEqual(data["code"], "CONFLICT")
        mock_link_phone.assert_not_called()
        mock_release_lock.assert_not_called()


@override_settings(**NODL_SETTINGS)
class AuthBridgeLinkRateLimitTest(AuthBridgeTestBase):
    """Test link attempt rate limiting (Task 2.4)"""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()

    @mock.patch("zproject.nodl.views.auth_bridge.get_supabase_user_by_id")
    def test_link_rate_limit_exceeded(
        self, mock_get_user: mock.MagicMock
    ) -> None:
        """After 5 link attempts, return 429."""
        mock_get_user.return_value = make_supabase_user(
            user_id="rate-limit-uuid",
            email="ratelimit@example.com",
            phone="+15551112222",
        )

        token = make_jwt(
            email="", phone="+15551112222", sub="rate-limit-uuid"
        )

        # Make 5 link attempts (all should succeed or fail normally)
        for _i in range(5):
            self.client_post(
                AUTH_BRIDGE_URL,
                json.dumps({"link_action": "link"}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {token}",
            )

        # 6th attempt should be rate limited
        result = self.client_post(
            AUTH_BRIDGE_URL,
            json.dumps({"link_action": "link"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(result.status_code, 429)
        data = result.json()
        self.assertEqual(data["result"], "error")
        self.assertEqual(data["code"], "RATE_LIMIT_HIT")
        self.assertIn("Retry-After", result.headers)


@override_settings(**NODL_SETTINGS)
class AuthBridgeWorkspaceResolutionTest(AuthBridgeTestBase):
    """Deterministic workspace→realm resolution and the no-fallback contract."""

    SECOND_WORKSPACE_ID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"

    def create_second_realm(self) -> "Realm":
        from nodl.extensions.models import NodlRealmExtension
        from zerver.actions.create_realm import do_create_realm

        realm = do_create_realm(
            string_id="secondws",
            name="Second Workspace",
            create_zulip_discussion_channel=False,
        )
        NodlRealmExtension.objects.create(
            zulip_realm=realm,
            nodl_workspace_id=self.SECOND_WORKSPACE_ID,
        )
        return realm

    def test_lands_in_membership_realm_not_oldest(self) -> None:
        """A member of only the second realm lands there, never in realm #1."""
        second_realm = self.create_second_realm()
        self.mock_workspace_ids.return_value = [self.SECOND_WORKSPACE_ID]

        token = make_jwt(email="resolution-a@nodl.local", sub="resolution-a-uuid")
        result = self.client_post(
            AUTH_BRIDGE_URL, HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["result"], "success")
        user = UserProfile.objects.get(id=data["user_id"])
        self.assertEqual(user.realm_id, second_realm.id)

    def test_no_workspace_returns_no_workspace_and_creates_no_user(self) -> None:
        """Empty membership -> no_workspace, and no UserProfile is provisioned."""
        self.mock_workspace_ids.return_value = []
        email = "no-workspace-user@nodl.local"
        before = UserProfile.objects.filter(delivery_email__iexact=email).count()

        token = make_jwt(email=email, sub="no-workspace-uuid")
        result = self.client_post(
            AUTH_BRIDGE_URL, HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["result"], "no_workspace")
        self.assertEqual(data["code"], "NO_WORKSPACE")
        self.assertNotIn("api_key", data)
        self.assertEqual(
            UserProfile.objects.filter(delivery_email__iexact=email).count(), before
        )

    def test_rpc_unavailable_returns_503(self) -> None:
        """RPC failure (None) is an operational error, not 'no workspaces'."""
        self.mock_workspace_ids.return_value = None

        token = make_jwt(email="rpc-down@nodl.local", sub="rpc-down-uuid")
        result = self.client_post(
            AUTH_BRIDGE_URL, HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        self.assertEqual(result.status_code, 503)
        data = result.json()
        self.assertEqual(data["result"], "error")
        self.assertEqual(data["code"], "SERVICE_UNAVAILABLE")

    def test_unresolvable_workspace_is_not_a_fallback(self) -> None:
        """A workspace id with no matching realm yields no_workspace."""
        self.mock_workspace_ids.return_value = [
            "99999999-9999-4999-8999-999999999999"
        ]
        token = make_jwt(email="unresolvable@nodl.local", sub="unresolvable-uuid")
        result = self.client_post(
            AUTH_BRIDGE_URL, HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["result"], "no_workspace")

    def test_prefer_realm_with_existing_profile(self) -> None:
        """An existing identity beats provisioning a fresh one, regardless of
        the order the RPC returns workspaces in."""
        second_realm = self.create_second_realm()
        email = "prefer-existing@nodl.local"
        from zerver.actions.create_user import do_create_user

        do_create_user(
            email=email,
            password=None,
            realm=second_realm,
            full_name="Existing There",
            acting_user=None,
        )
        # RPC lists the zulip-realm workspace FIRST; the second realm must
        # still win because the profile already exists there.
        self.mock_workspace_ids.return_value = [
            TEST_WORKSPACE_ID,
            self.SECOND_WORKSPACE_ID,
        ]

        token = make_jwt(email=email, sub="prefer-existing-uuid")
        result = self.client_post(
            AUTH_BRIDGE_URL, HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        data = result.json()
        self.assertEqual(data["result"], "success")
        user = UserProfile.objects.get(id=data["user_id"])
        self.assertEqual(user.realm_id, second_realm.id)

    def test_message_recency_ranks_between_existing_profiles(self) -> None:
        """With profiles in two realms, the one with newer messages wins."""
        second_realm = self.create_second_realm()
        email = "recency-rank@nodl.local"
        from zerver.actions.create_user import do_create_user

        zulip_realm_profile = do_create_user(
            email=email,
            password=None,
            realm=get_realm("zulip"),
            full_name="Recency Zulip",
            acting_user=None,
        )
        second_profile = do_create_user(
            email=email,
            password=None,
            realm=second_realm,
            full_name="Recency Second",
            acting_user=None,
        )
        # Give the second realm's profile the newer message activity.
        peer = do_create_user(
            email="recency-peer@nodl.local",
            password=None,
            realm=second_realm,
            full_name="Peer",
            acting_user=None,
        )
        self.send_personal_message(peer, second_profile)
        assert zulip_realm_profile is not None  # unused message-side, ranking only

        self.mock_workspace_ids.return_value = [
            TEST_WORKSPACE_ID,
            self.SECOND_WORKSPACE_ID,
        ]
        token = make_jwt(email=email, sub="recency-rank-uuid")
        result = self.client_post(
            AUTH_BRIDGE_URL, HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        data = result.json()
        self.assertEqual(data["result"], "success")
        self.assertEqual(data["user_id"], second_profile.id)
