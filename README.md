# Skylight Sync

Skylight Sync is a small Python application that maintains a local archive of photos and videos from a Skylight frame.

It retrieves the frame's media manifest, downloads assets that are not already in the archive, and keeps track of synchronization progress so later runs can be incremental. The intended result is an independently accessible archive of the family's Skylight photos and videos, rather than relying on the frame or its app as the only place those memories exist.

## What it does

- Connects to Skylight using the authenticated session managed by [`pyskylight`](https://github.com/joshuaswarren/pyskylight).
- Reads the selected frame's media manifest.
- Downloads photos and videos using their original asset identity.
- Names archived files with their Skylight-created timestamp and asset key.
- Uses a checkpoint to select assets for incremental synchronization.
- Recognizes assets already present in the archive and avoids downloading them again.
- Verifies the archive against the active manifest before advancing the checkpoint.
- Detects unexpected archive contents and missing historical assets rather than silently accepting an inconsistent archive.
- Can send failure details to Home Assistant through a protected webhook.
- Is designed to run unattended under systemd.

## Archive integrity and recovery

The application is deliberately cautious about archive state.

- The first synchronization requires an empty destination directory.
- An initialization marker allows an interrupted first synchronization to be recognized and resumed safely.
- Existing archive files are matched to Skylight asset keys.
- Active historical assets are checked against the archive.
- Downloaded files are checked for nonzero size and expected JPEG or MP4 signatures.
- The checkpoint advances only after final archive verification succeeds.
- Unexpected files, directories, and symbolic links in the archive destination cause validation to fail.

The integrity check verifies archive consistency and basic file signatures. It is not a full media decode or cryptographic checksum verification.

Do not manually delete the checkpoint or initialization marker as a troubleshooting shortcut. They are part of the application's recovery and synchronization state.

## Requirements

- Linux
- Python 3.11 or newer
- Git
- Network access to Skylight's website
- A dedicated archive destination with sufficient storage
- `systemd` for the documented unattended service and timer setup

The reference deployment uses Debian 13 in a Proxmox LXC container and stores the archive on a NAS-mounted CIFS share. Those are deployment choices, not application requirements; local storage or another mounted volume can also be used.

## Installation and deployment

See **[DEPLOYMENT.md](DEPLOYMENT.md)** for the complete installation and configuration procedure, including:

- Preparing the Linux host and Python virtual environment
- Installing the pinned dependencies
- Authenticating with Skylight and caching the session
- Configuring archive storage, including an optional NAS/CIFS mount
- Installing the systemd service and weekly timer
- Running the initial synchronization and verifying the archive
- Troubleshooting authentication, storage, service, and timer issues

## Configuration

Application settings are maintained in `config.py`. Copy `config.example.py` to `config.py` and edit the private copy before the first run and confirm that the selected Skylight frame, timezone, archive destination, and synchronization lookback settings match your environment.

The service must run as the same operating-system account that owns the cached Skylight authentication session. The reference deployment uses `root`.

## Home Assistant notifications

Home Assistant failure notifications are optional. When configured, Skylight Sync reads a webhook ID from:

```text
/etc/skylight-sync/webhook_id
```

On a synchronization failure, it sends a structured notification payload containing the run time, failed stage, affected asset when applicable, download-attempt information, and a sanitized error summary.

The notification path is best-effort: a Home Assistant notification failure is logged and does not replace the original synchronization error. Sensitive session tokens and signed download URLs are not included in the error summary.

## Running

The reference deployment uses the systemd service for an on-demand run:

```bash
systemctl start skylight-sync.service
```

The weekly timer can be checked with:

```bash
systemctl status skylight-sync.timer
systemctl list-timers skylight-sync.timer
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for the complete service and timer setup. The reference schedule is Sunday at noon in the host's configured local timezone.

## Project dependencies

- [`pyskylight`](https://github.com/joshuaswarren/pyskylight) — Skylight authentication and API access
- [`httpx`](https://www.python-httpx.org/) — HTTP client used for downloads and Home Assistant notifications

The deployment guide pins the dependency versions/commit used by the reference installation.
