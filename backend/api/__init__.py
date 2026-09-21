# backend/api/__init__.py

"""Package initializer for API routers.
We expose the routers here so that main.py can import them cleanly.
"""

from .rooms import router as rooms_router
from .social import router as social_router
