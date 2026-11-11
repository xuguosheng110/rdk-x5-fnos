"""Command line entry points for ``python3 -m fnos.lab``."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from . import __version__
from .backup import build_backup_plan, write_backup_plan
from .candidate import load_candidate
from .authorization import (
    issue_authorization,
    load_authorization,
    require_real_gate,
    save_authorization,
    token_fingerprint,
)
from .errors import AuthorizationError, LabError, Refused
from .fakes import build_fake_backends
from .flash import MacDiskutilBackend, parse_identity
from .live import LiveProviderContext
from .target_mapping import load_mapping_record
from .inspect import host_text_summary, load_inventory
from .plan import build_plan, load_plan, plan_summary
from .power import Cp5CliClient, Cp5PowerBackend
from .runner import BackendSet, execute, request_cancel
from .serial import PosixSerialCapture, SessionSerialCapture
from .simulate import FAULTS, Simulation
from .status import inspect_run

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CP5_SCRIPT = ROOT / "scripts" / "mijia-cp5" / "cp5.py"


def default_state_dir() -> Path:
    override = os.environ.get("FNOS_LAB_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "state" / "fnos-lab"


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m fnos.lab",
        description=(
            "Offline-safe RDK X5 laboratory tooling. inspect/plan/status are "
            "read-only or file-only; simulate never touches hardware. The real "
            "backend is disabled by default; no real flashing, power or serial "
            "action is taken without the explicit gate."
        ),
        epilog=(
            "Exit codes: 0 success/passed, 1 failed, 2 refused, 3 cancelled, "
            "70 simulated crash."
        ),
    )
    parser.add_argument("--version", action="version", version=f"fnos-lab {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser(
        "inspect",
        help="read-only host tools, serial candidates and disk identity",
        description=(
            "Enumerate tools, serial device names (never opened) and disk "
            "identity. inspect never grants write eligibility."
        ),
    )
    inspect.add_argument("--fixture", type=Path, help="explicit offline fixture")
    inspect.add_argument(
        "--bindings",
        type=Path,
        help="reviewed board bindings (real mode only; default repo binding file)",
    )
    inspect.add_argument(
        "--format", choices=("json", "text"), default="json", help="output format"
    )

    plan = sub.add_parser(
        "plan",
        help="create a versioned, atomic plan for one explicit target",
        description=(
            "Bind board_id, absolute image digest/size, one identity-verified "
            "target and one wired power channel. The plan is not a write "
            "authorization and is marked write_eligible=false."
        ),
    )
    plan.add_argument("--board", required=True, help="board_id from the reviewed inventory")
    plan.add_argument("--image", required=True, type=Path, help="absolute image path")
    plan.add_argument(
        "--target",
        required=True,
        help="whole-disk device path (e.g. /dev/disk4) or diskN; identity must also match",
    )
    plan.add_argument(
        "--expect-serial",
        required=True,
        help="expected disk serial; diskN alone is never accepted as identity",
    )
    plan.add_argument("--fixture", type=Path, help="explicit offline fixture")
    plan.add_argument(
        "--bindings",
        type=Path,
        help="reviewed board bindings (real mode only; default repo binding file)",
    )
    plan.add_argument("--out", required=True, type=Path, help="absolute plan output path")
    plan.add_argument(
        "--reserve-bytes",
        type=int,
        default=64 * 1024 * 1024,
        help="free space kept beyond the image (default 64 MiB)",
    )

    simulate = sub.add_parser(
        "simulate",
        help="run the persistent hardware-free state machine",
        description=(
            "Simulate preflight, power cycle, write, full read-back, cancel, "
            "hash mismatch, cloud-unknown recovery and crash detection. No disk, "
            "serial or power backend is used."
        ),
    )
    simulate.add_argument("--plan", required=True, type=Path, help="plan JSON")
    simulate.add_argument(
        "--state-dir",
        type=Path,
        default=default_state_dir(),
        help="private run/state directory (default: FNOS_LAB_STATE_DIR or ~/.local/state/fnos-lab)",
    )
    simulate.add_argument("--run-id", help="explicit run id (default: generated)")
    simulate.add_argument(
        "--cancel-at",
        choices=("preflight", "writing", "reading"),
        help="model a cancel request at a safe boundary",
    )
    simulate.add_argument(
        "--crash-at",
        choices=("writing", "reading"),
        help="model abrupt process death after entering the phase",
    )
    simulate.add_argument("--fault", choices=FAULTS, help="inject one modelled fault")

    run = sub.add_parser(
        "run",
        help="execute a reviewed plan with fake or real backends",
        description=(
            "Execute the shared state machine. --backends fake uses file "
            "substitutes and never touches /dev or the cloud. --backends real "
            "requires FNOS_LAB_ENABLE_REAL_BACKENDS=1, root, a plan-bound "
            "authorization token and --confirm-board."
        ),
    )
    run.add_argument("--plan", required=True, type=Path)
    run.add_argument("--state-dir", type=Path, default=default_state_dir())
    run.add_argument("--run-id", help="explicit run id (default: generated)")
    run.add_argument(
        "--backends", required=True, choices=("fake", "real"), help="backend set"
    )
    run.add_argument("--auth", type=Path, help="plan-bound authorization token (real only)")
    run.add_argument("--manifest", type=Path, help="candidate manifest for real runtime identity")
    run.add_argument(
        "--product-version",
        help="explicit fnOS trim version adapter for fnos-x5-assembly/v1 manifests",
    )
    run.add_argument("--mmc-index", type=int, help="exact U-Boot mmc index for target mapping")
    run.add_argument(
        "--mapping-record",
        type=Path,
        help="reviewed multi-evidence mapping record when the bridge exposes no CID/serial",
    )
    run.add_argument("--confirm-board", help="must exactly equal the plan board_id")
    run.add_argument(
        "--authorize-write",
        action="store_true",
        help="explicitly acknowledge this reviewed real write (still requires the token and gate)",
    )
    run.add_argument("--allow-planned-fault", action="store_true")
    run.add_argument(
        "--planned-fault-at", choices=("writing", "reading"), help="fake-only test fault"
    )
    run.add_argument(
        "--crash-at", choices=("writing", "reading"), help="fake-only process crash"
    )
    run.add_argument("--boot-timeout", type=float, default=30.0)
    run.add_argument(
        "--cut-hold-seconds",
        type=int,
        default=5,
        help="minimum confirmed OFF hold before ON (5..60 seconds)",
    )

    authorize = sub.add_parser(
        "authorize",
        help="issue one plan-bound authorization token (real gate required)",
        description=(
            "Issue a device-key-signed capability bound to the plan digest, "
            "image, target and board. This does not write a disk or switch power."
        ),
    )
    authorize.add_argument("--plan", required=True, type=Path)
    authorize.add_argument("--out", required=True, type=Path)
    authorize.add_argument("--state-dir", type=Path, default=default_state_dir())
    authorize.add_argument("--confirm-board", required=True)
    authorize.add_argument("--ttl-seconds", type=int, default=1800)
    authorize.add_argument(
        "--authorize-real-hardware",
        action="store_true",
        help="explicit acknowledgement; the environment gate is still required",
    )

    backup = sub.add_parser(
        "backup-plan",
        help="create a non-executed backup plan for the unique X5 test medium",
        description=(
            "Read-only disk identity and free-space checks; never opens the "
            "source or writes the destination. Requires an expected serial and "
            "a new destination filename."
        ),
    )
    backup.add_argument("--board", required=True)
    backup.add_argument("--source", required=True, help="external whole /dev/diskN")
    backup.add_argument("--expect-serial", required=True)
    backup.add_argument("--destination-dir", required=True, type=Path)
    backup.add_argument("--destination-name", required=True)
    backup.add_argument("--out", required=True, type=Path)
    backup.add_argument("--bindings", type=Path)

    cancel = sub.add_parser(
        "cancel",
        help="persist a cancel request without cutting power",
        description=(
            "Write a cancel.request file. The executor observes it at a safe "
            "boundary; cancel never issues a power-off and never resumes a run."
        ),
    )
    cancel.add_argument("--state-dir", type=Path, default=default_state_dir())
    cancel.add_argument("--run", required=True, help="run id")

    status = sub.add_parser(
        "status",
        help="inspect persisted run state and detect a dead process",
        description=(
            "Report state, plan/image integrity, power/media flags, residual "
            "writers and whether an implicit resume would be allowed (always false)."
        ),
    )
    status.add_argument("--state-dir", type=Path, default=default_state_dir())
    status.add_argument("--run", required=True, help="run id")
    status.add_argument(
        "--verify-image",
        action="store_true",
        help="hash the image when its mtime differs from the plan",
    )

    return parser


def _cmd_inspect(args: argparse.Namespace) -> int:
    inventory = load_inventory(args.fixture, bindings=args.bindings)
    if args.format == "text":
        print(host_text_summary(inventory))
    else:
        _print_json(inventory.summary())
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    inventory = load_inventory(args.fixture, bindings=args.bindings)
    plan = build_plan(
        inventory,
        board_id=args.board,
        image_path=args.image,
        target_selector=args.target,
        expect_serial=args.expect_serial,
        out_path=args.out,
        reserve_bytes=args.reserve_bytes,
    )
    _print_json(plan_summary(plan, args.out))
    return 0


def _cmd_simulate(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    simulation = Simulation(
        plan,
        plan_path=args.plan,
        state_root=args.state_dir,
        run_id=args.run_id,
        cancel_at=args.cancel_at,
        crash_at=args.crash_at,
        fault=args.fault,
    )
    state = simulation.run()
    _print_json(simulation.summary())
    if state.get("status") == "passed":
        return 0
    if state.get("status") == "cancelled":
        return 3
    return 1


def _real_backend_factory(plan: dict[str, Any], context: LiveProviderContext):
    def factory(held) -> BackendSet:
        board_id = plan["board"]["board_id"]
        script = Path(os.environ.get("FNOS_LAB_CP5_SCRIPT", str(DEFAULT_CP5_SCRIPT)))
        client = Cp5CliClient(script, board_id=board_id, lock=held)
        power = Cp5PowerBackend(
            client, board_id=board_id, sleep=lambda _: None, wait_seconds=20.0
        )
        from .uart_session import SerialSession
        from .locks import lock_root
        session = SerialSession(
            board_id=board_id,
            held=held,
            port=(plan.get("serial") or {}).get("port", ""),
            state_root=Path(lock_root()),
            baud=int((plan.get("serial") or {}).get("baud") or 115200),
        )
        context.bind_session(session)
        serial = SessionSerialCapture(session)
        flash = MacDiskutilBackend((plan.get("target") or {}).get("device", ""))
        return BackendSet(power=power, serial=serial, flash=flash)

    return factory


def _cmd_run(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    manifest = (
        load_candidate(args.manifest, product_version=args.product_version)
        if args.manifest
        else None
    )
    authorization = None
    context = None
    if args.backends == "real":
        if not args.authorize_write:
            raise AuthorizationError(
                "real execution requires --authorize-write in addition to the gate and token"
            )
        if not args.auth:
            raise AuthorizationError("real execution requires --auth <token>")
        require_real_gate()
        authorization = load_authorization(args.auth)
        from .inspect import load_inventory
        context = LiveProviderContext(
            plan,
            disk_provider=lambda: [
                parse_identity(d) for d in load_inventory(None).disks
            ],
            authorize_ums=args.authorize_write,
            mmc_index=args.mmc_index,
            reviewed_record=(
                load_mapping_record(args.mapping_record)
                if getattr(args, "mapping_record", None)
                else None
            ),
        )
        factory = _real_backend_factory(plan, context)
    else:
        if args.auth or args.confirm_board or args.authorize_write:
            raise Refused(
                "fake backends do not accept real authorization flags; they are offline substitutes"
            )

        def factory(_held):
            return build_fake_backends(plan, state_root=args.state_dir)

    engine, state = execute(
        plan,
        plan_path=args.plan,
        state_root=args.state_dir,
        backend_factory=factory,
        execution_mode=args.backends,
        run_id=args.run_id,
        authorization=authorization,
        confirm_board=args.confirm_board,
        allow_planned_fault=args.allow_planned_fault,
        planned_fault_at=args.planned_fault_at,
        crash_at=args.crash_at,
        boot_timeout=args.boot_timeout,
        cut_hold_seconds=args.cut_hold_seconds,
        candidate_manifest=manifest,
        cut_evidence_provider=(context.cut_provider if context else None),
        cut_evidence_validator=(context.cut_validator if context else None),
        runtime_identity_provider=(context.runtime_identity if context else None),
        target_map_provider=(context.target_map if context else None),
        ums_prepare_provider=(context.prepare_ums if context else None),
        normal_shutdown_provider=(context.normal_shutdown if context else None),
        cold_boot_prepare_provider=(context.prepare_cold_boot if context else None),
    )
    _print_json(engine.summary())
    if state.get("status") == "passed":
        return 0
    if state.get("status") == "cancelled":
        return 3
    return 1


def _cmd_authorize(args: argparse.Namespace) -> int:
    require_real_gate()
    if not args.authorize_real_hardware:
        raise AuthorizationError(
            "authorization requires --authorize-real-hardware acknowledgement"
        )
    plan = load_plan(args.plan)
    if plan.get("simulated") is not False:
        raise Refused("only a real (simulated=false) plan can be authorized")
    board_id = (plan.get("board") or {}).get("board_id")
    if args.confirm_board != board_id:
        raise AuthorizationError("--confirm-board must exactly match the plan board_id")
    token = issue_authorization(
        plan, state_root=args.state_dir, ttl_seconds=args.ttl_seconds
    )
    save_authorization(args.out, token)
    payload = token["payload"]
    _print_json(
        {
            "authorization": str(args.out),
            "schema": token["schema"],
            "scope": payload["scope"],
            "board_id": payload["board_id"],
            "target": payload["target"],
            "image_sha256": payload["image_sha256"],
            "expires_at": payload["expires_at"],
            "token_sha256": token_fingerprint(token),
            "power_changed": False,
            "disk_written": False,
        }
    )
    return 0


def _cmd_backup_plan(args: argparse.Namespace) -> int:
    inventory = load_inventory(None, bindings=args.bindings)
    disk = next((d for d in inventory.disks if d["device"] == args.source), None)
    if disk is None:
        raise Refused(f"source {args.source} is not in the current external inventory")
    source = parse_identity(disk)
    plan = build_backup_plan(
        board_id=args.board,
        source=source,
        destination_dir=args.destination_dir,
        destination_name=args.destination_name,
        expect_serial=args.expect_serial,
    )
    write_backup_plan(plan, args.out)
    _print_json(plan.as_dict())
    return 0


def _cmd_cancel(args: argparse.Namespace) -> int:
    _print_json(request_cancel(args.state_dir, args.run))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    _print_json(inspect_run(args.state_dir, args.run, verify_hash=args.verify_image))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            return _cmd_inspect(args)
        if args.command == "plan":
            return _cmd_plan(args)
        if args.command == "simulate":
            return _cmd_simulate(args)
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "authorize":
            return _cmd_authorize(args)
        if args.command == "backup-plan":
            return _cmd_backup_plan(args)
        if args.command == "cancel":
            return _cmd_cancel(args)
        if args.command == "status":
            return _cmd_status(args)
        parser.error("unknown command")
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except LabError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Interrupted; inspect persisted state before any power action",
            file=sys.stderr,
        )
        return 130
    except BrokenPipeError:
        return 1
    except Exception as exc:
        # Never print a traceback or environment/credential detail.
        print(
            f"FAILED: {type(exc).__name__} (details withheld; no hardware action taken)",
            file=sys.stderr,
        )
        return 1
    return 0
