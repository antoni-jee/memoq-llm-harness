"""XML backend shim.

Prefers ``lxml`` (better round-tripping of memoQ files: it preserves the
original namespace prefixes, comments and processing instructions).  Falls back
to the stdlib ``xml.etree.ElementTree`` when lxml is unavailable, which is a
real possibility on Windows-on-ARM where a matching lxml wheel may not exist.

The rest of the package only talks to this module so the two backends never
leak into the parser/serialiser logic.
"""

from __future__ import annotations

import re
from typing import Dict, Optional

try:  # pragma: no cover - exercised implicitly by whichever backend is present
    from lxml import etree as _etree

    HAVE_LXML = True
except ImportError:  # pragma: no cover
    import xml.etree.ElementTree as _etree  # type: ignore[no-redef]

    HAVE_LXML = False

etree = _etree

XLIFF_NS = "urn:oasis:names:tc:xliff:document:1.2"
MQ_NS = "http://www.memoq.com/memoq/xliff"

# Namespaces memoQ is known to emit.  Registering them keeps the ElementTree
# fallback from rewriting every prefix as ``ns0:``/``ns1:``.
KNOWN_NAMESPACES: Dict[str, str] = {
    "": XLIFF_NS,
    "mq": MQ_NS,
    "xml": "http://www.w3.org/XML/1998/namespace",
}

_XMLNS_RE = re.compile(rb"""xmlns(?::([A-Za-z_][\w.\-]*))?\s*=\s*["']([^"']+)["']""")
# Same shape, unicode, with the leading space so removal leaves no double gap.
_NS_DECL_RE = re.compile(r"""\s+xmlns(:[A-Za-z_][\w.\-]*)?\s*=\s*["']([^"']*)["']""")
_DECL_RE = re.compile(rb"""^\s*<\?xml[^>]*\?>""")


def qname(ns: str, tag: str) -> str:
    """Build a Clark-notation qualified name."""
    return "{%s}%s" % (ns, tag)


def localname(tag: object) -> str:
    """Return the local part of a (possibly namespaced) tag.

    Comments and processing instructions have a callable/non-string ``tag`` in
    both backends; those yield an empty string.
    """
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def harvest_namespaces(raw: bytes) -> Dict[str, str]:
    """Scan raw document bytes for ``xmlns`` declarations.

    Used to register prefixes with the ElementTree fallback before writing, so
    that a round-tripped file keeps memoQ's original prefixes.
    """
    found: Dict[str, str] = {}
    for prefix, uri in _XMLNS_RE.findall(raw):
        found[(prefix or b"").decode("utf-8")] = uri.decode("utf-8")
    return found


def register_namespaces(mapping: Dict[str, str]) -> None:
    """Register prefix->URI pairs with the ElementTree fallback (no-op on lxml)."""
    if HAVE_LXML:
        return
    merged = dict(KNOWN_NAMESPACES)
    merged.update(mapping)
    for prefix, uri in merged.items():
        if prefix == "xml":
            continue
        try:
            _etree.register_namespace(prefix, uri)
        except (ValueError, TypeError):
            continue


def parse_bytes(raw: bytes):
    """Parse a document from bytes and return its root element."""
    if HAVE_LXML:
        parser = _etree.XMLParser(
            remove_blank_text=False,
            resolve_entities=False,
            recover=False,
            huge_tree=True,
        )
        return _etree.fromstring(raw, parser=parser)
    register_namespaces(harvest_namespaces(raw))
    return _etree.fromstring(raw)


def fragment_to_element(fragment: str, nsmap: Optional[Dict[str, str]] = None):
    """Parse an XML *fragment* (no single root required) into a wrapper element.

    The fragment is wrapped in a ``<w>`` element carrying the supplied namespace
    declarations so that prefixes used inside the fragment resolve.
    """
    decls = []
    for prefix, uri in (nsmap or {}).items():
        if not uri or prefix == "xml":
            continue
        attr = "xmlns" if prefix in ("", None) else "xmlns:%s" % prefix
        decls.append('%s="%s"' % (attr, _escape_attr(uri)))
    wrapper = "<w %s>%s</w>" % (" ".join(decls), fragment)
    if HAVE_LXML:
        parser = _etree.XMLParser(resolve_entities=False, huge_tree=True)
        return _etree.fromstring(wrapper.encode("utf-8"), parser=parser)
    return _etree.fromstring(wrapper.encode("utf-8"))


def inner_xml(elem, nsmap: Optional[Dict[str, str]] = None) -> str:
    """Serialise an element's *children and text* without its own tags.

    This is the textual form the tag-isolation layer operates on.

    Serialising a child out of its document context makes lxml repeat every
    inherited namespace declaration on it, so a two-attribute ``<bpt>`` comes
    back as ``<bpt xmlns="urn:oasis:..." xmlns:mq="http://..." id="1">``. That
    is valid but noisy: it bloats every re-exported target and makes the raw
    tag inspector unreadable. Declarations that merely restate the document's
    own namespaces are stripped; anything genuinely local is kept.
    """
    parts = [elem.text or ""]
    for child in elem:
        parts.append(strip_inherited_namespaces(
            node_to_string(child, with_tail=True), nsmap))
    return "".join(parts)


def strip_inherited_namespaces(xml: str, nsmap: Optional[Dict[str, str]]) -> str:
    """Remove ``xmlns`` declarations that duplicate the document's own."""
    if not nsmap or not xml:
        return xml

    def _drop(match: "re.Match") -> str:
        prefix = (match.group(1) or "").lstrip(":")
        uri = match.group(2)
        return "" if nsmap.get(prefix) == uri else match.group(0)

    return _NS_DECL_RE.sub(_drop, xml)


def node_to_string(node, with_tail: bool = False) -> str:
    """Serialise a single node to a unicode string."""
    if HAVE_LXML:
        return _etree.tostring(node, encoding="unicode", with_tail=with_tail)
    saved_tail = node.tail
    if not with_tail:
        node.tail = None
    try:
        return _etree.tostring(node, encoding="unicode")
    finally:
        node.tail = saved_tail


def replace_children(elem, fragment: str, nsmap: Optional[Dict[str, str]] = None) -> None:
    """Replace ``elem``'s entire content with a parsed XML fragment."""
    wrapper = fragment_to_element(fragment, nsmap)
    for child in list(elem):
        elem.remove(child)
    elem.text = wrapper.text
    for child in list(wrapper):
        elem.append(child)


def element_nsmap(root) -> Dict[str, str]:
    """Best-effort namespace map for a root element."""
    if HAVE_LXML:
        return {k or "": v for k, v in (root.nsmap or {}).items()}
    return dict(KNOWN_NAMESPACES)


def serialize_document(root, original: bytes) -> bytes:
    """Serialise a document root, preserving the original XML declaration."""
    if HAVE_LXML:
        body = _etree.tostring(root, encoding="utf-8", xml_declaration=False)
    else:
        body = _etree.tostring(root, encoding="utf-8", xml_declaration=False)
    decl_match = _DECL_RE.match(original)
    decl = decl_match.group(0) if decl_match else b'<?xml version="1.0" encoding="utf-8"?>'
    return decl.strip() + b"\r\n" + body


def _escape_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


def escape_text(value: str) -> str:
    """Escape a plain-text string for embedding as XML character data."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def backend_name() -> str:
    return "lxml" if HAVE_LXML else "xml.etree.ElementTree"
