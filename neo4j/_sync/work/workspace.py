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

from ..._conf import WorkspaceConfig
from ..._deadline import Deadline
from ..._meta import (
    deprecation_warn,
    unclosed_resource_warn,
)
from ...exceptions import (
    ServiceUnavailable,
    SessionExpired,
)
from ..io import Neo4jPool


class Workspace:

    def __init__(self, pool, config):
        assert isinstance(config, WorkspaceConfig)
        self._pool = pool
        self._config = config
        self._connection = None
        self._connection_access_mode = None
        # Sessions are supposed to cache the database on which to operate.
        self._cached_database = False
        self._bookmarks = None
        # Workspace has been closed.
        self._closed = False

    def __del__(self):
        if self._closed:
            return
        unclosed_resource_warn(self)
        # TODO: 6.0 - remove this
        if asyncio.iscoroutinefunction(self.close):
            return
        try:
            deprecation_warn(
                "Relying on Session's destructor to close the session "
                "is deprecated. Please make sure to close the session. Use it "
                "as a context (`with` statement) or make sure to call "
                "`.close()` explicitly. Future versions of the driver will "
                "not close sessions automatically."
            )
            self.close()
        except (OSError, ServiceUnavailable, SessionExpired):
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _set_cached_database(self, database):
        self._cached_database = True
        self._config.database = database

    def _connect(self, access_mode, **acquire_kwargs):
        timeout = Deadline(self._config.session_connection_timeout)
        if self._connection:
            # TODO: Investigate this
            # log.warning("FIXME: should always disconnect before connect")
            self._connection.send_all()
            self._connection.fetch_all()
            self._disconnect()
        if not self._cached_database:
            if (self._config.database is not None
                    or not isinstance(self._pool, Neo4jPool)):
                self._set_cached_database(self._config.database)
            else:
                # This is the first time we open a connection to a server in a
                # cluster environment for this session without explicitly
                # configured database. Hence, we request a routing table update
                # to try to fetch the home database. If provided by the server,
                # we shall use this database explicitly for all subsequent
                # actions within this session.
                self._pool.update_routing_table(
                    database=self._config.database,
                    imp_user=self._config.impersonated_user,
                    bookmarks=self._bookmarks,
                    timeout=timeout,
                    database_callback=self._set_cached_database
                )
        acquire_kwargs_ = {
            "access_mode": access_mode,
            "timeout": timeout,
            "acquisition_timeout": self._config.connection_acquisition_timeout,
            "database": self._config.database,
            "bookmarks": self._bookmarks,
            "liveness_check_timeout": None,
        }
        acquire_kwargs_.update(acquire_kwargs)
        self._connection = self._pool.acquire(**acquire_kwargs_)
        self._connection_access_mode = access_mode

    def _disconnect(self, sync=False):
        if self._connection:
            if sync:
                try:
                    self._connection.send_all()
                    self._connection.fetch_all()
                except ServiceUnavailable:
                    pass
            if self._connection:
                self._pool.release(self._connection)
                self._connection = None
            self._connection_access_mode = None

    def close(self):
        if self._closed:
            return
        self._disconnect(sync=True)
        self._closed = True
