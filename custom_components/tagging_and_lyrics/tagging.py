# Update the service schema in tagging.py:
SERVICE_FETCH_AUDIO_TAG_SCHEMA = vol.Schema({
    vol.Optional("duration", default=MAX_TOTAL_DURATION): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
    vol.Optional("include_lyrics", default=True): vol.All(vol.Coerce(bool)),
    vol.Optional("add_to_spotify", default=True): vol.All(vol.Coerce(bool)),
    vol.Required("tagging_switch_entity_id"): cv.entity_id,
})

# Update the handle_fetch_audio_tag function:
async def handle_fetch_audio_tag(hass: HomeAssistant, call: ServiceCall):
    """Handle the service call for fetching audio tags."""
    try:
        duration = call.data.get("duration", MAX_TOTAL_DURATION)
        include_lyrics = call.data.get("include_lyrics", True)
        add_to_spotify = call.data.get("add_to_spotify", True)
        tagging_switch_entity_id = call.data["tagging_switch_entity_id"]
        
        # Validate the entity ID format
        if not tagging_switch_entity_id.startswith("switch."):
            error_msg = f"Invalid switch entity ID format: {tagging_switch_entity_id}. Must start with 'switch.'"
            _LOGGER.error(error_msg)
            await hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Audio Tagging Error",
                    "message": error_msg,
                    "notification_id": "tagging_error"
                }
            )
            return
            
        # Check if the entity exists
        if not hass.states.get(tagging_switch_entity_id):
            error_msg = f"Switch entity '{tagging_switch_entity_id}' not found in Home Assistant"
            _LOGGER.error(error_msg)
            await hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Audio Tagging Error",
                    "message": error_msg,
                    "notification_id": "tagging_error"
                }
            )
            return

        # Auto-detect which device to use (use the first available device, or None for default)
        device_configs = get_device_configs(hass)
        entry_id = device_configs[0][0] if device_configs else None
        
        if entry_id:
            device_name = device_configs[0][1].get("device_name", "Unknown Device")
            _LOGGER.info("Using device configuration: %s (ID: %s)", device_name, entry_id)
        else:
            _LOGGER.info("No device configuration found, using master config only")

        _LOGGER.info("fetch_audio_tag service called. Duration: %s, Lyrics: %s, Spotify: %s, Switch: %s", 
                    duration, include_lyrics, add_to_spotify, tagging_switch_entity_id)
        
        # Stop any running instance before starting a new one
        service_key = f"tagging_service_{entry_id}" if entry_id else "tagging_service"
        if service_key in hass.data:
            _LOGGER.info("Stopping existing tagging service before starting a new one.")
            hass.data[service_key].stop()

        # Create and initialize the tagging service
        try:
            tagging_service = TaggingService(hass, tagging_switch_entity_id, entry_id)
            hass.data[service_key] = tagging_service
            
            await tagging_service.listen_for_audio(duration, include_lyrics, add_to_spotify)
        except ValueError as ve:
            error_msg = f"Error initializing tagging service: {str(ve)}"
            _LOGGER.error(error_msg)
            await hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Audio Tagging Error",
                    "message": error_msg,
                    "notification_id": "tagging_error"
                }
            )
        except Exception as e:
            error_msg = f"Unexpected error initializing tagging service: {str(e)}"
            _LOGGER.error(error_msg)
            await hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Audio Tagging Error",
                    "message": error_msg,
                    "notification_id": "tagging_error"
                }
            )
    except Exception as e:
        _LOGGER.error(f"Error in handle_fetch_audio_tag: {str(e)}")
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Audio Tagging Error",
                "message": f"Service call error: {str(e)}",
                "notification_id": "tagging_error"
            }
        )

# Add this helper function to get device configs:
def get_device_configs(hass: HomeAssistant):
    """Get all device configuration entries."""
    if DOMAIN not in hass.data:
        return []
    
    devices = []
    for entry_id, data in hass.data[DOMAIN].items():
        if isinstance(data, dict) and data.get("entry_type") == ENTRY_TYPE_DEVICE:
            devices.append((entry_id, data))
    return devices