from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Skylight account
# ---------------------------------------------------------------------------

# Replace with the ID of your Skylight frame.
FRAME_ID = "YOUR_FRAME_ID"

# Timezone used for archive filenames and synchronization timestamps.
TIMEZONE = ZoneInfo("America/Chicago")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

# Directory where photos and videos will be archived.
# This may be a local directory, mounted share, NAS, or other storage path.
DESTINATION = Path("/path/to/your/Skylight")

# Internal synchronization checkpoint.
CHECKPOINT = DESTINATION / ".skylight-sync.json"


# ---------------------------------------------------------------------------
# Request behavior
# ---------------------------------------------------------------------------

REQUEST_PAUSE_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 2


# ---------------------------------------------------------------------------
# Manifest lookback
# ---------------------------------------------------------------------------

LOOKBACK_HOURS = 48


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# Logs are stored in the default XDG state directory.
LOG_DIRECTORY = Path.home() / ".local" / "state" / "skylight-sync"


# ---------------------------------------------------------------------------
# Home Assistant notifications (optional)
# ---------------------------------------------------------------------------

# Enable failure notifications through a Home Assistant webhook.
HA_NOTIFICATIONS_ENABLED = False

# Home Assistant base URL.
HA_URL = "http://homeassistant.local:8123"

# Path to a protected file containing the webhook ID.
HA_WEBHOOK_ID_FILE = (
    Path.home() / ".config" / "skylight-sync" / "webhook_id"
)

HA_NOTIFICATION_TIMEOUT = 10.0
