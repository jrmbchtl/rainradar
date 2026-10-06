from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import Any

import aiohttp
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import aiohttp_client, config_entry_oauth2_flow, selector
import voluptuous as vol

from .const import (
    ADVANCED_SECTION,
    CONF_DEVICE_TRACKER,
    CONF_DEVICE_TRACKERS,
    CONF_ENABLE_AIR_QUALITY,
    CONF_ENABLE_CAMS_UV,
    CONF_ENABLE_FORECAST,
    CONF_ENABLE_ICON_EU,
    CONF_ENABLE_PKG_PROBABILITY,
    CONF_ENABLE_PKG_SOLAR,
    CONF_ENABLE_PKG_WIND,
    CONF_ENABLE_UV,
    CONF_ENABLE_WARNINGS,
    CONF_ENABLE_WEATHERNEXT,
    CONF_ENABLE_WN_OVERLAY,
    CONF_LOCATIONS,
    CONF_SCAN_INTERVAL,
    CONF_WN_GCP_PROJECT_ID,
    CONF_ZONES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    normalize_entity_list,
)
from .credentials import async_get_credentials, async_save_credentials

ENABLE_TOGGLE_KEYS = (
    CONF_ENABLE_FORECAST,
    CONF_ENABLE_ICON_EU,
    CONF_ENABLE_UV,
    CONF_ENABLE_WARNINGS,
    CONF_ENABLE_AIR_QUALITY,
)

WN_TOGGLE_KEYS = (
    CONF_ENABLE_WEATHERNEXT,
    CONF_ENABLE_WN_OVERLAY,
    CONF_ENABLE_PKG_SOLAR,
    CONF_ENABLE_PKG_WIND,
    CONF_ENABLE_PKG_PROBABILITY,
)

# Secrets live in the credentials store, never in entry options.
SECRET_KEYS = ("cams_api_token",)

CONF_WN_SIGN_IN = "wn_sign_in_google"

_LOGGER = logging.getLogger(__name__)


async def _fetch_account_email(
    session: aiohttp.ClientSession, token: dict[str, Any]
) -> str | None:
    """Look up the signed-in Google account address, for the status field.

    Purely cosmetic: a failure here must never block the sign-in.
    """
    access_token = token.get("access_token")
    if not access_token:
        return None
    try:
        async with asyncio.timeout(15):
            async with session.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError, ValueError):
        return None
    email = data.get("email")
    return email if isinstance(email, str) else None


def _advanced_schema(
    wn_toggles: dict[str, bool],
    has_wn_credentials: bool,
    wn_status: str,
    enable_cams_uv: bool,
    has_cams_token: bool,
) -> vol.Schema:
    """Schema for the collapsed Advanced/Experimental section."""
    cams_note = "configured" if has_cams_token else "not_configured"
    return vol.Schema(
        {
            vol.Required(
                CONF_ENABLE_WEATHERNEXT,
                default=wn_toggles.get(CONF_ENABLE_WEATHERNEXT, False),
            ): selector.BooleanSelector(),
            vol.Optional(CONF_WN_SIGN_IN, default=False): selector.BooleanSelector(),
            vol.Required(
                CONF_ENABLE_WN_OVERLAY,
                default=wn_toggles.get(CONF_ENABLE_WN_OVERLAY, True),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_ENABLE_PKG_SOLAR,
                default=wn_toggles.get(CONF_ENABLE_PKG_SOLAR, False),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_ENABLE_PKG_WIND,
                default=wn_toggles.get(CONF_ENABLE_PKG_WIND, False),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_ENABLE_PKG_PROBABILITY,
                default=wn_toggles.get(CONF_ENABLE_PKG_PROBABILITY, False),
            ): selector.BooleanSelector(),
            vol.Required(CONF_ENABLE_CAMS_UV, default=enable_cams_uv): selector.BooleanSelector(),
            vol.Optional("cams_api_token"): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Optional(
                "wn_credentials_status", default=wn_status
            ): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.TEXT, read_only=True
                )
            ),
            vol.Optional(
                "cams_credentials_status", default=cams_note
            ): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.TEXT, read_only=True
                )
            ),
        }
    )


def _extract_advanced(user_input: dict[str, Any]) -> dict[str, Any]:
    """Pull the advanced section payload out of the (possibly nested) input."""
    advanced = user_input.get(ADVANCED_SECTION, {})
    if not isinstance(advanced, dict):
        advanced = {}
    # HA delivers sections as nested dicts; some clients flatten with "__".
    prefix = f"{ADVANCED_SECTION}__"
    for key, value in user_input.items():
        if isinstance(key, str) and key.startswith(prefix):
            advanced[key[len(prefix):]] = value
    return advanced


def _build_schema(
    zones: list[str],
    device_trackers: list[str],
    scan_interval: int,
    toggles: dict[str, bool],
    advanced_schema: vol.Schema | None,
) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Optional(CONF_ZONES, default=zones): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="zone", multiple=True)
        ),
        vol.Optional(
            CONF_DEVICE_TRACKERS, default=device_trackers
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="device_tracker", multiple=True)
        ),
        vol.Optional(
            CONF_SCAN_INTERVAL, default=scan_interval
        ): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
        vol.Optional(
            CONF_ENABLE_FORECAST, default=toggles.get(CONF_ENABLE_FORECAST, True)
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_ENABLE_ICON_EU, default=toggles.get(CONF_ENABLE_ICON_EU, True)
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_ENABLE_UV, default=toggles.get(CONF_ENABLE_UV, True)
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_ENABLE_WARNINGS, default=toggles.get(CONF_ENABLE_WARNINGS, True)
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_ENABLE_AIR_QUALITY,
            default=toggles.get(CONF_ENABLE_AIR_QUALITY, True),
        ): selector.BooleanSelector(),
    }
    if advanced_schema is not None:
        # Must be a data_entry_flow.section — the frontend serializer only
        # knows how to render `section` objects as expandable groups; a plain
        # nested vol.Schema raises "unable to serialize schema" in the
        # flow-manager response.
        fields[
            vol.Optional(ADVANCED_SECTION)
        ] = section(advanced_schema, {"collapsed": True})
    return vol.Schema(fields)


def _result_options(user_input: dict[str, Any], current_locations: list) -> dict[str, Any]:
    zone_entities = normalize_entity_list(user_input.get(CONF_ZONES))
    device_trackers = normalize_entity_list(user_input.get(CONF_DEVICE_TRACKERS))
    return {
        CONF_LOCATIONS: current_locations,
        CONF_ZONES: zone_entities,
        CONF_DEVICE_TRACKERS: device_trackers,
        # Legacy single-tracker mirror kept for older card versions.
        CONF_DEVICE_TRACKER: device_trackers[0] if device_trackers else None,
        CONF_SCAN_INTERVAL: user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        **{key: bool(user_input.get(key, True)) for key in ENABLE_TOGGLE_KEYS},
    }


def _wn_defaults(options: dict[str, Any]) -> dict[str, Any]:
    """Carry the WeatherNext toggles into the entry options.

    ``wn_gcp_project_id`` is preserved for entries written by older versions so
    they round-trip unchanged, but it is no longer offered in the form: it only
    ever mattered for Requester-Pays buckets, which we deliberately never read.
    """
    defaults = {key: bool(options.get(key, False)) for key in WN_TOGGLE_KEYS} | {
        CONF_ENABLE_WN_OVERLAY: bool(options.get(CONF_ENABLE_WN_OVERLAY, True))
    }
    if CONF_WN_GCP_PROJECT_ID in options:
        defaults[CONF_WN_GCP_PROJECT_ID] = options[CONF_WN_GCP_PROJECT_ID]
    return defaults


class _SecretsPreflightMixin:
    """Shared secret-storage + preflight logic for both flow types."""

    hass: Any

    def _credential_key(self) -> str:
        """Return the credentials-store key for this flow.

        During the user flow there is no entry yet, so credentials are keyed
        under the flow id and re-keyed to the entry id by
        ``async_copy_flow_credentials`` (called from ``__init__.py`` setup).
        A reauth flow already knows its entry, so it writes straight there.
        """
        entry_key = getattr(self, "config_entry", None)
        if entry_key is not None and getattr(entry_key, "entry_id", None):
            return entry_key.entry_id
        if entry_id := (self.context or {}).get("entry_id"):
            return entry_id
        return self.flow_id

    async def _handle_secrets_and_preflight(
        self, advanced: dict[str, Any], options: dict[str, Any]
    ) -> dict[str, str]:
        """Store secrets, run preflights; return flow errors keyed by field."""
        from . import cams as cams_mod, weathernext as wn, wnauth

        errors: dict[str, str] = {}
        entry_id = self._credential_key()
        creds = await async_get_credentials(self.hass, entry_id)

        if options.get(CONF_ENABLE_WEATHERNEXT) and not wnauth.has_credentials(creds):
            errors[CONF_WN_SIGN_IN] = "sign_in_required"
        elif options.get(CONF_ENABLE_WEATHERNEXT):
            session = aiohttp_client.async_get_clientsession(self.hass)
            error = await wn.preflight(session, creds)
            if error:
                errors[CONF_WN_SIGN_IN] = error

        cams_token = advanced.get("cams_api_token")
        if cams_token:
            creds["cams_api_token"] = cams_token.strip()
        if options.get(CONF_ENABLE_CAMS_UV) and not creds.get("cams_api_token"):
            errors["cams_api_token"] = "cams_token_required"
        if options.get(CONF_ENABLE_CAMS_UV) and "cams_api_token" not in errors:
            error = await cams_mod.preflight(
                creds.get("cams_api_token", ""),
                aiohttp_client.async_get_clientsession(self.hass),
            )
            if error:
                errors["cams_api_token"] = error

        if not any(k in errors for k in SECRET_KEYS):
            await async_save_credentials(self.hass, entry_id, creds)
        return errors


class RainradarConfigFlow(
    _SecretsPreflightMixin, config_entries.ConfigFlow, domain=DOMAIN
):
    """Handle the rainradar config flow."""

    VERSION = 4

    def _get_default_zones(self) -> list[str]:
        if self.hass.states.get("zone.home") is not None:
            return ["zone.home"]
        return []

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        from . import wnauth

        if user_input is not None:
            advanced = _extract_advanced(user_input)
            options = _result_options(user_input, current_locations=[])
            options.update(_wn_defaults(advanced))
            options[CONF_ENABLE_CAMS_UV] = bool(
                advanced.get(CONF_ENABLE_CAMS_UV, False)
            )

            # "Sign in with Google" is checked and we have no credential yet:
            # stash the form input and hand over to the OAuth external step.
            creds = await async_get_credentials(self.hass, self._credential_key())
            if advanced.get(CONF_WN_SIGN_IN) and not wnauth.has_credentials(creds):
                self._pending_user_input = user_input
                return await self.async_step_auth()

            errors = await self._handle_secrets_and_preflight(advanced, options)
            if errors:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_build_schema(
                        zones=normalize_entity_list(user_input.get(CONF_ZONES)),
                        device_trackers=normalize_entity_list(
                            user_input.get(CONF_DEVICE_TRACKERS)
                        ),
                        scan_interval=user_input.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                        toggles=options,
                        advanced_schema=_advanced_schema(
                            options,
                            wnauth.has_credentials(creds),
                            wnauth.describe(creds),
                            options[CONF_ENABLE_CAMS_UV],
                            bool(creds.get("cams_api_token")),
                        ),
                    ),
                    errors=errors,
                )
            return self.async_create_entry(title="Rainradar", data={}, options=options)

        creds = await async_get_credentials(self.hass, self._credential_key())
        return self.async_show_form(
            step_id="user",
            data_schema=_build_schema(
                zones=self._get_default_zones(),
                device_trackers=[],
                scan_interval=DEFAULT_SCAN_INTERVAL,
                toggles={},
                advanced_schema=_advanced_schema(
                    {},
                    wnauth.has_credentials(creds),
                    wnauth.describe(creds),
                    False,
                    bool(creds.get("cams_api_token")),
                ),
            ),
        )

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Run Google's consent screen, then return to the form."""

        if user_input is not None:
            # Resumed by /auth/external/callback.
            if "error" in user_input:
                return self.async_abort(reason="authorize_rejected")
            self.external_data = user_input
            return self.async_external_step_done(next_step_id="creation")

        try:
            implementations = await config_entry_oauth2_flow.async_get_implementations(
                self.hass, DOMAIN
            )
        except config_entry_oauth2_flow.ImplementationUnavailableError:
            return self.async_abort(reason="missing_google_credentials")

        if not implementations:
            return self.async_abort(reason="missing_google_credentials")

        impl = next(iter(implementations.values()))
        try:
            url = await impl.async_generate_authorize_url(self.flow_id)
        except Exception:
            _LOGGER.exception("Failed to build the Google authorize URL")
            return self.async_abort(reason="authorize_url_failed")

        self._flow_impl = impl
        return self.async_external_step(step_id="auth", url=url)

    async def async_step_creation(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Exchange the auth code for tokens and store them privately."""

        impl = getattr(self, "_flow_impl", None)
        if impl is None:
            return self.async_abort(reason="authorize_url_failed")

        try:
            token = await impl.async_resolve_external_data(self.external_data)
        except Exception as exc:
            _LOGGER.warning("WeatherNext OAuth token exchange failed: %s", exc)
            return self.async_abort(reason="token_exchange_failed")

        refresh_token = token.get("refresh_token")
        if not refresh_token:
            # Google only issues a refresh token on the first consent; ask again
            # rather than storing a credential that dies in an hour.
            return self.async_abort(reason="no_refresh_token")

        client_id = getattr(impl, "client_id", "")
        account = await _fetch_account_email(
            aiohttp_client.async_get_clientsession(self.hass), token
        )

        creds = await async_get_credentials(self.hass, self._credential_key())
        creds["wn_google_token"] = {
            "refresh_token": refresh_token,
            "access_token": token.get("access_token"),
            "client_id": client_id,
            "client_secret": getattr(impl, "client_secret", ""),
            "token_uri": getattr(impl, "token_url", ""),
            "scope": token.get("scope", ""),
        }
        if account:
            creds["wn_account_email"] = account
        # A legacy service-account key would otherwise keep taking precedence.
        creds.pop("wn_service_account_info", None)
        await async_save_credentials(self.hass, self._credential_key(), creds)

        _LOGGER.info("Rainradar: WeatherNext 3 signed in as %s", account or "unknown")

        # A reauth flow must update the existing entry, never create a second
        # one. Branch on `source` — HA passes the entry data to async_step_reauth
        # as an argument (flow.init_data), it is NOT in flow.context.
        if self.source == config_entries.SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates={}
            )

        # Otherwise the user already submitted their settings before signing in,
        # so replay that submission. The sign-in flag is now a no-op because a
        # credential exists, which keeps this from looping back into OAuth.
        pending = getattr(self, "_pending_user_input", None) or {}
        pending.setdefault(CONF_ZONES, self._get_default_zones())
        pending.setdefault(CONF_DEVICE_TRACKERS, [])
        pending.setdefault(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        for key in ENABLE_TOGGLE_KEYS:
            pending.setdefault(key, True)
        return await self.async_step_user(pending)

    async def async_step_reauth(
        self, entry_data: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Re-run the Google sign-in for an existing entry.

        Reached either from the "Attention required" card (token failure) or
        chained from the options dialog via ``next_flow``. The entry data arrives
        as an argument — HA keeps it on ``flow.init_data``, never in the flow
        context — but we only need the entry id, which ``_get_reauth_entry()``
        reads from the context, so the data is not retained.
        """
        del entry_data
        return await self.async_step_auth()

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return RainradarOptionsFlow()


class RainradarOptionsFlow(_SecretsPreflightMixin, config_entries.OptionsFlow):
    """Handle the rainradar options flow.

    Uses the built-in ``self.config_entry`` property — do not store the
    entry on the instance (that pattern is phased out in HA).
    """

    @callback
    def async_abort(
        self,
        *,
        reason: str,
        description_placeholders: Mapping[str, str] | None = None,
        translation_domain: str | None = None,
        next_flow: tuple[config_entries.FlowType, str] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Abort the options flow, optionally chaining into another flow.

        Upstream only accepts ``next_flow`` on ``ConfigFlow.async_abort``, but the
        frontend honours it on an *abort* result from any flow type (it opens
        ``showConfigFlowDialog({continueFlowId})``), and the flow-result
        serializer copies extra keys through verbatim. OptionsFlow is the only
        route to "sign in again" that does not make the user leave for the
        Devices & Services page to click an "Attention required" card.
        """
        result = super().async_abort(
            reason=reason,
            description_placeholders=description_placeholders,
            translation_domain=translation_domain,
        )
        if next_flow is not None:
            result["next_flow"] = next_flow
        return result

    async def _async_start_wn_signin(
        self, options: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Persist options, then chain to a reauth flow that runs the sign-in.

        ``next_flow`` on an abort result makes the frontend open the chained
        config-flow dialog straight away, which is what renders the external
        step and triggers the Google consent window.
        """
        entry = self.config_entry
        self.hass.config_entries.async_update_entry(entry, options=options)

        result = await self.hass.config_entries.flow.async_init(
            DOMAIN,
            context=config_entries.ConfigFlowContext(
                source=config_entries.SOURCE_REAUTH,
                entry_id=entry.entry_id,
                title_placeholders={"name": entry.title},
                unique_id=entry.unique_id,
            ),
            data=entry.data,
        )
        return self.async_abort(
            reason="reauth_started", next_flow=("config_flow", result["flow_id"])
        )

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        from . import wnauth

        entry = self.config_entry
        current_locations = entry.options.get(CONF_LOCATIONS, [])

        zones = normalize_entity_list(entry.options.get(CONF_ZONES))
        if not zones and self.hass.states.get("zone.home") is not None:
            zones = ["zone.home"]

        device_trackers = normalize_entity_list(entry.options.get(CONF_DEVICE_TRACKERS))
        if not device_trackers:
            device_trackers = normalize_entity_list(entry.options.get(CONF_DEVICE_TRACKER))

        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        toggles = {key: entry.options.get(key, True) for key in ENABLE_TOGGLE_KEYS}

        if user_input is not None:
            advanced = _extract_advanced(user_input)
            options = _result_options(user_input, current_locations=current_locations)
            options.update(_wn_defaults(advanced))
            options[CONF_ENABLE_CAMS_UV] = bool(advanced.get(CONF_ENABLE_CAMS_UV, False))

            # The OAuth external step cannot run in the options flow: HA's
            # /auth/external/callback resumes the *config* flow manager only,
            # and the frontend does not send the HA-Frontend-Base header on
            # options-flow API calls, so async_get_redirect_uri() would raise.
            # Start a reauth *config* flow ourselves so we hold its flow_id, and
            # chain to it with next_flow — the frontend opens that dialog
            # immediately. entry.async_start_reauth() cannot be used here: it
            # returns no flow_id (so no chaining), opens nothing, and files a
            # repair issue that is wrong for a deliberate sign-in.
            if advanced.get(CONF_WN_SIGN_IN):
                return await self._async_start_wn_signin(options)

            errors = await self._handle_secrets_and_preflight(advanced, options)
            if errors:
                creds = await async_get_credentials(self.hass, entry.entry_id)
                return self.async_show_form(
                    step_id="init",
                    data_schema=_build_schema(
                        zones=normalize_entity_list(user_input.get(CONF_ZONES)),
                        device_trackers=normalize_entity_list(
                            user_input.get(CONF_DEVICE_TRACKERS)
                        ),
                        scan_interval=user_input.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                        toggles=options,
                        advanced_schema=_advanced_schema(
                            options,
                            wnauth.has_credentials(creds),
                            wnauth.describe(creds),
                            options[CONF_ENABLE_CAMS_UV],
                            bool(creds.get("cams_api_token")),
                        ),
                    ),
                    errors=errors,
                )
            return self.async_create_entry(title="", data=options)

        creds = await async_get_credentials(self.hass, entry.entry_id)
        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(
                zones=zones,
                device_trackers=device_trackers,
                scan_interval=scan_interval,
                toggles=toggles,
                advanced_schema=_advanced_schema(
                    _wn_defaults(entry.options),
                    wnauth.has_credentials(creds),
                    wnauth.describe(creds),
                    entry.options.get(CONF_ENABLE_CAMS_UV, False),
                    bool(creds.get("cams_api_token")),
                ),
            ),
        )
