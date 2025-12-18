## Payload Manager (PM)

This repository houses scripts and other resources related to the Payload Manager & Hyperion OBC.

## Lab Test Setup

Step by step setup procedure. Use Windows.

1. Connect USB1_DEVICE and USB_D8 to your computer with cables. For a pictoral representation refer to the manual _How do you connect the OBC to the Computer_ at the WebDrive Location: `/361K`.
2. Run the `START_OBC400.bat` script using the samba tools, located at `/3630` in the Webdrive.
3. Press the RESET_MAIN button.
4. Linux should now be booting.
5. Use PuTTy to open UART (use Device Manager to find the correct COM port). Set the Baud to 115200 for shell or 9600 for power statistics, with the connection type as Serial.
6. Login on the linux shell:
   - username: `debian`
   - password: `DaVinci21! `
