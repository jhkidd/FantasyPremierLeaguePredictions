"""The MVP heuristic optimiser: turns a predictions archive partition into
a recommended 15-man squad, starting XI, captaincy and bench order (Phase
D, `.github/context/subsystem3-close-and-mvp-site.md`) - subsystem 4's
first slice. Explicitly a heuristic, not a global optimum; a real MILP
solver is deferred to a later task."""

from __future__ import annotations
