"""Backfill task-stream display metadata: title-as-description + "Task chats" folder.

Task streams created before sync_task_stream carried display metadata have
the placeholder description "Task discussion <uuid>" and no channel folder.
This command reconciles every NodlTaskStreamExtension whose stream is still
active: description becomes the task title (when one is recorded) and the
stream is filed under the realm's "Task chats" folder.

Dry-run by default; pass --commit to apply.
"""

from typing import Any

from django.core.management.base import BaseCommand

from nodl.extensions.models import NodlTaskStreamExtension
from nodl.extensions.task_chats import (
    get_or_create_task_chats_folder,
    task_stream_description,
    update_task_stream_description,
)
from zerver.actions.streams import do_change_stream_folder


class Command(BaseCommand):
    help = (
        "Set task-stream descriptions to task titles and file task streams "
        "under the 'Task chats' folder.  Dry-run by default; --commit to apply."
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

        extensions = (
            NodlTaskStreamExtension.objects.select_related("zulip_stream", "zulip_realm")
            .order_by("zulip_realm_id", "zulip_stream_id")
        )
        if realm_filter:
            extensions = extensions.filter(zulip_realm__string_id=realm_filter)

        descriptions_changed = 0
        folders_assigned = 0
        skipped_archived = 0
        skipped_untitled = 0
        folders_by_realm: dict[int, Any] = {}

        for extension in extensions:
            stream = extension.zulip_stream
            realm = extension.zulip_realm
            if stream is None or realm is None or stream.deactivated or realm.deactivated:
                skipped_archived += 1
                continue

            title = (extension.task_title or "").strip()
            wants_description = bool(title) and stream.description != task_stream_description(title)
            wants_folder = stream.folder_id is None
            if not title:
                skipped_untitled += 1

            if not commit:
                if wants_description:
                    descriptions_changed += 1
                if wants_folder:
                    folders_assigned += 1
                if wants_description or wants_folder:
                    self.stdout.write(
                        f"[dry-run] realm {realm.id} stream {stream.id} ({stream.name!r}): "
                        f"{'description ' if wants_description else ''}"
                        f"{'folder' if wants_folder else ''}"
                    )
                continue

            if wants_description and update_task_stream_description(stream, title):
                descriptions_changed += 1
            if wants_folder:
                folder = folders_by_realm.get(realm.id)
                if folder is None:
                    folder = get_or_create_task_chats_folder(realm)
                    folders_by_realm[realm.id] = folder
                do_change_stream_folder(stream, folder, acting_user=None)
                folders_assigned += 1

        mode = "would change" if not commit else "changed"
        self.stdout.write(
            self.style.SUCCESS(
                f"Done: {mode} {descriptions_changed} descriptions, "
                f"{folders_assigned} folder assignments "
                f"({skipped_archived} archived/missing skipped, "
                f"{skipped_untitled} without a recorded title)."
            )
        )
