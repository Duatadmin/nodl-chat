"""Guard tests for message edit/delete delegation (2026-08 edit/delete epic).

nodl-chat registers its own ``api/v1/messages/<int:message_id>`` URL *before*
Zulip's rest_path, shadowing it. The Flutter client edits and deletes via
PATCH/DELETE on that exact URL, so the nodl view layer MUST serve those verbs
by delegating to Zulip's own backend views. Delegation (rather than the old
hand-rolled logic) is load-bearing for four separate guarantees:

* DM edits work at all (the old code built a ``StreamMessageEditRequest`` from
  ``message.recipient.type_id`` and 404'd on every direct message);
* realm permission gates apply (``allow_message_editing``, the group-based
  delete permissions, and the edit/delete time windows);
* deletes stay SOFT — ``do_delete_messages`` archives into
  ``ArchivedMessage`` — and the archive transaction is protected from the
  retention vacuum;
* edit history is recorded and a content edit re-queues the message for
  nodl translation (the re-render wipes the appended translation block).

These are source guards in the ``test_media_access_contract.py`` pattern: the
fork has no local DB test env, so we assert the load-bearing shape of the
patched files. If a future upstream merge or refactor reverts any of them,
these fail loudly.
"""

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

MESSAGES_VIEWS_PY = REPO_ROOT / "nodl/api/views/messages.py"
NODL_URLS_PY = REPO_ROOT / "nodl/api/urls.py"
RETENTION_PY = REPO_ROOT / "zerver/lib/retention.py"
MESSAGE_EDIT_ACTIONS_PY = REPO_ROOT / "zerver/actions/message_edit.py"
WORKSPACE_SYNC_PY = REPO_ROOT / "nodl/sync/workspace_sync.py"


def _function_source(path: Path, name: str) -> str:
    """Source of a module-level function, via AST (immune to formatting)."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(path.read_text(), node) or ""
    raise AssertionError(f"function {name} not found in {path}")


class EditDelegationTest(unittest.TestCase):
    def test_edit_message_delegates_to_zulip_backend(self) -> None:
        src = _function_source(MESSAGES_VIEWS_PY, "edit_message")
        self.assertIn("update_message_backend", src)
        self.assertIn("process_as_post", src)

    def test_edit_message_has_no_hand_rolled_stream_edit_request(self) -> None:
        # The DM-edit bug lived in a hand-built StreamMessageEditRequest from
        # message.recipient.type_id; its return would break every DM edit.
        src = MESSAGES_VIEWS_PY.read_text()
        self.assertNotIn("StreamMessageEditRequest(", src)
        self.assertNotIn("get_stream_by_id_in_realm", src)

    def test_legacy_edit_path_keeps_message_payload_contract(self) -> None:
        # The web client reads data.message from PATCH .../edit responses.
        src = _function_source(MESSAGES_VIEWS_PY, "edit_message")
        self.assertIn('request.path.endswith("/edit")', src)
        self.assertIn('"message": serializer.model_dump()', src)


class DeleteDelegationTest(unittest.TestCase):
    def test_delete_message_delegates_to_zulip_backend(self) -> None:
        # delete_message_backend = permission groups + delete window +
        # row lock + soft delete via do_delete_messages.
        src = _function_source(MESSAGES_VIEWS_PY, "delete_message")
        self.assertIn("delete_message_backend", src)

    def test_no_hand_rolled_role_check_remains(self) -> None:
        src = _function_source(MESSAGES_VIEWS_PY, "delete_message")
        self.assertNotIn("ROLE_REALM_ADMINISTRATOR", src)


class RoutingTest(unittest.TestCase):
    def test_canonical_message_url_serves_all_three_verbs(self) -> None:
        src = NODL_URLS_PY.read_text()
        self.assertIn('path("api/v1/messages/<int:message_id>", message_detail_dispatch', src)

    def test_dispatch_covers_get_patch_delete(self) -> None:
        src = _function_source(MESSAGES_VIEWS_PY, "message_detail_dispatch")
        for verb, view in (
            ("GET", "get_message"),
            ("PATCH", "edit_message"),
            ("DELETE", "delete_message"),
        ):
            self.assertIn(f'"{verb}"', src)
            self.assertIn(view, src)

    def test_legacy_web_aliases_still_registered(self) -> None:
        src = NODL_URLS_PY.read_text()
        self.assertIn("<int:message_id>/edit", src)
        self.assertIn("<int:message_id>/delete", src)


class SoftDeleteProtectionTest(unittest.TestCase):
    def test_manual_archive_transactions_are_vacuum_protected(self) -> None:
        # User deletes archive under ArchiveTransaction(type=MANUAL); the
        # protect flag exempts them from clean_archived_data's 30-day vacuum,
        # keeping every user delete restorable forever.
        src = _function_source(RETENTION_PY, "run_archiving")
        self.assertIn("protect_from_deletion=(type == ArchiveTransaction.MANUAL)", src)


class TranslationRequeueTest(unittest.TestCase):
    def test_content_edit_requeues_nodl_derived_content(self) -> None:
        src = _function_source(MESSAGE_EDIT_ACTIONS_PY, "check_update_message")
        self.assertIn('"nodl_derived_content"', src)
        self.assertIn("translation_text", src)
        # Scope parity with the send-time block: humans + no attachments only.
        self.assertIn("is_bot", src)
        self.assertIn("potential_attachment_path_ids", src)


class RealmWindowDefaultsTest(unittest.TestCase):
    def test_whatsapp_model_windows(self) -> None:
        # Founder decision 2026-08-14: edit 15 min, delete-own 60 h.
        import re

        src = WORKSPACE_SYNC_PY.read_text()
        edit = re.search(r"REALM_MESSAGE_CONTENT_EDIT_LIMIT_SECONDS = (.+)", src)
        delete = re.search(r"REALM_MESSAGE_CONTENT_DELETE_LIMIT_SECONDS = (.+)", src)
        assert edit and delete
        self.assertEqual(eval(edit.group(1)), 900)  # noqa: S307
        self.assertEqual(eval(delete.group(1)), 216000)  # noqa: S307

    def test_create_realm_applies_both_windows(self) -> None:
        src = _function_source(WORKSPACE_SYNC_PY, "_create_realm")
        self.assertEqual(src.count("do_set_realm_property("), 2)
        self.assertIn('"message_content_edit_limit_seconds"', src)
        self.assertIn('"message_content_delete_limit_seconds"', src)


if __name__ == "__main__":
    unittest.main()
