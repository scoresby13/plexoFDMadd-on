"""Plexo bridge for Free Download Manager.

A small local HTTP server that FDM downloads from. Behind it, every file is cut into blocks and
fetched in parallel over every network the computer has, each stream pinned to one network, the
way Plexo (https://github.com/anmolkapil/plexo) does it. Standard library only, so the Python FDM
installs for add-ons is all it needs.
"""

VERSION = "1.0.0"
