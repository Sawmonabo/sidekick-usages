"""Windows owner-only DACL construction and validation."""

import sys
from typing import TYPE_CHECKING

from sidekick_usages.persistence._platform import (
    NativeFailureKind,
    NativeFilesystemError,
)

if TYPE_CHECKING and sys.platform == "win32":
    import _win32typing

if sys.platform == "win32":
    import pywintypes
    import win32api
    import win32file
    import win32security

    def _unsafe() -> NativeFilesystemError:
        return NativeFilesystemError(NativeFailureKind.UNSAFE)

    def _current_user_sid() -> _win32typing.PySID:
        try:
            token = win32security.OpenProcessToken(
                win32api.GetCurrentProcess(),
                win32security.TOKEN_QUERY,
            )
            try:
                sid, _attributes = win32security.GetTokenInformation(
                    token,
                    win32security.TokenUser,
                )
                if not sid.IsValid():
                    raise _unsafe()
                return sid
            finally:
                win32api.CloseHandle(token)
        except pywintypes.error:
            raise _unsafe() from None

    def _allowed_sids() -> tuple[
        _win32typing.PySID,
        _win32typing.PySID,
        _win32typing.PySID,
    ]:
        try:
            return (
                _current_user_sid(),
                win32security.CreateWellKnownSid(
                    win32security.WinLocalSystemSid,
                ),
                win32security.CreateWellKnownSid(
                    win32security.WinBuiltinAdministratorsSid,
                ),
            )
        except pywintypes.error:
            raise _unsafe() from None

    def _private_acl(*, directory: bool) -> _win32typing.PyACL:
        current_user, local_system, administrators = _allowed_sids()
        access = win32file.FILE_ALL_ACCESS
        acl = win32security.ACL()
        if directory:
            inheritance = (
                win32security.OBJECT_INHERIT_ACE
                | win32security.CONTAINER_INHERIT_ACE
            )
            for sid in (current_user, local_system, administrators):
                acl.AddAccessAllowedAceEx(
                    win32security.ACL_REVISION,
                    inheritance,
                    access,
                    sid,
                )
        else:
            for sid in (current_user, local_system, administrators):
                acl.AddAccessAllowedAce(
                    win32security.ACL_REVISION,
                    access,
                    sid,
                )
        return acl

    def private_security_attributes(
        *,
        directory: bool,
    ) -> _win32typing.PySECURITY_ATTRIBUTES:
        """Build a protected owner/system/administrators DACL."""
        current_user = _current_user_sid()
        acl = _private_acl(directory=directory)
        descriptor = win32security.SECURITY_DESCRIPTOR()
        descriptor.Initialize()
        descriptor.SetSecurityDescriptorOwner(current_user, False)
        descriptor.SetSecurityDescriptorDacl(True, acl, False)
        descriptor.SetSecurityDescriptorControl(
            win32security.SE_DACL_PROTECTED,
            win32security.SE_DACL_PROTECTED,
        )
        attributes = win32security.SECURITY_ATTRIBUTES()
        attributes.bInheritHandle = False
        attributes.SECURITY_DESCRIPTOR = descriptor
        return attributes

    def validate_repair_owner(handle: int) -> None:
        """Require a valid current-user-owned object before DACL repair."""
        try:
            descriptor = win32security.GetSecurityInfo(
                handle,
                win32security.SE_FILE_OBJECT,
                win32security.OWNER_SECURITY_INFORMATION,
            )
            if (
                not descriptor.IsValid()
                or descriptor.GetSecurityDescriptorOwner()
                != _current_user_sid()
            ):
                raise _unsafe()
        except pywintypes.error:
            raise _unsafe() from None

    def repair_security(handle: int, *, directory: bool) -> None:
        """Install and verify the exact protected private DACL."""
        validate_repair_owner(handle)
        try:
            descriptor = win32security.GetSecurityInfo(
                handle,
                win32security.SE_FILE_OBJECT,
                win32security.OWNER_SECURITY_INFORMATION,
            )
            owner = descriptor.GetSecurityDescriptorOwner()
            ignored_sacl = win32security.ACL()
            win32security.SetSecurityInfo(
                handle,
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION
                | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                owner,
                owner,
                _private_acl(directory=directory),
                ignored_sacl,
            )
        except pywintypes.error:
            raise _unsafe() from None
        validate_security(handle, directory=directory)

    def _validate_acl(
        dacl: _win32typing.PyACL,
        *,
        directory: bool,
    ) -> None:
        try:
            allowed = _allowed_sids()
            if dacl.GetAceCount() != len(allowed):
                raise _unsafe()
            seen = [False for _sid in allowed]
            expected_flags = (
                win32security.OBJECT_INHERIT_ACE
                | win32security.CONTAINER_INHERIT_ACE
                if directory
                else 0
            )
            for index in range(dacl.GetAceCount()):
                header, mask, sid = dacl.GetAce(index)
                ace_type, ace_flags = header
                if (
                    ace_type != win32security.ACCESS_ALLOWED_ACE_TYPE
                    or ace_flags & win32security.INHERITED_ACE
                    or ace_flags != expected_flags
                    or mask != win32file.FILE_ALL_ACCESS
                ):
                    raise _unsafe()
                positions = tuple(
                    position
                    for position, allowed_sid in enumerate(allowed)
                    if sid == allowed_sid
                )
                if len(positions) != 1 or seen[positions[0]]:
                    raise _unsafe()
                seen[positions[0]] = True
        except pywintypes.error, TypeError, ValueError:
            raise _unsafe() from None
        if not all(seen):
            raise _unsafe()

    def validate_security(handle: int, *, directory: bool) -> None:
        """Require one protected DACL with effective principal access."""
        try:
            descriptor = win32security.GetSecurityInfo(
                handle,
                win32security.SE_FILE_OBJECT,
                win32security.OWNER_SECURITY_INFORMATION
                | win32security.DACL_SECURITY_INFORMATION,
            )
            if not descriptor.IsValid():
                raise _unsafe()
            owner = descriptor.GetSecurityDescriptorOwner()
            dacl = descriptor.GetSecurityDescriptorDacl()
            control = descriptor.GetSecurityDescriptorControl()[0]
        except pywintypes.error:
            raise _unsafe() from None
        if (
            owner != _current_user_sid()
            or dacl is None
            or type(control) is not int
            or not control & win32security.SE_DACL_PROTECTED
        ):
            raise _unsafe()
        _validate_acl(dacl, directory=directory)


__all__ = [
    "private_security_attributes",
    "repair_security",
    "validate_repair_owner",
    "validate_security",
]
