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


import asyncio
import logging
import selectors
import socket
import struct
from socket import (
    AF_INET,
    AF_INET6,
    SHUT_RDWR,
    SO_KEEPALIVE,
    socket,
    SOL_SOCKET,
    timeout as SocketTimeout,
)
from ssl import (
    CertificateError,
    HAS_SNI,
    SSLError,
)
from time import perf_counter

from ... import addressing
from ..._deadline import Deadline
from ..._exceptions import (
    BoltError,
    BoltProtocolError,
    BoltSecurityError,
    SocketDeadlineExceeded,
)
from ...exceptions import (
    DriverError,
    ServiceUnavailable,
)
from ._util import (
    AsyncNetworkUtil,
    NetworkUtil,
)


log = logging.getLogger("neo4j")


def _sanitize_deadline(deadline):
    if deadline is None:
        return None
    deadline = Deadline.from_timeout_or_deadline(deadline)
    if deadline.to_timeout() is None:
        return None
    return deadline


class AsyncBoltSocket:
    Bolt = None

    def __init__(self, reader, protocol, writer):
        self._reader = reader  # type: asyncio.StreamReader
        self._protocol = protocol  # type: asyncio.StreamReaderProtocol
        self._writer = writer  # type: asyncio.StreamWriter
        # 0 - non-blocking
        # None infinitely blocking
        # int - seconds to wait for data
        self._timeout = None
        self._deadline = None

    async def _wait_for_io(self, io_fut):
        timeout = self._timeout
        to_raise = SocketTimeout
        if self._deadline is not None:
            deadline_timeout = self._deadline.to_timeout()
            if deadline_timeout <= 0:
                raise SocketDeadlineExceeded("timed out")
            if timeout is None or deadline_timeout <= timeout:
                timeout = deadline_timeout
                to_raise = SocketDeadlineExceeded

        if timeout is not None and timeout <= 0:
            # give the io-operation time for one loop cycle to do its thing
            io_fut = asyncio.create_task(io_fut)
            await asyncio.sleep(0)
        try:
            return await asyncio.wait_for(io_fut, timeout)
        except asyncio.TimeoutError as e:
            raise to_raise("timed out") from e

    def get_deadline(self):
        return self._deadline

    def set_deadline(self, deadline):
        self._deadline = _sanitize_deadline(deadline)

    @property
    def _socket(self) -> socket:
        return self._writer.transport.get_extra_info("socket")

    def getsockname(self):
        return self._writer.transport.get_extra_info("sockname")

    def getpeername(self):
        return self._writer.transport.get_extra_info("peername")

    def getpeercert(self, *args, **kwargs):
        return self._writer.transport.get_extra_info("ssl_object")\
            .getpeercert(*args, **kwargs)

    def gettimeout(self):
        return self._timeout

    def settimeout(self, timeout):
        if timeout is None:
            self._timeout = timeout
        else:
            assert timeout >= 0
            self._timeout = timeout

    async def recv(self, n):
        io_fut = self._reader.read(n)
        return await self._wait_for_io(io_fut)

    async def recv_into(self, buffer, nbytes):
        # FIXME: not particularly memory or time efficient
        io_fut = self._reader.read(nbytes)
        res = await self._wait_for_io(io_fut)
        buffer[:len(res)] = res
        return len(res)

    async def sendall(self, data):
        self._writer.write(data)
        io_fut = self._writer.drain()
        return await self._wait_for_io(io_fut)

    async def close(self):
        self._writer.close()
        await self._writer.wait_closed()

    @classmethod
    async def _connect_secure(cls, resolved_address, timeout, keep_alive, ssl):
        """

        :param resolved_address:
        :param timeout: seconds
        :param keep_alive: True or False
        :param ssl: SSLContext or None

        :return: AsyncBoltSocket object
        """

        loop = asyncio.get_event_loop()
        s = None

        # TODO: tomorrow me: fix this mess
        try:
            if len(resolved_address) == 2:
                s = socket(AF_INET)
            elif len(resolved_address) == 4:
                s = socket(AF_INET6)
            else:
                raise ValueError(
                    "Unsupported address {!r}".format(resolved_address))
            s.setblocking(False)  # asyncio + blocking = no-no!
            log.debug("[#0000]  C: <OPEN> %s", resolved_address)
            await asyncio.wait_for(
                loop.sock_connect(s, resolved_address),
                timeout
            )

            keep_alive = 1 if keep_alive else 0
            s.setsockopt(SOL_SOCKET, SO_KEEPALIVE, keep_alive)

            ssl_kwargs = {}

            if ssl is not None:
                hostname = resolved_address.host_name or None
                ssl_kwargs.update(
                    ssl=ssl, server_hostname=hostname if HAS_SNI else None
                )

            reader = asyncio.StreamReader(
                limit=2 ** 16,  # 64 KiB,
                loop=loop
            )
            protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
            transport, _ = await loop.create_connection(
                lambda: protocol, sock=s, **ssl_kwargs
            )
            writer = asyncio.StreamWriter(transport, protocol, reader, loop)

            if ssl is not None:
                # Check that the server provides a certificate
                der_encoded_server_certificate = \
                    transport.get_extra_info("ssl_object").getpeercert(
                        binary_form=True)
                if der_encoded_server_certificate is None:
                    local_port = s.getsockname()[1]
                    raise BoltProtocolError(
                        "When using an encrypted socket, the server should "
                        "always provide a certificate",
                        address=(resolved_address.host_name, local_port)
                    )

            return cls(reader, protocol, writer)

        except asyncio.TimeoutError:
            log.debug("[#0000]  C: <TIMEOUT> %s", resolved_address)
            log.debug("[#0000]  C: <CLOSE> %s", resolved_address)
            if s:
                await cls.close_socket(s)
            raise ServiceUnavailable(
                "Timed out trying to establish connection to {!r}".format(
                    resolved_address))
        except (SSLError, CertificateError) as error:
            local_port = s.getsockname()[1]
            raise BoltSecurityError(
                message="Failed to establish encrypted connection.",
                address=(resolved_address.host_name, local_port)
            ) from error
        except OSError as error:
            log.debug("[#0000]  C: <ERROR> %s %s", type(error).__name__,
                      " ".join(map(repr, error.args)))
            log.debug("[#0000]  C: <CLOSE> %s", resolved_address)
            s.close()
            raise ServiceUnavailable(
                "Failed to establish connection to {!r} (reason {})".format(
                    resolved_address, error))

    async def _handshake(self, resolved_address):
        """

        :param s: Socket
        :param resolved_address:

        :return: (socket, version, client_handshake, server_response_data)
        """
        local_port = self.getsockname()[1]

        # TODO: Optimize logging code
        handshake = self.Bolt.get_handshake()
        handshake = struct.unpack(">16B", handshake)
        handshake = [handshake[i:i + 4] for i in range(0, len(handshake), 4)]

        supported_versions = [
            ("0x%02X%02X%02X%02X" % (vx[0], vx[1], vx[2], vx[3])) for vx in
            handshake]

        log.debug("[#%04X]  C: <MAGIC> 0x%08X", local_port,
                  int.from_bytes(self.Bolt.MAGIC_PREAMBLE, byteorder="big"))
        log.debug("[#%04X]  C: <HANDSHAKE> %s %s %s %s", local_port,
                  *supported_versions)

        data = self.Bolt.MAGIC_PREAMBLE + self.Bolt.get_handshake()
        await self.sendall(data)

        # Handle the handshake response
        original_timeout = self.gettimeout()
        if original_timeout is not None:
            self.settimeout(original_timeout + 1)
        try:
            data = await self.recv(4)
        except OSError:
            raise ServiceUnavailable(
                "Failed to read any data from server {!r} "
                "after connected".format(resolved_address))
        finally:
            self.settimeout(original_timeout)
        data_size = len(data)
        if data_size == 0:
            # If no data is returned after a successful select
            # response, the server has closed the connection
            log.debug("[#%04X]  S: <CLOSE>", local_port)
            await self.close()
            raise ServiceUnavailable(
                "Connection to {address} closed without handshake response".format(
                    address=resolved_address))
        if data_size != 4:
            # Some garbled data has been received
            log.debug("[#%04X]  S: @*#!", local_port)
            await self.close()
            raise BoltProtocolError(
                "Expected four byte Bolt handshake response from %r, received %r instead; check for incorrect port number" % (
                resolved_address, data), address=resolved_address)
        elif data == b"HTTP":
            log.debug("[#%04X]  S: <CLOSE>", local_port)
            await self.close()
            raise ServiceUnavailable(
                "Cannot to connect to Bolt service on {!r} "
                "(looks like HTTP)".format(resolved_address))
        agreed_version = data[-1], data[-2]
        log.debug("[#%04X]  S: <HANDSHAKE> 0x%06X%02X", local_port,
                  agreed_version[1], agreed_version[0])
        return self, agreed_version, handshake, data

    @classmethod
    async def close_socket(cls, socket_):
        try:
            if isinstance(socket_, AsyncBoltSocket):
                await socket_.close()
            else:
                socket_.shutdown(SHUT_RDWR)
                socket_.close()
        except OSError:
            pass

    @classmethod
    async def connect(cls, address, *, timeout, custom_resolver, ssl_context,
                      keep_alive):
        """ Connect and perform a handshake and return a valid Connection object,
        assuming a protocol version can be agreed.
        """
        errors = []
        failed_addresses = []
        # Establish a connection to the host and port specified
        # Catches refused connections see:
        # https://docs.python.org/2/library/errno.html

        resolved_addresses = AsyncNetworkUtil.resolve_address(
            addressing.Address(address), resolver=custom_resolver
        )
        async for resolved_address in resolved_addresses:
            s = None
            try:
                s = await cls._connect_secure(
                    resolved_address, timeout, keep_alive, ssl_context
                )
                return await s._handshake(resolved_address)
            except (BoltError, DriverError, OSError) as error:
                try:
                    local_port = s.getsockname()[1]
                except (OSError, AttributeError, TypeError):
                    local_port = 0
                err_str = error.__class__.__name__
                if str(error):
                    err_str += ": " + str(error)
                log.debug("[#%04X]  C: <CONNECTION FAILED> %s", local_port,
                          err_str)
                if s:
                    await cls.close_socket(s)
                errors.append(error)
                failed_addresses.append(resolved_address)
            except Exception:
                if s:
                    await cls.close_socket(s)
                raise
        if not errors:
            raise ServiceUnavailable(
                "Couldn't connect to %s (resolved to %s)" % (
                    str(address), tuple(map(str, failed_addresses)))
            )
        else:
            raise ServiceUnavailable(
                "Couldn't connect to %s (resolved to %s):\n%s" % (
                    str(address), tuple(map(str, failed_addresses)),
                    "\n".join(map(str, errors))
                )
            ) from errors[0]


class BoltSocket:
    Bolt = None

    def __init__(self, socket_: socket):
        self._socket = socket_
        self._deadline = None

    @property
    def _socket(self):
        return self.__socket

    @_socket.setter
    def _socket(self, socket_: socket):
        self.__socket = socket_
        self.getsockname = socket_.getsockname
        self.getpeername = socket_.getpeername
        if hasattr(socket, "getpeercert"):
            self.getpeercert = socket_.getpeercert
        elif hasattr(self, "getpeercert"):
            del self.getpeercert
        self.gettimeout = socket_.gettimeout
        self.settimeout = socket_.settimeout

    def _wait_for_io(self, func, *args, **kwargs):
        if self._deadline is None:
            return func(*args, **kwargs)
        timeout = self._socket.gettimeout()
        deadline_timeout = self._deadline.to_timeout()
        if deadline_timeout <= 0:
            raise SocketDeadlineExceeded("timed out")
        if timeout is None or deadline_timeout <= timeout:
            self._socket.settimeout(deadline_timeout)
            try:
                return func(*args, **kwargs)
            except SocketTimeout as e:
                raise SocketDeadlineExceeded("timed out") from e
            finally:
                self._socket.settimeout(timeout)
        return func(*args, **kwargs)

    def get_deadline(self):
        return self._deadline

    def set_deadline(self, deadline):
        self._deadline = _sanitize_deadline(deadline)

    def recv(self, n):
        return self._wait_for_io(self._socket.recv, n)

    def recv_into(self, buffer, nbytes):
        return self._wait_for_io(self._socket.recv_into, buffer, nbytes)

    def sendall(self, data):
        return self._wait_for_io(self._socket.sendall, data)

    def close(self):
        self._socket.shutdown(SHUT_RDWR)
        self._socket.close()

    @classmethod
    def _connect(cls, resolved_address, timeout, keep_alive):
        """

        :param resolved_address:
        :param timeout: seconds
        :param keep_alive: True or False
        :return: socket object
        """

        s = None  # The socket

        try:
            if len(resolved_address) == 2:
                s = socket(AF_INET)
            elif len(resolved_address) == 4:
                s = socket(AF_INET6)
            else:
                raise ValueError(
                    "Unsupported address {!r}".format(resolved_address))
            t = s.gettimeout()
            if timeout:
                s.settimeout(timeout)
            log.debug("[#0000]  C: <OPEN> %s", resolved_address)
            s.connect(resolved_address)
            s.settimeout(t)
            keep_alive = 1 if keep_alive else 0
            s.setsockopt(SOL_SOCKET, SO_KEEPALIVE, keep_alive)
            return s
        except SocketTimeout:
            log.debug("[#0000]  C: <TIMEOUT> %s", resolved_address)
            log.debug("[#0000]  C: <CLOSE> %s", resolved_address)
            cls.close_socket(s)
            raise ServiceUnavailable(
                "Timed out trying to establish connection to {!r}".format(
                    resolved_address))
        except OSError as error:
            log.debug("[#0000]  C: <ERROR> %s %s", type(error).__name__,
                      " ".join(map(repr, error.args)))
            log.debug("[#0000]  C: <CLOSE> %s", resolved_address)
            s.close()
            raise ServiceUnavailable(
                "Failed to establish connection to {!r} (reason {})".format(
                    resolved_address, error))

    @classmethod
    def _secure(cls, s, host, ssl_context):
        local_port = s.getsockname()[1]
        # Secure the connection if an SSL context has been provided
        if ssl_context:
            log.debug("[#%04X]  C: <SECURE> %s", local_port, host)
            try:
                sni_host = host if HAS_SNI and host else None
                s = ssl_context.wrap_socket(s, server_hostname=sni_host)
            except (OSError, SSLError, CertificateError) as cause:
                raise BoltSecurityError(
                    message="Failed to establish encrypted connection.",
                    address=(host, local_port)
                ) from cause
            # Check that the server provides a certificate
            der_encoded_server_certificate = s.getpeercert(binary_form=True)
            if der_encoded_server_certificate is None:
                raise BoltProtocolError(
                    "When using an encrypted socket, the server should always "
                    "provide a certificate", address=(host, local_port)
                )
            return s
        return s

    @classmethod
    def _handshake(cls, s, resolved_address):
        """

        :param s: Socket
        :param resolved_address:

        :return: (socket, version, client_handshake, server_response_data)
        """
        local_port = s.getsockname()[1]

        # TODO: Optimize logging code
        handshake = cls.Bolt.get_handshake()
        handshake = struct.unpack(">16B", handshake)
        handshake = [handshake[i:i + 4] for i in range(0, len(handshake), 4)]

        supported_versions = [
            ("0x%02X%02X%02X%02X" % (vx[0], vx[1], vx[2], vx[3])) for vx in
            handshake]

        log.debug("[#%04X]  C: <MAGIC> 0x%08X", local_port,
                  int.from_bytes(cls.Bolt.MAGIC_PREAMBLE, byteorder="big"))
        log.debug("[#%04X]  C: <HANDSHAKE> %s %s %s %s", local_port,
                  *supported_versions)

        data = cls.Bolt.MAGIC_PREAMBLE + cls.Bolt.get_handshake()
        s.sendall(data)

        # Handle the handshake response
        ready_to_read = False
        with selectors.DefaultSelector() as selector:
            selector.register(s, selectors.EVENT_READ)
            selector.select(1)
        try:
            data = s.recv(4)
        except OSError:
            raise ServiceUnavailable(
                "Failed to read any data from server {!r} "
                "after connected".format(resolved_address))
        data_size = len(data)
        if data_size == 0:
            # If no data is returned after a successful select
            # response, the server has closed the connection
            log.debug("[#%04X]  S: <CLOSE>", local_port)
            BoltSocket.close_socket(s)
            raise ServiceUnavailable(
                "Connection to {address} closed without handshake response".format(
                    address=resolved_address))
        if data_size != 4:
            # Some garbled data has been received
            log.debug("[#%04X]  S: @*#!", local_port)
            s.close()
            raise BoltProtocolError(
                "Expected four byte Bolt handshake response from %r, received %r instead; check for incorrect port number" % (
                resolved_address, data), address=resolved_address)
        elif data == b"HTTP":
            log.debug("[#%04X]  S: <CLOSE>", local_port)
            BoltSocket.close_socket(s)
            raise ServiceUnavailable(
                "Cannot to connect to Bolt service on {!r} "
                "(looks like HTTP)".format(resolved_address))
        agreed_version = data[-1], data[-2]
        log.debug("[#%04X]  S: <HANDSHAKE> 0x%06X%02X", local_port,
                  agreed_version[1], agreed_version[0])
        return cls(s), agreed_version, handshake, data

    @classmethod
    def close_socket(cls, socket_):
        try:
            if isinstance(socket_, BoltSocket):
                socket.close()
            else:
                socket_.shutdown(SHUT_RDWR)
                socket_.close()
        except OSError:
            pass

    @classmethod
    def connect(cls, address, *, timeout, custom_resolver, ssl_context,
                keep_alive):
        """ Connect and perform a handshake and return a valid Connection object,
        assuming a protocol version can be agreed.
        """
        errors = []
        # Establish a connection to the host and port specified
        # Catches refused connections see:
        # https://docs.python.org/2/library/errno.html

        resolved_addresses = NetworkUtil.resolve_address(
            addressing.Address(address), resolver=custom_resolver
        )
        for resolved_address in resolved_addresses:
            s = None
            try:
                s = BoltSocket._connect(resolved_address, timeout, keep_alive)
                s = BoltSocket._secure(s, resolved_address.host_name,
                                       ssl_context)
                return BoltSocket._handshake(s, resolved_address)
            except (BoltError, DriverError, OSError) as error:
                try:
                    local_port = s.getsockname()[1]
                except (OSError, AttributeError):
                    local_port = 0
                err_str = error.__class__.__name__
                if str(error):
                    err_str += ": " + str(error)
                log.debug("[#%04X]  C: <CONNECTION FAILED> %s", local_port,
                          err_str)
                if s:
                    BoltSocket.close_socket(s)
                errors.append(error)
            except Exception:
                if s:
                    BoltSocket.close_socket(s)
                raise
        if not errors:
            raise ServiceUnavailable(
                "Couldn't connect to %s (resolved to %s)" % (
                    str(address), tuple(map(str, resolved_addresses)))
            )
        else:
            raise ServiceUnavailable(
                "Couldn't connect to %s (resolved to %s):\n%s" % (
                    str(address), tuple(map(str, resolved_addresses)),
                    "\n".join(map(str, errors))
                )
            ) from errors[0]
