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

""" Base classes and helpers.
"""


from urllib.parse import (
    parse_qs,
    urlparse,
)

from ._meta import deprecated
from .exceptions import ConfigurationError


READ_ACCESS = "READ"
WRITE_ACCESS = "WRITE"

DRIVER_BOLT = "DRIVER_BOLT"
DRIVER_NEO4j = "DRIVER_NEO4J"

SECURITY_TYPE_NOT_SECURE = "SECURITY_TYPE_NOT_SECURE"
SECURITY_TYPE_SELF_SIGNED_CERTIFICATE = "SECURITY_TYPE_SELF_SIGNED_CERTIFICATE"
SECURITY_TYPE_SECURE = "SECURITY_TYPE_SECURE"

URI_SCHEME_BOLT = "bolt"
URI_SCHEME_BOLT_SELF_SIGNED_CERTIFICATE = "bolt+ssc"
URI_SCHEME_BOLT_SECURE = "bolt+s"

URI_SCHEME_NEO4J = "neo4j"
URI_SCHEME_NEO4J_SELF_SIGNED_CERTIFICATE = "neo4j+ssc"
URI_SCHEME_NEO4J_SECURE = "neo4j+s"

URI_SCHEME_BOLT_ROUTING = "bolt+routing"

# TODO: 6.0 - remove TRUST constants
TRUST_SYSTEM_CA_SIGNED_CERTIFICATES = "TRUST_SYSTEM_CA_SIGNED_CERTIFICATES"  # Default
TRUST_ALL_CERTIFICATES = "TRUST_ALL_CERTIFICATES"

SYSTEM_DATABASE = "system"
DEFAULT_DATABASE = None  # Must be a non string hashable value


# TODO: This class is not tested
class Auth:
    """Container for auth details.

    :param scheme: specifies the type of authentication, examples: "basic",
                   "kerberos"
    :type scheme: str
    :param principal: specifies who is being authenticated
    :type principal: str or None
    :param credentials: authenticates the principal
    :type credentials: str or None
    :param realm: specifies the authentication provider
    :type realm: str or None
    :param parameters: extra key word parameters passed along to the
                       authentication provider
    :type parameters: Dict[str, Any]
    """

    def __init__(self, scheme, principal, credentials, realm=None, **parameters):
        self.scheme = scheme
        # Neo4j servers pre 4.4 require the principal field to always be
        # present. Therefore, we transmit it even if it's an empty sting.
        if principal is not None:
            self.principal = principal
        if credentials:
            self.credentials = credentials
        if realm:
            self.realm = realm
        if parameters:
            self.parameters = parameters


# For backwards compatibility
AuthToken = Auth


def basic_auth(user, password, realm=None):
    """Generate a basic auth token for a given user and password.

    This will set the scheme to "basic" for the auth token.

    :param user: user name, this will set the
    :type user: str
    :param password: current password, this will set the credentials
    :type password: str
    :param realm: specifies the authentication provider
    :type realm: str or None

    :return: auth token for use with :meth:`GraphDatabase.driver` or
        :meth:`AsyncGraphDatabase.driver`
    :rtype: :class:`neo4j.Auth`
    """
    return Auth("basic", user, password, realm)


def kerberos_auth(base64_encoded_ticket):
    """Generate a kerberos auth token with the base64 encoded ticket.

    This will set the scheme to "kerberos" for the auth token.

    :param base64_encoded_ticket: a base64 encoded service ticket, this will set
                                  the credentials
    :type base64_encoded_ticket: str

    :return: auth token for use with :meth:`GraphDatabase.driver` or
        :meth:`AsyncGraphDatabase.driver`
    :rtype: :class:`neo4j.Auth`
    """
    return Auth("kerberos", "", base64_encoded_ticket)


def bearer_auth(base64_encoded_token):
    """Generate an auth token for Single-Sign-On providers.

    This will set the scheme to "bearer" for the auth token.

    :param base64_encoded_token: a base64 encoded authentication token generated
                                 by a Single-Sign-On provider.
    :type base64_encoded_token: str

    :return: auth token for use with :meth:`GraphDatabase.driver` or
        :meth:`AsyncGraphDatabase.driver`
    :rtype: :class:`neo4j.Auth`
    """
    return Auth("bearer", None, base64_encoded_token)


def custom_auth(principal, credentials, realm, scheme, **parameters):
    """Generate a custom auth token.

    :param principal: specifies who is being authenticated
    :type principal: str or None
    :param credentials: authenticates the principal
    :type credentials: str or None
    :param realm: specifies the authentication provider
    :type realm: str or None
    :param scheme: specifies the type of authentication
    :type scheme: str or None
    :param parameters: extra key word parameters passed along to the
                       authentication provider
    :type parameters: Dict[str, Any]

    :return: auth token for use with :meth:`GraphDatabase.driver` or
        :meth:`AsyncGraphDatabase.driver`
    :rtype: :class:`neo4j.Auth`
    """
    return Auth(scheme, principal, credentials, realm, **parameters)


# TODO 6.0 - remove this class
class Bookmark:
    """A Bookmark object contains an immutable list of bookmark string values.

    .. deprecated:: 5.0
        `Bookmark` will be removed in version 6.0.
        Use :class:`Bookmarks` instead.

    :param values: ASCII string values
    """

    @deprecated("Use the `Bookmarks`` class instead.")
    def __init__(self, *values):
        if values:
            bookmarks = []
            for ix in values:
                try:
                    if ix:
                        ix.encode("ascii")
                        bookmarks.append(ix)
                except UnicodeEncodeError as e:
                    raise ValueError("The value {} is not ASCII".format(ix))
            self._values = frozenset(bookmarks)
        else:
            self._values = frozenset()

    def __repr__(self):
        """
        :return: repr string with sorted values
        """
        return "<Bookmark values={{{}}}>".format(", ".join(["'{}'".format(ix) for ix in sorted(self._values)]))

    def __bool__(self):
        return bool(self._values)

    @property
    def values(self):
        """
        :return: immutable list of bookmark string values
        :rtype: frozenset
        """
        return self._values


class Bookmarks:
    """Container for an immutable set of bookmark string values.

    Bookmarks are used to causally chain session.
    See :meth:`Session.last_bookmarks` or :meth:`AsyncSession.last_bookmarks`
    for more information.

    Use addition to combine multiple Bookmarks objects::

        bookmarks3 = bookmarks1 + bookmarks2
    """

    def __init__(self):
        self._raw_values = frozenset()

    def __repr__(self):
        """
        :return: repr string with sorted values
        """
        return "<Bookmarks values={{{}}}>".format(
            ", ".join(map(repr, sorted(self._raw_values)))
        )

    def __bool__(self):
        return bool(self._raw_values)

    def __add__(self, other):
        if isinstance(other, Bookmarks):
            if not other:
                return self
            ret = self.__class__()
            ret._raw_values = self._raw_values | other._raw_values
            return ret
        return NotImplemented

    @property
    def raw_values(self):
        """The raw bookmark values.

        You should not need to access them unless you want to serialize
        bookmarks.

        :return: immutable list of bookmark string values
        :rtype: frozenset[str]
        """
        return self._raw_values

    @classmethod
    def from_raw_values(cls, values):
        """Create a Bookmarks object from a list of raw bookmark string values.

        You should not need to use this method unless you want to deserialize
        bookmarks.

        :param values: ASCII string values (raw bookmarks)
        :type values: Iterable[str]
        """
        obj = cls()
        bookmarks = []
        for value in values:
            if not isinstance(value, str):
                raise TypeError("Raw bookmark values must be str. "
                                "Found {}".format(type(value)))
            try:
                value.encode("ascii")
            except UnicodeEncodeError as e:
                raise ValueError(f"The value {value} is not ASCII") from e
            bookmarks.append(value)
        obj._raw_values = frozenset(bookmarks)
        return obj


class ServerInfo:
    """ Represents a package of information relating to a Neo4j server.
    """

    def __init__(self, address, protocol_version):
        self._address = address
        self._protocol_version = protocol_version
        self._metadata = {}

    @property
    def address(self):
        """ Network address of the remote server.
        """
        return self._address

    @property
    def protocol_version(self):
        """ Bolt protocol version with which the remote server
        communicates. This is returned as a :class:`.Version`
        object, which itself extends a simple 2-tuple of
        (major, minor) integers.
        """
        return self._protocol_version

    @property
    def agent(self):
        """ Server agent string by which the remote server identifies
        itself.
        """
        return self._metadata.get("server")

    @property
    @deprecated("The connection id is considered internal information "
                "and will no longer be exposed in future versions.")
    def connection_id(self):
        """ Unique identifier for the remote server connection.
        """
        return self._metadata.get("connection_id")

    def update(self, metadata):
        """ Update server information with extra metadata. This is
        typically drawn from the metadata received after successful
        connection initialisation.
        """
        self._metadata.update(metadata)


class Version(tuple):

    def __new__(cls, *v):
        return super().__new__(cls, v)

    def __repr__(self):
        return "{}{}".format(self.__class__.__name__, super().__repr__())

    def __str__(self):
        return ".".join(map(str, self))

    def to_bytes(self):
        b = bytearray(4)
        for i, v in enumerate(self):
            if not 0 <= i < 2:
                raise ValueError("Too many version components")
            if isinstance(v, list):
                b[-i - 1] = int(v[0] % 0x100)
                b[-i - 2] = int((v[0] - v[-1]) % 0x100)
            else:
                b[-i - 1] = int(v % 0x100)
        return bytes(b)

    @classmethod
    def from_bytes(cls, b):
        b = bytearray(b)
        if len(b) != 4:
            raise ValueError("Byte representation must be exactly four bytes")
        if b[0] != 0 or b[1] != 0:
            raise ValueError("First two bytes must contain zero")
        return Version(b[-1], b[-2])


def parse_neo4j_uri(uri):
    parsed = urlparse(uri)

    if parsed.username:
        raise ConfigurationError("Username is not supported in the URI")

    if parsed.password:
        raise ConfigurationError("Password is not supported in the URI")

    if parsed.scheme == URI_SCHEME_BOLT_ROUTING:
        raise ConfigurationError("Uri scheme {!r} have been renamed. Use {!r}".format(parsed.scheme, URI_SCHEME_NEO4J))
    elif parsed.scheme == URI_SCHEME_BOLT:
        driver_type = DRIVER_BOLT
        security_type = SECURITY_TYPE_NOT_SECURE
    elif parsed.scheme == URI_SCHEME_BOLT_SELF_SIGNED_CERTIFICATE:
        driver_type = DRIVER_BOLT
        security_type = SECURITY_TYPE_SELF_SIGNED_CERTIFICATE
    elif parsed.scheme == URI_SCHEME_BOLT_SECURE:
        driver_type = DRIVER_BOLT
        security_type = SECURITY_TYPE_SECURE
    elif parsed.scheme == URI_SCHEME_NEO4J:
        driver_type = DRIVER_NEO4j
        security_type = SECURITY_TYPE_NOT_SECURE
    elif parsed.scheme == URI_SCHEME_NEO4J_SELF_SIGNED_CERTIFICATE:
        driver_type = DRIVER_NEO4j
        security_type = SECURITY_TYPE_SELF_SIGNED_CERTIFICATE
    elif parsed.scheme == URI_SCHEME_NEO4J_SECURE:
        driver_type = DRIVER_NEO4j
        security_type = SECURITY_TYPE_SECURE
    else:
        raise ConfigurationError("URI scheme {!r} is not supported. Supported URI schemes are {}. Examples: bolt://host[:port] or neo4j://host[:port][?routing_context]".format(
            parsed.scheme,
            [
                URI_SCHEME_BOLT,
                URI_SCHEME_BOLT_SELF_SIGNED_CERTIFICATE,
                URI_SCHEME_BOLT_SECURE,
                URI_SCHEME_NEO4J,
                URI_SCHEME_NEO4J_SELF_SIGNED_CERTIFICATE,
                URI_SCHEME_NEO4J_SECURE
            ]
        ))

    return driver_type, security_type, parsed


def check_access_mode(access_mode):
    if access_mode is None:
        return WRITE_ACCESS
    if access_mode not in (READ_ACCESS, WRITE_ACCESS):
        msg = "Unsupported access mode {}".format(access_mode)
        raise ConfigurationError(msg)

    return access_mode


def parse_routing_context(query):
    """ Parse the query portion of a URI to generate a routing context dictionary.
    """
    if not query:
        return {}

    context = {}
    parameters = parse_qs(query, True)
    for key in parameters:
        value_list = parameters[key]
        if len(value_list) != 1:
            raise ConfigurationError("Duplicated query parameters with key '%s', value '%s' found in query string '%s'" % (key, value_list, query))
        value = value_list[0]
        if not value:
            raise ConfigurationError("Invalid parameters:'%s=%s' in query string '%s'." % (key, value, query))
        context[key] = value

    return context
