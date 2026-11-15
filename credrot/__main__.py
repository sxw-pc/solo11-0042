"""Command-line entry point for credrot."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .archive import archive_batch
from .clock import Clock, ClockError, FixedClock, SystemClock, format_time
from .config import (
    ConfigError,
    Issue,
    Policy,
    default_db_path,
    default_fixtures_dir,
    load_policy,
)
from .db import Database
from .gating import load_gates
from .planner import build_plan
from .plan_export import plan_to_markdown
from .registry import load_demo_registry
from .report import ReportError, build_report, render_json
from .report_markdown import render_markdown
from .resolver import resolve
from .rotator import UnknownBatch, rotate_batch
from .store import load_credentials, load_tasks, save_plan, seed_registry
from .stubs.verify_stub import run_stub
from .verifier import verify_all


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (ConfigError, ClockError, ReportError, UnknownBatch, ValueError) as exc:
        print(f"credrot: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db",
        default=os.environ.get("CREDROT_DB", str(default_db_path())),
        help="SQLite ledger path (default: .credrot/credrot.db)",
    )
    common.add_argument(
        "--fixtures",
        default=os.environ.get("CREDROT_FIXTURES", str(default_fixtures_dir())),
        help="offline fixture directory",
    )
    now = argparse.ArgumentParser(add_help=False)
    now.add_argument("--now", help="inject a fixed ISO-8601 UTC timestamp")

    parser = argparse.ArgumentParser(prog="credrot")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-demo", parents=[common, now])
    init.set_defaults(handler=_init_demo)

    plan = sub.add_parser("plan", parents=[common, now])
    plan.add_argument("--format", choices=("json", "markdown"), default="json")
    plan.set_defaults(handler=_plan)

    rotate = sub.add_parser("rotate", parents=[common, now])
    rotate.add_argument("--batch", required=True)
    rotate.set_defaults(handler=_rotate)

    verify = sub.add_parser("verify", parents=[common, now])
    verify.add_argument("--batch")
    verify.add_argument("--stub-url")
    verify.set_defaults(handler=_verify)

    report = sub.add_parser("report", parents=[common, now])
    report.add_argument("--batch")
    report.add_argument("--format", choices=("json", "markdown", "both"), default="both")
    report.add_argument("--out", default="reports")
    report.set_defaults(handler=_report)

    archive = sub.add_parser("archive", parents=[common, now])
    archive.add_argument("--batch", required=True)
    archive.add_argument("--out")
    archive.set_defaults(handler=_archive)

    resolve_cmd = sub.add_parser("resolve", parents=[common, now])
    resolve_cmd.add_argument("--task", required=True)
    resolve_cmd.set_defaults(handler=_resolve)

    serve = sub.add_parser("serve", parents=[common])
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(handler=_serve)

    stub = sub.add_parser("verify-stub", parents=[common])
    stub.add_argument("--port", type=int, default=8777)
    stub.set_defaults(handler=_stub)
    return parser


def _clock(args: argparse.Namespace) -> Clock:
    return FixedClock(args.now) if getattr(args, "now", None) else SystemClock()


def _db(args: argparse.Namespace) -> Database:
    db = Database(args.db)
    db.initialize()
    return db


def _policy(args: argparse.Namespace) -> Policy:
    policy, issues = load_policy(Path(args.fixtures) / "policy.yaml")
    _raise_issues(issues)
    return policy


def _raise_issues(issues: list[Issue]) -> None:
    if issues:
        raise ValueError("; ".join(issue.render() for issue in issues))


def _init_demo(args: argparse.Namespace) -> int:
    db = _db(args)
    clock = _clock(args)
    registry = load_demo_registry(Path(args.fixtures))
    _raise_issues(registry.issues)
    seed_registry(db, registry.credentials, registry.tasks, clock.now())
    print(
        json.dumps(
            {
                "loaded": {
                    "credentials": len(registry.credentials),
                    "tasks": len(registry.tasks),
                },
                "db": db.path,
                "seeded_at": format_time(clock.now()),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _plan(args: argparse.Namespace) -> int:
    db = _db(args)
    clock = _clock(args)
    policy = _policy(args)
    credentials = load_credentials(db)
    tasks = load_tasks(db)
    if not credentials:
        raise ValueError("ledger is empty; run init-demo first")
    plan = build_plan(
        credentials, tasks, policy, clock.now(),
        gates=load_gates(db, credentials, clock.now()),
    )
    save_plan(db, plan)
    if args.format == "markdown":
        print(plan_to_markdown(plan), end="")
    else:
        _print_json(plan.to_dict())
    return 0


def _rotate(args: argparse.Namespace) -> int:
    db = _db(args)
    result = rotate_batch(db, args.batch, _clock(args), _policy(args))
    _print_json(result.to_dict())
    return 0 if result.status == "completed" else 1


def _verify(args: argparse.Namespace) -> int:
    db = _db(args)
    result = verify_all(
        db, _clock(args), _policy(args), batch_id=args.batch,
        base_url=args.stub_url,
    )
    _print_json(result)
    return 0


def _report(args: argparse.Namespace) -> int:
    db = _db(args)
    clock = _clock(args)
    payload = build_report(db, clock, _policy(args), args.batch)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    if args.format in {"json", "both"}:
        target = out_dir / "report.json"
        target.write_text(render_json(payload), encoding="utf-8")
        written["json"] = str(target)
    if args.format in {"markdown", "both"}:
        target = out_dir / "report.md"
        target.write_text(render_markdown(payload), encoding="utf-8")
        written["markdown"] = str(target)
    _print_json({"written": written, "batch_id":
                 payload["batch"]["batch_id"] if payload["batch"] else None})
    return 0


def _archive(args: argparse.Namespace) -> int:
    db = _db(args)
    clock = _clock(args)
    payload = archive_batch(db, clock, args.batch)
    default = Path(".credrot") / "archives" / f"{args.batch}.json"
    target = Path(args.out) if args.out else default
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_json(payload), encoding="utf-8")
    _print_json({"archive": str(target), "batch_id": args.batch})
    return 0


def _resolve(args: argparse.Namespace) -> int:
    db = _db(args)
    result = resolve(db, args.task, _clock(args), _policy(args))
    _print_json(result)
    return 0 if result["ok"] else 1


def _serve(args: argparse.Namespace) -> int:
    if args.host != "127.0.0.1":
        raise ValueError("serve is loopback-only; --host must be 127.0.0.1")
    if not 1 <= args.port <= 65535:
        raise ValueError("--port must be in 1..65535")
    from .app import create_app

    app = create_app(
        db_path=args.db,
        clock=SystemClock(),
        policy=_policy(args),
        fixtures_dir=args.fixtures,
    )
    print(f"credrot serving on http://127.0.0.1:{args.port}")
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
    return 0


def _stub(args: argparse.Namespace) -> int:
    run_stub(args.port, Path(args.fixtures) / "verify_stub_rules.yaml")
    return 0


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
