# The compiled C extension is named 'logdata_ext'
from logdata_ext import LogData

LOG_TYPE_BASIC = 0x00
LOG_TYPE_MEM = 0x01
LOG_TYPE_RES = 0x02
LOG_TYPE_PORT = 0x03
TARGET_DIGIT_SHIFT = 20

level2str = {
    '0': "INFO", '1': "TRACE ", '2': "WARN ", '3': "ERROR",
    '4': "FATAL", '5': "PANIC"
}

# The decoding logic including fndecode and all parsers has been moved to the C extension
# `logdatamodule.c` for performance.

__all__ = [
    'LogData', 'TARGET_DIGIT_SHIFT', 'LOG_TYPE_PORT', 'LOG_TYPE_BASIC',
    'LOG_TYPE_MEM', 'level2str'
]
