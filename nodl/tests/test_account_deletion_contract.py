"""Guard tests for chat-side account deletion (App Store 5.1.1(v), 2026-08).

nodl-backend's account-deletion purge task calls
``POST /api/v1/internal/users/delete``; the chat side must hard-delete every
Zulip profile mapped to the Supabase user. Load-bearing choices these tests
pin down:

* The service uses ``do_delete_user_preserving_messages`` — NOT
  ``do_delete_user`` — because the approved policy is "destroy personal data,
  preserve shared workspace history": plain ``do_delete_user`` CASCADE-deletes
  every message the user ever sent, silently destroying stream history for
  the whole team.
* Physical upload bytes are purged from storage before the profile deletion
  (Zulip only destroys the Attachment rows, orphaning the R2 blobs).
* The endpoint is service-key gated (``require_service_auth``) and routed.

Source guards in the ``test_media_access_contract.py`` pattern: the fork has
no local DB test env, so we assert the load-bearing shape of the files. The
DB-backed behavior test lives in ``nodl/sync/tests/test_user_deletion.py``
(CI only).
"""

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

USER_DELETION_PY = REPO_ROOT / "nodl/sync/user_deletion.py"
INTERNAL_VIEWS_PY = REPO_ROOT / "nodl/api/views/internal.py"
NODL_URLS_PY = REPO_ROOT / "nodl/api/urls.py"


def _function_source(path: Path, name: str) -> str:
    """Source of a module-level function, via AST (immune to formatting)."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(path.read_text(), node) or ""
    raise AssertionError(f"{name} not found in {path}")


def _decorators(path: Path, name: str) -> list[str]:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return [ast.unparse(d) for d in node.decorator_list]
    raise AssertionError(f"{name} not found in {path}")


class AccountDeletionContractTest(unittest.TestCase):
    def test_route_is_registered(self) -> None:
        source = NODL_URLS_PY.read_text()
        self.assertIn('"api/v1/internal/users/delete"', source)
        self.assertIn("delete_user", source)

    def test_view_is_service_key_gated(self) -> None:
        decorators = _decorators(INTERNAL_VIEWS_PY, "delete_user")
        self.assertIn("require_service_auth", decorators)
        self.assertIn("csrf_exempt", decorators)

    def test_deletion_preserves_shared_messages(self) -> None:
        source = USER_DELETION_PY.read_text()
        self.assertIn("do_delete_user_preserving_messages", source)
        # Guard against a future "simplification" to the message-destroying
        # variant: the plain name must never be imported.
        tree = ast.parse(source)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        self.assertNotIn("do_delete_user", imported)

    def test_deletion_purges_physical_uploads_first(self) -> None:
        source = USER_DELETION_PY.read_text()
        self.assertIn("delete_message_attachments", source)
        # Purge must be invoked before the profile deletion.
        purge_pos = source.index("self._purge_uploaded_files(profile)")
        delete_pos = source.index("do_delete_user_preserving_messages(profile)")
        self.assertLess(purge_pos, delete_pos)

    def test_idempotent_lookup_by_supabase_id(self) -> None:
        source = USER_DELETION_PY.read_text()
        self.assertIn("NodlRealmUserExtension.objects.filter", source)
        self.assertIn("supabase_user_id=supabase_user_id", source)


if __name__ == "__main__":
    unittest.main()
