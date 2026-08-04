"""Deterministic workspace-to-realm resolution for the auth bridge.

Given the workspace UUIDs a Supabase user belongs to, resolve them to active
Zulip realms and rank the realms so the bridge always picks the same, most
relevant one.  Shared by bridge v1 (single-realm response) and, later, bridge
v2 (full workspace list).
"""

import logging

from django.core.exceptions import ValidationError
from django.db.models import Max

from nodl.extensions.models import NodlRealmExtension
from zerver.models import Realm, UserActivity, UserMessage, UserProfile

logger = logging.getLogger(__name__)


def resolve_candidate_realms(workspace_ids: list[str]) -> list[Realm]:
    """Resolve nodl workspace UUIDs to active Zulip realms, preserving order.

    Resolution goes through NodlRealmExtension — the authoritative
    workspace↔realm map.  The legacy ``workspace_id[:20]`` string_id lookup is
    kept only for realms created before the extension table existed.
    Deactivated realms and unknown workspace ids are dropped.
    """
    realms: list[Realm] = []
    seen: set[int] = set()
    for ws_id in workspace_ids:
        realm = None
        try:
            extension = NodlRealmExtension.objects.select_related("zulip_realm").get(
                nodl_workspace_id=ws_id
            )
            realm = extension.zulip_realm
        except (NodlRealmExtension.DoesNotExist, ValidationError, ValueError):
            # Legacy fallback: realms provisioned before NodlRealmExtension.
            try:
                realm = Realm.objects.get(string_id=ws_id[:20].lower())
            except Realm.DoesNotExist:
                logger.warning(
                    "NODL_DEBUG: workspace %s has no matching realm", ws_id
                )
                continue
        if realm is None or realm.deactivated or realm.string_id == "zulipinternal":
            continue
        if realm.id in seen:
            continue
        seen.add(realm.id)
        realms.append(realm)
    return realms


def rank_realms_for_user(realms: list[Realm], email: str) -> list[Realm]:
    """Order candidate realms by relevance for the user with *email*.

    Ranking keys, most significant first:
    1. An active UserProfile with this email already exists in the realm
       (an established identity always beats provisioning a fresh one).
    2. Highest ``UserMessage.message_id`` for that profile — message ids are
       globally monotonic across realms in one deployment, so this is a
       direct cross-realm recency comparison that always has data.
    3. Most recent ``UserActivity.last_visit`` (tiebreak; JWT-path views do
       not write UserActivity, so this only helps for API-key clients).
    4. Most recent ``UserProfile.date_joined``.
    5. Lowest ``Realm.id`` — guarantees a total order.
    """
    if not realms:
        return []

    profiles = {
        p.realm_id: p
        for p in UserProfile.objects.filter(
            delivery_email__iexact=email,
            is_active=True,
            realm__in=realms,
        )
    }
    profile_ids = [p.id for p in profiles.values()]

    max_message_by_profile: dict[int, int] = {}
    last_visit_by_profile: dict[int, object] = {}
    if profile_ids:
        for row in (
            UserMessage.objects.filter(user_profile_id__in=profile_ids)
            .values("user_profile_id")
            .annotate(max_id=Max("message_id"))
        ):
            max_message_by_profile[row["user_profile_id"]] = row["max_id"]
        for row in (
            UserActivity.objects.filter(user_profile_id__in=profile_ids)
            .values("user_profile_id")
            .annotate(last=Max("last_visit"))
        ):
            last_visit_by_profile[row["user_profile_id"]] = row["last"]

    def sort_key(realm: Realm) -> tuple:
        profile = profiles.get(realm.id)
        if profile is None:
            return (1, 0, 0, 0, realm.id)
        max_message_id = max_message_by_profile.get(profile.id, 0)
        last_visit = last_visit_by_profile.get(profile.id)
        last_visit_ts = last_visit.timestamp() if last_visit is not None else 0.0
        date_joined_ts = profile.date_joined.timestamp() if profile.date_joined else 0.0
        return (0, -max_message_id, -last_visit_ts, -date_joined_ts, realm.id)

    return sorted(realms, key=sort_key)
