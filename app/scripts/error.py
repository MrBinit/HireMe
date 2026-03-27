"""Standardized script error output and exit-code handling."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
import sys
import traceback


def _timestamp() -> str:
    """Return local ISO timestamp for script logs."""

    return datetime.now().astimezone().isoformat(timespec="seconds")


def print_script_error(
    *,
    script_name: str,
    message: str,
    exc: BaseException | None = None,
) -> None:
    """Print a standardized script error message to stderr."""

    print(f"[{_timestamp()}] [ERROR] [{script_name}] {message}", file=sys.stderr)
    if exc is not None:
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)


def run_script_entrypoint(
    main: Callable[[], None],
    *,
    script_name: str | None = None,
) -> int:
    """Execute a script entrypoint with standardized error handling."""

    resolved_script_name = script_name
    if not resolved_script_name:
        module_name = getattr(main, "__module__", "")
        if module_name and module_name != "__main__":
            resolved_script_name = module_name
        else:
            main_module = sys.modules.get("__main__")
            main_file = getattr(main_module, "__file__", None) if main_module else None
            if isinstance(main_file, str) and main_file:
                resolved_script_name = Path(main_file).name
            else:
                resolved_script_name = module_name or "script"
    try:
        main()
        return 0
    except KeyboardInterrupt:
        print_script_error(
            script_name=resolved_script_name,
            message="Interrupted by user (Ctrl+C).",
        )
        return 130
    except SystemExit as exc:
        code = exc.code
        if code in (None, 0):
            return 0
        if isinstance(code, int):
            print_script_error(
                script_name=resolved_script_name,
                message=f"Exited with status code {code}.",
            )
            return code
        print_script_error(
            script_name=resolved_script_name,
            message=f"Exited: {code}",
        )
        return 1
    except Exception as exc:
        print_script_error(
            script_name=resolved_script_name,
            message=f"{type(exc).__name__}: {exc}",
            exc=exc,
        )
        return 1
