"""Keep relative documentation links and GitHub heading fragments usable."""

import html
import re
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]


def without_fences(text):
    lines = []
    fence = None
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence:
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence):
                fence = None
        elif match:
            fence = match[1]
        else:
            lines.append(line)
    return "\n".join(lines)


def heading_ids(text):
    """GitHub lower-cases headings, removes formatting and suffixes duplicate slugs."""
    counts = Counter()
    ids = set()
    lines = without_fences(text).splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            title = match[1]
        elif index + 1 < len(lines) and re.fullmatch(r"\s{0,3}(?:=+|-+)\s*", lines[index + 1]):
            title = line.strip()
        else:
            continue
        title = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", title)
        title = re.sub(r"<[^>]+>", "", title)
        title = re.sub(r"(?<!\w)_{1,2}(.+?)_{1,2}(?!\w)", r"\1", title)
        title = html.unescape(title).lower().replace("`", "").replace("*", "").replace("~", "")
        slug = "".join(c for c in title if c in "_- " or unicodedata.category(c)[0] in "LN")
        slug = slug.replace(" ", "-")
        candidate = slug
        while candidate in ids:
            counts[slug] += 1
            candidate = f"{slug}-{counts[slug]}"
        ids.add(candidate)
    # Explicit HTML anchors are also valid documentation targets.
    ids.update(re.findall(r'<a\s+(?:id|name)=["\']([^"\']+)', text, re.IGNORECASE))
    return ids


def destinations(text):
    text = without_fences(text)
    references = {}

    def ref_key(value):
        return " ".join(value.split()).casefold()

    def definition(match):
        references[ref_key(match[1])] = match[2] or match[3]
        return ""

    text = re.sub(
        r"^\s{0,3}\[([^\]]+)\]:\s*(?:<([^>]+)>|(\S+))[^\n]*$",
        definition,
        text,
        flags=re.MULTILINE,
    )
    # Inline code cannot contain Markdown links. Keep the code in headings above.
    text = re.sub(r"(`+).*?\1", "code", text, flags=re.DOTALL)
    pattern = re.compile(
        r"!?\[([^\]\n]+)\](?:"
        r'\(\s*(?:<([^>]+)>|((?:[^\s()]|\([^()]*\))+))(?:\s+["\'][^\n]*?["\'])?\s*\)'
        r"|\[([^\]]*)\])?"
    )
    for match in pattern.finditer(text):
        if match[2] or match[3]:
            yield match[2] or match[3]
        else:
            key = ref_key(match[4] or match[1])
            if match[4] is not None and key not in references:
                yield f"__missing_reference__/{key}"
            elif key in references:
                yield references[key]


def link_errors(source):
    errors = []
    for destination in destinations(source.read_text()):
        url = urlsplit(html.unescape(destination))
        if url.scheme or url.netloc or destination.startswith("/"):
            continue
        target = (source.parent / unquote(url.path)).resolve() if url.path else source
        if not target.exists():
            errors.append(f"{destination}: missing file")
        elif (
            url.fragment
            and target.suffix.lower() == ".md"
            and unquote(url.fragment) not in heading_ids(target.read_text())
        ):
            errors.append(f"{destination}: missing anchor")
    return errors


@pytest.mark.parametrize(
    "source",
    [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))],
    ids=lambda path: path.name,
)
def test_documentation_links_resolve(source):
    assert not (errors := link_errors(source)), f"{source.name}: " + "; ".join(errors)


def test_formatted_duplicate_and_reference_targets(tmp_path):
    (tmp_path / "guide.md").write_text(
        "# A **warm** `welcome` &amp; [hello](elsewhere.md)\n"
        "# Same\n# Same\n# Same-1\n# Same\n"
        "Old *heading*\n----------\n# An _underlined_ heading\n"
    )
    (tmp_path / "image.png").write_bytes(b"invented image")
    source = tmp_path / "README.md"
    source.write_text(
        "[guide](guide.md#a-warm-welcome--hello)\n"
        "[duplicate][two]\n[two]: guide.md#same-1\n"
        "[collision](guide.md#same-1-1)\n[third](guide.md#same-2)\n"
        "[old](guide.md#old-heading)\n![image](image.png)\n"
        "[formatted](guide.md#an-underlined-heading)\n"
        "[shortcut]\n[shortcut]: guide.md#same\n"
        "[collapsed][]\n[collapsed]: guide.md#same\n"
        "[decoded](guide.md#%73ame)\n"
        "```md\n[ignored](missing.md)\n```\n"
        "`[ignored](missing.md)`\n[external](https://example.invalid/missing)\n"
    )
    assert link_errors(source) == []


@pytest.mark.parametrize(
    "link,reason",
    [
        ("[lost](missing.md)", "missing file"),
        ("[`lost`](missing.md)", "missing file"),
        ("![lost image](missing.png)", "missing file"),
        ("[lost](guide.md#missing)", "missing anchor"),
        ("[lost][undefined]", "missing file"),
    ],
)
def test_missing_targets_fail(tmp_path, link, reason):
    (tmp_path / "guide.md").write_text("# Existing\n")
    source = tmp_path / "README.md"
    source.write_text(link)
    assert reason in link_errors(source)[0]
