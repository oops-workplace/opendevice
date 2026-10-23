from __future__ import annotations


class OpenDeviceError(Exception):
    'Base class for every opendevice error.'


class MuxError(OpenDeviceError):
    'usbmux communication failed.'


class NotPairedError(OpenDeviceError):
    'No pairing record for this device (created after trusting this computer).'


class ServiceError(OpenDeviceError):
    'lockdownd failed to start the service, or the service returned an error.'


class LockdownError(OpenDeviceError):
    'lockdownd returned an error.'


class TunnelError(OpenDeviceError):
    'Tunnel handshake failed.'


class DeviceBusyError(OpenDeviceError):
    'The device is held by another process (long tasks are exclusive, see opendevice.core.lock).'


class AfcError(OpenDeviceError):
    'AFC (device filesystem) operation failed.'


class InstallError(OpenDeviceError):
    'Install / uninstall failed.'


class DeveloperModeError(OpenDeviceError):
    'Developer mode operation failed (for example, the device has a passcode).'


class NotSupportedError(OpenDeviceError):
    'The device does not support this command / field.'
