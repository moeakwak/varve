"""The 5-segment ``input_key`` and the source-match rule.

``input_key = H(config, upstream, inputs, values, version)``; the source
fingerprint is kept separate (``units.source_fp``) and compared by
:func:`source_match`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from varve.keying.config_access import check_config, project_config
from varve.keying.fingerprint import CANONICAL_ENCODER, InputDigest, json_hash, normalize_json

SEGMENTS = ("config", "upstream", "inputs", "values", "version")
SEGMENT_REASONS = {
    "config": "config-changed",
    "upstream": "upstream-changed",
    "inputs": "input-changed",
    "values": "value-changed",
    "version": "version-changed",
}
FILESET_THRESHOLD = 32


def config_data(config: Any) -> dict[str, Any]:
    """JSON view of a pydantic or dict Config used for keying."""

    check_config(config)
    if isinstance(config, BaseModel):
        return config.model_dump(mode="json")
    return dict(config)


def plain_hash(obj: Any) -> str:
    """``json_hash`` for data that is already plain JSON (str/int/float/bool/None/list/dict).

    Skips the normalization pass; the result equals ``json_hash(obj)`` for such data.
    """

    try:
        text = CANONICAL_ENCODER.encode(obj)
    except ValueError as error:
        raise ValueError(f"NaN and Infinity are not allowed in varve keys ({error})") from None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def version_hash(version: str | None, produces: str | list[str] | None) -> str:
    """Hash of the ``version`` segment: ``version=`` plus the normalized ``produces=``.

    Changing ``produces=`` changes the artifact contract, so it invalidates even
    when ``version=`` pins the source.
    """

    if produces is None:
        return json_hash(version)
    return json_hash({"version": version, "produces": produces})


def combine_hashes(hashes: Mapping[str, str]) -> str:
    """The ``input_key`` from the five segment hashes (equals ``json_hash(hashes)``)."""

    text = (
        f'{{"config":"{hashes["config"]}","inputs":"{hashes["inputs"]}",'
        f'"upstream":"{hashes["upstream"]}","values":"{hashes["values"]}",'
        f'"version":"{hashes["version"]}"}}'
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def inputs_hash(inputs: Mapping[str, InputDigest]) -> str:
    return plain_hash({name: inputs[name].digest for name in inputs})


@dataclass(frozen=True)
class FileSetRow:
    """A ``file_sets`` row: an input's material stored once, referenced by hash."""

    fileset_hash: str
    file_count: int
    total_bytes: int
    entries: list[Any]


@dataclass(frozen=True)
class InputKey:
    input_key: str
    components: dict[str, Any]
    """``key_components_json``: ``{segment: {"hash": ..., "summary": ...}}``."""
    file_sets: list[FileSetRow]
    """Large input materials to upsert into ``file_sets`` before committing the unit."""


def compute_input_key(
    *,
    config: Any,
    config_access: list[str] | None,
    upstream: Mapping[str, Any],
    inputs: Mapping[str, InputDigest],
    values: Mapping[str, Any],
    version: str | None,
    produces: str | list[str] | None = None,
    fileset_threshold: int = FILESET_THRESHOLD,
    hashes: Mapping[str, str] | None = None,
) -> InputKey:
    """Assemble the key of one unit.

    ``config`` is projected on ``config_access`` (``None``: whole config).
    ``upstream`` maps upstream/Ref names to their key entries (an artifact fp,
    or ``[[unit_key, fp], ...]`` for fan-in).
    ``inputs`` come from ``FingerprintSession.inputs`` (Dependencies inputs
    plus ``Unit.axis_inputs()``); ``values`` are Dependencies values plus
    ``Unit.axis_values()``. ``hashes`` may carry segment hashes the probe
    already computed for this material (any of the five segments); they must
    equal what this function would compute.

    Every segment summary is a function of the segment hash (the store keeps
    long summaries once per hash), so input summaries carry no absolute paths:
    an input records how many roots it declared (``root_count``), not where
    they are. Inputs with more than ``fileset_threshold`` files store their
    entries in ``file_sets``.
    """

    known = hashes or {}
    projected = project_config(config_data(config), config_access)
    input_summary: dict[str, Any] = {}
    file_sets: list[FileSetRow] = []
    for name in sorted(inputs):
        digest = inputs[name]
        summary: dict[str, Any] = {
            "digest": digest.digest,
            "files": digest.files,
            "bytes": digest.bytes,
            "root_count": len(digest.roots),
        }
        if digest.files > fileset_threshold:
            summary["fileset"] = digest.digest
            file_sets.append(FileSetRow(digest.digest, digest.files, digest.bytes, digest.material))
        else:
            summary["entries"] = digest.material
        input_summary[name] = summary
    upstream_summary = dict(upstream)
    values_summary = normalize_json(dict(values))
    segment_hashes = {
        "config": known.get("config") or json_hash(projected),
        "upstream": known.get("upstream") or json_hash(upstream_summary),
        "inputs": known.get("inputs") or inputs_hash(inputs),
        "values": known.get("values") or plain_hash(values_summary),
        "version": known.get("version") or version_hash(version, produces),
    }
    components = {
        "config": {"hash": segment_hashes["config"], "summary": projected, "access": config_access},
        "upstream": {"hash": segment_hashes["upstream"], "summary": upstream_summary},
        "inputs": {"hash": segment_hashes["inputs"], "summary": input_summary},
        "values": {"hash": segment_hashes["values"], "summary": values_summary},
        "version": {"hash": segment_hashes["version"], "summary": version, "produces": produces},
    }
    return InputKey(combine_hashes(segment_hashes), components, file_sets)


def changed_segments(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    """Segments whose hash differs between two ``key_components`` documents."""

    return [
        segment
        for segment in SEGMENTS
        if old.get(segment, {}).get("hash") != new.get(segment, {}).get("hash")
    ]


SourceMatch = Literal["match", "reused", "code-modified", "changed"]


def source_match(
    *,
    version: str | None,
    current: str,
    recorded: str,
    reused: str | None,
) -> SourceMatch:
    """Compare the current ``source_fp`` with a unit record (spec §4.4.1).

    With ``version`` set, a differing source never invalidates (``code-modified``
    is a display label). Otherwise the current fp must equal the recorded one
    (``match``) or the ``varve reuse`` exemption (``reused``), else ``changed``.
    """

    if current == recorded:
        return "match"
    if version is not None:
        return "code-modified"
    if reused is not None and current == reused:
        return "reused"
    return "changed"
