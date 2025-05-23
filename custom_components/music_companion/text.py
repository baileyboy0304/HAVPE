"""Text entities for Music Companion lyrics display."""
import logging
from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, CONF_DEVICE_NAME

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up lyrics text entities for a device."""
    device_name = config_entry.data.get(CONF_DEVICE_NAME, "Music Companion Device")
    
    # Only create entities for device entries, not master entries
    if config_entry.data.get("entry_type") != "device":
        return
    
    safe_name = device_name.lower().replace(" ", "_").replace("-", "_")
    
    entities = [
        LyricsTextEntity(
            config_entry,
            "line1",
            f"{device_name} Lyrics Line 1",
            f"{safe_name}_lyrics_line1"
        ),
        LyricsTextEntity(
            config_entry,
            "line2", 
            f"{device_name} Lyrics Line 2",
            f"{safe_name}_lyrics_line2"
        ),
        LyricsTextEntity(
            config_entry,
            "line3",
            f"{device_name} Lyrics Line 3", 
            f"{safe_name}_lyrics_line3"
        ),
    ]
    
    async_add_entities(entities)
    _LOGGER.info("Created lyrics text entities for device: %s", device_name)


class LyricsTextEntity(TextEntity):
    """Text entity for displaying lyrics lines."""
    
    def __init__(self, config_entry: ConfigEntry, line_type: str, name: str, unique_id: str):
        """Initialize the lyrics text entity."""
        self._config_entry = config_entry
        self._line_type = line_type
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{config_entry.entry_id}_{unique_id}"
        self._attr_native_value = ""
        self._attr_icon = "mdi:music-note"
        self._attr_mode = "text"
        self._attr_native_max = 255
        self._attr_native_min = 0
        
        # Set the entity ID we want
        device_name = config_entry.data.get(CONF_DEVICE_NAME, "Music Companion Device")
        safe_name = device_name.lower().replace(" ", "_").replace("-", "_")
        self._entity_id = f"text.{safe_name}_lyrics_{line_type}"
        
        # Device information
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config_entry.entry_id)},
            name=f"Music Companion - {device_name}",
            manufacturer="Music Companion",
            model="Lyrics Display",
            sw_version="1.0.0",
        )
    
    @property
    def entity_id(self) -> str:
        """Return the entity ID."""
        return self._entity_id
    
    @entity_id.setter
    def entity_id(self, value: str) -> None:
        """Set the entity ID."""
        self._entity_id = value
    
    async def async_set_value(self, value: str) -> None:
        """Set the text value."""
        self._attr_native_value = value
        self.async_write_ha_state()
        
    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True