import logging
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from .tagging import async_setup_tagging_service
from .lyrics import async_setup_lyrics_service
from .spotify import async_setup_spotify_service
from .const import (
    DOMAIN,
    CONF_MEDIA_PLAYER,
    CONF_ACCESS_KEY,
    CONF_ACCESS_SECRET,
    CONF_PORT,
    CONF_HOST,
    # Add Spotify constants
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_PLAYLIST_ID,
    SPOTIFY_CREATE_PLAYLIST,
    SPOTIFY_PLAYLIST_NAME
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

async def async_setup(hass: HomeAssistant, config) -> bool:
    """Set up the Tagging and Lyrics integration from yaml configuration."""
    if DOMAIN not in config:
        return True

    hass.data[DOMAIN] = config[DOMAIN]
    
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

async def async_setup_entry(hass: HomeAssistant, config_entry) -> bool:
    """Set up the Tagging and Lyrics integration from a config entry."""
    _LOGGER.info("Setting up the Tagging and Lyrics integration from config entry.")

    hass.data[DOMAIN] = config_entry.data

    # Register the tagging and lyrics services asynchronously
    await async_setup_tagging_service(hass)
    await async_setup_lyrics_service(hass)
    
    try:
        # Add Spotify configuration from constants - with explicit debugging
        _LOGGER.debug("Preparing Spotify configuration from constants")
        spotify_config = {
            "client_id": SPOTIFY_CLIENT_ID,
            "client_secret": SPOTIFY_CLIENT_SECRET,
            "playlist_id": SPOTIFY_PLAYLIST_ID,
            "create_playlist": SPOTIFY_CREATE_PLAYLIST,
            "playlist_name": SPOTIFY_PLAYLIST_NAME
        }
        
        # Log spotify config (but mask secret)
        safe_config = {**spotify_config}
        if "client_secret" in safe_config:
            safe_config["client_secret"] = "****"
        _LOGGER.debug("Spotify configuration prepared: %s", safe_config)
        
        # Create a config dictionary with the spotify section
        modified_config = {"spotify": spotify_config}
        
        # Call the Spotify setup service with additional error handling
        _LOGGER.info("Initializing Spotify service...")
        await async_setup_spotify_service(hass, modified_config)
        _LOGGER.info("Spotify service initialization completed")
        
        # Create a notification to confirm Spotify is set up
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Spotify Integration Status",
                "message": "Spotify integration has been initialized. You can add tracks to your playlist from song identifications.",
                "notification_id": "spotify_setup_status"
            }
        )
    except Exception as e:
        _LOGGER.error("Failed to initialize Spotify service: %s", e)
        # Create error notification
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Spotify Integration Error",
                "message": f"Failed to initialize Spotify integration: {str(e)}\n\nCheck logs for more details.",
                "notification_id": "spotify_setup_error"
            }
        )
    
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

    # Ensure logging level is set to debug for troubleshooting
    logging.getLogger("custom_components.tagging_and_lyrics").setLevel(logging.DEBUG)

    # Autostart the fetch_lyrics service
    async def autostart(event):
        _LOGGER.debug("Autostarting fetch_lyrics service.")
        try:
            entity_id = config_entry.data[CONF_MEDIA_PLAYER]  # Use the configured media player
            await hass.services.async_call(
                "tagging_and_lyrics",
                "fetch_lyrics",
                {"entity_id": entity_id}
            )
            _LOGGER.info("Autostarted fetch_lyrics service for entity: %s", entity_id)
        except Exception as e:
            _LOGGER.error("Error in autostarting fetch_lyrics service: %s", e)

    # Listen for Home Assistant start event
    hass.bus.async_listen_once("homeassistant_start", autostart)
    _LOGGER.debug("Registered autostart listener for homeassistant_start.")

    return True