"""A create that LXD accepts and then fails must not stay recorded as a success.

The POST returns as soon as LXD queues the operation, so only synchronous validation reaches
the caller. Anything failing while the operation runs was silent: the API answered 200 with an
operation id, the audit trail recorded result=success, and no instance existed. Reproduced live
by creating from an image fingerprint that had been pruned from the host: the create reported
success and nothing ever appeared.
"""

import ast
import inspect

from app.services.lxd import instance as instance_module


def _create_source() -> str:
    return inspect.getsource(instance_module)


def test_a_create_without_autostart_still_watches_its_operation() -> None:
    src = _create_source()
    assert "_watch_create_operation" in src, "every create must have its operation watched"
    tree = ast.parse(src)
    watcher = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
         and n.name == "_watch_create_operation"),
        None,
    )
    assert watcher is not None, "_watch_create_operation must exist"
    body = ast.unparse(watcher)
    assert "wait_for_operation" in body, "the watcher must actually await the operation"
    # ast.unparse normalises quoting, so compare without depending on it.
    assert "result='failure'" in body or 'result="failure"' in body, (
        "a failed create must be recorded as a failure, not left as the success the POST logged"
    )


def test_the_watcher_task_is_strongly_referenced() -> None:
    """A bare create_task can be collected mid-run, which would make the watch unreliable."""
    src = _create_source()
    watch_block = src.split("_watch_create_operation(create_client")[1].split("return operation_id")[0]
    assert "_bg_tasks.add" in watch_block, (
        "hold a strong reference like the auto-start branch does, or the loop may collect the "
        "task before the operation finishes"
    )
