"""Requirement-file and pylock parsing."""

from kpip.resolution.files.models import (
    ParsedRequirement,
    RequirementsFileParseError,
)
from kpip.resolution.files.options import (
    expand_env_variables,
    merge_config_setting,
    normalize_reference,
    strip_matching_quotes,
)
from kpip.resolution.files.parser import (
    parse_line,
    parse_requirement_line,
    parse_requirements,
    parse_requirements_internal,
)
from kpip.resolution.files.pylock import (
    is_pylock_reference,
    parse_pylock,
    pylock_location,
)

__all__ = [
    "ParsedRequirement",
    "RequirementsFileParseError",
    "expand_env_variables",
    "is_pylock_reference",
    "merge_config_setting",
    "normalize_reference",
    "parse_line",
    "parse_pylock",
    "parse_requirement_line",
    "parse_requirements",
    "parse_requirements_internal",
    "pylock_location",
    "strip_matching_quotes",
]
