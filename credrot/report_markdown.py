"""Markdown rendering kept separate to honor the file-size limit."""

from __future__ import annotations

from typing import Any


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# credrot report",
        "",
        f"Generated at: `{report['generated_at']}`",
        "",
        "All data is synthetic metadata. No secret value is stored.",
        "",
        "## Credential overview",
        "",
        "| credential | source | role | ver | state | expires | days |",
        "| --- | --- | --- | ---: | --- | --- | ---: |",
    ]
    for row in report["credentials"]:
        lines.append(
            f"| {row['id']} | {row['source']} | {row['role']} | "
            f"{row['version']} | {row['effective_state']} | {row['expires_at']} | "
            f"{row['days_to_expiry']} |"
        )

    batch = report["batch"]
    lines.extend(["", "## Rotation batch", ""])
    if batch is None:
        lines.append("No rotation batch has been generated.")
    else:
        lines.append(
            f"- Batch: `{batch['batch_id']}`\n"
            f"- Status: **{batch['status']}**\n"
            f"- Generated: `{batch['generated_at']}`\n"
            f"- Executed: `{batch['executed_at'] or 'not executed'}`"
        )
        if batch.get("failure_reason"):
            lines.append(f"- Failure: {batch['failure_reason']}")
        lines.extend(
            [
                "",
                "| credential | action | from | to | reason | result |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for item in batch["items"]:
            lines.append(
                f"| {item['credential_id']} | {item['action']} | "
                f"{item['from_state']} | {item['to_state']} | "
                f"{item['reason_code']} | {item['result']} |"
            )

    lines.extend(["", "## Health matrix", "",
                  f"Conclusion: **{report['health']['conclusion']}**", "",
                  "| credential | state | outcome | code | checked_at |",
                  "| --- | --- | --- | --- | --- |"])
    for row in report["health"]["matrix"]:
        lines.append(
            f"| {row['credential_id']} | {row['state']} | {row['outcome']} | "
            f"{row['code']} | {row['checked_at'] or '-'} |"
        )

    lines.extend(["", "## Task resolvability", "",
                  "| task | ok | code | degraded | roles |",
                  "| --- | --- | --- | --- | --- |"])
    for task in report["tasks"]:
        roles = ", ".join(
            f"{role}={_render_resolved(value)}"
            for role, value in task["resolved"].items()
        )
        lines.append(
            f"| {task['task_id']} | {str(task['ok']).lower()} | {task['code']} | "
            f"{str(task['degraded']).lower()} | {roles or '-'} |"
        )

    lines.extend(["", "## Failures", ""])
    if not report["failures"]:
        lines.append("No failures recorded.")
    else:
        for failure in report["failures"]:
            lines.append(
                f"- `{failure['kind']}` {failure['subject']}: "
                f"{failure['message']}"
            )
    return "\n".join(lines) + "\n"


def _render_resolved(value: Any) -> str:
    if not value:
        return "-"
    label = value["credential_id"]
    if value.get("softened"):
        label += " (softened)"
    return label


