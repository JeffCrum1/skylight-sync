# Deployment Guide

This guide covers deploying Skylight Sync on a Linux server using Python
and systemd.

The reference deployment uses Debian 13, a Proxmox LXC container, and a
NAS-mounted archive directory. None of those are requirements. Skylight
Sync can run on another Linux system and store its archive on local
storage, a separate mounted disk, or a network share.

## 1. Prepare the Linux server

### Requirements

-   Linux
-   Python 3.11 or newer
-   Git
-   A dedicated archive destination with sufficient storage
-   Network access to Skylight and the archive destination
-   systemd, if using the included service and timer configuration

The pinned `pyskylight` dependency requires Python 3.11 or newer.

### Reference deployment

The following describes the environment used to develop and test this
deployment:

  Component          Configuration
  ------------------ --------------------------
  Operating system   Debian 13 (Trixie)
  Python             3.13.5
  CPU                2 cores
  Memory             2048 MiB
  Swap               512 MiB
  Root disk          8 GB
  Timezone           America/Chicago
  Service account    root
  Archive storage    NAS mounted through CIFS

These are reference deployment settings, not minimum hardware
requirements.

### Install system packages

On Debian:

``` bash
apt update
apt full-upgrade -y
apt install -y git python3 python3-venv python3-pip
```

If using a CIFS network share, also install:

``` bash
apt install -y cifs-utils
```

### Configure the timezone

Set the server's local timezone. This is used for archive filenames and
synchronization timestamps.

For example:

``` bash
timedatectl set-timezone America/Chicago
timedatectl
```

Use the timezone appropriate for your deployment.

## 2. Install Skylight Sync

Clone the application repository into `/opt/skylight-sync`:

``` bash
git clone https://github.com/JeffCrum1/skylight-sync.git /opt/skylight-sync
cd /opt/skylight-sync
```

Create a dedicated Python virtual environment:

``` bash
mkdir -p /opt/venvs
python3 -m venv /opt/venvs/skylight-sync
```

Install the application dependencies:

``` bash
/opt/venvs/skylight-sync/bin/python -m pip install --upgrade pip

/opt/venvs/skylight-sync/bin/python -m pip install httpx==0.28.1

/opt/venvs/skylight-sync/bin/python -m pip install \
  "git+https://github.com/joshuaswarren/pyskylight.git@69e4576b9035d71aacda9ade7a4afea05a663e94"
```

The `pyskylight` package is installed directly from its Git repository
at a specific commit because it is not published on PyPI.

## 3. Authenticate with Skylight

The Skylight CLI uses the `SKYLIGHT_EMAIL` and `SKYLIGHT_PASSWORD`
environment variables to log in and cache a session token.

Run the login as the same operating-system account that will run the
synchronization service.

The reference deployment uses root.

To enter credentials without placing the password in shell history:

``` bash
read -r -p "Skylight email: " SKYLIGHT_EMAIL
read -r -s -p "Skylight password: " SKYLIGHT_PASSWORD
echo
export SKYLIGHT_EMAIL SKYLIGHT_PASSWORD

/opt/venvs/skylight-sync/bin/skylight login

unset SKYLIGHT_EMAIL SKYLIGHT_PASSWORD
```

Verify the login and identify the available frames:

``` bash
/opt/venvs/skylight-sync/bin/skylight whoami
/opt/venvs/skylight-sync/bin/skylight frames
```

The application uses the cached session token during unattended
synchronization. It refreshes the token when necessary; an interactive
login is not required for each run.

**Important:** The service must be able to access the authentication
cache created during login. If you choose a dedicated service account
instead of root, perform the login as that account and ensure its
configuration and cache remain accessible to the service.

## 4. Choose and prepare archive storage

Choose a destination for the downloaded photos and videos.

Use a dedicated, initially empty directory for the archive. The
application maintains its own checkpoint and initialization state inside
this directory.

The destination does not have to be a NAS or network share.

### Option A: Local directory

For an existing Linux filesystem:

``` bash
mkdir -p /srv/skylight/Photos/Skylight
```

This is the simplest configuration when local storage has sufficient
capacity.

### Option B: Separate disk or mounted volume

Mount the storage volume at a stable path, for example:

``` text
/mnt/photos
```

Create the archive directory:

``` bash
mkdir -p /mnt/photos/Skylight
```

Make sure the filesystem is mounted and writable before running the
initial synchronization.

### Option C: NAS or network share

The reference deployment uses a CIFS mount to a NAS.

Install CIFS support if you have not already:

``` bash
apt install -y cifs-utils
```

Create a protected directory for the NAS credentials:

``` bash
mkdir -p /root/.smb
chmod 700 /root/.smb
```

Create `/root/.smb/skylight`:

``` text
username=YOUR_NAS_USERNAME
password=YOUR_NAS_PASSWORD
```

Protect the credentials file:

``` bash
chmod 600 /root/.smb/skylight
```

Create the mount point:

``` bash
mkdir -p /mnt/skylight
```

Add the following entry to `/etc/fstab`, adjusting the server address
and share as needed:

``` fstab
//192.168.0.100/multimedia /mnt/skylight cifs credentials=/root/.smb/skylight,uid=0,gid=0,file_mode=0664,dir_mode=0775,vers=3.0,_netdev,nofail 0 0
```

Mount the share:

``` bash
mount /mnt/skylight
```

Verify that it is mounted and writable:

``` bash
findmnt /mnt/skylight
touch /mnt/skylight/.skylight-write-test
rm /mnt/skylight/.skylight-write-test
```

Create the archive directory:

``` bash
mkdir -p /mnt/skylight/Photos/Skylight
```

#### Networking considerations

The reference Proxmox LXC uses ifupdown2 and requires the CIFS mount to
wait for `networking.service`.

In that environment, the following systemd mount drop-in was needed:

`/etc/systemd/system/mnt-skylight.mount.d/network.conf`

``` ini
[Unit]
Wants=networking.service
After=networking.service
```

Create the directory and file if your environment requires this
ordering:

``` bash
mkdir -p /etc/systemd/system/mnt-skylight.mount.d
```

After creating or changing the drop-in:

``` bash
systemctl daemon-reload
```

This networking adjustment is specific to the tested reference
environment. Other Linux systems may have different network
initialization requirements.

**Verify the mount after reboot before proceeding.** A successful manual
mount does not guarantee that a network share will be available when the
system starts.

## 5. Configure the application

Create the private configuration file from the supplied example:

``` bash
cd /opt/skylight-sync
cp config.example.py config.py
chmod 600 config.py
```

Edit `config.py`:

``` bash
nano /opt/skylight-sync/config.py
```

At minimum, configure:

-   `FRAME_ID` --- the ID of the Skylight frame to synchronize.
-   `TIMEZONE` --- the timezone used for archive filenames and
    timestamps.
-   `DESTINATION` --- the archive directory selected in the previous
    section.

For example:

``` python
FRAME_ID = "YOUR_FRAME_ID"
TIMEZONE = ZoneInfo("America/Chicago")
DESTINATION = Path("/mnt/skylight/Photos/Skylight")
CHECKPOINT = DESTINATION / ".skylight-sync.json"
```

The example configuration already defines the other application
settings, including request behavior, retries, and manifest lookback.

### Logging

The example configuration uses an XDG state directory under the service
account's home directory:

``` python
LOG_DIRECTORY = Path.home() / ".local" / "state" / "skylight-sync"
```

The reference deployment instead uses:

``` python
LOG_DIRECTORY = Path("/var/log/skylight-sync")
```

If using the reference setting, create the directory:

``` bash
mkdir -p /var/log/skylight-sync
```

Ensure the service account can write to it.

Logs rotate at local midnight, with 30 backup files retained.

### Home Assistant notifications (optional)

Skylight Sync can send failure notifications through a Home Assistant
webhook.

First, create a corresponding webhook automation in Home Assistant and
obtain its webhook ID.

Create a protected directory:

``` bash
mkdir -p /etc/skylight-sync
chmod 700 /etc/skylight-sync
```

Create `/etc/skylight-sync/webhook_id` containing only the webhook ID,
then protect it:

``` bash
chmod 600 /etc/skylight-sync/webhook_id
```

In `config.py`, enable notifications and configure the Home Assistant
URL and webhook file:

``` python
HA_NOTIFICATIONS_ENABLED = True
HA_URL = "http://homeassistant.local:8123"
HA_WEBHOOK_ID_FILE = Path("/etc/skylight-sync/webhook_id")
```

Use the actual Home Assistant URL for your installation.

If notifications are not needed, leave
`HA_NOTIFICATIONS_ENABLED = False`.

## 6. Configure the systemd service

Create:

`/etc/systemd/system/skylight-sync.service`

``` ini
[Unit]
Description=Skylight Photo and Video Synchronization
Wants=network-online.target
After=network-online.target
RequiresMountsFor=/mnt/skylight

[Service]
Type=oneshot
User=root
WorkingDirectory=/opt/skylight-sync
ExecStart=/opt/venvs/skylight-sync/bin/python /opt/skylight-sync/sync.py
```

If using a local directory that does not require a separate mount,
remove the `RequiresMountsFor` line.

If using a mounted disk or network share, change that line to the actual
mount path. This ensures systemd brings the required mount into the
service's dependency chain before starting synchronization.

The reference service runs as root. If using another service account,
change `User=root` and ensure that account has access to:

-   The application and virtual environment
-   `config.py`
-   The Skylight authentication cache
-   The archive destination
-   The log directory
-   The Home Assistant webhook ID file, if notifications are enabled

The dedicated-account arrangement is an adaptation; the tested reference
deployment uses root.

Reload systemd:

``` bash
systemctl daemon-reload
```

## 7. Run the initial synchronization

Before starting the service, verify that the archive destination is
mounted if applicable and writable.

Start the first synchronization manually:

``` bash
systemctl start skylight-sync.service
```

Check the service result:

``` bash
systemctl status skylight-sync.service
```

Review the journal:

``` bash
journalctl -u skylight-sync.service -n 100 --no-pager
```

A successful `Type=oneshot` service normally returns to:

``` text
inactive (dead)
```

That is expected. The process has completed; it is not a continuously
running service.

### Initial synchronization and recovery

On its first run, Skylight Sync establishes an initial checkpoint and
downloads the archive.

An initialization marker is used to identify an incomplete initial
synchronization. The application does not advance the checkpoint until
the final archive verification succeeds.

If an initial synchronization is interrupted, the initialization marker
allows the application to recognize the incomplete operation and resume
safely.

Do not manually delete the checkpoint or initialization marker as a
troubleshooting shortcut.

### Archive verification

At the end of a synchronization, the application verifies the archive
against the active Skylight manifest.

Verification includes:

-   Confirming that active manifest assets have corresponding archive
    files.
-   Checking that files are nonzero in size.
-   Checking JPEG and MP4 file signatures.
-   Checking expected filenames and asset keys.
-   Rejecting unexpected archive entries and duplicate active asset
    keys.

This is an archive consistency check, not a full media decode or
cryptographic checksum verification.

The checkpoint advances only after final verification succeeds.

## 8. Schedule automatic synchronization

The reference deployment runs once a week, Sunday at noon.

Create:

`/etc/systemd/system/skylight-sync.timer`

``` ini
[Unit]
Description=Weekly Skylight Synchronization Schedule

[Timer]
OnCalendar=Sun *-*-* 12:00:00
Persistent=true
Unit=skylight-sync.service

[Install]
WantedBy=timers.target
```

`Persistent=true` allows systemd to trigger a catch-up run if the timer
was inactive when its scheduled time passed.

It does not automatically retry a service execution that ran and failed.

Enable and start the timer:

``` bash
systemctl daemon-reload
systemctl enable --now skylight-sync.timer
```

Check its status:

``` bash
systemctl status skylight-sync.timer
systemctl list-timers skylight-sync.timer
```

To run synchronization manually at any time:

``` bash
systemctl start skylight-sync.service
```

## 9. Verify the deployment

Use this checklist after installation:

-   [ ] Python 3.11 or newer is installed.
-   [ ] Application dependencies are installed in the virtual
    environment.
-   [ ] Skylight login succeeds.
-   [ ] `skylight whoami` and `skylight frames` work.
-   [ ] `config.py` contains the correct frame, timezone, and
    destination.
-   [ ] The archive destination is writable.
-   [ ] Mounted storage survives a reboot, if applicable.
-   [ ] The systemd service completes successfully.
-   [ ] The final archive verification passes.
-   [ ] The systemd timer is enabled and active.
-   [ ] Optional Home Assistant failure notifications work.

## 10. Troubleshooting

### Authentication errors

Verify that the Skylight CLI can access the cached session:

``` bash
/opt/venvs/skylight-sync/bin/skylight whoami
```

If the session needs to be re-established, repeat the login procedure as
the service account.

### Storage or mount errors

Check whether the destination is mounted:

``` bash
findmnt /mnt/skylight
```

Check write access:

``` bash
touch /mnt/skylight/.skylight-write-test
rm /mnt/skylight/.skylight-write-test
```

Review mount errors:

``` bash
journalctl -b | grep -iE 'cifs|mount'
```

### Service errors

Review the service journal:

``` bash
journalctl -u skylight-sync.service -n 200 --no-pager
```

### Timer errors

Check timer status and the next scheduled run:

``` bash
systemctl status skylight-sync.timer
systemctl list-timers skylight-sync.timer
```

Remember that a timer's persistent catch-up behavior does not retry a
failed synchronization.

------------------------------------------------------------------------

**We move the pictures. We don't move mountains.**
