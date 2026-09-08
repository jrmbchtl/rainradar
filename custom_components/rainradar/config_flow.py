from __future__ import annotations

import json
from typing import Any

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import aiohttp_client, selector
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
SECRET_KEYS = ("wn_service_account_json", "cams_api_token")


def _advanced_schema(
    wn_toggles: dict[str, bool],
    wn_project_id: str,
    has_wn_credentials: bool,
    enable_cams_uv: bool,
    has_cams_token: bool,
) -> vol.Schema:
    """Schema for the collapsed Advanced/Experimental section."""
    del wn_project_id  # kept for signature compatibility with defaults below
    wn_note = "configured" if has_wn_credentials else "not_configured"
    cams_note = "configured" if has_cams_token else "not_configured"
    return vol.Schema(
        {
            vol.Required(
                CONF_ENABLE_WEATHERNEXT,
                default=wn_toggles.get(CONF_ENABLE_WEATHERNEXT, False),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_WN_GCP_PROJECT_ID,
                default=wn_toggles.get(CONF_WN_GCP_PROJECT_ID, ""),
            ): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
            ),
            vol.Optional("wn_service_account_json"): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
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
                "wn_credentials_status", default=wn_note
            ): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)),
            vol.Optional(
                "cams_credentials_status", default=cams_note
            ): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)),
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
    return {key: bool(options.get(key, False)) for key in WN_TOGGLE_KEYS} | {
        CONF_ENABLE_WN_OVERLAY: bool(options.get(CONF_ENABLE_WN_OVERLAY, True)),
        CONF_WN_GCP_PROJECT_ID: options.get(CONF_WN_GCP_PROJECT_ID, ""),
    }


class _SecretsPreflightMixin:
    """Shared secret-storage + preflight logic for both flow types."""

    hass: Any

    async def _handle_secrets_and_preflight(
        self, advanced: dict[str, Any], options: dict[str, Any]
    ) -> dict[str, str]:
        """Store secrets, run preflights; return flow errors keyed by field.

        During the user flow there is no entry yet, so credentials are keyed
        under the flow id and re-keyed to the entry id by
        ``async_copy_flow_credentials`` (called from ``__init__.py`` setup).
        """
        from . import cams as cams_mod, weathernext as wn

        errors: dict[str, str] = {}
        entry_key = getattr(self, "config_entry", None)
        entry_id = (
            entry_key.entry_id
            if entry_key is not None and getattr(entry_key, "entry_id", None)
            else self.flow_id
        )
        creds = await async_get_credentials(self.hass, entry_id)

        sa_json = advanced.get("wn_service_account_json")
        if sa_json:
            try:
                parsed = json.loads(sa_json)
                if not isinstance(parsed, dict) or "client_email" not in parsed:
                    errors["wn_service_account_json"] = "invalid_service_account_json"
                else:
                    creds["wn_service_account_info"] = parsed
                    creds["wn_gcp_project_id"] = advanced.get(
                        CONF_WN_GCP_PROJECT_ID, ""
                    )
            except (ValueError, TypeError):
                errors["wn_service_account_json"] = "invalid_service_account_json"
        elif options.get(CONF_ENABLE_WEATHERNEXT) and not creds.get(
            "wn_service_account_info"
        ):
            errors["wn_service_account_json"] = "service_account_required"

        if options.get(CONF_ENABLE_WEATHERNEXT) and (
            "wn_service_account_json" not in errors
        ):
            session = aiohttp_client.async_get_clientsession(self.hass)
            error = await wn.preflight(session, creds.get("wn_service_account_info", {}))
            if error:
                errors["wn_service_account_json"] = error

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

    VERSION = 3

    def _get_default_zones(self) -> list[str]:
        if self.hass.states.get("zone.home") is not None:
            return ["zone.home"]
        return []

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            advanced = _extract_advanced(user_input)
            options = _result_options(user_input, current_locations=[])
            options.update(_wn_defaults(advanced))
            options[CONF_ENABLE_CAMS_UV] = bool(advanced.get(CONF_ENABLE_CAMS_UV, False))
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
                            "",
                            False,
                            options[CONF_ENABLE_CAMS_UV],
                            False,
                        ),
                    ),
                    errors=errors,
                )
            return self.async_create_entry(title="Rainradar", data={}, options=options)

        return self.async_show_form(
            step_id="user",
            data_schema=_build_schema(
                zones=self._get_default_zones(),
                device_trackers=[],
                scan_interval=DEFAULT_SCAN_INTERVAL,
                toggles={},
                advanced_schema=_advanced_schema({}, "", False, False, False),
            ),
        )

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

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
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
                            "",
                            bool(creds.get("wn_service_account_info")),
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
                    "",
                    bool(creds.get("wn_service_account_info")),
                    entry.options.get(CONF_ENABLE_CAMS_UV, False),
                    bool(creds.get("cams_api_token")),
                ),
            ),
        )
