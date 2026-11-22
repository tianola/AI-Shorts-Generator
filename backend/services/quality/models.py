"""The quality report: findings with evidence, and a status from hard rules.

There is no quality score. A report is a list of findings, each tied to a time and to the
measurement or frame that shows it, plus a status:
  failed  - at least one critical finding (the file is unusable or breaks a hard rule)
  review  - warnings a person should look at before the clip is published
  passed  - nothing found, or only information

A finding with no evidence is dropped before it is stored, so a report never says something the
file does not show.
"""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

REPORT_VERSION = 1
CATEGORIES = ("audio", "captions", "visual", "sync", "editing", "hook", "title", "file")
SEVERITIES = ("critical", "warning", "info")
# A warning counts towards "review" only when the check is at least this sure of it.
REVIEW_CONFIDENCE = 0.6


class Evidence(BaseModel):
    kind: str                       # measurement | frame | audio_excerpt | timeline_event | filter_output
    ref: str = ""                   # file, frame path or filter name the value comes from
    value: object = None            # the measured number or the quoted observation


class Finding(BaseModel):
    id: str
    category: str
    severity: str
    source: str = "technical"       # technical | editorial
    issue: str
    explanation: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: Evidence
    recommendation: str = ""
    timestamp_clip: Optional[float] = None    # seconds in the cut clip, before any speed change
    timestamp_final: Optional[float] = None   # seconds in the shipped file
    evaluator_version: str = "technical-1"


class QualityReport(BaseModel):
    version: int = REPORT_VERSION
    status: str = "passed"
    findings: List[Finding] = Field(default_factory=list)
    checks: Dict[str, str] = Field(default_factory=dict)       # check name -> passed | warning | failed | skipped
    evaluator_versions: Dict[str, str] = Field(default_factory=dict)
    agreement_rate: Optional[float] = None                     # editorial evaluator only
    corrections: List[str] = Field(default_factory=list)
    metadata: Dict[str, object] = Field(default_factory=dict)


def has_evidence(evidence: Evidence) -> bool:
    return bool(evidence.ref) or evidence.value not in (None, "", [], {})


def status_for(findings: List[Finding]) -> str:
    if any(f.severity == "critical" for f in findings):
        return "failed"
    if any(f.severity == "warning" and f.confidence >= REVIEW_CONFIDENCE for f in findings):
        return "review"
    return "passed"


def summary(report: Optional[QualityReport]) -> Dict[str, object]:
    """A small view for list payloads: status and counts only."""
    if report is None:
        return {}
    counts: Dict[str, int] = {}
    for f in report.findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return {"status": report.status, "findings": counts}
