# nodl fork: user-facing rebranding of system-generated message content.

from django.conf import settings


def debrand(text: str) -> str:
    """Swap the upstream brand name for Nodle in user-visible bot copy.

    Applied AFTER translation (gettext), so every locale is covered:
    translations keep the Latin brand name "Zulip" verbatim, which makes a
    plain string replacement safe without forking the .po catalogs.

    Only apply this to system-generated copy (Welcome Bot, Notification
    Bot, onboarding channel names/content) — never to user-authored
    content.

    Gated on NODL_REBRAND_SYSTEM_MESSAGES (True in production, False in
    the test settings) so the upstream test suite, which asserts the
    upstream copy verbatim, keeps passing.
    """
    if not settings.NODL_REBRAND_SYSTEM_MESSAGES:
        return text
    return text.replace("Zulip", "Nodle")
