# backend/models/mutable.py

"""Helpers that make SQLAlchemy JSON columns track in-place mutations.

SQLAlchemy does not detect changes like ``room.player_ids.append(x)``
on a plain JSON column unless the value is reassigned or the column type
tracks mutations. Without this, room membership updates are silently lost
(e.g. joining a room does not persist).
"""

from sqlalchemy import JSON
from sqlalchemy.ext.mutable import MutableDict, MutableList

# Drop-in replacements for JSON column definitions:
#   player_ids = Column(JSONList, default=list)
JSONList = MutableList.as_mutable(JSON)
JSONDict = MutableDict.as_mutable(JSON)
