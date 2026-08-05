"""Unit tests for record_realm_user_mapping — the single mapping writer."""

import uuid

from django.test import TestCase

from nodl.extensions.mapping import record_realm_user_mapping
from nodl.extensions.models import NodlRealmExtension, NodlRealmUserExtension, SyncStatus
from zerver.actions.create_realm import do_create_realm
from zerver.actions.create_user import do_create_user
from zerver.models import Realm, UserProfile


class RecordRealmUserMappingTest(TestCase):
    def _make_realm(self, name: str = "Mapping Test") -> Realm:
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
        return realm

    def _make_user(self, realm: Realm, email: str) -> UserProfile:
        return do_create_user(
            email=email,
            password=None,
            realm=realm,
            full_name="Mapping User",
            acting_user=None,
        )

    def test_creates_mapping(self) -> None:
        realm = self._make_realm()
        user = self._make_user(realm, "map-create@example.com")
        supabase_id = uuid.uuid4()

        mapping = record_realm_user_mapping(realm, user, supabase_id)

        assert mapping is not None
        self.assertEqual(mapping.zulip_user_id, user.id)
        self.assertEqual(mapping.zulip_realm_id, realm.id)
        self.assertEqual(mapping.supabase_user_id, supabase_id)
        self.assertIsNotNone(mapping.last_synced_at)

    def test_idempotent_rerun_keeps_single_row(self) -> None:
        realm = self._make_realm()
        user = self._make_user(realm, "map-idem@example.com")
        supabase_id = uuid.uuid4()

        first = record_realm_user_mapping(realm, user, supabase_id)
        second = record_realm_user_mapping(realm, user, str(supabase_id))

        assert first is not None and second is not None
        self.assertEqual(first.id, second.id)
        self.assertEqual(NodlRealmUserExtension.objects.filter(zulip_realm=realm).count(), 1)

    def test_repoints_stale_mapping_to_new_profile(self) -> None:
        """Self-heal: same (realm, supabase user), recreated Zulip profile."""
        realm = self._make_realm()
        old_user = self._make_user(realm, "map-old@example.com")
        new_user = self._make_user(realm, "map-new@example.com")
        supabase_id = uuid.uuid4()

        record_realm_user_mapping(realm, old_user, supabase_id)
        mapping = record_realm_user_mapping(realm, new_user, supabase_id)

        assert mapping is not None
        self.assertEqual(mapping.zulip_user_id, new_user.id)
        self.assertEqual(NodlRealmUserExtension.objects.filter(zulip_realm=realm).count(), 1)

    def test_never_rehomes_profile_claimed_by_other_supabase_user(self) -> None:
        """A profile mapped to supabase user A is never remapped to user B."""
        realm = self._make_realm()
        user = self._make_user(realm, "map-claimed@example.com")
        supabase_a = uuid.uuid4()
        supabase_b = uuid.uuid4()

        original = record_realm_user_mapping(realm, user, supabase_a)
        result = record_realm_user_mapping(realm, user, supabase_b)

        self.assertIsNone(result)
        assert original is not None
        original.refresh_from_db()
        self.assertEqual(original.supabase_user_id, supabase_a)
        self.assertEqual(NodlRealmUserExtension.objects.filter(zulip_user=user).count(), 1)

    def test_skips_profile_from_wrong_realm(self) -> None:
        realm_a = self._make_realm("Realm A")
        realm_b = self._make_realm("Realm B")
        user_in_b = self._make_user(realm_b, "map-wrong-realm@example.com")

        result = record_realm_user_mapping(realm_a, user_in_b, uuid.uuid4())

        self.assertIsNone(result)
        self.assertEqual(NodlRealmUserExtension.objects.count(), 0)

    def test_skips_unusable_supabase_id(self) -> None:
        realm = self._make_realm()
        user = self._make_user(realm, "map-bad-id@example.com")

        self.assertIsNone(record_realm_user_mapping(realm, user, None))
        self.assertIsNone(record_realm_user_mapping(realm, user, "not-a-uuid"))
        self.assertEqual(NodlRealmUserExtension.objects.count(), 0)

    def test_same_human_maps_once_per_realm(self) -> None:
        """One supabase user gets an independent mapping row in each realm."""
        realm_a = self._make_realm("Realm A")
        realm_b = self._make_realm("Realm B")
        user_a = self._make_user(realm_a, "human@example.com")
        user_b = self._make_user(realm_b, "human@example.com")
        supabase_id = uuid.uuid4()

        mapping_a = record_realm_user_mapping(realm_a, user_a, supabase_id)
        mapping_b = record_realm_user_mapping(realm_b, user_b, supabase_id)

        assert mapping_a is not None and mapping_b is not None
        self.assertNotEqual(mapping_a.id, mapping_b.id)
        self.assertEqual(
            NodlRealmUserExtension.objects.filter(supabase_user_id=supabase_id).count(),
            2,
        )
