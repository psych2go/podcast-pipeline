import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agent_pipeline import merge_alias_repair_changes


def test_int_alias_repairs_merge_without_typeerror():
    out = merge_alias_repair_changes(["x"], 3)
    assert out == ["x", "public_entity_alias_repairs: 3"]


def test_zero_repairs_and_none_changes():
    assert merge_alias_repair_changes(None, 0) == []
