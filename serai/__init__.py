"""serai -- a single attach point for terminal, SSH, and Claude Code sessions."""

# The single source of truth for serai's version: pyproject reads it at build
# time (setuptools dynamic attr), main.py stamps it on gated API responses as
# X-Serai-Version, and the web UI shows it in the status bar (and prompts a
# reload when it changes under an open tab). Bump with any change that ships.
__version__ = "2.16.0"
