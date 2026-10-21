"""Keep the shipped examples and configuration in sync with runtime behavior."""

import json
from pathlib import Path

from analyse_message.codec import Compiler, Limits, templates

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_examples_match_and_compile():
    expected = templates()
    files = list((ROOT / "examples").glob("*.json"))
    assert {file.stem for file in files} == set(expected)
    for file in files:
        document = json.loads(file.read_text(encoding="utf-8"))
        assert document == expected[file.stem]
        assert Compiler(Limits(), "1234", "测试用户").compile(document).segments


def test_configuration_defaults_match_limits():
    config = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    defaults = {name: item["default"] for name, item in config.items()}
    assert Limits.from_config(defaults) == Limits()
    assert defaults["build_admin_only"] is True
