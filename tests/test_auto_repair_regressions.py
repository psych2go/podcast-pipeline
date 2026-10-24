import json

from scripts.canonical_entities import public_entity_alias_errors
from scripts.content_map import enrich_content_map_evidence, enrich_summary_map_evidence


def test_missing_claim_mapping_is_attached_to_matching_unit_chapter():
    summary = {"chapters": [{"title": "Unit", "unit_ids": ["U0008"]}]}
    result = enrich_summary_map_evidence(
        summary, "notes", {"units": [{"id": "U0008", "claims": ["claim"], "importance": "high", "status": "included"}]})
    assert "U0008-C01" in result["notes_claim_ids"]
    assert "U0008-C01" in result["chapters"][0]["claim_ids"]


def test_person_short_name_is_allowed_after_canonical_name():
    entities = {"entities": [{
        "entity_type": "person", "canonical_name": "Arvind Krishna",
        "observed_names": ["Arvind"], "public_aliases": [],
    }]}
    assert public_entity_alias_errors(entities, "Arvind Krishna later said Arvind agreed.") == []


def test_previous_segment_ids_recover_empty_timestamps():
    transcript = {"segments": [
        {"id": "S0001", "start": 0, "end": 1, "text": "evidence"},
    ]}
    content = {"units": [{"id": "U0001", "claims": ["claim"],
                           "timestamps": [], "evidence": {"segment_ids": ["S0001"]}}]}
    enriched, _ = enrich_content_map_evidence(content, transcript)
    assert enriched["units"][0]["evidence"]["segment_ids"] == ["S0001"]
