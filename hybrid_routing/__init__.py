"""Hybrid Contextual Inference for Microsoft Scout.

Classifies a task by sensitivity, role and difficulty, then routes it to the
right model across a hybrid stack — Scout's hosted cloud models, on-device
runtimes, and org-hosted endpoints — with an enforced egress boundary.
"""

from .backends import Backend, BackendError, load_backends, probe
from .classify import (
    CONFIDENTIAL,
    NORMAL,
    RESTRICTED,
    Classification,
    Classifier,
    normalize_label,
)
from .config import ConfigError, install_user_config, load_config, resolve_config_path
from .egress import (
    CLOUD_PUBLIC,
    ON_DEVICE,
    ORG_TENANT,
    ModelRef,
    ModelRefError,
    ResolvedEgress,
    normalize_egress,
    parse_model_ref,
    permits,
    resolve_egress,
    url_is_loopback,
)
from .router import (
    ROUTE_BLOCKED,
    ROUTE_OK,
    ROUTE_UNCONFIGURED,
    HybridRouter,
    RoutingDecision,
)

__version__ = "1.2.0"

__all__ = [
    "Backend",
    "BackendError",
    "load_backends",
    "probe",
    "Classification",
    "Classifier",
    "normalize_label",
    "NORMAL",
    "CONFIDENTIAL",
    "RESTRICTED",
    "ConfigError",
    "install_user_config",
    "load_config",
    "resolve_config_path",
    "CLOUD_PUBLIC",
    "ORG_TENANT",
    "ON_DEVICE",
    "ModelRef",
    "ModelRefError",
    "ResolvedEgress",
    "normalize_egress",
    "parse_model_ref",
    "permits",
    "resolve_egress",
    "url_is_loopback",
    "HybridRouter",
    "RoutingDecision",
    "ROUTE_OK",
    "ROUTE_BLOCKED",
    "ROUTE_UNCONFIGURED",
    "__version__",
]


