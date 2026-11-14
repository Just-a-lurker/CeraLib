"""CeraLib: community library for PCM03-family BLE espresso machines."""

from .codec import Frame, FrameError, Reassembler, decode, encode  # noqa: F401
from .messages import (  # noqa: F401
    BrewMode, Bucket, DeviceInfo, Settings, State, Stats, Status, c_to_f, f_to_c,
)

__version__ = "0.3.0"
from .scan import Found, discover  # noqa: F401,E402
