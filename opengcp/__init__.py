"""opengcp - an independent, open-source LOCAL reimplementation of core
Google Cloud Platform primitives for offline development and testing.

This package is NOT affiliated with, endorsed by, or sponsored by Google.
Vendor names are used only nominatively to describe API compatibility.
"""

__version__ = "0.3.0"

from .storage import ObjectStorage
from .firestore import DocumentStore
from .pubsub import PubSub, DeadLetterPolicy
from .functions import FunctionRunner
from .datastore import DatastoreDB, Key as DSKey
from .bigtable import BigtableAdmin
from .bigquery import BigQueryDB
from .tasks import CloudTasks, Queue as TaskQueue, RetryConfig, RateLimits
from .scheduler import CloudScheduler
from .cloudrun import CloudRun, ServiceConfig

__all__ = [
    "ObjectStorage",
    "DocumentStore",
    "PubSub",
    "DeadLetterPolicy",
    "FunctionRunner",
    "DatastoreDB",
    "DSKey",
    "BigtableAdmin",
    "BigQueryDB",
    "CloudTasks",
    "TaskQueue",
    "RetryConfig",
    "RateLimits",
    "CloudScheduler",
    "CloudRun",
    "ServiceConfig",
    "__version__",
]
