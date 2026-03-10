"""
pylxd Audit Wrapper - Audit Logging for LXD Operations.

Provides a decorator-based audit trail for ALL pylxd/LXD operations.
Every operation through the service layer gets an audit event with:
- Pre-operation intent (what will be done)
- Post-operation result (success/failure + timing)
- Target details (host, project, resource type/id)
- Actor context (from RequestContext)
"""

import functools
import logging
import time
from typing import Any, Callable, Dict, Optional

from app.services.activity_logger import get_activity_logger, EventSeverity

logger = logging.getLogger(__name__)


def _extract_audit_params(args, kwargs, param_map: Dict[str, str]) -> Dict[str, Optional[str]]:
    """
    Extract audit-relevant params from function arguments.
    """
    result = {}
    for audit_key, param_name in param_map.items():
        # try kwargs first
        if param_name in kwargs:
            result[audit_key] = str(kwargs[param_name])
        else:
            result[audit_key] = None
    return result


def audit_operation(
    action: str,
    target_type: str,
    *,
    severity: Optional[str] = None,
    target_id_param: str = "name",
    target_host_param: str = "host",
    target_project_param: str = "project",
    extract_details: Optional[Callable] = None,
):
    """
    Decorator for async LXD service methods that adds audit logging.

    Logs both intent (before) and result (after) as a single audit event
    with timing information.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            activity_logger = get_activity_logger()

            # extract target info from function arguments
            # skip 'self' (args[0]) for bound methods
            func_args = args[1:] if args else args

            # build param map from function signature
            import inspect
            sig = inspect.signature(func)
            param_names = list(sig.parameters.keys())
            # skip 'self'
            if param_names and param_names[0] == "self":
                param_names = param_names[1:]

            # map positional args to param names
            bound_kwargs = dict(kwargs)
            for i, arg_val in enumerate(func_args):
                if i < len(param_names):
                    bound_kwargs[param_names[i]] = arg_val

            target_id = str(bound_kwargs.get(target_id_param, "unknown"))
            target_host = str(bound_kwargs.get(target_host_param, "unknown")) if target_host_param in bound_kwargs else None
            target_project = str(bound_kwargs.get(target_project_param, "default")) if target_project_param in bound_kwargs else None

            start_time = time.perf_counter()

            try:
                result = await func(*args, **kwargs)
                duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

                # extract additional details if provided
                details = {}
                if extract_details:
                    try:
                        details = extract_details(func_args, bound_kwargs, result)
                    except Exception:
                        pass

                activity_logger.log_activity(
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    target_host=target_host,
                    target_project=target_project,
                    result="success",
                    details=details if details else None,
                    duration_ms=duration_ms,
                )

                return result

            except Exception as exc:
                duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

                activity_logger.log_activity(
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    target_host=target_host,
                    target_project=target_project,
                    result="failure",
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                    duration_ms=duration_ms,
                )

                raise

        return wrapper
    return decorator


def audit_operation_sync(
    action: str,
    target_type: str,
    *,
    severity: Optional[str] = None,
    target_id_param: str = "name",
    target_host_param: str = "host",
    target_project_param: str = "project",
    extract_details: Optional[Callable] = None,
):
    """
    Decorator for synchronous LXD service methods that adds audit logging.
    Same as audit_operation but for sync functions.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            activity_logger = get_activity_logger()

            # extract target info from function arguments
            func_args = args[1:] if args else args

            import inspect
            sig = inspect.signature(func)
            param_names = list(sig.parameters.keys())
            if param_names and param_names[0] == "self":
                param_names = param_names[1:]

            bound_kwargs = dict(kwargs)
            for i, arg_val in enumerate(func_args):
                if i < len(param_names):
                    bound_kwargs[param_names[i]] = arg_val

            target_id = str(bound_kwargs.get(target_id_param, "unknown"))
            target_host = str(bound_kwargs.get(target_host_param, "unknown")) if target_host_param in bound_kwargs else None
            target_project = str(bound_kwargs.get(target_project_param, "default")) if target_project_param in bound_kwargs else None

            start_time = time.perf_counter()

            try:
                result = func(*args, **kwargs)
                duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

                details = {}
                if extract_details:
                    try:
                        details = extract_details(func_args, bound_kwargs, result)
                    except Exception:
                        pass

                activity_logger.log_activity(
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    target_host=target_host,
                    target_project=target_project,
                    result="success",
                    details=details if details else None,
                    duration_ms=duration_ms,
                )

                return result

            except Exception as exc:
                duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

                activity_logger.log_activity(
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    target_host=target_host,
                    target_project=target_project,
                    result="failure",
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                    duration_ms=duration_ms,
                )

                raise

        return wrapper
    return decorator
