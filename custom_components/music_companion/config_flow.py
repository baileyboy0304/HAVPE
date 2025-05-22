import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from .const import (
    DOMAIN, 
    CONF_ACRCLOUD_HOST,
    CONF_HOME_ASSISTANT_UDP_PORT,
    CONF_ACRCLOUD_ACCESS_KEY,
    CONF_ACRCLOUD_ACCESS_SECRET,
    CONF_SPOTIFY_CLIENT_ID,
    CONF_SPOTIFY_CLIENT_SECRET,
    CONF_SPOTIFY_PLAYLIST_ID,
    CONF_SPOTIFY_CREATE_PLAYLIST,
    CONF_SPOTIFY_PLAYLIST_NAME,
    CONF_DEVICE_NAME,
    CONF_MEDIA_PLAYER_ENTITY,
    ENTRY_TYPE_MASTER,
    ENTRY_TYPE_DEVICE,
    DEFAULT_SPOTIFY_PLAYLIST_NAME
)

class MusicCompanionConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    CONNECTION_CLASS = "local_push"

    def __init__(self):
        """Initialize the config flow."""
        self._master_config_exists = False

    def _check_master_config(self):
        """Check if master configuration already exists."""
        if not self.hass:
            return
            
        for entry in self._async_current_entries():
            if entry.data.get("entry_type") == ENTRY_TYPE_MASTER:
                self._master_config_exists = True
                break

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        self._check_master_config()
        
        if not self._master_config_exists:
            return await self.async_step_master_config()
        else:
            return await self.async_step_menu()

    async def async_step_menu(self, user_input=None):
        """Show menu for choosing setup type."""
        if user_input is not None:
            if user_input["setup_type"] == "master":
                return await self.async_step_master_config()
            elif user_input["setup_type"] == "device":
                return await self.async_step_device()

        self._check_master_config()
        
        return self.async_show_menu(
            step_id="menu",
            menu_options={
                "device": "Add Device",
                "master": "Update Master Configuration" if self._master_config_exists else "Setup Master Configuration"
            }
        )

    async def async_step_master_config(self, user_input=None):
        """Configure master settings."""
        errors = {}
        
        if user_input is not None:
            existing_master = None
            for entry in self._async_current_entries():
                if entry.data.get("entry_type") == ENTRY_TYPE_MASTER:
                    existing_master = entry
                    break
            
            data = {
                **user_input,
                "entry_type": ENTRY_TYPE_MASTER
            }
            
            if existing_master:
                self.hass.config_entries.async_update_entry(existing_master, data=data)
                return self.async_abort(reason="master_updated")
            else:
                return self.async_create_entry(title="Master Configuration", data=data)

        # Get existing values if updating
        existing_data = {}
        for entry in self._async_current_entries():
            if entry.data.get("entry_type") == ENTRY_TYPE_MASTER:
                existing_data = entry.data
                break

        data_schema = vol.Schema({
            vol.Required(CONF_ACRCLOUD_HOST, default=existing_data.get(CONF_ACRCLOUD_HOST, "")): cv.string,
            vol.Required(CONF_HOME_ASSISTANT_UDP_PORT, default=existing_data.get(CONF_HOME_ASSISTANT_UDP_PORT, 6056)): cv.port,
            vol.Required(CONF_ACRCLOUD_ACCESS_KEY, default=existing_data.get(CONF_ACRCLOUD_ACCESS_KEY, "")): cv.string,
            vol.Required(CONF_ACRCLOUD_ACCESS_SECRET, default=existing_data.get(CONF_ACRCLOUD_ACCESS_SECRET, "")): cv.string,
            vol.Required(CONF_SPOTIFY_CLIENT_ID, default=existing_data.get(CONF_SPOTIFY_CLIENT_ID, "")): cv.string,
            vol.Required(CONF_SPOTIFY_CLIENT_SECRET, default=existing_data.get(CONF_SPOTIFY_CLIENT_SECRET, "")): cv.string,
            vol.Optional(CONF_SPOTIFY_PLAYLIST_ID, default=existing_data.get(CONF_SPOTIFY_PLAYLIST_ID, "")): cv.string,
            vol.Optional(CONF_SPOTIFY_CREATE_PLAYLIST, default=existing_data.get(CONF_SPOTIFY_CREATE_PLAYLIST, True)): cv.boolean,
            vol.Optional(CONF_SPOTIFY_PLAYLIST_NAME, default=existing_data.get(CONF_SPOTIFY_PLAYLIST_NAME, DEFAULT_SPOTIFY_PLAYLIST_NAME)): cv.string,
        })

        return self.async_show_form(step_id="master_config", data_schema=data_schema, errors=errors)

    async def async_step_device(self, user_input=None):
        """Configure individual device."""
        errors = {}
        
        self._check_master_config()
        if not self._master_config_exists:
            return self.async_abort(reason="master_required")
        
        if user_input is not None:
            device_name = user_input[CONF_DEVICE_NAME]
            assist_satellite = user_input[CONF_ASSIST_SATELLITE_ENTITY]
            
            # Check for duplicate device names
            for entry in self._async_current_entries():
                if (entry.data.get("entry_type") == ENTRY_TYPE_DEVICE and 
                    entry.data.get(CONF_DEVICE_NAME) == device_name):
                    errors[CONF_DEVICE_NAME] = "name_exists"
                    break
            
            if not errors:
                # Infer entities from assist satellite
                entity_info = infer_entities_from_assist_satellite(self.hass, assist_satellite)
                
                if not entity_info["exists"]:
                    if not entity_info["switch_exists"]:
                        errors[CONF_ASSIST_SATELLITE_ENTITY] = "tagging_switch_not_found"
                    if not entity_info["media_player_exists"]:
                        errors[CONF_ASSIST_SATELLITE_ENTITY] = "media_player_not_found"
                else:
                    # Override media player if provided
                    media_player = user_input.get(CONF_MEDIA_PLAYER_ENTITY_OVERRIDE) or entity_info["media_player"]
                    
                    # Validate override if provided
                    if user_input.get(CONF_MEDIA_PLAYER_ENTITY_OVERRIDE):
                        if not self.hass.states.get(media_player):
                            errors[CONF_MEDIA_PLAYER_ENTITY_OVERRIDE] = "media_player_not_found"
                    
                    if not errors:
                        data = {
                            "device_name": device_name,
                            "assist_satellite_entity": assist_satellite,
                            "base_name": entity_info["base_name"],
                            "tagging_switch_entity": entity_info["tagging_switch"],
                            "media_player_entity": media_player,
                            "entry_type": ENTRY_TYPE_DEVICE,
                        }
                        return self.async_create_entry(title=device_name, data=data)

        # Get available assist satellites
        assist_satellites = []
        for state in self.hass.states.async_all():
            if state.entity_id.startswith("assist_satellite."):
                assist_satellites.append(state.entity_id)

        data_schema = vol.Schema({
            vol.Required(CONF_DEVICE_NAME): cv.string,
            vol.Required(CONF_ASSIST_SATELLITE_ENTITY): vol.In(assist_satellites),
            vol.Optional(CONF_MEDIA_PLAYER_ENTITY_OVERRIDE, description="Override auto-detected media player"): cv.string,
        })

        return self.async_show_form(step_id="device", data_schema=data_schema, errors=errors)