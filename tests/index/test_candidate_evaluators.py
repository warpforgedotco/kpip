from kpip.core.packaging import parse_requirement
from kpip.core.versions import Version
from kpip.index.candidate_evaluators import CandidateEvaluator
from kpip.index.candidates import InstallationCandidate
from kpip.index.links import Link
from kpip.index.source_models import CandidateRecord, RejectedCandidate


def test_unnamed_direct_archive_uses_materializable_record() -> None:
    link = Link.from_url(
        "https://example.invalid/archive/master.zip",
        source_url=None,
    )
    parsed = InstallationCandidate.from_link(link)
    assert isinstance(parsed, RejectedCandidate)
    requirement = parse_requirement(f"source @ {link.url}")
    assert requirement is not None

    candidate = CandidateEvaluator.evaluate_parsed_link(
        link,
        parsed,
        requirement,
        allow_yanked=True,
        allow_binary=True,
        allow_source=True,
    )

    assert type(candidate) is CandidateRecord
    assert candidate.version == Version("0")


def test_unparseable_requires_python_is_ignored() -> None:
    """pip ignores ``>=3.6.*`` rather than the release that declares it."""
    CandidateEvaluator.requires_python_matches.cache_clear()
    assert CandidateEvaluator.requires_python_matches(">=3.6.*") is True
    assert CandidateEvaluator.requires_python_matches("not a specifier") is True
    assert CandidateEvaluator.requires_python_matches("<2") is False
