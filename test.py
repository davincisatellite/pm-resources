import serial
import time

PORT = "/dev/cu.usbserial-0001"  # Note 'cu' here
BAUD = 115200

ser = serial.Serial(PORT, BAUD, timeout=2)
time.sleep(2)  # Wait for the port to initialize

ser.reset_input_buffer()
ser.reset_output_buffer()

print("Serial port opened:", ser.name)

ser.write(b"ls -l\n")
time.sleep(1)

response = ser.read(ser.in_waiting or 1)  # Read whatever is available
print("Response:", response.decode(errors="ignore"))

ser.close()