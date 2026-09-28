import pytest
from kpip.core.packaging import SpecifierSet, parse_requirement
from kpip.core.versions import Version
from kpip.core.wheel import WheelTag
from kpip.index.candidate_evaluators import CandidateEvaluator
from kpip.index.candidates import InstallationCandidate
from kpip.index.links import Link
from kpip.index.source_models import CandidateRecord, RejectedCandidate


def test_sort_key_uses_best_supported_tag_rank() -> None:
    evaluator = CandidateEvaluator(
        "demo-pkg",
        supported_tags=[
            WheelTag("py3", "none", "any"),
            WheelTag("py2", "none", "any"),
        ],
        specifier=SpecifierSet(),
    )
    candidate = InstallationCandidate(
        "demo-pkg",
        "1.0",
        Link.from_url(
            "https://example.invalid/demo_pkg-1.0-py2.py3-none-any.whl",
            source_url=None,
        ),
    )

    assert evaluator.sort_key_internal(candidate)[6] == 0


def test_sort_key_reuses_preparsed_wheel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link = Link.from_url(
        "https://example.invalid/demo_pkg-1.0-py3-none-any.whl",
        source_url=None,
    )
    candidate = InstallationCandidate.from_link(link)
    assert isinstance(candidate, InstallationCandidate)
    evaluator = CandidateEvaluator(
        "demo-pkg",
        supported_tags=[WheelTag("py3", "none", "any")],
        specifier=SpecifierSet(),
    )

    def fail_wheel_parse(filename: str) -> None:
        raise AssertionError(f"reparsed wheel filename: {filename}")

    monkeypatch.setattr("kpip.index.candidate_evaluators.Wheel", fail_wheel_parse)

    assert evaluator.sort_key_internal(candidate)[6] == 0


def test_sort_key_marks_unsupported_wheel_tag() -> None:
    evaluator = CandidateEvaluator(
        "demo-pkg",
        supported_tags=[WheelTag("py3", "none", "any")],
        specifier=SpecifierSet(),
    )
    candidate = InstallationCandidate(
        "demo-pkg",
        "1.0",
        Link.from_url(
            "https://example.invalid/demo_pkg-1.0-py2-none-any.whl",
            source_url=None,
        ),
    )

    assert evaluator.sort_key_internal(candidate)[6] == -1_000_000


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
