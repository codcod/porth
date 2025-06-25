"""SMS encoding utilities for Unicode and GSM."""

import logging
from typing import Tuple, List, Optional
from enum import Enum

logger = logging.getLogger(__name__)


class SMSEncoding(str, Enum):
    GSM_7BIT = 'gsm_7bit'
    UCS2 = 'ucs2'
    LATIN1 = 'latin1'


# GSM 7-bit alphabet
GSM_7BIT_CHARSET = (
    '@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !"#¤%&\'()*+,-./0123456789:;<=>?'
    '¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà'
)

# Extended GSM characters (ESC + char)
GSM_EXTENDED_CHARS = {
    '\f': '\x1b\f',  # Form Feed
    '^': '\x1b\x14',  # Circumflex
    '{': '\x1b\x28',  # Left Brace
    '}': '\x1b\x29',  # Right Brace
    '\\': '\x1b\x2f',  # Backslash
    '[': '\x1b\x3c',  # Left Bracket
    '~': '\x1b\x3d',  # Tilde
    ']': '\x1b\x3e',  # Right Bracket
    '|': '\x1b\x40',  # Pipe
    '€': '\x1b\x65',  # Euro
}


def detect_encoding(text: str) -> SMSEncoding:
    """Detect the best encoding for SMS text."""
    try:
        # Check if text can be encoded as GSM 7-bit
        if all(char in GSM_7BIT_CHARSET or char in GSM_EXTENDED_CHARS for char in text):
            return SMSEncoding.GSM_7BIT

        # Check if text can be encoded as Latin-1
        text.encode('latin1')
        return SMSEncoding.LATIN1

    except UnicodeEncodeError:
        # Fall back to UCS2 for Unicode characters
        return SMSEncoding.UCS2


def encode_sms_text(
    text: str, encoding: Optional[SMSEncoding] = None
) -> Tuple[bytes, SMSEncoding]:
    """Encode SMS text with appropriate encoding."""
    if encoding is None:
        encoding = detect_encoding(text)

    try:
        if encoding == SMSEncoding.GSM_7BIT:
            return encode_gsm_7bit(text), encoding
        elif encoding == SMSEncoding.LATIN1:
            return text.encode('latin1'), encoding
        elif encoding == SMSEncoding.UCS2:
            return text.encode('utf-16-be'), encoding
        else:
            raise ValueError(f'Unsupported encoding: {encoding}')

    except Exception as e:
        logger.error(f'Failed to encode SMS text: {e}')
        # Fall back to UCS2
        return text.encode('utf-16-be'), SMSEncoding.UCS2


def encode_gsm_7bit(text: str) -> bytes:
    """Encode text using GSM 7-bit alphabet."""
    result = []

    for char in text:
        if char in GSM_EXTENDED_CHARS:
            # Extended character - add escape sequence
            extended = GSM_EXTENDED_CHARS[char]
            for ext_char in extended:
                if ext_char in GSM_7BIT_CHARSET:
                    result.append(GSM_7BIT_CHARSET.index(ext_char))
        elif char in GSM_7BIT_CHARSET:
            result.append(GSM_7BIT_CHARSET.index(char))
        else:
            raise ValueError(f"Character '{char}' not in GSM 7-bit charset")

    return bytes(result)


def decode_gsm_7bit(data: bytes) -> str:
    """Decode GSM 7-bit encoded data."""
    result = []
    i = 0

    while i < len(data):
        char_code = data[i]

        # Check for escape character (0x1B)
        if char_code == 0x1B and i + 1 < len(data):
            # Extended character
            next_char = data[i + 1]
            extended_char = None

            # Find extended character
            for char, escape_seq in GSM_EXTENDED_CHARS.items():
                if len(escape_seq) == 2 and escape_seq[1] == chr(next_char):
                    extended_char = char
                    break

            if extended_char:
                result.append(extended_char)
                i += 2
            else:
                # Unknown extended character, use replacement
                result.append('?')
                i += 2
        else:
            # Regular character
            if char_code < len(GSM_7BIT_CHARSET):
                result.append(GSM_7BIT_CHARSET[char_code])
            else:
                result.append('?')
            i += 1

    return ''.join(result)


def calculate_sms_parts(
    text: str, encoding: Optional[SMSEncoding] = None
) -> Tuple[int, int]:
    """Calculate number of SMS parts and characters per part."""
    if encoding is None:
        encoding = detect_encoding(text)

    # SMS length limits
    if encoding == SMSEncoding.GSM_7BIT:
        single_sms_limit = 160
        concat_sms_limit = 153  # 7 bytes for UDH
    else:  # UCS2 or Latin1
        single_sms_limit = 70
        concat_sms_limit = 67  # 6 bytes for UDH

    text_length = len(text)

    if text_length <= single_sms_limit:
        return 1, text_length
    else:
        parts = (text_length + concat_sms_limit - 1) // concat_sms_limit
        return parts, concat_sms_limit


def split_sms_text(text: str, encoding: Optional[SMSEncoding] = None) -> List[str]:
    """Split SMS text into multiple parts if needed."""
    if encoding is None:
        encoding = detect_encoding(text)

    parts_count, chars_per_part = calculate_sms_parts(text, encoding)

    if parts_count == 1:
        return [text]

    # Split into parts
    parts = []
    for i in range(parts_count):
        start = i * chars_per_part
        end = start + chars_per_part
        parts.append(text[start:end])

    return parts
