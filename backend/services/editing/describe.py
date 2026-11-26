"""Say in words what a timeline does and why, for logs, the dashboard and debugging."""

from typing import List

from services.editing.models import Timeline


def explain(timeline: Timeline) -> List[str]:
    """One line per event: what it is, what was applied, and what was refused and why."""
    refused = {}
    for item in timeline.suppressed:
        refused.setdefault(item.event_id, []).append(f"{item.action} refused ({item.reason})")
    lines = [
        f"profile '{timeline.profile}' for tone '{timeline.tone or 'unknown'}': "
        f"{len(timeline.visual_tracks)} visual effect(s), {len(timeline.audio_tracks)} sound(s) "
        f"over {timeline.clip_duration:g}s"
    ]
    for event in timeline.events:
        applied = event.visual_actions + event.audio_actions
        head = f"{event.start:6.2f}s {event.kind:<14} intensity {event.intensity:.2f}"
        if applied:
            head += " -> " + ", ".join(applied)
        notes = refused.get(event.id, [])
        if notes:
            head += " | " + "; ".join(notes)
        if not applied and not notes:
            head += " -> nothing (no action wanted)"
        lines.append(head)
    return lines
