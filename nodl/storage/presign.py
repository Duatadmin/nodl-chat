"""Presigned R2/S3 GET URLs for message attachments.

The client-visible ``/user_uploads/`` path is auth-proxied, so nodl-backend
(and the services it hands work to, e.g. AssemblyAI) cannot fetch it. Every
service-side reader instead gets a short-lived presigned URL minted here:
the voice-note derive worker (zerver/worker/nodl_derived_content.py) and the
task-stream attachments read (nodl/api/views/assistant.py). Callers pass
their own TTL and must not call this on local-uploads deployments
(``settings.LOCAL_UPLOADS_DIR`` set), where there is no bucket to sign for.
"""

from django.conf import settings


def presign_attachment(path_id: str, expires_in: int) -> str:
    """Mint a presigned GET URL for an R2/S3-stored attachment."""
    from zerver.lib.upload.s3 import get_boto_client

    return get_boto_client().generate_presigned_url(
        ClientMethod="get_object",
        Params={
            "Bucket": settings.S3_AUTH_UPLOADS_BUCKET,
            "Key": path_id,
        },
        ExpiresIn=expires_in,
        HttpMethod="GET",
    )
