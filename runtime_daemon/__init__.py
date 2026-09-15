"""CoreMan external runtime, Python 3.10+ with bundled Go AI drivers."""

__version__ = "0.1.0"
# Node protocol version reported at enroll and heartbeat. 2 adds long polling,
# batched response frames and node concurrency telemetry.
PROTOCOL_VERSION = 2
