DOMAIN = "music_companion"

# Master Configuration Constants
CONF_MASTER_CONFIG = "master_config"
CONF_HOST = "host"
CONF_PORT = "port"
CONF_ACCESS_KEY = "access_key"
CONF_ACCESS_SECRET = "access_secret"
CONF_SPOTIFY_CLIENT_ID = "spotify_client_id"
CONF_SPOTIFY_CLIENT_SECRET = "spotify_client_secret"
CONF_SPOTIFY_PLAYLIST_ID = "spotify_playlist_id"
CONF_SPOTIFY_CREATE_PLAYLIST = "spotify_create_playlist"
CONF_SPOTIFY_PLAYLIST_NAME = "spotify_playlist_name"

# Device Configuration Constants
CONF_DEVICE_NAME = "device_name"
CONF_MEDIA_PLAYER = "media_player"

# Entry Types
ENTRY_TYPE_MASTER = "master"
ENTRY_TYPE_DEVICE = "device"

# Spotify Auth Constants
SPOTIFY_AUTH_CALLBACK_PATH = "/api/music_companion/spotify_callback"
SPOTIFY_STORAGE_VERSION = 1
SPOTIFY_STORAGE_KEY = "spotify_tokens"
SPOTIFY_SCOPE = "playlist-modify-private playlist-modify-public user-read-private"
DEFAULT_SPOTIFY_PLAYLIST_NAME = "Home Assistant Music Discoveries"