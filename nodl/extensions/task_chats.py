"""Display metadata for task-owned streams: title-as-description + "Task chats" folder.

Task streams are provisioned by service-auth sync endpoints with no acting
user, so these helpers mirror the corresponding zerver actions where the
upstream versions require a real ``acting_user`` (folder creation, description
change).  The description helper also deliberately skips the notification-bot
"changed the description" message upstream sends — task-title renames are
routine sync traffic, not user actions worth announcing in-channel.
"""

import logging

from django.db import IntegrityError, transaction
from django.utils.timezone import now as timezone_now

from zerver.lib.channel_folders import get_channel_folder_dict
from zerver.lib.streams import can_access_stream_metadata_user_ids, render_stream_description
from zerver.models import ChannelFolder, Realm, RealmAuditLog, Stream
from zerver.models.realm_audit_logs import AuditLogEventType
from zerver.models.users import active_user_ids
from zerver.tornado.django_api import send_event_on_commit

logger = logging.getLogger(__name__)

TASK_CHATS_FOLDER_NAME = "Task chats"


def task_stream_description(task_title: str) -> str:
    """Normalize a task title into a valid single-line stream description."""
    return " ".join(task_title.split())[: Stream.MAX_DESCRIPTION_LENGTH]


def get_or_create_task_chats_folder(realm: Realm) -> ChannelFolder:
    """Fetch or create the realm's "Task chats" channel folder.

    Mirrors ``check_add_channel_folder`` (order = id, audit log, add event)
    but with no creator: this runs from service-auth sync, not a user action.
    """
    folder = ChannelFolder.objects.filter(
        realm=realm, name__iexact=TASK_CHATS_FOLDER_NAME, is_archived=False
    ).first()
    if folder is not None:
        return folder

    try:
        with transaction.atomic():
            folder = ChannelFolder.objects.create(
                realm=realm,
                name=TASK_CHATS_FOLDER_NAME,
                description="",
                rendered_description="",
                creator=None,
            )
            folder.order = folder.id
            folder.save(update_fields=["order"])
            RealmAuditLog.objects.create(
                realm=realm,
                acting_user=None,
                event_type=AuditLogEventType.CHANNEL_FOLDER_CREATED,
                event_time=timezone_now(),
                modified_channel_folder=folder,
            )
            event = {
                "type": "channel_folder",
                "op": "add",
                "channel_folder": get_channel_folder_dict(folder),
            }
            send_event_on_commit(realm, event, active_user_ids(realm.id))
            return folder
    except IntegrityError:
        # Lost a creation race against a concurrent sync; the winner's row
        # satisfies the (lower(name), realm) unique constraint.
        folder = ChannelFolder.objects.filter(
            realm=realm, name__iexact=TASK_CHATS_FOLDER_NAME, is_archived=False
        ).first()
        if folder is None:
            raise
        return folder


def update_task_stream_description(stream: Stream, task_title: str) -> bool:
    """Set a task stream's description to its task title; returns True if changed.

    Mirrors ``do_change_stream_description`` (save, audit log, update event)
    minus the notification-bot message, which both requires an acting user and
    would spam every task stream on routine title syncs.
    """
    description = task_stream_description(task_title)
    if not description or stream.description == description:
        return False

    old_description = stream.description
    stream.description = description
    stream.rendered_description = render_stream_description(
        description, stream.realm, acting_user=None
    )
    stream.save(update_fields=["description", "rendered_description"])
    RealmAuditLog.objects.create(
        realm=stream.realm,
        acting_user=None,
        modified_stream=stream,
        event_type=AuditLogEventType.CHANNEL_PROPERTY_CHANGED,
        event_time=timezone_now(),
        extra_data={
            RealmAuditLog.OLD_VALUE: old_description,
            RealmAuditLog.NEW_VALUE: description,
            "property": "description",
        },
    )
    event = {
        "type": "stream",
        "op": "update",
        "property": "description",
        "name": stream.name,
        "stream_id": stream.id,
        "value": description,
        "rendered_description": stream.rendered_description,
    }
    send_event_on_commit(stream.realm, event, can_access_stream_metadata_user_ids(stream))
    return True
