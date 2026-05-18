"""Defensive XML parser for analyzers and remediators.

Every analyzer that reads attacker-supplied OOXML (DOCX, PPTX, XLSX) routes
through :data:`SAFE_XML_PARSER`. The parser is configured to:

* refuse external entity resolution (no XXE file/URL exfil)
* refuse network fetches by the parser itself
* refuse the "huge_tree" relaxation that allows pathological nesting
* refuse recovery mode (malformed XML fails fast rather than smuggling content)

The parser is a singleton because :class:`lxml.etree.XMLParser` is thread-safe
for repeated ``fromstring`` calls within the same process and instantiating one
parser per call costs measurable CPU on large workbooks.
"""

from __future__ import annotations

from lxml import etree

# Shared, immutable parser. Do not mutate at runtime.
SAFE_XML_PARSER: etree.XMLParser = etree.XMLParser(
    resolve_entities=False,  # block XXE
    no_network=True,         # block parser-level network fetches
    huge_tree=False,         # cap nesting / entity expansion
    recover=False,           # fail fast on malformed XML
)


__all__ = ["SAFE_XML_PARSER"]
