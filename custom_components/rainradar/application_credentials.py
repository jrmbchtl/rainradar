"""Application credentials platform for the WeatherNext 3 Google sign-in.

Registers Google's OAuth2 endpoints with Home Assistant's Application
Credentials store, so the user supplies their own OAuth client id / secret
instead of the integration shipping one. The client is a *Web application*
client whose authorized redirect URI must be this instance's
``/auth/external/callback`` (see the README).
"""

from __future__ import annotations

from homeassistant.components.application_credentials import (
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow


async def async_get_authorization_server(
    hass: HomeAssistant,
) -> AuthorizationServer:
    """Return Google's OAuth2 endpoints."""
    return AuthorizationServer(
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
    )


async def async_get_auth_implementation(
    hass: HomeAssistant,
    auth_domain: str,
    credential: ClientCredential,
) -> config_entry_oauth2_flow.AbstractOAuth2Implementation:
    """Return a sign-in implementation for one stored client credential."""
    return WeatherNextGoogleOAuthImplementation(
        hass,
        auth_domain,
        credential,
        await async_get_authorization_server(hass),
    )


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    """Return the description placeholders for the credentials dialog."""
    return {
        "oauth_consent_url": (
            "https://console.cloud.google.com/apis/credentials/consent"
        ),
        "oauth_creds_url": "https://console.cloud.google.com/apis/credentials",
        "more_info_url": "https://github.com/jorim/rainradar",
    }


class WeatherNextGoogleOAuthImplementation(
    config_entry_oauth2_flow.LocalOAuth2Implementation
):
    """Google sign-in scoped to read-only Cloud Storage.

    Two deviations from the stock implementation:

    - ``access_type=offline`` + ``prompt=consent`` so Google issues a refresh
      token. Without these the integration would need a new sign-in every hour.
    - The scope is the read-only storage scope. The WeatherNext statistics
      bucket has Requester Pays **off**, so no ``x-goog-user-project`` billing
      header is needed and a plain user token is sufficient.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        auth_domain: str,
        credential: ClientCredential,
        authorization_server: AuthorizationServer,
    ) -> None:
        """Initialize the Google sign-in implementation."""
        super().__init__(
            hass,
            auth_domain,
            credential.client_id,
            credential.client_secret,
            authorization_server.authorize_url,
            authorization_server.token_url,
        )

    @property
    def name(self) -> str:
        """Return the display name of this credential."""
        return "Google (WeatherNext 3)"

    @property
    def extra_authorize_data(self) -> dict:
        """Return extra query parameters for the authorize URL."""
        from .const import WEATHERNEXT_OAUTH_SCOPE

        return {
            "scope": WEATHERNEXT_OAUTH_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
