class SerialConfig:
    BAUD_RATE = 115200
    TTY_PORT = "/dev/ttyS0"
    BYTE_CHUNK_SIZE = 1024
    TIMEOUT = 256
    ENCODING = 'utf-8'

class DiceConfig:
    ADDRESS = 0x01 # TBD
    
    # Command definitions
    CMD_STATUS = 0x00
    CMD_R_M1_SPEED = 0x01
    CMD_R_M2_SPEED = 0x02
    CMD_R_M1_LENGTH = 0x03
    CMD_R_M2_LENGTH = 0x04
    CMD_R_M1_POSITION = 0x05
    CMD_R_LED_BRIGHTNESS = 0x06
    CMD_R_LED_STATUS = 0x07
    CMD_W_M1_SPEED = 0x08
    CMD_W_M2_SPEED = 0x09
    CMD_W_M1_LENGTH = 0x0A
    CMD_W_M2_LENGTH = 0x0B
    CMD_W_LED_BRIGHTNESS = 0x0C
    CMD_R_PHOTO_SENSOR = 0x0D  # Not implemented
    CMD_R_TEMP_SENSOR = 0x0E   # Not implemented
    CMD_R_SWITCHES = 0x0F      # Not implemented
    
    CMD_STOP_M1_M2 = 0x10
    CMD_RUN_M1_CW = 0x12
    CMD_RUN_M1_CCW = 0x13
    CMD_RUN_M2_CW = 0x18
    CMD_RUN_M2_CCW = 0x1C
    CMD_RUN_M1_CW_M2_CW = 0x1A
    CMD_RUN_M1_CCW_M2_CW = 0x1B
    CMD_RUN_M1_CW_M2_CCW = 0x1E
    CMD_RUN_M1_CCW_M2_CCW = 0x1F
    
    CMD_LED_ON = 0x20
    CMD_LED_OFF = 0x21
    CMD_CLAMP = 0x22
    CMD_UNCLAMP = 0x23
    
    CMD_W_DISABLE_SWITCH_ALL = 0xE0
    CMD_W_ENABLE_SWITCH_ALL = 0xEF
    CMD_RESET = 0xF2
    
    # Status return values
    STATUS_INIT = 0x00
    STATUS_OK = 0x01
    STATUS_FAIL = 0xF1
    
    # Motor position values
    POS_UNKNOWN = 0x00
    POS_CLAMPED = 0x01
    POS_UNCLAMPED = 0x02