"""Local process exclusion shared by CLI recovery and direct engine callers."""

import os
import stat
from contextlib import contextmanager


class ActiveRunError(ValueError):
    pass


@contextmanager
def execution_lock(output):
    import fcntl

    output.mkdir(parents=True, exist_ok=True)
    # Never follow a substituted lock symlink or block while opening a FIFO.
    descriptor = os.open(
        output / "execution.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Unsafe execution lock: expected a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ActiveRunError("Another Faultline operation owns this output directory") from exc
        yield
    finally:
        os.close(descriptor)
