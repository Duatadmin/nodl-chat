import os

################################################################
# Mandatory settings - read from environment variables
################################################################

EXTERNAL_HOST = os.environ.get("EXTERNAL_HOST", "localhost")
ZULIP_ADMINISTRATOR = os.environ.get("ZULIP_ADMINISTRATOR", "admin@localhost")

# Allow Railway's domain and any custom domains
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("ALLOWED_HOSTS", "").split(",")
    if host.strip()
]

# CSRF trusted origins for cross-origin requests (Django 4.0+)
# Must include full scheme (https://) - Railway domain and frontend domain
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

################################################################
# Static Files
################################################################

# NOTE: STATICFILES_STORAGE is deprecated in Django 4.2+.
# The STORAGES dict in computed_settings.py is used instead.
# The fix for staticfiles manifest errors is in computed_settings.py:658-668

################################################################
# Authentication - Supabase JWT only (no email/password)
################################################################

# NODL MODIFICATION START - Use Supabase auth backend
# Reason: Replace Zulip's email/password auth with Supabase JWT
# Date: 2024-12-01
# See: architecture/chat-architecture.md, Story 1.2
AUTHENTICATION_BACKENDS: tuple[str, ...] = (
    "nodl.auth.backends.SupabaseAuthBackend",
)
# NODL MODIFICATION END

################################################################
# Cloudflare R2 Storage (S3-compatible)
################################################################

# NODL MODIFICATION START - Use Cloudflare R2 for file uploads
# Reason: Railway deployments need external storage for persistence
# Date: 2024-12-01
S3_AUTH_UPLOADS_BUCKET = os.environ.get("S3_AUTH_UPLOADS_BUCKET", "")
S3_AVATAR_BUCKET = os.environ.get("S3_AVATAR_BUCKET", "")
S3_REGION = os.environ.get("S3_REGION", "auto")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL")

# R2-specific settings
S3_SKIP_CHECKSUM = True  # R2 has limited checksum support
S3_ADDRESSING_STYLE = "path"  # R2 prefers path-style URLs

# Override Zulip's secret-based credentials with env vars
# (Zulip normally uses get_secret() but we use Railway env vars)
S3_KEY = os.environ.get("S3_KEY")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY")
# NODL MODIFICATION END

################################################################
# Mobile push notifications (native APNs + FCM, no bouncer)
################################################################

# NODL MODIFICATION START - Native mobile push notifications
# Reason: Zulip's message-push senders read credentials from FILE paths;
# entrypoint.sh materializes those files from the same Railway env vars the
# nodl call-push service uses (APNS_AUTH_KEY_B64, FIREBASE_CREDENTIALS_JSON).
# NOTE: upstream defines APNS_TOKEN_KEY_ID / APNS_TEAM_ID via
# get_secret(..., development_only=True), which ALWAYS resolves to None in
# production — secrets-file entries silently do nothing, so the literal
# overrides below are the only working configuration path.
# Date: 2026-08-18
_apns_token_key_file = "/etc/zulip/apns_auth_key.p8"
if os.path.isfile(_apns_token_key_file) and os.environ.get("APNS_KEY_ID"):
    APNS_TOKEN_KEY_FILE = _apns_token_key_file
    APNS_TOKEN_KEY_ID = os.environ.get("APNS_KEY_ID")
    APNS_TEAM_ID = os.environ.get("APNS_TEAM_ID")
    # false => production APNs. The device fleet is mixed (dev-signed builds
    # hold sandbox tokens, TestFlight builds production tokens); the
    # env-mismatch retry in zerver/lib/push_notifications.py covers the
    # other environment.
    APNS_SANDBOX = os.environ.get("APNS_USE_SANDBOX", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )

_fcm_credentials_file = "/etc/zulip/firebase-credentials.json"
if os.path.isfile(_fcm_credentials_file):
    ANDROID_FCM_CREDENTIALS_PATH = _fcm_credentials_file
# NODL MODIFICATION END
