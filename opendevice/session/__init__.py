from __future__ import annotations

from .controls import ControlsMixin
from .display import DisplayMixin
from .link import LinkMixin
from .services import ServicesMixin
from .tree import TreeMixin
from .video import VideoMixin


class DeviceSession(LinkMixin, DisplayMixin, VideoMixin, ControlsMixin, TreeMixin,
                    ServicesMixin):
    'One complete session with a device.'


__all__ = ["DeviceSession"]
