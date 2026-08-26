"""av2ra.buildkit -- worktrees, hermetic builds, and the artifact cache.

Named ``buildkit`` rather than ``build`` on purpose. Almost every C project's
``.gitignore`` contains a bare ``build/`` rule, and the AVM tree's does: a
package called ``build`` is silently dropped when this project is committed
inside such a repository, and the failure appears far downstream as an import
error in a fresh checkout. It cost one round trip to find here, which is one
more than the name is worth.
"""
