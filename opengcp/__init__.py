"""opengcp - an independent, open-source LOCAL reimplementation of core
Google Cloud Platform primitives for offline development and testing.

This package is NOT affiliated with, endorsed by, or sponsored by Google.
Vendor names are used only nominatively to describe API compatibility.
"""

__version__ = "0.1.0"

from .storage import ObjectStorage
from .firestore import DocumentStore
from .pubsub import PubSub
from .functions import FunctionRunner

__all__ = [
    "ObjectStorage",
    "DocumentStore",
    "PubSub",
    "FunctionRunner",
    "__version__",
]
