"""One-time unmute of force-muted task-stream subscriptions.

sync_task_stream historically forced ``is_muted=True`` onto every task-stream
subscription, which suppressed unread counts and (once message push exists)
push notifications on the primary mobile surface.  The force-mute has been
removed from the sync path; this command repairs existing subscriptions on
active task streams.  It runs once: after that, any mute is a deliberate user
choice and must not be touched again.

Uses do_change_subscription_property per subscription so live clients receive
proper subscription-update events.

Dry-run by default; pass --commit to apply.
"""

from typing import Any

from django.core.management.base import BaseCommand

from nodl.extensions.models import NodlTaskStreamExtension
from zerver.actions.streams import do_change_subscription_property
from zerver.models import Subscription


class Command(BaseCommand):
    help = (
        "Unmute force-muted subscriptions on active task streams. "
        "Dry-run by default; --commit to apply."
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

        extensions = NodlTaskStreamExtension.objects.select_related(
            "zulip_stream", "zulip_realm"
        ).order_by("zulip_realm_id", "zulip_stream_id")
        if realm_filter:
            extensions = extensions.filter(zulip_realm__string_id=realm_filter)

        unmuted = 0
        for extension in extensions:
            stream = extension.zulip_stream
            realm = extension.zulip_realm
            if stream is None or realm is None or stream.deactivated or realm.deactivated:
                continue

            muted_subs = list(
                Subscription.objects.select_related("user_profile").filter(
                    recipient_id=stream.recipient_id,
                    active=True,
                    is_muted=True,
                    user_profile__is_active=True,
                )
            )
            if not muted_subs:
                continue

            if not commit:
                self.stdout.write(
                    f"[dry-run] realm {realm.id} stream {stream.id} ({stream.name!r}): "
                    f"{len(muted_subs)} muted subscriptions"
                )
                unmuted += len(muted_subs)
                continue

            for sub in muted_subs:
                do_change_subscription_property(
                    sub.user_profile,
                    sub,
                    stream,
                    "is_muted",
                    False,
                    acting_user=None,
                )
                unmuted += 1
            self.stdout.write(
                f"realm {realm.id} stream {stream.id} ({stream.name!r}): "
                f"unmuted {len(muted_subs)}"
            )

        mode = "would unmute" if not commit else "unmuted"
        self.stdout.write(self.style.SUCCESS(f"Done: {mode} {unmuted} subscriptions."))
