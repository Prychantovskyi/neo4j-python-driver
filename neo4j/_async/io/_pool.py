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


import abc
import logging
from collections import (
    defaultdict,
    deque,
)
from contextlib import asynccontextmanager
from logging import getLogger
from random import choice

from ..._async_compat.concurrency import (
    AsyncCondition,
    AsyncRLock,
)
from ..._async_compat.network import AsyncNetworkUtil
from ..._conf import (
    PoolConfig,
    WorkspaceConfig,
)
from ..._deadline import (
    connection_deadline,
    Deadline,
    merge_deadlines,
    merge_deadlines_and_timeouts,
)
from ..._exceptions import BoltError
from ..._routing import RoutingTable
from ...api import (
    READ_ACCESS,
    WRITE_ACCESS,
)
from ...exceptions import (
    ClientError,
    ConfigurationError,
    DriverError,
    Neo4jError,
    ReadServiceUnavailable,
    ServiceUnavailable,
    SessionExpired,
    WriteServiceUnavailable,
)
from ._bolt import AsyncBolt


# Set up logger
log = getLogger("neo4j")


class AsyncIOPool(abc.ABC):
    """ A collection of connections to one or more server addresses.
    """

    def __init__(self, opener, pool_config, workspace_config):
        assert callable(opener)
        assert isinstance(pool_config, PoolConfig)
        assert isinstance(workspace_config, WorkspaceConfig)

        self.opener = opener
        self.pool_config = pool_config
        self.workspace_config = workspace_config
        self.connections = defaultdict(deque)
        self.connections_reservations = defaultdict(lambda: 0)
        self.lock = AsyncRLock()
        self.cond = AsyncCondition(self.lock)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()

    async def _acquire_from_pool(self, address):
        async with self.lock:
            for connection in list(self.connections.get(address, [])):
                if connection.in_use:
                    continue
                connection.pool = self
                connection.in_use = True
                return connection
        return None  # no free connection available

    async def _acquire_from_pool_checked(
        self, address, health_check, deadline
    ):
        while not deadline.expired():
            connection = await self._acquire_from_pool(address)
            if not connection:
                return None  # no free connection available
            if not await health_check(connection, deadline):
                # `close` is a noop on already closed connections.
                # This is to make sure that the connection is
                # gracefully closed, e.g. if it's just marked as
                # `stale` but still alive.
                if log.isEnabledFor(logging.DEBUG):
                    log.debug(
                        "[#%04X]  C: <POOL> removing old connection "
                        "(closed=%s, defunct=%s, stale=%s, in_use=%s)",
                        connection.local_port,
                        connection.closed(), connection.defunct(),
                        connection.stale(), connection.in_use
                    )
                await connection.close()
                async with self.lock:
                    try:
                        self.connections.get(address, []).remove(connection)
                    except ValueError:
                        # If closure fails (e.g. because the server went
                        # down), all connections to the same address will
                        # be removed. Therefore, we silently ignore if the
                        # connection isn't in the pool anymore.
                        pass
                continue  # try again with a new connection
            else:
                return connection

    async def _acquire_new_later(self, address, deadline):
        async def connection_creator():
            released_reservation = False
            try:
                try:
                    connection = await self.opener(
                        address, deadline.to_timeout()
                    )
                except ServiceUnavailable:
                    await self.deactivate(address)
                    raise
                connection.pool = self
                connection.in_use = True
                async with self.lock:
                    self.connections_reservations[address] -= 1
                    released_reservation = True
                    self.connections[address].append(connection)
                return connection
            finally:
                if not released_reservation:
                    async with self.lock:
                        self.connections_reservations[address] -= 1

        max_pool_size = self.pool_config.max_connection_pool_size
        infinite_pool_size = (max_pool_size < 0
                              or max_pool_size == float("inf"))
        async with self.lock:
            connections = self.connections[address]
            pool_size = (len(connections)
                         + self.connections_reservations[address])
            can_create_new_connection = (infinite_pool_size
                                         or pool_size < max_pool_size)
            self.connections_reservations[address] += 1
        if can_create_new_connection:
            return connection_creator
        return None

    async def _acquire(self, address, deadline, liveness_check_timeout):
        """ Acquire a connection to a given address from the pool.
        The address supplied should always be an IP address, not
        a host name.

        This method is thread safe.
        """
        async def health_check(connection_, deadline_):
            if (connection_.closed()
                    or connection_.defunct()
                    or connection_.stale()):
                return False
            if liveness_check_timeout is not None:
                if connection_.is_idle_for(liveness_check_timeout):
                    with connection_deadline(connection_, deadline_):
                        try:
                            await connection_.reset()
                        except (OSError, ServiceUnavailable, SessionExpired):
                            return False
            return True

        while True:
            # try to find a free connection in the pool
            connection = await self._acquire_from_pool_checked(
                address, health_check, deadline
            )
            if connection:
                return connection
            # all connections in pool are in-use
            async with self.lock:
                connection_creator = await self._acquire_new_later(
                    address, deadline
                )
                if connection_creator:
                    break

                # failed to obtain a connection from pool because the
                # pool is full and no free connection in the pool
                timeout = deadline.to_timeout()
                if (
                    timeout == 0  # deadline expired
                    or not await self.cond.wait(timeout)
                ):
                    raise ClientError(
                        "failed to obtain a connection from the pool within "
                        "{!r}s (timeout)".format(deadline.original_timeout)
                    )
        return await connection_creator()

    @abc.abstractmethod
    async def acquire(
        self, access_mode, timeout, acquisition_timeout,
        database, bookmarks, liveness_check_timeout
    ):
        """ Acquire a connection to a server that can satisfy a set of parameters.

        :param access_mode:
        :param timeout: total timeout (including potential preparation)
        :param acquisition_timeout: timeout for actually acquiring a connection
        :param database:
        :param bookmarks:
        :param liveness_check_timeout:
        """

    async def release(self, *connections):
        """ Release a connection back into the pool.
        This method is thread safe.
        """
        async with self.lock:
            for connection in connections:
                if not (connection.defunct()
                        or connection.closed()
                        or connection.is_reset):
                    try:
                        await connection.reset()
                    except (Neo4jError, DriverError, BoltError) as e:
                        log.debug(
                            "Failed to reset connection on release: %s", e
                        )
                connection.in_use = False
            self.cond.notify_all()

    def in_use_connection_count(self, address):
        """ Count the number of connections currently in use to a given
        address.
        """
        try:
            connections = self.connections[address]
        except KeyError:
            return 0
        else:
            return sum(1 if connection.in_use else 0 for connection in connections)

    async def mark_all_stale(self):
        async with self.lock:
            for address in self.connections:
                for connection in self.connections[address]:
                    connection.set_stale()

    async def deactivate(self, address):
        """ Deactivate an address from the connection pool, if present, closing
        all idle connection to that address
        """
        async with self.lock:
            try:
                connections = self.connections[address]
            except KeyError:  # already removed from the connection pool
                return
            closable_connections = [
                conn for conn in connections if not conn.in_use
            ]
            # First remove all connections in question, then try to close them.
            # If closing of a connection fails, we will end up in this method
            # again.
            for conn in closable_connections:
                connections.remove(conn)
            for conn in closable_connections:
                await conn.close()
            if not self.connections[address]:
                del self.connections[address]

    def on_write_failure(self, address):
        raise WriteServiceUnavailable(
            "No write service available for pool {}".format(self)
        )

    async def close(self):
        """ Close all connections and empty the pool.
        This method is thread safe.
        """
        try:
            async with self.lock:
                for address in list(self.connections):
                    for connection in self.connections.pop(address, ()):
                        await connection.close()
        except TypeError:
            pass


class AsyncBoltPool(AsyncIOPool):

    @classmethod
    def open(cls, address, *, auth, pool_config, workspace_config):
        """Create a new BoltPool

        :param address:
        :param auth:
        :param pool_config:
        :param workspace_config:
        :return: BoltPool
        """

        async def opener(addr, timeout):
            return await AsyncBolt.open(
                addr, auth=auth, timeout=timeout, routing_context=None,
                **pool_config
            )

        pool = cls(opener, pool_config, workspace_config, address)
        return pool

    def __init__(self, opener, pool_config, workspace_config, address):
        super().__init__(opener, pool_config, workspace_config)
        self.address = address

    def __repr__(self):
        return "<{} address={!r}>".format(self.__class__.__name__,
                                          self.address)

    async def acquire(
        self, access_mode, timeout,  acquisition_timeout,
        database, bookmarks, liveness_check_timeout
    ):
        # The access_mode and database is not needed for a direct connection,
        # it's just there for consistency.
        deadline = merge_deadlines_and_timeouts(timeout, acquisition_timeout)
        return await self._acquire(
            self.address, deadline, liveness_check_timeout
        )


class AsyncNeo4jPool(AsyncIOPool):
    """ Connection pool with routing table.
    """

    @classmethod
    def open(cls, *addresses, auth, pool_config, workspace_config,
             routing_context=None):
        """Create a new Neo4jPool

        :param addresses: one or more address as positional argument
        :param auth:
        :param pool_config:
        :param workspace_config:
        :param routing_context:
        :return: Neo4jPool
        """

        address = addresses[0]
        if routing_context is None:
            routing_context = {}
        elif "address" in routing_context:
            raise ConfigurationError("The key 'address' is reserved for routing context.")
        routing_context["address"] = str(address)

        async def opener(addr, timeout):
            return await AsyncBolt.open(
                addr, auth=auth, timeout=timeout,
                routing_context=routing_context, **pool_config
            )

        pool = cls(opener, pool_config, workspace_config, address)
        return pool

    def __init__(self, opener, pool_config, workspace_config, address):
        """

        :param opener:
        :param pool_config:
        :param workspace_config:
        :param address:
        """
        super().__init__(opener, pool_config, workspace_config)
        # Each database have a routing table, the default database is a special case.
        log.debug("[#0000]  C: <NEO4J POOL> routing address %r", address)
        self.address = address
        self.routing_tables = {workspace_config.database: RoutingTable(database=workspace_config.database, routers=[address])}
        self.refresh_lock = AsyncRLock()

    def __repr__(self):
        """ The representation shows the initial routing addresses.

        :return: The representation
        :rtype: str
        """
        return "<{} addresses={!r}>".format(self.__class__.__name__, self.get_default_database_initial_router_addresses())

    @asynccontextmanager
    async def _refresh_lock_deadline(self, deadline):
        timeout = deadline.to_timeout()
        if timeout == float("inf"):
            timeout = -1
        if not await self.refresh_lock.acquire(timeout=timeout):
            raise ClientError(
                "pool failed to update routing table within {!r}s (timeout)"
                .format(deadline.original_timeout)
            )

        try:
            yield
        finally:
            self.refresh_lock.release()

    @property
    def first_initial_routing_address(self):
        return self.get_default_database_initial_router_addresses()[0]

    def get_default_database_initial_router_addresses(self):
        """ Get the initial router addresses for the default database.

        :return:
        :rtype: OrderedSet
        """
        return self.get_routing_table_for_default_database().initial_routers

    def get_default_database_router_addresses(self):
        """ Get the router addresses for the default database.

        :return:
        :rtype: OrderedSet
        """
        return self.get_routing_table_for_default_database().routers

    def get_routing_table_for_default_database(self):
        return self.routing_tables[self.workspace_config.database]

    async def get_or_create_routing_table(self, database):
        async with self.refresh_lock:
            if database not in self.routing_tables:
                self.routing_tables[database] = RoutingTable(
                    database=database,
                    routers=self.get_default_database_initial_router_addresses()
                )
            return self.routing_tables[database]

    async def fetch_routing_info(
        self, address, database, imp_user, bookmarks, deadline
    ):
        """ Fetch raw routing info from a given router address.

        :param address: router address
        :param database: the database name to get routing table for
        :param imp_user: the user to impersonate while fetching the routing
                         table
        :type imp_user: str or None
        :param bookmarks: iterable of bookmark values after which the routing
                          info should be fetched
        :param deadline: connection acquisition deadline

        :return: list of routing records, or None if no connection
            could be established or if no readers or writers are present
        :raise ServiceUnavailable: if the server does not support
            routing, or if routing support is broken or outdated
        """
        cx = await self._acquire(address, deadline, None)
        try:
            with connection_deadline(cx, deadline):
                routing_table = await cx.route(
                    database or self.workspace_config.database,
                    imp_user or self.workspace_config.impersonated_user,
                    bookmarks
                )
        finally:
            await self.release(cx)
        return routing_table

    async def fetch_routing_table(
        self, *, address, deadline, database, imp_user, bookmarks
    ):
        """ Fetch a routing table from a given router address.

        :param address: router address
        :param deadline: deadline
        :param database: the database name
        :type: str
        :param imp_user: the user to impersonate while fetching the routing
                         table
        :type imp_user: str or None
        :param bookmarks: bookmarks used when fetching routing table

        :return: a new RoutingTable instance or None if the given router is
                 currently unable to provide routing information
        """
        new_routing_info = None
        try:
            new_routing_info = await self.fetch_routing_info(
                address, database, imp_user, bookmarks, deadline
            )
        except Neo4jError as e:
            # checks if the code is an error that is caused by the client. In
            # this case there is no sense in trying to fetch a RT from another
            # router. Hence, the driver should fail fast during discovery.
            if e.is_fatal_during_discovery():
                raise
        except (ServiceUnavailable, SessionExpired):
            pass
        if not new_routing_info:
            log.debug("Failed to fetch routing info %s", address)
            return None
        else:
            servers = new_routing_info[0]["servers"]
            ttl = new_routing_info[0]["ttl"]
            database = new_routing_info[0].get("db", database)
            new_routing_table = RoutingTable.parse_routing_info(
                database=database, servers=servers, ttl=ttl
            )

        # Parse routing info and count the number of each type of server
        num_routers = len(new_routing_table.routers)
        num_readers = len(new_routing_table.readers)

        # num_writers = len(new_routing_table.writers)
        # If no writers are available. This likely indicates a temporary state,
        # such as leader switching, so we should not signal an error.

        # No routers
        if num_routers == 0:
            log.debug("No routing servers returned from server %s", address)
            return None

        # No readers
        if num_readers == 0:
            log.debug("No read servers returned from server %s", address)
            return None

        # At least one of each is fine, so return this table
        return new_routing_table

    async def _update_routing_table_from(
        self, *routers, database, imp_user, bookmarks, deadline,
        database_callback
    ):
        """ Try to update routing tables with the given routers.

        :return: True if the routing table is successfully updated,
        otherwise False
        """
        log.debug("Attempting to update routing table from {}".format(
            ", ".join(map(repr, routers)))
        )
        for router in routers:
            async for address in AsyncNetworkUtil.resolve_address(
                router, resolver=self.pool_config.resolver
            ):
                if deadline.expired():
                    return False
                new_routing_table = await self.fetch_routing_table(
                    address=address,
                    deadline=deadline,
                    database=database, imp_user=imp_user, bookmarks=bookmarks
                )
                if new_routing_table is not None:
                    new_database = new_routing_table.database
                    old_routing_table = await self.get_or_create_routing_table(
                        new_database
                    )
                    old_routing_table.update(new_routing_table)
                    log.debug(
                        "[#0000]  C: <UPDATE ROUTING TABLE> address=%r (%r)",
                        address, self.routing_tables[new_database]
                    )
                    if callable(database_callback):
                        database_callback(new_database)
                    return True
            await self.deactivate(router)
        return False

    async def update_routing_table(
        self, *, database, imp_user, bookmarks, timeout=None,
        database_callback=None
    ):
        """ Update the routing table from the first router able to provide
        valid routing information.

        :param database: The database name
        :param imp_user: the user to impersonate while fetching the routing
                         table
        :type imp_user: str or None
        :param bookmarks: bookmarks used when fetching routing table
        :param timeout: timeout in seconds for how long to try updating
        :param database_callback: A callback function that will be called with
            the database name as only argument when a new routing table has been
            acquired. This database name might different from `database` if that
            was None and the underlying protocol supports reporting back the
            actual database.

        :raise neo4j.exceptions.ServiceUnavailable:
        """
        deadline = merge_deadlines_and_timeouts(
            timeout, self.pool_config.update_routing_table_timeout
        )
        async with self._refresh_lock_deadline(deadline):
            routing_table = await self.get_or_create_routing_table(database)
            # copied because it can be modified
            existing_routers = set(routing_table.routers)

            prefer_initial_routing_address = \
                self.routing_tables[database].initialized_without_writers

            if prefer_initial_routing_address:
                # TODO: Test this state
                if await self._update_routing_table_from(
                    self.first_initial_routing_address, database=database,
                    imp_user=imp_user, bookmarks=bookmarks,
                    deadline=deadline, database_callback=database_callback
                ):
                    # Why is only the first initial routing address used?
                    return
            if await self._update_routing_table_from(
                *(existing_routers - {self.first_initial_routing_address}),
                database=database, imp_user=imp_user, bookmarks=bookmarks,
                deadline=deadline, database_callback=database_callback
            ):
                return

            if not prefer_initial_routing_address:
                if await self._update_routing_table_from(
                    self.first_initial_routing_address, database=database,
                    imp_user=imp_user, bookmarks=bookmarks,
                    deadline=deadline,
                    database_callback=database_callback
                ):
                    # Why is only the first initial routing address used?
                    return

            # None of the routers have been successful, so just fail
            log.error("Unable to retrieve routing information")
            raise ServiceUnavailable("Unable to retrieve routing information")

    async def update_connection_pool(self, *, database):
        routing_table = await self.get_or_create_routing_table(database)
        servers = routing_table.servers()
        for address in list(self.connections):
            if address.unresolved not in servers:
                await super(AsyncNeo4jPool, self).deactivate(address)

    async def ensure_routing_table_is_fresh(
        self, *, access_mode, database, imp_user, bookmarks, deadline=None,
        database_callback=None
    ):
        """ Update the routing table if stale.

        This method performs two freshness checks, before and after acquiring
        the refresh lock. If the routing table is already fresh on entry, the
        method exits immediately; otherwise, the refresh lock is acquired and
        the second freshness check that follows determines whether an update
        is still required.

        This method is thread-safe.

        :return: `True` if an update was required, `False` otherwise.
        """
        from neo4j.api import READ_ACCESS
        async with self._refresh_lock_deadline(deadline):
            routing_table = await self.get_or_create_routing_table(database)
            if routing_table.is_fresh(readonly=(access_mode == READ_ACCESS)):
                # Readers are fresh.
                return False

            await self.update_routing_table(
                database=database, imp_user=imp_user, bookmarks=bookmarks,
                timeout=deadline, database_callback=database_callback
            )
            await self.update_connection_pool(database=database)

            for database in list(self.routing_tables.keys()):
                # Remove unused databases in the routing table
                # Remove the routing table after a timeout = TTL + 30s
                log.debug("[#0000]  C: <ROUTING AGED> database=%s", database)
                if (self.routing_tables[database].should_be_purged_from_memory()
                        and database != self.workspace_config.database):
                    del self.routing_tables[database]

            return True

    async def _select_address(self, *, access_mode, database):
        from ...api import READ_ACCESS
        """ Selects the address with the fewest in-use connections.
        """
        async with self.refresh_lock:
            if access_mode == READ_ACCESS:
                addresses = self.routing_tables[database].readers
            else:
                addresses = self.routing_tables[database].writers
            addresses_by_usage = {}
            for address in addresses:
                addresses_by_usage.setdefault(
                    self.in_use_connection_count(address), []
                ).append(address)
        if not addresses_by_usage:
            if access_mode == READ_ACCESS:
                raise ReadServiceUnavailable(
                    "No read service currently available"
                )
            else:
                raise WriteServiceUnavailable(
                    "No write service currently available"
                )
        return choice(addresses_by_usage[min(addresses_by_usage)])

    async def acquire(
        self, access_mode, timeout, acquisition_timeout,
        database, bookmarks, liveness_check_timeout
    ):
        if access_mode not in (WRITE_ACCESS, READ_ACCESS):
            raise ClientError("Non valid 'access_mode'; {}".format(access_mode))
        if not timeout:
            raise ClientError("'timeout' must be a float larger than 0; {}"
                              .format(timeout))
        if not acquisition_timeout:
            raise ClientError("'acquisition_timeout' must be a float larger "
                              "than 0; {}".format(acquisition_timeout))
        deadline = Deadline.from_timeout_or_deadline(timeout)

        from neo4j.api import check_access_mode
        access_mode = check_access_mode(access_mode)
        async with self._refresh_lock_deadline(deadline):
            log.debug("[#0000]  C: <ROUTING TABLE ENSURE FRESH> %r",
                      self.routing_tables)
            await self.ensure_routing_table_is_fresh(
                access_mode=access_mode, database=database, imp_user=None,
                bookmarks=bookmarks, deadline=deadline
            )

        # Making sure the routing table is fresh is not considered part of the
        # connection acquisition. Hence, the acquisition_timeout starts now!
        deadline = merge_deadlines(
            deadline, Deadline.from_timeout_or_deadline(acquisition_timeout)
        )
        while True:
            try:
                # Get an address for a connection that have the fewest in-use
                # connections.
                address = await self._select_address(
                    access_mode=access_mode, database=database
                )
            except (ReadServiceUnavailable, WriteServiceUnavailable) as err:
                raise SessionExpired("Failed to obtain connection towards '%s' server." % access_mode) from err
            try:
                log.debug("[#0000]  C: <ACQUIRE ADDRESS> database=%r address=%r", database, address)
                # should always be a resolved address
                connection = await self._acquire(
                    address, deadline, liveness_check_timeout
                )
            except (ServiceUnavailable, SessionExpired):
                await self.deactivate(address=address)
            else:
                return connection

    async def deactivate(self, address):
        """ Deactivate an address from the connection pool,
        if present, remove from the routing table and also closing
        all idle connections to that address.
        """
        log.debug("[#0000]  C: <ROUTING> Deactivating address %r", address)
        # We use `discard` instead of `remove` here since the former
        # will not fail if the address has already been removed.
        for database in self.routing_tables.keys():
            self.routing_tables[database].routers.discard(address)
            self.routing_tables[database].readers.discard(address)
            self.routing_tables[database].writers.discard(address)
        log.debug("[#0000]  C: <ROUTING> table=%r", self.routing_tables)
        await super(AsyncNeo4jPool, self).deactivate(address)

    def on_write_failure(self, address):
        """ Remove a writer address from the routing table, if present.
        """
        log.debug("[#0000]  C: <ROUTING> Removing writer %r", address)
        for database in self.routing_tables.keys():
            self.routing_tables[database].writers.discard(address)
        log.debug("[#0000]  C: <ROUTING> table=%r", self.routing_tables)
