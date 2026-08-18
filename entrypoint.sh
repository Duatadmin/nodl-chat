#!/bin/bash
set -e

echo "=== entrypoint.sh starting ==="
echo "SECRET_KEY length: ${#SECRET_KEY}"

# Validate SECRET_KEY is set
if [ -z "$SECRET_KEY" ]; then
    echo "ERROR: SECRET_KEY environment variable is not set!"
    echo "Available environment variables:"
    env | grep -v SECRET | head -20
    exit 1
fi

# Create Zulip config directory
mkdir -p /etc/zulip

# Create Zulip config file (required for PRODUCTION mode)
# Without this, Zulip runs in DEVELOPMENT mode with DEBUG=True
cat > /etc/zulip/zulip.conf << EOF
[machine]
deploy_type = production
EOF

# Write secrets file from environment variables
# Zulip expects an INI-style file with [secrets] section
# Include nodl-specific secrets for JWT authentication
cat > /etc/zulip/zulip-secrets.conf << EOF
[secrets]
secret_key = ${SECRET_KEY}
rabbitmq_password = ${RABBITMQ_PASSWORD}
supabase_jwt_secret = ${SUPABASE_JWT_SECRET}
chat_service_key = ${CHAT_SERVICE_KEY}
EOF

# Set permissions
chmod 640 /etc/zulip/zulip-secrets.conf
chown zulip:zulip /etc/zulip/zulip-secrets.conf

echo "=== Secrets file created ==="
echo "File contents (first 2 lines):"
head -2 /etc/zulip/zulip-secrets.conf

# Materialize mobile-push credential files for Zulip's native senders.
# zerver/lib/push_notifications.py reads file paths (APNS_TOKEN_KEY_FILE,
# ANDROID_FCM_CREDENTIALS_PATH); Railway delivers env vars. These are the
# same env vars the nodl call-push service already uses.
if [ -n "$APNS_AUTH_KEY_B64" ]; then
    if echo "$APNS_AUTH_KEY_B64" | base64 -d > /etc/zulip/apns_auth_key.p8 2>/dev/null; then
        chown zulip:zulip /etc/zulip/apns_auth_key.p8
        chmod 640 /etc/zulip/apns_auth_key.p8
        echo "=== APNs auth key written to /etc/zulip/apns_auth_key.p8 ==="
    else
        rm -f /etc/zulip/apns_auth_key.p8
        echo "WARNING: APNS_AUTH_KEY_B64 set but base64 decode failed; native APNs push disabled"
    fi
fi
if [ -n "$FIREBASE_CREDENTIALS_JSON" ]; then
    # Same parse strategies as call_push_service._parse_firebase_json
    # (raw JSON / base64 / newline-mangled), normalized via json.dump.
    if /app/.venv/bin/python - <<'PYEOF'
import base64, json, os, sys

raw = os.environ["FIREBASE_CREDENTIALS_JSON"]
stripped = raw.strip().strip('"').strip("'")
parsed = None
try:
    parsed = json.loads(stripped)
except ValueError:
    pass
if parsed is None:
    try:
        parsed = json.loads(base64.b64decode(stripped).decode("utf-8"))
    except Exception:
        pass
if parsed is None:
    try:
        parsed = json.loads(stripped.replace("\r", "").replace("\n", "\\n"))
    except ValueError:
        pass
if parsed is None:
    print("Cannot parse FIREBASE_CREDENTIALS_JSON (tried raw, base64, newline-fix)", file=sys.stderr)
    sys.exit(1)
with open("/etc/zulip/firebase-credentials.json", "w") as f:
    json.dump(parsed, f)
PYEOF
    then
        chown zulip:zulip /etc/zulip/firebase-credentials.json
        chmod 640 /etc/zulip/firebase-credentials.json
        echo "=== Firebase credentials written to /etc/zulip/firebase-credentials.json ==="
    else
        rm -f /etc/zulip/firebase-credentials.json
        echo "WARNING: FIREBASE_CREDENTIALS_JSON set but unparseable; native FCM push disabled"
    fi
fi

# Create zulip schema if it doesn't exist (required before migrations)
# Zulip's setup scripts normally do this, but Railway has a fresh PostgreSQL
# Using Python/psycopg2 since psql client is not installed in the container
echo "=== Creating zulip schema ==="
/app/.venv/bin/python -c "
import os, psycopg2
conn = psycopg2.connect(
    host=os.environ['POSTGRES_HOST'],
    port=os.environ.get('POSTGRES_PORT', '5432'),
    user=os.environ['POSTGRES_USER'],
    password=os.environ['POSTGRES_PASSWORD'],
    dbname=os.environ['POSTGRES_DB']
)
conn.autocommit = True
cur = conn.cursor()
cur.execute('CREATE SCHEMA IF NOT EXISTS zulip')
cur.close()
conn.close()
print('zulip schema created/verified')
"

# Run database migrations (must run as zulip user, not root)
echo "=== Running database migrations ==="
cd /app
su zulip -c '/app/.venv/bin/python manage.py migrate --noinput'

# Wait for RabbitMQ to be reachable (up to 30 seconds)
echo "=== Checking RabbitMQ connectivity ==="
RABBITMQ_HOST_CHECK="${RABBITMQ_HOST:-127.0.0.1}"
RABBITMQ_PORT_CHECK="${RABBITMQ_PORT:-5672}"
MAX_RETRIES=15
RETRY_COUNT=0
while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if /app/.venv/bin/python -c "
import socket, sys
host = '$RABBITMQ_HOST_CHECK'
port = $RABBITMQ_PORT_CHECK
try:
    s = socket.create_connection((host, port), timeout=2)
    s.close()
    sys.exit(0)
except Exception as e:
    print(f'  connect failed: {e}', file=sys.stderr)
    sys.exit(1)
"; then
        echo "RabbitMQ is reachable at ${RABBITMQ_HOST_CHECK}:${RABBITMQ_PORT_CHECK}"
        break
    fi
    RETRY_COUNT=$((RETRY_COUNT + 1))
    echo "Waiting for RabbitMQ at ${RABBITMQ_HOST_CHECK}:${RABBITMQ_PORT_CHECK}... (attempt ${RETRY_COUNT}/${MAX_RETRIES})"
    sleep 2
done

if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
    echo "WARNING: RabbitMQ not reachable after ${MAX_RETRIES} attempts. Tornado event queues will fail."
    echo "Check RABBITMQ_HOST environment variable and RabbitMQ service status."
fi

echo "=== Starting supervisord ==="

# Start supervisord
exec supervisord -n -c /etc/supervisor/conf.d/supervisord.conf
