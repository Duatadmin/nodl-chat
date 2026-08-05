"""Single writer for NodlRealmUserExtension — the human-to-realm-profile map.

NodlRealmUserExtension answers "which Zulip profile realizes this Supabase
user inside this realm".  Bridge v2 serves a user's workspace list from this
table with a local join (no Supabase RPC at request time), so every code path
that provisions or resolves a realm profile for a Supabase user must record
the fact here — bridge provisioning, user sync, and task-stream membership
sync all go through record_realm_user_mapping.
"""

import logging
import uuid

from django.db import IntegrityError, transaction
from django.utils import timezone

from nodl.extensions.models import NodlRealmUserExtension
from zerver.models import Realm, UserProfile

logger = logging.getLogger(__name__)


def record_realm_user_mapping(
    realm: Realm,
    user: UserProfile,
    supabase_user_id: str | uuid.UUID | None,
) -> NodlRealmUserExtension | None:
    """Record that *user* is *supabase_user_id*'s profile inside *realm*.

    Idempotent: re-recording the same triple is a no-op, and a stale mapping
    for the same (realm, supabase user) is re-pointed to *user* (self-heal
    after a profile was recreated).

    Returns the mapping row, or None when the write was skipped:
    - missing/invalid supabase user id,
    - *user* does not belong to *realm*,
    - *user* is already mapped to a DIFFERENT supabase user.  That means two
      Supabase identities claim one Zulip profile — a data problem a sync
      write must never "fix" by re-homing; it is logged loudly and skipped.
    """
    try:
        supabase_uuid = uuid.UUID(str(supabase_user_id))
    except (AttributeError, TypeError, ValueError):
        logger.warning(
            "Skipping realm-user mapping for profile %d: unusable supabase id %r",
            user.id,
            supabase_user_id,
        )
        return None

    if user.realm_id != realm.id:
        logger.error(
            "Skipping realm-user mapping: profile %d belongs to realm %d, not realm %d",
            user.id,
            user.realm_id,
            realm.id,
        )
        return None

    try:
        # Savepoint so an IntegrityError cannot poison a caller's transaction.
        with transaction.atomic():
            mapping, _created = NodlRealmUserExtension.objects.update_or_create(
                zulip_realm=realm,
                supabase_user_id=supabase_uuid,
                defaults={"zulip_user": user, "last_synced_at": timezone.now()},
            )
            return mapping
    except IntegrityError:
        # Concurrent create of the same triple loses the race but the row it
        # wanted now exists — return it.
        existing = NodlRealmUserExtension.objects.filter(
            zulip_realm=realm, supabase_user_id=supabase_uuid, zulip_user=user
        ).first()
        if existing is not None:
            return existing

        conflict = (
            NodlRealmUserExtension.objects.filter(zulip_user=user)
            .exclude(supabase_user_id=supabase_uuid)
            .first()
        )
        logger.error(
            "Refusing to re-home realm-user mapping: profile %d (realm %d) is "
            "already mapped to supabase user %s; not remapping to %s",
            user.id,
            realm.id,
            conflict.supabase_user_id if conflict else "<unknown>",
            supabase_uuid,
        )
        return None
