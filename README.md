# Payload Manager

This repository houses scripts and other resources related to the Payload Manager & Hyperion OBC.

## What it does

- Runs `payload_manager.py` as the `debian` user under systemd.
- Handles UART commands for DICE, telemetry, camera, and bootloader provisioning.
- Delegates bootloader changes to the root-owned helper `provision_boot.sh`.
- Supports both `grub` and `uboot` targets through `payload_manager.yaml`.

## Deploy on the board

1. Copy this directory to the board, then run:
   - `sudo ./install.sh`
2. Confirm the helper and sudo rule were installed:
   - `ls -l /opt/dvs/provision_boot.sh`
   - `sudo -l -U debian | grep provision_boot`
3. Confirm the service is running:
   - `systemctl status dvs-payload.service`

## UART command

Send this over the payload UART:

- `OBC_PROVISION_BOOT`

Expected responses:

- `OK BOOT_PROVISIONED`
- `OK BOOT_ALREADY_AUTONOMOUS`
- `ERR elevated permissions failed`
- `ERR provisioning failed`

The command reads the bootloader section in `payload_manager.yaml`, then calls `/opt/dvs/provision_boot.sh` via the narrow sudoers rule installed by `install.sh`.

## Validate on the board

### GRUB target

1. Set `bootloader.target_type: grub` in `payload_manager.yaml`.
2. Make sure `/etc/default/grub` exists.
3. Send `OBC_PROVISION_BOOT` over UART.
4. Verify the result:
   - `grep -E '^GRUB_TIMEOUT=0$|^GRUB_TIMEOUT_STYLE=hidden$' /etc/default/grub`
   - `sudo update-grub`
   - `journalctl -u dvs-payload.service -b | tail -50`

### U-Boot target

1. Set `bootloader.target_type: uboot` in `payload_manager.yaml`.
2. Make sure `fw_setenv` and `fw_printenv` are installed.
3. Send `OBC_PROVISION_BOOT` over UART.
4. Verify the result:
   - `fw_printenv bootdelay`
   - `journalctl -u dvs-payload.service -b | tail -50`

## Manual helper test

Run the helper directly as root for a dry validation of the privileged path:

- `sudo /opt/dvs/provision_boot.sh --target grub --dry-run`
- `sudo /opt/dvs/provision_boot.sh --target uboot --dry-run`

## Runtime log locations

- File log: `/var/log/payload_manager.log`
- systemd journal: `journalctl -u dvs-payload.service`

## Re-run after changes

- `sudo systemctl restart dvs-payload.service`
- `journalctl -u dvs-payload.service -f`
