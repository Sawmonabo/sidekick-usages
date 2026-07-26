# Wheel Artifact Contract Research

## Decision

Replace the exhaustive filename tuple in `packaging/smoke_wheel.py` with one
source-derived contract:

1. Read the sole package root from Hatch's existing `packages` declaration.
2. Derive all regular Python members below that root.
3. Reject symlinks, undeclared package data, alternate selection mechanisms,
   duplicate archive members, missing members, and extra members.
4. Require the wheel and sdist package trees to equal that derived set.
5. Keep the isolated installed-wheel CLI smoke.

This makes `pyproject.toml` and the actual package tree the authority. Adding or
removing a Python module no longer requires a second packaging edit.

## Why this design

[Hatch documents `packages = ["src/foo"]`][hatch-packages] as explicit source
selection whose prefix is collapsed in the wheel. That directly defines
Sidekick's package mapping without another manifest.

The [wheel standard][wheel-standard] already assigns archive inventory and
integrity to `.dist-info/RECORD`, and installers verify members against it.
Sidekick should retain a real isolated installation instead of recreating the
wheel standard.

The [sdist standard][sdist-standard] deliberately leaves most included content
to the build backend, so Sidekick still needs the narrow source-to-sdist
package-tree check.

## Build versus adopt

The maintained [`check-wheel-contents` project][check-wheel-contents] supports
wheel-to-source parity through its `--package` checks.

It was not adopted because it does not replace Sidekick's sdist parity,
artifact naming, isolated install, or CLI checks. Adding it would introduce
another dependency and process while leaving local orchestration in place.
The chosen local mechanism is deliberately smaller: one configuration read,
one bounded source walk, and exact set comparisons for both artifacts.

`check-wheel-contents` remains a reasonable future addition if generic wheel
lint rules become a separate requirement.

## Implementation ownership

The stable `packaging/smoke_wheel.py` executable is intentionally only command
composition and error rendering. The cohesive `packaging/wheel_verification/`
package owns:

- project and Hatch contract loading in `project.py`;
- wheel, `RECORD`, and source-distribution inspection in `artifacts.py`;
- isolated subprocess and installed-wheel behavior in `runtime.py`;
- build and verification sequencing in `service.py`; and
- command parsing in `cli.py`.

One immutable project model is loaded once and passed between those owners.
This avoids mutable module configuration, dynamic test imports, repeated TOML
decoding, and a flat verifier that mixes archive, process, and CLI policy.

The public CLI is exercised through its installed console script and
`python -m` entry point. The internal supervisor and worker targets use a
static-import subprocess probe because a successful real invocation is not a
bounded smoke operation: the supervisor runs until signaled and owns service
state, while the worker requires a real durable operation and may mutate it.
The verifier first requires the exact internal metadata targets, then imports
those named modules and checks their `main` callables without dynamic loading.

## Failure policy

The verifier fails closed when the build mapping changes. Legitimate package
data, a second package root, Hatch include/exclude rules, forced files, source
rewrites, or build artifacts require an explicit design update rather than
being silently accepted.

## Verification evidence

A live build on 2026-07-24 produced:

- 205 source-derived package members;
- 205 wheel package members;
- 205 sdist package members;
- 210 wheel members and 210 `RECORD` rows.

The refactored verifier then passed its isolated wheel build and runtime smoke
without an exhaustive or generated filename list.

[check-wheel-contents]: https://github.com/jwodder/check-wheel-contents
[hatch-packages]: https://hatch.pypa.io/1.10/config/build/#packages
[sdist-standard]: https://packaging.python.org/en/latest/specifications/source-distribution-format/#source-distribution-file-format
[wheel-standard]: https://packaging.python.org/en/latest/specifications/binary-distribution-format/#the-dist-info-directory
