"""Tests for the backfill_realm_user_mappings management command."""

import uuid
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from nodl.extensions.management.commands.backfill_realm_user_mappings import Command
from nodl.extensions.models import (
    NodlRealmExtension,
    NodlRealmUserExtension,
    NodlUserExtension,
    SyncStatus,
)
from zerver.actions.create_realm import do_create_realm
from zerver.actions.create_user import do_create_user
from zerver.models import Realm, UserProfile


class BackfillRealmUserMappingsTest(TestCase):
    def _make_realm(self, name: str) -> Realm:
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
            full_name="Backfill User",
            acting_user=None,
        )

    def _run(self, *args: str) -> str:
        out = StringIO()
        with mock.patch.object(Command, "_supabase_lookup", return_value=None):
            call_command("backfill_realm_user_mappings", *args, stdout=out)
        return out.getvalue()

    def test_one_human_many_realms_reconstructed_via_email_tier(self) -> None:
        """The founder shape: one Supabase identity, one profile per realm.

        Only realm A has a global NodlUserExtension link; realms B and C are
        reconstructed by the same-email tier.
        """
        supabase_id = uuid.uuid4()
        email = "founder@example.com"
        realm_a = self._make_realm("Realm A")
        realm_b = self._make_realm("Realm B")
        realm_c = self._make_realm("Realm C")
        profile_a = self._make_user(realm_a, email)
        profile_b = self._make_user(realm_b, email)
        profile_c = self._make_user(realm_c, email)
        NodlUserExtension.objects.create(
            zulip_user=profile_a,
            supabase_user_id=supabase_id,
            sync_status=SyncStatus.SYNCED,
        )

        self._run("--commit")

        mappings = NodlRealmUserExtension.objects.filter(supabase_user_id=supabase_id)
        self.assertEqual(
            {(m.zulip_realm_id, m.zulip_user_id) for m in mappings},
            {
                (realm_a.id, profile_a.id),
                (realm_b.id, profile_b.id),
                (realm_c.id, profile_c.id),
            },
        )

    def test_dry_run_writes_nothing(self) -> None:
        realm = self._make_realm("Dry Run Realm")
        profile = self._make_user(realm, "dry-run@example.com")
        NodlUserExtension.objects.create(
            zulip_user=profile,
            supabase_user_id=uuid.uuid4(),
            sync_status=SyncStatus.SYNCED,
        )

        output = self._run()

        self.assertEqual(NodlRealmUserExtension.objects.count(), 0)
        self.assertIn("[dry-run]", output)
        self.assertIn("direct=1", output)

    def test_supabase_tier_resolves_unknown_email(self) -> None:
        realm = self._make_realm("Lookup Realm")
        profile = self._make_user(realm, "only-in-supabase@example.com")
        supabase_id = uuid.uuid4()

        out = StringIO()
        with mock.patch.object(Command, "_supabase_lookup", return_value=supabase_id):
            call_command("backfill_realm_user_mappings", "--commit", stdout=out)

        mapping = NodlRealmUserExtension.objects.get(zulip_user=profile)
        self.assertEqual(mapping.supabase_user_id, supabase_id)
        self.assertIn("supabase=1", out.getvalue())

    def test_unresolvable_profile_is_skipped_and_reported(self) -> None:
        realm = self._make_realm("Skip Realm")
        profile = self._make_user(realm, "nobody-knows@example.com")

        output = self._run("--commit")

        self.assertFalse(NodlRealmUserExtension.objects.filter(zulip_user=profile).exists())
        self.assertIn("skipped=1", output)
        self.assertIn("nobody-knows@example.com", output)

    def test_rerun_is_idempotent(self) -> None:
        supabase_id = uuid.uuid4()
        realm = self._make_realm("Idempotent Realm")
        profile = self._make_user(realm, "idem@example.com")
        NodlUserExtension.objects.create(
            zulip_user=profile,
            supabase_user_id=supabase_id,
            sync_status=SyncStatus.SYNCED,
        )

        self._run("--commit")
        first = NodlRealmUserExtension.objects.get(zulip_user=profile)
        output = self._run("--commit")

        self.assertEqual(NodlRealmUserExtension.objects.count(), 1)
        self.assertEqual(NodlRealmUserExtension.objects.get(zulip_user=profile).id, first.id)
        self.assertIn("direct=0", output)

    def test_existing_mapping_left_untouched(self) -> None:
        """A profile that already has a mapping row is never a candidate."""
        supabase_original = uuid.uuid4()
        realm = self._make_realm("Untouched Realm")
        profile = self._make_user(realm, "mapped@example.com")
        NodlRealmUserExtension.objects.create(
            zulip_realm=realm,
            zulip_user=profile,
            supabase_user_id=supabase_original,
        )
        # A conflicting global link exists — backfill must not re-home.
        NodlUserExtension.objects.create(
            zulip_user=profile,
            supabase_user_id=uuid.uuid4(),
            sync_status=SyncStatus.SYNCED,
        )

        self._run("--commit")

        mapping = NodlRealmUserExtension.objects.get(zulip_user=profile)
        self.assertEqual(mapping.supabase_user_id, supabase_original)

    def test_bots_and_inactive_profiles_ignored(self) -> None:
        realm = self._make_realm("Bot Realm")
        human = self._make_user(realm, "human-active@example.com")
        inactive = self._make_user(realm, "inactive@example.com")
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])
        NodlUserExtension.objects.create(
            zulip_user=human,
            supabase_user_id=uuid.uuid4(),
            sync_status=SyncStatus.SYNCED,
        )

        output = self._run("--commit")

        self.assertEqual(NodlRealmUserExtension.objects.count(), 1)
        self.assertNotIn("inactive@example.com", output)
