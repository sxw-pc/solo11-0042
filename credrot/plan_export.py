"""Deterministic Markdown export for rotation plans."""

from __future__ import annotations

from .model import RotationPlan


def plan_to_markdown(plan: RotationPlan) -> str:
    data = plan.to_dict()
    lines = [
        "# credrot rotation plan",
        "",
        f"Batch: `{data['batch_id']}`",
        f"Generated at: `{data['generated_at']}`",
        "",
        "| credential | action | from | to | reason | tasks |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in data["items"]:
        lines.append(
            f"| {item['credential_id']} | {item['action']} | {item['from_state']} | "
            f"{item['target_state']} | {item['reason_code']} | "
            f"{', '.join(item['affected_tasks']) or '-'} |"
        )
    lines.extend(["", f"Items: {len(data['items'])}", ""])
    return "\n".join(lines)


