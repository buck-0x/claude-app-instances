import fcntl
import os
import resource

import pytest

from claude_instances.secret import Secret, read_secret_fd


def test_repr_and_str_redact():
    s = Secret("hunter2")
    assert repr(s) == "Secret(<redacted>)" and str(s) == "Secret(<redacted>)"
    assert "hunter2" not in f"{s!r}{s}"
    assert s.reveal() == "hunter2"


def test_secret_not_in_format_of_container():
    assert "hunter2" not in repr({"k": Secret("hunter2")})


def test_read_secret_fd_from_pipe():
    r, w = os.pipe()
    os.write(w, b"pw\n")
    os.close(w)
    assert read_secret_fd(r) == Secret("pw")


def test_read_secret_fd_closed_fd_is_none():
    fd = resource.getrlimit(resource.RLIMIT_NOFILE)[0] - 1  # far above anything pytest opens
    with pytest.raises(OSError):
        fcntl.fcntl(fd, fcntl.F_GETFD)  # proves it is not open
    assert read_secret_fd(fd) is None


def _pipe_with(payload: bytes) -> int:
    r, w = os.pipe()
    os.write(w, payload)
    os.close(w)
    return r


def test_read_secret_fd_lone_newline_is_none():
    assert read_secret_fd(_pipe_with(b"\n")) is None


def test_read_secret_fd_closes_fd():
    r = _pipe_with(b"pw\n")
    read_secret_fd(r)
    with pytest.raises(OSError):
        os.fstat(r)


def test_read_secret_fd_non_utf8_raises_sanitized_error():
    payload = b"hunter2\xff"
    with pytest.raises(ValueError) as excinfo:
        read_secret_fd(_pipe_with(payload))
    err = excinfo.value
    assert type(err) is ValueError  # not UnicodeDecodeError, whose .object holds the payload
    assert err.__cause__ is None and err.__suppress_context__
    assert "hunter2" not in str(err) and "\\xff" not in str(err) and "0xff" not in str(err)
    tb = excinfo.traceback[-1]
    assert all("hunter2" not in repr(v) for v in tb.locals.values())
