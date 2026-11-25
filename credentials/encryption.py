from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.views.decorators.debug import sensitive_variables


@sensitive_variables("key")
def credential_cipher():
    key = settings.VIBEARR_CREDENTIAL_KEY
    if not key:
        raise ImproperlyConfigured(
            "Configure VIBEARR_CREDENTIAL_KEY to store or use credentials."
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise ImproperlyConfigured(
            "VIBEARR_CREDENTIAL_KEY must be a valid Fernet key."
        ) from None
