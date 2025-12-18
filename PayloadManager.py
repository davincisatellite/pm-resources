import serial
import subprocess
import shlex
import time
from Constants import SerialConfig

def run_command(command_str: str) -> bytes:
    
    try:
        command_parts = shlex.split(command_str)
    except ValueError:
        return b"Error: Invalid command format."

    if not command_parts:
        return b"Empty command received."

    try:
        result = subprocess.run(command_parts, capture_output=True, text=True, timeout=10)
        
        # Combine stdout and stderr to send everything back to the user
        if result.stdout and result.stderr:
            return f"STDOUT\n{result.stdout}\nSTDERR\n{result.stderr}".encode(SerialConfig.ENCODING)
        elif result.stdout:
            return result.stdout.encode(SerialConfig.ENCODING)
        elif result.stderr:
            return result.stderr.encode(SerialConfig.ENCODING)
        else:
            return b"No output"

    except FileNotFoundError:
        return f"Error: Command not found: {command_parts[0]}".encode(SerialConfig.ENCODING)
    except subprocess.TimeoutExpired:
        return b"Error: Command timed out."
    except Exception as e:
        return f"An error occurred: {e}".encode(SerialConfig.ENCODING)


def main():
    ser = None
    try:
        ser = serial.Serial(SerialConfig.TTY_PORT, SerialConfig.BAUD_RATE, timeout=SerialConfig.TIMEOUT)
        time.sleep(1) 
        ser.reset_input_buffer()


        # listener loop
        while True:
            command_bytes = ser.readline()
            
            if not command_bytes:
                continue

            command_str = command_bytes.decode(SerialConfig.ENCODING).strip()

            if command_str:
                output_bytes = run_command(command_str)

                # Send the output back 
                if output_bytes:
                    ser.write(output_bytes)
                    ser.write(b'\n') 
                    ser.flush()

            time.sleep(1) 
    except serial.SerialException as e:
        print(f"Serial error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
    finally:
        if ser and ser.is_open:
            ser.close()
            print("Serial port closed.")


if __name__ == "__main__":
    main()