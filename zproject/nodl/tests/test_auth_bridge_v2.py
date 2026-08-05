"""Tests for bridge v2 (workspace-list bridge) and GET /nodl/auth/workspaces.

Unlike the v1 tests, the steady-state v2 tests do NOT patch the Supabase
workspace RPC — the whole point of v2 is answering from the local
NodlRealmUserExtension join.  The RPC mock exists only to assert it is NOT
called (and to drive the explicit cold-start reconcile tests).
"""

import base64
import uuid
from unittest import mock

from django.http import HttpResponse
from django.test import override_settings

from nodl.extensions.mapping import record_realm_user_mapping
from nodl.extensions.models import NodlRealmExtension, NodlRealmUserExtension, SyncStatus
from zerver.actions.create_realm import do_create_realm
from zerver.actions.create_user import do_create_user
from zerver.actions.realm_settings import do_deactivate_realm
from zerver.lib.test_classes import ZulipTestCase
from zerver.models import Realm, UserProfile
from zerver.models.users import get_user_profile_by_api_key
from zproject.nodl.models import NodlRegistrationPin
from zproject.nodl.tests.test_auth_bridge import NODL_SETTINGS, make_jwt

AUTH_BRIDGE_V2_URL = "/nodl/auth/bridge/v2"
AUTH_WORKSPACES_URL = "/nodl/auth/workspaces"


@override_settings(**NODL_SETTINGS)
class AuthBridgeV2TestBase(ZulipTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Admin-API touchpoints are always mocked (no network in tests).
        patcher = mock.patch(
            "zproject.nodl.views.auth_bridge_v2.get_supabase_user_by_id",
            return_value=None,
        )
        self.mock_supabase_user = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch(
            "zproject.nodl.views.auth_bridge_v2.check_duplicate_phone",
            return_value=False,
        )
        self.mock_duplicate = patcher.start()
        self.addCleanup(patcher.stop)
        # The RPC must stay silent on the steady-state path; cold-start tests
        # override return_value explicitly.
        patcher = mock.patch(
            "zproject.nodl.views.auth_bridge_v2.get_user_workspace_ids",
            return_value=[],
        )
        self.mock_rpc = patcher.start()
        self.addCleanup(patcher.stop)

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

    def _make_mapped_profile(self, realm: Realm, email: str, supabase_id: uuid.UUID) -> UserProfile:
        profile = do_create_user(
            email=email,
            password=None,
            realm=realm,
            full_name="V2 Test User",
            acting_user=None,
        )
        assert record_realm_user_mapping(realm, profile, supabase_id) is not None
        return profile

    def _post(self, token: str) -> "HttpResponse":
        return self.client_post(AUTH_BRIDGE_V2_URL, HTTP_AUTHORIZATION=f"Bearer {token}")


class AuthBridgeV2WorkspaceListTest(AuthBridgeV2TestBase):
    def test_lists_all_mapped_workspaces_with_per_realm_credentials(self) -> None:
        supabase_id = uuid.uuid4()
        email = "multi@example.com"
        realm_a, ws_a = self._make_workspace_realm("Workspace A")
        realm_b, ws_b = self._make_workspace_realm("Workspace B")
        profile_a = self._make_mapped_profile(realm_a, email, supabase_id)
        profile_b = self._make_mapped_profile(realm_b, email, supabase_id)

        token = make_jwt(sub=str(supabase_id), email=email)
        result = self._post(token)

        data = self.assert_json_success(result)
        self.assertEqual(data["result"], "success")
        self.assertEqual(data["supabase_user_id"], str(supabase_id))
        self.assertEqual(len(data["workspaces"]), 2)

        by_workspace = {entry["workspace_id"]: entry for entry in data["workspaces"]}
        self.assertEqual(set(by_workspace), {str(ws_a), str(ws_b)})
        for profile, ws in ((profile_a, ws_a), (profile_b, ws_b)):
            entry = by_workspace[str(ws)]
            self.assertEqual(entry["user_id"], profile.id)
            self.assertEqual(entry["api_key"], profile.api_key)
            self.assertEqual(entry["email"], email)
            # The credential must bind to its own realm — realm resolution
            # from api keys is what makes the shared bare host unambiguous.
            self.assertEqual(
                get_user_profile_by_api_key(entry["api_key"]).realm_id,
                profile.realm_id,
            )
        self.assertEqual(data["default_workspace_id"], data["workspaces"][0]["workspace_id"])
        self.mock_rpc.assert_not_called()

    def test_message_recency_drives_rank(self) -> None:
        supabase_id = uuid.uuid4()
        email = "ranked@example.com"
        realm_a, ws_a = self._make_workspace_realm("Older Join Active")
        realm_b, ws_b = self._make_workspace_realm("Recent Join Quiet")
        profile_a = self._make_mapped_profile(realm_a, email, supabase_id)
        self._make_mapped_profile(realm_b, email, supabase_id)

        # profile_a joined earlier but has message activity — activity wins.
        self.subscribe(profile_a, "ranking-stream")
        self.send_stream_message(profile_a, "ranking-stream", "recent activity")

        token = make_jwt(sub=str(supabase_id), email=email)
        data = self.assert_json_success(self._post(token))

        self.assertEqual(data["workspaces"][0]["workspace_id"], str(ws_a))
        self.assertEqual(data["default_workspace_id"], str(ws_a))
        self.assertEqual([entry["rank"] for entry in data["workspaces"]], [0, 1])
        self.assertGreater(data["workspaces"][0]["last_message_id"], 0)

    def test_deactivated_realm_excluded(self) -> None:
        supabase_id = uuid.uuid4()
        email = "deact@example.com"
        realm_a, ws_a = self._make_workspace_realm("Alive")
        realm_b, _ws_b = self._make_workspace_realm("Archived")
        self._make_mapped_profile(realm_a, email, supabase_id)
        self._make_mapped_profile(realm_b, email, supabase_id)
        do_deactivate_realm(
            realm_b,
            acting_user=None,
            deactivation_reason="owner_request",
            email_owners=False,
        )

        token = make_jwt(sub=str(supabase_id), email=email)
        data = self.assert_json_success(self._post(token))

        self.assertEqual(len(data["workspaces"]), 1)
        self.assertEqual(data["workspaces"][0]["workspace_id"], str(ws_a))

    def test_self_heal_maps_existing_profile_without_rpc(self) -> None:
        """A profile matching the JWT email is auto-linked and listed."""
        supabase_id = uuid.uuid4()
        email = "healme@example.com"
        realm, ws = self._make_workspace_realm("Heal Realm")
        profile = do_create_user(
            email=email,
            password=None,
            realm=realm,
            full_name="Unmapped Profile",
            acting_user=None,
        )

        token = make_jwt(sub=str(supabase_id), email=email)
        data = self.assert_json_success(self._post(token))

        self.assertEqual(data["result"], "success")
        self.assertEqual(data["workspaces"][0]["workspace_id"], str(ws))
        self.assertEqual(data["workspaces"][0]["user_id"], profile.id)
        self.assertTrue(data["is_new_device"])
        mapping = NodlRealmUserExtension.objects.get(zulip_user=profile)
        self.assertEqual(mapping.supabase_user_id, supabase_id)
        self.mock_rpc.assert_not_called()

    def test_self_heal_never_steals_claimed_profile(self) -> None:
        """A profile mapped to another Supabase user is not listed or re-homed."""
        other_supabase = uuid.uuid4()
        caller_supabase = uuid.uuid4()
        email = "contested@example.com"
        realm, _ws = self._make_workspace_realm("Contested Realm")
        self._make_mapped_profile(realm, email, other_supabase)

        token = make_jwt(sub=str(caller_supabase), email=email)
        result = self._post(token)

        data = self.assert_json_success(result)
        self.assertEqual(data["result"], "no_workspace")
        mapping = NodlRealmUserExtension.objects.get(zulip_realm=realm)
        self.assertEqual(mapping.supabase_user_id, other_supabase)


class AuthBridgeV2ColdStartTest(AuthBridgeV2TestBase):
    def test_no_workspace_when_rpc_empty_and_no_profile_created(self) -> None:
        before = UserProfile.objects.count()
        token = make_jwt(sub=str(uuid.uuid4()), email="nobody@example.com")

        data = self.assert_json_success(self._post(token))

        self.assertEqual(data["result"], "no_workspace")
        self.assertEqual(data["code"], "NO_WORKSPACE")
        self.assertNotIn("api_key", str(data))
        self.assertEqual(UserProfile.objects.count(), before)
        self.mock_rpc.assert_called_once()

    def test_503_when_rpc_unavailable(self) -> None:
        self.mock_rpc.return_value = None
        token = make_jwt(sub=str(uuid.uuid4()), email="unlucky@example.com")

        result = self._post(token)

        self.assertEqual(result.status_code, 503)

    def test_cold_start_provisions_all_member_realms(self) -> None:
        supabase_id = uuid.uuid4()
        email = "fresh@example.com"
        realm_a, ws_a = self._make_workspace_realm("Cold A")
        realm_b, ws_b = self._make_workspace_realm("Cold B")
        self.mock_rpc.return_value = [str(ws_a), str(ws_b)]

        token = make_jwt(sub=str(supabase_id), email=email)
        data = self.assert_json_success(self._post(token))

        self.assertEqual(data["result"], "success")
        self.assertEqual(len(data["workspaces"]), 2)
        self.assertFalse(data["is_new_device"])
        for realm in (realm_a, realm_b):
            profile = UserProfile.objects.get(delivery_email=email, realm=realm)
            self.assertTrue(
                NodlRealmUserExtension.objects.filter(
                    zulip_user=profile, supabase_user_id=supabase_id
                ).exists()
            )


class AuthBridgeV2SafetyTest(AuthBridgeV2TestBase):
    def test_duplicate_phone_is_a_distinct_result_without_credentials(self) -> None:
        self.mock_duplicate.return_value = True
        token = make_jwt(sub=str(uuid.uuid4()), phone="+15551230000")

        data = self.assert_json_success(self._post(token))

        self.assertEqual(data["result"], "duplicate_phone")
        self.assertEqual(data["code"], "DUPLICATE_PHONE")
        self.assertNotIn("api_key", str(data))
        self.assertNotIn("workspaces", data)

    def test_invalid_jwt_rejected(self) -> None:
        result = self.client_post(AUTH_BRIDGE_V2_URL, HTTP_AUTHORIZATION="Bearer not-a-jwt")
        self.assertEqual(result.status_code, 401)


class AuthBridgeV2PinTest(AuthBridgeV2TestBase):
    def _pin_fixture(self) -> tuple[str, uuid.UUID]:
        supabase_id = uuid.uuid4()
        email = "pinned@example.com"
        realm_a, _ = self._make_workspace_realm("Pin Default")
        realm_b, _ = self._make_workspace_realm("Pin Other")
        profile_a = self._make_mapped_profile(realm_a, email, supabase_id)
        profile_b = self._make_mapped_profile(realm_b, email, supabase_id)
        # Activity puts profile_a on rank 0; only profile_b carries a PIN.
        self.subscribe(profile_a, "pin-stream")
        self.send_stream_message(profile_a, "pin-stream", "activity")
        NodlRegistrationPin.objects.create(user=profile_b, pin_hash="hash")
        return make_jwt(sub=str(supabase_id), email=email), supabase_id

    def test_default_flag_uses_top_ranked_profile_pin(self) -> None:
        token, _ = self._pin_fixture()
        data = self.assert_json_success(self._post(token))
        self.assertFalse(data["has_pin"])

    @override_settings(NODL_PIN_PER_HUMAN=True)
    def test_per_human_flag_any_profile_pin_counts(self) -> None:
        token, _ = self._pin_fixture()
        data = self.assert_json_success(self._post(token))
        self.assertTrue(data["has_pin"])


class AuthWorkspacesEndpointTest(AuthBridgeV2TestBase):
    def _basic_auth(self, profile: UserProfile) -> str:
        credentials = f"{profile.delivery_email}:{profile.api_key}"
        return "Basic " + base64.b64encode(credentials.encode()).decode()

    def test_lists_workspaces_without_credentials(self) -> None:
        supabase_id = uuid.uuid4()
        email = "reader@example.com"
        realm_a, ws_a = self._make_workspace_realm("Read A")
        realm_b, ws_b = self._make_workspace_realm("Read B")
        profile_a = self._make_mapped_profile(realm_a, email, supabase_id)
        self._make_mapped_profile(realm_b, email, supabase_id)

        result = self.client_get(
            AUTH_WORKSPACES_URL, HTTP_AUTHORIZATION=self._basic_auth(profile_a)
        )

        data = self.assert_json_success(result)
        self.assertEqual(len(data["workspaces"]), 2)
        self.assertEqual(
            {entry["workspace_id"] for entry in data["workspaces"]},
            {str(ws_a), str(ws_b)},
        )
        for entry in data["workspaces"]:
            self.assertNotIn("api_key", entry)
            self.assertIn("unread_count", entry)
            self.assertIn("last_message_id", entry)

    def test_unmapped_caller_degrades_to_own_workspace(self) -> None:
        realm, ws = self._make_workspace_realm("Solo Realm")
        profile = do_create_user(
            email="solo@example.com",
            password=None,
            realm=realm,
            full_name="Solo",
            acting_user=None,
        )

        result = self.client_get(AUTH_WORKSPACES_URL, HTTP_AUTHORIZATION=self._basic_auth(profile))

        data = self.assert_json_success(result)
        self.assertEqual(len(data["workspaces"]), 1)
        self.assertEqual(data["workspaces"][0]["workspace_id"], str(ws))
        self.assertNotIn("api_key", data["workspaces"][0])

    def test_requires_authentication(self) -> None:
        result = self.client_get(AUTH_WORKSPACES_URL)
        self.assertEqual(result.status_code, 401)

    def test_read_only_no_provisioning(self) -> None:
        realm, _ws = self._make_workspace_realm("No Side Effects")
        profile = do_create_user(
            email="readonly@example.com",
            password=None,
            realm=realm,
            full_name="Read Only",
            acting_user=None,
        )
        before_profiles = UserProfile.objects.count()
        before_mappings = NodlRealmUserExtension.objects.count()

        self.client_get(AUTH_WORKSPACES_URL, HTTP_AUTHORIZATION=self._basic_auth(profile))

        self.assertEqual(UserProfile.objects.count(), before_profiles)
        self.assertEqual(NodlRealmUserExtension.objects.count(), before_mappings)
