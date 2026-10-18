"""``notify / model`` commands: test the channels and the model provider.

notify test | confirm-email; model test.

E-mail approval gate: ``notify test --channel email`` sends a test mail, the
user says it arrived, the agent runs ``notify confirm-email`` which writes
``notify.email.confirmed_at``; until then ``schedule install`` exits 5
(``notify.email_smtp.ensure_email_confirmed``).
"""

from __future__ import annotations

import argparse
import datetime as _dt

from kallimachos.cli_run import emit, flag, load_context
from kallimachos.cli_support import add_command, add_group
from kallimachos.config import load as config_load
from kallimachos.interfaces import ExitCode, UsageError, dump
from kallimachos.model import provider as model_provider
from kallimachos.notify import base as notify_base
from kallimachos.state import store


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    g = add_group(sub, "notify", help="test notification channels", path="notify")
    p = add_command(g, "test", cmd_notify_test, help="send a test message", path="notify test")
    p.add_argument(
        "--channel",
        choices=["markdown", "telegram", "email"],
        help="one channel (default: every configured channel)",
    )
    add_command(
        g,
        "confirm-email",
        cmd_confirm_email,
        help="record that the test e-mail arrived",
        path="notify confirm-email",
    )
    m = add_group(sub, "model", help="test the model provider", path="model")
    add_command(m, "test", cmd_model_test, help="one small probe request", path="model test")


def cmd_notify_test(args: argparse.Namespace) -> int:
    """Notifier.test() per channel; exit 3 when one fails."""
    config, layout = load_context(args)
    channels = [args.channel] if getattr(args, "channel", None) else list(config.notify.channels)
    results = {}
    for name in channels:
        notifier = notify_base.get_notifier(name, config, layout, approved=flag(args, "yes"))
        results[name] = notifier.test()
        if name == "email" and results[name].ok and not flag(args, "dry_run"):
            # `notify confirm-email` accepts the confirmation only after a test mail went out
            details = results[name].details or {}
            store.write_json_atomic(
                layout.email_test_file,
                {
                    "sent_at": _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat(),
                    "message_id": str(details.get("message_id", "")),
                    "to": len(config.notify.email.to),
                },
            )
    lines = [f"{name}: {'ok' if r.ok else 'FAILED'} - {r.message}" for name, r in results.items()]
    emit(args, {name: dump(r) for name, r in results.items()}, "\n".join(lines))
    return int(ExitCode.PARTIAL if any(not r.ok for r in results.values()) else ExitCode.OK)


def cmd_confirm_email(args: argparse.Namespace) -> int:
    """Set ``notify.email.confirmed_at`` in config.yaml in place (comments kept, backup written).
    Refused (exit 2) until ``notify test --channel email`` has sent a test mail."""
    config, layout = load_context(args)
    cfg = config.notify.email
    if not (cfg.smtp_host and cfg.from_ and cfg.to):
        raise UsageError("notify.email is not configured; nothing to confirm")
    record = store.read_json(layout.email_test_file)
    if not isinstance(record, dict) or not record.get("sent_at"):
        raise UsageError(
            "no test e-mail has been sent yet; run `kallimachos notify test --channel email`, "
            "ask the user whether it arrived, then confirm"
        )
    cfg.confirmed_at = _dt.datetime.now(_dt.UTC).replace(microsecond=0)
    stamp = cfg.confirmed_at.isoformat()
    backup = None
    if not flag(args, "dry_run"):
        backup = config_load.update_config_values(
            layout.config_file, config, {"notify.email.confirmed_at": stamp}
        )
    emit(
        args,
        {"confirmed_at": stamp, "test_sent_at": record["sent_at"], "backup": str(backup or "")},
        f"notify.email.confirmed_at = {stamp}" + (f" (backup {backup})" if backup else ""),
    )
    return int(ExitCode.OK)


def cmd_model_test(args: argparse.Namespace) -> int:
    """Provider.test(); ~20-token probe. Exit 3 when the provider does not answer."""
    config, _layout = load_context(args)
    result = model_provider.get_provider(config.model).test()
    emit(
        args,
        dump(result),
        f"{config.model.provider}: {'ok' if result.ok else 'FAILED'} - {result.message}",
    )
    return int(ExitCode.OK if result.ok else ExitCode.PARTIAL)
