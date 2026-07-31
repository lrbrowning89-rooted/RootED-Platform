from pathlib import Path
import json
import xml.etree.ElementTree as ET
import sys

ROOT = Path(__file__).resolve().parents[1]
errors = []

manifest = json.loads((ROOT / "metadata" / "asset_manifest.json").read_text())
index = json.loads((ROOT / "metadata" / "component_index.json").read_text())

for label, rel in manifest["overlays"].items():
    path = ROOT / rel
    if not path.exists():
        errors.append(f"Missing overlay: {rel}")
        continue
    tree = ET.parse(path)
    root = tree.getroot()
    ids = []
    for elem in root.iter():
        value = elem.attrib.get("id")
        if value:
            ids.append(value)
    duplicates = sorted({x for x in ids if ids.count(x) > 1})
    if duplicates:
        errors.append(f"{label}: duplicate IDs: {duplicates}")
    expected_key = "MASTER_CELL_LIBRARY_PLANT" if label == "plant" else "MASTER_CELL_LIBRARY_ANIMAL"
    expected = set(index["assets"][expected_key])
    missing = sorted(expected - set(ids))
    if missing:
        errors.append(f"{label}: IDs missing from overlay: {missing}")

if errors:
    print("VALIDATION FAILED")
    for error in errors:
        print("-", error)
    sys.exit(1)

print("VALIDATION PASSED")
print("Checked manifests, overlay files, and stable ID coverage.")
