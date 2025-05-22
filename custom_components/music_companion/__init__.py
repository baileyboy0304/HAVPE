import logging
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.config_entries import ConfigEntry
from .tagging import async_setup_tagging_service
from .lyrics import async_setup_lyrics_service
from .spotify import async_setup_spotify_service
from .const import (
    DOMAIN,
    CONF_MASTER_CONFIG,
    CONF_MEDIA_PLAYER,
    CONF_ACCESS_KEY,
    CONF_ACCESS_SECRET,
    CONF_PORT,
    CONF_HOST,
    CONF_DEVICE_NAME,
    CONF_SPOTIFY_CLIENT_ID,
    CONF_SPOTIFY_CLIENT_SECRET,
    CONF_SPOTIFY_PLAYLIST_ID,
    CONF_SPOTIFY_CREATE_PLAYLIST,
    CONF_SPOTIFY_PLAYLIST_NAME,
    ENTRY_TYPE_MASTER,
    ENTRY_TYPE_DEVICE
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_MEDIA_PLAYER): cv.entity_id,
                vol.Required(CONF_PORT, default=6056): cv.port,
                vol.Required(CONF_HOST): cv.string,
                vol.Required(CONF_ACCESS_KEY): cv.string,
                vol.Required(CONF_ACCESS_SECRET): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

def get_master_config(hass: HomeAssistant):
    """Get the master configuration entry."""
    if DOMAIN not in hass.data:
        return None
    
    for entry_id, data in hass.data[DOMAIN].items():
        if isinstance(data, dict) and data.get("entry_type") == ENTRY_TYPE_MASTER:
            return data
    return None

def get_device_configs(hass: HomeAssistant):
    """Get all device configuration entries."""
    if DOMAIN not in hass.data:
        return []
    
    devices = []
    for entry_id, data in hass.data[DOMAIN].items():
        if isinstance(data, dict) and data.get("entry_type") == ENTRY_TYPE_DEVICE:
            devices.append((entry_id, data))
    return devices

async def async_setup(hass: HomeAssistant, config) -> bool:
    """Set up the Tagging and Lyrics integration from yaml configuration."""
    if DOMAIN not in config:
        return True

    # Store YAML config in a separate key to avoid conflicts with config entries
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["yaml_config"] = config[DOMAIN]
    
    # Register the tagging and lyrics services
    await async_setup_tagging_service(hass)
    await async_setup_lyrics_service(hass)
    
    # Create sensor for last tagged song
    hass.states.async_set(
        "sensor.last_tagged_song", 
        "None", 
        {
            "title": "",
            "artist": "",
            "play_offset": 0,
            "friendly_name": "Last Tagged Song"
        }
    )
    
    return True

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up the Tagging and Lyrics integration from a config entry."""
    entry_type = config_entry.data.get("entry_type", ENTRY_TYPE_DEVICE)
    
    # Initialize the domain data structure if it doesn't exist
    hass.data.setdefault(DOMAIN, {})
    
    # Store this entry's data using the entry ID as the key
    hass.data[DOMAIN][config_entry.entry_id] = config_entry.data

    if entry_type == ENTRY_TYPE_MASTER:
        return await async_setup_master_entry(hass, config_entry)
    else:
        return await async_setup_device_entry(hass, config_entry)

async def async_setup_master_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up the master configuration entry."""
    _LOGGER.info("Setting up Tagging and Lyrics Master Configuration")

    # Register the tagging and lyrics services (only once)
    if not hass.data[DOMAIN].get('_services_registered'):
        await async_setup_tagging_service(hass)
        await async_setup_lyrics_service(hass)
        hass.data[DOMAIN]['_services_registered'] = True
    
    try:
        # Set up Spotify service using master config credentials
        if "spotify_service" not in hass.data.get(DOMAIN, {}):
            spotify_config = {
                "client_id": config_entry.data.get(CONF_SPOTIFY_CLIENT_ID),
                "client_secret": config_entry.data.get(CONF_SPOTIFY_CLIENT_SECRET),
                "playlist_id": config_entry.data.get(CONF_SPOTIFY_PLAYLIST_ID),
                "create_playlist": config_entry.data.get(CONF_SPOTIFY_CREATE_PLAYLIST, True),
                "playlist_name": config_entry.data.get(CONF_SPOTIFY_PLAYLIST_NAME, "Home Assistant Discovered Tracks")
            }
            
            # Log spotify config (but mask secret)
            safe_config = {**spotify_config}
            if "client_secret" in safe_config:
                safe_config["client_secret"] = "****"
            _LOGGER.debug("Spotify configuration prepared: %s", safe_config)
            
            # Create a config dictionary with the spotify section
            modified_config = {"spotify": spotify_config}
            
            # Call the Spotify setup service
            _LOGGER.info("Initializing Spotify service from master configuration...")
            await async_setup_spotify_service(hass, modified_config)
            _LOGGER.info("Spotify service initialization completed")
            
            # Create a notification to confirm Spotify is set up
            await hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Master Configuration Setup",
                    "message": "Master configuration with Spotify integration has been initialized.",
                    "notification_id": "master_config_setup"
                }
            )
    except Exception as e:
        _LOGGER.error("Failed to initialize Spotify service from master config: %s", e)
        # Create error notification
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Master Configuration Error",
                "message": f"Failed to initialize master configuration: {str(e)}\n\nCheck logs for more details.",
                "notification_id": "master_config_error"
            }
        )

    # Ensure logging level is set to debug for troubleshooting
    logging.getLogger("custom_components.tagging_and_lyrics").setLevel(logging.DEBUG)

    return True

async def async_setup_device_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up a device entry."""
    device_name = config_entry.data.get(CONF_DEVICE_NAME, "Tagging and Lyrics Device")
    _LOGGER.info("Setting up Tagging and Lyrics device: %s", device_name)

    # Check if master configuration exists
    master_config = get_master_config(hass)
    if not master_config:
        _LOGGER.error("Master configuration not found for device: %s", device_name)
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Device Setup Error",
                "message": f"Device '{device_name}' cannot be set up without master configuration. Please set up master configuration first.",
                "notification_id": f"device_setup_error_{config_entry.entry_id}"
            }
        )
        return False

    # Create sensor for last tagged song (one per device)
    sensor_name = f"sensor.last_tagged_song_{config_entry.entry_id}"
    hass.states.async_set(
        sensor_name, 
        "None", 
        {
            "title": "",
            "artist": "",
            "play_offset": 0,
            "friendly_name": f"Last Tagged Song - {device_name}",
            "device_name": device_name,
            "device_id": config_entry.entry_id
        }
    )

    # Autostart the fetch_lyrics service for this device
    async def autostart(event):
        _LOGGER.debug("Autostarting fetch_lyrics service for device: %s", device_name)
        try:
            entity_id = config_entry.data[CONF_MEDIA_PLAYER]
            await hass.services.async_call(
                "tagging_and_lyrics",
                "fetch_lyrics",
                {"entity_id": entity_id}
            )
            _LOGGER.info("Autostarted fetch_lyrics service for entity: %s (device: %s)", entity_id, device_name)
        except Exception as e:
            _LOGGER.error("Error in autostarting fetch_lyrics service for device %s: %s", device_name, e)

    # Listen for Home Assistant start event
    hass.bus.async_listen_once("homeassistant_start", autostart)
    _LOGGER.debug("Registered autostart listener for device: %s", device_name)

    return True

async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    entry_type = config_entry.data.get("entry_type", ENTRY_TYPE_DEVICE)
    
    if entry_type == ENTRY_TYPE_MASTER:
        _LOGGER.info("Unloading Tagging and Lyrics Master Configuration")
        # Don't remove shared services as devices might still need them
    else:
        device_name = config_entry.data.get(CONF_DEVICE_NAME, "Tagging and Lyrics Device")
        _LOGGER.info("Unloading Tagging and Lyrics device: %s", device_name)
        
        # Clean up the device sensor
        sensor_name = f"sensor.last_tagged_song_{config_entry.entry_id}"
        if hass.states.get(sensor_name):
            hass.states.async_remove(sensor_name)
    
    # Remove this entry's data
    if DOMAIN in hass.data and config_entry.entry_id in hass.data[DOMAIN]:
        del hass.data[DOMAIN][config_entry.entry_id]
    
    return True

async def async_reload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, config_entry)
    await async_setup_entry(hass, config_entry)