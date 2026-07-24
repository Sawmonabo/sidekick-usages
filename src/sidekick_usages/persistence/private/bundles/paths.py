"""Bounded namespace grammar for private credential bundles."""

import ntpath
import unicodedata

from sidekick_usages.persistence.artifacts import (
    portable_basename_key,
    require_safe_basename,
)

MAX_PRIVATE_BUNDLE_COMPONENTS = 8
MAX_PRIVATE_BUNDLE_COMPONENT_BYTES = 255
MAX_PRIVATE_BUNDLE_PATH_BYTES = 1024
PRIVATE_TRANSACTION_DIRECTORY = ".credential-transaction"
PRIVATE_TRANSACTION_JOURNAL = "journal.json"


def _portable_component_key(value: str) -> str:
    return unicodedata.normalize(
        "NFC",
        portable_basename_key(value),
    ).casefold()


def _validate_private_bundle_component(component: str) -> None:
    require_safe_basename(component)
    try:
        encoded = component.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(
            "Private bundle components must be valid UTF-8."
        ) from None
    if len(encoded) > MAX_PRIVATE_BUNDLE_COMPONENT_BYTES:
        raise ValueError("Private bundle component is too long.")
    if unicodedata.normalize("NFC", component) != component:
        raise ValueError("Private bundle components must use NFC text.")
    if (
        ":" in component
        or ntpath.isreserved(component)
        or (component.partition(".")[0].casefold() == "clock$")
    ):
        raise ValueError("Private bundle component is platform-reserved.")


def private_bundle_relative_components(value: str) -> tuple[str, ...]:
    """Validate and split one portable private-bundle relative path."""
    if not isinstance(value, str):
        raise TypeError("Private bundle relative path must be text.")
    if not value or value.startswith("/") or "\\" in value:
        raise ValueError("Private bundle path must be relative POSIX text.")
    components = tuple(value.split("/"))
    if len(components) > MAX_PRIVATE_BUNDLE_COMPONENTS or any(
        not component for component in components
    ):
        raise ValueError("Private bundle path has unsupported depth.")
    for component in components:
        _validate_private_bundle_component(component)
        if _portable_component_key(component) == _portable_component_key(
            PRIVATE_TRANSACTION_DIRECTORY
        ):
            raise ValueError("Private bundle path uses a reserved namespace.")
    try:
        joined_size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("Private bundle path must be valid UTF-8.") from None
    if joined_size > MAX_PRIVATE_BUNDLE_PATH_BYTES:
        raise ValueError("Private bundle path is too long.")
    return components


def portable_private_bundle_path_key(value: str) -> tuple[str, ...]:
    """Return the portable namespace identity of a validated bundle path."""
    return tuple(
        _portable_component_key(component)
        for component in private_bundle_relative_components(value)
    )


def require_portable_unique_private_bundle_paths(
    values: tuple[str, ...],
) -> None:
    """Reject aliases, ancestor collisions, and inconsistent path spelling."""
    paths = tuple(
        (value, private_bundle_relative_components(value)) for value in values
    )
    keys = tuple(
        tuple(_portable_component_key(part) for part in components)
        for _value, components in paths
    )
    if len(keys) != len(set(keys)):
        raise ValueError(
            "Private bundle paths must be unique in the portable namespace."
        )
    for index, key in enumerate(keys):
        for other in keys[index + 1 :]:
            common = min(len(key), len(other))
            if key[:common] == other[:common]:
                raise ValueError(
                    "Private bundle paths must not contain one another."
                )
    spellings: dict[tuple[tuple[str, ...], str], str] = {}
    for _value, components in paths:
        parent: tuple[str, ...] = ()
        for component in components:
            key = _portable_component_key(component)
            identity = (parent, key)
            if (
                existing := spellings.get(identity)
            ) is not None and existing != component:
                raise ValueError(
                    "Private bundle paths use inconsistent portable aliases."
                )
            spellings[identity] = component
            parent = (*parent, key)
