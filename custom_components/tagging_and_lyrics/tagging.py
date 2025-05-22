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
from .const import DOMAIN

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
    vol.Required("tagging_switch_entity_id"): cv.entity_id
})

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
    def __init__(self, hass: HomeAssistant, tagging_switch_entity_id):
        self.hass = hass
        
        # Validate the switch entity ID exists in Home Assistant
        if not tagging_switch_entity_id or not hass.states.get(tagging_switch_entity_id):
            _LOGGER.error(f"Invalid tagging switch entity ID provided: {tagging_switch_entity_id}")
            raise ValueError(f"The provided switch entity ID '{tagging_switch_entity_id}' does not exist or is invalid")
            
        self.tagging_switch_entity_id = tagging_switch_entity_id

        if self.hass:
            _LOGGER.debug("TaggingService initialized with hass.")
        else:
            _LOGGER.error("TaggingService initialized WITHOUT hass.")

        conf = hass.data["tagging_and_lyrics"]

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
        first_match = response_data["metadata"]["music"][0]  # Get the first match
        
        artist_name = clean_text(first_match["artists"][0]["name"]) if "artists" in first_match else "Unknown Artist"
        title = clean_text(first_match.get("title", "Unknown Title"))
        play_offset_ms = first_match.get("play_offset_ms", 0)
        play_time = format_time(play_offset_ms)

        # Extract Spotify-specific information
        spotify_id = None
        if "external_metadata" in first_match and "spotify" in first_match["external_metadata"]:
            spotify_id = first_match["external_metadata"]["spotify"]["track"]["id"]
            _LOGGER.warning(f"Extracted Spotify ID: {spotify_id}")

        # Store track details for Spotify integration
        self.hass.loop.call_soon_threadsafe(
            lambda: self.hass.states.async_set(
                "sensor.last_tagged_song", 
                f"{title} - {artist_name}",
                {
                    "title": title,
                    "artist": artist_name,
                    "play_offset": play_offset_ms,
                    "spotify_id": spotify_id,  # Add Spotify ID to attributes
                    "friendly_name": "Last Tagged Song"
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

        # Call add_to_spotify service if requested
        if add_to_spotify:
            _LOGGER.info(f"Adding to Spotify")
            await self.hass.services.async_call(
                'tagging_and_lyrics', 
                'add_to_spotify', 
                service_data
            )

        ## Formatted response for the main notification
        message = f"🎵 **Title**: {title}\n👤 **Artist**: {artist_name}\n⏱️ **Play Offset**: {play_time} (MM:SS)"

        await update_lyrics_input_text(self.hass, "", "", "")

        # Create a persistent notification with the formatted response
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Audio Tagging Result",
                "message": message,
                "notification_id": "tagging_result"
            }
        )

        # Trigger lyrics lookup if enabled
        if ENABLE_LYRICS_LOOKUP and include_lyrics:
            if title and artist_name:
                process_begin = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=FINETUNE_SYNC)
                _LOGGER.info("Triggering lyrics lookup for: %s - %s", title, artist_name)
                await trigger_lyrics_lookup(self.hass, title, artist_name, play_offset_ms, process_begin.isoformat())

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
        
        # Get the required tagging switch entity ID
        if "tagging_switch_entity_id" not in call.data:
            error_msg = "Required parameter 'tagging_switch_entity_id' is missing"
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

        _LOGGER.info("fetch_audio_tag service called. Max recording duration: %s seconds, Lyrics requested: %s, Add to Spotify: %s, Switch entity: %s", 
                    duration, include_lyrics, add_to_spotify, tagging_switch_entity_id)
        
        # Stop any running instance before starting a new one
        if "tagging_service" in hass.data:
            _LOGGER.info("Stopping existing tagging service before starting a new one.")
            hass.data["tagging_service"].stop()

        # Create and initialize the tagging service
        try:
            tagging_service = TaggingService(hass, tagging_switch_entity_id)
            hass.data["tagging_service"] = tagging_service  # Store the instance
            
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


async def async_setup_tagging_service(hass: HomeAssistant):
    """Register the fetch_audio_tag service in Home Assistant."""
    _LOGGER.info("Registering the fetch_audio_tag service.")

    async def async_wrapper(call):
        await handle_fetch_audio_tag(hass, call)

    hass.services.async_register(
        "tagging_and_lyrics",
        "fetch_audio_tag",
        async_wrapper,
        schema=SERVICE_FETCH_AUDIO_TAG_SCHEMA
    )

    _LOGGER.info("fetch_audio_tag service registered successfully.")