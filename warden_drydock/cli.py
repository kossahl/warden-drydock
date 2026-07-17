from __future__ import annotations
import argparse
from pathlib import Path
from .core.generator import init_campaign
from .core.validation import validate_campaign
from .core.context import build_context


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="drydock", description="Warden Drydock campaign tooling")
    p.add_argument("--version", action="version", version="Warden Drydock 0.1.0")
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create a standalone campaign repository")
    init.add_argument("path", type=Path)
    init.add_argument("--adapter", default="mothership", choices=["mothership"])
    init.add_argument("--name")
    init.add_argument("--interactive", action="store_true")
    init.add_argument("--force", action="store_true")

    val = sub.add_parser("validate", help="Validate a campaign repository")
    val.add_argument("path", type=Path, nargs="?", default=Path.cwd())

    ctx = sub.add_parser("context", help="Build generated AI context")
    ctx.add_argument("path", type=Path, nargs="?", default=Path.cwd())

    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.command == "init":
        name = args.name
        if args.interactive and not name:
            default = args.path.resolve().name.replace("-", " ").title()
            entered = input(f"Campaign name [{default}]: ").strip()
            name = entered or default
        name = name or args.path.resolve().name.replace("-", " ").title()
        init_campaign(args.path, name=name, adapter=args.adapter, force=args.force)
        print(f"Created Warden Drydock campaign at {args.path.resolve()}")
        return 0
    if args.command == "validate":
        return validate_campaign(args.path)
    if args.command == "context":
        build_context(args.path)
        return 0
    return 2
