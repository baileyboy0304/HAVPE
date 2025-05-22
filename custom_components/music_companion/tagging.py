import json
import logging
import socket
import time
import datetime
import io
import re
import wave
import threading
import voluptuous as vol
import asyncio
import urllib.parse
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_registry import async_get
from acrcloud.recognizer import ACRCloudRecognizer, ACRCloudRecognizeType
# Import trigger function from lyrics.py
from .lyrics import trigger_lyrics_lookup, update_lyrics_input_text
from .const import DOMAIN, ENTRY_TYPE_MASTER, ENTRY_TYPE_DEVICE

# Define whether lyrics lookup should be enabled after tagging
ENABLE_LYRICS_LOOKUP = True  # Change to False if you don't want automatic lyrics lookup
FINETUNE_SYNC = 2 #was 3

_LOGGER = logging.getLogger(__name__)

# Constants
UDP_PORT = 6056
CHUNK_SIZE = 4096
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2

# New constants for modified approach
CHUNK_DURATION = 3  # Duration of each audio chunk in seconds
MAX_TOTAL_DURATION = 12  # Maximum total recording time in seconds

# Service Schema
SERVICE_FETCH_AUDIO_TAG_SCHEMA = vol.Schema({
    vol.Optional("duration", default=MAX_TOTAL_DURATION): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
    vol.Optional("include_lyrics", default=True): vol.All(vol.Coerce(bool)),
    vol.Optional("add_to_spotify", default=True): vol.All(vol.Coerce(bool)),
    vol.Optional("device_name"): cv.string,
})

def get_master_config(hass: HomeAssistant):
    """Get the master configuration."""
    if DOMAIN not in hass.data:
        return None
    
    for entry_id, data in hass.data[DOMAIN].items():
        # Check if data is dict-like (includes both dict and mappingproxy)
        if hasattr(data, 'get') and data.get("entry_type") == ENTRY_TYPE_MASTER:
            return data
    return None

def get_device_config(hass: HomeAssistant, entry_id=None):
    """Get device configuration by entry_id."""
    if not entry_id or DOMAIN not in hass.data:
        return None
    
    data = hass.data[DOMAIN].get(entry_id)
    # Check if data is dict-like and is a device entry
    if hasattr(data, 'get') and data.get("entry_type") == ENTRY_TYPE_DEVICE:
        return data
    return None

def get_device_configs(hass: HomeAssistant):
    """Get all device configuration entries."""
    if DOMAIN not in hass.data:
        return []
    
    devices = []
    for entry_id, data in hass.data[DOMAIN].items():
        if hasattr(data, 'get') and data.get("entry_type") == ENTRY_TYPE_DEVICE:
            devices.append((entry_id, data))
    return devices

def get_device_config_by_name(hass: HomeAssistant, device_name):
    """Get device configuration by device name."""
    device_configs = get_device_configs(hass)
    for entry_id, device_config in device_configs:
        if device_config.get("device_name") == device_name:
            return entry_id, device_config
    return None, None

def get_tagging_config(hass: HomeAssistant, entry_id=None):
    """Get combined configuration for tagging (master + device)."""
    master_config = get_master_config(hass)
    if not master_config:
        return None
    
    device_config = get_device_config(hass, entry_id) if entry_id else {}
    
    # Simple, clean field mapping
    combined_config = {
        "host": master_config.get("acrcloud_host_url"),
        "port": master_config.get("home_assistant_udp_port", 6056),
        "access_key": master_config.get("acrcloud_access_key"),
        "access_secret": master_config.get("acrcloud_access_secret"),
        "media_player": device_config.get("media_player_entity") if device_config else None
    }
    
    return combined_config

def clean_text(text):
    """Remove Chinese characters from the given text."""
    return re.sub(r'[\u4e00-\u9fff]+', '', text).strip()

def format_time(ms):
    """Convert milliseconds to MM:SS format."""
    minutes = ms // 60000
    seconds = (ms % 60000) // 1000
    return f"{minutes}:{seconds:02d}"

class TaggingService:
    """Service to listen for UDP audio samples and process them."""
    def __init__(self, hass: HomeAssistant, tagging_switch_entity_id, entry_id=None):
        self.hass = hass
        self.entry_id = entry_id
        
        # Validate the switch entity ID exists in Home Assistant
        if not tagging_switch_entity_id or not hass.states.get(tagging_switch_entity_id):
            _LOGGER.error(f"Invalid tagging switch entity ID provided: {tagging_switch_entity_id}")
            raise ValueError(f"The provided switch entity ID '{tagging_switch_entity_id}' does not exist or is invalid")
            
        self.tagging_switch_entity_id = tagging_switch_entity_id

        if self.hass:
            _LOGGER.debug("TaggingService initialized with hass.")
        else:
            _LOGGER.error("TaggingService initialized WITHOUT hass.")

        # Get configuration (master + device)
        conf = get_tagging_config(hass, entry_id)
        if not conf:
            raise ValueError("No configuration found for tagging service. Please ensure master configuration is set up.")

        if not all(key in conf for key in ["host", "access_key", "access_secret"]):
            raise ValueError("Missing required ACRCloud configuration in master settings.")

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Allow reuse
        self.sock.bind(("0.0.0.0", conf["port"]))
        self.sock.setblocking(False)  # Set to non-blocking
        self.running = True

        _LOGGER.info("Set up UDP on port %d", conf["port"])

        self.config = {
            'host': conf["host"],
            'access_key': conf["access_key"],
            'access_secret': conf["access_secret"],
            'recognize_type': ACRCloudRecognizeType.ACR_OPT_REC_AUDIO,
            'debug': False,
            'timeout': 10
        }

        _LOGGER.debug("ACRCloud - host: %s, access_key: %s, port: %s", self.config['host'], self.config['access_key'], conf["port"])
        
        self.recognizer = ACRCloudRecognizer(self.config)

    async def receive_udp_data(self, duration):
        """Non-blocking UDP data reception using asyncio."""
        loop = asyncio.get_running_loop()
        data_buffer = []

        _LOGGER.info("Recording for %d seconds...", duration)

        start_time = time.time()
        while time.time() - start_time < duration:
            try:
                data, addr = await loop.sock_recvfrom(self.sock, CHUNK_SIZE)
                data_buffer.append(data)
            except BlockingIOError:
                pass  # No data available yet, continue
            except Exception as e:
                _LOGGER.error(f"Error receiving data: {e}")
                break
            await asyncio.sleep(0.01)  # Yield control to the event loop

        return data_buffer
    
    def _write_audio_file(self, filename, frames):
        """Write audio data to a WAV file in a blocking way."""
        with wave.open(filename, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(b"".join(frames))
    
    async def write_audio_file(self, filename, frames):
        """Write audio data to a WAV file in a non-blocking way."""
        await asyncio.to_thread(self._write_audio_file, filename, frames)

    async def recognize_audio(self, filename):
        """Recognize audio file using ACRCloud."""
        return await asyncio.to_thread(self.recognizer.recognize_by_file, filename, 0, CHUNK_DURATION)

    def _set_state_in_loop(self, entity_id, state):
        """Set state in the Home Assistant event loop."""
        self.hass.states.async_set(entity_id, state)

    async def process_audio_chunk(self, chunk_buffer, chunk_index):
        """Process a single audio chunk."""
        # Convert buffer to WAV file
        wav_filename = f"recorded_audio_chunk_{chunk_index}.wav"
        await self.write_audio_file(wav_filename, chunk_buffer)
        _LOGGER.info(f"Chunk {chunk_index} recording complete. Sending to ACRCloud...")
        
        try:
            response = await self.recognize_audio(wav_filename)
            _LOGGER.info(f"ACRCloud Response for chunk {chunk_index}: %s", response)
            
            # Parse JSON response
            response_data = json.loads(response)
            
            # Check if we have a successful match
            if ("status" in response_data and 
                response_data["status"].get("msg") == "Success" and 
                "metadata" in response_data and 
                "music" in response_data["metadata"]):
                
                return response_data, True  # Return data and success flag
            
            return response_data, False  # Return data but not successful
            
        except Exception as e:
            _LOGGER.error(f"Error recognizing chunk {chunk_index}: %s", e)
            return None, False

    async def handle_successful_match(self, response_data, include_lyrics, add_to_spotify):
        """Handle a successful match from ACRCloud."""
        first_match = response_data["metadata"]["music"][0]
        
        artist_name = clean_text(first_match["artists"][0]["name"]) if "artists" in first_match else "Unknown Artist"
        title = clean_text(first_match.get("title", "Unknown Title"))
        play_offset_ms = first_match.get("play_offset_ms", 0)
        play_time = format_time(play_offset_ms)

        # Extract Spotify-specific information
        spotify_id = None
        if "external_metadata" in first_match and "spotify" in first_match["external_metadata"]:
            spotify_id = first_match["external_metadata"]["spotify"]["track"]["id"]
            _LOGGER.warning(f"Extracted Spotify ID: {spotify_id}")

        # Use entry-specific sensor name
        sensor_name = f"sensor.last_tagged_song_{self.entry_id}" if self.entry_id else "sensor.last_tagged_song"
        
        # Get device name for friendly display
        device_config = get_device_config(self.hass, self.entry_id)
        device_name = device_config.get("device_name", "Unknown Device") if device_config else "Unknown Device"
        
        # Store track details for Spotify integration
        self.hass.loop.call_soon_threadsafe(
            lambda: self.hass.states.async_set(
                sensor_name, 
                f"{title} - {artist_name}",
                {
                    "title": title,
                    "artist": artist_name,
                    "play_offset": play_offset_ms,
                    "spotify_id": spotify_id,
                    "friendly_name": f"Last Tagged Song - {device_name}",
                    "device_name": device_name,
                    "entry_id": self.entry_id
                }
            )
        )

        # Prepare service call data
        service_data = {
            'title': title,
            'artist': artist_name
        }

        # Add Spotify ID if available
        if spotify_id:
            service_data['spotify_id'] = spotify_id

        # Call add_to_spotify service if requested - USE DOMAIN CONSTANT
        if add_to_spotify:
            _LOGGER.info(f"Adding to Spotify from device: {device_name}")
            await self.hass.services.async_call(
                DOMAIN,
                'add_to_spotify', 
                service_data
            )

        # Formatted response for the main notification
        message = f"🎵 **Title**: {title}\n👤 **Artist**: {artist_name}\n⏱️ **Play Offset**: {play_time} (MM:SS)\n📱 **Device**: {device_name}"

        await update_lyrics_input_text(self.hass, "", "", "")

        # Create a persistent notification with the formatted response
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": f"Audio Tagging Result - {device_name}",
                "message": message,
                "notification_id": f"tagging_result_{self.entry_id}" if self.entry_id else "tagging_result"
            }
        )

        # Trigger lyrics lookup if enabled
        if ENABLE_LYRICS_LOOKUP and include_lyrics:
            if title and artist_name:
                process_begin = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=FINETUNE_SYNC)
                _LOGGER.info("Triggering lyrics lookup for: %s - %s (device: %s)", title, artist_name, device_name)
                await trigger_lyrics_lookup(self.hass, title, artist_name, play_offset_ms, process_begin.isoformat(), self.entry_id)

    async def listen_for_audio(self, max_duration, include_lyrics, add_to_spotify):
        """Listen for UDP audio data in chunks until successful recognition or timeout."""
        try:
            _LOGGER.info("Waiting for incoming UDP audio data...")
            await update_lyrics_input_text(self.hass, "Listening......", "", "")
            
            # Check if the switch entity exists before using it
            if not self.hass.states.get(self.tagging_switch_entity_id):
                error_msg = f"Tagging switch entity '{self.tagging_switch_entity_id}' not found"
                _LOGGER.error(error_msg)
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Audio Tagging Error",
                        "message": error_msg,
                        "notification_id": "tagging_error"
                    }
                )
                return
            
            # Turn on the tagging switch
            try:
                await self.hass.services.async_call(
                    "switch", 
                    "turn_on", 
                    {"entity_id": self.tagging_switch_entity_id}
                )
                _LOGGER.info(f"Turned ON tagging switch: {self.tagging_switch_entity_id}")
            except Exception as e:
                _LOGGER.error(f"Failed to turn on tagging switch: {e}")
                await update_lyrics_input_text(self.hass, "", "", "")
                return
            
            total_chunks = max_duration // CHUNK_DURATION
            all_audio_data = []
            success = False
            successful_response = None
            
            for i in range(total_chunks):
                _LOGGER.info(f"Recording chunk {i+1}/{total_chunks} ({CHUNK_DURATION} seconds)...")
                
                # Collect audio data for this chunk
                chunk_buffer = await self.receive_udp_data(CHUNK_DURATION)
                all_audio_data.extend(chunk_buffer)
                
                # Process this chunk
                response_data, is_success = await self.process_audio_chunk(chunk_buffer, i+1)
                
                if is_success:
                    _LOGGER.info(f"Successfully recognized audio in chunk {i+1}")
                    success = True
                    successful_response = response_data
                    break
                else:
                    _LOGGER.info(f"No match in chunk {i+1}, continuing...")
            
            # Turn off the tagging switch
            try:
                await self.hass.services.async_call(
                    "switch", 
                    "turn_off", 
                    {"entity_id": self.tagging_switch_entity_id}
                )
                _LOGGER.info(f"Turned OFF tagging switch: {self.tagging_switch_entity_id}")
            except Exception as e:
                _LOGGER.error(f"Failed to turn off tagging switch: {e}")
            
            # Handle results
            if success:
                await self.handle_successful_match(successful_response, include_lyrics, add_to_spotify)
            else:
                _LOGGER.info("No music recognized in any chunk.")
                self.hass.loop.call_soon_threadsafe(self._set_state_in_loop, "sensor.tagging_result", "No match")
                
                # Create a notification for no match
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Audio Tagging Result",
                        "message": "No music recognized after trying all audio chunks.",
                        "notification_id": "tagging_result"
                    }
                )
                
                await update_lyrics_input_text(self.hass, "", "", "")

        except Exception as e:
            _LOGGER.error("Error in Tagging Service: %s", e)
            # Ensure switch is turned off in case of an error
            try:
                await self.hass.services.async_call(
                    "switch", 
                    "turn_off", 
                    {"entity_id": self.tagging_switch_entity_id}
                )
            except Exception as switch_e:
                _LOGGER.error(f"Failed to turn off tagging switch during error handling: {switch_e}")
            
            # Create a notification for the error
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Audio Tagging Error",
                    "message": f"An error occurred: {str(e)}",
                    "notification_id": "tagging_error"
                }
            )
            
            await update_lyrics_input_text(self.hass, "", "", "")

    def stop(self):
        """Stop the tagging service."""
        self.running = False
        self.sock.close()


async def handle_fetch_audio_tag(hass: HomeAssistant, call: ServiceCall):
    """Handle the service call for fetching audio tags."""
    try:
        duration = call.data.get("duration", MAX_TOTAL_DURATION)
        include_lyrics = call.data.get("include_lyrics", True)
        add_to_spotify = call.data.get("add_to_spotify", True)
        device_name = call.data.get("device_name")
        
        tagging_switch_entity_id = None
        entry_id = None
        
        if device_name:
            # Find device config by name
            entry_id, device_config = get_device_config_by_name(hass, device_name)
            if not device_config:
                error_msg = f"No device configuration found with name: {device_name}"
                _LOGGER.error(error_msg)
                await create_error_notification(hass, error_msg)
                return
            tagging_switch_entity_id = device_config.get("tagging_switch_entity")
        else:
            # Auto-detect: use first available device
            device_configs = get_device_configs(hass)
            if device_configs:
                entry_id = device_configs[0][0]
                device_config = device_configs[0][1]
                tagging_switch_entity_id = device_config.get("tagging_switch_entity")
                auto_device_name = device_config.get("device_name", "Unknown Device")
                _LOGGER.info("Auto-selected device: %s", auto_device_name)
            else:
                error_msg = "No device configurations found. Please set up a device first."
                _LOGGER.error(error_msg)
                await create_error_notification(hass, error_msg)
                return
        
        # Validate the switch entity exists
        if not hass.states.get(tagging_switch_entity_id):
            error_msg = f"Tagging switch entity '{tagging_switch_entity_id}' not found"
            _LOGGER.error(error_msg)
            await create_error_notification(hass, error_msg)
            return

        _LOGGER.info("Audio tagging service called - Duration: %s, Device: %s", duration, entry_id)
        
        # Create and run tagging service
        tagging_service = TaggingService(hass, tagging_switch_entity_id, entry_id)
        service_key = f"tagging_service_{entry_id}"
        
        # Stop any existing service
        if service_key in hass.data:
            hass.data[service_key].stop()
        
        hass.data[service_key] = tagging_service
        await tagging_service.listen_for_audio(duration, include_lyrics, add_to_spotify)
        
    except Exception as e:
        error_msg = f"Error in audio tagging service: {str(e)}"
        _LOGGER.error(error_msg)
        await create_error_notification(hass, error_msg)

async def create_error_notification(hass, message):
    """Create an error notification."""
    await hass.services.async_call(
        "persistent_notification",
        "create",
        {
            "title": "Audio Tagging Error",
            "message": message,
            "notification_id": "tagging_error"
        }
    )


async def async_setup_tagging_service(hass: HomeAssistant):
    """Register the fetch_audio_tag service in Home Assistant."""
    _LOGGER.info("Registering the fetch_audio_tag service.")

    async def async_wrapper(call):
        await handle_fetch_audio_tag(hass, call)

    hass.services.async_register(
        DOMAIN,
        "fetch_audio_tag",
        async_wrapper,
        schema=SERVICE_FETCH_AUDIO_TAG_SCHEMA
    )

    _LOGGER.info("fetch_audio_tag service registered successfully.")