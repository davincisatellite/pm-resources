#!/usr/bin/env python3
"""
Serial file transfer utility - sends a file to Linux via COM8 serial connection.
Usage: python transfer_file.py <COM_PORT> <LOCAL_FILE> <REMOTE_FILEPATH>
Example: python transfer_file.py COM8 DicecamService.py /opt/dicepayload/DicecamService.py
"""

import serial
import time
import base64
import sys
import os
from pathlib import Path

def transfer_file(com_port, local_file, remote_path, baudrate=115200, timeout=5):
    """Transfer file to Linux system via serial connection."""
    
    # Read local file
    local_path = Path(local_file)
    if not local_path.exists():
        print(f"Error: File not found: {local_file}")
        return False
    
    print(f"Reading {local_file}...")
    file_content = local_path.read_bytes()
    file_b64 = base64.b64encode(file_content).decode('ascii')
    
    print(f"File size: {len(file_content)} bytes")
    print(f"Encoded size: {len(file_b64)} bytes")
    
    try:
        # Open serial connection
        print(f"Opening {com_port} at {baudrate} baud...")
        ser = serial.Serial(com_port, baudrate=baudrate, timeout=timeout)
        time.sleep(1)  # Wait for connection to settle
        
        # Clear any pending input
        ser.reset_input_buffer()
        
        # Step 1: Create the file using base64 decode
        print(f"Sending file to {remote_path}...")
        
        # Send command to create file from base64
        cmd = f"echo '{file_b64}' | base64 -d > {remote_path}\n"
        print(f"Command length: {len(cmd)} chars")
        
        ser.write(cmd.encode('utf-8'))
        time.sleep(2)
        
        # Read response
        response = ser.read_all().decode('utf-8', errors='ignore')
        if response:
            print(f"Response: {response[:200]}")
        
        # Verify file was created
        print(f"Verifying file...")
        verify_cmd = f"ls -lh {remote_path}\n"
        ser.write(verify_cmd.encode('utf-8'))
        time.sleep(1)
        response = ser.read_all().decode('utf-8', errors='ignore')
        print(f"Verification: {response}")
        
        ser.close()
        print("Transfer complete!")
        return True
        
    except serial.SerialException as e:
        print(f"Serial error: {e}")
        return False
    except Exception as e:
        print(f"Error: {e}")
        return False


def transfer_file_via_cat(com_port, local_file, remote_path, baudrate=115200, timeout=5):
    """Alternative method using cat with heredoc."""
    
    local_path = Path(local_file)
    if not local_path.exists():
        print(f"Error: File not found: {local_file}")
        return False
    
    print(f"Reading {local_file}...")
    file_content = local_path.read_text()
    
    try:
        print(f"Opening {com_port} at {baudrate} baud...")
        ser = serial.Serial(com_port, baudrate=baudrate, timeout=timeout)
        time.sleep(1)
        ser.reset_input_buffer()
        
        # Use cat with heredoc
        print(f"Creating {remote_path}...")
        delimiter = "EOF_TRANSFER"
        cmd = f"cat > {remote_path} << '{delimiter}'\n"
        
        ser.write(cmd.encode('utf-8'))
        time.sleep(0.5)
        
        # Write file content line by line
        for i, line in enumerate(file_content.split('\n'), 1):
            ser.write((line + '\n').encode('utf-8'))
            if i % 20 == 0:
                print(f"Sent {i} lines...")
            time.sleep(0.01)
        
        # End heredoc
        ser.write(f"{delimiter}\n".encode('utf-8'))
        time.sleep(1)
        
        response = ser.read_all().decode('utf-8', errors='ignore')
        if response:
            print(f"Response: {response}")
        
        # Verify
        print(f"Verifying...")
        ser.write(f"wc -l {remote_path}\n".encode('utf-8'))
        time.sleep(1)
        response = ser.read_all().decode('utf-8', errors='ignore')
        print(f"File info: {response}")
        
        ser.close()
        print("Transfer complete!")
        return True
        
    except serial.SerialException as e:
        print(f"Serial error: {e}")
        return False
    except Exception as e:
        print(f"Error: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python transfer_file.py <COM_PORT> <LOCAL_FILE> [REMOTE_PATH] [--method base64|cat]")
        print("Example: python transfer_file.py COM8 DicecamService.py /opt/dicepayload/DicecamService.py")
        sys.exit(1)
    
    com_port = sys.argv[1]
    local_file = sys.argv[2]
    remote_path = sys.argv[3] if len(sys.argv) > 3 else f"/tmp/{Path(local_file).name}"
    method = "cat"  # Default to cat method (more reliable)
    
    if "--method" in sys.argv:
        method = sys.argv[sys.argv.index("--method") + 1]
    
    print(f"=== Serial File Transfer ===")
    print(f"Port: {com_port}")
    print(f"Local: {local_file}")
    print(f"Remote: {remote_path}")
    print(f"Method: {method}")
    print()
    
    if method == "base64":
        success = transfer_file(com_port, local_file, remote_path)
    else:
        success = transfer_file_via_cat(com_port, local_file, remote_path)
    
    sys.exit(0 if success else 1)
