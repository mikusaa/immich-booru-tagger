"""Versioned offline translations with user overrides."""
import hashlib
import json
from collections import Counter
from pathlib import Path


class TranslationCatalog:
    categories = {"general": "属性", "character": "角色", "rating": "评级"}

    def __init__(self, path: Path, overrides: Path | None = None):
        raw = path.read_bytes()
        data = json.loads(raw)
        self.tags = data["tags"]
        if not isinstance(self.tags, dict):
            raise ValueError("Translation catalog must contain a tags object")
        if overrides:
            override_bytes = overrides.read_bytes()
            self.tags = {**self.tags, **json.loads(override_bytes)}
            raw += override_bytes
        for name, entry in self.tags.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                raise ValueError("Invalid translation entry")
            if not isinstance(entry.get("zh"), str) or entry.get("kind") not in self.categories:
                raise ValueError(f"Invalid translation for {name}")
            if any(ord(char) < 32 or ord(char) == 127 for char in entry["zh"]):
                raise ValueError(f"Translation contains control characters: {name}")
        self.revision = hashlib.sha256(raw).hexdigest()[:16]
        self.missing = Counter()

    def translate(self, name, kind=None):
        entry = self.tags.get(name)
        if not entry or not entry["zh"].strip():
            self.missing[name] += 1
            return None
        category = self.categories[kind or entry["kind"]]
        # A translated slash is text, not a new hierarchy level.
        label = entry["zh"].strip().replace("/", "／")
        if any(c in label for c in "\n\r\t"):
            raise ValueError(f"Translation contains control characters: {name}")
        return f"{category}/{label}"
