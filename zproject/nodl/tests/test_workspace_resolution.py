"""Unit tests for workspace→realm resolution helpers."""

from nodl.extensions.models import NodlRealmExtension
from zerver.actions.create_realm import do_create_realm
from zerver.actions.create_user import do_create_user
from zerver.actions.realm_settings import do_deactivate_realm
from zerver.lib.test_classes import ZulipTestCase
from zerver.models.realms import get_realm
from zproject.nodl.workspace_resolution import (
    rank_realms_for_user,
    resolve_candidate_realms,
)

WS_A = "11111111-2222-4333-8444-555555555555"
WS_B = "66666666-7777-4888-8999-aaaaaaaaaaaa"


class ResolveCandidateRealmsTest(ZulipTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.realm_a = get_realm("zulip")
        NodlRealmExtension.objects.get_or_create(
            zulip_realm=self.realm_a, defaults={"nodl_workspace_id": WS_A}
        )
        self.realm_b = do_create_realm(
            string_id="resolvews",
            name="Resolve WS",
            create_zulip_discussion_channel=False,
        )
        NodlRealmExtension.objects.create(
            zulip_realm=self.realm_b, nodl_workspace_id=WS_B
        )

    def test_resolves_via_extension_preserving_order(self) -> None:
        realms = resolve_candidate_realms([WS_B, WS_A])
        self.assertEqual([r.id for r in realms], [self.realm_b.id, self.realm_a.id])

    def test_unknown_and_invalid_ids_are_dropped(self) -> None:
        realms = resolve_candidate_realms(
            ["99999999-9999-4999-8999-999999999999", "not-a-uuid", WS_A]
        )
        self.assertEqual([r.id for r in realms], [self.realm_a.id])

    def test_deactivated_realms_are_dropped(self) -> None:
        do_deactivate_realm(
            self.realm_b,
            acting_user=None,
            deactivation_reason="owner_request",
            email_owners=False,
        )
        realms = resolve_candidate_realms([WS_B, WS_A])
        self.assertEqual([r.id for r in realms], [self.realm_a.id])

    def test_duplicates_are_dropped(self) -> None:
        realms = resolve_candidate_realms([WS_A, WS_A])
        self.assertEqual([r.id for r in realms], [self.realm_a.id])

    def test_legacy_truncated_string_id_fallback(self) -> None:
        # A realm with no extension row resolves via workspace_id[:20].
        legacy_ws_id = "cccccccc-dddd-4eee-8fff-000000000000"
        legacy_realm = do_create_realm(
            string_id=legacy_ws_id[:20].lower(),
            name="Legacy",
            create_zulip_discussion_channel=False,
        )
        realms = resolve_candidate_realms([legacy_ws_id])
        self.assertEqual([r.id for r in realms], [legacy_realm.id])


class RankRealmsForUserTest(ZulipTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.realm_a = get_realm("zulip")
        self.realm_b = do_create_realm(
            string_id="rankws",
            name="Rank WS",
            create_zulip_discussion_channel=False,
        )

    def test_empty_input(self) -> None:
        self.assertEqual(rank_realms_for_user([], "x@example.com"), [])

    def test_realm_with_existing_profile_wins(self) -> None:
        email = "ranked-user@nodl.local"
        do_create_user(
            email=email,
            password=None,
            realm=self.realm_b,
            full_name="Ranked",
            acting_user=None,
        )
        ranked = rank_realms_for_user([self.realm_a, self.realm_b], email)
        self.assertEqual(ranked[0].id, self.realm_b.id)

    def test_no_profiles_orders_by_realm_id(self) -> None:
        ranked = rank_realms_for_user(
            [self.realm_b, self.realm_a], "nobody@nodl.local"
        )
        self.assertEqual([r.id for r in ranked], sorted([self.realm_a.id, self.realm_b.id]))

    def test_message_recency_beats_join_date(self) -> None:
        email = "recency-user@nodl.local"
        profile_a = do_create_user(
            email=email,
            password=None,
            realm=self.realm_a,
            full_name="In A",
            acting_user=None,
        )
        profile_b = do_create_user(
            email=email,
            password=None,
            realm=self.realm_b,
            full_name="In B",
            acting_user=None,
        )
        # profile_a joined earlier but gets the newer message.
        peer = self.example_user("othello")
        self.send_personal_message(peer, profile_a)
        assert profile_b is not None

        ranked = rank_realms_for_user([self.realm_a, self.realm_b], email)
        self.assertEqual(ranked[0].id, self.realm_a.id)
