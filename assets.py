from datetime import datetime, timedelta, timezone

from config import LOOKBACK_HOURS, TIMEZONE


def parse_created_at(value):
    """Parse an ISO timestamp from Skylight and require a timezone."""
    if not isinstance(value, str):
        raise ValueError("Asset timestamp is missing or invalid.")

    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid asset timestamp.") from exc

    if timestamp.tzinfo is None:
        raise ValueError("Asset timestamp does not include a timezone.")

    return timestamp.astimezone(timezone.utc)


def make_filename(attributes):
    """Build a filename using local time and the original asset key."""
    asset_key = attributes.get("asset_key")

    if (
        not isinstance(asset_key, str)
        or not asset_key
        or "/" in asset_key
        or "\\" in asset_key
        or asset_key in (".", "..")
    ):
        raise ValueError("Asset has an invalid original asset key.")

    created_at = parse_created_at(attributes.get("created_at"))
    local_time = created_at.astimezone(TIMEZONE)

    timestamp = local_time.strftime("%Y-%m-%d_%H-%M-%S.%f")[:-3]
    offset = local_time.strftime("%z")

    return f"{timestamp}{offset}__{asset_key}"


def select_assets(records, checkpoint):
    """Select valid photos and videos newer than checkpoint minus overlap."""
    if checkpoint.tzinfo is None:
        raise ValueError("Checkpoint must include a timezone.")

    cutoff = checkpoint - timedelta(hours=LOOKBACK_HOURS)
    selected = []

    for record in records:
        attributes = record.get("attributes")

        if not isinstance(attributes, dict):
            raise ValueError("Manifest record has invalid attributes.")

        if attributes.get("destroyed_at"):
            continue

        asset_type = attributes.get("asset_type")
        if asset_type not in ("photo", "video"):
            raise ValueError(f"Unexpected asset type: {asset_type!r}")

        asset_url = attributes.get("asset_url")
        if not isinstance(asset_url, str) or not asset_url.startswith("https://"):
            raise ValueError("Asset has a missing or invalid HTTPS URL.")

        created_at = parse_created_at(attributes.get("created_at"))
        filename = make_filename(attributes)

        if created_at >= cutoff.astimezone(timezone.utc):
            selected.append({
                "asset_type": asset_type,
                "asset_key": attributes["asset_key"],
                "created_at": created_at,
                "asset_url": asset_url,
                "filename": filename,
            })

    return sorted(selected, key=lambda asset: asset["created_at"])
