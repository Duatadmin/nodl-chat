"""Backfill #general as a DefaultStream and subscribe existing members.

Realms created before workspace_sync registered #general as a DefaultStream
have a #general nobody is subscribed to, and users provisioned into them
start with zero subscriptions.  This command repairs all existing
nodl-managed realms (those with a NodlRealmExtension).

Dry-run by default; pass --commit to apply.
"""

from typing import Any

from django.core.management.base import BaseCommand

from nodl.extensions.models import NodlRealmExtension
from zerver.actions.default_streams import do_add_default_stream
from zerver.actions.streams import bulk_add_subscriptions
from zerver.lib.streams import ensure_stream
from zerver.models import Stream, Subscription, UserProfile


class Command(BaseCommand):
    help = (
        "Backfill #general as a DefaultStream and subscribe existing members "
        "for all nodl-managed realms.  Dry-run by default; --commit to apply."
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

    def handle(self, *args: Any, **options: Any) -> None:
        commit: bool = options["commit"]
        realm_filter: str | None = options.get("realm")

        extensions = NodlRealmExtension.objects.select_related("zulip_realm").order_by(
            "zulip_realm_id"
        )
        if realm_filter:
            extensions = extensions.filter(zulip_realm__string_id=realm_filter)

        total_subscribed = 0
        for extension in extensions:
            realm = extension.zulip_realm
            if realm is None or realm.deactivated:
                continue

            members = list(UserProfile.objects.filter(realm=realm, is_active=True, is_bot=False))
            if not commit:
                # Dry run: report what would happen without creating streams.
                existing_stream = Stream.objects.filter(realm=realm, name="general").first()
                already: set[int] = set()
                if existing_stream is not None:
                    already = set(
                        Subscription.objects.filter(
                            user_profile__in=members,
                            recipient=existing_stream.recipient,
                            active=True,
                        ).values_list("user_profile_id", flat=True)
                    )
                missing = [m for m in members if m.id not in already]
                self.stdout.write(
                    f"[dry-run] realm {realm.id} ({realm.string_id!r}): "
                    f"{len(members)} members, {len(missing)} to subscribe"
                )
                total_subscribed += len(missing)
                continue

            stream = ensure_stream(
                realm=realm,
                stream_name="general",
                invite_only=False,
                stream_description="General discussion",
                acting_user=None,
            )
            do_add_default_stream(stream)

            already = set(
                Subscription.objects.filter(
                    user_profile__in=members,
                    recipient=stream.recipient,
                    active=True,
                ).values_list("user_profile_id", flat=True)
            )
            to_subscribe = [m for m in members if m.id not in already]
            if to_subscribe:
                bulk_add_subscriptions(
                    realm,
                    [stream],
                    to_subscribe,
                    from_user_creation=True,
                    acting_user=None,
                )
            self.stdout.write(
                f"realm {realm.id} ({realm.string_id!r}): subscribed "
                f"{len(to_subscribe)} of {len(members)} members"
            )
            total_subscribed += len(to_subscribe)

        mode = "would subscribe" if not commit else "subscribed"
        self.stdout.write(self.style.SUCCESS(f"Done: {mode} {total_subscribed} users."))
