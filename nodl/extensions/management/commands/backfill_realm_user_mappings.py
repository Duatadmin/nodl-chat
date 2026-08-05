"""Backfill NodlRealmUserExtension — the human-to-realm-profile map.

Bridge v2 answers "which workspaces does this human have, with which
credentials" from NodlRealmUserExtension alone, so every existing profile in
a nodl-managed realm needs a row.  Historically only task-stream sync wrote
them; this command reconstructs the rest from what the deployment already
knows, in four tiers (first match wins):

1. direct    — the profile is linked from the global NodlUserExtension.
2. email     — another profile with the same delivery email is already
               mapped to a Supabase user (cross-realm identity; this is how
               one human's N per-realm profiles are reconstructed).
3. supabase  — Supabase Admin lookup by email, or by phone for derived
               ``<phone>@nodl.local`` addresses.
4. skip      — reported, never guessed.

Dry-run by default; pass --commit to write.  --verify diffs committed
mappings against live Supabase workspace membership and writes nothing.
"""

import re
import uuid
from typing import Any

from django.core.management.base import BaseCommand

from nodl.extensions.mapping import record_realm_user_mapping
from nodl.extensions.models import (
    NodlRealmExtension,
    NodlRealmUserExtension,
    NodlUserExtension,
)
from zerver.models import UserProfile

AMBIGUOUS = object()  # email index sentinel: two Supabase users claim one email


class Command(BaseCommand):
    help = (
        "Backfill NodlRealmUserExtension for existing profiles in nodl-managed "
        "realms.  Dry-run by default; --commit to write; --verify to diff "
        "against Supabase membership."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Apply changes (default is a dry run that only reports).",
        )
        parser.add_argument(
            "--realm",
            help="Limit to a single realm by string_id.",
        )
        parser.add_argument(
            "--verify",
            action="store_true",
            help="Read-only: diff existing mappings against Supabase workspace membership.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["verify"]:
            self._verify(options.get("realm"))
            return

        commit: bool = options["commit"]
        realm_filter: str | None = options.get("realm")

        extensions = NodlRealmExtension.objects.select_related("zulip_realm").order_by(
            "zulip_realm_id"
        )
        if realm_filter:
            extensions = extensions.filter(zulip_realm__string_id=realm_filter)
        realms = [
            ext.zulip_realm
            for ext in extensions
            if ext.zulip_realm is not None and not ext.zulip_realm.deactivated
        ]

        by_profile_id, by_email = self._build_identity_index()

        counts = {"direct": 0, "email": 0, "supabase": 0, "skip": 0, "conflict": 0}
        skipped: list[str] = []
        supabase_cache: dict[str, uuid.UUID | None] = {}

        for realm in realms:
            candidates = UserProfile.objects.filter(
                realm=realm,
                is_active=True,
                is_bot=False,
                nodl_realm_user_extension__isnull=True,
            ).order_by("id")
            for profile in candidates:
                email_key = profile.delivery_email.lower()

                supabase_id = by_profile_id.get(profile.id)
                tier = "direct"
                if supabase_id is None:
                    indexed = by_email.get(email_key)
                    if indexed is AMBIGUOUS:
                        indexed = None
                    if indexed is not None:
                        supabase_id = indexed
                        tier = "email"
                if supabase_id is None:
                    if email_key not in supabase_cache:
                        supabase_cache[email_key] = self._supabase_lookup(email_key)
                    supabase_id = supabase_cache[email_key]
                    tier = "supabase"

                if supabase_id is None:
                    counts["skip"] += 1
                    skipped.append(
                        f"realm {realm.id} ({realm.string_id!r}) profile "
                        f"{profile.id} <{profile.delivery_email}>"
                    )
                    continue

                if commit:
                    mapping = record_realm_user_mapping(realm, profile, supabase_id)
                    if mapping is None:
                        counts["conflict"] += 1
                        continue
                counts[tier] += 1
                # Newly established identities extend the email index so later
                # realms in this run resolve via tier 2 instead of re-querying.
                if by_email.get(email_key) not in (supabase_id, AMBIGUOUS):
                    by_email[email_key] = AMBIGUOUS if email_key in by_email else supabase_id

        mode = "backfilled" if commit else "[dry-run] would backfill"
        self.stdout.write(
            f"{mode}: direct={counts['direct']} email={counts['email']} "
            f"supabase={counts['supabase']} skipped={counts['skip']} "
            f"conflicts={counts['conflict']}"
        )
        for line in skipped:
            self.stdout.write(f"  skipped: {line}")
        self.stdout.write(self.style.SUCCESS("Done."))

    def _build_identity_index(
        self,
    ) -> tuple[dict[int, uuid.UUID], dict[str, Any]]:
        """Index known Supabase identities: by profile id and by email.

        Sources: the global NodlUserExtension links and every existing
        NodlRealmUserExtension row.  An email claimed by two different
        Supabase users is marked AMBIGUOUS and never used for tier 2.
        """
        by_profile_id: dict[int, uuid.UUID] = {}
        by_email: dict[str, Any] = {}

        def index_email(email: str, supabase_id: uuid.UUID) -> None:
            key = email.lower()
            existing = by_email.get(key)
            if existing is None:
                by_email[key] = supabase_id
            elif existing is not AMBIGUOUS and existing != supabase_id:
                by_email[key] = AMBIGUOUS

        for ext in NodlUserExtension.objects.select_related("zulip_user").exclude(zulip_user=None):
            by_profile_id[ext.zulip_user_id] = ext.supabase_user_id
            index_email(ext.zulip_user.delivery_email, ext.supabase_user_id)

        for row in NodlRealmUserExtension.objects.select_related("zulip_user"):
            by_profile_id.setdefault(row.zulip_user_id, row.supabase_user_id)
            index_email(row.zulip_user.delivery_email, row.supabase_user_id)

        return by_profile_id, by_email

    def _supabase_lookup(self, email: str) -> uuid.UUID | None:
        """Tier 3: resolve an email to a Supabase user via the Admin API."""
        from zproject.nodl.actions import (
            get_supabase_user_by_email,
            get_supabase_user_by_phone,
        )

        local, _, domain = email.partition("@")
        if domain == "nodl.local":
            digits = re.sub(r"\D", "", local)
            user = get_supabase_user_by_phone(digits) if digits else None
        else:
            user = get_supabase_user_by_email(email)
        if user is None:
            return None
        try:
            return uuid.UUID(str(user.get("id")))
        except (TypeError, ValueError):
            return None

    def _verify(self, realm_filter: str | None) -> None:
        """Diff committed mappings against live Supabase workspace membership.

        Read-only.  Reports, per Supabase user:
        - stale mappings: realms mapped locally whose workspace Supabase no
          longer lists (revoked membership that kept a live profile), and
        - missing mappings: workspaces Supabase lists with a resolvable realm
          but no mapping row.
        """
        from zproject.nodl.actions import get_user_workspace_ids

        rows = NodlRealmUserExtension.objects.select_related("zulip_realm")
        if realm_filter:
            rows = rows.filter(zulip_realm__string_id=realm_filter)

        mapped_by_user: dict[uuid.UUID, set[int]] = {}
        for row in rows:
            mapped_by_user.setdefault(row.supabase_user_id, set()).add(row.zulip_realm_id)

        realm_by_workspace = {
            str(ext.nodl_workspace_id): ext.zulip_realm_id
            for ext in NodlRealmExtension.objects.exclude(zulip_realm=None)
        }

        drift = 0
        for supabase_id, mapped_realm_ids in sorted(
            mapped_by_user.items(), key=lambda item: str(item[0])
        ):
            workspace_ids = get_user_workspace_ids(str(supabase_id))
            if workspace_ids is None:
                self.stdout.write(f"{supabase_id}: Supabase lookup FAILED — skipped")
                continue
            member_realm_ids = {
                realm_by_workspace[ws] for ws in workspace_ids if ws in realm_by_workspace
            }
            stale = mapped_realm_ids - member_realm_ids
            missing = member_realm_ids - mapped_realm_ids
            if stale or missing:
                drift += 1
                self.stdout.write(
                    f"{supabase_id}: stale realm mappings {sorted(stale)}, "
                    f"missing realm mappings {sorted(missing)}"
                )

        if drift:
            self.stdout.write(
                self.style.WARNING(f"Drift on {drift} of {len(mapped_by_user)} users.")
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"No drift across {len(mapped_by_user)} users."))
