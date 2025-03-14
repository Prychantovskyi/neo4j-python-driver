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


import inspect

import pytest

from neo4j import ServerInfo
from neo4j._deadline import Deadline
from neo4j._sync.io import Bolt


@pytest.fixture
def fake_connection_generator(session_mocker):
    mock = session_mocker.mock_module

    class FakeConnection(mock.NonCallableMagicMock):
        callbacks = []
        server_info = ServerInfo("127.0.0.1", (4, 3))
        local_port = 1234

        def __init__(self, *args, **kwargs):
            kwargs["spec"] = Bolt
            super().__init__(*args, **kwargs)
            self.attach_mock(mock.Mock(return_value=True), "is_reset_mock")
            self.attach_mock(mock.Mock(return_value=False), "defunct")
            self.attach_mock(mock.Mock(return_value=False), "stale")
            self.attach_mock(mock.Mock(return_value=False), "closed")
            self.attach_mock(mock.Mock(return_value=False), "socket")
            self.attach_mock(mock.Mock(), "unresolved_address")

            def close_side_effect():
                self.closed.return_value = True

            self.attach_mock(mock.Mock(side_effect=close_side_effect),
                             "close")

            self.socket.attach_mock(
                mock.Mock(return_value=None), "get_deadline"
            )

            def set_deadline_side_effect(deadline):
                deadline = Deadline.from_timeout_or_deadline(deadline)
                self.socket.get_deadline.return_value = deadline

            self.socket.attach_mock(
                mock.Mock(side_effect=set_deadline_side_effect), "set_deadline"
            )

        @property
        def is_reset(self):
            if self.closed.return_value or self.defunct.return_value:
                raise AssertionError(
                    "is_reset should not be called on a closed or defunct "
                    "connection."
                )
            return self.is_reset_mock()

        def fetch_message(self, *args, **kwargs):
            if self.callbacks:
                cb = self.callbacks.pop(0)
                cb()
            return super().__getattr__("fetch_message")(*args, **kwargs)

        def fetch_all(self, *args, **kwargs):
            while self.callbacks:
                cb = self.callbacks.pop(0)
                cb()
            return super().__getattr__("fetch_all")(*args, **kwargs)

        def __getattr__(self, name):
            parent = super()

            def build_message_handler(name):
                def func(*args, **kwargs):
                    def callback():
                        for cb_name, param_count in (
                            ("on_success", 1),
                            ("on_summary", 0)
                        ):
                            cb = kwargs.get(cb_name, None)
                            if callable(cb):
                                try:
                                    param_count = \
                                        len(inspect.signature(cb).parameters)
                                except ValueError:
                                    # e.g. built-in method as cb
                                    pass
                                if param_count == 1:
                                    res = cb({})
                                else:
                                    res = cb()
                                try:
                                    res  # maybe the callback is async
                                except TypeError:
                                    pass  # or maybe it wasn't ;)

                    self.callbacks.append(callback)

                return func

            method_mock = parent.__getattr__(name)
            if name in ("run", "commit", "pull", "rollback", "discard"):
                method_mock.side_effect = build_message_handler(name)
            return method_mock

    return FakeConnection


@pytest.fixture
def fake_connection(fake_connection_generator):
    return fake_connection_generator()


@pytest.fixture
def scripted_connection_generator(fake_connection_generator):
    class ScriptedConnection(fake_connection_generator):
        _script = []
        _script_pos = 0

        def set_script(self, callbacks):
            """Set a scripted sequence of callbacks.

            :param callbacks: The callbacks. They should be a list of 2-tuples.
                `("name_of_message", {"callback_name": arguments})`. E.g.,
                ```
                [
                    ("run", {"on_success": ({},), "on_summary": None}),
                    ("pull", {
                        "on_success": None,
                        "on_summary": None,
                        "on_records":
                    })
                ]
                ```
                Note that arguments can be `None`. In this case, ScriptedConnection
                will make a guess on best-suited default arguments.
            """
            self._script = callbacks
            self._script_pos = 0

        def __getattr__(self, name):
            parent = super()

            def build_message_handler(name):
                try:
                    expected_message, scripted_callbacks = \
                        self._script[self._script_pos]
                except IndexError:
                    pytest.fail("End of scripted connection reached.")
                assert name == expected_message
                self._script_pos += 1

                def func(*args, **kwargs):
                    def callback():
                        for cb_name, default_cb_args in (
                            ("on_ignored", ({},)),
                            ("on_failure", ({},)),
                            ("on_records", ([],)),
                            ("on_success", ({},)),
                            ("on_summary", ()),
                        ):
                            cb = kwargs.get(cb_name, None)
                            if (not callable(cb)
                                or cb_name not in scripted_callbacks):
                                continue
                            cb_args = scripted_callbacks[cb_name]
                            if cb_args is None:
                                cb_args = default_cb_args
                            res = cb(*cb_args)
                            try:
                                res  # maybe the callback is async
                            except TypeError:
                                pass  # or maybe it wasn't ;)

                    self.callbacks.append(callback)

                return func

            method_mock = parent.__getattr__(name)
            if name in ("run", "commit", "pull", "rollback", "discard"):
                method_mock.side_effect = build_message_handler(name)
            return method_mock

    return ScriptedConnection


@pytest.fixture
def scripted_connection(scripted_connection_generator):
    return scripted_connection_generator()
