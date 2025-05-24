import voluptuous as vol
import logging
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
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
    CONF_ASSIST_SATELLITE_ENTITY,
    CONF_MEDIA_PLAYER_ENTITY,
    CONF_DISPLAY_DEVICE,
    CONF_USE_DISPLAY_DEVICE,
    ENTRY_TYPE_MASTER,
    ENTRY_TYPE_DEVICE,
    VIEW_ASSIST_DOMAIN,
    REMOTE_ASSIST_DISPLAY_DOMAIN,
    DEFAULT_SPOTIFY_PLAYLIST_NAME
)

_LOGGER = logging.getLogger(__name__)

def infer_tagging_switch_from_assist_satellite(hass, assist_satellite_entity):
    """Infer tagging switch entity from assist satellite entity ID."""
    if not assist_satellite_entity.startswith("assist_satellite.") or not assist_satellite_entity.endswith("_assist_satellite"):
        return None, "Invalid assist satellite entity format"
    
    # Extract base name: assist_satellite.home_assistant_voice_093d58_assist_satellite -> home_assistant_voice_093d58
    base_name = assist_satellite_entity[17:-17]  # Remove "assist_satellite." and "_assist_satellite"
    
    # Infer tagging switch entity
    tagging_switch = f"switch.{base_name}_tagging_enable"
    
    # Check if switch exists
    if hass.states.get(tagging_switch) is None:
        return None, f"Tagging switch '{tagging_switch}' not found"
    
    return tagging_switch, None

def get_devices_for_domain(hass: HomeAssistant, domain: str):
    """Get devices for a specific domain."""
    device_registry = dr.async_get(hass)
    return [
        device for device in device_registry.devices.values()
        if any(entry.domain == domain for entry in device.config_entries)
    ]

def get_display_device_options(hass: HomeAssistant):
    """Get available View Assist display devices for selection."""
    display_devices = {}
    
    # Look for View Assist display devices (exactly like View Assist does it)
    try:
        # Check View Assist domain data for browser IDs - this is the main source
        view_assist_data = hass.data.setdefault(VIEW_ASSIST_DOMAIN, {})
        va_browser_ids = view_assist_data.get("va_browser_ids", {})
        
        _LOGGER.debug("View Assist browser IDs found: %s", list(va_browser_ids.keys()))
        
        for device_id, device_name in va_browser_ids.items():
            display_devices[device_id] = f"View Assist: {device_name}"
            
    except Exception as e:
        _LOGGER.debug("Error getting View Assist browser IDs: %s", e)
    
    # Add Remote Assist Display devices from device registry
    try:
        remote_display_devices = get_devices_for_domain(hass, REMOTE_ASSIST_DISPLAY_DOMAIN)
        _LOGGER.debug("Found %d Remote Assist Display devices", len(remote_display_devices))
        for device in remote_display_devices:
            if device.id not in display_devices:
                display_devices[device.id] = f"Remote Display: {device.name or device.id}"
    except Exception as e:
        _LOGGER.debug("Error getting Remote Assist Display devices: %s", e)
    
    # Check for View Assist entities to find display devices (additional check)
    try:
        entity_registry = er.async_get(hass)
        for entity in entity_registry.entities.values():
            if entity.platform == VIEW_ASSIST_DOMAIN and entity.device_id:
                device_registry = dr.async_get(hass)
                device = device_registry.async_get(entity.device_id)
                if device and device.id not in display_devices:
                    display_devices[device.id] = f"View Assist Device: {device.name or entity.device_id}"
    except Exception as e:
        _LOGGER.debug("Error checking View Assist entities: %s", e)
    
    # Add current setting if not already in list (for existing configs)
    # This matches View Assist's approach for backwards compatibility
    
    # Set a dummy device for initial setup if no devices found (matches View Assist)
    if not display_devices:
        display_devices = {"dummy": "dummy (no View Assist devices found)"}
    
    # Always add none option
    display_devices["none"] = "None (use text entities only)"
    
    _LOGGER.debug("Available display devices: %s", list(display_devices.keys()))
    return display_devices

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
            
        self._master_config_exists = False
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
            # Check for existing master configuration
            existing_master = None
            all_entries = self._async_current_entries()
            
            _LOGGER.debug("Checking for existing master config. Total entries: %d", len(all_entries))
            
            for entry in all_entries:
                entry_type = entry.data.get("entry_type")
                _LOGGER.debug("Entry: %s, Type: %s", entry.entry_id, entry_type)
                if entry_type == ENTRY_TYPE_MASTER:
                    if existing_master is not None:
                        # Found multiple master configs - this shouldn't happen!
                        _LOGGER.error("Multiple master configurations found! Deleting duplicate.")
                        await self.hass.config_entries.async_remove(entry.entry_id)
                    else:
                        existing_master = entry
            
            data = {
                **user_input,
                "entry_type": ENTRY_TYPE_MASTER
            }
            
            if existing_master:
                _LOGGER.info("Updating existing master configuration: %s", existing_master.entry_id)
                self.hass.config_entries.async_update_entry(existing_master, data=data)
                return self.async_abort(reason="master_updated")
            else:
                _LOGGER.info("Creating new master configuration")
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
            media_player = user_input[CONF_MEDIA_PLAYER_ENTITY]
            use_display_device = user_input.get(CONF_USE_DISPLAY_DEVICE, False)
            display_device = user_input.get(CONF_DISPLAY_DEVICE) if use_display_device else None
            
            # Check for duplicate device names
            for entry in self._async_current_entries():
                if (entry.data.get("entry_type") == ENTRY_TYPE_DEVICE and 
                    entry.data.get(CONF_DEVICE_NAME) == device_name):
                    errors[CONF_DEVICE_NAME] = "name_exists"
                    break
            
            if not errors:
                # Validate assist satellite entity
                if not assist_satellite.startswith("assist_satellite."):
                    errors[CONF_ASSIST_SATELLITE_ENTITY] = "invalid_assist_satellite"
                else:
                    # Try to infer tagging switch from assist satellite
                    tagging_switch, error = infer_tagging_switch_from_assist_satellite(self.hass, assist_satellite)
                    tagging_enabled = tagging_switch is not None
                    
                    if error and not tagging_enabled:
                        _LOGGER.info("Device '%s' will be configured without tagging capability: %s", device_name, error)
                
                # Validate media player entity
                if not self.hass.states.get(media_player):
                    errors[CONF_MEDIA_PLAYER_ENTITY] = "media_player_not_found"
                
                # Validate display device if selected
                if use_display_device and display_device and display_device not in ["none", "dummy"]:
                    # Basic validation - check if device exists in View Assist or device registry
                    valid_device = False
                    
                    # Check if it's a View Assist browser ID
                    view_assist_data = hass.data.setdefault(VIEW_ASSIST_DOMAIN, {})
                    va_browser_ids = view_assist_data.get("va_browser_ids", {})
                    if display_device in va_browser_ids:
                        valid_device = True
                        _LOGGER.debug("Found device in View Assist browser IDs: %s", display_device)
                    
                    # Check device registry for Remote Assist Display devices
                    if not valid_device:
                        device_registry = dr.async_get(self.hass)
                        if display_device in [device.id for device in device_registry.devices.values()]:
                            valid_device = True
                            _LOGGER.debug("Found device in device registry: %s", display_device)
                    
                    if not valid_device:
                        _LOGGER.warning("Display device not found: %s. Available View Assist devices: %s", 
                                      display_device, list(va_browser_ids.keys()))
                        errors[CONF_DISPLAY_DEVICE] = "display_device_not_found"
                
                if not errors:
                    # Extract base name for storage
                    base_name = assist_satellite[17:-17] if assist_satellite.endswith("_assist_satellite") else ""
                    
                    data = {
                        "device_name": device_name,
                        "assist_satellite_entity": assist_satellite,
                        "media_player_entity": media_player,
                        "base_name": base_name,
                        "tagging_enabled": tagging_enabled,
                        "use_display_device": use_display_device,
                        "entry_type": ENTRY_TYPE_DEVICE,
                    }
                    
                    # Only add tagging switch if it exists
                    if tagging_enabled and tagging_switch:
                        data["tagging_switch_entity"] = tagging_switch
                    
                    # Only add display device if enabled and valid
                    if use_display_device and display_device and display_device != "none":
                        data[CONF_DISPLAY_DEVICE] = display_device
                    
                    # Log the device creation for debugging
                    _LOGGER.info("Creating device entry: %s with tagging enabled: %s, display device: %s", 
                               device_name, tagging_enabled, display_device if use_display_device else "None")
                    if tagging_enabled:
                        _LOGGER.info("Tagging switch: %s", tagging_switch)
                    else:
                        _LOGGER.info("Device will support lyrics display only (no audio tagging)")
                    
                    return self.async_create_entry(title=device_name, data=data)

        # Get available assist satellites and media players
        assist_satellites = []
        media_players = []
        
        for state in self.hass.states.async_all():
            if state.entity_id.startswith("assist_satellite."):
                assist_satellites.append(state.entity_id)
            elif state.entity_id.startswith("media_player."):
                media_players.append(state.entity_id)

        # Sort the lists for better user experience
        assist_satellites.sort()
        media_players.sort()

        # Get display device options
        display_devices = get_display_device_options(self.hass)
        display_options = [{"value": key, "label": value} for key, value in display_devices.items()]

        data_schema = vol.Schema({
            vol.Required(CONF_DEVICE_NAME): cv.string,
            vol.Required(CONF_ASSIST_SATELLITE_ENTITY): vol.In(assist_satellites),
            vol.Required(CONF_MEDIA_PLAYER_ENTITY): vol.In(media_players),
            vol.Optional(CONF_USE_DISPLAY_DEVICE, default=False): cv.boolean,
            vol.Optional(CONF_DISPLAY_DEVICE): SelectSelector(
                SelectSelectorConfig(
                    options=display_options,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        })

        return self.async_show_form(step_id="device", data_schema=data_schema, errors=errors)