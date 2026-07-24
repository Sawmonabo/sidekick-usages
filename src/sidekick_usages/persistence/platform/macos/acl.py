"""macOS descriptor-based extended ACL inspection."""

import ctypes
import errno
import os
from functools import cache

_ACL_TYPE_EXTENDED = 0x00000100
_ACL_FIRST_ENTRY = 0


@cache
def _libsystem() -> ctypes.CDLL:
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    library.acl_get_fd_np.argtypes = (ctypes.c_int, ctypes.c_int)
    library.acl_get_fd_np.restype = ctypes.c_void_p
    library.acl_valid.argtypes = (ctypes.c_void_p,)
    library.acl_valid.restype = ctypes.c_int
    library.acl_get_entry.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
    )
    library.acl_get_entry.restype = ctypes.c_int
    library.acl_free.argtypes = (ctypes.c_void_p,)
    library.acl_free.restype = ctypes.c_int
    return library


def _system_error() -> OSError:
    error_number = ctypes.get_errno() or errno.EIO
    return OSError(error_number, os.strerror(error_number))


def has_extended_acl(descriptor: int) -> bool:
    """Return whether a macOS descriptor has any extended ACL entry."""
    library = _libsystem()
    ctypes.set_errno(0)
    pointer_value = library.acl_get_fd_np(descriptor, _ACL_TYPE_EXTENDED)
    if pointer_value is None:
        if ctypes.get_errno() == errno.ENOENT:
            return False
        raise _system_error()
    acl = ctypes.c_void_p(pointer_value)
    result: bool | None = None
    failure: OSError | None = None
    try:
        if library.acl_valid(acl) != 0:
            failure = _system_error()
        else:
            entry = ctypes.c_void_p()
            ctypes.set_errno(0)
            status = library.acl_get_entry(
                acl,
                _ACL_FIRST_ENTRY,
                ctypes.byref(entry),
            )
            if status == 0:
                result = True
            elif status == -1 and ctypes.get_errno() == errno.EINVAL:
                result = False
            else:
                failure = _system_error()
    finally:
        ctypes.set_errno(0)
        if library.acl_free(acl) != 0 and failure is None:
            failure = _system_error()
    if failure is not None:
        raise failure
    if result is None:
        raise OSError(errno.EIO, os.strerror(errno.EIO))
    return result
