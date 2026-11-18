"""Deny network before collection; use with an empty environment and a temporary HOME."""

import os
import socket
import sys


def denied(*args, **kwargs):
    raise AssertionError("Network is forbidden in offline verification")


socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
socket.getaddrinfo = denied
socket.gethostbyname = denied
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
os.environ["PYDANTIC_AI_NO_BANNER"] = "1"

import pytest  # noqa: E402

sys.exit(pytest.main(["-p", "pytest_asyncio.plugin", "-p", "no:cacheprovider", *sys.argv[1:]]))
