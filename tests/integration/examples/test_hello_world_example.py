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


from contextlib import redirect_stdout
from io import StringIO

import pytest

from neo4j._exceptions import BoltHandshakeError
from neo4j.exceptions import ServiceUnavailable


# isort: off
# tag::hello-world-import[]
from neo4j import GraphDatabase
# end::hello-world-import[]
# isort: on


# python -m pytest tests/integration/examples/test_hello_world_example.py -s -v

# tag::hello-world[]
class HelloWorldExample:

    def __init__(self, uri, user, password):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self):
        self.driver.close()

    def print_greeting(self, message):
        with self.driver.session() as session:
            greeting = session.write_transaction(self._create_and_return_greeting, message)
            print(greeting)

    @staticmethod
    def _create_and_return_greeting(tx, message):
        result = tx.run("CREATE (a:Greeting) "
                        "SET a.message = $message "
                        "RETURN a.message + ', from node ' + id(a)", message=message)
        return result.single()[0]


if __name__ == "__main__":
    greeter = HelloWorldExample("bolt://localhost:7687", "neo4j", "password")
    greeter.print_greeting("hello, world")
    greeter.close()
# end::hello-world[]

# tag::hello-world-output[]
# hello, world, from node 1234
# end::hello-world-output[]


def test_hello_world_example(uri, auth):
    try:
        s = StringIO()
        with redirect_stdout(s):
            example = HelloWorldExample(uri, auth[0], auth[1])
            example.print_greeting("hello, world")
            example.close()

        assert s.getvalue().startswith("hello, world, from node ")
    except ServiceUnavailable as error:
        if isinstance(error.__cause__, BoltHandshakeError):
            pytest.skip(error.args[0])
