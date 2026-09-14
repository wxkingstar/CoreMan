import os
import re

__version__ = "0.1.0"
_revision = os.environ.get("COREMAN_BUILD_REVISION", "")
if re.fullmatch(r"[0-9a-f]{7,40}(?:-dirty)?", _revision):
    __version__ += (
        "+"
        + _revision.removesuffix("-dirty")[:12]
        + ("-dirty" if _revision.endswith("-dirty") else "")
    )
