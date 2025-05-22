import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from .const import (
    DOMAIN, 
    CONF_MASTER_CONFIG,
    CONF_MEDIA_PLAYER, 
    CONF_HOST, 
    CONF_PORT, 
    CONF_ACCESS_KEY, 
    CONF_ACCESS_SECRET,
    CONF_DEVICE_NAME,
    CONF_SPOTIFY_CLIENT_ID,
    CONF_SPOTIFY_CLIENT_SECRET,
    CONF_SPOTIFY_PLAYLIST_ID,
    CONF_SPOTIFY_CREATE_PLAYLIST,
    CONF_SPOTIFY_PLAYLIST_NAME,
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
        # Only check when we have access to hass
        if not self.hass:
            return
            
        for entry in self._async_current_entries():
            if entry.data.get("entry_type") == ENTRY_TYPE_MASTER:
                self._master_config_exists = True
                break

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        # Check master config when we have hass access
        self._check_master_config()
        
        # Show menu to choose between master config and device
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

        # Check if master config exists
        self._check_master_config()
        
        menu_options = ["device"]
        if self._master_config_exists:
            menu_options.append("master")

        return self.async_show_menu(
            step_id="menu",
            menu_options={
                "device": "Add Device",
                "master": "Update Master Configuration" if self._master_config_exists else "Setup Master Configuration"
            }
        )

    async def async_step_master_config(self, user_input=None):
        """Configure master settings (ACRCloud and Spotify credentials)."""
        errors = {}
        
        if user_input is not None:
            # Check if we're updating existing master config
            existing_master = None
            for entry in self._async_current_entries():
                if entry.data.get("entry_type") == ENTRY_TYPE_MASTER:
                    existing_master = entry
                    break
            
            if existing_master and not user_input.get("_update_existing"):
                errors["base"] = "master_exists"
            else:
                # Create or update the master configuration
                data = {
                    **user_input,
                    "entry_type": ENTRY_TYPE_MASTER
                }
                
                if existing_master:
                    # Update existing entry
                    self.hass.config_entries.async_update_entry(existing_master, data=data)
                    return self.async_abort(reason="master_updated")
                else:
                    # Create new master config
                    return self.async_create_entry(
                        title="Master Configuration",
                        data=data
                    )

        # Get existing values if updating
        existing_data = {}
        for entry in self._async_current_entries():
            if entry.data.get("entry_type") == ENTRY_TYPE_MASTER:
                existing_data = entry.data
                break

        # Clean schema without description parameters - let translations handle labels
        data_schema = vol.Schema({
            vol.Required(CONF_HOST, default=existing_data.get(CONF_HOST, "")): cv.string,
            vol.Required(CONF_PORT, default=existing_data.get(CONF_PORT, 6056)): cv.port,
            vol.Required(CONF_ACCESS_KEY, default=existing_data.get(CONF_ACCESS_KEY, "")): cv.string,
            vol.Required(CONF_ACCESS_SECRET, default=existing_data.get(CONF_ACCESS_SECRET, "")): cv.string,
            vol.Required(CONF_SPOTIFY_CLIENT_ID, default=existing_data.get(CONF_SPOTIFY_CLIENT_ID, "")): cv.string,
            vol.Required(CONF_SPOTIFY_CLIENT_SECRET, default=existing_data.get(CONF_SPOTIFY_CLIENT_SECRET, "")): cv.string,
            vol.Optional(CONF_SPOTIFY_PLAYLIST_ID, default=existing_data.get(CONF_SPOTIFY_PLAYLIST_ID, "")): cv.string,
            vol.Optional(CONF_SPOTIFY_CREATE_PLAYLIST, default=existing_data.get(CONF_SPOTIFY_CREATE_PLAYLIST, True)): cv.boolean,
            vol.Optional(CONF_SPOTIFY_PLAYLIST_NAME, default=existing_data.get(CONF_SPOTIFY_PLAYLIST_NAME, DEFAULT_SPOTIFY_PLAYLIST_NAME)): cv.string,
        })

        if existing_data:
            data_schema = data_schema.extend({
                vol.Optional("_update_existing", default=True): cv.boolean,
            })

        return self.async_show_form(
            step_id="master_config",
            data_schema=data_schema,
            errors=errors
        )

    async def async_step_device(self, user_input=None):
        """Configure individual device."""
        errors = {}
        
        # Check if master config exists
        self._check_master_config()
        if not self._master_config_exists:
            return self.async_abort(reason="master_required")
        
        if user_input is not None:
            device_name = user_input[CONF_DEVICE_NAME]
            
            # Check if this device name already exists
            for entry in self._async_current_entries():
                if (entry.data.get("entry_type") == ENTRY_TYPE_DEVICE and 
                    entry.data.get(CONF_DEVICE_NAME) == device_name):
                    errors[CONF_DEVICE_NAME] = "name_exists"
                    break
            
            if not errors:
                # Create the device entry
                data = {
                    **user_input,
                    "entry_type": ENTRY_TYPE_DEVICE
                }
                return self.async_create_entry(
                    title=device_name,
                    data=data
                )

        data_schema = vol.Schema({
            vol.Required(CONF_DEVICE_NAME): cv.string,
            vol.Required(CONF_MEDIA_PLAYER): cv.string,
        })

        return self.async_show_form(
            step_id="device",
            data_schema=data_schema,
            errors=errors
        )
    
    @staticmethod
    def async_get_options_flow(config_entry):
        return MusicCompanionOptionsFlow(config_entry)


class MusicCompanionOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Music Companion."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        entry_type = self.config_entry.data.get("entry_type", ENTRY_TYPE_DEVICE)
        
        if entry_type == ENTRY_TYPE_MASTER:
            return await self.async_step_master_options()
        else:
            return await self.async_step_device_options()

    async def async_step_master_options(self):
        """Handle master configuration options."""
        return self.async_show_form(
            step_id="master_options",
            data_schema=vol.Schema({
                vol.Optional(CONF_HOST, default=self.config_entry.data.get(CONF_HOST)): cv.string,
                vol.Optional(CONF_PORT, default=self.config_entry.data.get(CONF_PORT, 6056)): cv.port,
                vol.Optional(CONF_ACCESS_KEY, default=self.config_entry.data.get(CONF_ACCESS_KEY)): cv.string,
                vol.Optional(CONF_ACCESS_SECRET, default=self.config_entry.data.get(CONF_ACCESS_SECRET)): cv.string,
                vol.Optional(CONF_SPOTIFY_CLIENT_ID, default=self.config_entry.data.get(CONF_SPOTIFY_CLIENT_ID)): cv.string,
                vol.Optional(CONF_SPOTIFY_CLIENT_SECRET, default=self.config_entry.data.get(CONF_SPOTIFY_CLIENT_SECRET)): cv.string,
                vol.Optional(CONF_SPOTIFY_PLAYLIST_ID, default=self.config_entry.data.get(CONF_SPOTIFY_PLAYLIST_ID, "")): cv.string,
                vol.Optional(CONF_SPOTIFY_CREATE_PLAYLIST, default=self.config_entry.data.get(CONF_SPOTIFY_CREATE_PLAYLIST, True)): cv.boolean,
                vol.Optional(CONF_SPOTIFY_PLAYLIST_NAME, default=self.config_entry.data.get(CONF_SPOTIFY_PLAYLIST_NAME, DEFAULT_SPOTIFY_PLAYLIST_NAME)): cv.string,
            })
        )

    async def async_step_device_options(self):
        """Handle device options."""
        return self.async_show_form(
            step_id="device_options",
            data_schema=vol.Schema({
                vol.Optional(CONF_DEVICE_NAME, default=self.config_entry.data.get(CONF_DEVICE_NAME)): cv.string,
                vol.Optional(CONF_MEDIA_PLAYER, default=self.config_entry.data.get(CONF_MEDIA_PLAYER)): cv.string,
            })
        )