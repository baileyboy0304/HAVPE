import logging
import datetime
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
import lrc_kit
import time
import re
import asyncio
import aiohttp
from homeassistant.helpers.event import async_track_state_change_event
from .media_tracker import MediaTracker

_LOGGER = logging.getLogger(__name__)

# Global variables
LAST_MEDIA_CONTENT_ID = None
ACTIVE_LYRICS_SYNC = None  # Instance of LyricsSynchronizer

SERVICE_FETCH_LYRICS_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_id
})


class LyricsSynchronizer:
    """Manages lyrics synchronization using MediaTracker."""
    
    def __init__(self, hass: HomeAssistant):
        self.hass = hass
        self.media_tracker = None
        self.entity_id = None
        
        # Track information for comparison
        self.current_track = ""
        self.current_artist = ""
        
        # Lyrics data
        self.timeline = []
        self.lyrics = []
        self.current_line_index = -1
        
        # Control flags
        self.active = False
        
        # Display update handling
        self.last_update_time = 0
        self.force_update_interval = 3  # Force display update every 3 seconds even without position change
    
    async def start(self, entity_id: str, timeline: list, lyrics: list, pos=None, updated_at=None, is_radio_source=False):
        """Start lyrics synchronization for the given entity."""
        if self.active:
            await self.stop()
            
        self.entity_id = entity_id
        self.timeline = timeline
        self.lyrics = lyrics
        self.current_line_index = -1
        self.last_update_time = datetime.datetime.now().timestamp()
        
        # Store the current track info from the player state
        player_state = self.hass.states.get(entity_id)
        if player_state:
            self.current_track = player_state.attributes.get("media_title", "")
            self.current_artist = player_state.attributes.get("media_artist", "")
            _LOGGER.info("LyricsSynchronizer: Tracking track '%s' by '%s'", self.current_track, self.current_artist)
        
        # Immediately show "Loading lyrics..." message
        await update_lyrics_input_text(self.hass, "", "Loading lyrics...", 
                                     self.lyrics[0] if lyrics and len(lyrics) > 0 else "")
        
        # Initialize media tracker with callbacks, passing radio source flag
        self.media_tracker = MediaTracker(
            self.hass, 
            self.entity_id,
            self.update_lyrics_display,  # Position update callback
            self.handle_track_change,    # Track change callback
            is_radio_source              # Flag for radio source
        )
        
        # Set the initial position if provided
        if pos is not None and updated_at is not None:
            # Use the set_initial_position method which works for all source types
            self.media_tracker.set_initial_position(pos, updated_at)
            
            # Calculate initial position in milliseconds for better lyrics placement
            initial_position_ms = pos * 1000
            _LOGGER.info("LyricsSynchronizer: Initial position is %.2f ms", initial_position_ms)
            
            # Find the correct starting point in lyrics
            line_found = False
            if timeline:
                # For radio sources, we need to be more aggressive in finding the right starting point
                if is_radio_source:
                    # Calculate where we expect to be in about 500ms (to account for processing delay)
                    target_position_ms = initial_position_ms + 500
                    
                    # Try to find a line that's AFTER our current position but within a reasonable window
                    for i in range(1, len(timeline)):
                        if timeline[i-1] > initial_position_ms and timeline[i-1] < initial_position_ms + 10000:
                            # Found a line coming up soon - use it
                            self.current_line_index = i-1
                            _LOGGER.info("LyricsSynchronizer: Starting at upcoming line index %d at %d ms", 
                                       i-1, timeline[i-1])
                            line_found = True
                            
                            # Display immediately
                            previous_line = lyrics[i-2] if i > 1 else ""
                            current_line = lyrics[i-1]
                            next_line = lyrics[i] if i < len(lyrics) else ""
                            
                            await update_lyrics_input_text(self.hass, previous_line, current_line, next_line)
                            break
                
                # If we didn't find an upcoming line, or this isn't a radio source,
                # fall back to finding the line that matches our current position
                if not line_found:
                    for i in range(1, len(timeline)):
                        if timeline[i-1] <= initial_position_ms < timeline[i]:
                            # We found the right line to start with
                            self.current_line_index = i-1
                            _LOGGER.info("LyricsSynchronizer: Starting at line index %d", self.current_line_index)
                            
                            # Display the initial lyrics
                            previous_line = lyrics[i-2] if i > 1 else ""
                            current_line = lyrics[i-1]
                            next_line = lyrics[i] if i < len(lyrics) else ""
                            
                            await update_lyrics_input_text(self.hass, previous_line, current_line, next_line)
                            break
            
            # If we couldn't find the right position in the timeline, 
            # at least display the first line if we have lyrics
            if self.current_line_index == -1 and len(lyrics) > 0:
                # Show first few lines immediately
                _LOGGER.info("LyricsSynchronizer: No matching position found, showing first lines")
                if len(lyrics) > 1:
                    await update_lyrics_input_text(self.hass, "", lyrics[0], lyrics[1])
                else:
                    await update_lyrics_input_text(self.hass, "", lyrics[0], "")
        
        # Start tracking
        await self.media_tracker.start_tracking()
        self.active = True
        
        # Start a periodic force update task
        asyncio.create_task(self._force_update_task())
        
        _LOGGER.info("LyricsSynchronizer: Started for %s with %d lyrics lines (radio source: %s)", 
                    self.entity_id, len(self.lyrics), is_radio_source)
    
    async def stop(self):
        """Stop lyrics synchronization."""
        if not self.active:
            return
            
        self.active = False
        
        if self.media_tracker:
            await self.media_tracker.stop_tracking()
            self.media_tracker = None
        
        # Clear display
        await update_lyrics_input_text(self.hass, "", "", "")
        
        _LOGGER.info("LyricsSynchronizer: Stopped")
    
    def update_lyrics_display(self, media_timecode: float):
        """Update lyrics display based on current media position."""
        if not self.active or not self.timeline or not self.lyrics:
            _LOGGER.warning("LyricsSynchronizer: Unable to update lyrics - no active timeline or lyrics")
            return
            
        # Record update time for force update mechanism
        self.last_update_time = datetime.datetime.now().timestamp()
            
        # Convert to milliseconds for comparison with timeline
        position_ms = media_timecode * 1000
        
        # Log position occasionally for debugging
        if int(media_timecode) % 5 == 0 and abs(media_timecode - int(media_timecode)) < 0.15:  # Log roughly every 5 seconds
            _LOGGER.debug("LyricsSynchronizer: Current position: %.2f seconds (%.2f ms)", 
                        media_timecode, position_ms)
        
        # Check if lyrics finished
        if position_ms >= self.timeline[-1]:
            _LOGGER.info("LyricsSynchronizer: Lyrics finished")
            asyncio.create_task(self.stop())
            return
            
        # Find current line
        found_position = False
        for i in range(1, len(self.timeline)):
            if self.timeline[i-1] <= position_ms < self.timeline[i]:
                if i-1 != self.current_line_index:
                    self.current_line_index = i-1
                    
                    # Get lines to display
                    previous_line = self.lyrics[i-2] if i > 1 else ""
                    current_line = self.lyrics[i-1]
                    next_line = self.lyrics[i] if i < len(self.lyrics) else ""
                    
                    # Update display
                    asyncio.create_task(
                        update_lyrics_input_text(self.hass, previous_line, current_line, next_line)
                    )
                    
                    _LOGGER.debug("LyricsSynchronizer: Updated to line %d at %f ms", 
                                i-1, position_ms)
                
                found_position = True
                break
        
        # If position wasn't found in any interval but lyrics exist,
        # it might be before the first line
        if not found_position and position_ms < self.timeline[0]:
            if self.current_line_index != -1:
                self.current_line_index = -1
                asyncio.create_task(
                    update_lyrics_input_text(self.hass, "", "Waiting for first line...", self.lyrics[0])
                )
    
    def handle_track_change(self, is_track_change=True):
        """Handle track changes or seek operations detected by the media tracker.
        
        Args:
            is_track_change: True if actual track changed, False if just a seek operation
        """
        if is_track_change:
            # For actual track changes, stop lyrics entirely
            _LOGGER.info("LyricsSynchronizer: Track change detected, stopping lyrics")
            asyncio.create_task(self.stop())
        else:
            # For seek operations, just reset the current line index to force resyncing
            _LOGGER.info("LyricsSynchronizer: Seek operation detected, resyncing lyrics")
            
            # Get current position to find the right lyrics line
            if self.media_tracker and self.media_tracker.media_position is not None:
                current_position = self.media_tracker.calculate_current_position()
                position_ms = current_position * 1000
                
                # Find the appropriate lyrics line for current position
                self.current_line_index = -1  # Reset first
                
                # Look for the right lyrics line
                for i in range(1, len(self.timeline)):
                    if self.timeline[i-1] <= position_ms < self.timeline[i]:
                        # Found the right line
                        self.current_line_index = i-1
                        
                        # Display corresponding lyrics
                        previous_line = self.lyrics[i-2] if i > 1 else ""
                        current_line = self.lyrics[i-1]
                        next_line = self.lyrics[i] if i < len(self.lyrics) else ""
                        
                        asyncio.create_task(
                            update_lyrics_input_text(self.hass, previous_line, current_line, next_line)
                        )
                        
                        _LOGGER.info("LyricsSynchronizer: Resynced to line %d at %f ms", 
                                   i-1, position_ms)
                        break
                
                # If we couldn't find a matching line, check if we're before the first line
                if self.current_line_index == -1:
                    if position_ms < self.timeline[0]:
                        asyncio.create_task(
                            update_lyrics_input_text(self.hass, "", "Waiting for first line...", self.lyrics[0])
                        )
                    else:
                        # We might be past the end
                        asyncio.create_task(
                            update_lyrics_input_text(self.hass, "", "Lyrics finished", "")
                        )
    
    async def _force_update_task(self):
        """Periodically force update the lyrics display to ensure it doesn't get stuck."""
        try:
            # Use a shorter interval for the first few updates to ensure quick initial sync
            initial_updates = 0
            max_initial_updates = 5
            initial_interval = 0.5  # Faster initial updates
            
            while self.active:
                # Use shorter interval for initial updates
                if initial_updates < max_initial_updates:
                    await asyncio.sleep(initial_interval)
                    initial_updates += 1
                else:
                    await asyncio.sleep(self.force_update_interval)
                
                # If we're active but no update in a while, force one
                current_time = datetime.datetime.now().timestamp()
                time_since_update = current_time - self.last_update_time
                
                if time_since_update > (0.5 if initial_updates < max_initial_updates else self.force_update_interval):
                    _LOGGER.debug("LyricsSynchronizer: Forcing display update (%.1f seconds since last update)", 
                                 time_since_update)
                    
                    # Recalculate current position
                    if self.media_tracker and self.media_tracker.state == "playing":
                        current_position = self.media_tracker.calculate_current_position()
                        position_ms = current_position * 1000
                        
                        if len(self.timeline) > 1 and len(self.lyrics) > 1:
                            # Find appropriate line for current position
                            found_line = False
                            for i in range(1, len(self.timeline)):
                                if self.timeline[i-1] <= position_ms < self.timeline[i]:
                                    # Get lines to display
                                    previous_line = self.lyrics[i-2] if i > 1 else ""
                                    current_line = self.lyrics[i-1]
                                    next_line = self.lyrics[i] if i < len(self.lyrics) else ""
                                    
                                    # Force update the display
                                    await update_lyrics_input_text(self.hass, previous_line, current_line, next_line)
                                    self.last_update_time = current_time
                                    _LOGGER.debug("LyricsSynchronizer: Force updated to line %d (%.1f ms)", 
                                                i-1, position_ms)
                                    found_line = True
                                    break
                            
                            # If no matching line found, check if we're past the end
                            if not found_line:
                                if position_ms < self.timeline[0]:
                                    # Before first line
                                    await update_lyrics_input_text(self.hass, "", 
                                                                "Coming up...", self.lyrics[0])
                                elif position_ms >= self.timeline[-1]:
                                    # Past the last line
                                    await update_lyrics_input_text(self.hass, 
                                                                self.lyrics[-1], "End of lyrics", "")
                                else:
                                    # We should have found a line - try to recover
                                    # Find the closest line
                                    closest_idx = min(range(len(self.timeline)), 
                                                    key=lambda i: abs(self.timeline[i] - position_ms))
                                    
                                    _LOGGER.info("LyricsSynchronizer: Couldn't find exact line match, using closest: %d", 
                                               closest_idx)
                                    
                                    previous_line = self.lyrics[closest_idx-1] if closest_idx > 0 else ""
                                    current_line = self.lyrics[closest_idx]
                                    next_line = self.lyrics[closest_idx+1] if closest_idx < len(self.lyrics)-1 else ""
                                    
                                    await update_lyrics_input_text(self.hass, previous_line, current_line, next_line)
                                
                                self.last_update_time = current_time
                        else:
                            # If we have lyrics but no timeline yet, or in initialization
                            if len(self.lyrics) > 0:
                                await update_lyrics_input_text(self.hass, "", "Loading lyrics...", self.lyrics[0])
                                self.last_update_time = current_time
        except asyncio.CancelledError:
            _LOGGER.debug("LyricsSynchronizer: Force update task cancelled")
        except Exception as e:
            _LOGGER.error("LyricsSynchronizer: Error in force update task: %s", str(e))


def lyricSplit(lyrics):
    """Split lyrics into a timeline and corresponding lines."""
    timeline = []
    lrc = []

    for line in lyrics.splitlines():
        if line.startswith(("[0", "[1", "[2", "[3")):
            # Match timestamp in square brackets (e.g., [01:15.35])
            regex = re.compile(r'\[.+?\]')
            match = re.match(regex, line)

            if not match:
                continue  # Skip lines with no timestamp

            # Extract and clean the timestamp
            _time = match.group(0)[1:-1]  # Remove square brackets
            line = regex.sub('', line).strip()  # Remove timestamp from the line

            if not line:  # Skip if the line is empty after removing the timestamp
                continue

            # Convert the timestamp to milliseconds
            try:
                time_parts = _time.split(':')
                minutes = int(time_parts[0])
                seconds = float(time_parts[1])
                milliseconds = int((minutes * 60 + seconds) * 1000)

                timeline.append(milliseconds)
                lrc.append(line)
            except (ValueError, IndexError) as e:
                _LOGGER.warning("Invalid timestamp format: %s", _time)
                continue

    return timeline, lrc


async def update_lyrics_input_text(hass: HomeAssistant, previous_line: str, current_line: str, next_line: str):
    """Update the input_text entities with the current lyrics lines."""
    await hass.services.async_call("input_text", "set_value", {"entity_id": "input_text.line1", "value": previous_line})
    await hass.services.async_call("input_text", "set_value", {"entity_id": "input_text.line2", "value": current_line})
    await hass.services.async_call("input_text", "set_value", {"entity_id": "input_text.line3", "value": next_line})


def clean_track_name(track):
    """Improved function to clean up track names."""
    if not track:
        return ""
    
    original_track = track

    _LOGGER.info("Pre-cleaned up track = %s", track)
    
    # 1. Handle nested brackets by recursively removing them from the outermost in
    while re.search(r'\s*[\(\[\{\<].*?[\)\]\}\>]', track):
        track = re.sub(r'\s*[\(\[\{\<].*?[\)\]\}\>]', '', track)
    
    # 2. Remove dates in various formats (1999, '99, etc.)
    track = re.sub(r'\b\d{4}\b', '', track)
    track = re.sub(r'\b\'\d{2}\b', '', track)
    
    # 3. More careful handling of dashes - only split on dashes surrounded by spaces
    # or dashes followed by specific version-related words
    dash_pattern = r'\s+-\s+|\s*-\s*(?:remaster|version|edit|mix|single|live|from)\b'
    parts = re.split(dash_pattern, track, flags=re.IGNORECASE)
    track = parts[0]
    
    # 4. Remove common phrases
    common_phrases = [
        r'\b(?:from|on)\s+(?:the\s+)?(?:"[^"]*"|\'[^\']*\'|\S+)?\s*(?:soundtrack|album|movie|film|series|show)\b',
        r'\b(?:original|movie|film|radio|single|album|instrumental|acoustic|live|studio|extended|shortened)\s+(?:version|edit|mix|cut|recording)\b',
        r'\b(?:remaster(?:ed)?|remix(?:ed)?|feat\.?|ft\.?|featuring)\b',
        r'\b(?:bonus\s+track|deluxe\s+edition|digital\s+exclusive)\b',
        r'\b(?:explicit|clean)\s+(?:version|edit)?\b',
        r'\d+(?:th|st|nd|rd)?\s+(?:anniversary|edition)\b',
        r'\b(?:anthology|world\s+wildlife\s+fund)\s+(?:version)?\b'
    ]
    
    for phrase in common_phrases:
        track = re.sub(phrase, '', track, flags=re.IGNORECASE)
    
    # 5. Remove non-Latin characters while preserving accented characters
    track = re.sub(r'[^\x00-\x7F\xC0-\xFF\u2000-\u206F]', '', track)
    
    # 6. Normalize quotes and apostrophes
    track = track.replace("'", "'").replace(""", '"').replace(""", '"').replace("´", "'").replace("`", "'")
    
    # 7. Replace multiple spaces with a single space
    track = re.sub(r'\s+', ' ', track)
    
    # 8. Trim whitespace and remove trailing punctuation
    track = track.strip()
    track = re.sub(r'[.,;:!?]+$', '', track).strip()
    
    # 9. Check if we've removed everything - if so, return at least part of the original
    if len(track) < 2 and len(original_track) > 0:
        # Try to extract any word characters from the original
        words = re.findall(r'\b[A-Za-z]+\b', original_track)
        if words:
            return ' '.join(words)
        # If no words, return the original but cleaned of special characters
        return re.sub(r'[^\w\s]', '', original_track).strip()
    
    _LOGGER.info("Cleaned up track = %s", track)
    
    return track


def get_media_player_info(hass: HomeAssistant, entity_id: str):
    """Retrieve track, artist, media position, and last update time from media player."""
    player_state = hass.states.get(entity_id)

    if not player_state:
        _LOGGER.error("Get Media Info: Media player entity not found.")
        hass.async_create_task(update_lyrics_input_text(hass, "Media player entity not found", "", ""))
        return None, None, None, None  # Return empty values

    if player_state.state != "playing":
        _LOGGER.info("Get Media Info: Media player is not playing. Waiting...")
        hass.async_create_task(update_lyrics_input_text(hass, "Waiting for playback to start", "", ""))
        return None, None, None, None

    track = clean_track_name(player_state.attributes.get("media_title", ""))
    artist = player_state.attributes.get("media_artist", "")
    pos = player_state.attributes.get("media_position")
    updated_at = player_state.attributes.get("media_position_updated_at")

    if not track or not artist:
        _LOGGER.warning("Get Media Info: Missing track or artist information.")
        hass.async_create_task(update_lyrics_input_text(hass, "Missing track or artist", "", ""))
        return None, None, None, None

    return track, artist, pos, updated_at


async def fetch_lyrics_for_track(hass: HomeAssistant, track: str, artist: str, pos, updated_at, entity_id, audiofingerprint):
    """Fetch lyrics for a given track and synchronize with playback."""
    global ACTIVE_LYRICS_SYNC
    global LAST_MEDIA_CONTENT_ID

    _LOGGER.info("Fetch: Fetching lyrics for: %s %s", artist, track)
    _LOGGER.info("Fetch: pos=%s, updated_at=%s, audiofingerprint=%s", pos, updated_at, audiofingerprint)

    # Reset the current display first to show we're working on it
    await update_lyrics_input_text(hass, "", "Searching for lyrics...", "")

    # Ensure parameters are valid
    if pos is None or updated_at is None:
        # Try to get current position if not provided
        player_state = hass.states.get(entity_id)
        if player_state and player_state.state == "playing":
            pos = player_state.attributes.get("media_position")
            updated_at = player_state.attributes.get("media_position_updated_at")
            _LOGGER.info("Fetch: Retrieved current position: pos=%s, updated_at=%s", pos, updated_at)
        
        # If still not available, exit
        if pos is None or updated_at is None:
            _LOGGER.error("Fetch: pos or updated_at is not initialized. Exiting lyrics sync.")
            if ACTIVE_LYRICS_SYNC and ACTIVE_LYRICS_SYNC.active:
                await ACTIVE_LYRICS_SYNC.stop()
            return

    # Check if the switch is enabled
    if not hass.states.is_state("input_boolean.lyrics_enable", "on"):
        _LOGGER.info("Fetch: Lyrics fetching is disabled by switch. Exiting.")
        return

    # Get current media_content_id for tracking
    player_state = hass.states.get(entity_id)
    current_track = player_state.attributes.get("media_title", "") if player_state else ""
    current_artist = player_state.attributes.get("media_artist", "") if player_state else ""
    current_media_id = player_state.attributes.get("media_content_id", "") if player_state else ""
    
    # Always stop existing lyrics if this is a fingerprint-based identification
    # This allows for correction of misidentified tracks
    should_stop_existing = True
    
    # For non-fingerprint calls, check if we already have lyrics running for this track
    if not audiofingerprint and ACTIVE_LYRICS_SYNC and ACTIVE_LYRICS_SYNC.active:
        # Check if we're already displaying lyrics for this track/artist
        if (ACTIVE_LYRICS_SYNC.media_tracker and 
            ACTIVE_LYRICS_SYNC.media_tracker.current_track == current_track and
            ACTIVE_LYRICS_SYNC.media_tracker.current_artist == current_artist):
            _LOGGER.info("Fetch: Already displaying lyrics for this track. Skipping.")
            should_stop_existing = False
            return
    
    # Stop any existing lyrics synchronization if needed
    if should_stop_existing and ACTIVE_LYRICS_SYNC and ACTIVE_LYRICS_SYNC.active:
        _LOGGER.info("Fetch: Stopping current lyrics session for new request.")
        await ACTIVE_LYRICS_SYNC.stop()
    
    # Update the last media content ID
    LAST_MEDIA_CONTENT_ID = current_media_id
    
    _LOGGER.info("Fetch: Start new session")
    
    # Load lyrics provider
    lyrics_provider = [lrc_kit.QQProvider]
    provider = lrc_kit.ComboLyricsProvider(lyrics_provider)
    
    # Try with the combined artist name first
    _LOGGER.info("Fetch: Searching for lyrics with combined artist name.")
    search_request = await hass.async_add_executor_job(lrc_kit.SearchRequest, artist, track)
    lyrics_result = await hass.async_add_executor_job(provider.search, search_request)
    
    # If no lyrics found and artist contains separators, try with individual artists
    if not lyrics_result:
        # Define common artist separators
        separators = ["/", "|", "&", ",", " and ", " with ", " feat ", " feat. ", " ft ", " ft. ", " featuring "]
        
        # Check if any separator is in the artist name
        contains_separator = any(sep in artist for sep in separators)
        
        if contains_separator:
            _LOGGER.info("Fetch: No lyrics found with combined artist name. Trying individual artists.")
            
            # Split the artist string using multiple possible separators
            individual_artists = artist
            for sep in separators:
                if sep in individual_artists:
                    individual_artists = individual_artists.replace(sep, "|")  # Normalize to one separator
            
            # Split by the normalized separator and strip whitespace
            artist_list = [a.strip() for a in individual_artists.split("|") if a.strip()]
            
            # Try each individual artist
            for single_artist in artist_list:
                _LOGGER.info("Fetch: Trying with artist: %s", single_artist)
                search_request = await hass.async_add_executor_job(lrc_kit.SearchRequest, single_artist, track)
                lyrics_result = await hass.async_add_executor_job(provider.search, search_request)
                
                if lyrics_result:
                    _LOGGER.info("Fetch: Lyrics found with artist: %s", single_artist)
                    break
    
    # If still no lyrics found
    if not lyrics_result:
        _LOGGER.warning("Fetch: No lyrics found for '%s'.", track)
        await update_lyrics_input_text(hass, "", "No lyrics found", "")
        return

    _LOGGER.info("Fetch: Processing lyrics into timeline")
    timeline, lrc = lyricSplit(str(lyrics_result))

    if not timeline:
        _LOGGER.error("Fetch: Lyrics have no timeline.")
        await update_lyrics_input_text(hass, "", "Lyrics not synced", "")
        return
        
    # Debug information
    _LOGGER.info("Fetch: Found %d lines of lyrics", len(lrc))
    if len(lrc) > 0:
        _LOGGER.info("Fetch: First line: %s", lrc[0])
        _LOGGER.info("Fetch: Last line: %s", lrc[-1])

    # Create lyrics synchronizer if it doesn't exist
    if not ACTIVE_LYRICS_SYNC:
        ACTIVE_LYRICS_SYNC = LyricsSynchronizer(hass)
    
    # Start synchronized lyrics display, passing the audiofingerprint flag
    await ACTIVE_LYRICS_SYNC.start(entity_id, timeline, lrc, pos, updated_at, audiofingerprint)


async def trigger_lyrics_lookup(hass: HomeAssistant, title: str, artist: str, play_offset_ms: int, process_begin: str):
    """Trigger lyrics lookup based on a recognized song."""

    if not title or not artist:
        _LOGGER.warning("Trigger Lyrics: Cannot trigger lyrics lookup: Missing title or artist.")
        return

    _LOGGER.info("Trigger Lyrics (from tagging) -> Artist: %s Title: %s", artist, title)

    # Get the configured media player entity ID
    media_player = hass.data["tagging_and_lyrics"]["media_player"]

    clean_track = clean_track_name(title)
    await fetch_lyrics_for_track(hass, clean_track, artist, play_offset_ms/1000, process_begin, media_player, True)


async def handle_fetch_lyrics(hass: HomeAssistant, call: ServiceCall):
    """Main service handler: gets media info and fetches lyrics."""
    entity_id = call.data.get("entity_id")
    
    # Get current track info
    track, artist, pos, updated_at = get_media_player_info(hass, entity_id)
    
    if not track or not artist:
        _LOGGER.warning("Handle Fetch Lyrics: Missing track or artist information.")
        return
    
    # Fetch and display lyrics
    await fetch_lyrics_for_track(hass, track, artist, pos, updated_at, entity_id, False)
    
    #async def monitor_playback(entity, old_state, new_state):
    async def monitor_playback_event(event):
        """Monitor media player state changes."""
        entity = event.data.get('entity_id')
        old_state = event.data.get('old_state')
        new_state = event.data.get('new_state')
        global LAST_MEDIA_CONTENT_ID
        global ACTIVE_LYRICS_SYNC

        _LOGGER.debug("Monitor Playback: Media player state changed: %s -> %s", 
                     old_state.state if old_state else "None", new_state.state)

        media_content_id = hass.states.get(entity).attributes.get("media_content_id", "")

        # Ignore updates if the state remains unchanged (e.g., volume changes)
        if old_state and new_state and old_state.state == new_state.state:
            if old_state.attributes.get("media_content_id") == media_content_id:
                _LOGGER.debug("Monitor Playback: State unchanged and media_content_id unchanged. Ignoring attribute-only update.")
                return
            
        # Only act if the player changes to 'playing' and it's not a radio station
        if new_state.state == "playing" and not media_content_id.startswith("library://radio"):
            
            _LOGGER.debug("Monitor Playback: LAST_MEDIA_CONTENT_ID: %s", LAST_MEDIA_CONTENT_ID)
            _LOGGER.debug("Monitor Playback: media_content_id: %s", media_content_id)

            # Check if the media_content_id is different from the last one processed
            if media_content_id and media_content_id != LAST_MEDIA_CONTENT_ID:
                _LOGGER.info("Monitor Playback: Content has changed, not a radio station.")
                
                # Stop any existing lyrics display
                if ACTIVE_LYRICS_SYNC and ACTIVE_LYRICS_SYNC.active:
                    await ACTIVE_LYRICS_SYNC.stop()
                
                await update_lyrics_input_text(hass, "", "", "")
                track, artist, pos, updated_at = get_media_player_info(hass, entity)
                _LOGGER.info("Monitor Playback: New Info -> Artist %s, Track %s, media_content_id %s", 
                            artist, track, media_content_id)
                _LOGGER.info("Monitor Playback: New Info -> pos %s, updated_at %s", pos, updated_at)

                # Call the lyrics function and update the last processed ID
                if track and artist:
                    _LOGGER.debug("Monitor Playback: Fetching lyrics for new track")
                    LAST_MEDIA_CONTENT_ID = media_content_id
                    hass.async_create_task(fetch_lyrics_for_track(hass, track, artist, pos, updated_at, entity, False))
            else:
                _LOGGER.info("Monitor Playback: Track already processed. Skipping lyrics fetch.")
        # Playing, radio
        elif new_state.state == "playing" and media_content_id.startswith("library://radio"):
            LAST_MEDIA_CONTENT_ID = media_content_id # Radio station, don't fetch lyrics
            _LOGGER.info("Monitor Playback: Radio station detected.")
            
            # Stop any existing lyrics display
            if ACTIVE_LYRICS_SYNC and ACTIVE_LYRICS_SYNC.active:
                await ACTIVE_LYRICS_SYNC.stop()
                
            await update_lyrics_input_text(hass, "", "", "")
        else:
            # Not playing, but lyrics display will be handled by MediaTracker
            _LOGGER.info("Monitor Playback: Media player is not playing.")

    # Register listener for state change events
    #hass.helpers.event.async_track_state_change(entity_id, monitor_playback)
    hass.helpers.event.async_track_state_change_event(entity_id, monitor_playback_event)
    _LOGGER.debug("Registered state change listener for: %s", entity_id)


async def async_setup_lyrics_service(hass: HomeAssistant):
    """Register the fetch_lyrics service."""
    _LOGGER.debug("Registering the fetch_lyrics service.")

    async def async_wrapper(call):
        await handle_fetch_lyrics(hass, call)

    hass.services.async_register(
        "tagging_and_lyrics",
        "fetch_lyrics",
        async_wrapper,
        schema=SERVICE_FETCH_LYRICS_SCHEMA
    )

    _LOGGER.info("fetch_lyrics service registered successfully.")