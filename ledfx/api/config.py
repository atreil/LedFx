import logging
from json import JSONDecodeError

import voluptuous as vol
from aiohttp import web

from ledfx.api import RestEndpoint
from ledfx.api.utils import PERMITTED_KEYS
from ledfx.config import (
    CORE_CONFIG_KEYS_NO_RESTART,
    CORE_CONFIG_SCHEMA,
    WLED_CONFIG_SCHEMA,
    create_backup,
    migrate_config,
    parse_version,
    save_config,
)
from ledfx.consts import CONFIGURATION_VERSION
from ledfx.effects.audio import AudioAnalysisSource, AudioInputSource
from ledfx.effects.melbank import Melbanks
from ledfx.events import BaseConfigUpdateEvent

_LOGGER = logging.getLogger(__name__)

CORE_CONFIG_KEYS = set(map(str, CORE_CONFIG_SCHEMA.schema.keys()))


def validate_and_trim_config(config, schema, node):
    for key in config.keys():
        if key not in PERMITTED_KEYS[node] and key != "user_presets":
            raise KeyError(f"Unknown/forbidden {node} config key: '{key}'")

    validated_config = schema(config)
    return {key: validated_config[key] for key in config.keys()}


class ConfigEndpoint(RestEndpoint):
    ENDPOINT_PATH = "/api/config"

    async def get(self, request: web.Request) -> web.Response:
        """
        Get complete ledfx config.
        You may ask for a specific key/keys in the request body
        eg. "audio" will return audio config
        eg. ["audio", "melbanks"] will return audio and melbanks config

        Parameters:
        - request (web.Request): The request object.

        Returns:
        - web.Response: The response object containing the ledfx config.
        """
        keys = set()

        if request.can_read_body:
            try:
                wanted_keys = await request.json()
            except JSONDecodeError:
                return await self.json_decode_error()

            if isinstance(wanted_keys, list):
                keys.update(wanted_keys)
            elif isinstance(wanted_keys, str):
                keys.add(wanted_keys)

            keys = keys & CORE_CONFIG_KEYS

        # if no keys left after filtering, or none requested, send them all
        if not keys:
            keys = CORE_CONFIG_KEYS

        response = {}
        for key in keys:
            config = self._ledfx.config.get(key)

            if key == "audio":
                config = AudioInputSource.AUDIO_CONFIG_SCHEMA.fget()(config)
            elif key == "melbanks":
                config = Melbanks.CONFIG_SCHEMA(config)
            elif key == "wled_preferences":
                config = WLED_CONFIG_SCHEMA(config)

            response[key] = config
        return await self.bare_request_success(response)

    async def delete(self) -> web.Response:
        """
        Resets config to defaults and restarts ledfx

        Returns:
            web.Response: The response indicating the success of the operation.
        """
        create_backup(self._ledfx.config_dir, "DELETE")
        self._ledfx.config = CORE_CONFIG_SCHEMA({})

        save_config(
            config=self._ledfx.config,
            config_dir=self._ledfx.config_dir,
        )

        self._ledfx.loop.call_soon_threadsafe(self._ledfx.stop, 4)
        return await self.request_success(
            "success", "Config reset to default values, LedFx restarting."
        )

    async def post(self, request: web.Request) -> web.Response:
        """
        Loads a complete config and restarts ledfx

        Parameters:
        - request (web.Request): The request containing the config to load.

        Returns:
        - web.Response: The HTTP response object

        """
        try:
            config = await request.json()

            try:
                assert parse_version(
                    config["configuration_version"]
                ) == parse_version(CONFIGURATION_VERSION)
            except (KeyError, AssertionError):
                _LOGGER.warning(
                    f"LedFx config version: {CONFIGURATION_VERSION}, import config version: {config.get('configuration_version', 'UNDEFINED (old!)')}"
                )
                try:
                    config = migrate_config(config)
                except Exception as e:
                    msg = f"Failed to migrate import config to the new standard: {e}"
                    _LOGGER.exception(msg)
                    return await self.internal_error(msg, "error")

            # if we got this far, we are happy with and committing to the import config
            # so backup the old one
            create_backup(self._ledfx.config_dir, "IMPORT")

            audio_config = AudioInputSource.AUDIO_CONFIG_SCHEMA.fget()(
                config.pop("audio", {})
            )
            wled_config = WLED_CONFIG_SCHEMA(
                config.pop("wled_preferences", {})
            )
            melbanks_config = Melbanks.CONFIG_SCHEMA(
                config.pop("melbanks", {})
            )
            core_config = CORE_CONFIG_SCHEMA(config)

            core_config["audio"] = audio_config
            core_config["wled_preferences"] = wled_config
            core_config["melbanks"] = melbanks_config

            self._ledfx.config = core_config

            save_config(
                config=self._ledfx.config,
                config_dir=self._ledfx.config_dir,
            )

            self._ledfx.loop.call_soon_threadsafe(self._ledfx.stop, 4)
            return await self.request_success()

        except JSONDecodeError:
            return await self.json_decode_error()

        except vol.MultipleInvalid as msg:
            error_message = f"Error loading config: {msg}"
            _LOGGER.warning(error_message)
            return await self.internal_error(error_message, "error")

    async def put(self, request: web.Request) -> web.Response:
        """
        Updates ledfx config

        Parameters:
            request (web.Request): The request containing the config to update.

        Returns:
            web.Response: The HTTP response object
        """

        try:
            config = await request.json()
        except JSONDecodeError:
            return await self.json_decode_error()
        except Exception as e:
            return await self.generic_error(str(e))

        try:
            self.update_config(config)
            self._ledfx.events.fire_event(BaseConfigUpdateEvent(config))
            save_config(
                config=self._ledfx.config, config_dir=self._ledfx.config_dir
            )
            need_restart = self.check_need_restart(config)
            if need_restart:
                # Ugly - return success to frontend before restarting
                try:
                    return await self.request_success(
                        type="success",
                        message="LedFx is restarting to apply the new configuration",
                    )
                finally:
                    self._ledfx.loop.call_soon_threadsafe(self._ledfx.stop, 4)
            return await self.request_success(
                type="success", message="Configuration Updated"
            )
        except (KeyError, vol.MultipleInvalid) as msg:
            error_message = f"Error updating config: {msg}"
            _LOGGER.warning(error_message)
            return await self.invalid_request(error_message)
        except Exception as e:
            return await self.internal_error(str(e))

    def update_config(self, config):
        """
        Updates the configuration of the LedFx instance with the provided config.

        Args:
            config (dict): The new configuration to be applied.

        Returns:
            None
        """
        audio_config = config.pop("audio", {})

        audio_config = validate_and_trim_config(
            audio_config,
            AudioInputSource.AUDIO_CONFIG_SCHEMA.fget(),
            "audio",
        )

        audio_config = validate_and_trim_config(
            audio_config,
            AudioAnalysisSource.CONFIG_SCHEMA,
            "audio",
        )
        wled_config = validate_and_trim_config(
            config.pop("wled_preferences", {}),
            WLED_CONFIG_SCHEMA,
            "wled_preferences",
        )
        melbanks_config = validate_and_trim_config(
            config.pop("melbanks", {}), Melbanks.CONFIG_SCHEMA, "melbanks"
        )
        core_config = validate_and_trim_config(
            config, CORE_CONFIG_SCHEMA, "core"
        )

        self._ledfx.config["audio"].update(audio_config)
        self._ledfx.config["melbanks"].update(melbanks_config)
        self._ledfx.config.update(core_config)

        # handle special case wled_preferences nested dict
        for key in wled_config:
            if key in self._ledfx.config["wled_preferences"]:
                self._ledfx.config["wled_preferences"][key].update(
                    wled_config[key]
                )
            else:
                self._ledfx.config["wled_preferences"][key] = wled_config[key]

        if (
            hasattr(self._ledfx, "audio")
            and self._ledfx.audio is not None
            and audio_config
        ):
            self._ledfx.audio.update_config(self._ledfx.config["audio"])

        if hasattr(self._ledfx, "audio") and melbanks_config:
            self._ledfx.audio.melbanks.update_config(
                self._ledfx.config["melbanks"]
            )

        if core_config:
            self._ledfx.events.fire_event(BaseConfigUpdateEvent(config))

    def check_need_restart(self, config):
        """
        Checks if a restart is needed based on the provided configuration.

        Args:
            config (dict): The configuration to be checked.

        Returns:
            bool: True if a restart is needed, False otherwise.
        """
        core_config = validate_and_trim_config(
            config, CORE_CONFIG_SCHEMA, "core"
        )

        # If core_config is empty, no restart is needed
        if not core_config:
            return False

        need_restart = True
        if any(key in core_config for key in CORE_CONFIG_KEYS_NO_RESTART):
            need_restart = False

        return need_restart
