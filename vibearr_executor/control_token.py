"""Read only operator-selected regular tokens with trusted permissions."""

import os
from pathlib import Path
import stat

from .contracts import Rejected


SHARED_PATHS = {
    Path("/run/vibearr/worker/control-token"),
    Path("/run/vibearr/worker/mediation-token"),
}


def readonly_token_mount(path):
    if path not in SHARED_PATHS:
        return False
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if len(fields) > 5 and fields[4] == str(path.parent):
            return "ro" in fields[5].split(",")
    return False


def read_control_token(path):
    path = Path(path).absolute()
    if path.resolve(strict=True) != path:
        raise Rejected("Control token path cannot contain links.")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 512:
            raise Rejected("Control token must be a bounded regular private file.")
        mode = stat.S_IMODE(info.st_mode)
        private = (
            mode == 0o600 and info.st_uid in {0, 10001} and os.geteuid() in {0, 10001}
        )
        shared = (
            mode == 0o440
            and info.st_uid == 0
            and info.st_gid == 10001
            and readonly_token_mount(path)
            and os.fstatvfs(stream.fileno()).f_flag & os.ST_RDONLY
        )
        if not private and not shared:
            raise Rejected("Control token ownership, mode or mount is unsafe.")
        content = stream.read(513)
    try:
        token = content.decode("ascii").strip()
    except UnicodeDecodeError:
        raise Rejected("Control token must be ASCII.") from None
    if not 32 <= len(token) <= 512 or any(not 33 <= ord(char) <= 126 for char in token):
        raise Rejected("Control token is missing or malformed.")
    return token
