# backend/models/__init__.py

"""Expose model Base classes for import.
Each model file defines its own declarative Base, but we expose them here
so that init_db can import and create tables.
"""

# Import all model modules so that their Base metadata is registered.
from .user import Base as UserBase
from .room import Base as RoomBase
from .game import Base as GameBase
from .social import Base as SocialBase

# Provide a list for convenience
MODEL_BASES = [UserBase, RoomBase, GameBase, SocialBase]
