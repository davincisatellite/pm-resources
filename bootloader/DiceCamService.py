import cv2
import time
from pathlib import Path
from datetime import datetime

BASE_DIR = Path('/opt/dicepayload')
OUTPUT_DIR = BASE_DIR / 'output'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


class IMX179Controller:
    def __init__(self, camera_index=0):
        self.camera_index = camera_index
        self.cap = None
        self.supported_controls = {}

    def connect(self):
        self.cap = cv2.VideoCapture(self.camera_index)
        if not self.cap.isOpened():
            raise RuntimeError(f'Cannot open camera at index {self.camera_index}')
        print('IMX179 camera connected')
        self._discover_controls()
        return True

    def _discover_controls(self):
        controls = {
            cv2.CAP_PROP_BRIGHTNESS: 'Brightness',
            cv2.CAP_PROP_CONTRAST: 'Contrast',
            cv2.CAP_PROP_SATURATION: 'Saturation',
            cv2.CAP_PROP_HUE: 'Hue',
            cv2.CAP_PROP_GAIN: 'Gain',
            cv2.CAP_PROP_EXPOSURE: 'Exposure',
            cv2.CAP_PROP_AUTO_EXPOSURE: 'Auto Exposure',
            cv2.CAP_PROP_FOCUS: 'Focus',
            cv2.CAP_PROP_AUTOFOCUS: 'Auto Focus',
            cv2.CAP_PROP_WHITE_BALANCE_BLUE_U: 'White Balance',
            cv2.CAP_PROP_FPS: 'FPS',
            cv2.CAP_PROP_SHARPNESS: 'Sharpness',
            cv2.CAP_PROP_GAMMA: 'Gamma',
            cv2.CAP_PROP_BACKLIGHT: 'Backlight Compensation',
        }
        self.supported_controls = {}
        for prop_id, name in controls.items():
            try:
                value = self.cap.get(prop_id)
                if value != -1 and value is not None:
                    self.supported_controls[name] = prop_id
                    print(f'{name}: {value} (supported)')
            except Exception:
                pass
        return self.supported_controls

    def set_resolution(self, width, height):
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        actual_width = self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        actual_height = self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        print(f'Resolution set to {actual_width}x{actual_height}')
        return actual_width, actual_height

    def set_fps(self, fps):
        self.cap.set(cv2.CAP_PROP_FPS, fps)
        actual_fps = self.cap.get(cv2.CAP_PROP_FPS)
        print(f'FPS set to {actual_fps}')
        return actual_fps

    def set_autofocus(self, enable):
        if 'Auto Focus' in self.supported_controls:
            self.cap.set(cv2.CAP_PROP_AUTOFOCUS, 1 if enable else 0)
            print(f'Autofocus {"enabled" if enable else "disabled"}')
            return True
        print('Autofocus not supported')
        return False

    def set_focus(self, focus_value):
        if 'Focus' in self.supported_controls:
            self.set_autofocus(False)
            time.sleep(0.1)
            self.cap.set(cv2.CAP_PROP_FOCUS, focus_value)
            actual_focus = self.cap.get(cv2.CAP_PROP_FOCUS)
            print(f'Focus set to {actual_focus}')
            return actual_focus
        print('Manual focus not supported')
        return None

    def set_auto_exposure(self, enable):
        if 'Auto Exposure' in self.supported_controls:
            self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1 if enable else 0)
            print(f'Auto exposure {"enabled" if enable else "disabled"}')
            return True
        print('Auto exposure control not supported')
        return False

    def set_exposure(self, exposure_value):
        if 'Exposure' in self.supported_controls:
            self.set_auto_exposure(False)
            time.sleep(0.1)
            self.cap.set(cv2.CAP_PROP_EXPOSURE, exposure_value)
            actual_exposure = self.cap.get(cv2.CAP_PROP_EXPOSURE)
            print(f'Exposure set to {actual_exposure}')
            return actual_exposure
        print('Manual exposure not supported')
        return None

    def set_brightness(self, value):
        if 'Brightness' in self.supported_controls:
            self.cap.set(cv2.CAP_PROP_BRIGHTNESS, value)
            return self.cap.get(cv2.CAP_PROP_BRIGHTNESS)
        return None

    def set_contrast(self, value):
        if 'Contrast' in self.supported_controls:
            self.cap.set(cv2.CAP_PROP_CONTRAST, value)
            return self.cap.get(cv2.CAP_PROP_CONTRAST)
        return None

    def set_gain(self, value):
        if 'Gain' in self.supported_controls:
            self.cap.set(cv2.CAP_PROP_GAIN, value)
            return self.cap.get(cv2.CAP_PROP_GAIN)
        return None

    def set_white_balance(self, value):
        if 'White Balance' in self.supported_controls:
            self.cap.set(cv2.CAP_PROP_WHITE_BALANCE_BLUE_U, value)
            return self.cap.get(cv2.CAP_PROP_WHITE_BALANCE_BLUE_U)
        return None

    def set_sharpness(self, value):
        if 'Sharpness' in self.supported_controls:
            self.cap.set(cv2.CAP_PROP_SHARPNESS, value)
            return self.cap.get(cv2.CAP_PROP_SHARPNESS)
        return None

    def capture_frame(self):
        ret, frame = self.cap.read()
        if not ret:
            raise RuntimeError('Failed to capture frame')
        return frame

    def disconnect(self):
        if self.cap:
            self.cap.release()
            print('Camera disconnected')


def wait_for_camera(camera_index=0, retries=15, delay=2):
    last_error = None
    for attempt in range(1, retries + 1):
        cam = IMX179Controller(camera_index)
        try:
            cam.connect()
            print(f'Camera ready on attempt {attempt}')
            return cam
        except Exception as exc:
            last_error = exc
            print(f'Camera not ready on attempt {attempt}/{retries}: {exc}')
            cam.disconnect()
            time.sleep(delay)
    raise RuntimeError(f'Camera failed to become ready: {last_error}')


def main():
    exposures = [-10, -9, -8, -7, -6, -5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 20]
    number_of_lights = '3'
    lighting = 'DARK'
    lens = 'BLUE'
    led_cap = 'WITHOUT'
    led_delay = '3ms'

    cam = wait_for_camera(camera_index=0, retries=15, delay=2)
    try:
        cam.set_resolution(3264, 2448) # 8MP native
        # cam.set_resolution(1920, 1080)  # 1080p
        # cam.set_resolution(1280, 720)   # 720p
        cam.set_fps(30)
        cam.set_autofocus(True) # Enable autofocus
        # cam.set_focus(00)       # Or set manual focus
        cam.set_brightness(50)
        cam.set_contrast(50)
        cam.set_sharpness(50)

        run_stamp = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        print(f'Starting capture run {run_stamp}')

        for expo in exposures:
            cam.set_exposure(expo)
            time.sleep(0.5)
            frame = cam.capture_frame()
            filename = OUTPUT_DIR / (
                f'{run_stamp}_{lighting}_lens{lens}_exposure{expo}_'
                f'lights{number_of_lights}LEDdelay{led_delay}_{led_cap}cap.jpg'
            )
            ok = cv2.imwrite(str(filename), frame)
            if not ok:
                raise RuntimeError(f'Failed to write image {filename}')
            print(f'Saved {filename}')

        print('Capture run completed successfully')
    finally:
        cam.disconnect()


if __name__ == '__main__':
    main()