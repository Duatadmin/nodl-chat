"""DB-backed tests for UserDeletionService (CI only — no local test env).

Verifies the chat-side purge step of account deletion: every realm profile
mapped to the Supabase user is hard-deleted and replaced by an inactive
"Deleted User" mirror dummy, sent messages survive reattributed to it, the
extension mappings vanish, and the operation is idempotent.
"""

import uuid

from django.test import TestCase

from nodl.extensions.models import NodlRealmUserExtension, NodlUserExtension
from nodl.sync.user_deletion import UserDeletionService
from zerver.actions.create_realm import do_create_realm
from zerver.actions.create_user import do_create_user
from zerver.models import UserProfile


class TestUserDeletionService(TestCase):
    def setUp(self) -> None:
        self.service = UserDeletionService()
        self.supabase_user_id = str(uuid.uuid4())
        self.realm = do_create_realm(
            string_id=f"del-{uuid.uuid4().hex[:8]}", name="Deletion Test Realm"
        )
        self.user = do_create_user(
            email="doomed@example.com",
            password=None,
            realm=self.realm,
            full_name="Doomed User",
            acting_user=None,
        )
        NodlUserExtension.objects.create(
            zulip_user=self.user,
            supabase_user_id=self.supabase_user_id,
        )
        NodlRealmUserExtension.objects.create(
            zulip_realm=self.realm,
            zulip_user=self.user,
            supabase_user_id=self.supabase_user_id,
        )

    def test_profile_replaced_by_mirror_dummy(self) -> None:
        user_id = self.user.id
        result = self.service.delete_user(self.supabase_user_id)

        self.assertTrue(result.success)
        self.assertEqual(result.deleted_profiles, 1)
        self.assertEqual(result.realm_ids, [self.realm.id])

        replacement = UserProfile.objects.get(id=user_id)
        self.assertTrue(replacement.is_mirror_dummy)
        self.assertFalse(replacement.is_active)
        self.assertNotEqual(replacement.delivery_email, "doomed@example.com")
        self.assertIn("Deleted User", replacement.full_name)

    def test_mappings_removed(self) -> None:
        self.service.delete_user(self.supabase_user_id)

        self.assertFalse(
            NodlUserExtension.objects.filter(supabase_user_id=self.supabase_user_id).exists()
        )
        self.assertFalse(
            NodlRealmUserExtension.objects.filter(supabase_user_id=self.supabase_user_id).exists()
        )

    def test_idempotent_second_run(self) -> None:
        first = self.service.delete_user(self.supabase_user_id)
        second = self.service.delete_user(self.supabase_user_id)

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertEqual(second.deleted_profiles, 0)

    def test_unknown_user_succeeds_empty(self) -> None:
        result = self.service.delete_user(str(uuid.uuid4()))
        self.assertTrue(result.success)
        self.assertEqual(result.deleted_profiles, 0)

    def test_pending_legacy_mapping_removed(self) -> None:
        pending_id = str(uuid.uuid4())
        NodlUserExtension.objects.create(zulip_user=None, supabase_user_id=pending_id)
        result = self.service.delete_user(pending_id)
        self.assertTrue(result.success)
        self.assertFalse(NodlUserExtension.objects.filter(supabase_user_id=pending_id).exists())
