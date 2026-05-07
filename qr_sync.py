from __future__ import annotations

import struct
import zlib
from pathlib import Path


ECC_LOW = 0

_ECC_CODEWORDS_PER_BLOCK = (
    (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
)

_NUM_ERROR_CORRECTION_BLOCKS = (
    (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
)

_FORMAT_BITS = (1, 0, 3, 2)


class BitBuffer:
    def __init__(self) -> None:
        self.bits: list[int] = []

    def append_bits(self, value: int, length: int) -> None:
        if length < 0 or value >> length:
            raise ValueError("value does not fit in bit length")
        for shift in range(length - 1, -1, -1):
            self.bits.append((value >> shift) & 1)

    def __len__(self) -> int:
        return len(self.bits)

    def to_bytes(self) -> bytes:
        result = bytearray()
        for index in range(0, len(self.bits), 8):
            value = 0
            for bit in self.bits[index : index + 8]:
                value = (value << 1) | bit
            result.append(value)
        return bytes(result)


def make_qr_matrix(text: str, ecl: int = ECC_LOW) -> list[list[bool]]:
    data = text.encode("utf-8")
    version = _choose_version(data, ecl)
    data_codewords = _encode_data(data, version, ecl)
    all_codewords = _add_error_correction(data_codewords, version, ecl)
    return _QrMatrix(version, ecl, all_codewords).modules


def write_qr_png(text: str, path: Path, scale: int = 14, border: int = 4) -> None:
    matrix = make_qr_matrix(text)
    size = len(matrix)
    pixel_size = (size + border * 2) * scale
    rows: list[bytes] = []

    for pixel_y in range(pixel_size):
        module_y = (pixel_y // scale) - border
        row = bytearray(pixel_size)
        for pixel_x in range(pixel_size):
            module_x = (pixel_x // scale) - border
            dark = 0 <= module_x < size and 0 <= module_y < size and matrix[module_y][module_x]
            row[pixel_x] = 0 if dark else 255
        rows.append(bytes(row))

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_bytes(_png_bytes(pixel_size, pixel_size, rows))
    temp_path.replace(path)


def _choose_version(data: bytes, ecl: int) -> int:
    for version in range(1, 41):
        capacity_bits = _num_data_codewords(version, ecl) * 8
        char_count_bits = 8 if version <= 9 else 16
        if 4 + char_count_bits + len(data) * 8 <= capacity_bits:
            return version
    raise ValueError("QR payload is too large")


def _encode_data(data: bytes, version: int, ecl: int) -> bytes:
    capacity_bits = _num_data_codewords(version, ecl) * 8
    char_count_bits = 8 if version <= 9 else 16
    buffer = BitBuffer()
    buffer.append_bits(0b0100, 4)
    buffer.append_bits(len(data), char_count_bits)
    for byte in data:
        buffer.append_bits(byte, 8)

    buffer.append_bits(0, min(4, capacity_bits - len(buffer)))
    while len(buffer) % 8:
        buffer.append_bits(0, 1)

    pad_byte = 0xEC
    while len(buffer) < capacity_bits:
        buffer.append_bits(pad_byte, 8)
        pad_byte ^= 0xEC ^ 0x11

    return buffer.to_bytes()


def _add_error_correction(data: bytes, version: int, ecl: int) -> bytes:
    raw_codewords = _num_raw_data_modules(version) // 8
    num_blocks = _NUM_ERROR_CORRECTION_BLOCKS[ecl][version]
    block_ecc_len = _ECC_CODEWORDS_PER_BLOCK[ecl][version]
    short_block_data_len = raw_codewords // num_blocks - block_ecc_len
    num_short_blocks = num_blocks - raw_codewords % num_blocks
    divisor = _reed_solomon_divisor(block_ecc_len)

    blocks: list[bytes] = []
    offset = 0
    for block_index in range(num_blocks):
        data_len = short_block_data_len + (0 if block_index < num_short_blocks else 1)
        block_data = data[offset : offset + data_len]
        offset += data_len
        ecc = _reed_solomon_remainder(block_data, divisor)
        if block_index < num_short_blocks:
            block_data += b"\0"
        blocks.append(block_data + ecc)

    result = bytearray()
    for byte_index in range(len(blocks[0])):
        for block_index, block in enumerate(blocks):
            if byte_index == short_block_data_len and block_index < num_short_blocks:
                continue
            result.append(block[byte_index])
    return bytes(result)


def _num_data_codewords(version: int, ecl: int) -> int:
    return _num_raw_data_modules(version) // 8 - _ECC_CODEWORDS_PER_BLOCK[ecl][version] * _NUM_ERROR_CORRECTION_BLOCKS[ecl][version]


def _num_raw_data_modules(version: int) -> int:
    result = (16 * version + 128) * version + 64
    if version >= 2:
        num_align = version // 7 + 2
        result -= (25 * num_align - 10) * num_align - 55
        if version >= 7:
            result -= 36
    return result


def _reed_solomon_divisor(degree: int) -> bytes:
    result = bytearray([0] * (degree - 1) + [1])
    root = 1
    for _ in range(degree):
        for index in range(degree):
            result[index] = _reed_solomon_multiply(result[index], root)
            if index + 1 < degree:
                result[index] ^= result[index + 1]
        root = _reed_solomon_multiply(root, 0x02)
    return bytes(result)


def _reed_solomon_remainder(data: bytes, divisor: bytes) -> bytes:
    result = bytearray(len(divisor))
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for index, coefficient in enumerate(divisor):
            result[index] ^= _reed_solomon_multiply(coefficient, factor)
    return bytes(result)


def _reed_solomon_multiply(x: int, y: int) -> int:
    result = 0
    for shift in range(7, -1, -1):
        result = (result << 1) ^ ((result >> 7) * 0x11D)
        result ^= ((y >> shift) & 1) * x
    return result


class _QrMatrix:
    def __init__(self, version: int, ecl: int, codewords: bytes):
        self.version = version
        self.ecl = ecl
        self.size = version * 4 + 17
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.is_function = [[False] * self.size for _ in range(self.size)]

        self._draw_function_patterns()
        self._draw_codewords(codewords)
        mask = self._best_mask()
        self._apply_mask(mask)
        self._draw_format_bits(mask)
        if self.version >= 7:
            self._draw_version_bits()

    def _set_function(self, x: int, y: int, dark: bool) -> None:
        if 0 <= x < self.size and 0 <= y < self.size:
            self.modules[y][x] = dark
            self.is_function[y][x] = True

    def _draw_function_patterns(self) -> None:
        for i in range(self.size):
            self._set_function(6, i, i % 2 == 0)
            self._set_function(i, 6, i % 2 == 0)

        self._draw_finder_pattern(3, 3)
        self._draw_finder_pattern(self.size - 4, 3)
        self._draw_finder_pattern(3, self.size - 4)

        positions = self._alignment_positions()
        for x in positions:
            for y in positions:
                if (x == 6 and y == 6) or (x == 6 and y == self.size - 7) or (x == self.size - 7 and y == 6):
                    continue
                self._draw_alignment_pattern(x, y)

        self._draw_format_bits(0)
        if self.version >= 7:
            self._draw_version_bits()

    def _draw_finder_pattern(self, center_x: int, center_y: int) -> None:
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                distance = max(abs(dx), abs(dy))
                self._set_function(center_x + dx, center_y + dy, distance != 2 and distance != 4)

    def _draw_alignment_pattern(self, center_x: int, center_y: int) -> None:
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                self._set_function(center_x + dx, center_y + dy, max(abs(dx), abs(dy)) != 1)

    def _alignment_positions(self) -> list[int]:
        if self.version == 1:
            return []
        num_align = self.version // 7 + 2
        step = 26 if self.version == 32 else ((self.version * 4 + num_align * 2 + 1) // (num_align * 2 - 2)) * 2
        result = [6]
        position = self.size - 7
        for _ in range(num_align - 1):
            result.insert(1, position)
            position -= step
        return result

    def _draw_format_bits(self, mask: int) -> None:
        data = (_FORMAT_BITS[self.ecl] << 3) | mask
        remainder = data
        for _ in range(10):
            remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
        bits = ((data << 10) | remainder) ^ 0x5412

        for i in range(6):
            self._set_function(8, i, _bit(bits, i))
        self._set_function(8, 7, _bit(bits, 6))
        self._set_function(8, 8, _bit(bits, 7))
        self._set_function(7, 8, _bit(bits, 8))
        for i in range(9, 15):
            self._set_function(14 - i, 8, _bit(bits, i))

        for i in range(8):
            self._set_function(self.size - 1 - i, 8, _bit(bits, i))
        for i in range(8, 15):
            self._set_function(8, self.size - 15 + i, _bit(bits, i))
        self._set_function(8, self.size - 8, True)

    def _draw_version_bits(self) -> None:
        remainder = self.version
        for _ in range(12):
            remainder = (remainder << 1) ^ ((remainder >> 11) * 0x1F25)
        bits = (self.version << 12) | remainder
        for i in range(18):
            dark = _bit(bits, i)
            x = self.size - 11 + i % 3
            y = i // 3
            self._set_function(x, y, dark)
            self._set_function(y, x, dark)

    def _draw_codewords(self, codewords: bytes) -> None:
        bit_index = 0
        total_bits = len(codewords) * 8
        right = self.size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(self.size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = self.size - 1 - vert if upward else vert
                    if self.is_function[y][x]:
                        continue
                    dark = bit_index < total_bits and _bit(codewords[bit_index >> 3], 7 - (bit_index & 7))
                    self.modules[y][x] = dark
                    bit_index += 1
            right -= 2

    def _best_mask(self) -> int:
        best_mask = 0
        best_penalty = 1 << 62
        original = [row[:] for row in self.modules]
        for mask in range(8):
            self.modules = [row[:] for row in original]
            self._apply_mask(mask)
            self._draw_format_bits(mask)
            penalty = self._penalty_score()
            if penalty < best_penalty:
                best_mask = mask
                best_penalty = penalty
        self.modules = original
        return best_mask

    def _apply_mask(self, mask: int) -> None:
        for y in range(self.size):
            for x in range(self.size):
                if not self.is_function[y][x] and _mask_bit(mask, x, y):
                    self.modules[y][x] = not self.modules[y][x]

    def _penalty_score(self) -> int:
        result = 0
        for rows in (self.modules, _columns(self.modules)):
            for row in rows:
                run_color = row[0]
                run_len = 1
                for color in row[1:]:
                    if color == run_color:
                        run_len += 1
                    else:
                        if run_len >= 5:
                            result += 3 + run_len - 5
                        run_color = color
                        run_len = 1
                if run_len >= 5:
                    result += 3 + run_len - 5

                for index in range(len(row) - 6):
                    if row[index : index + 7] == [True, False, True, True, True, False, True]:
                        before = index >= 4 and row[index - 4 : index] == [False] * 4
                        after = index + 11 <= len(row) and row[index + 7 : index + 11] == [False] * 4
                        if before or after:
                            result += 40

        for y in range(self.size - 1):
            for x in range(self.size - 1):
                color = self.modules[y][x]
                if (
                    color == self.modules[y][x + 1]
                    and color == self.modules[y + 1][x]
                    and color == self.modules[y + 1][x + 1]
                ):
                    result += 3

        dark = sum(1 for row in self.modules for value in row if value)
        total = self.size * self.size
        k = abs(dark * 20 - total * 10) // total
        result += k * 10
        return result


def _mask_bit(mask: int, x: int, y: int) -> bool:
    if mask == 0:
        return (x + y) % 2 == 0
    if mask == 1:
        return y % 2 == 0
    if mask == 2:
        return x % 3 == 0
    if mask == 3:
        return (x + y) % 3 == 0
    if mask == 4:
        return (x // 3 + y // 2) % 2 == 0
    if mask == 5:
        return (x * y) % 2 + (x * y) % 3 == 0
    if mask == 6:
        return ((x * y) % 2 + (x * y) % 3) % 2 == 0
    if mask == 7:
        return ((x + y) % 2 + (x * y) % 3) % 2 == 0
    raise ValueError("mask out of range")


def _columns(matrix: list[list[bool]]) -> list[list[bool]]:
    return [[matrix[y][x] for y in range(len(matrix))] for x in range(len(matrix))]


def _bit(value: int, index: int) -> bool:
    return ((value >> index) & 1) != 0


def _png_bytes(width: int, height: int, rows: list[bytes]) -> bytes:
    def chunk(name: bytes, data: bytes) -> bytes:
        body = name + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\0" + row for row in rows)
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)),
            chunk(b"IDAT", zlib.compress(raw, 9)),
            chunk(b"IEND", b""),
        )
    )
