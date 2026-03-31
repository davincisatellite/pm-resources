# Dice payload boot automation

This setup automates the Linux side only: after Debian boots, systemd starts the payload camera script automatically.

## Files
- `DiceCamService.py` — Linux-ready capture script.
- `dicecam.service` — systemd unit that starts the script at boot.

## Assumptions
- `python3` is installed.
- A writable mount is available at `/opt/dicepayload`.
- The camera is available as OpenCV camera index `0`.
- OpenCV for Python is installed and `import cv2` works.

## Install
1. Create the target directory:
   - `sudo mkdir -p /opt/dicepayload/output`
2. Copy the Python file:
   - `sudo cp DiceCamService.py /opt/dicepayload/`
3. Set ownership:
   - `sudo chown -R debian:debian /opt/dicepayload`
4. Copy the service file:
   - `sudo cp dicecam.service /etc/systemd/system/`
5. Reload systemd:
   - `sudo systemctl daemon-reload`
6. Enable boot start:
   - `sudo systemctl enable dicecam.service`
7. Start once for validation:
   - `sudo systemctl start dicecam.service`

## Verify
- Service state:
  - `systemctl status dicecam.service`
- Boot logs for this service:
  - `journalctl -u dicecam.service -b`
- Output images:
  - `ls -lah /opt/dicepayload/output`

## Behavior
- The service waits for the camera and retries before failing.
- It captures one image per configured exposure value.
- Files are written to `/opt/dicepayload/output`.
- Logs go to journald.
- On failure, systemd restarts the service after 10 seconds.

## Disable
- Stop now:
  - `sudo systemctl stop dicecam.service`
- Disable on future boots:
  - `sudo systemctl disable dicecam.service`