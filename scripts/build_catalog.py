"""Build the offline WD14 vocabulary from a pinned translation database."""
import argparse
import csv
import hashlib
import io
import json
import sqlite3
import zipfile
from pathlib import Path

SOURCE_REVISION = "e665a101ed16adc019c8d5c892f5567735156381"
SOURCE = "https://github.com/ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--labels-wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("data/tag_translations.json"))
    args = parser.parse_args()
    with zipfile.ZipFile(args.labels_wheel) as archive:
        raw = archive.read("wdtagger/assets/selected_tags.csv")
    labels = list(csv.DictReader(io.StringIO(raw.decode())))
    tags = {}
    with sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True) as database:
        for label in labels:
            if label["category"] not in ("0", "4"):
                continue
            row = database.execute("SELECT cn_name FROM tags WHERE name = ?", (label["name"],)).fetchone()
            if row and row[0] and row[0].strip():
                tags[label["name"]] = {"zh": row[0].strip(), "kind": "character" if label["category"] == "4" else "general"}
    for name, label in {"general": "全年龄", "sensitive": "敏感", "questionable": "疑似成人", "explicit": "成人"}.items():
        tags[name] = {"zh": label, "kind": "rating"}
    output = {
        "source": SOURCE, "revision": SOURCE_REVISION, "license": "MIT",
        "vocabulary": "wdtagger==0.16.0 / wd-swinv2-tagger-v3",
        "labels_sha256": hashlib.sha256(raw).hexdigest(),
        "database_sha256": hashlib.sha256(args.database.read_bytes()).hexdigest(),
        "labels_count": len(labels), "translated_count": len(tags),
        "tags": dict(sorted(tags.items())),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Translations: {len(tags)}/{len(labels)}")


if __name__ == "__main__":
    main()
