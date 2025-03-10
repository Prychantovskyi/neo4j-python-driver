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


from logging import getLogger
from random import random
from time import perf_counter

from ..._async_compat import async_sleep
from ..._conf import SessionConfig
from ..._meta import (
    deprecated,
    deprecation_warn,
)
from ...api import (
    Bookmarks,
    READ_ACCESS,
    WRITE_ACCESS,
)
from ...exceptions import (
    ClientError,
    DriverError,
    Neo4jError,
    ServiceUnavailable,
    SessionExpired,
    TransactionError,
)
from ...work import Query
from .result import AsyncResult
from .transaction import (
    AsyncManagedTransaction,
    AsyncTransaction,
)
from .workspace import AsyncWorkspace


log = getLogger("neo4j")


class AsyncSession(AsyncWorkspace):
    """A :class:`.AsyncSession` is a logical context for transactional units
    of work. Connections are drawn from the :class:`.AsyncDriver` connection
    pool as required.

    Session creation is a lightweight operation and sessions are not safe to
    be used in concurrent contexts (multiple threads/coroutines).
    Therefore, a session should generally be short-lived, and must not
    span multiple threads/coroutines.

    In general, sessions will be created and destroyed within a `with`
    context. For example::

        async with driver.session() as session:
            result = await session.run("MATCH (n:Person) RETURN n.name AS name")
            # do something with the result...

    :param pool: connection pool instance
    :param config: session config instance
    """

    # The current connection.
    _connection = None

    # The current :class:`.Transaction` instance, if any.
    _transaction = None

    # The current auto-transaction result, if any.
    _auto_result = None

    # The state this session is in.
    _state_failed = False

    def __init__(self, pool, session_config):
        super().__init__(pool, session_config)
        assert isinstance(session_config, SessionConfig)
        self._bookmarks = self._prepare_bookmarks(session_config.bookmarks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exception_type, exception_value, traceback):
        if exception_type:
            self._state_failed = True
        await self.close()

    def _prepare_bookmarks(self, bookmarks):
        if isinstance(bookmarks, Bookmarks):
            return tuple(bookmarks.raw_values)
        if hasattr(bookmarks, "__iter__"):
            deprecation_warn(
                "Passing an iterable as `bookmarks` to `Session` is "
                "deprecated. Please use a `Bookmarks` instance.",
                stack_level=5
            )
            return tuple(bookmarks)
        if not bookmarks:
            return ()
        raise TypeError("Bookmarks must be an instance of Bookmarks or an "
                        "iterable of raw bookmarks (deprecated).")

    async def _connect(self, access_mode, **access_kwargs):
        if access_mode is None:
            access_mode = self._config.default_access_mode
        await super()._connect(access_mode, **access_kwargs)

    def _collect_bookmark(self, bookmark):
        if bookmark:
            self._bookmarks = bookmark,

    async def _result_closed(self):
        if self._auto_result:
            self._collect_bookmark(self._auto_result._bookmark)
            self._auto_result = None
            await self._disconnect()

    async def _result_error(self, _):
        if self._auto_result:
            self._auto_result = None
            await self._disconnect()

    async def _get_server_info(self):
        assert not self._connection
        await self._connect(READ_ACCESS, liveness_check_timeout=0)
        server_info = self._connection.server_info
        await self._disconnect()
        return server_info

    async def close(self):
        """Close the session.

        This will release any borrowed resources, such as connections, and will
        roll back any outstanding transactions.
        """
        if self._closed:
            return
        if self._connection:
            if self._auto_result:
                if self._state_failed is False:
                    try:
                        await self._auto_result.consume()
                        self._collect_bookmark(self._auto_result._bookmark)
                    except Exception as error:
                        # TODO: Investigate potential non graceful close states
                        self._auto_result = None
                        self._state_failed = True

            if self._transaction:
                if self._transaction._closed() is False:
                    # roll back the transaction if it is not closed
                    await self._transaction._rollback()
                self._transaction = None

            try:
                if self._connection:
                    await self._connection.send_all()
                    await self._connection.fetch_all()
                    # TODO: Investigate potential non graceful close states
            except Neo4jError:
                pass
            except TransactionError:
                pass
            except ServiceUnavailable:
                pass
            except SessionExpired:
                pass
            finally:
                await self._disconnect()

            self._state_failed = False
        self._closed = True

    async def run(self, query, parameters=None, **kwargs):
        """Run a Cypher query within an auto-commit transaction.

        The query is sent and the result header received
        immediately but the :class:`neo4j.Result` content is
        fetched lazily as consumed by the client application.

        If a query is executed before a previous
        :class:`neo4j.AsyncResult` in the same :class:`.AsyncSession` has
        been fully consumed, the first result will be fully fetched
        and buffered. Note therefore that the generally recommended
        pattern of usage is to fully consume one result before
        executing a subsequent query. If two results need to be
        consumed in parallel, multiple :class:`.AsyncSession` objects
        can be used as an alternative to result buffering.

        For more usage details, see :meth:`.AsyncTransaction.run`.

        :param query: cypher query
        :type query: str, neo4j.Query
        :param parameters: dictionary of parameters
        :type parameters: dict
        :param kwargs: additional keyword parameters
        :returns: a new :class:`neo4j.AsyncResult` object
        :rtype: AsyncResult
        """
        if not query:
            raise ValueError("Cannot run an empty query")
        if not isinstance(query, (str, Query)):
            raise TypeError("query must be a string or a Query instance")

        if self._transaction:
            raise ClientError("Explicit Transaction must be handled explicitly")

        if self._auto_result:
            # This will buffer upp all records for the previous auto-transaction
            await self._auto_result._buffer_all()

        if not self._connection:
            await self._connect(self._config.default_access_mode)
        cx = self._connection
        protocol_version = cx.PROTOCOL_VERSION
        server_info = cx.server_info

        self._auto_result = AsyncResult(
            cx, self._config.fetch_size, self._result_closed,
            self._result_error
        )
        await self._auto_result._run(
            query, parameters, self._config.database,
            self._config.impersonated_user, self._config.default_access_mode,
            self._bookmarks, **kwargs
        )

        return self._auto_result

    @deprecated(
        "`last_bookmark` has been deprecated in favor of `last_bookmarks`. "
        "This method can lead to unexpected behaviour."
    )
    async def last_bookmark(self):
        """Return the bookmark received following the last completed transaction.

        Note: For auto-transactions (:meth:`Session.run`), this will trigger
        :meth:`Result.consume` for the current result.

        .. warning::
            This method can lead to unexpected behaviour if the session has not
            yet successfully completed a transaction.

        .. deprecated:: 5.0
            :meth:`last_bookmark` will be removed in version 6.0.
            Use :meth:`last_bookmarks` instead.

        :returns: last bookmark
        :rtype: str or None
        """
        # The set of bookmarks to be passed into the next transaction.

        if self._auto_result:
            await self._auto_result.consume()

        if self._transaction and self._transaction._closed:
            self._collect_bookmark(self._transaction._bookmark)
            self._transaction = None

        if self._bookmarks:
            return self._bookmarks[-1]
        return None

    async def last_bookmarks(self):
        """Return most recent bookmarks of the session.

        Bookmarks can be used to causally chain sessions. For example,
        if a session (``session1``) wrote something, that another session
        (``session2``) needs to read, use
        ``session2 = driver.session(bookmarks=session1.last_bookmarks())`` to
        achieve this.

        Combine the bookmarks of multiple sessions like so::

            bookmarks1 = await session1.last_bookmarks()
            bookmarks2 = await session2.last_bookmarks()
            session3 = driver.session(bookmarks=bookmarks1 + bookmarks2)

        A session automatically manages bookmarks, so this method is rarely
        needed. If you need causal consistency, try to run the relevant queries
        in the same session.

        "Most recent bookmarks" are either the bookmarks passed to the session
        or creation, or the last bookmark the session received after committing
        a transaction to the server.

        Note: For auto-transactions (:meth:`Session.run`), this will trigger
        :meth:`Result.consume` for the current result.

        :returns: the session's last known bookmarks
        :rtype: Bookmarks
        """
        # The set of bookmarks to be passed into the next transaction.

        if self._auto_result:
            await self._auto_result.consume()

        if self._transaction and self._transaction._closed():
            self._collect_bookmark(self._transaction._bookmark)
            self._transaction = None

        return Bookmarks.from_raw_values(self._bookmarks)

    async def _transaction_closed_handler(self):
        if self._transaction:
            self._collect_bookmark(self._transaction._bookmark)
            self._transaction = None
            await self._disconnect()

    async def _transaction_error_handler(self, _):
        if self._transaction:
            self._transaction = None
            await self._disconnect()

    async def _open_transaction(
        self, *, tx_cls, access_mode, metadata=None, timeout=None
    ):
        await self._connect(access_mode=access_mode)
        self._transaction = tx_cls(
            self._connection, self._config.fetch_size,
            self._transaction_closed_handler,
            self._transaction_error_handler
        )
        await self._transaction._begin(
            self._config.database, self._config.impersonated_user,
            self._bookmarks, access_mode, metadata, timeout
        )

    async def begin_transaction(self, metadata=None, timeout=None):
        """ Begin a new unmanaged transaction. Creates a new :class:`.AsyncTransaction` within this session.
            At most one transaction may exist in a session at any point in time.
            To maintain multiple concurrent transactions, use multiple concurrent sessions.

            Note: For auto-transaction (AsyncSession.run) this will trigger an consume for the current result.

        :param metadata:
            a dictionary with metadata.
            Specified metadata will be attached to the executing transaction and visible in the output of ``dbms.listQueries`` and ``dbms.listTransactions`` procedures.
            It will also get logged to the ``query.log``.
            This functionality makes it easier to tag transactions and is equivalent to ``dbms.setTXMetaData`` procedure, see https://neo4j.com/docs/operations-manual/current/reference/procedures/ for procedure reference.
        :type metadata: dict

        :param timeout:
            the transaction timeout in seconds.
            Transactions that execute longer than the configured timeout will be terminated by the database.
            This functionality allows to limit query/transaction execution time.
            Specified timeout overrides the default timeout configured in the database using ``dbms.transaction.timeout`` setting.
            Value should not represent a duration of zero or negative duration.
        :type timeout: int

        :returns: A new transaction instance.
        :rtype: AsyncTransaction

        :raises TransactionError: :class:`neo4j.exceptions.TransactionError` if a transaction is already open.
        """
        # TODO: Implement TransactionConfig consumption

        if self._auto_result:
            await self._auto_result.consume()

        if self._transaction:
            raise TransactionError("Explicit transaction already open")

        await self._open_transaction(
            tx_cls=AsyncTransaction,
            access_mode=self._config.default_access_mode, metadata=metadata,
            timeout=timeout
        )

        return self._transaction

    async def _run_transaction(
        self, access_mode, transaction_function, *args, **kwargs
    ):
        if not callable(transaction_function):
            raise TypeError("Unit of work is not callable")

        metadata = getattr(transaction_function, "metadata", None)
        timeout = getattr(transaction_function, "timeout", None)

        retry_delay = retry_delay_generator(
            self._config.initial_retry_delay,
            self._config.retry_delay_multiplier,
            self._config.retry_delay_jitter_factor
        )

        errors = []

        t0 = -1  # Timer

        while True:
            try:
                await self._open_transaction(
                    tx_cls=AsyncManagedTransaction,
                    access_mode=access_mode, metadata=metadata,
                    timeout=timeout
                )
                tx = self._transaction
                try:
                    result = await transaction_function(tx, *args, **kwargs)
                except Exception:
                    await tx._close()
                    raise
                else:
                    await tx._commit()
            except (DriverError, Neo4jError) as error:
                await self._disconnect()
                if not error.is_retryable():
                    raise
                errors.append(error)
            else:
                return result
            if t0 == -1:
                # The timer should be started after the first attempt
                t0 = perf_counter()
            t1 = perf_counter()
            if t1 - t0 > self._config.max_transaction_retry_time:
                break
            delay = next(retry_delay)
            log.warning("Transaction failed and will be retried in {}s ({})"
                        "".format(delay, "; ".join(errors[-1].args)))
            await async_sleep(delay)

        if errors:
            raise errors[-1]
        else:
            raise ServiceUnavailable("Transaction failed")

    async def read_transaction(self, transaction_function, *args, **kwargs):
        """Execute a unit of work in a managed read transaction.

        .. note::
            This does not necessarily imply access control, see the session
            configuration option :ref:`default-access-mode-ref`.

        This transaction will automatically be committed unless an exception is thrown during query execution or by the user code.
        Note, that this function perform retries and that the supplied `transaction_function` might get invoked more than once.

        Managed transactions should not generally be explicitly committed
        (via ``await tx.commit()``).

        Example::

            async def do_cypher_tx(tx, cypher):
                result = await tx.run(cypher)
                values = [record.values() async for record in result]
                return values

            async with driver.session() as session:
                values = await session.read_transaction(do_cypher_tx, "RETURN 1 AS x")

        Example::

            async def get_two_tx(tx):
                result = await tx.run("UNWIND [1,2,3,4] AS x RETURN x")
                values = []
                async for record in result:
                    if len(values) >= 2:
                        break
                    values.append(record.values())
                # or shorter: values = [record.values()
                #                       for record in await result.fetch(2)]

                # discard the remaining records if there are any
                summary = await result.consume()
                # use the summary for logging etc.
                return values

            async with driver.session() as session:
                values = await session.read_transaction(get_two_tx)

        :param transaction_function: a function that takes a transaction as an
            argument and does work with the transaction.
            `transaction_function(tx, *args, **kwargs)` where `tx` is a
            :class:`.AsyncTransaction`.
        :param args: arguments for the `transaction_function`
        :param kwargs: key word arguments for the `transaction_function`
        :return: a result as returned by the given unit of work
        """
        return await self._run_transaction(
            READ_ACCESS, transaction_function, *args, **kwargs
        )

    async def write_transaction(self, transaction_function, *args, **kwargs):
        """Execute a unit of work in a managed write transaction.

        .. note::
            This does not necessarily imply access control, see the session
            configuration option :ref:`default-access-mode-ref`.

        This transaction will automatically be committed unless an exception is thrown during query execution or by the user code.
        Note, that this function perform retries and that the supplied `transaction_function` might get invoked more than once.

        Managed transactions should not generally be explicitly committed (via tx.commit()).

        Example::

            async def create_node_tx(tx, name):
                query = "CREATE (n:NodeExample { name: $name }) RETURN id(n) AS node_id"
                result = await tx.run(query, name=name)
                record = await result.single()
                return record["node_id"]

            async with driver.session() as session:
                node_id = await session.write_transaction(create_node_tx, "example")

        :param transaction_function: a function that takes a transaction as an
            argument and does work with the transaction.
            `transaction_function(tx, *args, **kwargs)` where `tx` is a
            :class:`.AsyncTransaction`.
        :param args: key word arguments for the `transaction_function`
        :param kwargs: key word arguments for the `transaction_function`
        :return: a result as returned by the given unit of work
        """
        return await self._run_transaction(
            WRITE_ACCESS, transaction_function, *args, **kwargs
        )


def retry_delay_generator(initial_delay, multiplier, jitter_factor):
    delay = initial_delay
    while True:
        jitter = jitter_factor * delay
        yield delay - jitter + (2 * jitter * random())
        delay *= multiplier
