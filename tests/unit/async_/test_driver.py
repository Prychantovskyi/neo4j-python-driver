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


import ssl
from functools import wraps

import pytest

from neo4j import (
    AsyncBoltDriver,
    AsyncGraphDatabase,
    AsyncNeo4jDriver,
    ExperimentalWarning,
    TRUST_ALL_CERTIFICATES,
    TRUST_SYSTEM_CA_SIGNED_CERTIFICATES,
    TrustAll,
    TrustCustomCAs,
    TrustSystemCAs,
)
from neo4j._async_compat.util import AsyncUtil
from neo4j.api import (
    READ_ACCESS,
    WRITE_ACCESS,
)
from neo4j.exceptions import ConfigurationError

from ..._async_compat import mark_async_test


@wraps(AsyncGraphDatabase.driver)
def create_driver(*args, **kwargs):
    if AsyncUtil.is_async_code:
        with pytest.warns(ExperimentalWarning, match="async") as warnings:
            driver = AsyncGraphDatabase.driver(*args, **kwargs)
        print(warnings)
        return driver
    else:
        return AsyncGraphDatabase.driver(*args, **kwargs)


def driver(*args, **kwargs):
    return AsyncNeo4jDriver(*args, **kwargs)


@pytest.mark.parametrize("protocol", ("bolt://", "bolt+s://", "bolt+ssc://"))
@pytest.mark.parametrize("host", ("localhost", "127.0.0.1",
                                  "[::1]", "[0:0:0:0:0:0:0:1]"))
@pytest.mark.parametrize("port", (":1234", "", ":7687"))
@pytest.mark.parametrize("params", ("", "?routing_context=test"))
@pytest.mark.parametrize("auth_token", (("test", "test"), None))
@mark_async_test
async def test_direct_driver_constructor(protocol, host, port, params, auth_token):
    uri = protocol + host + port + params
    if params:
        with pytest.warns(DeprecationWarning, match="routing context"):
            driver = AsyncGraphDatabase.driver(uri, auth=auth_token)
    else:
        driver = create_driver(uri, auth=auth_token)
    assert isinstance(driver, AsyncBoltDriver)
    await driver.close()


@pytest.mark.parametrize("protocol",
                         ("neo4j://", "neo4j+s://", "neo4j+ssc://"))
@pytest.mark.parametrize("host", ("localhost", "127.0.0.1",
                                  "[::1]", "[0:0:0:0:0:0:0:1]"))
@pytest.mark.parametrize("port", (":1234", "", ":7687"))
@pytest.mark.parametrize("params", ("", "?routing_context=test"))
@pytest.mark.parametrize("auth_token", (("test", "test"), None))
@mark_async_test
async def test_routing_driver_constructor(protocol, host, port, params, auth_token):
    uri = protocol + host + port + params
    driver = create_driver(uri, auth=auth_token)
    assert isinstance(driver, AsyncNeo4jDriver)
    await driver.close()


@pytest.mark.parametrize("test_uri", (
    "bolt+ssc://127.0.0.1:9001",
    "bolt+s://127.0.0.1:9001",
    "bolt://127.0.0.1:9001",
    "neo4j+ssc://127.0.0.1:9001",
    "neo4j+s://127.0.0.1:9001",
    "neo4j://127.0.0.1:9001",
))
@pytest.mark.parametrize(
    ("test_config", "expected_failure", "expected_failure_message"),
    (
        ({"encrypted": False}, ConfigurationError, "The config settings"),
        ({"encrypted": True}, ConfigurationError, "The config settings"),
        (
            {"encrypted": True, "trust": TRUST_ALL_CERTIFICATES},
            ConfigurationError, "The config settings"
        ),
        (
            {"trust": TRUST_ALL_CERTIFICATES},
            ConfigurationError, "The config settings"
        ),
        (
            {"trust": TRUST_SYSTEM_CA_SIGNED_CERTIFICATES},
            ConfigurationError, "The config settings"
        ),
        (
            {"encrypted": True, "trusted_certificates": TrustAll()},
            ConfigurationError, "The config settings"
        ),
        (
            {"trusted_certificates": TrustAll()},
            ConfigurationError, "The config settings"
        ),
        (
            {"trusted_certificates": TrustSystemCAs()},
            ConfigurationError, "The config settings"
        ),
        (
            {"trusted_certificates": TrustCustomCAs("foo", "bar")},
            ConfigurationError, "The config settings"
        ),
        (
            {"ssl_context": None},
            ConfigurationError, "The config settings"
        ),
        (
            {"ssl_context": ssl.SSLContext(ssl.PROTOCOL_TLSv1)},
            ConfigurationError, "The config settings"
        ),
    )
)
@mark_async_test
async def test_driver_config_error(
    test_uri, test_config, expected_failure, expected_failure_message
):
    def driver_builder():
        if "trust" in test_config:
            with pytest.warns(DeprecationWarning, match="trust"):
                return AsyncGraphDatabase.driver(test_uri, **test_config)
        else:
            return create_driver(test_uri, **test_config)

    if "+" in test_uri:
        # `+s` and `+ssc` are short hand syntax for not having to configure the
        # encryption behavior of the driver. Specifying both is invalid.
        with pytest.raises(expected_failure, match=expected_failure_message):
            driver_builder()
    else:
        driver = driver_builder()
        await driver.close()


@pytest.mark.parametrize("test_uri", (
    "http://localhost:9001",
    "ftp://localhost:9001",
    "x://localhost:9001",
))
def test_invalid_protocol(test_uri):
    with pytest.raises(ConfigurationError, match="scheme"):
        create_driver(test_uri)


@pytest.mark.parametrize(
    ("test_config", "expected_failure", "expected_failure_message"),
    (
        ({"trust": 1}, ConfigurationError, "The config setting `trust`"),
        ({"trust": True}, ConfigurationError, "The config setting `trust`"),
        ({"trust": None}, ConfigurationError, "The config setting `trust`"),
    )
)
def test_driver_trust_config_error(
    test_config, expected_failure, expected_failure_message
):
    with pytest.raises(expected_failure, match=expected_failure_message):
        create_driver("bolt://127.0.0.1:9001", **test_config)


@pytest.mark.parametrize("uri", (
    "bolt://127.0.0.1:9000",
    "neo4j://127.0.0.1:9000",
))
@mark_async_test
async def test_driver_opens_write_session_by_default(uri, mocker):
    driver = create_driver(uri)
    from neo4j import AsyncTransaction

    # we set a specific db, because else the driver would try to fetch a RT
    # to get hold of the actual home database (which won't work in this
    # unittest)
    async with driver.session(database="foobar") as session:
        acquire_mock = mocker.patch.object(session._pool, "acquire",
                                           autospec=True)
        tx_begin_mock = mocker.patch.object(AsyncTransaction, "_begin",
                                            autospec=True)
        tx = await session.begin_transaction()
    acquire_mock.assert_called_once_with(
        access_mode=WRITE_ACCESS,
        timeout=mocker.ANY,
        acquisition_timeout=mocker.ANY,
        database=mocker.ANY,
        bookmarks=mocker.ANY,
        liveness_check_timeout=mocker.ANY
    )
    tx_begin_mock.assert_called_once_with(
        tx,
        mocker.ANY,
        mocker.ANY,
        mocker.ANY,
        WRITE_ACCESS,
        mocker.ANY,
        mocker.ANY
    )

    await driver.close()


@pytest.mark.parametrize("uri", (
    "bolt://127.0.0.1:9000",
    "neo4j://127.0.0.1:9000",
))
@mark_async_test
async def test_verify_connectivity(uri, mocker):
    driver = create_driver(uri)
    pool_mock = mocker.patch.object(driver, "_pool", autospec=True)

    try:
        ret = await driver.verify_connectivity()
    finally:
        await driver.close()

    assert ret is None
    pool_mock.acquire.assert_awaited_once()
    assert pool_mock.acquire.call_args.kwargs["liveness_check_timeout"] == 0
    pool_mock.release.assert_awaited_once()


@pytest.mark.parametrize("uri", (
    "bolt://127.0.0.1:9000",
    "neo4j://127.0.0.1:9000",
))
@pytest.mark.parametrize("kwargs", (
    {"default_access_mode": WRITE_ACCESS},
    {"default_access_mode": READ_ACCESS},
    {"fetch_size": 69},
))
@mark_async_test
async def test_verify_connectivity_parameters_are_deprecated(
    uri, kwargs, mocker
):
    driver = create_driver(uri)
    mocker.patch.object(driver, "_pool", autospec=True)

    try:
        with pytest.warns(ExperimentalWarning, match="configuration"):
            await driver.verify_connectivity(**kwargs)
    finally:
        await driver.close()


@pytest.mark.parametrize("uri", (
    "bolt://127.0.0.1:9000",
    "neo4j://127.0.0.1:9000",
))
@pytest.mark.parametrize("kwargs", (
    {"default_access_mode": WRITE_ACCESS},
    {"default_access_mode": READ_ACCESS},
    {"fetch_size": 69},
))
@mark_async_test
async def test_get_server_info_parameters_are_experimental(
    uri, kwargs, mocker
):
    driver = create_driver(uri)
    mocker.patch.object(driver, "_pool", autospec=True)

    try:
        with pytest.warns(ExperimentalWarning, match="configuration"):
            await driver.get_server_info(**kwargs)
    finally:
        await driver.close()
