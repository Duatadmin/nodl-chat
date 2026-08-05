"""Unit tests for WorkspaceSyncService.

Tests cover:
- Workspace sync creates realm (IV1)
- Member sync (IV2)
- Workspace deletion deactivates realm (IV3)
"""

import uuid
from unittest.mock import MagicMock, patch

from django.test import TestCase

from nodl.extensions.models import NodlRealmExtension, SyncStatus
from nodl.sync.workspace_sync import (
    WorkspaceSyncRequest,
    WorkspaceSyncResult,
    WorkspaceSyncService,
)


class TestWorkspaceSyncService(TestCase):
    """Test cases for WorkspaceSyncService."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.service = WorkspaceSyncService()
        self.sample_request = WorkspaceSyncRequest(
            nodl_workspace_id=str(uuid.uuid4()),
            name="Test Workspace",
            description="A test workspace",
            members=[],
        )

    @patch("nodl.sync.workspace_sync.do_add_default_stream")
    @patch("nodl.sync.workspace_sync.do_create_realm")
    @patch("nodl.sync.workspace_sync.ensure_stream")
    def test_sync_creates_realm_with_default_stream(
        self,
        mock_ensure_stream: MagicMock,
        mock_create_realm: MagicMock,
        mock_add_default: MagicMock,
    ) -> None:
        """Test sync creates realm with default #general stream (IV1)."""
        mock_realm = MagicMock()
        mock_realm.id = 1
        mock_create_realm.return_value = mock_realm

        result = self.service.sync_workspace(self.sample_request)

        self.assertTrue(result.success)
        self.assertEqual(result.zulip_realm_id, 1)

        # Verify realm created with correct params
        mock_create_realm.assert_called_once()
        call_kwargs = mock_create_realm.call_args.kwargs
        self.assertEqual(call_kwargs["name"], "Test Workspace")
        self.assertEqual(call_kwargs["description"], "A test workspace")
        self.assertFalse(call_kwargs["create_zulip_discussion_channel"])

        # Verify default stream created and registered as a DefaultStream
        mock_ensure_stream.assert_called_once()
        stream_kwargs = mock_ensure_stream.call_args.kwargs
        self.assertEqual(stream_kwargs["stream_name"], "general")
        self.assertFalse(stream_kwargs["invite_only"])
        mock_add_default.assert_called_once_with(mock_ensure_stream.return_value)

    @patch("nodl.sync.workspace_sync.do_add_default_stream")
    @patch("nodl.sync.workspace_sync.do_create_realm")
    @patch("nodl.sync.workspace_sync.ensure_stream")
    def test_sync_creates_extension_record(
        self,
        mock_ensure_stream: MagicMock,
        mock_create_realm: MagicMock,
        mock_add_default: MagicMock,
    ) -> None:
        """Test sync creates NodlRealmExtension record."""
        mock_realm = MagicMock()
        mock_realm.id = 1
        mock_create_realm.return_value = mock_realm

        workspace_id = str(uuid.uuid4())
        request = WorkspaceSyncRequest(
            nodl_workspace_id=workspace_id,
            name="Test Workspace",
            description=None,
            members=[],
        )

        result = self.service.sync_workspace(request)

        self.assertTrue(result.success)

        # Verify extension record created
        extension = NodlRealmExtension.objects.get(nodl_workspace_id=uuid.UUID(workspace_id))
        self.assertEqual(extension.sync_status, SyncStatus.SYNCED)
        self.assertIsNotNone(extension.last_synced_at)

    @patch("nodl.sync.workspace_sync.NodlRealmExtension.objects.select_related")
    def test_sync_updates_existing_realm(self, mock_select_related: MagicMock) -> None:
        """Test sync updates existing realm instead of creating new one."""
        mock_realm = MagicMock()
        mock_realm.id = 1
        mock_realm.name = "Old Name"
        mock_realm.description = "Old description"

        mock_extension = MagicMock()
        mock_extension.zulip_realm = mock_realm
        mock_extension.nodl_workspace_id = uuid.UUID(self.sample_request.nodl_workspace_id)

        mock_queryset = MagicMock()
        mock_queryset.get_or_create.return_value = (mock_extension, False)
        mock_select_related.return_value = mock_queryset

        result = self.service.sync_workspace(self.sample_request)

        self.assertTrue(result.success)
        self.assertEqual(result.zulip_realm_id, 1)

        # Verify realm was updated, not created
        self.assertEqual(mock_realm.name, "Test Workspace")

    @patch("nodl.sync.workspace_sync.do_create_realm")
    @patch("nodl.sync.workspace_sync.ensure_stream")
    def test_sync_fails_on_exception(
        self, mock_ensure_stream: MagicMock, mock_create_realm: MagicMock
    ) -> None:
        """Test sync handles exceptions and sets failed status."""
        mock_create_realm.side_effect = Exception("Realm creation failed")

        result = self.service.sync_workspace(self.sample_request)

        self.assertFalse(result.success)
        self.assertIsNone(result.zulip_realm_id)
        self.assertIn("Realm creation failed", result.error or "")

        # Verify extension status set to failed
        extension = NodlRealmExtension.objects.get(
            nodl_workspace_id=uuid.UUID(self.sample_request.nodl_workspace_id)
        )
        self.assertEqual(extension.sync_status, SyncStatus.FAILED)


class TestMemberSync(TestCase):
    """Test cases for member synchronization (IV2)."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.service = WorkspaceSyncService()

    @patch.object(WorkspaceSyncService, "_deactivate_removed_members")
    @patch.object(WorkspaceSyncService, "_ensure_general_subscriptions")
    @patch("nodl.sync.workspace_sync.UserSyncService")
    def test_sync_workspace_members(
        self,
        mock_user_sync_service: MagicMock,
        mock_ensure_general: MagicMock,
        mock_deactivate_removed: MagicMock,
    ) -> None:
        """Test member sync calls UserSyncService for each member."""
        mock_instance = MagicMock()
        mock_instance.sync_user.return_value = MagicMock(success=True, zulip_user_id=1)
        mock_user_sync_service.return_value = mock_instance

        mock_realm = MagicMock()
        mock_realm.string_id = "test-workspace"
        mock_realm.id = 1

        members = [
            {
                "supabase_user_id": str(uuid.uuid4()),
                "email": "user1@example.com",
                "full_name": "User One",
                "role": "editor",
            },
            {
                "supabase_user_id": str(uuid.uuid4()),
                "email": "user2@example.com",
                "full_name": "User Two",
                "role": "viewer",
            },
        ]

        self.service.sync_workspace_members(mock_realm, members)

        # Verify UserSyncService called for each member, and the synced ids
        # were handed to the #general subscription pass
        self.assertEqual(mock_instance.sync_user.call_count, 2)
        mock_ensure_general.assert_called_once_with(mock_realm, [1, 1])
        mock_deactivate_removed.assert_called_once_with(mock_realm, members)

    @patch.object(WorkspaceSyncService, "_deactivate_removed_members")
    @patch.object(WorkspaceSyncService, "_ensure_general_subscriptions")
    @patch("nodl.sync.workspace_sync.UserSyncService")
    def test_member_sync_continues_on_failure(
        self,
        mock_user_sync_service: MagicMock,
        mock_ensure_general: MagicMock,
        mock_deactivate_removed: MagicMock,
    ) -> None:
        """Test member sync continues even if one member fails."""
        mock_instance = MagicMock()
        # First member fails, second succeeds
        mock_instance.sync_user.side_effect = [
            MagicMock(success=False, error="User sync failed"),
            MagicMock(success=True, zulip_user_id=2),
        ]
        mock_user_sync_service.return_value = mock_instance

        mock_realm = MagicMock()
        mock_realm.string_id = "test-workspace"
        mock_realm.id = 1

        members = [
            {"supabase_user_id": str(uuid.uuid4()), "email": "fail@example.com", "role": "editor"},
            {
                "supabase_user_id": str(uuid.uuid4()),
                "email": "success@example.com",
                "role": "viewer",
            },
        ]

        # Should not raise exception
        self.service.sync_workspace_members(mock_realm, members)

        # Both members were attempted; only the successful one is subscribed
        self.assertEqual(mock_instance.sync_user.call_count, 2)
        mock_ensure_general.assert_called_once_with(mock_realm, [2])


class TestRealmDeactivation(TestCase):
    """Test cases for realm deactivation (IV3)."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.service = WorkspaceSyncService()

    def test_deactivate_realm_not_found(self) -> None:
        """Test deactivate fails when workspace not found."""
        result = self.service.deactivate_realm(str(uuid.uuid4()))

        self.assertFalse(result.success)
        self.assertIn("No realm found", result.error or "")

    @patch("nodl.sync.workspace_sync.do_deactivate_realm")
    @patch("nodl.sync.workspace_sync.NodlRealmExtension.objects.select_related")
    def test_deactivate_realm_success(
        self, mock_select_related: MagicMock, mock_deactivate: MagicMock
    ) -> None:
        """Test realm deactivation (soft delete) success (IV3)."""
        mock_realm = MagicMock()
        mock_realm.id = 1

        mock_extension = MagicMock()
        mock_extension.zulip_realm = mock_realm

        mock_queryset = MagicMock()
        mock_queryset.get.return_value = mock_extension
        mock_select_related.return_value = mock_queryset

        workspace_id = str(uuid.uuid4())
        result = self.service.deactivate_realm(workspace_id)

        self.assertTrue(result.success)
        self.assertEqual(result.zulip_realm_id, 1)

        # Verify soft delete called (not hard delete)
        mock_deactivate.assert_called_once()
        call_kwargs = mock_deactivate.call_args.kwargs
        self.assertEqual(call_kwargs["deactivation_reason"], "workspace_deleted")
        self.assertFalse(call_kwargs["email_owners"])

    @patch("nodl.sync.workspace_sync.do_deactivate_realm")
    @patch("nodl.sync.workspace_sync.NodlRealmExtension.objects.select_related")
    def test_deactivate_realm_handles_exception(
        self, mock_select_related: MagicMock, mock_deactivate: MagicMock
    ) -> None:
        """Test deactivate handles exceptions gracefully."""
        mock_realm = MagicMock()
        mock_realm.id = 1

        mock_extension = MagicMock()
        mock_extension.zulip_realm = mock_realm

        mock_queryset = MagicMock()
        mock_queryset.get.return_value = mock_extension
        mock_select_related.return_value = mock_queryset

        mock_deactivate.side_effect = Exception("Deactivation failed")

        result = self.service.deactivate_realm(str(uuid.uuid4()))

        self.assertFalse(result.success)
        self.assertIn("Deactivation failed", result.error or "")


class TestWorkspaceSyncRequest(TestCase):
    """Test cases for WorkspaceSyncRequest dataclass."""

    def test_request_creation(self) -> None:
        """Test WorkspaceSyncRequest can be created with all fields."""
        request = WorkspaceSyncRequest(
            nodl_workspace_id="workspace-uuid",
            name="Test Workspace",
            description="A test workspace",
            members=[
                {"supabase_user_id": "user-uuid", "email": "test@example.com", "role": "editor"}
            ],
        )

        self.assertEqual(request.nodl_workspace_id, "workspace-uuid")
        self.assertEqual(request.name, "Test Workspace")
        self.assertEqual(request.description, "A test workspace")
        self.assertEqual(len(request.members), 1)

    def test_request_with_empty_members(self) -> None:
        """Test WorkspaceSyncRequest accepts empty members list."""
        request = WorkspaceSyncRequest(
            nodl_workspace_id="workspace-uuid",
            name="Test Workspace",
            description=None,
            members=[],
        )

        self.assertEqual(len(request.members), 0)
        self.assertIsNone(request.description)


class TestWorkspaceSyncResult(TestCase):
    """Test cases for WorkspaceSyncResult dataclass."""

    def test_success_result(self) -> None:
        """Test successful sync result."""
        result = WorkspaceSyncResult(
            success=True,
            zulip_realm_id=123,
            error=None,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.zulip_realm_id, 123)
        self.assertIsNone(result.error)

    def test_failure_result(self) -> None:
        """Test failed sync result."""
        result = WorkspaceSyncResult(
            success=False,
            zulip_realm_id=None,
            error="Test error message",
        )

        self.assertFalse(result.success)
        self.assertIsNone(result.zulip_realm_id)
        self.assertEqual(result.error, "Test error message")


class TestEnsureGeneralSubscriptions(TestCase):
    """Real-DB tests for the #general DefaultStream + subscription pass."""

    def _make_realm_and_users(self) -> tuple:
        from zerver.actions.create_realm import do_create_realm
        from zerver.actions.create_user import do_create_user

        realm = do_create_realm(
            string_id="gensubtest",
            name="General Sub Test",
            create_zulip_discussion_channel=False,
        )
        users = [
            do_create_user(
                email=f"gensub{i}@nodl.local",
                password=None,
                realm=realm,
                full_name=f"Gen Sub {i}",
                acting_user=None,
            )
            for i in range(2)
        ]
        return realm, users

    def test_subscribes_members_and_registers_default_stream(self) -> None:
        from zerver.models import DefaultStream, Stream, Subscription

        realm, users = self._make_realm_and_users()
        service = WorkspaceSyncService()

        service._ensure_general_subscriptions(realm, [u.id for u in users])

        stream = Stream.objects.get(realm=realm, name="general")
        self.assertTrue(DefaultStream.objects.filter(realm=realm, stream=stream).exists())
        for user in users:
            self.assertTrue(
                Subscription.objects.filter(
                    user_profile=user, recipient=stream.recipient, active=True
                ).exists()
            )

    def test_idempotent_on_second_run(self) -> None:
        from zerver.models import Stream, Subscription

        realm, users = self._make_realm_and_users()
        service = WorkspaceSyncService()
        user_ids = [u.id for u in users]

        service._ensure_general_subscriptions(realm, user_ids)
        service._ensure_general_subscriptions(realm, user_ids)

        stream = Stream.objects.get(realm=realm, name="general")
        self.assertEqual(
            Subscription.objects.filter(
                recipient=stream.recipient,
                active=True,
                user_profile_id__in=user_ids,
            ).count(),
            len(users),
        )

    def test_never_raises(self) -> None:
        realm, _users = self._make_realm_and_users()
        service = WorkspaceSyncService()
        with patch(
            "nodl.sync.workspace_sync.ensure_stream",
            side_effect=Exception("boom"),
        ):
            # Must swallow — a subscription failure never fails the sync.
            service._ensure_general_subscriptions(realm, [1])

    def test_empty_user_list_is_noop(self) -> None:
        from zerver.models import Stream

        realm, _users = self._make_realm_and_users()
        service = WorkspaceSyncService()
        service._ensure_general_subscriptions(realm, [])
        self.assertFalse(Stream.objects.filter(realm=realm, name="general").exists())


class TestDeactivateRemovedMembers(TestCase):
    """Real-DB tests for member-removal reconciliation.

    A removed member's api_key keeps authenticating until their realm
    profile is deactivated — the reconciliation pass closes that hole.
    """

    def _make_realm(self):
        from zerver.actions.create_realm import do_create_realm

        return do_create_realm(
            string_id=f"removal{uuid.uuid4().hex[:8]}",
            name="Removal Test",
            create_zulip_discussion_channel=False,
        )

    def _make_mapped_user(self, realm, email, supabase_id):
        from nodl.extensions.mapping import record_realm_user_mapping
        from zerver.actions.create_user import do_create_user

        user = do_create_user(
            email=email,
            password=None,
            realm=realm,
            full_name="Removal User",
            acting_user=None,
        )
        assert record_realm_user_mapping(realm, user, supabase_id) is not None
        return user

    def test_absent_member_is_deactivated(self) -> None:
        realm = self._make_realm()
        kept_id, removed_id = uuid.uuid4(), uuid.uuid4()
        kept = self._make_mapped_user(realm, "kept@example.com", kept_id)
        removed = self._make_mapped_user(realm, "removed@example.com", removed_id)

        WorkspaceSyncService()._deactivate_removed_members(
            realm,
            [{"supabase_user_id": str(kept_id), "email": "kept@example.com", "role": "editor"}],
        )

        kept.refresh_from_db()
        removed.refresh_from_db()
        self.assertTrue(kept.is_active)
        self.assertFalse(removed.is_active)

    def test_empty_member_list_never_mass_deactivates(self) -> None:
        realm = self._make_realm()
        user = self._make_mapped_user(realm, "safe@example.com", uuid.uuid4())

        WorkspaceSyncService()._deactivate_removed_members(realm, [])

        user.refresh_from_db()
        self.assertTrue(user.is_active)

    def test_unparseable_member_id_skips_reconciliation(self) -> None:
        realm = self._make_realm()
        user = self._make_mapped_user(realm, "guarded@example.com", uuid.uuid4())

        WorkspaceSyncService()._deactivate_removed_members(
            realm,
            [{"supabase_user_id": "not-a-uuid", "email": "x@example.com", "role": "editor"}],
        )

        user.refresh_from_db()
        self.assertTrue(user.is_active)

    def test_unmapped_profile_untouched(self) -> None:
        """A profile with no mapping row has unknown identity — never guessed."""
        from zerver.actions.create_user import do_create_user

        realm = self._make_realm()
        unmapped = do_create_user(
            email="unmapped@example.com",
            password=None,
            realm=realm,
            full_name="Unmapped",
            acting_user=None,
        )

        WorkspaceSyncService()._deactivate_removed_members(
            realm,
            [
                {
                    "supabase_user_id": str(uuid.uuid4()),
                    "email": "other@example.com",
                    "role": "editor",
                }
            ],
        )

        unmapped.refresh_from_db()
        self.assertTrue(unmapped.is_active)


class TestReAddedMemberReactivation(TestCase):
    """sync_user resurrects a deactivated profile for a re-added member."""

    def _fixture(self):
        from zerver.actions.create_realm import do_create_realm
        from zerver.actions.create_user import do_create_user
        from zerver.actions.users import do_deactivate_user

        ws_uuid = uuid.uuid4()
        realm = do_create_realm(
            string_id=str(ws_uuid)[:20].lower(),
            name="Readd Test",
            create_zulip_discussion_channel=False,
        )
        NodlRealmExtension.objects.create(
            zulip_realm=realm,
            nodl_workspace_id=ws_uuid,
            sync_status=SyncStatus.SYNCED,
        )
        user = do_create_user(
            email="readd@example.com",
            password=None,
            realm=realm,
            full_name="Re Added",
            acting_user=None,
        )
        do_deactivate_user(user, acting_user=None)
        return realm, ws_uuid, user

    def test_sync_user_reactivates_deactivated_profile(self) -> None:
        from nodl.sync.user_sync import UserSyncRequest, UserSyncService

        realm, ws_uuid, user = self._fixture()

        result = UserSyncService().sync_user(
            UserSyncRequest(
                supabase_user_id=str(uuid.uuid4()),
                email="readd@example.com",
                full_name="Re Added",
                avatar_url=None,
                workspace_id=str(ws_uuid),
                role="editor",
            )
        )

        self.assertTrue(result.success, result.error)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertEqual(result.zulip_user_id, user.id)
