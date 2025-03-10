# Copyright (c) "Neo4j"
# Neo4j Sweden AB [https://neo4j.com]
#
# This file is part of Neo4j.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import struct
from io import BytesIO
from math import pi
from uuid import uuid4

import pytest

from neo4j._codec.packstream import Structure
from neo4j._codec.packstream.v1 import (
    PackableBuffer,
    Packer,
    UnpackableBuffer,
    Unpacker,
)


standard_ascii = [chr(i) for i in range(128)]
not_ascii = "♥O◘♦♥O◘♦"


class TestPackStream:
    @pytest.fixture
    def packer_with_buffer(self):
        packable_buffer = Packer.new_packable_buffer()
        return Packer(packable_buffer), packable_buffer

    @pytest.fixture
    def unpacker_with_buffer(self):
        unpackable_buffer = Unpacker.new_unpackable_buffer()
        return Unpacker(unpackable_buffer), unpackable_buffer

    def test_packable_buffer(self, packer_with_buffer):
        packer, packable_buffer = packer_with_buffer
        assert isinstance(packable_buffer, PackableBuffer)
        assert packable_buffer is packer.stream

    def test_unpackable_buffer(self, unpacker_with_buffer):
        unpacker, unpackable_buffer = unpacker_with_buffer
        assert isinstance(unpackable_buffer, UnpackableBuffer)
        assert unpackable_buffer is unpacker.unpackable

    @pytest.fixture
    def pack(self, packer_with_buffer):
        packer, packable_buffer = packer_with_buffer

        def _pack(*values, dehydration_hooks=None):
            for value in values:
                packer.pack(value, dehydration_hooks=dehydration_hooks)
            data = bytearray(packable_buffer.data)
            packable_buffer.clear()
            return data

        return _pack

    @pytest.fixture
    def assert_packable(self, packer_with_buffer, unpacker_with_buffer):
        def _assert(value, packed_value):
            nonlocal packer_with_buffer, unpacker_with_buffer
            packer, packable_buffer = packer_with_buffer
            unpacker, unpackable_buffer = unpacker_with_buffer
            packable_buffer.clear()
            unpackable_buffer.reset()

            packer.pack(value)
            packed_data = packable_buffer.data
            assert packed_data == packed_value

            unpackable_buffer.data = bytearray(packed_data)
            unpackable_buffer.used = len(packed_data)
            unpacked_data = unpacker.unpack()
            assert unpacked_data == value

        return _assert

    def test_none(self, assert_packable):
        assert_packable(None, b"\xC0")

    def test_boolean(self, assert_packable):
        assert_packable(True, b"\xC3")
        assert_packable(False, b"\xC2")

    def test_negative_tiny_int(self, assert_packable):
        for z in range(-16, 0):
            assert_packable(z, bytes(bytearray([z + 0x100])))

    def test_positive_tiny_int(self, assert_packable):
        for z in range(0, 128):
            assert_packable(z, bytes(bytearray([z])))

    def test_negative_int8(self, assert_packable):
        for z in range(-128, -16):
            assert_packable(z, bytes(bytearray([0xC8, z + 0x100])))

    def test_positive_int16(self, assert_packable):
        for z in range(128, 32768):
            expected = b"\xC9" + struct.pack(">h", z)
            assert_packable(z, expected)

    def test_negative_int16(self, assert_packable):
        for z in range(-32768, -128):
            expected = b"\xC9" + struct.pack(">h", z)
            assert_packable(z, expected)

    def test_positive_int32(self, assert_packable):
        for e in range(15, 31):
            z = 2 ** e
            expected = b"\xCA" + struct.pack(">i", z)
            assert_packable(z, expected)

    def test_negative_int32(self, assert_packable):
        for e in range(15, 31):
            z = -(2 ** e + 1)
            expected = b"\xCA" + struct.pack(">i", z)
            assert_packable(z, expected)

    def test_positive_int64(self, assert_packable):
        for e in range(31, 63):
            z = 2 ** e
            expected = b"\xCB" + struct.pack(">q", z)
            assert_packable(z, expected)

    def test_negative_int64(self, assert_packable):
        for e in range(31, 63):
            z = -(2 ** e + 1)
            expected = b"\xCB" + struct.pack(">q", z)
            assert_packable(z, expected)

    def test_integer_positive_overflow(self, pack, assert_packable):
        with pytest.raises(OverflowError):
            pack(2 ** 63 + 1)

    def test_integer_negative_overflow(self, pack, assert_packable):
        with pytest.raises(OverflowError):
            pack(-(2 ** 63) - 1)

    def test_zero_float64(self, assert_packable):
        zero = 0.0
        expected = b"\xC1" + struct.pack(">d", zero)
        assert_packable(zero, expected)

    def test_tau_float64(self, assert_packable):
        tau = 2 * pi
        expected = b"\xC1" + struct.pack(">d", tau)
        assert_packable(tau, expected)

    def test_positive_float64(self, assert_packable):
        for e in range(0, 100):
            r = float(2 ** e) + 0.5
            expected = b"\xC1" + struct.pack(">d", r)
            assert_packable(r, expected)

    def test_negative_float64(self, assert_packable):
        for e in range(0, 100):
            r = -(float(2 ** e) + 0.5)
            expected = b"\xC1" + struct.pack(">d", r)
            assert_packable(r, expected)

    def test_empty_bytes(self, assert_packable):
        assert_packable(b"", b"\xCC\x00")

    def test_empty_bytearray(self, assert_packable):
        assert_packable(bytearray(), b"\xCC\x00")

    def test_bytes_8(self, assert_packable):
        assert_packable(bytearray(b"hello"), b"\xCC\x05hello")

    def test_bytes_16(self, assert_packable):
        b = bytearray(40000)
        assert_packable(b, b"\xCD\x9C\x40" + b)

    def test_bytes_32(self, assert_packable):
        b = bytearray(80000)
        assert_packable(b, b"\xCE\x00\x01\x38\x80" + b)

    def test_bytearray_size_overflow(self, assert_packable):
        stream_out = BytesIO()
        packer = Packer(stream_out)
        with pytest.raises(OverflowError):
            packer.pack_bytes_header(2 ** 32)

    def test_empty_string(self, assert_packable):
        assert_packable(u"", b"\x80")

    def test_tiny_strings(self, assert_packable):
        for size in range(0x10):
            assert_packable(u"A" * size, bytes(bytearray([0x80 + size]) + (b"A" * size)))

    def test_string_8(self, assert_packable):
        t = u"A" * 40
        b = t.encode("utf-8")
        assert_packable(t, b"\xD0\x28" + b)

    def test_string_16(self, assert_packable):
        t = u"A" * 40000
        b = t.encode("utf-8")
        assert_packable(t, b"\xD1\x9C\x40" + b)

    def test_string_32(self, assert_packable):
        t = u"A" * 80000
        b = t.encode("utf-8")
        assert_packable(t, b"\xD2\x00\x01\x38\x80" + b)

    def test_unicode_string(self, assert_packable):
        t = u"héllö"
        b = t.encode("utf-8")
        assert_packable(t, bytes(bytearray([0x80 + len(b)])) + b)

    def test_string_size_overflow(self):
        stream_out = BytesIO()
        packer = Packer(stream_out)
        with pytest.raises(OverflowError):
            packer.pack_string_header(2 ** 32)

    def test_empty_list(self, assert_packable):
        assert_packable([], b"\x90")

    def test_tiny_lists(self, assert_packable):
        for size in range(0x10):
            data_out = bytearray([0x90 + size]) + bytearray([1] * size)
            assert_packable([1] * size, bytes(data_out))

    def test_list_8(self, assert_packable):
        l = [1] * 40
        assert_packable(l, b"\xD4\x28" + (b"\x01" * 40))

    def test_list_16(self, assert_packable):
        l = [1] * 40000
        assert_packable(l, b"\xD5\x9C\x40" + (b"\x01" * 40000))

    def test_list_32(self, assert_packable):
        l = [1] * 80000
        assert_packable(l, b"\xD6\x00\x01\x38\x80" + (b"\x01" * 80000))

    def test_nested_lists(self, assert_packable):
        assert_packable([[[]]], b"\x91\x91\x90")

    def test_list_size_overflow(self):
        stream_out = BytesIO()
        packer = Packer(stream_out)
        with pytest.raises(OverflowError):
            packer.pack_list_header(2 ** 32)

    def test_empty_map(self, assert_packable):
        assert_packable({}, b"\xA0")

    @pytest.mark.parametrize("size", range(0x10))
    def test_tiny_maps(self, assert_packable, size):
        data_in = dict()
        data_out = bytearray([0xA0 + size])
        for el in range(1, size + 1):
            data_in[chr(64 + el)] = el
            data_out += bytearray([0x81, 64 + el, el])
        assert_packable(data_in, bytes(data_out))

    def test_map_8(self, pack, assert_packable):
        d = dict([(u"A%s" % i, 1) for i in range(40)])
        b = b"".join(pack(u"A%s" % i, 1) for i in range(40))
        assert_packable(d, b"\xD8\x28" + b)

    def test_map_16(self, pack, assert_packable):
        d = dict([(u"A%s" % i, 1) for i in range(40000)])
        b = b"".join(pack(u"A%s" % i, 1) for i in range(40000))
        assert_packable(d, b"\xD9\x9C\x40" + b)

    def test_map_32(self, pack, assert_packable):
        d = dict([(u"A%s" % i, 1) for i in range(80000)])
        b = b"".join(pack(u"A%s" % i, 1) for i in range(80000))
        assert_packable(d, b"\xDA\x00\x01\x38\x80" + b)

    def test_map_size_overflow(self):
        stream_out = BytesIO()
        packer = Packer(stream_out)
        with pytest.raises(OverflowError):
            packer.pack_map_header(2 ** 32)

    @pytest.mark.parametrize(("map_", "exc_type"), (
        ({1: "1"}, TypeError),
        ({"x": {1: 'eins', 2: 'zwei', 3: 'drei'}}, TypeError),
        ({"x": {(1, 2): '1+2i', (2, 0): '2'}}, TypeError),
    ))
    def test_map_key_type(self, packer_with_buffer, map_, exc_type):
        # maps must have string keys
        packer, packable_buffer = packer_with_buffer
        with pytest.raises(exc_type, match="strings"):
            packer.pack(map_)

    def test_illegal_signature(self, assert_packable):
        with pytest.raises(ValueError):
            assert_packable(Structure(b"XXX"), b"\xB0XXX")

    def test_empty_struct(self, assert_packable):
        assert_packable(Structure(b"X"), b"\xB0X")

    def test_tiny_structs(self, assert_packable):
        for size in range(0x10):
            fields = [1] * size
            data_in = Structure(b"A", *fields)
            data_out = bytearray([0xB0 + size, 0x41] + fields)
            assert_packable(data_in, bytes(data_out))

    def test_struct_size_overflow(self, pack):
        with pytest.raises(OverflowError):
            fields = [1] * 16
            pack(Structure(b"X", *fields))

    def test_illegal_uuid(self, assert_packable):
        with pytest.raises(ValueError):
            assert_packable(uuid4(), b"\xB0XXX")
