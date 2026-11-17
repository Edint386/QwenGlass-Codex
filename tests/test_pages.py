from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from glasscodex.panel import PAGE_TABLES, Panel

ROOT = Path(__file__).resolve().parents[1]


class Link:
    online = True
    sessions = [{
        "id": "t1",
        "name": "测试任务",
        "project": "acme-web",
        "state": "done",
        "state_label": "已完成",
        "active": False,
        "at": 1,
    }]
    last_selected = "t1"
    font_size = 18

    def turn(self, session: str):
        return None

    def watch(self, session: str | None) -> None:
        pass

    def send(self, session: str, text: str):
        return "cmd"


def test_page_is_well_formed_and_within_limits():
    root = ET.parse(ROOT / "pages/codex-panel/template.xml").getroot()
    assert root.tag == "view"
    assert len(list(root)) <= 64

    ignored = {"json", "event", "key-event", "lifecycle-event", "data"}
    bindings = Counter()
    for element in root.iter():
        if element.tag in ignored:
            continue
        for name, value in element.attrib.items():
            if "{{" in value:
                bindings[(element.tag, name)] += 1
    assert sum(bindings.values()) <= 128

    events = [element for element in root if element.tag == "event"]
    assert len(events) <= 64
    assert len({element.get("match") for element in events}) <= 32


def test_page_data_has_no_more_than_64_top_level_keys():
    panel = Panel("g", Link())
    messages = panel.start({"pages": [{"templateId": "1", "description": "codex-panel"}]})
    data = messages[0]["payload"]["actions"][0]["payload"]["view"]["props"]["data"]
    assert len(data) <= 64
    assert set(PAGE_TABLES) <= set(data)

    mock = json.loads((ROOT / "pages/codex-panel/data.json").read_text())
    assert len(mock) <= 64
