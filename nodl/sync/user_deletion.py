"""Cross-realm hard deletion of a nodl user's Zulip profiles.

Account deletion (App Store Guideline 5.1.1(v) / GDPR erasure) is
orchestrated by nodl-backend; this service is the chat-side purge step. For
every realm profile mapped to the Supabase user it:

1. Deletes the physical files the user uploaded (R2/S3 bytes, via the
   configured upload backend) — Zulip's profile deletion only destroys the
   Attachment rows, leaving the blobs orphaned in storage.
2. Deletes the avatar image files.
3. Runs ``do_delete_user_preserving_messages``: identity, settings, API key,
   sessions, reactions and DM/group subscriptions are destroyed; messages the
   user sent survive, reattributed to an inactive "Deleted User {id}" mirror
   dummy, so shared workspace history stays intact for the team.

The Nodl extension mappings (``NodlUserExtension`` /
``NodlRealmUserExtension``) reference the profile with ``on_delete=CASCADE``,
so they vanish with it — which also makes this operation idempotent: a retry
finds no mappings and succeeds with ``deleted_profiles=0``.
"""

import logging
from dataclasses import dataclass, field

from nodl.extensions.models import NodlRealmUserExtension, NodlUserExtension
from zerver.actions.users import do_delete_user_preserving_messages
from zerver.lib.upload import delete_avatar_image, delete_message_attachments
from zerver.models import Attachment, UserProfile

logger = logging.getLogger(__name__)


@dataclass
class UserDeletionResult:
    """Result of a cross-realm user deletion."""

    success: bool
    deleted_profiles: int = 0
    realm_ids: list[int] = field(default_factory=list)
    error: str | None = None


class UserDeletionService:
    """Hard-deletes every Zulip profile linked to one Supabase user."""

    def delete_user(self, supabase_user_id: str) -> UserDeletionResult:
        profiles: dict[int, UserProfile] = {}

        realm_mappings = NodlRealmUserExtension.objects.filter(
            supabase_user_id=supabase_user_id
        ).select_related("zulip_user")
        for mapping in realm_mappings:
            profiles[mapping.zulip_user_id] = mapping.zulip_user

        legacy = (
            NodlUserExtension.objects.filter(supabase_user_id=supabase_user_id)
            .select_related("zulip_user")
            .first()
        )
        if legacy is not None and legacy.zulip_user is not None:
            profiles[legacy.zulip_user.id] = legacy.zulip_user

        deleted = 0
        realm_ids: list[int] = []
        for profile in profiles.values():
            if profile.is_bot or profile.is_mirror_dummy:
                # Mappings only ever point at human profiles; a mirror dummy
                # here would mean deletion already ran — skip defensively.
                continue
            try:
                self._purge_uploaded_files(profile)
                do_delete_user_preserving_messages(profile)
                deleted += 1
                realm_ids.append(profile.realm_id)
                logger.info(
                    "nodl_user_deleted",
                    extra={
                        "supabase_user_id": supabase_user_id,
                        "zulip_user_id": profile.id,
                        "realm_id": profile.realm_id,
                    },
                )
            except Exception as e:
                logger.exception(
                    "nodl_user_deletion_failed",
                    extra={
                        "supabase_user_id": supabase_user_id,
                        "zulip_user_id": profile.id,
                    },
                )
                return UserDeletionResult(
                    success=False,
                    deleted_profiles=deleted,
                    realm_ids=realm_ids,
                    error=f"deleting zulip user {profile.id} failed: {e}",
                )

        # A pending legacy mapping (zulip_user=None) has no profile to cascade
        # from — remove it explicitly so no identifier survives.
        NodlUserExtension.objects.filter(
            supabase_user_id=supabase_user_id, zulip_user__isnull=True
        ).delete()

        return UserDeletionResult(success=True, deleted_profiles=deleted, realm_ids=realm_ids)

    def _purge_uploaded_files(self, profile: UserProfile) -> None:
        """Delete the physical storage objects behind the user's uploads.

        Best-effort: storage failures are logged but never block the profile
        deletion — the Attachment rows die with the profile either way, so
        the files become inaccessible even if bytes linger.
        """
        try:
            path_ids = list(
                Attachment.objects.filter(owner=profile).values_list("path_id", flat=True)
            )
            if path_ids:
                delete_message_attachments(path_ids)
                logger.info(
                    "nodl_user_uploads_purged",
                    extra={"zulip_user_id": profile.id, "files": len(path_ids)},
                )
        except Exception:
            logger.exception(
                "nodl_user_upload_purge_failed",
                extra={"zulip_user_id": profile.id},
            )

        try:
            if profile.avatar_source == UserProfile.AVATAR_FROM_USER:
                delete_avatar_image(profile, profile.avatar_version)
        except Exception:
            logger.exception(
                "nodl_user_avatar_purge_failed",
                extra={"zulip_user_id": profile.id},
            )
