"""Guard tests for the three nodl patches that make media sharing work.

nodl-chat is a fork of Zulip.  Media access in this fork depends on three
non-upstream commits.  If a future `git merge upstream/main` silently reverts
any of them, mobile and web media rendering breaks for every workspace at once
and nothing else in the tree notices.  These tests exist to make that failure
loud.

Guarded commits:

* ``ac14c76869`` — ``zerver/lib/attachments.py``:
  ``validate_attachment_request_for_spectator_access`` serves any attachment
  whose realm matches the requested realm to anonymous callers
  ("anyone with the link"), replacing upstream's web-public spectator gate.
* ``06737d414f`` — ``zerver/views/upload.py``: ``serve_file`` resolves an
  anonymous caller's realm from the URL path instead of the request subdomain
  (Railway serves every realm from one hostname).
* ``785a9f6f83`` — JWT/Basic processing on file paths:
  ``nodl/auth/middleware.py`` (``OPTIONAL_AUTH_PREFIXES`` and the optional-auth
  and Basic-auth branches) plus ``zerver/lib/rest.py`` (skip Zulip's auth
  decorators when the middleware already authenticated; strip an unparseable
  ``Authorization`` header and fall through to anonymous access).

Two layers, deliberately:

1. **Source guards** — parse the patched files and assert the load-bearing
   shape is still there.  Pure stdlib, so they run even where Django is not
   installed.  These are the anti-revert canaries.
2. **Behavioural guards** — call the patched functions for real.  They need the
   Django app registry (``django.setup()`` against ``zproject.test_settings``)
   but no database, no Redis and no dev environment, so they run under
   ``scripts/run-tests.sh``.  They skip — rather than error — where Django is
   unavailable; layer 1 still covers the revert case there.

End-to-end HTTP coverage (a real ``ZulipTestCase`` issuing
``GET /user_uploads/<realm_id>/<path>``) is *not* possible in this fork today:
``nodl.auth.middleware.SupabaseJWTMiddleware`` is unconditionally installed in
``zproject/computed_settings.py`` and answers 401 to any session-authenticated
request, which is how ``ZulipTestCase`` drives the client.  See the S4.1 report
for the follow-up needed to unblock the zerver suite.
"""

import ast
import base64
import os
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import jwt
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

ATTACHMENTS_PY = "zerver/lib/attachments.py"
UPLOAD_VIEWS_PY = "zerver/views/upload.py"
REST_PY = "zerver/lib/rest.py"
MIDDLEWARE_PY = "nodl/auth/middleware.py"

TEST_JWT_SECRET = "test-jwt-secret-for-media-access-contract"
TEST_EMAIL = "media-tester@example.com"
TEST_API_KEY = "0123456789abcdef0123456789abcdef"


def _stub_pyvips_if_unavailable() -> None:
    """Make ``import pyvips`` succeed without the libvips shared library.

    ``zerver/lib/thumbnail.py`` calls into libvips at import time, and almost
    everything in ``zerver`` reaches it transitively, so a workstation without
    the native library cannot import ``zerver.lib.rest`` at all.  None of the
    guards below touch image processing, so a stub that fails loudly on any
    real use is enough to get the auth and realm-resolution code loaded.  A
    provisioned Zulip environment has the real library and never sees this.
    """
    try:
        import pyvips  # noqa: F401
    except Exception:  # pragma: no cover - depends on the environment
        pass
    else:
        return

    class _Unavailable:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("libvips is not installed; image processing is unavailable here")

    def _noop(*args: object, **kwargs: object) -> None:
        return None

    module = types.ModuleType("pyvips")
    attributes: dict[str, object] = {
        # Classes, not mocks: zerver/lib/thumbnail.py uses these in annotations
        # that are evaluated at definition time.
        "Image": type("Image", (_Unavailable,), {}),
        "Source": type("Source", (_Unavailable,), {}),
        "Error": type("Error", (Exception,), {}),
        "operation_block_set": _noop,
        "block_untrusted_set": _noop,
        "voperation": SimpleNamespace(cache_set_max=_noop),
        "at_least_libvips": lambda *args, **kwargs: True,
        "version": lambda *args, **kwargs: 0,
    }
    for name, value in attributes.items():
        setattr(module, name, value)
    sys.modules["pyvips"] = module


def _bootstrap_django() -> str | None:
    """Configure the Django app registry.  Returns an error string, or None."""
    try:
        import django
    except ImportError as exc:  # pragma: no cover - depends on the environment
        return f"Django is not installed ({exc})"

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zproject.test_settings")
    try:
        django.setup()
    except Exception as exc:  # pragma: no cover - depends on the environment
        return f"django.setup() failed: {type(exc).__name__}: {exc}"
    _stub_pyvips_if_unavailable()
    return None


DJANGO_BOOTSTRAP_ERROR = _bootstrap_django()
requires_django = pytest.mark.skipif(
    DJANGO_BOOTSTRAP_ERROR is not None,
    reason=f"Django app registry unavailable: {DJANGO_BOOTSTRAP_ERROR}",
)


# ---------------------------------------------------------------------------
# Source-guard helpers (stdlib only — no Django required)
# ---------------------------------------------------------------------------


def _read_source(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _find_definition(source: str, qualname: str) -> ast.AST:
    """Look up a module-level ``func`` or ``Class.func`` in parsed source."""
    node: ast.AST = ast.parse(source)
    for part in qualname.split("."):
        for child in ast.iter_child_nodes(node):
            if (
                isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                and child.name == part
            ):
                node = child
                break
        else:
            raise AssertionError(f"{qualname!r} no longer exists — the patch it guards is gone")
    return node


def _definition_source(relative_path: str, qualname: str) -> str:
    source = _read_source(relative_path)
    segment = ast.get_source_segment(source, _find_definition(source, qualname))
    assert segment is not None
    return segment


def _class_attribute_literal(relative_path: str, qualname: str, attribute: str) -> object:
    class_node = _find_definition(_read_source(relative_path), qualname)
    for child in ast.iter_child_nodes(class_node):
        if isinstance(child, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == attribute for target in child.targets
        ):
            return ast.literal_eval(child.value)
    raise AssertionError(f"{qualname}.{attribute} no longer exists")


# ---------------------------------------------------------------------------
# Layer 1 — source guards
# ---------------------------------------------------------------------------


def test_source_guard_spectator_gate_serves_any_same_realm_attachment() -> None:
    """Guards ac14c76869 (zerver/lib/attachments.py).

    Upstream gates anonymous attachment access on ``attachment.is_web_public``.
    nodl replaced that with a realm-match check so that anyone holding the
    (crypto-token) URL can read the file.  If an upstream merge restores the
    web-public gate, every image and file in every nodl chat stops loading for
    the mobile app, which fetches media anonymously.
    """
    body = _definition_source(ATTACHMENTS_PY, "validate_attachment_request_for_spectator_access")

    assert "if attachment.realm != realm:" in body, (
        "the realm boundary — the only access check left after ac14c76869 — is gone"
    )
    assert "is_web_public" not in body, (
        "upstream's web_public spectator gate is back in "
        f"{ATTACHMENTS_PY}; ac14c76869 has been reverted and anonymous media "
        "reads (i.e. all mobile media) now fail realm-wide"
    )
    assert "enable_spectator_access" not in body, (
        f"upstream's realm spectator-access gate is back in {ATTACHMENTS_PY}; see ac14c76869"
    )


def test_source_guard_serve_file_resolves_anonymous_realm_from_path() -> None:
    """Guards 06737d414f (zerver/views/upload.py).

    Railway serves every realm from a single hostname, so upstream's
    subdomain-based realm lookup cannot work for anonymous callers.  If this
    reverts, anonymous ``GET /user_uploads/<realm_id>/...`` resolves the wrong
    realm (or none) and every media fetch 404s.
    """
    body = _definition_source(UPLOAD_VIEWS_PY, "serve_file")

    anonymous_branch = None
    for node in ast.walk(ast.parse(body)):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.dump(node.test)
        if "AnonymousUser" in test_src and "isinstance" in test_src:
            anonymous_branch = node
            break

    assert anonymous_branch is not None, (
        f"serve_file no longer branches on AnonymousUser in {UPLOAD_VIEWS_PY}; "
        "06737d414f has been reverted"
    )

    branch_src = "\n".join(ast.dump(stmt) for stmt in anonymous_branch.body)
    assert "realm_id_str" in branch_src and "Realm" in branch_src, (
        "the anonymous branch of serve_file no longer resolves the realm from "
        "the URL path (06737d414f); anonymous media fetches will 404 on the "
        "single-domain Railway deploy"
    )
    assert "get_valid_realm_from_request" not in branch_src, (
        "serve_file resolves an anonymous caller's realm from the request host "
        "again — that is exactly what 06737d414f removed"
    )


def test_source_guard_optional_auth_prefixes_cover_media_paths() -> None:
    """Guards 785a9f6f83 (nodl/auth/middleware.py).

    ``SupabaseJWTMiddleware`` answers 401 for every non-exempt path.  File
    paths are in OPTIONAL_AUTH_PREFIXES so credentials are used when present
    but never required.  Drop an entry and that media surface 401s for
    unauthenticated browsers even though the view layer would have served it.
    """
    prefixes = _class_attribute_literal(
        MIDDLEWARE_PY, "SupabaseJWTMiddleware", "OPTIONAL_AUTH_PREFIXES"
    )

    assert isinstance(prefixes, tuple)
    assert "/user_uploads" in prefixes, (
        "/user_uploads left OPTIONAL_AUTH_PREFIXES; the middleware will now 401 "
        "anonymous file fetches before the view layer sees them (785a9f6f83)"
    )
    assert "/thumbnail" in prefixes, (
        "/thumbnail left OPTIONAL_AUTH_PREFIXES; inline image thumbnails will "
        "401 for anonymous callers (785a9f6f83)"
    )


def test_source_guard_rest_dispatch_keeps_the_nodl_auth_branches() -> None:
    """Guards 785a9f6f83 (zerver/lib/rest.py).

    Two hunks: (a) when the middleware already authenticated, skip Zulip's auth
    decorators — they call ``validate_account_and_subdomain``, which rejects
    every request on Railway's single domain; (b) when an ``Authorization``
    header is present but Zulip's Basic parser rejects it, strip it and fall
    through to anonymous instead of erroring.  Lose (a) and mobile uploads
    break; lose (b) and any client sending a Bearer token gets an error page
    where a file should be.
    """
    body = _definition_source(REST_PY, "rest_dispatch")

    assert 'hasattr(request, "supabase_user_id")' in body, (
        "rest_dispatch no longer detects middleware-authenticated requests; "
        "785a9f6f83 has been reverted and every Basic-auth upload will fail "
        "validate_account_and_subdomain on the single-domain deploy"
    )
    assert "_nodl_upload_wrapper" in body, (
        "the override_api_url_scheme bypass for middleware-authenticated "
        "callers is gone (785a9f6f83)"
    )
    assert "_jwt_wrapper" in body, (
        "the /api JWT/Basic bypass wrapper is gone (785a9f6f83); uploads from "
        "the mobile app will 401"
    )
    assert 'request.META.pop("HTTP_AUTHORIZATION", None)' in body, (
        "rest_dispatch no longer strips an unparseable Authorization header "
        "before falling through to anonymous access (785a9f6f83); clients "
        "sending Bearer tokens on file paths will get errors instead of files"
    )


# ---------------------------------------------------------------------------
# Layer 2 — behavioural guards (Django app registry, no database)
# ---------------------------------------------------------------------------


def _fake_realm(realm_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=realm_id, string_id=f"realm{realm_id}", deactivated=False)


def _fake_attachment(realm: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        realm=realm,
        path_id=f"{realm.id}/ab/cd-EFgh_ijKLmn/site-plan.pdf",
        # Upstream's gate reads this field; nodl's does not.  False here means a
        # reverted gate denies access and the test fails loudly.
        is_web_public=False,
    )


def _fake_user_profile(user_id: int = 42, realm_id: int = 7) -> SimpleNamespace:
    realm = _fake_realm(realm_id)
    return SimpleNamespace(
        id=user_id,
        delivery_email=TEST_EMAIL,
        email=TEST_EMAIL,
        is_active=True,
        is_authenticated=True,
        realm=realm,
        realm_id=realm_id,
    )


def _bearer_token(email: str = TEST_EMAIL, secret: str = TEST_JWT_SECRET) -> str:
    payload = {
        "sub": "supabase-user-uuid",
        "email": email,
        "role": "authenticated",
        "aud": "authenticated",
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def _basic_header(email: str = TEST_EMAIL, api_key: str = TEST_API_KEY) -> str:
    return "Basic " + base64.b64encode(f"{email}:{api_key}".encode()).decode()


@requires_django
def test_anonymous_same_realm_attachment_is_authorized() -> None:
    """S4.1 #1 — guards ac14c76869.

    The anonymous read path for an attachment in the requested realm must
    authorize, *without* the attachment being web-public.  If this fails after
    an upstream merge, mobile media rendering is broken realm-wide.
    """
    from django.test import override_settings

    from zerver.lib.attachments import validate_attachment_request_for_spectator_access

    realm = _fake_realm(7)
    attachment = _fake_attachment(realm)

    with override_settings(RATE_LIMITING=False):
        assert validate_attachment_request_for_spectator_access(realm, attachment) is True


@requires_django
def test_anonymous_cross_realm_attachment_is_denied() -> None:
    """S4.1 #2 — guards ac14c76869.

    The realm match is the one boundary the anyone-with-the-link model still
    enforces.  If this fails, an attachment URL from one workspace is readable
    by pointing it at another workspace's realm id — a cross-tenant leak.

    (``serve_file`` turns this False into a 302 to /login for anonymous
    callers, or a 403 image when the client sends ``Accept: image/png``; the
    HTTP shape is upstream's, the decision under test is nodl's.)
    """
    from django.test import override_settings

    from zerver.lib.attachments import validate_attachment_request_for_spectator_access

    requested_realm = _fake_realm(7)
    attachment = _fake_attachment(_fake_realm(8))

    with override_settings(RATE_LIMITING=False):
        assert (
            validate_attachment_request_for_spectator_access(requested_realm, attachment) is False
        )


@requires_django
def test_serve_file_resolves_anonymous_realm_from_url_path() -> None:
    """S4.1 #1 (mechanism) — guards 06737d414f.

    An anonymous ``GET /user_uploads/<realm_id>/<path>`` must resolve the realm
    from ``<realm_id>``, never from the request host.  If this fails, every
    anonymous media fetch on the single-domain Railway deploy 404s.
    """
    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory

    from zerver.views import upload as upload_views

    realm = _fake_realm(7)
    stop = RuntimeError("stop after realm resolution")
    request = RequestFactory().get("/user_uploads/7/ab/cd-EFgh_ijKLmn/site-plan.pdf")

    with (
        mock.patch.object(upload_views, "Realm") as realm_model,
        mock.patch.object(upload_views, "get_valid_realm_from_request") as from_host,
        mock.patch.object(
            upload_views, "validate_attachment_request", side_effect=stop
        ) as validate,
    ):
        realm_model.objects.get.return_value = realm
        with pytest.raises(RuntimeError):
            upload_views.serve_file(
                request, AnonymousUser(), "7", "ab/cd-EFgh_ijKLmn/site-plan.pdf"
            )

    realm_model.objects.get.assert_called_once_with(id=7)
    from_host.assert_not_called()
    assert validate.call_args.args[2] is realm


@requires_django
def test_optional_auth_matches_media_paths_and_nothing_else() -> None:
    """Guards 785a9f6f83 — the optional-auth predicate itself.

    ``/user_uploads`` and ``/thumbnail`` must never be blocked by the
    middleware; ``/api/v1/user_uploads`` (the upload POST) must stay on the
    mandatory-auth path so uploads keep authenticating.
    """
    from nodl.auth.middleware import SupabaseJWTMiddleware

    middleware = SupabaseJWTMiddleware(lambda request: None)

    assert middleware._is_optional_auth("/user_uploads/7/ab/cd/site-plan.pdf")
    assert middleware._is_optional_auth("/user_uploads")
    assert middleware._is_optional_auth("/thumbnail/7/ab/cd/photo.jpg/840x560.webp")
    assert not middleware._is_optional_auth("/api/v1/user_uploads")
    assert not middleware._is_optional_auth("/user_uploadsomething")


@requires_django
def test_bearer_jwt_is_accepted_on_file_paths() -> None:
    """S4.1 #4 — guards 785a9f6f83 (middleware half).

    A valid Supabase JWT on a file path authenticates the caller and the
    request continues to the view.  If this fails, the mobile app's
    authenticated media fetches 401 instead of being served.
    """
    from django.http import HttpResponse
    from django.test import RequestFactory, override_settings

    from nodl.auth.middleware import SupabaseJWTMiddleware

    user_profile = _fake_user_profile()
    downstream = mock.Mock(return_value=HttpResponse(status=200))
    middleware = SupabaseJWTMiddleware(downstream)
    request = RequestFactory().get(
        "/user_uploads/7/ab/cd-EFgh_ijKLmn/photo.jpg",
        headers={"authorization": f"Bearer {_bearer_token()}"},
    )

    with (
        override_settings(SUPABASE_JWT_SECRET=TEST_JWT_SECRET),
        mock.patch.object(
            SupabaseJWTMiddleware, "_get_user_profile_cached", return_value=user_profile
        ),
    ):
        response = middleware(request)

    assert response.status_code == 200
    downstream.assert_called_once_with(request)
    assert request.supabase_user_id == "supabase-user-uuid"
    assert request.user_profile is user_profile


@requires_django
def test_malformed_authorization_header_falls_through_to_anonymous() -> None:
    """S4.1 #5 — guards 785a9f6f83 (middleware half).

    Garbage credentials on a file path must degrade to anonymous access, not
    401.  If this fails, one stale token on one client turns every media fetch
    into an error page.
    """
    from django.http import HttpResponse
    from django.test import RequestFactory, override_settings

    from nodl.auth.middleware import SupabaseJWTMiddleware

    for header in ("Bearer garbage", "NotAScheme x", "Basic !!!not-base64!!!"):
        downstream = mock.Mock(return_value=HttpResponse(status=200))
        middleware = SupabaseJWTMiddleware(downstream)
        request = RequestFactory().get(
            "/user_uploads/7/ab/cd-EFgh_ijKLmn/photo.jpg",
            headers={"authorization": header},
        )

        with override_settings(SUPABASE_JWT_SECRET=TEST_JWT_SECRET):
            response = middleware(request)

        assert response.status_code == 200, f"{header!r} was rejected instead of ignored"
        downstream.assert_called_once_with(request)
        assert not hasattr(request, "supabase_user_id")


@requires_django
def test_basic_auth_upload_marks_the_request_middleware_authenticated() -> None:
    """S4.1 #3 (middleware half) — guards 785a9f6f83.

    ``POST /api/v1/user_uploads`` with Zulip Basic auth (email:api_key) must
    set ``supabase_user_id``; that flag is what makes ``rest_dispatch`` skip
    ``validate_account_and_subdomain``.  If this fails, mobile uploads 401 on
    the single-domain Railway deploy.  See nodl-mobile M0.4a, which keeps the
    upload path on Basic auth precisely because of this.
    """
    from django.http import HttpResponse
    from django.test import RequestFactory

    from nodl.auth.middleware import SupabaseJWTMiddleware

    user_profile = _fake_user_profile()
    downstream = mock.Mock(return_value=HttpResponse(status=200))
    middleware = SupabaseJWTMiddleware(downstream)
    request = RequestFactory().post(
        "/api/v1/user_uploads", headers={"authorization": _basic_header()}
    )

    with mock.patch(
        "zerver.models.users.get_user_profile_by_api_key", return_value=user_profile
    ) as by_api_key:
        response = middleware(request)

    by_api_key.assert_called_once_with(TEST_API_KEY)
    assert response.status_code == 200
    assert request.supabase_user_id == f"api_key:{user_profile.id}"
    assert request.user_profile is user_profile
    assert request._dont_enforce_csrf_checks is True


@requires_django
def test_rest_dispatch_skips_subdomain_validation_for_basic_auth_upload() -> None:
    """S4.1 #3 — guards 785a9f6f83 (``zerver/lib/rest.py`` half).

    The upload POST, already authenticated by the middleware, must reach the
    view without ``validate_account_and_subdomain`` running.  If this fails,
    every mobile upload dies with "Account is not associated with this
    subdomain" on the single-domain Railway deploy.
    """
    from django.http import HttpResponse
    from django.test import RequestFactory

    import zerver.decorator
    from zerver.lib.rest import rest_dispatch

    user_profile = _fake_user_profile()
    request = RequestFactory().post(
        "/api/v1/user_uploads", headers={"authorization": _basic_header()}
    )
    request.user = user_profile
    request.user_profile = user_profile
    request.supabase_user_id = f"api_key:{user_profile.id}"

    view = mock.Mock(return_value=HttpResponse(status=200))
    view.__name__ = "upload_file_backend"

    with (
        mock.patch.object(zerver.decorator, "validate_account_and_subdomain") as subdomain_check,
        mock.patch("zerver.lib.rest.process_client"),
    ):
        response = rest_dispatch(request, POST=view)

    assert response.status_code == 200
    subdomain_check.assert_not_called()
    assert view.call_args.args[1] is user_profile


@requires_django
def test_rest_dispatch_serves_files_to_middleware_authenticated_callers() -> None:
    """S4.1 #4 — guards 785a9f6f83 (``zerver/lib/rest.py`` half).

    A JWT-authenticated ``GET /user_uploads/...`` (an ``override_api_url_scheme``
    view) reaches ``serve_file_backend`` with the resolved user profile and
    without Zulip's subdomain validation.  If this fails, authenticated media
    fetches break even while anonymous ones still work.
    """
    from django.http import HttpResponse
    from django.test import RequestFactory

    import zerver.decorator
    from zerver.lib.rest import rest_dispatch

    user_profile = _fake_user_profile()
    request = RequestFactory().get(
        "/user_uploads/7/ab/cd-EFgh_ijKLmn/photo.jpg",
        headers={"authorization": f"Bearer {_bearer_token()}"},
    )
    request.user = user_profile
    request.user_profile = user_profile
    request.supabase_user_id = "supabase-user-uuid"

    view = mock.Mock(return_value=HttpResponse(status=200))
    view.__name__ = "serve_file_backend"

    with (
        mock.patch.object(zerver.decorator, "validate_account_and_subdomain") as subdomain_check,
        mock.patch("zerver.lib.rest.process_client"),
    ):
        response = rest_dispatch(
            request,
            GET=(view, {"override_api_url_scheme", "allow_anonymous_user_web"}),
        )

    assert response.status_code == 200
    subdomain_check.assert_not_called()
    assert view.call_args.args[1] is user_profile


@requires_django
def test_rest_dispatch_strips_unparseable_authorization_and_serves_anonymously() -> None:
    """S4.1 #5 — guards 785a9f6f83 (``zerver/lib/rest.py`` half).

    An ``Authorization`` header Zulip's Basic parser rejects must be stripped so
    the request falls through to the anonymous (spectator) path and the file is
    still served.  If this fails, any client with a stale or wrong-scheme token
    gets an error instead of the file.
    """
    from django.contrib.auth.models import AnonymousUser
    from django.http import HttpResponse
    from django.test import RequestFactory

    from zerver.lib.rest import rest_dispatch

    for header in ("Bearer garbage", "NotAScheme x"):
        request = RequestFactory().get(
            "/user_uploads/7/ab/cd-EFgh_ijKLmn/photo.jpg",
            headers={"authorization": header},
        )
        request.user = AnonymousUser()

        view = mock.Mock(return_value=HttpResponse(status=200))
        view.__name__ = "serve_file_backend"

        with mock.patch("zerver.decorator.process_client"):
            response = rest_dispatch(
                request,
                GET=(view, {"override_api_url_scheme", "allow_anonymous_user_web"}),
            )

        assert response.status_code == 200, f"{header!r} was not stripped"
        assert "HTTP_AUTHORIZATION" not in request.META
        assert isinstance(view.call_args.args[1], AnonymousUser)
