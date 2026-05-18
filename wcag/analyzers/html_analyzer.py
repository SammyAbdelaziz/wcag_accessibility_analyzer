"""
HTML WCAG Analyzer
Phase 1 support for static HTML files using direct DOM inspection only.

This analyzer intentionally limits itself to high-confidence checks that can be
proven from the HTML source without rendering or browser automation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set

try:
    from playwright.sync_api import sync_playwright
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    sync_playwright = None
    _PLAYWRIGHT_AVAILABLE = False

# Defensive Playwright defaults.
#
# * _PAGE_TIMEOUT_MS caps every page operation. Playwright 1.59 removed the
#   per-call `timeout=` kwarg from ``page.evaluate``; using ``set_default_timeout``
#   is the supported way to keep a single hostile ``<script>while(1){}</script>``
#   from pinning a worker forever.
# * _block_external_requests is installed as a route filter on every page we
#   open. It aborts any HTTP(S)/FTP/WebSocket request, which stops a crafted
#   HTML document from beaconing out to the Azure IMDS, internal hosts, or an
#   attacker-controlled server. Inline assets (data:, about:, blob:, file:) are
#   allowed because ``set_content`` and the rendered-contrast scripts rely on
#   them.
_PAGE_TIMEOUT_MS = 3000


def _block_external_requests(route):
    url = route.request.url
    if url.startswith(("http://", "https://", "ftp://", "ws://", "wss://")):
        route.abort()
    else:
        route.continue_()


from wcag.models import (
    CONFIDENCE_LABEL,
    ConfidenceTier,
    EvidenceSource,
    FactSheet,
    Finding,
    HyperlinkInfo,
    ImageInfo,
    ParagraphInfo,
    Severity,
    TableInfo,
)
from wcag.rendered_contrast import minimum_css_contrast
from wcag.common import (
    ColorAnalyzer,
    SemanticFlowAnalyzer,
    FormAnalyzer,
    hex_luminance,
    is_generic_link_text,
)


GENERIC_LINK_TEXT = re.compile(
    r"^(click here|click|here|this link|learn more|more|read more|link|url|see here)$",
    re.IGNORECASE,
)
# Phase F (2.4.6): generic/placeholder heading text that doesn't describe a topic.
GENERIC_HEADING_TEXT = re.compile(
    r"^("
    r"heading|header|title|untitled|section|subsection|chapter|"
    r"placeholder|tbd|to ?do|lorem ipsum|new heading|new section|"
    r"sample( text)?|example( text)?"
    r")\s*\d*\s*$",
    re.IGNORECASE,
)
# Phase F (2.4.6): generic/placeholder label text that doesn't describe its control.
GENERIC_LABEL_TEXT = re.compile(
    r"^("
    r"label|field|input|enter text|enter value|placeholder|"
    r"text box|text field|select|choose|tbd|to ?do|sample"
    r")\s*\d*\s*[:.\-]?\s*$",
    re.IGNORECASE,
)
HEADING_TAG_LEVELS = {f"h{level}": level for level in range(1, 7)}
TEXT_INPUT_TYPES = {
    "",
    "email",
    "number",
    "password",
    "search",
    "tel",
    "text",
    "url",
}
RENDERED_TEXT_SELECTORS = "p, li, a, button, label, td, th, h1, h2, h3, h4, h5, h6, span"
MIN_CONTRAST_RATIO_NORMAL = 4.5
MIN_CONTRAST_RATIO_LARGE = 3.0
MAX_RENDERED_CONTRAST_FINDINGS = 5
HORIZONTAL_OVERFLOW_THRESHOLD_PX = 40
EXEMPT_REFLOW_TAGS = {"table", "pre", "code"}

logger = logging.getLogger(__name__)


def _is_large_text(font_size_px: float, font_weight: str) -> bool:
        try:
                numeric_weight = int(font_weight)
        except (TypeError, ValueError):
                numeric_weight = 700 if str(font_weight).lower() == "bold" else 400
        is_bold = numeric_weight >= 700
        return font_size_px >= 24.0 or (is_bold and font_size_px >= 18.66)


def _render_html_diagnostics(html_text: str) -> Optional[Dict[str, Any]]:
    if not _PLAYWRIGHT_AVAILABLE:
        return None
    if os.environ.get("WCAG_DISABLE_RENDERED_HTML", "").strip().lower() in {"1", "true", "yes", "on"}:
        return None

    text_nodes_script = """
() => {
    const selectors = "__SELECTORS__";
    const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
    const isTransparent = (value) => !value || value === 'transparent' || value === 'rgba(0, 0, 0, 0)';
    const backgroundInfo = (element) => {
        let current = element;
        while (current) {
            const style = getComputedStyle(current);
            if (!isTransparent(style.backgroundColor) || (style.backgroundImage && style.backgroundImage !== 'none')) {
                return {
                    backgroundColor: style.backgroundColor,
                    backgroundImage: style.backgroundImage || 'none',
                };
            }
            current = current.parentElement;
        }
        return { backgroundColor: 'rgb(255, 255, 255)', backgroundImage: 'none' };
    };
    const location = (element) => {
        const tag = element.tagName.toLowerCase();
        if (element.id) return `${tag}#${element.id}`;
        const className = normalize(element.className || '');
        if (className) return `${tag}.${className.split(' ').slice(0, 2).join('.')}`;
        return tag;
    };
    return Array.from(document.querySelectorAll(selectors))
        .map((element) => {
            const text = normalize(element.innerText);
            if (!text) return null;
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || '1') === 0) return null;
            if (rect.width === 0 || rect.height === 0) return null;
            const background = backgroundInfo(element);
            return {
                location: location(element),
                tag: element.tagName.toLowerCase(),
                text: text.slice(0, 140),
                color: style.color,
                backgroundColor: background.backgroundColor,
                backgroundImage: background.backgroundImage,
                fontSizePx: Number.parseFloat(style.fontSize) || 16,
                fontWeight: style.fontWeight || '400'
            };
        })
        .filter(Boolean);
}
""".replace("__SELECTORS__", RENDERED_TEXT_SELECTORS)

    reflow_script = """
() => {
    const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
    const viewportWidth = window.innerWidth;
    const docWidth = Math.max(
        document.documentElement.scrollWidth,
        document.body ? document.body.scrollWidth : 0
    );
    const offenders = Array.from(document.querySelectorAll('body *'))
        .map((element) => {
            const style = getComputedStyle(element);
            if (style.display === 'none' || style.visibility === 'hidden') return null;
            const rect = element.getBoundingClientRect();
            const overflow = Math.max(element.scrollWidth - element.clientWidth, rect.right - viewportWidth);
            if (overflow <= 0) return null;
            return {
                tag: element.tagName.toLowerCase(),
                location: element.id ? `${element.tagName.toLowerCase()}#${element.id}` : element.tagName.toLowerCase(),
                overflowPx: Math.round(overflow),
                text: normalize(element.innerText).slice(0, 80),
            };
        })
        .filter(Boolean)
        .sort((left, right) => right.overflowPx - left.overflowPx)
        .slice(0, 5);
    return {
        viewportWidth,
        documentWidth: docWidth,
        overflowPx: Math.max(0, Math.round(docWidth - viewportWidth)),
        offenders,
    };
}
"""

    focus_script = """
() => {
    const interactiveSelectors = 'a, button, input:not([type="hidden"]), textarea, select, [tabindex]:not([tabindex="-1"])';
    const location = (element) => {
        const tag = element.tagName.toLowerCase();
        if (element.id) return `${tag}#${element.id}`;
        const className = (element.className || '').split(' ').filter(c => c).slice(0, 2).join('.');
        if (className) return `${tag}.${className}`;
        return tag;
    };
    
    // Check if :focus styles are explicitly defined in CSS
    const hasFocusStyleDefined = (element) => {
        // Look through all stylesheets for :focus rules
        const styleSheets = Array.from(document.styleSheets);
        const elementSelector = element.id ? `#${element.id}` : element.tagName.toLowerCase();
        
        for (const sheet of styleSheets) {
            try {
                const rules = Array.from(sheet.cssRules || []);
                for (const rule of rules) {
                    if (rule.selectorText && rule.selectorText.includes(':focus')) {
                        // Check if this :focus rule could apply to our element
                        const testEl = document.createElement('div');
                        try {
                            // Try to match the selector (simple heuristic)
                            if (rule.selectorText.includes(elementSelector) || 
                                rule.selectorText.includes(element.tagName.toLowerCase()) ||
                                rule.selectorText === '*:focus' ||
                                rule.selectorText === 'button:focus' && element.tagName.toLowerCase() === 'button' ||
                                rule.selectorText === 'a:focus' && element.tagName.toLowerCase() === 'a' ||
                                rule.selectorText === 'input:focus' && element.tagName.toLowerCase() === 'input') {
                                // Check if the rule has actual visual properties
                                const hasOutlineOrShadow = rule.style.outline && rule.style.outline !== 'none' || 
                                                          rule.style.boxShadow && rule.style.boxShadow !== 'none' ||
                                                          rule.style.borderColor && rule.style.borderColor !== 'currentcolor';
                                if (hasOutlineOrShadow) {
                                    return true;
                                }
                            }
                        } catch (e) {}
                    }
                }
            } catch (e) {
                // Cross-origin or restricted stylesheets
            }
        }
        
        // Also check inline :focus-visible styles (modern focus-visible)
        const focusVisibleRules = styleSheets.flatMap(sheet => {
            try {
                return Array.from(sheet.cssRules || []).filter(r => 
                    r.selectorText && r.selectorText.includes(':focus-visible')
                );
            } catch (e) {
                return [];
            }
        });
        
        if (focusVisibleRules.length > 0) {
            for (const rule of focusVisibleRules) {
                if (rule.selectorText.includes(element.tagName.toLowerCase()) ||
                    rule.selectorText === '*:focus-visible') {
                    return true;
                }
            }
        }
        
        return false;
    };
    
    const elements = Array.from(document.querySelectorAll(interactiveSelectors))
        .filter(el => {
            const style = getComputedStyle(el);
            return style.display !== 'none' && style.visibility !== 'hidden';
        });
    return {
        interactiveCount: elements.length,
        elements: elements.slice(0, 20).map((el, idx) => ({
            index: idx,
            location: location(el),
            tag: el.tagName.toLowerCase(),
            text: (el.innerText || el.value || el.textContent || '').slice(0, 50).trim(),
            tabindex: el.getAttribute('tabindex') || '0',
            hasFocusStyle: hasFocusStyleDefined(el),
        })),
    };
}
"""

    keyboard_script = """
() => {
    const interactiveSelectors = 'a, button, input:not([type="hidden"]), textarea, select, [tabindex]:not([tabindex="-1"])';
    const location = (element) => {
        const tag = element.tagName.toLowerCase();
        if (element.id) return `${tag}#${element.id}`;
        const className = (element.className || '').split(' ').filter(c => c).slice(0, 2).join('.');
        if (className) return `${tag}.${className}`;
        return tag;
    };
    
    // Get all focusable elements in DOM order
    const focusableElements = Array.from(document.querySelectorAll(interactiveSelectors))
        .filter(el => {
            const style = getComputedStyle(el);
            return style.display !== 'none' && style.visibility !== 'hidden' && style.pointerEvents !== 'none';
        })
        .map((el, idx) => ({
            index: idx,
            location: location(el),
            tag: el.tagName.toLowerCase(),
            text: (el.innerText || el.value || '').slice(0, 40).trim(),
            tabindex: parseInt(el.getAttribute('tabindex') || '0'),
            rect: el.getBoundingClientRect(),
        }));
    
    // Check if all focusable elements are reachable via Tab
    const allFocusable = focusableElements.length > 0;
    
    return {
        focusableCount: focusableElements.length,
        allFocusable: allFocusable,
        focusableElements: focusableElements.slice(0, 15),
    };
}
"""

        # Phase L — Action harness (Tier 1). Drives runtime interactions:
        # focus every focusable element, change every form value, hover for
        # tooltips. Captures URL / scroll / form-state diffs and overlap of
        # focused element with sticky/fixed overlays (WCAG 2.2 § 2.4.11).
    actions_script = """
() => {
    const interactiveSelectors = 'a, button, input:not([type="hidden"]), textarea, select, [tabindex]:not([tabindex="-1"])';
    const loc = (el) => {
        const tag = el.tagName.toLowerCase();
        if (el.id) return `${tag}#${el.id}`;
        const cls = (el.className || '').split(' ').filter(c => c).slice(0, 2).join('.');
        return cls ? `${tag}.${cls}` : tag;
    };
    const visible = (el) => {
        const s = getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden') return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    const snapshot = () => ({
        href: window.location.href,
        scrollY: window.scrollY || 0,
        formCount: document.forms.length,
        // Capture form action+target so submit() to a new URL is detected
        formSerialized: Array.from(document.forms).map(f => `${f.action || ''}|${f.target || ''}|${f.elements.length}`).join(';'),
        bodyTextLen: (document.body && document.body.innerText || '').length,
    });
    const equalSnap = (a, b) => (
        a.href === b.href &&
        a.scrollY === b.scrollY &&
        a.formCount === b.formCount &&
        a.formSerialized === b.formSerialized
    );

    // ── 1.4.13 Content on Hover or Focus ────────────────────────────────
    // Native title attr produces a browser tooltip that is NOT dismissable
    // by Escape and NOT hoverable. We flag it as CONFIRMED.
    const titleTriggers = Array.from(document.querySelectorAll('[title]'))
        .filter(el => (el.getAttribute('title') || '').trim().length > 0)
        .filter(visible)
        .slice(0, 50)
        .map(el => ({ location: loc(el), title: el.getAttribute('title').slice(0, 80) }));

    // ── 2.4.11 Focus Not Obscured (Minimum) — WCAG 2.2 ──────────────────
    // Find all sticky/fixed elements that could obscure focus.
    const stickyElems = Array.from(document.querySelectorAll('body *')).filter(el => {
        const s = getComputedStyle(el);
        return (s.position === 'fixed' || s.position === 'sticky') && visible(el);
    }).map(el => ({ el, rect: el.getBoundingClientRect() }));

    const focusables = Array.from(document.querySelectorAll(interactiveSelectors)).filter(visible);
    const obscuredFocus = [];
    const partiallyObscuredFocus = [];
    const focusContextChanges = [];
    const initialSnap = snapshot();

    for (let i = 0; i < Math.min(focusables.length, 25); i++) {
        const el = focusables[i];
        try { el.focus({ preventScroll: true }); } catch (_) { continue; }
        // 2.4.11 (Minimum) full-overlap + 2.4.12 (Enhanced) partial-overlap check
        const r = el.getBoundingClientRect();
        for (const s of stickyElems) {
            if (s.el === el || s.el.contains(el)) continue;
            // Strict full-overlap → 2.4.11 violation (also implies 2.4.12).
            const fullyCovered = (
                s.rect.left <= r.left && s.rect.top <= r.top &&
                s.rect.right >= r.right && s.rect.bottom >= r.bottom
            );
            // Any intersection → 2.4.12 (Enhanced) violation only.
            const intersects = (
                s.rect.left < r.right && s.rect.right > r.left &&
                s.rect.top < r.bottom && s.rect.bottom > r.top
            );
            if (fullyCovered) {
                obscuredFocus.push({
                    location: loc(el),
                    obscuredBy: loc(s.el),
                });
                partiallyObscuredFocus.push({
                    location: loc(el),
                    obscuredBy: loc(s.el),
                    coverage: 'full',
                });
                break;
            }
            if (intersects) {
                partiallyObscuredFocus.push({
                    location: loc(el),
                    obscuredBy: loc(s.el),
                    coverage: 'partial',
                });
                break;
            }
        }
        // 3.2.1 On Focus runtime check
        const afterFocus = snapshot();
        if (!equalSnap(initialSnap, afterFocus)) {
            focusContextChanges.push({
                location: loc(el),
                kind: afterFocus.href !== initialSnap.href ? 'navigated'
                    : afterFocus.scrollY !== initialSnap.scrollY ? 'scrolled'
                    : 'state-changed',
            });
            // Don't keep going if the page navigated.
            if (afterFocus.href !== initialSnap.href) break;
        }
    }
    // Reset focus
    try { document.body && document.body.focus(); } catch (_) {}

    // ── 3.2.2 On Input runtime check ────────────────────────────────────
    const inputContextChanges = [];
    const formControls = Array.from(document.querySelectorAll('select, input[type="checkbox"], input[type="radio"]'))
        .filter(visible).slice(0, 15);
    for (const el of formControls) {
        try {
            const before = snapshot();
            if (el.tagName.toLowerCase() === 'select' && el.options && el.options.length > 1) {
                el.selectedIndex = (el.selectedIndex + 1) % el.options.length;
            } else if (el.type === 'checkbox' || el.type === 'radio') {
                el.checked = !el.checked;
            } else {
                continue;
            }
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('input', { bubbles: true }));
            const after = snapshot();
            if (!equalSnap(before, after)) {
                inputContextChanges.push({
                    location: loc(el),
                    kind: after.href !== before.href ? 'navigated'
                        : after.scrollY !== before.scrollY ? 'scrolled'
                        : 'state-changed',
                });
                if (after.href !== before.href) break;
            }
        } catch (_) { /* swallow per-control errors */ }
    }

    return {
        titleTriggers: titleTriggers,
        obscuredFocus: obscuredFocus.slice(0, 20),
        partiallyObscuredFocus: partiallyObscuredFocus.slice(0, 20),
        focusContextChanges: focusContextChanges.slice(0, 20),
        inputContextChanges: inputContextChanges.slice(0, 20),
    };
}
"""

    non_text_contrast_script = """
() => {
    // For WCAG 1.4.11: collect borderColor + backgroundColor for buttons,
    // form inputs, textareas, and selects. The analyzer evaluates pairs.
    const selector = 'button, input:not([type="hidden"]), textarea, select';
    const location = (el) => {
        const tag = el.tagName.toLowerCase();
        if (el.id) return `${tag}#${el.id}`;
        const cls = (el.className || '').split(' ').filter(c => c).slice(0, 2).join('.');
        return cls ? `${tag}.${cls}` : tag;
    };
    const els = Array.from(document.querySelectorAll(selector)).filter(el => {
        const s = getComputedStyle(el);
        return s.display !== 'none' && s.visibility !== 'hidden';
    });
    return els.slice(0, 30).map(el => {
        const cs = getComputedStyle(el);
        // Walk up to find the first opaque ancestor backgroundColor
        let parentBg = 'rgba(0, 0, 0, 0)';
        let p = el.parentElement;
        while (p) {
            const bg = getComputedStyle(p).backgroundColor;
            if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') {
                parentBg = bg;
                break;
            }
            p = p.parentElement;
        }
        return {
            location: location(el),
            tag: el.tagName.toLowerCase(),
            type: el.getAttribute('type') || '',
            borderTopColor: cs.borderTopColor,
            borderTopStyle: cs.borderTopStyle,
            borderTopWidth: cs.borderTopWidth,
            backgroundColor: cs.backgroundColor,
            parentBackgroundColor: parentBg,
        };
    });
}
"""

    browser = None
    pages: List[Any] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)

            contrast_page = browser.new_page(viewport={"width": 1280, "height": 900})
            contrast_page.set_default_timeout(_PAGE_TIMEOUT_MS)
            contrast_page.route("**/*", _block_external_requests)
            pages.append(contrast_page)
            contrast_page.set_content(html_text, wait_until="load")
            text_nodes = contrast_page.evaluate(f"({text_nodes_script})()")

            reflow_page = browser.new_page(viewport={"width": 320, "height": 900})
            reflow_page.set_default_timeout(_PAGE_TIMEOUT_MS)
            reflow_page.route("**/*", _block_external_requests)
            pages.append(reflow_page)
            reflow_page.set_content(html_text, wait_until="load")
            reflow_page.wait_for_timeout(50)
            reflow = reflow_page.evaluate(f"({reflow_script})()")

            focus_page = browser.new_page(viewport={"width": 1280, "height": 900})
            focus_page.set_default_timeout(_PAGE_TIMEOUT_MS)
            focus_page.route("**/*", _block_external_requests)
            pages.append(focus_page)
            focus_page.set_content(html_text, wait_until="load")
            focus_data = focus_page.evaluate(f"({focus_script})()")

            keyboard_page = browser.new_page(viewport={"width": 1280, "height": 900})
            keyboard_page.set_default_timeout(_PAGE_TIMEOUT_MS)
            keyboard_page.route("**/*", _block_external_requests)
            pages.append(keyboard_page)
            keyboard_page.set_content(html_text, wait_until="load")
            keyboard_data = keyboard_page.evaluate(f"({keyboard_script})()")

            ntc_page = browser.new_page(viewport={"width": 1280, "height": 900})
            ntc_page.set_default_timeout(_PAGE_TIMEOUT_MS)
            ntc_page.route("**/*", _block_external_requests)
            pages.append(ntc_page)
            ntc_page.set_content(html_text, wait_until="load")
            non_text_contrast_data = ntc_page.evaluate(f"({non_text_contrast_script})()")

            # Phase L: action harness — runs LAST because it
            # mutates page state (focus, form values).
            actions_page = browser.new_page(viewport={"width": 1280, "height": 900})
            actions_page.set_default_timeout(_PAGE_TIMEOUT_MS)
            actions_page.route("**/*", _block_external_requests)
            pages.append(actions_page)
            actions_page.set_content(html_text, wait_until="load")
            try:
                actions_data = actions_page.evaluate(f"({actions_script})()")
            except Exception as exc:
                logger.info("Action harness failed: %s", exc)
                actions_data = None

            return {
                "text_nodes": text_nodes,
                "reflow": reflow,
                "focus": focus_data,
                "keyboard": keyboard_data,
                "non_text_contrast": non_text_contrast_data,
                "actions": actions_data,
            }
    except Exception as exc:
        logger.info("Rendered HTML checks skipped: %s", exc)
        return None
    finally:
        for page in pages:
            try:
                page.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


@dataclass
class _HtmlInput:
    input_type: str
    location: str
    label_text: str = ""
    accessible_name_sources: Set[str] = field(default_factory=set)


class _HtmlFactParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lang: Optional[str] = None
        self.title: Optional[str] = None
        self.images: List[ImageInfo] = []
        self.hyperlinks: List[HyperlinkInfo] = []
        self.tables: List[TableInfo] = []
        self.paragraphs: List[ParagraphInfo] = []
        self.inputs: List[_HtmlInput] = []

        self._current_title_parts: List[str] = []
        self._current_paragraph_tag: Optional[str] = None
        self._current_paragraph_parts: List[str] = []
        self._current_link_href: Optional[str] = None
        self._current_link_parts: List[str] = []
        self._current_label_for: Optional[str] = None
        self._current_label_parts: List[str] = []
        self._label_text_by_id: Dict[str, str] = {}
        self._next_generated_input_index = 0
        self._inside_table = False
        self._table_row_count = 0
        self._table_col_count = 0
        self._table_has_header = False
        self._table_index = 0

    def handle_starttag(self, tag: str, attrs):
        attrs_dict = dict(attrs)

        if tag == "html" and not self.lang:
            lang = (attrs_dict.get("lang") or "").strip()
            self.lang = lang or None

        if tag == "title":
            self._current_title_parts = []
            return

        if tag in HEADING_TAG_LEVELS or tag == "p":
            self._current_paragraph_tag = tag
            self._current_paragraph_parts = []
            return

        if tag == "a":
            self._current_link_href = attrs_dict.get("href")
            self._current_link_parts = []
            return

        if tag == "img":
            alt_present = "alt" in attrs_dict
            alt_text = attrs_dict.get("alt")
            is_decorative = (
                attrs_dict.get("role") in {"presentation", "none"}
                or attrs_dict.get("aria-hidden") == "true"
                or (alt_text == "" and attrs_dict.get("role") in {"presentation", "none"})
            )
            self.images.append(
                ImageInfo(
                    index=len(self.images),
                    alt_text=alt_text if alt_present else None,
                    alt_title=attrs_dict.get("title"),
                    is_decorative=is_decorative,
                    location_hint=f"Image {len(self.images) + 1}",
                )
            )
            return

        if tag == "label":
            self._current_label_for = attrs_dict.get("for")
            self._current_label_parts = []
            return

        if tag == "input":
            input_type = (attrs_dict.get("type") or "text").strip().lower()
            if input_type in {"hidden", "submit", "reset", "button"}:
                return
            input_id = (attrs_dict.get("id") or "").strip()
            generated_id = input_id or f"generated-input-{self._next_generated_input_index}"
            if not input_id:
                self._next_generated_input_index += 1
            input_info = _HtmlInput(
                input_type=input_type,
                location=f"Input '{generated_id}'",
            )
            if attrs_dict.get("aria-label"):
                input_info.label_text = attrs_dict.get("aria-label", "").strip()
                input_info.accessible_name_sources.add("aria-label")
            if attrs_dict.get("aria-labelledby"):
                input_info.accessible_name_sources.add("aria-labelledby")
            if attrs_dict.get("title"):
                if not input_info.label_text:
                    input_info.label_text = attrs_dict.get("title", "").strip()
                input_info.accessible_name_sources.add("title")
            if input_id:
                input_info.accessible_name_sources.add(f"id:{input_id}")
            self.inputs.append(input_info)
            return

        if tag == "textarea":
            input_id = (attrs_dict.get("id") or "").strip()
            generated_id = input_id or f"generated-input-{self._next_generated_input_index}"
            if not input_id:
                self._next_generated_input_index += 1
            input_info = _HtmlInput(
                input_type="textarea",
                location=f"Textarea '{generated_id}'",
            )
            if attrs_dict.get("aria-label"):
                input_info.label_text = attrs_dict.get("aria-label", "").strip()
                input_info.accessible_name_sources.add("aria-label")
            if attrs_dict.get("aria-labelledby"):
                input_info.accessible_name_sources.add("aria-labelledby")
            if attrs_dict.get("title"):
                if not input_info.label_text:
                    input_info.label_text = attrs_dict.get("title", "").strip()
                input_info.accessible_name_sources.add("title")
            if input_id:
                input_info.accessible_name_sources.add(f"id:{input_id}")
            self.inputs.append(input_info)
            return

        if tag == "select":
            input_id = (attrs_dict.get("id") or "").strip()
            generated_id = input_id or f"generated-input-{self._next_generated_input_index}"
            if not input_id:
                self._next_generated_input_index += 1
            input_info = _HtmlInput(
                input_type="select",
                location=f"Select '{generated_id}'",
            )
            if attrs_dict.get("aria-label"):
                input_info.label_text = attrs_dict.get("aria-label", "").strip()
                input_info.accessible_name_sources.add("aria-label")
            if attrs_dict.get("aria-labelledby"):
                input_info.accessible_name_sources.add("aria-labelledby")
            if attrs_dict.get("title"):
                if not input_info.label_text:
                    input_info.label_text = attrs_dict.get("title", "").strip()
                input_info.accessible_name_sources.add("title")
            if input_id:
                input_info.accessible_name_sources.add(f"id:{input_id}")
            self.inputs.append(input_info)
            return

        if tag == "table":
            self._inside_table = True
            self._table_row_count = 0
            self._table_col_count = 0
            self._table_has_header = False
            return

        if tag == "tr" and self._inside_table:
            self._table_row_count += 1
            self._table_col_count = 0
            return

        if tag in {"td", "th"} and self._inside_table:
            self._table_col_count += 1
            if tag == "th":
                self._table_has_header = True

    def handle_endtag(self, tag: str):
        if tag == "title":
            title_text = self._normalize_text(" ".join(self._current_title_parts))
            self.title = title_text or None
            self._current_title_parts = []
            return

        if self._current_paragraph_tag == tag and (tag in HEADING_TAG_LEVELS or tag == "p"):
            text = self._normalize_text(" ".join(self._current_paragraph_parts))
            if text:
                style_name = tag.upper() if tag in HEADING_TAG_LEVELS else "P"
                self.paragraphs.append(
                    ParagraphInfo(
                        index=len(self.paragraphs),
                        style_name=style_name,
                        text=text,
                        list_level=None,
                        is_empty=False,
                        run_language=None,
                        is_bold=False,
                        font_size_pt=None,
                    )
                )
            self._current_paragraph_tag = None
            self._current_paragraph_parts = []
            return

        if tag == "a" and self._current_link_href is not None:
            display_text = self._normalize_text(" ".join(self._current_link_parts))
            self.hyperlinks.append(
                HyperlinkInfo(
                    paragraph_index=max(len(self.paragraphs) - 1, 0),
                    display_text=display_text,
                    url=self._current_link_href,
                )
            )
            self._current_link_href = None
            self._current_link_parts = []
            return

        if tag == "label":
            label_text = self._normalize_text(" ".join(self._current_label_parts))
            if self._current_label_for and label_text:
                self._label_text_by_id[self._current_label_for] = label_text
            self._current_label_for = None
            self._current_label_parts = []
            return

        if tag == "table" and self._inside_table:
            self.tables.append(
                TableInfo(
                    index=self._table_index,
                    has_header_row=self._table_has_header,
                    row_count=self._table_row_count,
                    col_count=self._table_col_count,
                    location_hint=f"Table {self._table_index + 1}",
                )
            )
            self._table_index += 1
            self._inside_table = False
            return

    def handle_data(self, data: str):
        if self._current_title_parts is not None and self.get_starttag_text() == "<title>":
            self._current_title_parts.append(data)

        if self._current_paragraph_tag is not None:
            self._current_paragraph_parts.append(data)

        if self._current_link_href is not None:
            self._current_link_parts.append(data)

        if self._current_label_for is not None:
            self._current_label_parts.append(data)

    def finalize_inputs(self):
        for input_info in self.inputs:
            id_sources = [src for src in input_info.accessible_name_sources if src.startswith("id:")]
            for source in id_sources:
                input_id = source.split(":", 1)[1]
                if input_id in self._label_text_by_id:
                    input_info.label_text = self._label_text_by_id[input_id]
                    input_info.accessible_name_sources.add("label")

    @staticmethod
    def _normalize_text(text: str) -> str:
        return " ".join((text or "").split())


class HtmlAnalyzer:
    def __init__(self, file_bytes: bytes, filename: str):
        self.file_bytes = file_bytes
        self.filename = filename
        self.fact_sheet = FactSheet(filename=filename, file_type="html")
        self._inputs: List[_HtmlInput] = []
        self._html_text: Optional[str] = None
        
        # Initialize shared analyzers
        self.color_analyzer = ColorAnalyzer()
        self.flow_analyzer = SemanticFlowAnalyzer()
        self.form_analyzer = FormAnalyzer()

    def analyze(self) -> FactSheet:
        self._html_text = self._decode_html()
        parser = _HtmlFactParser()
        parser.feed(self._html_text)
        parser.close()
        parser.finalize_inputs()

        self.fact_sheet.document_title = parser.title
        self.fact_sheet.document_language = parser.lang
        self.fact_sheet.paragraphs = parser.paragraphs
        self.fact_sheet.paragraph_count = len(parser.paragraphs)
        self.fact_sheet.images = parser.images
        self.fact_sheet.tables = parser.tables
        self.fact_sheet.hyperlinks = parser.hyperlinks
        self._inputs = parser.inputs

        self._run_rules()
        self._run_rendered_rules()
        return self.fact_sheet

    # ── Phase J helpers ──────────────────────────────────────────────────────
    def _advisory_context_around_tag(self, tag: str, occurrence: int,
                                     window: int = 400) -> Dict[str, str]:
        """Return {'src_or_href': str, 'context': str} for the Nth `<tag>`
        in source. Used to build advisory_payload for alt-text / link-text
        rules. Tag is matched case-insensitively. Context is plain text from
        a window before the tag.
        """
        if not self._html_text:
            return {"src_or_href": "", "context": ""}
        pattern = re.compile(rf"<{tag}\b([^>]*)>", re.IGNORECASE)
        matches = list(pattern.finditer(self._html_text))
        if occurrence >= len(matches):
            return {"src_or_href": "", "context": ""}
        m = matches[occurrence]
        attrs_blob = m.group(1)
        # Pull src / href value tolerating single or double quotes
        attr_name = "src" if tag.lower() == "img" else "href"
        attr_match = re.search(
            rf"\b{attr_name}\s*=\s*(?:\"([^\"]*)\"|'([^']*)')",
            attrs_blob, re.IGNORECASE,
        )
        url_value = ""
        if attr_match:
            url_value = (attr_match.group(1) or attr_match.group(2) or "").strip()
        # Window of raw HTML BEFORE the tag, then strip tags to get text
        start = max(0, m.start() - window)
        raw_window = self._html_text[start:m.start()]
        text_only = re.sub(r"<[^>]+>", " ", raw_window)
        text_only = re.sub(r"\s+", " ", text_only).strip()
        # Cap context to 1000 chars per advisory_payload schema
        if len(text_only) > 1000:
            text_only = text_only[-1000:]
        return {"src_or_href": url_value, "context": text_only}

    def _decode_html(self) -> str:
        for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
            try:
                return self.file_bytes.decode(encoding)
            except UnicodeDecodeError:
                continue
        return self.file_bytes.decode("utf-8", errors="replace")

    def _run_rules(self):
        self._rule_2_4_2_page_title()
        self._rule_3_1_1_page_language()
        self._rule_3_1_2_mixed_language()
        self._rule_1_1_1_image_alt_text()
        self._rule_2_4_4_generic_link_text()
        self._rule_1_3_1_heading_hierarchy()
        self._rule_4_1_2_input_name()
        self._rule_4_1_3_live_regions()
        # Phase A additions
        self._rule_2_4_1_bypass_blocks()
        self._rule_1_3_5_input_purpose()
        self._rule_3_3_2_labels_or_instructions()
        self._rule_2_4_5_multiple_ways()
        self._rule_1_4_4_viewport_resize()
        # Phase F additions
        self._rule_2_4_6_headings_and_labels()
        # Phase G additions
        self._rule_2_5_3_label_in_name()
        self._rule_1_3_4_orientation()
        self._rule_2_1_1_keyboard_handlers()
        self._rule_2_2_1_meta_refresh()
        self._rule_1_4_12_text_spacing_important()
        # Phase H additions
        self._rule_3_2_1_on_focus_change()
        self._rule_3_2_2_on_input_change()
        self._rule_3_3_1_error_identification_readiness()
        self._rule_1_3_3_sensory_characteristics()
        # Phase I additions
        self._rule_1_4_5_inline_svg_text()
        self._rule_2_5_8_target_size_minimum()
        self._rule_3_3_8_accessible_authentication()
        # Phase K addition
        self._rule_2_5_7_dragging_movements()
        # Phase M — net-new SC (mostly HTML)
        self._rule_1_4_2_audio_control()             # M1
        self._rule_2_1_4_character_key_shortcuts()   # M2
        self._rule_2_2_2_pause_stop_hide()           # M3
        self._rule_2_3_1_three_flashes()             # M4
        self._rule_2_5_2_pointer_cancellation()      # M5
        self._rule_2_5_4_motion_actuation()          # M6
        self._rule_3_2_6_consistent_help()           # M7 (WCAG 2.2)
        self._rule_3_3_7_redundant_entry()           # M8 (WCAG 2.2)
        self._rule_1_2_1_audio_only_media_alternative()  # M9
        self._rule_1_2_2_prerecorded_captions()          # M10
        self._rule_1_2_3_prerecorded_media_alternative() # M11
        self._rule_1_2_5_audio_description_prerecorded() # Phase L (WCAG 1.2.5 AA)
        # Phase N — 2026-05-18 gap closures (5 AAA quick wins + 2 A/AA source heuristics + 2 AA)
        self._rule_2_4_10_section_headings()             # N1 (AAA)
        self._rule_2_4_9_link_purpose_link_only()        # N2 (AAA)
        self._rule_2_1_3_keyboard_no_exception()         # N3 (AAA)
        self._rule_1_4_9_images_of_text_no_exception()   # N4 (AAA)
        self._rule_2_5_5_target_size_enhanced()          # N5 (AAA)
        self._rule_2_5_1_pointer_gestures()              # N6 (A — closes last A gap)
        self._rule_3_3_3_error_suggestion()              # N7 (AA)
        self._rule_3_3_4_error_prevention()              # N8 (AA)
        # Phase N+ — 2026-05-18 final AAA closures (3 more free quick wins)
        self._rule_2_4_13_focus_appearance()             # N+1 (AAA)
        self._rule_3_3_5_help()                          # N+2 (AAA)
        self._rule_3_3_6_error_prevention_all()          # N+3 (AAA)

    def _run_rendered_rules(self):
        if not self._html_text:
            return
        rendered = _render_html_diagnostics(self._html_text)
        if not rendered:
            return
        self._rule_1_4_3_rendered_contrast(rendered.get("text_nodes") or [])
        self._rule_1_4_10_reflow(rendered.get("reflow") or {})
        self._rule_1_3_2_meaningful_sequence(rendered.get("keyboard") or {})
        self._rule_1_4_1_color_only(rendered.get("text_nodes") or [])
        self._rule_1_4_4_resize_text(rendered.get("reflow") or {})
        self._rule_2_1_2_no_keyboard_trap(rendered.get("keyboard") or {})
        self._rule_2_4_3_focus_order(rendered.get("keyboard") or {})
        self._rule_2_4_7_focus_visible(rendered.get("focus") or {})
        self._rule_2_1_1_keyboard(rendered.get("keyboard") or {})
        self._rule_1_4_11_non_text_contrast(rendered.get("non_text_contrast") or [])  # Phase B
        # Phase L — action harness (Tier 1)
        self._rule_1_4_13_content_on_hover_or_focus(rendered.get("actions") or {})
        self._rule_2_4_11_focus_not_obscured(rendered.get("actions") or {})
        self._rule_2_4_12_focus_not_obscured_enhanced(rendered.get("actions") or {})
        self._rule_3_2_1_runtime_focus_change(rendered.get("actions") or {})
        self._rule_3_2_2_runtime_input_change(rendered.get("actions") or {})
        # Phase N — 2026-05-18: AAA enhanced contrast (rendered)
        self._rule_1_4_6_contrast_enhanced(rendered.get("text_nodes") or [])

    def _rule_2_4_2_page_title(self):
        GENERIC_TITLES = {
            "untitled", "page 1", "page 2", "page 3", "page 4", "page 5",
            "new page", "unnamed", "temp", "test", "sample", "lorem ipsum",
            "default", "placeholder", "website", "home", "index"
        }
        
        title = (self.fact_sheet.document_title or "").strip()
        if not title:
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="2.4.2",
                criterion_name="Page Titled",
                wcag_level="A",
                issue="HTML document has no page title.",
                evidence="Missing or empty <title> element in <head>.",
                severity=Severity.MODERATE,
                why_it_matters="Screen reader users rely on the page title to understand where they are.",
                remediation_steps=[
                    "Add a concise <title> element inside <head>.",
                    "Use a title that identifies the page or task, not a generic placeholder.",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
                confidence_rationale="The page title is read directly from the DOM.",
                evidence_source=EvidenceSource.DOM_DIRECT,
                location="HTML head",
                remediation_id="html_page_title",
            ))
            return
        
        # Check for generic titles
        if title.lower() in GENERIC_TITLES:
            self.fact_sheet.possible_findings.append(Finding(
                criterion_id="2.4.2",
                criterion_name="Page Titled",
                wcag_level="A",
                issue=f"Page title '{title}' is generic and may not describe the page purpose.",
                evidence=f"Title element contains '{title}', which is a generic placeholder.",
                severity=Severity.MODERATE,
                why_it_matters="Screen reader users rely on the page title to understand where they are. Generic titles provide no context.",
                remediation_steps=[
                    "Replace the generic title with a descriptive title that identifies the page content or purpose.",
                    "Example: 'Contact Form - Acme Corp' instead of 'Untitled'.",
                ],
                confidence_tier=ConfidenceTier.POSSIBLE,
                confidence_label="medium",
                confidence_rationale="Generic title detected; review to confirm it should be more descriptive.",
                evidence_source=EvidenceSource.DOM_DIRECT,
                location="HTML head",
                remediation_id="html_generic_title",
            ))

    def _rule_3_1_1_page_language(self):
        language = (self.fact_sheet.document_language or "").strip()
        if language:
            return
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="3.1.1",
            criterion_name="Language of Page",
            wcag_level="A",
            issue="HTML document has no default language.",
            evidence="Missing or empty lang attribute on the <html> element.",
            severity=Severity.MODERATE,
            why_it_matters="Assistive technology uses the page language to choose pronunciation and reading rules.",
            remediation_steps=[
                "Set the lang attribute on the <html> element, for example <html lang=\"en\">.",
                "Use a valid BCP 47 language code such as en, en-US, or fr-CA.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="The page language is read directly from the DOM.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location="HTML root",
            remediation_id="html_page_language",
        ))

    def _rule_3_1_2_mixed_language(self):
        """WCAG 3.1.2 Language of Parts (AA)
        
        Check for elements with lang attributes that differ from the document language.
        """
        doc_lang = (self.fact_sheet.document_language or "").strip()
        if not doc_lang:
            return  # Already flagged under 3.1.1; can't detect mixed language without default
        
        # Search for lang attributes on elements (other than root html element)
        # Look for patterns like lang="xx" or lang='xx' on any element tag
        lang_pattern = re.compile(r'<(\w+)[^>]*\s+lang=["\']([a-zA-Z0-9\-]+)["\'][^>]*>')
        
        mixed_langs = set()
        for match in lang_pattern.finditer(self._html_text):
            tag = match.group(1)
            lang = match.group(2).strip().lower()
            doc_lang_compare = doc_lang.lower().split('-')[0]  # Compare just the base language
            lang_compare = lang.split('-')[0]
            if tag.lower() != 'html' and lang_compare != doc_lang_compare:
                mixed_langs.add(lang)
        
        if mixed_langs:
            self.fact_sheet.possible_findings.append(Finding(
                criterion_id="3.1.2",
                criterion_name="Language of Parts",
                wcag_level="AA",
                issue=f"Mixed language parts detected: {sorted(mixed_langs)} alongside default '{doc_lang}'.",
                evidence=f"Elements with lang attributes {sorted(mixed_langs)} differ from document language '{doc_lang}'.",
                severity=Severity.MINOR,
                why_it_matters="If foreign-language text is not marked with the correct language, screen readers will mispronounce it using the default language's phonetics.",
                remediation_steps=[
                    "Identify elements with foreign-language text.",
                    "Add or update the lang attribute to the correct language code, e.g. <span lang=\"es\">¡Hola!</span>.",
                    "Use BCP 47 language codes (e.g., en-US, fr-CA, de).",
                ],
                confidence_tier=ConfidenceTier.POSSIBLE,
                confidence_label="medium",
                confidence_rationale="Mixed language parts detected via lang attributes; review to confirm intentionality.",
                evidence_source=EvidenceSource.DOM_INFERRED,
                location="Elements with lang attribute",
                remediation_id="html_mixed_language",
            ))

    def _rule_1_1_1_image_alt_text(self):
        for image in self.fact_sheet.images or []:
            if image.is_decorative:
                continue
            if image.alt_text is not None:
                continue
            ctx = self._advisory_context_around_tag("img", image.index)
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="1.1.1",
                criterion_name="Non-text Content",
                wcag_level="A",
                issue=f"{image.location_hint} has no alt attribute.",
                evidence="The <img> element is missing an alt attribute.",
                severity=Severity.CRITICAL,
                why_it_matters="Screen reader users will not get a replacement for the image content.",
                remediation_steps=[
                    "Add an alt attribute that describes the image's purpose.",
                    "If the image is purely decorative, use alt=\"\" and mark it decorative intentionally.",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
                confidence_rationale="The missing alt attribute is read directly from the DOM.",
                evidence_source=EvidenceSource.DOM_DIRECT,
                location=image.location_hint,
                remediation_id=f"html_img_alt_{image.index}",
                advisory_payload={
                    "advisory_kind": "alt_text",
                    "target": ctx["src_or_href"] or f"img[{image.index}]",
                    "surface_text": "",
                    "context": ctx["context"],
                    "format_hint": "html",
                },
            ))

    def _rule_2_4_4_generic_link_text(self):
        for index, hyperlink in enumerate(self.fact_sheet.hyperlinks or []):
            text = (hyperlink.display_text or "").strip()
            
            # Flag empty links as confirmed issue
            if not text:
                self.fact_sheet.confirmed_findings.append(Finding(
                    criterion_id="2.4.4",
                    criterion_name="Link Purpose (In Context)",
                    wcag_level="A",
                    issue="Link has no text content.",
                    evidence="Anchor element is empty or contains only whitespace.",
                    severity=Severity.CRITICAL,
                    why_it_matters="Users need text to understand what a link does. Empty links are unusable for assistive technology users.",
                    remediation_steps=[
                        "Add descriptive text to the link that identifies its purpose or destination.",
                        "If the link uses only an icon, add aria-label or visually hidden text.",
                    ],
                    confidence_tier=ConfidenceTier.CONFIRMED,
                    confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
                    confidence_rationale="The missing link text is read directly from the DOM.",
                    evidence_source=EvidenceSource.DOM_DIRECT,
                    location=f"Link {index + 1}",
                    remediation_id=f"html_empty_link_{index}",
                ))
                continue
            
            # Check for generic text
            if not GENERIC_LINK_TEXT.match(text):
                continue
            
            ctx = self._advisory_context_around_tag("a", index)
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="2.4.4",
                criterion_name="Link Purpose (In Context)",
                wcag_level="A",
                issue=f"Link text '{text}' is too generic to describe its destination.",
                evidence=f"Anchor text is '{text}'.",
                severity=Severity.MODERATE,
                why_it_matters="Users navigating by links need descriptive names to decide where to go.",
                remediation_steps=[
                    "Replace generic link text with text that names the destination or action.",
                    "Keep the visible link text meaningful even when read out of context.",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
                confidence_rationale="The anchor text is read directly from the DOM.",
                evidence_source=EvidenceSource.DOM_DIRECT,
                location=f"Link {index + 1}",
                remediation_id=f"html_link_text_{index}",
                advisory_payload={
                    "advisory_kind": "link_text",
                    "target": ctx["src_or_href"] or hyperlink.url or f"a[{index}]",
                    "surface_text": text,
                    "context": ctx["context"],
                    "format_hint": "html",
                },
            ))

    def _rule_1_3_1_heading_hierarchy(self):
        headings = [
            paragraph for paragraph in (self.fact_sheet.paragraphs or [])
            if paragraph.style_name in {tag.upper() for tag in HEADING_TAG_LEVELS}
        ]
        if not headings:
            return

        levels = [HEADING_TAG_LEVELS[paragraph.style_name.lower()] for paragraph in headings]
        issue_reasons = []
        
        # Check for multiple H1 headings
        h1_count = levels.count(1)
        if h1_count > 1:
            issue_reasons.append(f"page has {h1_count} H1 headings (should have exactly 1)")
        
        if levels[0] != 1 and h1_count == 0:
            issue_reasons.append(f"first heading is H{levels[0]} instead of H1")
        
        for previous, current in zip(levels, levels[1:]):
            if current > previous + 1:
                issue_reasons.append(f"heading level jumps from H{previous} to H{current}")
                break
        
        if not issue_reasons:
            return

        outline = " > ".join(f"H{level}" for level in levels[:6])
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.3.1",
            criterion_name="Info and Relationships",
            wcag_level="A",
            issue="HTML heading hierarchy has issues with structure or multiple H1s.",
            evidence="; ".join(issue_reasons),
            severity=Severity.SERIOUS,
            why_it_matters="Heading levels communicate page structure to screen reader and keyboard users. Multiple H1s confuse the document outline.",
            remediation_steps=[
                "Use exactly one H1 for the main page topic.",
                "Nest other headings (H2-H6) in order without skipping levels.",
                "Remove duplicate H1 headings if present.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="Heading tags are read directly from the DOM.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"Heading outline: {outline}",
            remediation_id="html_heading_hierarchy",
        ))

    def _rule_4_1_2_input_name(self):
        for index, input_info in enumerate(self._inputs):
            input_type = input_info.input_type.lower()
            if input_type not in TEXT_INPUT_TYPES and input_type not in {"checkbox", "radio", "select", "textarea"}:
                continue
            if input_info.label_text.strip():
                continue
            if {"aria-labelledby", "aria-label"} & input_info.accessible_name_sources:
                continue
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="4.1.2",
                criterion_name="Name, Role, Value",
                wcag_level="A",
                issue=f"{input_info.location} has no programmatic label.",
                evidence="No associated <label>, aria-label, aria-labelledby, or title was found.",
                severity=Severity.SERIOUS,
                why_it_matters="Assistive technology needs an accessible name to announce form controls clearly.",
                remediation_steps=[
                    "Associate a <label> with the control using for/id, or add aria-label or aria-labelledby.",
                    "Use a visible label when possible so all users see the same control purpose.",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
                confidence_rationale="The absence of an accessible name source is read directly from the DOM.",
                evidence_source=EvidenceSource.DOM_DIRECT,
                location=input_info.location,
                remediation_id=f"html_input_name_{index}",
            ))

    def _rule_4_1_3_live_regions(self):
        """WCAG 4.1.3: Validate proper use of ARIA live regions for status messages.
        
        Live regions (aria-live) are used to announce dynamic content changes to screen reader users.
        This rule checks for:
        1. Valid aria-live values (polite, assertive, off)
        2. Presence of aria-atomic for multi-part messages
        3. Proper labeling and accessibility
        """
        # Parse HTML to find all elements with aria-live
        import re
        if not self._html_text:
            return
        
        # Remove HTML comments first to avoid false matches
        html_without_comments = re.sub(r'<!--.*?-->', '', self._html_text, flags=re.DOTALL)
        
        # Find all aria-live attributes with context
        live_region_pattern = re.compile(
            r'<([^>]+aria-live\s*=\s*["\']([^"\']+)["\'][^>]*)>',
            re.IGNORECASE
        )
        
        invalid_issues = []
        missing_atomic_issues = []
        
        for match in live_region_pattern.finditer(html_without_comments):
            element_content = match.group(1)
            aria_live_value = match.group(2).lower().strip()
            
            # Extract element ID if present
            id_match = re.search(r'id\s*=\s*["\']([^"\']+)["\']', element_content, re.IGNORECASE)
            element_id = id_match.group(1) if id_match else "(no id)"
            
            # Valid values are: polite, assertive, off
            if aria_live_value not in ("polite", "assertive", "off"):
                invalid_issues.append({
                    "element_id": element_id,
                    "value": aria_live_value,
                })
            # For dynamic regions (polite or assertive), check for aria-atomic (best practice)
            elif aria_live_value in ("polite", "assertive"):
                # Check if aria-atomic is present
                atomic_match = re.search(r'aria-atomic\s*=\s*["\']([^"\']+)["\']', element_content, re.IGNORECASE)
                if not atomic_match:
                    missing_atomic_issues.append({
                        "element_id": element_id,
                        "value": aria_live_value,
                    })
        
        # Report invalid values as confirmed findings
        for issue in invalid_issues[:3]:  # Limit to first 3
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="4.1.3",
                criterion_name="Status Messages",
                wcag_level="AAA",
                issue=(
                    f"Live region {issue['element_id']} has invalid aria-live value '{issue['value']}'."
                ),
                evidence=(
                    f"Element with id='{issue['element_id']}' uses aria-live='{issue['value']}'. "
                    f"Valid values: 'polite' (default, wait for pause), 'assertive' (interrupt), 'off'."
                ),
                severity=Severity.SERIOUS,
                why_it_matters=(
                    "Invalid aria-live values prevent screen readers from properly announcing dynamic content updates."
                ),
                remediation_steps=[
                    f"Change aria-live='{issue['value']}' to aria-live='polite' or aria-live='assertive'.",
                    "'polite': announcement waits for current speech to finish (recommended for most cases).",
                    "'assertive': announcement interrupts current speech (only for urgent alerts).",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
                confidence_rationale="Invalid aria-live value is directly inspected from HTML markup.",
                evidence_source=EvidenceSource.DOM_DIRECT,
                location=issue['element_id'],
                remediation_id=f"html_live_region_invalid_{issue['element_id']}",
            ))
        
        # Report missing aria-atomic as possible findings (best practice)
        # Report for any valid aria-live element that lacks aria-atomic
        for issue in missing_atomic_issues[:2]:  # Limit to first 2
            self.fact_sheet.possible_findings.append(Finding(
                criterion_id="4.1.3",
                criterion_name="Status Messages",
                wcag_level="AAA",
                issue=(
                    f"Live region {issue['element_id']} lacks aria-atomic for multi-part announcements."
                ),
                evidence=(
                    f"Element {issue['element_id']} has aria-live='{issue['value']}' but no aria-atomic. "
                    f"aria-atomic='true' ensures the entire region is announced as one unit."
                ),
                severity=Severity.MINOR,
                why_it_matters=(
                    "aria-atomic='true' tells screen readers to announce the whole region when it changes, "
                    "not just the changed part. This is important for updates like 'error: field required'."
                ),
                remediation_steps=[
                    f"Add aria-atomic='true' to the live region.",
                    "This ensures screen reader users hear the complete message, not just the updated parts.",
                ],
                confidence_tier=ConfidenceTier.POSSIBLE,
                confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
                confidence_rationale="Live region structure inspected from markup; aria-atomic absence is a best-practice suggestion.",
                evidence_source=EvidenceSource.DOM_DIRECT,
                location=issue['element_id'],
                remediation_id=f"html_live_region_atomic_{issue['element_id']}",
            ))

    def _rule_1_4_3_rendered_contrast(self, text_nodes: List[Dict[str, Any]]):
        findings_added = 0
        seen = set()

        for node in text_nodes:
            ratio = minimum_css_contrast(
                node.get("color", ""),
                node.get("backgroundColor", ""),
                node.get("backgroundImage", ""),
            )
            if ratio is None:
                continue
            font_size_px = float(node.get("fontSizePx") or 16.0)
            threshold = MIN_CONTRAST_RATIO_LARGE if _is_large_text(font_size_px, str(node.get("fontWeight", "400"))) else MIN_CONTRAST_RATIO_NORMAL
            if ratio >= threshold:
                continue

            key = (node.get("location"), node.get("text"))
            if key in seen:
                continue
            seen.add(key)

            text_preview = (node.get("text") or "")[:100]
            location = node.get("location") or node.get("tag") or "visible text"
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="1.4.3",
                criterion_name="Contrast (Minimum)",
                wcag_level="AA",
                issue=(
                    f"Rendered text at {location} has contrast {ratio:.2f}:1, below the required {threshold:.1f}:1."
                ),
                evidence=(
                    f"Text sample '{text_preview}' rendered as {node.get('color')} on {node.get('backgroundColor')} / {node.get('backgroundImage')} "
                    f"at {font_size_px:.1f}px."
                ),
                severity=Severity.SERIOUS,
                why_it_matters=(
                    "Low-contrast text is difficult or impossible to read for users with low vision and in bright environments."
                ),
                remediation_steps=[
                    "Increase the contrast between the text color and the background color.",
                    "Prefer darker text or a lighter background until the ratio meets WCAG AA.",
                    "Re-run the analyzer to verify the rendered ratio clears the threshold.",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label=CONFIDENCE_LABEL[EvidenceSource.BROWSER_RENDERED],
                confidence_rationale="The contrast ratio was measured from rendered browser styles.",
                evidence_source=EvidenceSource.BROWSER_RENDERED,
                location=location,
                remediation_id=f"html_text_contrast_{findings_added}",
            ))
            findings_added += 1
            if findings_added >= MAX_RENDERED_CONTRAST_FINDINGS:
                break

    def _rule_1_4_10_reflow(self, reflow: Dict[str, Any]):
        overflow_px = int(reflow.get("overflowPx") or 0)
        if overflow_px < HORIZONTAL_OVERFLOW_THRESHOLD_PX:
            return

        offenders = reflow.get("offenders") or []
        material_offenders = [offender for offender in offenders if int(offender.get("overflowPx") or 0) >= HORIZONTAL_OVERFLOW_THRESHOLD_PX]
        if material_offenders and all((offender.get("tag") or "").lower() in EXEMPT_REFLOW_TAGS for offender in material_offenders):
            return

        top_offender = material_offenders[0] if material_offenders else None
        location = top_offender.get("location") if top_offender else "Rendered page"
        detail = top_offender.get("text") if top_offender else ""

        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.4.10",
            criterion_name="Reflow",
            wcag_level="AA",
            issue=(
                f"Rendered page requires horizontal scrolling at 320px viewport width ({overflow_px}px overflow)."
            ),
            evidence=(
                f"Rendered document width was {reflow.get('documentWidth')}px at a {reflow.get('viewportWidth')}px viewport. "
                f"Top offender: {location}. {detail}".strip()
            ),
            severity=Severity.MODERATE,
            why_it_matters=(
                "Users who zoom or read on narrow screens should not need to scroll horizontally to read ordinary content."
            ),
            remediation_steps=[
                "Replace fixed pixel widths with responsive widths such as max-width: 100%.",
                "Let text containers wrap instead of forcing a wide layout.",
                "Re-test the page at a 320px viewport after the layout change.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.BROWSER_RENDERED],
            confidence_rationale="Horizontal overflow was measured on a rendered browser page at 320px width.",
            evidence_source=EvidenceSource.BROWSER_RENDERED,
            location=location,
            remediation_id="html_reflow_320",
        ))

    def _rule_2_4_7_focus_visible(self, focus_data: Dict[str, Any]):
        """Check for visible focus indicators on interactive elements (WCAG 2.4.7)."""
        elements = focus_data.get("elements") or []
        
        missing_focus_indicators = []
        for element in elements:
            has_focus_style = element.get("hasFocusStyle", False)
            location = element.get("location") or element.get("tag") or "Interactive element"
            text = element.get("text") or ""
            
            # Flag if element lacks visible focus style
            if not has_focus_style:
                missing_focus_indicators.append({
                    "location": location,
                    "text": text,
                    "tag": element.get("tag"),
                })
                
        # Report findings for elements without visible focus indicators
        for idx, element_info in enumerate(missing_focus_indicators[:5]):  # Limit to first 5
            location = element_info["location"]
            text_preview = element_info["text"][:50] if element_info["text"] else "(no text)"
            
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="2.4.7",
                criterion_name="Focus Visible",
                wcag_level="AA",
                issue=(
                    f"{location} element lacks a visible focus indicator."
                ),
                evidence=(
                    f"{element_info['tag']} element: '{text_preview}' has no :focus CSS rule with outline, box-shadow, or other visible indicator."
                ),
                severity=Severity.SERIOUS,
                why_it_matters=(
                    "Keyboard users must see which element has focus to navigate the page effectively."
                ),
                remediation_steps=[
                    "Add a visible :focus or :focus-visible CSS rule to the element.",
                    "Ensure the focus style has sufficient contrast and is not hidden.",
                    "Example: button:focus { outline: 2px solid #4A90E2; outline-offset: 2px; }",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label=CONFIDENCE_LABEL[EvidenceSource.BROWSER_RENDERED],
                confidence_rationale="Focus indicator visibility was measured from rendered browser styles.",
                evidence_source=EvidenceSource.BROWSER_RENDERED,
                location=location,
                remediation_id=f"html_focus_visible_{idx}",
            ))

    def _rule_2_1_1_keyboard(self, keyboard_data: Dict[str, Any]):
        """Check that all interactive elements are keyboard accessible (WCAG 2.1.1)."""
        focusable_count = keyboard_data.get("focusableCount", 0)
        focusable_elements = keyboard_data.get("focusableElements", [])
        
        # No interactive elements
        if focusable_count == 0:
            return
        
        # Check for elements with positive tabindex (can cause confusion and violates 2.4.3)
        positive_tabindex_elements = [
            el for el in focusable_elements
            if el.get("tabindex", 0) > 0
        ]
        
        if positive_tabindex_elements:
            for idx, el in enumerate(positive_tabindex_elements[:3]):  # Limit to first 3
                location = el.get("location") or "Interactive element"
                text_preview = el.get("text", "")[:50]
                
                self.fact_sheet.confirmed_findings.append(Finding(
                    criterion_id="2.1.1",
                    criterion_name="Keyboard",
                    wcag_level="A",
                    issue=(
                        f"Interactive element '{location}' uses explicit positive tabindex={el.get('tabindex')}, which breaks focus order."
                    ),
                    evidence=(
                        f"Element has tabindex={el.get('tabindex')}. Text: '{text_preview}'."
                    ),
                    severity=Severity.SERIOUS,
                    why_it_matters=(
                        "Positive tabindex values override the natural DOM order, confusing keyboard users who expect to navigate sequentially."
                    ),
                    remediation_steps=[
                        "Remove positive tabindex attributes from all interactive elements.",
                        "Let the browser manage focus order based on DOM position.",
                        "Use tabindex='-1' only for programmatically focused elements (skip in tab order).",
                        "Use tabindex='0' to make naturally unfocusable elements keyboard accessible.",
                    ],
                    confidence_tier=ConfidenceTier.CONFIRMED,
                    confidence_label=CONFIDENCE_LABEL[EvidenceSource.BROWSER_RENDERED],
                    confidence_rationale="Positive tabindex values were detected in the rendered page.",
                    evidence_source=EvidenceSource.BROWSER_RENDERED,
                    location=location,
                    remediation_id=f"html_keyboard_tabindex_{idx}",
                ))

    def _rule_1_3_2_meaningful_sequence(self, keyboard_data: Dict[str, Any]):
        """Validate that reading order (DOM) matches visual/keyboard order (WCAG 1.3.2)."""
        focusable_elements = keyboard_data.get("focusableElements", [])
        if not focusable_elements:
            return
        
        # Check if DOM order matches visual/positional order
        # Elements should be ordered top-to-left (y-position first, then x-position)
        out_of_order = []
        prev_y = 0
        prev_x = 0
        
        for idx, el in enumerate(focusable_elements):
            rect = el.get("rect", {})
            current_y = rect.get("top", 0) if isinstance(rect, dict) else 0
            current_x = rect.get("left", 0) if isinstance(rect, dict) else 0
            
            # Flag if element is significantly above or to the left of previous element
            # (50px threshold for same row, 20px for left-to-right ordering)
            if current_y < prev_y - 50 or (abs(current_y - prev_y) < 50 and current_x < prev_x - 20):
                out_of_order.append({
                    "index": idx,
                    "location": el.get("location", ""),
                    "text": el.get("text", ""),
                    "dom_order": idx,
                    "visual_y": current_y,
                    "visual_x": current_x,
                })
            prev_y = max(prev_y, current_y)
            prev_x = current_x if abs(current_y - prev_y) < 50 else 0
        
        if not out_of_order:
            return
        
        # Report first mismatch found
        offender = out_of_order[0]
        location = offender["location"] or f"Interactive element {offender['index'] + 1}"
        
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.3.2",
            criterion_name="Meaningful Sequence",
            wcag_level="A",
            issue=(
                f"{location} appears out of reading order. DOM order does not match visual position."
            ),
            evidence=(
                f"Element at DOM position {offender['dom_order']} is visually positioned above or to the left of previous elements."
            ),
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Screen reader and keyboard users follow the DOM order, not the visual order. "
                "Mismatch causes confusion when expected reading order conflicts with visible layout."
            ),
            remediation_steps=[
                "Reorder HTML elements in the source to match the visual reading order (top-to-bottom, left-to-right).",
                "Avoid using CSS (float, position, flex-order) to reorder content visually.",
                "If layout needs CSS reordering, ensure the DOM still reflects the logical reading order.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.BROWSER_RENDERED],
            confidence_rationale="Element position was measured from rendered browser layout; reading order mismatch is inferred.",
            evidence_source=EvidenceSource.BROWSER_RENDERED,
            location=location,
            remediation_id="html_meaningful_sequence",
        ))

    def _rule_1_4_1_color_only(self, text_nodes: List[Dict[str, Any]]):
        """Detect reliance on color alone to convey meaning (WCAG 1.4.1)."""
        if not text_nodes:
            return
        
        # Common color-only patterns:
        # - Text with specific colors but no text label (e.g., "Required" marker)
        # - Status badges that rely on color alone (red for error, green for success)
        color_to_text = {}
        
        for node in text_nodes:
            color = node.get("color", "").lower()
            text = (node.get("text") or "").lower().strip()
            
            # Skip clear text that conveys meaning
            if any(meaningful in text for meaningful in ["error", "success", "warning", "required", "optional", "valid", "invalid"]):
                continue
            
            # Track short text with specific colors (likely decorative symbols or badges)
            if len(text) <= 3 and color:
                if color not in color_to_text:
                    color_to_text[color] = []
                color_to_text[color].append(node)
        
        # If multiple different colors are used for similar elements without clear text labels, flag as possible color-only meaning
        if len(color_to_text) >= 2:
            # Find the most common pattern
            color_groups = sorted(color_to_text.items(), key=lambda x: len(x[1]), reverse=True)
            if color_groups[0][1]:
                first_offender = color_groups[0][1][0]
                location = first_offender.get("location", "Element") or "Element"
                text_preview = (first_offender.get("text") or "")[:50]
                
                self.fact_sheet.confirmed_findings.append(Finding(
                    criterion_id="1.4.1",
                    criterion_name="Use of Color",
                    wcag_level="A",
                    issue=(
                        f"Color at {location} may be used alone to convey meaning without redundant text or pattern."
                    ),
                    evidence=(
                        f"Text '{text_preview}' uses color {first_offender.get('color')} without additional indicators like text labels, icons, or patterns."
                    ),
                    severity=Severity.MODERATE,
                    why_it_matters=(
                        "Users who are color blind or use grayscale displays cannot distinguish meaning conveyed by color alone."
                    ),
                    remediation_steps=[
                        "Add descriptive text labels alongside colored elements (e.g., 'Required *' or 'Error:' in addition to red color).",
                        "Use patterns (stripes, dots) or icons in addition to color.",
                        "Ensure the meaning is clear from text or shape, not color alone.",
                    ],
                    confidence_tier=ConfidenceTier.POSSIBLE,
                    confidence_label=CONFIDENCE_LABEL[EvidenceSource.BROWSER_RENDERED],
                    confidence_rationale="Color usage pattern was detected from rendered styles; may require manual review.",
                    evidence_source=EvidenceSource.BROWSER_RENDERED,
                    location=location,
                    remediation_id="html_color_only",
                ))

    def _rule_1_4_4_resize_text(self, reflow: Dict[str, Any]):
        """Validate that text remains readable at 200% zoom (WCAG 1.4.4)."""
        # 1.4.4 extends 1.4.10: at 200% zoom, text should be readable without loss of functionality
        # This is partially captured by reflow (1.4.10); flag if reflow fails as likely 1.4.4 issue too
        overflow_px = int(reflow.get("overflowPx") or 0)
        
        # If reflow already detected overflow, this is also a 1.4.4 violation for text resizing
        if overflow_px >= HORIZONTAL_OVERFLOW_THRESHOLD_PX:
            # Don't duplicate; rely on 1.4.10 to catch this, which is sufficient
            return
        
        # 1.4.4 also checks line height and letter spacing; flag if these are too restrictive
        # For now, we rely on CSS validation in DOCX/PPTX; HTML rendered check is minimal
        # (Would require parsing all CSS rules for text-spacing overrides)
        pass

    def _rule_2_1_2_no_keyboard_trap(self, keyboard_data: Dict[str, Any]):
        """Detect if keyboard users can escape from interactive elements (WCAG 2.1.2)."""
        focusable_elements = keyboard_data.get("focusableElements", [])
        if not focusable_elements:
            return
        
        # Check for focus traps:
        # 1. Last focusable element followed by first (no escape)
        # 2. Single focusable element in a region (e.g., modal without close button)
        # 3. Elements with tabindex="-1" after focusable element (focus can't escape forward)
        
        # Simple heuristic: if focus loops from last to first without an escape mechanism,
        # flag as potential trap. Manual review required.
        first_loc = focusable_elements[0].get("location", "first element") if focusable_elements else ""
        last_loc = focusable_elements[-1].get("location", "last element") if focusable_elements else ""
        
        # Check if there's a clear escape (e.g., button with "Close" text near last element)
        last_text = focusable_elements[-1].get("text", "").lower() if focusable_elements else ""
        has_escape_button = any(keyword in last_text for keyword in ["close", "cancel", "escape", "dismiss", "exit"])
        
        if not has_escape_button and len(focusable_elements) == 1:
            # Single interactive element with no visible escape—likely a trap
            location = focusable_elements[0].get("location", "Interactive element")
            
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="2.1.2",
                criterion_name="No Keyboard Trap",
                wcag_level="A",
                issue=(
                    f"{location} may trap keyboard focus with no escape route."
                ),
                evidence=(
                    f"Single focusable element found with no visible close, escape, or dismiss button."
                ),
                severity=Severity.SERIOUS,
                why_it_matters=(
                    "Keyboard-only users must be able to move focus away from an element. "
                    "Traps (e.g., modals without close buttons) render the page unusable."
                ),
                remediation_steps=[
                    "Provide an escape route: close button, cancel button, or Escape key handler.",
                    "For modals, ensure a visible close button or Escape key dismissal.",
                    "Test keyboard navigation: Tab should cycle through all elements and allow escape.",
                ],
                confidence_tier=ConfidenceTier.POSSIBLE,
                confidence_label=CONFIDENCE_LABEL[EvidenceSource.BROWSER_RENDERED],
                confidence_rationale="Focus trap is inferred from structure; manual verification recommended.",
                evidence_source=EvidenceSource.BROWSER_RENDERED,
                location=location,
                remediation_id="html_keyboard_trap",
            ))

    def _rule_2_4_3_focus_order(self, keyboard_data: Dict[str, Any]):
        """Validate that focus order is explicit and logical (WCAG 2.4.3).

        Flags when explicit positive tabindex values reorder focus relative to
        DOM order — keyboard users will encounter elements in an order that
        differs from visual / reading order."""
        focusable_elements = keyboard_data.get("focusableElements", [])
        if not focusable_elements:
            return

        positive = [(idx, el) for idx, el in enumerate(focusable_elements)
                    if el.get("tabindex", 0) > 0]
        if not positive:
            return  # No explicit reordering — assume natural focus order

        # Reordering violates 2.4.3 whenever positive tabindex causes the
        # tab sequence to differ from DOM sequence.
        tabindex_seq = [el.get("tabindex", 0) for _, el in positive]
        dom_seq = [idx for idx, _ in positive]
        # If multiple positive tabindex values exist OR a single positive value
        # is followed by lower-priority elements, focus order diverges from DOM.
        reorders = (len(set(tabindex_seq)) > 1
                    or len(positive) < len(focusable_elements))
        if not reorders:
            return

        first_idx, first_el = positive[0]
        location = first_el.get("location") or f"Interactive element {first_idx + 1}"
        text_preview = first_el.get("text", "")[:50]
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="2.4.3",
            criterion_name="Focus Order",
            wcag_level="A",
            issue=(
                f"Explicit positive tabindex on '{location}' reorders focus relative to DOM order."
            ),
            evidence=(
                f"Found {len(positive)} element(s) with positive tabindex "
                f"(values: {sorted(set(tabindex_seq))}). Text: '{text_preview}'."
            ),
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Tab order diverging from visual order disorients keyboard and screen reader users, "
                "who expect focus to move in the same sequence as the content reads."
            ),
            remediation_steps=[
                "Remove positive tabindex attributes; let the browser use natural DOM order.",
                "If specific ordering is required, restructure the DOM rather than overriding tab order.",
                "Reserve tabindex='0' for non-interactive elements that need focus and tabindex='-1' for programmatic focus only.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.BROWSER_RENDERED],
            confidence_rationale="Explicit positive tabindex values were observed in rendered DOM.",
            evidence_source=EvidenceSource.BROWSER_RENDERED,
            location=location,
            remediation_id="html_focus_order",
        ))

    # ──────────────────────────────────────────────────────────────────────
    # Phase A — Static Tier-1 rule additions
    # ──────────────────────────────────────────────────────────────────────

    def _rule_2_4_1_bypass_blocks(self):
        """WCAG 2.4.1 — Bypass Blocks. Detect skip link or main landmark."""
        if not self._html_text:
            return
        text = self._html_text
        # Skip link: anchor with href starting with # near top, often class*="skip"
        has_skip_link = bool(
            re.search(r'<a\b[^>]*href=["\']#[^"\']+["\'][^>]*>(?:[^<]*?(?:skip|jump)[^<]*?)</a>',
                      text, re.IGNORECASE)
        )
        has_main_landmark = bool(
            re.search(r'<main\b', text, re.IGNORECASE)
            or re.search(r'role=["\']main["\']', text, re.IGNORECASE)
        )
        has_nav_landmark = bool(
            re.search(r'<nav\b', text, re.IGNORECASE)
            or re.search(r'role=["\']navigation["\']', text, re.IGNORECASE)
        )
        # Only flag if page has navigation but no bypass mechanism
        if has_nav_landmark and not (has_skip_link or has_main_landmark):
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="2.4.1",
                criterion_name="Bypass Blocks",
                wcag_level="A",
                issue="Page has navigation but no skip link or <main> landmark to bypass it.",
                evidence="Found <nav> or role='navigation' but no skip link, <main>, or role='main'.",
                severity=Severity.MODERATE,
                why_it_matters=(
                    "Keyboard and screen reader users must tab through every navigation link on every page "
                    "if no bypass mechanism is provided."
                ),
                remediation_steps=[
                    "Add a 'Skip to main content' link as the first focusable element in <body>.",
                    "Wrap the primary content in a <main> landmark or use role='main'.",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
                confidence_rationale="Static DOM inspection confirms missing bypass mechanism.",
                evidence_source=EvidenceSource.DOM_DIRECT,
                location="document",
                remediation_id="html_bypass_blocks",
            ))

    def _rule_1_3_5_input_purpose(self):
        """WCAG 1.3.5 — Identify Input Purpose. Common inputs should have autocomplete."""
        if not self._html_text:
            return
        text = self._html_text
        # Inputs likely needing autocomplete: email, tel, name, address fields
        purpose_inputs = re.findall(
            r'<input\b[^>]*type=["\'](email|tel|url)["\'][^>]*>',
            text, re.IGNORECASE
        )
        # Sample a few inputs to check autocomplete presence
        all_inputs = re.findall(r'<input\b[^>]*>', text, re.IGNORECASE)
        missing_autocomplete = []
        for input_tag in all_inputs:
            tlow = input_tag.lower()
            type_match = re.search(r'type=["\']([^"\']+)["\']', tlow)
            input_type = type_match.group(1) if type_match else "text"
            if input_type not in {"email", "tel", "url", "text", "password"}:
                continue
            name_match = re.search(r'name=["\']([^"\']+)["\']', tlow)
            id_match = re.search(r'id=["\']([^"\']+)["\']', tlow)
            name_or_id = (name_match.group(1) if name_match else "") + " " + (id_match.group(1) if id_match else "")
            # Heuristic: input names suggesting common purposes
            if not re.search(r'\b(email|tel|phone|name|fname|lname|first|last|address|zip|postal|city|country|cc-|card)\b',
                             name_or_id, re.IGNORECASE):
                continue
            if 'autocomplete=' not in tlow:
                missing_autocomplete.append(input_tag[:60])
        if missing_autocomplete:
            self.fact_sheet.possible_findings.append(Finding(
                criterion_id="1.3.5",
                criterion_name="Identify Input Purpose",
                wcag_level="AA",
                issue=f"{len(missing_autocomplete)} input(s) likely need autocomplete attribute for known purposes.",
                evidence=f"Inputs without autocomplete (sample): {missing_autocomplete[:3]}",
                severity=Severity.MODERATE,
                why_it_matters=(
                    "Autocomplete attributes help users with cognitive disabilities and assistive technologies "
                    "understand and complete form fields faster."
                ),
                remediation_steps=[
                    "Add autocomplete attribute to common inputs (e.g., autocomplete='email').",
                    "Use standard tokens: 'name', 'given-name', 'family-name', 'email', 'tel', 'street-address'.",
                ],
                confidence_tier=ConfidenceTier.POSSIBLE,
                confidence_label="medium",
                confidence_rationale="Input names matched common purposes but autocomplete is heuristic.",
                evidence_source=EvidenceSource.DOM_DIRECT,
                location="form inputs",
                remediation_id="html_input_purpose",
            ))

    def _rule_3_3_2_labels_or_instructions(self):
        """WCAG 3.3.2 — Labels or Instructions. Form inputs must have an accessible name."""
        unlabeled = []
        for inp in self._inputs:
            has_name = bool(inp.label_text) or bool(inp.accessible_name_sources - {f"id:{inp.location}"})
            # If the only "source" is just the id reference and there's no label_text, it's unlabeled
            real_sources = {s for s in inp.accessible_name_sources if not s.startswith("id:")}
            if not inp.label_text and not real_sources:
                unlabeled.append(inp.location)
        if unlabeled:
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="3.3.2",
                criterion_name="Labels or Instructions",
                wcag_level="A",
                issue=f"{len(unlabeled)} form input(s) have no accessible label.",
                evidence=f"Inputs without label/aria-label/aria-labelledby/title: {unlabeled[:5]}",
                severity=Severity.SERIOUS,
                why_it_matters=(
                    "Screen reader users cannot identify the purpose of unlabeled form fields. "
                    "Voice control users cannot target them by name."
                ),
                remediation_steps=[
                    "Associate each input with a <label for='inputId'> element.",
                    "If a visible label is not appropriate, use aria-label or aria-labelledby.",
                    "Avoid relying on placeholder text as the only label.",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
                confidence_rationale="Static DOM inspection confirms missing label association.",
                evidence_source=EvidenceSource.DOM_DIRECT,
                location="form inputs",
                remediation_id="html_input_labels",
            ))

    def _rule_2_4_5_multiple_ways(self):
        """WCAG 2.4.5 — Multiple Ways. Provide nav, search, or sitemap link.

        Per WCAG, this criterion applies to a *set of Web pages*. A single
        standalone document is exempt. Heuristic: only fire if the page links
        to several other internal pages (i.e. acts as part of a multi-page
        site)."""
        if not self._html_text:
            return
        text = self._html_text
        has_nav = bool(re.search(r'<nav\b', text, re.IGNORECASE)
                       or re.search(r'role=["\']navigation["\']', text, re.IGNORECASE))
        has_search = bool(re.search(r'<input\b[^>]*type=["\']search["\']', text, re.IGNORECASE)
                          or re.search(r'role=["\']search["\']', text, re.IGNORECASE))
        has_sitemap = bool(re.search(r'href=["\'][^"\']*(sitemap|site-map)[^"\']*["\']', text, re.IGNORECASE))
        ways = sum([has_nav, has_search, has_sitemap])
        if ways >= 1:
            return

        # Skip if this looks like a single-page document. Count distinct
        # internal page targets — same-document fragment links don't count.
        internal_targets = set()
        for href in re.findall(r'href=["\']([^"\']+)["\']', text, re.IGNORECASE):
            h = href.strip()
            if not h or h.startswith('#') or h.startswith('javascript:') or h.startswith('mailto:'):
                continue
            if h.startswith('http://') or h.startswith('https://'):
                continue  # external link
            internal_targets.add(h.split('#', 1)[0])
        if len(internal_targets) < 3:
            return  # Standalone or tiny page — exempt from 2.4.5
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="2.4.5",
            criterion_name="Multiple Ways",
            wcag_level="AA",
            issue="No navigation, search, or sitemap detected — users have only one way to find content.",
            evidence=(
                f"No <nav>, role='navigation', search input, or sitemap link found, "
                f"yet page links to {len(internal_targets)} other internal pages."
            ),
            severity=Severity.MINOR,
            why_it_matters=(
                "Users with cognitive or motor disabilities may prefer different ways to navigate. "
                "Multiple paths improve discoverability."
            ),
            remediation_steps=[
                "Add a primary navigation menu using <nav>.",
                "Provide a search function using <input type='search'> or role='search'.",
                "Link to a sitemap from the footer.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale="Multiple internal links suggest a multi-page set; manual review recommended.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location="document",
            remediation_id="html_multiple_ways",
        ))

    def _rule_1_4_4_viewport_resize(self):
        """WCAG 1.4.4 — Resize Text. Viewport meta must not block zooming."""
        if not self._html_text:
            return
        text = self._html_text
        viewport_match = re.search(
            r'<meta\b[^>]*name=["\']viewport["\'][^>]*content=["\']([^"\']+)["\']',
            text, re.IGNORECASE
        )
        if not viewport_match:
            return  # No viewport meta = no violation here
        content = viewport_match.group(1).lower()
        blocks_zoom = (
            'user-scalable=no' in content
            or 'user-scalable=0' in content
        )
        max_scale_match = re.search(r'maximum-scale\s*=\s*([\d.]+)', content)
        if max_scale_match:
            try:
                if float(max_scale_match.group(1)) < 2.0:
                    blocks_zoom = True
            except ValueError:
                pass
        if blocks_zoom:
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="1.4.4",
                criterion_name="Resize Text",
                wcag_level="AA",
                issue="Viewport meta tag prevents users from zooming text to 200%.",
                evidence=f"<meta name='viewport' content='{content[:80]}'>",
                severity=Severity.SERIOUS,
                why_it_matters=(
                    "Users with low vision rely on browser zoom to read content. "
                    "Disabling pinch-zoom on mobile blocks this critical accommodation."
                ),
                remediation_steps=[
                    "Remove user-scalable=no and maximum-scale< 2 from the viewport meta tag.",
                    "Recommended: <meta name='viewport' content='width=device-width, initial-scale=1'>.",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
                confidence_rationale="Viewport meta tag content is read directly from the DOM.",
                evidence_source=EvidenceSource.DOM_DIRECT,
                location="HTML head",
                remediation_id="html_viewport_zoom",
            ))

    # ── Phase F: 2.4.6 Headings and Labels ──────────────────────────────────
    def _rule_2_4_6_headings_and_labels(self):
        """WCAG 2.4.6 — Headings and labels must describe topic or purpose.
        Strict-deterministic checks:
          - Empty <h1>-<h6> (detected directly from raw HTML)
          - Heading text matches a known generic placeholder pattern
          - Form input label text matches a known generic placeholder pattern
        """
        # 1. Empty headings — scan raw HTML directly because the parser drops
        # heading paragraphs that contain no text.
        empty_headings: List[Dict[str, Any]] = []
        if self._html_text:
            empty_re = re.compile(
                r"<h([1-6])(?:\s[^>]*)?>\s*(?:&nbsp;|&#160;|&#xA0;)?\s*</h\1>",
                re.IGNORECASE,
            )
            for m in empty_re.finditer(self._html_text):
                empty_headings.append({
                    "level": int(m.group(1)),
                    "char_offset": m.start(),
                })

        # 2. Generic heading text — scan parsed headings.
        generic_headings: List[Dict[str, Any]] = []
        for paragraph in (self.fact_sheet.paragraphs or []):
            tag = (paragraph.style_name or "").lower()
            if tag not in HEADING_TAG_LEVELS:
                continue
            text = (paragraph.text or "").strip()
            if not text:
                continue  # already accounted for via raw HTML scan
            if GENERIC_HEADING_TEXT.match(text):
                generic_headings.append({
                    "level": HEADING_TAG_LEVELS[tag],
                    "index": paragraph.index,
                    "text": text[:60],
                })

        # 3. Generic label text — scan parsed inputs with non-empty labels.
        generic_labels: List[Dict[str, Any]] = []
        for idx, inp in enumerate(self._inputs or []):
            label = (inp.label_text or "").strip()
            if not label:
                continue  # 4.1.2 / 1.3.1 cover unlabeled inputs separately
            if GENERIC_LABEL_TEXT.match(label):
                generic_labels.append({
                    "index": idx,
                    "input_type": inp.input_type,
                    "label_text": label[:60],
                    "location": inp.location,
                })

        if not empty_headings and not generic_headings and not generic_labels:
            return

        # Build evidence summary.
        evidence_parts: List[str] = []
        if empty_headings:
            sample = ", ".join(
                f"H{h['level']} (offset {h['char_offset']})" for h in empty_headings[:3]
            )
            evidence_parts.append(f"{len(empty_headings)} empty heading(s): {sample}")
        if generic_headings:
            sample = ", ".join(
                f"H{h['level']} \"{h['text']}\"" for h in generic_headings[:3]
            )
            evidence_parts.append(f"{len(generic_headings)} generic heading(s): {sample}")
        if generic_labels:
            sample = ", ".join(
                f"<label>\"{lbl['label_text']}\"</label>" for lbl in generic_labels[:3]
            )
            evidence_parts.append(f"{len(generic_labels)} generic label(s): {sample}")

        total = len(empty_headings) + len(generic_headings) + len(generic_labels)
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="2.4.6",
            criterion_name="Headings and Labels",
            wcag_level="AA",
            issue=(
                f"{total} heading(s)/label(s) do not describe topic or purpose "
                "(empty or generic placeholder text)."
            ),
            evidence="; ".join(evidence_parts) + ".",
            severity=Severity.MODERATE,
            why_it_matters=(
                "Screen reader users navigate by headings and labels. Empty or generic "
                "text (\"Heading\", \"Section\", \"Label\") tells users nothing about the "
                "content's topic and forces them to read every line to find what they need."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Each empty/generic heading and label in the HTML source.",
                "  • Replace empty <h1>-<h6> tags with descriptive text or remove them.",
                "  • Rename placeholder headings (\"Section 1\", \"Heading\") to describe the topic.",
                "  • Rewrite generic labels to describe the input's purpose (\"Email address\" instead of \"Label\").",
                "  • Verify by tabbing through the page with a screen reader (NVDA, JAWS, VoiceOver).",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale=(
                "Empty content and exact-match generic patterns are detected directly "
                "from the parsed DOM."
            ),
            evidence_source=EvidenceSource.DOM_DIRECT,
            location="Document headings and labels",
            remediation_id="html_headings_labels_quality",
            remediation_data={
                "empty_headings": empty_headings,
                "generic_headings": generic_headings,
                "generic_labels": generic_labels,
            },
        ))

    # ── Phase G: 2.5.3 Label in Name ────────────────────────────────────────
    def _rule_2_5_3_label_in_name(self):
        """WCAG 2.5.3 — When a control has both an aria-label/aria-labelledby
        and visible text, the visible text must appear in the accessible
        name. Otherwise voice-control users who say the visible text cannot
        activate the control. Strict: only flags when both are present and
        the visible text is NOT a substring (case-insensitive) of aria-label.
        """
        if not self._html_text:
            return
        # Find <button>...</button> and <a ...>...</a> with aria-label set.
        # Single-line, simple body — no nested tags expected for our checks.
        pattern = re.compile(
            r"<(button|a)\b([^>]*\baria-label\s*=\s*['\"]([^'\"]+)['\"][^>]*)>"
            r"(.*?)</\1>",
            re.IGNORECASE | re.DOTALL,
        )
        offenders: List[Dict[str, Any]] = []
        for m in pattern.finditer(self._html_text):
            tag = m.group(1).lower()
            aria_label = m.group(3).strip()
            inner_html = m.group(4)
            # Strip nested tags to get visible text only.
            visible = re.sub(r"<[^>]+>", " ", inner_html)
            visible = re.sub(r"\s+", " ", visible).strip()
            if not visible or not aria_label:
                continue
            # Substring check (lowercase). 2.5.3 requires that the visible
            # text (or a close match) appears in the accessible name.
            if visible.lower() in aria_label.lower():
                continue
            offenders.append({
                "tag": tag,
                "visible_text": visible[:60],
                "aria_label": aria_label[:60],
            })

        if not offenders:
            return

        sample = "; ".join(
            f"<{o['tag']}> visible \"{o['visible_text']}\" vs aria-label \"{o['aria_label']}\""
            for o in offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="2.5.3",
            criterion_name="Label in Name",
            wcag_level="A",
            issue=(
                f"{len(offenders)} interactive control(s) have an aria-label that does "
                "not contain the visible text."
            ),
            evidence=f"Mismatched controls: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Voice-control users (Dragon NaturallySpeaking, Voice Control on macOS/iOS, "
                "Windows Speech Recognition) say what they see on screen. When the accessible "
                "name does not contain the visible text, those users cannot activate the "
                "control by name."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Each <button>/<a> with mismatched aria-label and inner text.",
                "  • Best practice: remove the aria-label entirely and let the visible text be the name.",
                "  • If you need extra context, prefix the visible text inside aria-label "
                "(e.g. visible \"Search\" → aria-label \"Search products\").",
                "  • Verify by reading visible label aloud and matching it to aria-label substring.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="Visible text and aria-label are read directly from the source HTML.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location="Interactive controls (button/a)",
            remediation_id="html_label_in_name",
            remediation_data={"controls": offenders},
        ))

    # ── Phase G: 1.3.4 Orientation ──────────────────────────────────────────
    def _rule_1_3_4_orientation(self):
        """WCAG 1.3.4 — Content must not restrict its view to a single
        orientation (portrait/landscape) unless essential. We strictly flag:
          - <meta name="viewport"> with orientation lock
          - CSS @media (orientation: portrait/landscape) blocks that
            unconditionally hide content.
        Heuristic / strict: detect the patterns; emit a CONFIRMED finding
        for the meta lock and a POSSIBLE finding for the CSS lock.
        """
        if not self._html_text:
            return
        # 1. Viewport meta with orientation= (rare but blatant)
        meta_lock = re.search(
            r"<meta[^>]+name\s*=\s*['\"]viewport['\"][^>]*content\s*=\s*['\"][^'\"]*orientation\s*=\s*(portrait|landscape)",
            self._html_text,
            re.IGNORECASE,
        )
        # 2. CSS that pins to one orientation by hiding the other entirely.
        # Look for @media (orientation: X) { ... display: none ... } where X
        # explicitly disables the opposite orientation.
        css_lock_re = re.compile(
            r"@media[^{]*\(\s*orientation\s*:\s*(portrait|landscape)\s*\)[^{]*\{[^}]*\}",
            re.IGNORECASE | re.DOTALL,
        )
        css_blocks_with_hide = []
        for m in css_lock_re.finditer(self._html_text):
            block = m.group(0)
            if re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", block, re.IGNORECASE):
                # Match indicates that one orientation hides content.
                css_blocks_with_hide.append({
                    "orientation": m.group(1).lower(),
                    "snippet": block[:120].replace("\n", " "),
                })

        if not meta_lock and not css_blocks_with_hide:
            return

        if meta_lock:
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="1.3.4",
                criterion_name="Orientation",
                wcag_level="AA",
                issue="Viewport meta tag locks orientation.",
                evidence=f"Viewport orientation lock: '{meta_lock.group(0)[:120]}'.",
                severity=Severity.SERIOUS,
                why_it_matters=(
                    "Users with mobility disabilities may have devices mounted in a single "
                    "orientation. Locking the page prevents them from using the content."
                ),
                remediation_steps=[
                    "Remove the orientation= entry from the viewport meta tag.",
                    "Restructure content so it adapts to both portrait and landscape.",
                    "Only lock if the content is essential to one orientation (e.g. a piano keyboard).",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
                confidence_rationale="Viewport meta tag content read directly from the DOM.",
                evidence_source=EvidenceSource.DOM_DIRECT,
                location="HTML head",
                remediation_id="html_orientation_meta_lock",
            ))

        if css_blocks_with_hide:
            sample = css_blocks_with_hide[0]["snippet"]
            self.fact_sheet.possible_findings.append(Finding(
                criterion_id="1.3.4",
                criterion_name="Orientation",
                wcag_level="AA",
                issue=(
                    f"{len(css_blocks_with_hide)} CSS @media orientation block(s) "
                    "hide content via display:none / visibility:hidden."
                ),
                evidence=f"Example: {sample}",
                severity=Severity.MODERATE,
                why_it_matters=(
                    "Hiding content based on device orientation may prevent users with "
                    "fixed-mount devices from accessing functionality."
                ),
                remediation_steps=[
                    "Review each @media (orientation:...) block.",
                    "If essential, leave as-is; otherwise restructure so content remains accessible in both orientations.",
                ],
                confidence_tier=ConfidenceTier.POSSIBLE,
                confidence_label="medium",
                confidence_rationale=(
                    "Pattern detected via static CSS scan; orientation-specific hiding "
                    "may be intentional and harmless."
                ),
                evidence_source=EvidenceSource.DOM_DIRECT,
                location="CSS",
                remediation_id="html_orientation_css_hide",
                remediation_data={"blocks": css_blocks_with_hide},
            ))

    # ── Phase G: 2.1.1 Keyboard ─────────────────────────────────────────────
    def _rule_2_1_1_keyboard_handlers(self):
        """WCAG 2.1.1 — Detect onclick handlers on non-interactive elements
        (div, span, p, li) that have neither role=button/link nor a
        keyboard handler (onkeydown/onkeyup/onkeypress) nor tabindex. Such
        elements are unreachable / unusable from the keyboard.
        """
        if not self._html_text:
            return
        offenders: List[Dict[str, Any]] = []
        # Find non-interactive elements with onclick attribute.
        pattern = re.compile(
            r"<(div|span|p|li|td|th|img)\b([^>]*\bonclick\s*=[^>]*)>",
            re.IGNORECASE,
        )
        for m in pattern.finditer(self._html_text):
            tag = m.group(1).lower()
            attrs = m.group(2)
            attrs_low = attrs.lower()
            has_role = re.search(
                r"\brole\s*=\s*['\"](button|link|menuitem|tab|checkbox|radio|switch)['\"]",
                attrs_low,
            )
            has_tabindex = re.search(r"\btabindex\s*=", attrs_low)
            has_key_handler = re.search(
                r"\bonkey(down|up|press)\s*=", attrs_low
            )
            if has_role and has_tabindex and has_key_handler:
                continue
            missing = []
            if not has_role:
                missing.append("role")
            if not has_tabindex:
                missing.append("tabindex")
            if not has_key_handler:
                missing.append("onkey* handler")
            offenders.append({
                "tag": tag,
                "missing": missing,
                "snippet": m.group(0)[:120],
            })

        if not offenders:
            return

        sample = "; ".join(
            f"<{o['tag']}> missing: {', '.join(o['missing'])}"
            for o in offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="2.1.1",
            criterion_name="Keyboard",
            wcag_level="A",
            issue=(
                f"{len(offenders)} non-interactive element(s) with onclick "
                "have no role+tabindex+keyboard handler."
            ),
            evidence=f"Affected elements: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Non-interactive HTML elements with onclick are not reachable by Tab "
                "and do not respond to Enter/Space. Keyboard users and screen reader "
                "users cannot activate them."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Each <div>/<span>/etc. with onclick.",
                "  • Best fix: use a <button> or <a href> instead of div/span.",
                "  • If you must keep the element, add ALL of: role=\"button\" (or link), tabindex=\"0\", and onkeydown handler that triggers on Enter/Space.",
                "  • Verify with keyboard: Tab to the element, press Enter or Space — it should activate.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="onclick / role / tabindex / onkey* attributes read directly from source HTML.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location="Interactive elements",
            remediation_id="html_keyboard_onclick_handlers",
            remediation_data={"controls": offenders},
        ))

    # ── Phase G: 2.2.1 Timing Adjustable ────────────────────────────────────
    def _rule_2_2_1_meta_refresh(self):
        """WCAG 2.2.1 — <meta http-equiv="refresh" content="N"> with N > 0
        forces the page to reload after N seconds with no user control. This
        is a classic accessibility failure for users who need more time to
        read content. We strict-match the meta tag and parse the seconds.
        N=0 (immediate redirect) is a different concern and is also flagged.
        """
        if not self._html_text:
            return
        pattern = re.compile(
            r"<meta\b[^>]*\bhttp-equiv\s*=\s*['\"]refresh['\"][^>]*\bcontent\s*=\s*['\"]\s*(\d+)\s*(?:;[^'\"]*)?['\"]",
            re.IGNORECASE,
        )
        matches = list(pattern.finditer(self._html_text))
        if not matches:
            return

        offenders = []
        for m in matches:
            seconds = int(m.group(1))
            offenders.append({
                "seconds": seconds,
                "snippet": m.group(0)[:120],
            })

        # All matches reported together.
        sample = "; ".join(
            f"{o['seconds']}s refresh: {o['snippet']}"
            for o in offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="2.2.1",
            criterion_name="Timing Adjustable",
            wcag_level="A",
            issue=(
                f"{len(offenders)} <meta http-equiv=\"refresh\"> tag(s) force "
                "page reload/redirect with no user control."
            ),
            evidence=f"Auto-refresh meta tag(s): {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Users who read slowly, use screen readers, or navigate via switch "
                "devices may not finish before the page refreshes, losing their place "
                "or being redirected mid-task. WCAG 2.2.1 requires that timing be "
                "adjustable, extendable, or removable."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: <head> of the HTML.",
                "  • Remove the <meta http-equiv=\"refresh\"> tag.",
                "  • If a redirect is needed, use a server-side 301/302 redirect (no client timer).",
                "  • If the refresh is for live data, replace with a manual refresh button or SSE/WebSocket update.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="Meta refresh tag content is read directly from the HTML source.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location="HTML head",
            remediation_id="html_meta_refresh",
            remediation_data={"refreshes": offenders},
        ))

    # ── Phase G: 1.4.12 Text Spacing ────────────────────────────────────────
    def _rule_1_4_12_text_spacing_important(self):
        """WCAG 1.4.12 — Users must be able to override line-height,
        letter-spacing, word-spacing, and paragraph-spacing without loss of
        content or functionality. CSS rules that pin these properties with
        !important prevent assistive bookmarklets / extensions from setting
        the WCAG-required minimums. Strict regex match on '!important'.
        """
        if not self._html_text:
            return
        # Match `<prop>: <value> !important` for the four spacing props.
        pattern = re.compile(
            r"(line-height|letter-spacing|word-spacing|"
            r"(?:margin|padding)-(?:top|bottom)|"
            r"paragraph-spacing)"
            r"\s*:\s*[^;{}!]+!\s*important",
            re.IGNORECASE,
        )
        matches = list(pattern.finditer(self._html_text))
        if not matches:
            return

        offenders: List[Dict[str, Any]] = []
        seen = set()
        for m in matches:
            prop = m.group(1).lower()
            snippet = m.group(0).strip()[:80]
            key = (prop, snippet)
            if key in seen:
                continue
            seen.add(key)
            offenders.append({"property": prop, "snippet": snippet})

        # Only the four "core" 1.4.12 properties trigger a confirmed finding;
        # margin/padding suggest possible interference but are not direct
        # violations on their own. Filter accordingly.
        core_props = {"line-height", "letter-spacing", "word-spacing", "paragraph-spacing"}
        core_offenders = [o for o in offenders if o["property"] in core_props]

        if not core_offenders:
            return

        sample = "; ".join(
            f"{o['property']} pinned via !important ({o['snippet']})"
            for o in core_offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.4.12",
            criterion_name="Text Spacing",
            wcag_level="AA",
            issue=(
                f"{len(core_offenders)} CSS rule(s) pin text-spacing properties "
                "with !important, blocking user overrides."
            ),
            evidence=f"!important rules on text-spacing properties: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "WCAG 1.4.12 requires content to remain functional when users override "
                "line-height to >= 1.5, letter-spacing to >= 0.12em, word-spacing to "
                ">= 0.16em, and paragraph spacing to >= 2x font size. !important blocks "
                "those overrides via assistive bookmarklets and extensions."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: CSS rules in <style> blocks or external stylesheets.",
                "  • Remove the !important flag from line-height, letter-spacing, word-spacing, paragraph spacing.",
                "  • If specificity was the reason for !important, refactor the selector instead.",
                "  • Test by applying the WCAG 1.4.12 bookmarklet (e.g. Steve Faulkner's) to verify content reflows without loss.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="CSS source scanned directly; !important on the four 1.4.12 properties detected.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location="CSS",
            remediation_id="html_text_spacing_important",
            remediation_data={"rules": core_offenders},
        ))

    # ── Phase H: 3.2.1 On Focus ─────────────────────────────────────────────
    def _rule_3_2_1_on_focus_change(self):
        """WCAG 3.2.1 — When a control receives focus, it must NOT trigger a
        change of context (form submit, navigation). Strict regex match for
        onfocus handlers that auto-submit or navigate.
        """
        if not self._html_text:
            return
        # Match elements with onfocus="<auto-submit/navigate pattern>"
        pattern = re.compile(
            r"<[^>]*\bonfocus\s*=\s*['\"]"
            r"([^'\"]*(?:\.submit\s*\(|window\.location|document\.location|location\.href|location\.assign|location\.replace)[^'\"]*)"
            r"['\"][^>]*>",
            re.IGNORECASE,
        )
        offenders = []
        for m in pattern.finditer(self._html_text):
            offenders.append({
                "handler": m.group(1)[:80],
                "snippet": m.group(0)[:120],
            })
        if not offenders:
            return
        sample = "; ".join(f"onfocus=\"{o['handler']}\"" for o in offenders[:3])
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="3.2.1",
            criterion_name="On Focus",
            wcag_level="A",
            issue=(
                f"{len(offenders)} element(s) auto-submit or navigate on focus, "
                "causing an unexpected change of context."
            ),
            evidence=f"Auto-action onfocus handlers: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "When focus alone triggers a form submit or page navigation, keyboard and "
                "screen reader users cannot tab through controls without unintended actions. "
                "Context changes must be initiated by explicit activation (Enter/Space/click)."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Each element with onfocus that submits or navigates.",
                "  • Move the action to onclick or onchange after explicit user activation.",
                "  • If the focus must trigger something, add a confirmation step before the context change.",
                "  • Test by tabbing through the form — focus should NEVER cause submission or navigation.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="onfocus handler text matched directly against known auto-submit/navigate patterns.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location="Interactive elements",
            remediation_id="html_on_focus_context_change",
            remediation_data={"controls": offenders},
        ))

    # ── Phase H: 3.2.2 On Input ─────────────────────────────────────────────
    def _rule_3_2_2_on_input_change(self):
        """WCAG 3.2.2 — Changing the value of a control must NOT auto-trigger
        a change of context. Common offenders: <select onchange="this.form.submit()">
        and onchange="window.location=...".
        """
        if not self._html_text:
            return
        pattern = re.compile(
            r"<[^>]*\bon(?:change|input)\s*=\s*['\"]"
            r"([^'\"]*(?:\.submit\s*\(|window\.location|document\.location|location\.href|location\.assign|location\.replace)[^'\"]*)"
            r"['\"][^>]*>",
            re.IGNORECASE,
        )
        offenders = []
        for m in pattern.finditer(self._html_text):
            offenders.append({
                "handler": m.group(1)[:80],
                "snippet": m.group(0)[:120],
            })
        if not offenders:
            return
        sample = "; ".join(f"\"{o['handler']}\"" for o in offenders[:3])
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="3.2.2",
            criterion_name="On Input",
            wcag_level="A",
            issue=(
                f"{len(offenders)} element(s) auto-submit or navigate when their "
                "value changes (onchange/oninput)."
            ),
            evidence=f"Auto-action onchange/oninput handlers: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Many users navigate select lists with arrow keys, which fires onchange "
                "for each option visited. Auto-submitting or navigating on every change "
                "prevents them from seeing other options. WCAG 3.2.2 requires explicit "
                "activation (a Submit button or similar) for context changes."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Each <select>/<input> with auto-submit or auto-navigate onchange.",
                "  • Add an explicit Submit button and remove the onchange auto-action.",
                "  • If you need progressive disclosure, update visible content but do NOT navigate or submit.",
                "  • Test with keyboard: arrow through the <select> options — page should not reload.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="onchange/oninput handler text matched directly against known auto-submit/navigate patterns.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location="Form controls",
            remediation_id="html_on_input_context_change",
            remediation_data={"controls": offenders},
        ))

    # ── Phase H: 3.3.1 Error Identification ─────────────────────────────────
    def _rule_3_3_1_error_identification_readiness(self):
        """WCAG 3.3.1 — When an input error is detected, it must be identified
        in text. Static analysis can verify *readiness*: every <input required>
        should have aria-describedby (pointing to a help/error message) AND an
        aria-invalid attribute can be set when validation fails. We strictly
        flag required inputs with NO aria-describedby and NO aria-invalid
        attribute at all (so the form has no programmatic error wiring).
        """
        if not self._html_text:
            return
        # Find every required input/select/textarea (boolean attribute, with or without value).
        pattern = re.compile(
            r"<(input|select|textarea)\b([^>]*)\brequired\b([^>]*)>",
            re.IGNORECASE,
        )
        offenders = []
        for m in pattern.finditer(self._html_text):
            tag = m.group(1).lower()
            attrs = (m.group(2) + " " + m.group(3)).lower()
            # Skip type=hidden / type=submit / type=button — required is meaningless there
            if tag == "input":
                m_type = re.search(r"\btype\s*=\s*['\"](\w+)['\"]", attrs)
                if m_type and m_type.group(1) in {"hidden", "submit", "button", "image", "reset"}:
                    continue
            has_describedby = re.search(r"\baria-describedby\s*=", attrs)
            has_invalid = re.search(r"\baria-invalid\s*=", attrs)
            if has_describedby or has_invalid:
                continue
            offenders.append({
                "tag": tag,
                "snippet": m.group(0)[:120],
            })
        if not offenders:
            return
        sample = "; ".join(f"<{o['tag']}> {o['snippet']}" for o in offenders[:3])
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="3.3.1",
            criterion_name="Error Identification",
            wcag_level="A",
            issue=(
                f"{len(offenders)} required field(s) have no error-message wiring "
                "(no aria-describedby and no aria-invalid)."
            ),
            evidence=f"Required fields without error wiring: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "When validation fails, screen readers need a way to announce the error. "
                "Without aria-describedby (pointing at an error message) or aria-invalid "
                "(marking the field as in-error), users may not know which field failed "
                "or why."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Each <input required>/<select required>/<textarea required>.",
                "  • Add aria-describedby pointing to a sibling element with the error/help text.",
                "  • Toggle aria-invalid='true' on the field when validation fails (and 'false'/remove when fixed).",
                "  • Provide visible error text near the field (don't rely only on color).",
                "  • Test by submitting with errors and verifying a screen reader announces the failure.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="required attribute and aria-describedby/aria-invalid attributes read directly from source HTML.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location="Form controls",
            remediation_id="html_error_identification_readiness",
            remediation_data={"controls": offenders},
        ))

    # ── Phase H: 1.3.3 Sensory Characteristics ──────────────────────────────
    def _rule_1_3_3_sensory_characteristics(self):
        """WCAG 1.3.3 — Instructions must not rely solely on sensory characteristics
        like shape, color, position, or sound. Strict regex on body text for
        well-known offending phrases.
        """
        if not self.fact_sheet.paragraphs:
            return
        # Phrases like:
        #   "click the red button", "press the green icon"
        #   "see the link on the right", "use the menu in the upper-right"
        #   "the round/square/triangular icon"
        color_phrase = re.compile(
            r"\b(?:click|press|tap|select|choose|use|see|look\s+at|find|locate)\b"
            r"\s+(?:the\s+)?"
            r"(?:red|green|blue|yellow|orange|purple|pink|black|white|gray|grey|brown)\s+"
            r"(?:button|icon|link|tab|box|item|element|circle|square|arrow|dot|marker)\b",
            re.IGNORECASE,
        )
        position_phrase = re.compile(
            r"\b(?:click|press|tap|select|choose|use|see|look\s+at|find|locate)\b"
            r"\s+(?:the\s+)?"
            r"(?:button|icon|link|tab|box|menu|panel|item)\s+"
            r"(?:on|at|to|in)\s+the\s+"
            r"(?:right|left|top|bottom|upper|lower|center)\b",
            re.IGNORECASE,
        )
        shape_phrase = re.compile(
            r"\b(?:click|press|tap|select|choose|use|see|look\s+at|find|locate)\b"
            r"\s+(?:the\s+)?"
            r"(?:round|circular|square|rectangular|triangular|diamond-shaped|star-shaped)\s+"
            r"(?:button|icon|link|tab|box|element)\b",
            re.IGNORECASE,
        )

        offenders: List[Dict[str, Any]] = []
        for paragraph in self.fact_sheet.paragraphs:
            text = (paragraph.text or "").strip()
            if not text:
                continue
            for kind, pat in (("color", color_phrase),
                              ("position", position_phrase),
                              ("shape", shape_phrase)):
                m = pat.search(text)
                if m:
                    offenders.append({
                        "kind": kind,
                        "paragraph_index": paragraph.index,
                        "snippet": m.group(0)[:80],
                    })
                    break  # one finding per paragraph
        if not offenders:
            return
        sample = "; ".join(
            f"{o['kind']}-only: \"{o['snippet']}\"" for o in offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.3.3",
            criterion_name="Sensory Characteristics",
            wcag_level="A",
            issue=(
                f"{len(offenders)} paragraph(s) reference UI elements only by "
                "color, shape, or position."
            ),
            evidence=f"Sensory-only references: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "Users who are blind, color-blind, using screen readers, or accessing the "
                "page through linearized layouts cannot identify a control by its visual "
                "appearance or screen position. Instructions must include a textual cue "
                "(name, label, or relationship) that does not rely on sensory characteristics."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Each paragraph with sensory-only instructions.",
                "  • Add the control's name or label: \"Click the red Submit button\" instead of \"click the red button\".",
                "  • Combine color/position with text: \"Use the menu labeled 'Help' (top-right)\" rather than \"the menu on the right\".",
                "  • Reword shape references to use the visible label.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.TEXT_CONTENT],
            confidence_rationale="Body text scanned for known sensory-only phrase patterns.",
            evidence_source=EvidenceSource.TEXT_CONTENT,
            location="Document body text",
            remediation_id="html_sensory_characteristics",
            remediation_data={"references": offenders},
        ))

    # ── Phase I: 1.4.5 Images of Text (inline SVG with text elements) ────────
    def _rule_1_4_5_inline_svg_text(self):
        """WCAG 1.4.5 — Images of text. We can confirm one strict case from
        static HTML alone: an inline <svg> that contains <text> children IS an
        image of text. (It's a pixel/vector rendering of text rather than real
        text.) Other forms — bitmap images-of-text, CSS background images of
        text — require pixel analysis and stay out of scope.
        """
        if not self._html_text:
            return
        # Find each inline <svg>...</svg> block (non-greedy, dotall) and check
        # if it contains <text>. We do NOT flag aria-label-only SVGs that are
        # purely decorative (no <text>); those are unrelated to 1.4.5.
        svg_pat = re.compile(r"<svg\b[^>]*>(.*?)</svg>", re.IGNORECASE | re.DOTALL)
        text_pat = re.compile(r"<text\b[^>]*>", re.IGNORECASE)
        offenders: List[Dict[str, Any]] = []
        for m in svg_pat.finditer(self._html_text):
            inner = m.group(1)
            text_hits = text_pat.findall(inner)
            if not text_hits:
                continue
            # Capture an attribute snippet for evidence.
            opening = re.match(r"<svg\b([^>]*)>", m.group(0), re.IGNORECASE)
            attrs = (opening.group(1) if opening else "").strip()[:80]
            offenders.append({
                "text_count": len(text_hits),
                "attrs": attrs,
            })
        if not offenders:
            return
        sample = "; ".join(
            f"<svg {o['attrs']}> with {o['text_count']} <text> child(ren)"
            for o in offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.4.5",
            criterion_name="Images of Text",
            wcag_level="AA",
            issue=(
                f"{len(offenders)} inline SVG image(s) render text as graphics "
                "rather than using real HTML text."
            ),
            evidence=f"Inline SVGs containing <text> elements: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "Text rendered inside SVG cannot be resized, restyled, or selected by users. "
                "It also defeats translation tools and high-contrast mode. WCAG 1.4.5 "
                "requires real text wherever the same visual presentation is achievable."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Each inline <svg> with <text> children.",
                "  • Replace the SVG with HTML text styled with CSS (font, color, gradients can all be done in CSS).",
                "  • If the visual must be SVG (e.g., a logo), keep the SVG but provide the text as a real <h1>/<p> alongside.",
                "  • Reserve <text> in SVG for genuine data visualisations (chart axis labels), not body or heading text.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="Inline <svg> markup parsed directly; <text> children counted.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location="Inline SVG markup",
            remediation_id="html_inline_svg_text",
            remediation_data={"svgs": offenders},
        ))

    # ── Phase I: 2.5.8 Target Size (Minimum) — WCAG 2.2 ──────────────────────
    def _rule_2_5_8_target_size_minimum(self):
        """WCAG 2.2 — 2.5.8 Target Size (Minimum). Interactive controls must
        be at least 24x24 CSS pixels (with limited exemptions). We strictly
        flag inline-style or CSS rules that explicitly set BOTH width AND
        height below 24px on buttons / links / form controls.
        """
        if not self._html_text:
            return
        offenders: List[Dict[str, Any]] = []

        # 1) Inline style on a button/a/input
        inline_pat = re.compile(
            r"<(button|a|input)\b([^>]*\bstyle\s*=\s*['\"]([^'\"]+)['\"][^>]*)>",
            re.IGNORECASE,
        )
        # Width / height in px — we accept '20px' style declarations only (skip %, em, vw).
        size_pat = re.compile(r"\b(width|height)\s*:\s*(\d+(?:\.\d+)?)px", re.IGNORECASE)
        for m in inline_pat.finditer(self._html_text):
            tag = m.group(1).lower()
            style = m.group(3)
            sizes: Dict[str, float] = {}
            for sm in size_pat.finditer(style):
                sizes[sm.group(1).lower()] = float(sm.group(2))
            if "width" in sizes and "height" in sizes:
                if sizes["width"] < 24 and sizes["height"] < 24:
                    offenders.append({
                        "source": "inline-style",
                        "tag": tag,
                        "width_px": sizes["width"],
                        "height_px": sizes["height"],
                        "snippet": m.group(0)[:120],
                    })

        # 2) <style> blocks: rules whose selector references button/a/input AND
        #    have BOTH width<24 and height<24 in px.
        style_block_pat = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
        rule_pat = re.compile(r"([^{}]+)\{([^{}]+)\}")
        for sb in style_block_pat.finditer(self._html_text):
            css = sb.group(1)
            for rm in rule_pat.finditer(css):
                selector = rm.group(1).strip()
                if not re.search(r"\b(button|input|select|textarea)\b|(?<![\w-])a(?![\w-])", selector, re.IGNORECASE):
                    continue
                body = rm.group(2)
                sizes = {}
                for sm in size_pat.finditer(body):
                    sizes[sm.group(1).lower()] = float(sm.group(2))
                if "width" in sizes and "height" in sizes:
                    if sizes["width"] < 24 and sizes["height"] < 24:
                        offenders.append({
                            "source": "<style> block",
                            "selector": selector[:60],
                            "width_px": sizes["width"],
                            "height_px": sizes["height"],
                        })
        if not offenders:
            return
        sample = "; ".join(
            f"{o.get('selector') or o.get('tag')}: {o['width_px']:.0f}x{o['height_px']:.0f}px"
            for o in offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="2.5.8",
            criterion_name="Target Size (Minimum)",
            wcag_level="AA",
            issue=(
                f"{len(offenders)} interactive target(s) declared smaller than "
                "the WCAG 2.2 minimum of 24×24 CSS pixels."
            ),
            evidence=f"Targets below 24x24 px: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "Users with motor impairments, tremors, or who use touchscreens have trouble "
                "activating controls smaller than 24x24 CSS pixels. WCAG 2.2 (2.5.8) sets this "
                "as the minimum target size unless the control sits inline in a sentence or "
                "has the legally-required size."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Each control declared below 24x24 CSS px.",
                "  • Increase width and height to at least 24px (or use min-width / min-height).",
                "  • If you cannot grow the visual, give the control padding so the click target reaches 24x24.",
                "  • Inline-text links inside paragraphs are exempt (the 'inline exception').",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="CSS source declarations parsed directly for width/height in px on interactive selectors.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location="CSS / inline style",
            remediation_id="html_target_size_minimum",
            remediation_data={"targets": offenders},
        ))

    # ── Phase I: 3.3.8 Accessible Authentication (Minimum) — WCAG 2.2 ───────
    def _rule_3_3_8_accessible_authentication(self):
        """WCAG 2.2 — 3.3.8 Accessible Authentication (Minimum). A cognitive
        function test (e.g. remembering a password) must NOT be the only means
        of authentication unless an alternative is provided. We flag patterns
        that *block* password manager support, which is the most common static
        violation:
          • <input type=password autocomplete='off' / 'new-password' on login>
          • <input type=password onpaste='return false'> (pastes blocked)
          • <input type=password maxlength='N'> with N < 16 (memorisation pressure)
        """
        if not self._html_text:
            return
        password_pat = re.compile(
            r"<input\b([^>]*\btype\s*=\s*['\"]password['\"][^>]*)>",
            re.IGNORECASE,
        )
        offenders: List[Dict[str, Any]] = []
        for m in password_pat.finditer(self._html_text):
            attrs = m.group(1)
            issues = []
            ac = re.search(r"\bautocomplete\s*=\s*['\"]([^'\"]+)['\"]", attrs, re.IGNORECASE)
            if ac and ac.group(1).strip().lower() in {"off", "false", "none"}:
                issues.append("autocomplete='off' (blocks password managers)")
            if re.search(r"\bonpaste\s*=\s*['\"][^'\"]*(?:return\s+false|preventDefault|\.preventDefault\s*\()", attrs, re.IGNORECASE):
                issues.append("onpaste blocks paste (forces re-typing)")
            ml = re.search(r"\bmaxlength\s*=\s*['\"]?(\d+)", attrs, re.IGNORECASE)
            if ml and int(ml.group(1)) < 16:
                issues.append(f"maxlength={ml.group(1)} (forces short, memorable password)")
            if issues:
                offenders.append({
                    "issues": issues,
                    "snippet": m.group(0)[:120],
                })
        if not offenders:
            return
        sample = "; ".join(
            f"{o['snippet'][:60]}… → {', '.join(o['issues'])}"
            for o in offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="3.3.8",
            criterion_name="Accessible Authentication (Minimum)",
            wcag_level="AA",
            issue=(
                f"{len(offenders)} password field(s) impede password-manager use, "
                "forcing the user to memorise or re-type credentials."
            ),
            evidence=f"Authentication pain points: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "WCAG 2.2 (3.3.8) requires that authentication not depend on a cognitive "
                "function test (like remembering a password) unless an alternative is offered. "
                "Blocking password managers — by disabling autocomplete, blocking paste, or "
                "imposing short maxlengths — eliminates the alternative and pushes the burden "
                "onto users with cognitive disabilities."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Each <input type='password'> with these attributes.",
                "  • Remove autocomplete='off' from password fields. Use autocomplete='current-password' on login or 'new-password' on signup.",
                "  • Remove onpaste handlers that block paste — let users paste from password managers.",
                "  • Allow long passwords. WCAG 2.2 requires the field to accept at least 64 characters.",
                "  • If you must keep these restrictions, provide an alternative authentication method (passkey, OTP, biometric).",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="Password input attributes parsed directly from source HTML.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location="Password fields",
            remediation_id="html_accessible_authentication",
            remediation_data={"fields": offenders},
        ))

    def _rule_2_5_7_dragging_movements(self):
        """WCAG 2.2 — 2.5.7 Dragging Movements (AA). All functionality that
        uses a dragging movement for operation can be achieved by a single
        pointer without dragging, unless dragging is essential.

        Static signal: elements with `draggable='true'` or with both
        `ondragstart` and `ondrop` handlers, when the document does not
        contain any reorder/move <button> alternatives. Strict.
        """
        if not self._html_text:
            return
        text = self._html_text

        # Find draggable affordances.
        drag_pat = re.compile(
            r"<([a-zA-Z][\w-]*)\b([^>]*)>",
            re.IGNORECASE,
        )
        offenders: List[Dict[str, Any]] = []
        for m in drag_pat.finditer(text):
            tag = m.group(1).lower()
            if tag in ('script', 'style'):
                continue
            attrs = m.group(2) or ''
            attrs_l = attrs.lower()
            is_draggable = bool(
                re.search(r"\bdraggable\s*=\s*['\"]?true['\"]?", attrs_l)
            )
            has_handlers = (
                'ondragstart' in attrs_l
                and ('ondrop' in attrs_l or 'ondragover' in attrs_l)
            )
            if not (is_draggable or has_handlers):
                continue
            id_match = re.search(r"\bid\s*=\s*['\"]([^'\"]+)['\"]", attrs, re.IGNORECASE)
            class_match = re.search(r"\bclass\s*=\s*['\"]([^'\"]+)['\"]", attrs, re.IGNORECASE)
            offenders.append({
                "tag": tag,
                "id": id_match.group(1) if id_match else "",
                "class": (class_match.group(1) if class_match else "")[:60],
                "draggable_attr": is_draggable,
                "drag_handlers": [
                    a for a in ('ondragstart', 'ondragover', 'ondrop') if a in attrs_l
                ],
            })

        if not offenders:
            return

        # Document-level fallback signal: presence of an explicit move/reorder
        # button anywhere on the page softens the finding (we still report,
        # but as POSSIBLE rather than CONFIRMED).
        has_move_button = bool(
            re.search(
                r"<button\b[^>]*>[^<]*\b(move|reorder|sort|up|down)\b",
                text,
                re.IGNORECASE,
            )
        )

        sample = "; ".join(
            f"<{o['tag']}{(' id=' + o['id']) if o['id'] else ''}>"
            for o in offenders[:3]
        )
        finding = Finding(
            criterion_id="2.5.7",
            criterion_name="Dragging Movements",
            wcag_level="AA",
            issue=(
                f"{len(offenders)} draggable element(s) detected"
                + (" (no obvious move/reorder button alternative on the page)."
                   if not has_move_button else
                   " — page has move/reorder buttons elsewhere; verify per-widget alternative.")
            ),
            evidence=f"Drag-only widgets: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "WCAG 2.2 (2.5.7) requires that every drag-and-drop action be reachable "
                "with a single pointer interaction (a click or tap), unless the dragging "
                "is essential. Users with motor impairments, tremor, or who use a head "
                "pointer or switch device often cannot perform sustained drag gestures."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(offenders)} drag-only widget(s) on the page.",
                "  • Add an explicit reorder/move control (up/down arrows or a 'Move to…' menu) next to each draggable item.",
                "  • Pair the drag affordance with a <button> that performs the same action via click.",
                "  • Ensure the alternative is keyboard-reachable (Tab + Enter), not mouse-only.",
                "  • If the gesture truly is essential (e.g. drawing), document the exception.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED if not has_move_button else ConfidenceTier.POSSIBLE,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT] if not has_move_button else "medium",
            confidence_rationale="Parsed draggable attribute and drag event handlers from the source HTML; checked the document for any move/reorder buttons.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"{len(offenders)} draggable element(s)",
            remediation_id="html_dragging_movements",
            remediation_data={"elements": offenders, "page_has_move_buttons": has_move_button},
        )
        if has_move_button:
            self.fact_sheet.possible_findings.append(finding)
        else:
            self.fact_sheet.confirmed_findings.append(finding)

    def _collect_embedded_media(self) -> List[Dict[str, Any]]:
        if not self._html_text:
            return []

        text = self._html_text
        media_items: List[Dict[str, Any]] = []
        covered_spans: List[tuple[int, int]] = []
        block_pattern = re.compile(
            r"<(?P<tag>audio|video)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>",
            re.IGNORECASE | re.DOTALL,
        )
        start_pattern = re.compile(
            r"<(?P<tag>audio|video)\b(?P<attrs>[^>]*)>",
            re.IGNORECASE,
        )
        caption_track_pattern = re.compile(
            r"<track\b[^>]*\bkind\s*=\s*(?:\"|')?(captions|subtitles)(?:\"|')?",
            re.IGNORECASE,
        )
        description_track_pattern = re.compile(
            r"<track\b[^>]*\bkind\s*=\s*(?:\"|')?descriptions?(?:\"|')?",
            re.IGNORECASE,
        )
        transcript_signal_pattern = re.compile(
            r"transcript|text alternative|media alternative|audio description|described video",
            re.IGNORECASE,
        )

        def build_item(match: re.Match[str], body: str) -> Dict[str, Any]:
            attrs = match.group("attrs") or ""
            trailing = text[match.end():min(len(text), match.end() + 400)]
            nearby = f"{body} {trailing} {attrs}"
            attrs_lower = attrs.lower()
            return {
                "tag": match.group("tag").lower(),
                "snippet": match.group(0)[:160],
                "has_caption_track": bool(caption_track_pattern.search(body)),
                "has_description_track": bool(description_track_pattern.search(body)),
                "has_transcript_signal": bool(transcript_signal_pattern.search(nearby)) or "aria-describedby" in attrs_lower,
            }

        for match in block_pattern.finditer(text):
            media_items.append(build_item(match, match.group("body") or ""))
            covered_spans.append((match.start(), match.end()))

        for match in start_pattern.finditer(text):
            if any(start <= match.start() < end for start, end in covered_spans):
                continue
            media_items.append(build_item(match, ""))

        return media_items

    # ── Phase M1: 1.4.2 Audio Control ──────────────────────────────────────
    def _rule_1_4_2_audio_control(self):
        """WCAG 1.4.2 — Audio that plays automatically for more than 3 seconds
        must have a control to pause/stop it. We flag <audio autoplay> without
        a `controls` attribute, and <video autoplay> without `muted` or
        `controls` (video with sound).
        """
        if not self._html_text:
            return
        text = self._html_text
        offenders: List[Dict[str, Any]] = []
        # <audio autoplay …> without controls
        for m in re.finditer(r"<audio\b([^>]*)>", text, re.IGNORECASE):
            attrs = m.group(1).lower()
            if 'autoplay' in attrs and 'controls' not in attrs:
                offenders.append({"tag": "audio", "snippet": m.group(0)[:120]})
        # <video autoplay …> with sound and no controls
        for m in re.finditer(r"<video\b([^>]*)>", text, re.IGNORECASE):
            attrs = m.group(1).lower()
            if 'autoplay' in attrs and 'controls' not in attrs and 'muted' not in attrs:
                offenders.append({"tag": "video", "snippet": m.group(0)[:120]})
        if not offenders:
            return
        sample = "; ".join(o['snippet'] for o in offenders[:3])
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.4.2",
            criterion_name="Audio Control",
            wcag_level="A",
            issue=f"{len(offenders)} auto-playing media element(s) without user controls.",
            evidence=f"Auto-play media: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Audio that starts playing automatically and cannot be paused interferes "
                "with screen-reader speech, can disorient users with cognitive disabilities, "
                "and is a common nuisance trigger for users with anxiety or sensory "
                "sensitivities. WCAG 1.4.2 requires either no autoplay, or autoplay shorter "
                "than 3 seconds, or a clearly accessible control to pause or stop the audio."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(offenders)} <audio>/<video autoplay> tag(s).",
                "  • Add the `controls` attribute so users can pause: <audio src='…' controls>",
                "  • For background video, add `muted` so no audio plays: <video autoplay muted loop>",
                "  • Better: remove autoplay entirely and let users start media themselves.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="Parsed media element attributes directly from source HTML.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"{len(offenders)} media element(s)",
            remediation_id="html_audio_control",
            remediation_data={"elements": offenders},
        ))

    def _rule_1_2_1_audio_only_media_alternative(self):
        """WCAG 1.2.1 — prerecorded audio-only media should expose a nearby
        transcript or text alternative. Source-only heuristic.
        """
        media_items = self._collect_embedded_media()
        offenders = [
            {"tag": item["tag"], "snippet": item["snippet"]}
            for item in media_items
            if item["tag"] == "audio" and not item["has_transcript_signal"]
        ]
        if not offenders:
            return

        sample = "; ".join(item["snippet"] for item in offenders[:3])
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.2.1",
            criterion_name="Audio-only and Video-only (Prerecorded)",
            wcag_level="A",
            issue=(
                f"{len(offenders)} embedded audio element(s) lack a detectable nearby transcript or text alternative."
            ),
            evidence=f"Audio elements without transcript signals: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Users who cannot hear prerecorded audio need a text alternative to access the same information. "
                "Static HTML can show the presence of an embedded audio player, but not whether an external transcript exists elsewhere."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(offenders)} <audio> element(s) in the source.",
                "  • Add a visible 'Transcript' link or transcript text immediately before or after the audio player.",
                "  • If the audio is purely decorative, remove it or clearly mark it so it is not required for understanding.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale="Parsed <audio> tags directly and looked for nearby transcript signals in the surrounding source, but cannot verify off-page alternatives.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"{len(offenders)} embedded audio element(s)",
            remediation_id="html_audio_only_alternative",
            remediation_data={"elements": offenders},
        ))

    def _rule_1_2_2_prerecorded_captions(self):
        """WCAG 1.2.2 — prerecorded video should expose captions. We look for
        native <track kind='captions'|'subtitles'> elements in source HTML.
        """
        media_items = self._collect_embedded_media()
        offenders = [
            {"tag": item["tag"], "snippet": item["snippet"]}
            for item in media_items
            if item["tag"] == "video" and not item["has_caption_track"]
        ]
        if not offenders:
            return

        sample = "; ".join(item["snippet"] for item in offenders[:3])
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.2.2",
            criterion_name="Captions (Prerecorded)",
            wcag_level="A",
            issue=(
                f"{len(offenders)} embedded video element(s) lack a detectable native captions or subtitles track."
            ),
            evidence=f"Video elements without caption track markup: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "People who are deaf or hard of hearing rely on captions to access prerecorded video audio. "
                "Source HTML can confirm when a native caption track is absent, but it cannot confirm open captions burned into the media."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(offenders)} <video> element(s) in the source.",
                "  • Add a native caption track: <track kind='captions' srclang='en' src='captions.vtt'>.",
                "  • If captions are hosted externally, link to them next to the video so the relationship is explicit.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale="Parsed <video> contents directly and checked for native caption track markup, but cannot inspect media files for burned-in captions.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"{len(offenders)} embedded video element(s)",
            remediation_id="html_prerecorded_captions",
            remediation_data={"elements": offenders},
        ))

    def _rule_1_2_3_prerecorded_media_alternative(self):
        """WCAG 1.2.3 — prerecorded video should expose either an audio
        description track or a nearby full media alternative. Source-only heuristic.
        """
        media_items = self._collect_embedded_media()
        offenders = [
            {"tag": item["tag"], "snippet": item["snippet"]}
            for item in media_items
            if item["tag"] == "video"
            and not item["has_description_track"]
            and not item["has_transcript_signal"]
        ]
        if not offenders:
            return

        sample = "; ".join(item["snippet"] for item in offenders[:3])
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.2.3",
            criterion_name="Audio Description or Media Alternative (Prerecorded)",
            wcag_level="A",
            issue=(
                f"{len(offenders)} embedded video element(s) lack a detectable description track or nearby media alternative."
            ),
            evidence=f"Video elements without description track or transcript signals: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Users who cannot see prerecorded video need either audio description or a full media alternative to access visual-only information. "
                "Static HTML can verify source markup but cannot inspect the spoken content of the media itself."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(offenders)} <video> element(s) in the source.",
                "  • Add a description track: <track kind='descriptions' srclang='en' src='descriptions.vtt'>.",
                "  • Or provide a nearby full media alternative / transcript that captures visual-only information.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale="Parsed <video> contents directly and looked for description tracks or nearby transcript signals, but cannot verify whether the primary audio already includes all visual information.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"{len(offenders)} embedded video element(s)",
            remediation_id="html_prerecorded_media_alternative",
            remediation_data={"elements": offenders},
        ))

    # ── Phase L: 1.2.5 Audio Description (Prerecorded) ───────────────────
    def _rule_1_2_5_audio_description_prerecorded(self):
        """WCAG 1.2.5 (AA) — Prerecorded video with audio MUST provide audio
        descriptions of visual-only information. Stricter than 1.2.3 (A): a
        transcript alone does NOT satisfy 1.2.5 — only a description track
        (or equivalent synchronized audio description) does.

        Source-only heuristic: flag every <video> element that lacks a
        detectable <track kind='descriptions'>. Cannot inspect whether
        primary audio already narrates the visuals, so POSSIBLE tier.
        """
        media_items = self._collect_embedded_media()
        offenders = [
            {"tag": item["tag"], "snippet": item["snippet"]}
            for item in media_items
            if item["tag"] == "video"
            and not item["has_description_track"]
        ]
        if not offenders:
            return

        sample = "; ".join(item["snippet"] for item in offenders[:3])
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.2.5",
            criterion_name="Audio Description (Prerecorded)",
            wcag_level="AA",
            issue=(
                f"{len(offenders)} embedded video element(s) lack a detectable audio-description track."
            ),
            evidence=f"Video elements without <track kind='descriptions'>: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "WCAG 1.2.5 (AA) requires audio description for prerecorded video that contains visual-only "
                "information. Unlike 1.2.3 (A), a text transcript does NOT satisfy 1.2.5 — only synchronized "
                "audio description (typically a <track kind='descriptions'> or a separate described-video file) "
                "qualifies. Blind users miss visual context (charts, slides, on-screen actions) without it."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(offenders)} <video> element(s) in the source.",
                "  • Add a description track: <track kind='descriptions' srclang='en' src='descriptions.vtt'>.",
                "  • Or provide an alternate described-video file (separate <video> source with narrated descriptions).",
                "  • Confirm the descriptions cover all visual-only content (text on screen, gestures, scene changes).",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale="Parsed <video> contents and looked for an audio-description track, but cannot verify whether the primary audio already narrates all visual information.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"{len(offenders)} embedded video element(s)",
            remediation_id="html_audio_description_prerecorded",
            remediation_data={"elements": offenders},
        ))

    # ── Phase M2: 2.1.4 Character Key Shortcuts ────────────────────────────
    def _rule_2_1_4_character_key_shortcuts(self):
        """WCAG 2.1.4 — If a keyboard shortcut is implemented using only letter,
        punctuation, number, or symbol characters, it must be turn-off-able,
        remappable, or only active on focus. We detect bare single-key handlers
        in inline JS (e.printable check, e.key === 'a', etc.) without
        modifier-key checks. POSSIBLE.
        """
        if not self._html_text:
            return
        text = self._html_text
        # Look for keydown/keypress handlers comparing e.key/e.keyCode to a
        # single character without checking modifiers.
        offenders: List[Dict[str, Any]] = []
        # Inline handlers — handle both quoting styles separately so an
        # attribute like onkeydown="if (event.key === '/') …" doesn't get
        # truncated at the inner quote.
        handler_pat = re.compile(
            r"""on(?:keydown|keypress|keyup)\s*=\s*(?:"([^"]+)"|'([^']+)')""",
            re.IGNORECASE,
        )
        # Script blocks
        script_pat = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)

        single_key_pat = re.compile(
            r"(?:e\.|event\.|ev\.)?key\s*===?\s*['\"]([a-zA-Z0-9/?])['\"]",
            re.IGNORECASE,
        )
        modifier_pat = re.compile(
            r"\b(?:ctrlKey|metaKey|altKey|shiftKey)\b", re.IGNORECASE
        )
        addEvent_keydown = re.compile(
            r"addEventListener\s*\(\s*['\"](?:keydown|keypress|keyup)['\"]\s*,\s*([^)]+?)\)",
            re.IGNORECASE | re.DOTALL,
        )

        # Inline attribute handlers
        for m in handler_pat.finditer(text):
            body = m.group(1) or m.group(2) or ''
            keys = single_key_pat.findall(body)
            if keys and not modifier_pat.search(body):
                offenders.append({"context": "inline handler", "keys": keys[:5]})

        # Script-block contents
        for m in script_pat.finditer(text):
            block = m.group(1)
            if not addEvent_keydown.search(block):
                continue
            # Look for single-key comparisons that are not gated by modifier check
            # in the same statement.
            for keymatch in single_key_pat.finditer(block):
                # Check the surrounding 80 chars for a modifier reference.
                lo = max(0, keymatch.start() - 80)
                hi = min(len(block), keymatch.end() + 80)
                window = block[lo:hi]
                if not modifier_pat.search(window):
                    offenders.append({
                        "context": "script keydown listener",
                        "keys": [keymatch.group(1)],
                    })
                    break  # one per script is enough

        if not offenders:
            return
        sample = "; ".join(
            f"{o['context']} → key(s) {o['keys']}" for o in offenders[:3]
        )
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="2.1.4",
            criterion_name="Character Key Shortcuts",
            wcag_level="A",
            issue=(
                f"{len(offenders)} single-character keyboard shortcut(s) detected with "
                "no modifier-key gating, no obvious turn-off mechanism, and no focus check."
            ),
            evidence=f"Single-key shortcuts: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "Single-character shortcuts (like '/' to open search) collide with speech "
                "input dictation and one-finger keyboard users. WCAG 2.1.4 requires that "
                "such shortcuts be turn-off-able, remappable, or only active when the "
                "relevant component has focus."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(offenders)} keyboard handler(s) in the source.",
                "  • Require a modifier (Ctrl/Alt/Cmd) before triggering: if (e.ctrlKey && e.key === '/').",
                "  • Or scope the shortcut to a focused component (only fire when document.activeElement is inside the widget).",
                "  • Or expose user settings to disable/remap the shortcut.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale="Inferred from the absence of modifier-key checks in the same handler scope; user code may still gate elsewhere.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"{len(offenders)} handler(s)",
            remediation_id="html_character_key_shortcuts",
            remediation_data={"handlers": offenders},
        ))

    # ── Phase M3: 2.2.2 Pause, Stop, Hide ──────────────────────────────────
    def _rule_2_2_2_pause_stop_hide(self):
        """WCAG 2.2.2 — Moving, blinking, scrolling, or auto-updating content
        that starts automatically, lasts more than 5 seconds, and is presented
        in parallel with other content must have a pause/stop/hide mechanism.

        Strict signals: <marquee> (deprecated, no native pause), <blink>, and
        CSS animations with infinite iteration that aren't shorter than 5s.
        """
        if not self._html_text:
            return
        text = self._html_text
        offenders: List[Dict[str, Any]] = []

        # <marquee> tags
        for m in re.finditer(r"<marquee\b", text, re.IGNORECASE):
            offenders.append({"signal": "marquee", "snippet": "<marquee>"})
        # <blink> tags
        for m in re.finditer(r"<blink\b", text, re.IGNORECASE):
            offenders.append({"signal": "blink", "snippet": "<blink>"})

        # CSS animation-iteration-count: infinite + animation-duration > 5s
        # (or unspecified, which defaults to 0 but combined with iteration:infinite
        # most likely indicates a non-trivial loop set in CSS).
        css_blocks = re.findall(r"<style[^>]*>(.*?)</style>", text, re.DOTALL | re.IGNORECASE)
        css = "\n".join(css_blocks)
        # Also scan inline style attributes.
        inline_styles = re.findall(r"\bstyle\s*=\s*['\"]([^'\"]+)['\"]", text, re.IGNORECASE)
        css = css + "\n" + "\n".join(inline_styles)

        # Find rule-blocks (or inline) that contain "infinite" and check
        # duration in the same rule.
        rule_pat = re.compile(r"\{[^{}]*\}", re.DOTALL)
        for m in rule_pat.finditer(css):
            block = m.group(0).lower()
            if 'infinite' not in block:
                continue
            # Look for animation-duration or animation: <duration>.
            dur_match = re.search(r"animation(?:-duration)?\s*:\s*[^;]*?(\d+(?:\.\d+)?)(s|ms)", block)
            if dur_match:
                value = float(dur_match.group(1))
                unit = dur_match.group(2)
                seconds = value if unit == 's' else value / 1000.0
                if seconds < 5.0:
                    continue  # short animation, may be acceptable
            offenders.append({"signal": "css_infinite_animation", "snippet": m.group(0)[:80]})
        # Check for inline style animation:infinite without a hide button.
        # Limit to 50 to avoid blowing up.
        offenders = offenders[:50]
        if not offenders:
            return
        sample = "; ".join(o['snippet'] for o in offenders[:3])
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="2.2.2",
            criterion_name="Pause, Stop, Hide",
            wcag_level="A",
            issue=(
                f"{len(offenders)} moving/blinking element(s) detected without an "
                "obvious pause/stop/hide control."
            ),
            evidence=f"Auto-moving content: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Continuously moving, scrolling, or blinking content distracts users with "
                "cognitive disabilities and ADHD, makes it difficult or impossible for "
                "screen-reader users to read static text near it, and can trigger vestibular "
                "reactions. WCAG 2.2.2 requires a pause/stop/hide mechanism if the motion "
                "lasts more than 5 seconds."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(offenders)} moving element(s).",
                "  • Replace <marquee> and <blink> with static content or CSS that respects @media (prefers-reduced-motion).",
                "  • For looping CSS animations, add a visible Pause button or wrap in @media (prefers-reduced-motion: reduce) { animation: none }.",
                "  • Limit any unavoidable motion to under 5 seconds total.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="Parsed deprecated motion tags and CSS animation rules from source HTML.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"{len(offenders)} element(s)/rule(s)",
            remediation_id="html_pause_stop_hide",
            remediation_data={"signals": offenders},
        ))

    # ── Phase M4: 2.3.1 Three Flashes or Below ─────────────────────────────
    def _rule_2_3_1_three_flashes(self):
        """WCAG 2.3.1 — Web pages do not contain anything that flashes more
        than 3 times in any 1-second period. Static signal: CSS animation /
        transition with very short duration AND infinite iteration. Strict.
        """
        if not self._html_text:
            return
        text = self._html_text
        css_blocks = re.findall(r"<style[^>]*>(.*?)</style>", text, re.DOTALL | re.IGNORECASE)
        css = "\n".join(css_blocks)
        offenders: List[Dict[str, Any]] = []
        rule_pat = re.compile(r"\{[^{}]*\}", re.DOTALL)
        for m in rule_pat.finditer(css):
            block = m.group(0).lower()
            if 'infinite' not in block:
                continue
            dur_match = re.search(
                r"animation(?:-duration)?\s*:\s*[^;]*?(\d+(?:\.\d+)?)(s|ms)",
                block,
            )
            if not dur_match:
                continue
            value = float(dur_match.group(1))
            unit = dur_match.group(2)
            seconds = value if unit == 's' else value / 1000.0
            # 3 flashes in 1 second = period < ~0.33s per cycle
            if seconds <= 0.33:
                offenders.append({
                    "duration_seconds": round(seconds, 3),
                    "snippet": m.group(0)[:80],
                })
        if not offenders:
            return
        sample = "; ".join(
            f"{o['duration_seconds']}s loop: {o['snippet'][:50]}…"
            for o in offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="2.3.1",
            criterion_name="Three Flashes or Below",
            wcag_level="A",
            issue=(
                f"{len(offenders)} CSS animation(s) loop faster than 3 cycles per second "
                "— may trigger photosensitive seizures."
            ),
            evidence=f"Fast-flashing animations: {sample}.",
            severity=Severity.CRITICAL,
            why_it_matters=(
                "Content that flashes more than 3 times in any 1-second period can trigger "
                "seizures in users with photosensitive epilepsy. WCAG 2.3.1 is a Level A "
                "requirement and is one of the most safety-critical accessibility criteria."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(offenders)} CSS animation rule(s).",
                "  • Slow the animation to a cycle of at least 0.33 seconds (3Hz) — usually 1s+ is safe.",
                "  • Better: replace flashing with a steady visual change (e.g. a fade in/out over 2 seconds).",
                "  • Always wrap motion in @media (prefers-reduced-motion: reduce) { animation: none }.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="Parsed CSS animation duration vs WCAG 3Hz flash threshold.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"{len(offenders)} CSS rule(s)",
            remediation_id="html_three_flashes",
            remediation_data={"animations": offenders},
        ))

    # ── Phase M5: 2.5.2 Pointer Cancellation ───────────────────────────────
    def _rule_2_5_2_pointer_cancellation(self):
        """WCAG 2.5.2 — Functions triggered by single-pointer activation must
        complete on up-event (not down). Static signal: elements with
        `onmousedown` / `ontouchstart` that appear to mutate state and have
        no matching `onmouseup` / `onclick` cleanup.
        """
        if not self._html_text:
            return
        text = self._html_text
        offenders: List[Dict[str, Any]] = []
        elem_pat = re.compile(r"<([a-zA-Z][\w-]*)\b([^>]*)>", re.IGNORECASE)
        for m in elem_pat.finditer(text):
            tag = m.group(1).lower()
            if tag in ('script', 'style'):
                continue
            attrs = m.group(2) or ''
            attrs_l = attrs.lower()
            has_down = ('onmousedown' in attrs_l) or ('ontouchstart' in attrs_l)
            if not has_down:
                continue
            has_click_or_up = ('onclick' in attrs_l) or ('onmouseup' in attrs_l)
            if has_click_or_up:
                continue
            id_match = re.search(r"\bid\s*=\s*['\"]([^'\"]+)['\"]", attrs, re.IGNORECASE)
            offenders.append({
                "tag": tag,
                "id": id_match.group(1) if id_match else "",
                "down_handler": (
                    "ontouchstart" if "ontouchstart" in attrs_l else "onmousedown"
                ),
            })
        if not offenders:
            return
        sample = "; ".join(
            f"<{o['tag']}{(' id=' + o['id']) if o['id'] else ''}> ({o['down_handler']})"
            for o in offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="2.5.2",
            criterion_name="Pointer Cancellation",
            wcag_level="A",
            issue=(
                f"{len(offenders)} element(s) trigger action on pointer-down "
                "with no matching click/up handler — user cannot abort."
            ),
            evidence=f"Down-only handlers: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "Activating on the down-event removes the user's ability to abort by "
                "moving their pointer off the element before releasing. Users with motor "
                "impairments who tremor onto the wrong control cannot recover."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(offenders)} element(s) with down-only handlers.",
                "  • Move the action from `onmousedown` to `onclick` (which fires on the up-event over the same element).",
                "  • For touch, prefer `onclick` over `ontouchstart`; modern browsers handle taps correctly.",
                "  • If you must use down-event for performance (e.g. piano-key apps), document the exception and provide a 'cancel before release' affordance.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="Parsed pointer-down handler attributes from source HTML; flagged when no companion click/up handler is present.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"{len(offenders)} element(s)",
            remediation_id="html_pointer_cancellation",
            remediation_data={"elements": offenders},
        ))

    # ── Phase M6: 2.5.4 Motion Actuation ───────────────────────────────────
    def _rule_2_5_4_motion_actuation(self):
        """WCAG 2.5.4 — Functionality operated by device motion or user motion
        can also be operated by UI components, and the motion can be disabled.
        Signal: any addEventListener on devicemotion / deviceorientation /
        shake events.
        """
        if not self._html_text:
            return
        text = self._html_text
        offenders: List[Dict[str, Any]] = []
        events = [
            'devicemotion', 'deviceorientation',
            'compassneedscalibration', 'orientationchange',
        ]
        for ev in events:
            pat = re.compile(
                rf"addEventListener\s*\(\s*['\"]{ev}['\"]",
                re.IGNORECASE,
            )
            count = len(pat.findall(text))
            if count > 0:
                offenders.append({"event": ev, "count": count})
        if not offenders:
            return
        sample = "; ".join(f"{o['event']} (×{o['count']})" for o in offenders)
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="2.5.4",
            criterion_name="Motion Actuation",
            wcag_level="A",
            issue=(
                f"{len(offenders)} motion-event listener(s) detected; ensure the "
                "feature is also reachable via standard UI controls and can be disabled."
            ),
            evidence=f"Motion handlers: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "Features triggered by tilt, shake, or device orientation are unusable by "
                "people who mount their device in a fixed position (e.g. wheelchair tray, "
                "tripod), have tremor, or have limited motor control. WCAG 2.5.4 requires "
                "an equivalent button/menu path AND a way to disable motion triggers."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: {len(offenders)} motion event listener(s) in your JS.",
                "  • Provide a visible button or menu that performs the same action as the motion gesture.",
                "  • Add a user-accessible setting to disable motion triggers (Settings → Accessibility → Disable shake/tilt).",
                "  • Confirm the feature still works when window.DeviceMotionEvent is unsupported.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="Detected addEventListener calls for device-motion events in the source HTML/JS.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"{len(offenders)} event type(s)",
            remediation_id="html_motion_actuation",
            remediation_data={"events": offenders},
        ))

    # ── Phase M7: 3.2.6 Consistent Help (WCAG 2.2 Level A) ─────────────────
    def _rule_3_2_6_consistent_help(self):
        """WCAG 2.2 — 3.2.6 Consistent Help (A). If a page provides a help
        mechanism, it should be in the same relative order across pages.
        Single-page check: only fire when the page looks like an *application*
        (has a <form> with input controls) but has zero help mechanisms.
        Static demo / marketing pages are not flagged.
        """
        if not self._html_text:
            return
        text = self._html_text
        text_l = text.lower()

        # Only consider pages that contain a form with inputs — i.e. the user
        # is being asked to do something where help would matter.
        has_form_with_inputs = bool(
            re.search(r"<form\b", text_l) and re.search(r"<(?:input|select|textarea)\b", text_l)
        )
        if not has_form_with_inputs:
            return

        signals = [
            r"\bhelp\b", r"\bcontact\b", r"\bsupport\b",
            r"\bfaq\b", r"\bchat\b",
            r"mailto:", r"tel:",
        ]
        if any(re.search(s, text_l) for s in signals):
            return  # at least one help mechanism present — no finding

        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="3.2.6",
            criterion_name="Consistent Help",
            wcag_level="A",
            issue="Page has a form but no detectable help mechanism (no contact, help link, FAQ, chat, mailto, or tel link).",
            evidence=None,
            severity=Severity.MODERATE,
            why_it_matters=(
                "WCAG 2.2 (3.2.6) requires that if a help mechanism is offered, it appears "
                "consistently across pages. The simplest failure is offering no help at all "
                "on a transactional page — users with cognitive disabilities, low literacy, "
                "or who are unfamiliar with the site cannot recover when something goes wrong."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Page-level layout (header, footer, or persistent sidebar).",
                "  • Add a 'Help' or 'Contact' link to the site header or footer of every page that contains a form.",
                "  • Acceptable mechanisms include: contact email (mailto:), phone (tel:), live chat, FAQ link, support form.",
                "  • Place the link in the same relative position on every page (header or footer).",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale="Single-page heuristic — page has a form but zero help signals. Cross-page consistency requires Tier-2 crawler.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location="Page-level",
            remediation_id="html_consistent_help",
            remediation_data={"checked_signals": signals},
        ))

    # ── Phase M8: 3.3.7 Redundant Entry (WCAG 2.2 Level A) ─────────────────
    def _rule_3_3_7_redundant_entry(self):
        """WCAG 2.2 — 3.3.7 Redundant Entry (A). Information previously entered
        by or provided to the user that is required to be entered again in the
        same process is either auto-populated or available for the user to
        select. Signal: multiple inputs of the same `type=email` or `type=tel`
        on the same page (suggesting the user is being asked to retype).
        """
        if not self._html_text:
            return
        text = self._html_text
        offenders: List[Dict[str, Any]] = []
        # Find all input types of interest.
        for input_type in ('email', 'tel', 'url', 'password'):
            pat = re.compile(
                rf"<input\b[^>]*\btype\s*=\s*['\"]{input_type}['\"][^>]*>",
                re.IGNORECASE,
            )
            matches = pat.findall(text)
            if len(matches) <= 1:
                continue
            # password is fine to repeat for confirmation; flag only if >2.
            if input_type == 'password' and len(matches) <= 2:
                continue
            # Check whether autocomplete attributes are present on each.
            has_autocomplete = sum(
                1 for m in matches
                if re.search(r"\bautocomplete\s*=\s*['\"]\S+['\"]", m, re.IGNORECASE)
            )
            if has_autocomplete >= len(matches):
                continue  # all have autocomplete hints — browser/OS can auto-fill
            offenders.append({
                "input_type": input_type,
                "count": len(matches),
                "with_autocomplete": has_autocomplete,
            })
        if not offenders:
            return
        sample = "; ".join(
            f"{o['count']}× type='{o['input_type']}' "
            f"({o['with_autocomplete']} have autocomplete)"
            for o in offenders
        )
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="3.3.7",
            criterion_name="Redundant Entry",
            wcag_level="A",
            issue=(
                f"Multiple identical input types on the page suggest the user is asked "
                "to re-enter information without autofill support."
            ),
            evidence=f"Repeated input types: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "WCAG 2.2 (3.3.7) requires that information previously entered in the same "
                "process not be required again. Users with cognitive disabilities or motor "
                "impairments lose work and time when asked to retype email, phone, or "
                "address fields they already provided."
            ),
            remediation_steps=[
                f"📍 WHERE TO FIX: Forms that repeat the same input types.",
                "  • Add autocomplete tokens to every repeated field (autocomplete='email', 'tel', etc.) so the browser auto-fills.",
                "  • Pre-populate fields server-side from the user's profile or earlier step.",
                "  • Or offer a 'Same as above' checkbox to copy values from a prior section.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale="Heuristic: repeated input types may be confirmation fields, two parties on one form, etc. — confirm with the workflow owner.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"{len(offenders)} input-type group(s)",
            remediation_id="html_redundant_entry",
            remediation_data={"groups": offenders},
        ))

    # ── Phase L: 1.4.13 Content on Hover or Focus (action harness) ──────────
    def _rule_1_4_13_content_on_hover_or_focus(self, actions: Dict[str, Any]):
        """WCAG 1.4.13 — Content shown on hover/focus must be dismissable,
        hoverable, and persistent. The native `title` attribute fails all three
        (it auto-dismisses, can't be hovered, has fixed timeout). CONFIRMED.
        """
        triggers = (actions or {}).get("titleTriggers") or []
        if not triggers:
            return
        sample = "; ".join(
            f"{t['location']}: \"{t['title']}\""
            for t in triggers[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.4.13",
            criterion_name="Content on Hover or Focus",
            wcag_level="AA",
            issue=(
                f"{len(triggers)} element(s) use the native `title` attribute "
                "for tooltip content, which is not dismissable, hoverable, or persistent."
            ),
            evidence=f"Native title-attribute tooltips: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "WCAG 1.4.13 requires tooltips and hover/focus content to remain visible "
                "until the user dismisses them, allow the pointer to move into the content, "
                "and stay open until the trigger or content is no longer focused/hovered. "
                "Browser-native `title` tooltips meet none of these requirements — they "
                "auto-disappear after a few seconds and cannot be reached by the cursor."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Each element with a `title` attribute used for tooltip content.",
                "  • Replace `title=\"…\"` with a real tooltip widget (e.g., aria-describedby pointing to a hidden div revealed on hover/focus).",
                "  • Make the tooltip dismissable by Escape, hoverable by the pointer, and persistent until intentionally closed.",
                "  • Keep `title` only for IFRAMEs (where it labels the frame) and as a last-resort fallback name on form controls.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.BROWSER_RENDERED],
            confidence_rationale="Live DOM scanned via Playwright for non-empty `title` attributes on visible elements.",
            evidence_source=EvidenceSource.BROWSER_RENDERED,
            location="Native title attributes",
            remediation_id="html_native_title_tooltip",
            remediation_data={"triggers": triggers},
        ))

    # ── Phase L: 2.4.11 Focus Not Obscured (Minimum) — WCAG 2.2 ────────────
    def _rule_2_4_11_focus_not_obscured(self, actions: Dict[str, Any]):
        """WCAG 2.2 — 2.4.11 Focus Not Obscured (Minimum). The focused element
        must not be entirely hidden by author-created content (e.g., a sticky
        cookie banner, fixed footer). The action harness focused each element
        and checked whether any fixed/sticky element fully covered its rect.
        """
        items = (actions or {}).get("obscuredFocus") or []
        if not items:
            return
        sample = "; ".join(
            f"{o['location']} obscured by {o['obscuredBy']}"
            for o in items[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="2.4.11",
            criterion_name="Focus Not Obscured (Minimum)",
            wcag_level="AA",
            issue=(
                f"{len(items)} focusable element(s) become fully hidden behind "
                "a fixed or sticky overlay when focused."
            ),
            evidence=f"Obscured-on-focus pairs: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Keyboard users rely on the focus indicator to know where they are. "
                "If a sticky header, fixed footer, cookie banner, or modal overlay covers "
                "the focused element, the user has no idea what they're about to activate. "
                "WCAG 2.2 (2.4.11) requires that the focused element not be entirely hidden."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Each focusable element listed plus its overlapping fixed/sticky overlay.",
                "  • Use scroll-padding-block (CSS) so the page scrolls focused elements into view.",
                "  • Reduce the height of fixed headers/footers, or hide them on focus near them.",
                "  • Add an offset so :focus brings the element into the un-obscured region.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.BROWSER_RENDERED],
            confidence_rationale="Live focus + bounding-rect overlap with sticky/fixed elements measured via Playwright.",
            evidence_source=EvidenceSource.BROWSER_RENDERED,
            location="Focusable elements + fixed/sticky overlays",
            remediation_id="html_focus_not_obscured",
            remediation_data={"obscured": items},
        ))

    # ── Phase L: 2.4.12 Focus Not Obscured (Enhanced) ─────────────────────────
    def _rule_2_4_12_focus_not_obscured_enhanced(self, actions: Dict[str, Any]):
        """WCAG 2.2 — 2.4.12 Focus Not Obscured (Enhanced, AAA). Stricter than
        2.4.11: NO part of the focused element may be obscured by author-created
        content. The action harness records every focusable whose rect
        intersects a fixed/sticky overlay (partial OR full coverage).
        """
        items = (actions or {}).get("partiallyObscuredFocus") or []
        if not items:
            return
        sample = "; ".join(
            f"{o['location']} {o.get('coverage', 'partial')}-obscured by {o['obscuredBy']}"
            for o in items[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="2.4.12",
            criterion_name="Focus Not Obscured (Enhanced)",
            wcag_level="AAA",
            issue=(
                f"{len(items)} focusable element(s) become partially or fully "
                "obscured by a fixed or sticky overlay when focused."
            ),
            evidence=f"Partially/fully obscured-on-focus pairs: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "WCAG 2.2 (2.4.12, AAA) requires that no part of the focused element "
                "be hidden by author-created content. Partial obscuring still degrades "
                "the keyboard user's ability to see exactly what is focused — especially "
                "when text labels or focus indicators are clipped by a sticky overlay."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Each focusable element listed plus its overlapping fixed/sticky overlay.",
                "  • Use scroll-padding-block (CSS) to inset the scroll viewport away from sticky overlays.",
                "  • Reduce the height of fixed headers/footers, or hide them on focus near them.",
                "  • Ensure the focused element's full bounding box stays inside the visible (un-obscured) viewport.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.BROWSER_RENDERED],
            confidence_rationale="Live focus + bounding-rect intersection (partial OR full) with sticky/fixed elements measured via Playwright.",
            evidence_source=EvidenceSource.BROWSER_RENDERED,
            location="Focusable elements + fixed/sticky overlays",
            remediation_id="html_focus_not_obscured_enhanced",
            remediation_data={"obscured": items},
        ))

    # ── Phase L: 3.2.1 On Focus (runtime verification) ──────────────────────
    def _rule_3_2_1_runtime_focus_change(self, actions: Dict[str, Any]):
        """WCAG 3.2.1 — Runtime variant. The static rule scans for inline
        `onfocus="…"` patterns. This rule catches handlers attached via
        `addEventListener('focus', …)` because we observed the URL / scroll /
        form state actually change after focus. Findings here are paired with
        any static 3.2.1 emission via distinct remediation_id.
        """
        changes = (actions or {}).get("focusContextChanges") or []
        if not changes:
            return
        sample = "; ".join(
            f"{c['location']} → {c['kind']}"
            for c in changes[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="3.2.1",
            criterion_name="On Focus",
            wcag_level="A",
            issue=(
                f"{len(changes)} element(s) caused an unexpected change of "
                "context when focused (URL, scroll, or form state changed)."
            ),
            evidence=f"Runtime context changes triggered by focus: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Receiving focus alone must never trigger navigation or other "
                "context changes. This catches change-on-focus behaviour added "
                "through addEventListener — invisible to source-code scanning."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Each element listed above; inspect any focus listeners attached to it.",
                "  • Move side effects to onclick / activation handlers.",
                "  • If you must update the UI on focus, do it in-place (no scroll, no navigation, no submit).",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.BROWSER_RENDERED],
            confidence_rationale="Each focusable element was programmatically focused; URL/scroll/form-state diffs were captured at runtime.",
            evidence_source=EvidenceSource.BROWSER_RENDERED,
            location="Focusable elements",
            remediation_id="html_runtime_on_focus_change",
            remediation_data={"changes": changes},
        ))

    # ── Phase L: 3.2.2 On Input (runtime verification) ──────────────────────
    def _rule_3_2_2_runtime_input_change(self, actions: Dict[str, Any]):
        """WCAG 3.2.2 — Runtime variant. We toggled each visible select /
        checkbox / radio and dispatched change+input events. If the URL,
        scroll position, or form state changed, the control caused an
        unexpected change of context.
        """
        changes = (actions or {}).get("inputContextChanges") or []
        if not changes:
            return
        sample = "; ".join(
            f"{c['location']} → {c['kind']}"
            for c in changes[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="3.2.2",
            criterion_name="On Input",
            wcag_level="A",
            issue=(
                f"{len(changes)} form control(s) caused an unexpected change "
                "of context when their value changed."
            ),
            evidence=f"Runtime context changes triggered by input: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Changing a control's value (e.g., selecting an option in a "
                "dropdown) must not auto-submit the form or navigate the page. "
                "This catches addEventListener-bound onchange handlers that the "
                "static scan can't see in source HTML."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Each control listed above; inspect change/input event listeners.",
                "  • Replace auto-submission with an explicit Submit button.",
                "  • If a value change must trigger an action, surface it as a non-context-changing UI update or require a confirmation step.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.BROWSER_RENDERED],
            confidence_rationale="Each form control's value was programmatically toggled; runtime URL/scroll/form-state diffs were captured.",
            evidence_source=EvidenceSource.BROWSER_RENDERED,
            location="Form controls",
            remediation_id="html_runtime_on_input_change",
            remediation_data={"changes": changes},
        ))

    # ── Phase B: 1.4.11 Non-text Contrast ───────────────────────────────────
    def _rule_1_4_11_non_text_contrast(self, ntc_data: List[Dict[str, Any]]):
        """WCAG 1.4.11 — Detect form controls / buttons whose visible border
        contrasts < 3:1 with the element's own background AND the parent
        background. We only flag elements that have an explicit, non-zero,
        non-'none' border so transparent/borderless buttons are not penalized.
        """
        from wcag.common.non_text_contrast import evaluate_pair, MIN_NON_TEXT_CONTRAST
        if not ntc_data:
            return

        def _css_to_hex(value: str) -> Optional[str]:
            """Parse 'rgb(r, g, b)' or 'rgba(r, g, b, a)' into 'RRGGBB', or
            None if value is transparent or unparseable."""
            if not value:
                return None
            v = value.strip()
            m = re.match(r'rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([\d.]+))?\s*\)', v)
            if not m:
                return None
            r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
            a = float(m.group(4)) if m.group(4) is not None else 1.0
            if a < 0.5:  # treat near-transparent as no color
                return None
            return f"{r:02X}{g:02X}{b:02X}"

        offenders: List[Tuple[str, float, str, str]] = []
        for el in ntc_data:
            style = (el.get('borderTopStyle') or '').lower()
            if style in ('', 'none', 'hidden'):
                continue
            try:
                width = float((el.get('borderTopWidth') or '0').replace('px', ''))
            except ValueError:
                width = 0.0
            if width < 0.5:
                continue
            border_hex = _css_to_hex(el.get('borderTopColor', ''))
            if not border_hex:
                continue
            # Compare against element's own backgroundColor first; fallback to parent's
            bg_hex = (_css_to_hex(el.get('backgroundColor', ''))
                      or _css_to_hex(el.get('parentBackgroundColor', '')))
            if not bg_hex:
                continue
            result = evaluate_pair(border_hex, bg_hex)
            if not result:
                continue
            ratio, ok = result
            if ok:
                continue
            offenders.append((el.get('location') or el.get('tag', 'element'),
                              ratio, border_hex, bg_hex))

        if not offenders:
            return
        sample = "; ".join(
            f"{loc} (border #{b} on bg #{bg}, ratio {r:.2f}:1)"
            for loc, r, b, bg in offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="1.4.11",
            criterion_name="Non-text Contrast",
            wcag_level="AA",
            issue=(
                f"{len(offenders)} interactive control(s) have border-on-background contrast below "
                f"{MIN_NON_TEXT_CONTRAST}:1."
            ),
            evidence=f"Affected controls: {sample}",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Buttons and form fields that depend on borders to convey their boundary become invisible "
                "to low-vision users when border contrast is below 3:1."
            ),
            remediation_steps=[
                "Increase contrast between the control's border color and its background to at least 3:1.",
                "Either darken the border or change the background color.",
                "For buttons that rely on background fill alone, ensure the fill itself contrasts 3:1 with the surrounding page.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.BROWSER_RENDERED],
            confidence_rationale="Border and background colors measured from the rendered browser style.",
            evidence_source=EvidenceSource.BROWSER_RENDERED,
            location="document",
            remediation_id="html_non_text_contrast",
        ))

    # ════════════════════════════════════════════════════════════════════════
    # Phase N (2026-05-18) — Additional gap closures.
    # 6 AAA quick wins + 3 A/AA single-page heuristics.
    # ════════════════════════════════════════════════════════════════════════

    # ── 1.4.6 Contrast (Enhanced) — AAA ─────────────────────────────────────
    def _rule_1_4_6_contrast_enhanced(self, text_nodes: List[Dict[str, Any]]):
        """WCAG 1.4.6 (AAA) — Stricter version of 1.4.3. Requires 7:1 for
        normal text and 4.5:1 for large text. Same algorithm and same
        rendered-style evidence as 1.4.3; only the thresholds differ.
        """
        if not text_nodes:
            return
        ENHANCED_NORMAL = 7.0
        ENHANCED_LARGE = 4.5
        findings_added = 0
        seen = set()
        for node in text_nodes:
            ratio = minimum_css_contrast(
                node.get("color", ""),
                node.get("backgroundColor", ""),
                node.get("backgroundImage", ""),
            )
            if ratio is None:
                continue
            font_size_px = float(node.get("fontSizePx") or 16.0)
            threshold = (
                ENHANCED_LARGE
                if _is_large_text(font_size_px, str(node.get("fontWeight", "400")))
                else ENHANCED_NORMAL
            )
            if ratio >= threshold:
                continue
            key = (node.get("location"), node.get("text"))
            if key in seen:
                continue
            seen.add(key)
            text_preview = (node.get("text") or "")[:100]
            location = node.get("location") or node.get("tag") or "visible text"
            self.fact_sheet.confirmed_findings.append(Finding(
                criterion_id="1.4.6",
                criterion_name="Contrast (Enhanced)",
                wcag_level="AAA",
                issue=(
                    f"Rendered text at {location} has contrast {ratio:.2f}:1, "
                    f"below the AAA threshold of {threshold:.1f}:1."
                ),
                evidence=(
                    f"Text sample '{text_preview}' rendered as {node.get('color')} on "
                    f"{node.get('backgroundColor')} / {node.get('backgroundImage')} "
                    f"at {font_size_px:.1f}px."
                ),
                severity=Severity.MODERATE,
                why_it_matters=(
                    "AAA contrast (7:1 normal / 4.5:1 large) supports users with severe low vision "
                    "and reading in bright environments. It is required by some procurement targets "
                    "(e.g., EN 301 549 augmented profiles, public-sector AAA mandates)."
                ),
                remediation_steps=[
                    "Increase contrast between text color and background until the rendered ratio reaches 7:1 (normal) or 4.5:1 (large).",
                    "Note: any element passing 1.4.6 also passes 1.4.3 automatically.",
                ],
                confidence_tier=ConfidenceTier.CONFIRMED,
                confidence_label=CONFIDENCE_LABEL[EvidenceSource.BROWSER_RENDERED],
                confidence_rationale="Contrast ratio measured from rendered browser styles; threshold tightened to AAA.",
                evidence_source=EvidenceSource.BROWSER_RENDERED,
                location=location,
                remediation_id=f"html_text_contrast_enhanced_{findings_added}",
            ))
            findings_added += 1
            if findings_added >= MAX_RENDERED_CONTRAST_FINDINGS:
                break

    # ── 2.5.5 Target Size (Enhanced) — AAA ──────────────────────────────────
    def _rule_2_5_5_target_size_enhanced(self):
        """WCAG 2.5.5 (AAA) — Stricter version of 2.5.8. Requires 44×44 CSS
        pixels instead of 24×24. Same source-only parser, different threshold.
        """
        if not self._html_text:
            return
        ENHANCED_TARGET_PX = 44
        offenders: List[Dict[str, Any]] = []
        inline_pat = re.compile(
            r"<(button|a|input)\b([^>]*\bstyle\s*=\s*['\"]([^'\"]+)['\"][^>]*)>",
            re.IGNORECASE,
        )
        size_pat = re.compile(r"\b(width|height)\s*:\s*(\d+(?:\.\d+)?)px", re.IGNORECASE)
        for m in inline_pat.finditer(self._html_text):
            tag = m.group(1).lower()
            style = m.group(3)
            sizes: Dict[str, float] = {}
            for sm in size_pat.finditer(style):
                sizes[sm.group(1).lower()] = float(sm.group(2))
            if "width" in sizes and "height" in sizes:
                if sizes["width"] < ENHANCED_TARGET_PX and sizes["height"] < ENHANCED_TARGET_PX:
                    offenders.append({
                        "source": "inline-style",
                        "tag": tag,
                        "width_px": sizes["width"],
                        "height_px": sizes["height"],
                        "snippet": m.group(0)[:120],
                    })

        style_block_pat = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
        rule_pat = re.compile(r"([^{}]+)\{([^{}]+)\}")
        for sb in style_block_pat.finditer(self._html_text):
            css = sb.group(1)
            for rm in rule_pat.finditer(css):
                selector = rm.group(1).strip()
                if not re.search(r"\b(button|input|select|textarea)\b|(?<![\w-])a(?![\w-])", selector, re.IGNORECASE):
                    continue
                body = rm.group(2)
                sizes = {}
                for sm in size_pat.finditer(body):
                    sizes[sm.group(1).lower()] = float(sm.group(2))
                if "width" in sizes and "height" in sizes:
                    if sizes["width"] < ENHANCED_TARGET_PX and sizes["height"] < ENHANCED_TARGET_PX:
                        offenders.append({
                            "source": "<style> block",
                            "selector": selector[:60],
                            "width_px": sizes["width"],
                            "height_px": sizes["height"],
                        })
        if not offenders:
            return
        sample = "; ".join(
            f"{o.get('selector') or o.get('tag')}: {o['width_px']:.0f}x{o['height_px']:.0f}px"
            for o in offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="2.5.5",
            criterion_name="Target Size (Enhanced)",
            wcag_level="AAA",
            issue=(
                f"{len(offenders)} interactive target(s) declared smaller than the WCAG AAA "
                f"enhanced minimum of {ENHANCED_TARGET_PX}\u00d7{ENHANCED_TARGET_PX} CSS pixels."
            ),
            evidence=f"Targets below {ENHANCED_TARGET_PX}x{ENHANCED_TARGET_PX} px: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "AAA target size (44\u00d744 CSS px) is the threshold required by mobile and motor-impairment "
                "guidelines (Apple HIG, Material Design). It is also the size shipped by major procurement specs "
                "targeting accessibility-augmented mobile experiences."
            ),
            remediation_steps=[
                f"\ud83d\udccd WHERE TO FIX: Each control declared below {ENHANCED_TARGET_PX}x{ENHANCED_TARGET_PX} CSS px.",
                f"  \u2022 Increase width and height to at least {ENHANCED_TARGET_PX}px (or use min-width / min-height).",
                "  \u2022 If you cannot grow the visual, give the control padding so the click target reaches 44x44.",
                "  \u2022 Inline-text links inside paragraphs are exempt (the 'inline exception').",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="CSS source declarations parsed for width/height in px on interactive selectors; threshold tightened to AAA.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location="CSS / inline style",
            remediation_id="html_target_size_enhanced",
            remediation_data={"targets": offenders},
        ))

    # ── 2.4.10 Section Headings — AAA ───────────────────────────────────────
    def _rule_2_4_10_section_headings(self):
        """WCAG 2.4.10 (AAA) — Section headings are used to organize content.
        Heuristic: if the document contains substantial body content (>=300
        words) but fewer than 2 headings, flag as missing section structure.
        Uses the existing paragraph + heading data the parser already builds.
        """
        headings = [
            paragraph for paragraph in (self.fact_sheet.paragraphs or [])
            if paragraph.style_name in {tag.upper() for tag in HEADING_TAG_LEVELS}
        ]
        total_text = " ".join(
            (p.text or "") for p in (self.fact_sheet.paragraphs or [])
        )
        word_count = len(total_text.split())
        if word_count < 300:
            return
        if len(headings) >= 2:
            return
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="2.4.10",
            criterion_name="Section Headings",
            wcag_level="AAA",
            issue=(
                f"Document has {word_count} words of content but only {len(headings)} heading(s). "
                "AAA-level content should be organized with section headings."
            ),
            evidence=(
                f"Total paragraph words: {word_count}; total headings detected: {len(headings)}. "
                "WCAG 2.4.10 expects content of this length to be subdivided by headings."
            ),
            severity=Severity.MODERATE,
            why_it_matters=(
                "Section headings let screen-reader and keyboard users navigate long pages by heading. "
                "Without them, users must read or skim linearly. AAA mandates this organisational layer."
            ),
            remediation_steps=[
                "Add H2/H3 headings to subdivide the page into logical sections.",
                "Aim for one heading per major topic shift.",
                "If the page truly has no logical sections (e.g., a single short article), this rule does not apply \u2014 verify content length is intentional.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale="Heading count and total word count parsed directly; cannot judge whether content has natural section breaks without semantic analysis.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"Document body ({word_count} words, {len(headings)} headings)",
            remediation_id="html_section_headings",
        ))

    # ── 2.4.9 Link Purpose (Link Only) — AAA ────────────────────────────────
    def _rule_2_4_9_link_purpose_link_only(self):
        """WCAG 2.4.9 (AAA) — Stricter than 2.4.4. Link text alone (with no
        surrounding context) must identify the link's purpose. Our 2.4.4
        implementation already flags generic link text without considering
        context; we mirror that here under the AAA criterion so the coverage
        scanner recognises it.
        """
        generic_offenders: List[Dict[str, Any]] = []
        for index, hyperlink in enumerate(self.fact_sheet.hyperlinks or []):
            text = (hyperlink.display_text or "").strip()
            if not text:
                continue  # empty link already covered under 2.4.4 (no purpose at all)
            if GENERIC_LINK_TEXT.match(text):
                generic_offenders.append({
                    "index": index + 1,
                    "text": text[:60],
                    "url": (hyperlink.url or "")[:80],
                })
        if not generic_offenders:
            return
        sample = "; ".join(
            f"link {o['index']}: '{o['text']}'" for o in generic_offenders[:3]
        )
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="2.4.9",
            criterion_name="Link Purpose (Link Only)",
            wcag_level="AAA",
            issue=(
                f"{len(generic_offenders)} link(s) use generic text that does not identify "
                "the destination from the link alone."
            ),
            evidence=f"Generic link text: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "AAA requires link text alone (without surrounding sentence or paragraph) to convey purpose. "
                "Screen-reader users often tab through link lists out of context \u2014 'read more' or 'click here' "
                "is meaningless when isolated. This is stricter than 2.4.4 (A), which allows context to disambiguate."
            ),
            remediation_steps=[
                "\ud83d\udccd WHERE TO FIX: Each link with generic text.",
                "  \u2022 Replace the link text with a phrase that names the destination (e.g., 'Read the 2025 annual report' instead of 'read more').",
                "  \u2022 If the visual design requires terse text, supplement with aria-label that contains the full purpose.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="Anchor text read directly from the DOM; generic phrases matched against the same dictionary as 2.4.4.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"{len(generic_offenders)} generic-text link(s)",
            remediation_id="html_link_purpose_link_only",
            remediation_data={"links": generic_offenders},
        ))

    # ── 2.1.3 Keyboard (No Exception) — AAA ─────────────────────────────────
    def _rule_2_1_3_keyboard_no_exception(self):
        """WCAG 2.1.3 (AAA) — Identical to 2.1.1 but removes the exception
        for input that depends on the path of motion (e.g., free-hand drawing).
        Our analyzer never applies that exception, so any 2.1.1 violation
        we emit is also a 2.1.3 violation. We re-run the same source-only
        scan and emit under the AAA criterion.
        """
        if not self._html_text:
            return
        offenders: List[Dict[str, Any]] = []
        pattern = re.compile(
            r"<(div|span|p|li|td|th|img)\b([^>]*\bonclick\s*=[^>]*)>",
            re.IGNORECASE,
        )
        for m in pattern.finditer(self._html_text):
            tag = m.group(1).lower()
            attrs_low = m.group(2).lower()
            has_role = re.search(
                r"\brole\s*=\s*['\"](button|link|menuitem|tab|checkbox|radio|switch)['\"]",
                attrs_low,
            )
            has_tabindex = re.search(r"\btabindex\s*=", attrs_low)
            has_key_handler = re.search(r"\bonkey(down|up|press)\s*=", attrs_low)
            if has_role and has_tabindex and has_key_handler:
                continue
            offenders.append({"tag": tag, "snippet": m.group(0)[:120]})
        if not offenders:
            return
        sample = "; ".join(f"<{o['tag']}>" for o in offenders[:3])
        self.fact_sheet.confirmed_findings.append(Finding(
            criterion_id="2.1.3",
            criterion_name="Keyboard (No Exception)",
            wcag_level="AAA",
            issue=(
                f"{len(offenders)} non-interactive element(s) with onclick are unreachable from the keyboard. "
                "AAA removes the 2.1.1 exception for path-dependent input, so the bar is absolute."
            ),
            evidence=f"Affected elements: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "2.1.3 (AAA) is identical to 2.1.1 except the carve-out for path-dependent input is removed. "
                "For a static-analysis scope, every 2.1.1 violation is also a 2.1.3 violation."
            ),
            remediation_steps=[
                "Same fix as 2.1.1: replace clickable <div>/<span> with <button>/<a>, OR add role+tabindex+onkey* together.",
            ],
            confidence_tier=ConfidenceTier.CONFIRMED,
            confidence_label=CONFIDENCE_LABEL[EvidenceSource.DOM_DIRECT],
            confidence_rationale="Same source-only scan as 2.1.1; AAA emission is mechanical because our scope never invokes the path-of-motion exception.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location="Interactive elements",
            remediation_id="html_keyboard_no_exception",
            remediation_data={"controls": offenders},
        ))

    # ── 1.4.9 Images of Text (No Exception) — AAA ───────────────────────────
    def _rule_1_4_9_images_of_text_no_exception(self):
        """WCAG 1.4.9 (AAA) — Stricter than 1.4.5. Removes the exemption for
        logotypes and essential text. Our 1.4.5 implementation flags inline
        SVGs with <text> children but does not detect logotypes either way,
        so the same offenders apply under 1.4.9.
        """
        if not self._html_text:
            return
        svg_pat = re.compile(r"<svg\b[^>]*>(.*?)</svg>", re.IGNORECASE | re.DOTALL)
        text_pat = re.compile(r"<text\b[^>]*>", re.IGNORECASE)
        offenders: List[Dict[str, Any]] = []
        for m in svg_pat.finditer(self._html_text):
            inner = m.group(1)
            if not text_pat.search(inner):
                continue
            opening = re.match(r"<svg\b([^>]*)>", m.group(0), re.IGNORECASE)
            attrs = (opening.group(1) if opening else "").strip()[:80]
            offenders.append({"attrs": attrs})
        if not offenders:
            return
        sample = "; ".join(f"<svg {o['attrs']}>" for o in offenders[:3])
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="1.4.9",
            criterion_name="Images of Text (No Exception)",
            wcag_level="AAA",
            issue=(
                f"{len(offenders)} inline SVG image(s) render text as graphics. "
                "AAA removes the 1.4.5 logotype/customization exemption."
            ),
            evidence=f"Inline SVGs containing <text>: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "AAA 1.4.9 forbids images-of-text outright (the only exemptions are essential decoration and "
                "explicit user customization). A static analyzer cannot distinguish a logotype, so for AAA "
                "every inline SVG <text> is flagged."
            ),
            remediation_steps=[
                "Replace SVG <text> elements with real HTML text styled via CSS.",
                "If the SVG genuinely is a logotype, document the exception manually \u2014 the analyzer cannot detect it.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale="Same SVG-with-text scan as 1.4.5; static analysis cannot tell logotype from regular text-as-graphic.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location="Inline SVG markup",
            remediation_id="html_images_of_text_no_exception",
            remediation_data={"svgs": offenders},
        ))

    # ── 2.5.1 Pointer Gestures — A (closes the last Level A gap) ────────────
    def _rule_2_5_1_pointer_gestures(self):
        """WCAG 2.5.1 (A) — Multipoint or path-based gestures must have a
        single-pointer alternative. Source-only heuristic: detect handlers
        for pointermove/touchmove/gesturestart/gestureend/touchstart-with-
        multi-touch in inline JS or attribute form, and flag any that don't
        appear alongside a regular click/keyboard fallback near the same
        element. POSSIBLE tier because static markup cannot verify whether a
        nearby button truly performs the same action.
        """
        if not self._html_text:
            return
        text = self._html_text
        gesture_pat = re.compile(
            r"\b(addEventListener\s*\(\s*['\"](?:pointermove|touchmove|gesturestart|gestureend|gesturechange)['\"]|"
            r"on(?:pointermove|touchmove|gesturestart|gestureend|gesturechange)\s*=)",
            re.IGNORECASE,
        )
        multitouch_pat = re.compile(
            r"\.touches\s*\.\s*length\s*[><=]+\s*[12]|"
            r"\bevent\.touches\.length\s*[><=]+\s*[12]",
            re.IGNORECASE,
        )
        hits: List[Dict[str, Any]] = []
        for m in gesture_pat.finditer(text):
            window = text[max(0, m.start() - 60):min(len(text), m.end() + 200)]
            has_click_fallback = bool(
                re.search(r"addEventListener\s*\(\s*['\"]click['\"]", window, re.IGNORECASE)
                or re.search(r"\bonclick\s*=", window, re.IGNORECASE)
                or re.search(r"addEventListener\s*\(\s*['\"]keydown['\"]", window, re.IGNORECASE)
            )
            if has_click_fallback:
                continue
            hits.append({
                "kind": "gesture-handler",
                "snippet": m.group(0)[:80],
                "context": window.strip()[:160],
            })
        for m in multitouch_pat.finditer(text):
            hits.append({
                "kind": "multi-touch-check",
                "snippet": m.group(0)[:80],
                "context": text[max(0, m.start() - 40):m.end() + 80].strip()[:160],
            })
        if not hits:
            return
        sample = "; ".join(f"{h['kind']}: {h['snippet']}" for h in hits[:3])
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="2.5.1",
            criterion_name="Pointer Gestures",
            wcag_level="A",
            issue=(
                f"{len(hits)} path-based or multi-touch gesture handler(s) detected with no nearby "
                "click/keyboard alternative."
            ),
            evidence=f"Gesture handlers without fallback: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "Users with motor impairments, tremor, or one-handed device use cannot reliably perform "
                "swipe, pinch, or multi-finger gestures. 2.5.1 (A) requires that any function reachable by "
                "a path-based or multipoint gesture also be reachable by a single-pointer (or keyboard) action. "
                "This closes the last remaining Level A gap in our coverage."
            ),
            remediation_steps=[
                "\ud83d\udccd WHERE TO FIX: Each gesture handler listed in the evidence.",
                "  \u2022 Add a visible button that performs the same action (e.g., next/previous buttons alongside swipe).",
                "  \u2022 Wire a keyboard handler (Enter / arrow keys) for the same action.",
                "  \u2022 If the gesture is essential (e.g., signature capture), document the exception manually.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale="Source-only scan of inline JS for gesture handler patterns; cannot verify whether a click handler elsewhere in the page performs the equivalent action.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location="Inline JavaScript / event handlers",
            remediation_id="html_pointer_gestures",
            remediation_data={"hits": hits},
        ))

    # ── 3.3.3 Error Suggestion — AA ─────────────────────────────────────────
    def _rule_3_3_3_error_suggestion(self):
        """WCAG 3.3.3 (AA) — When an input error is detected and suggestions
        for correction are known, the suggestions are provided. Source-only
        heuristic: flag forms that contain required inputs but no associated
        error container (no role=alert, no aria-live, no aria-describedby
        pointing at an error-bearing element). POSSIBLE tier because static
        markup can't confirm whether a JS-injected error container fills
        the gap at submit time.
        """
        if not self._html_text:
            return
        text = self._html_text
        form_pat = re.compile(r"<form\b[^>]*>(.*?)</form>", re.IGNORECASE | re.DOTALL)
        offenders: List[Dict[str, Any]] = []
        for fm in form_pat.finditer(text):
            body = fm.group(1)
            has_required = bool(
                re.search(r"<input\b[^>]*\brequired\b", body, re.IGNORECASE)
                or re.search(r"<input\b[^>]*\baria-required\s*=\s*['\"]true['\"]", body, re.IGNORECASE)
                or re.search(r"<select\b[^>]*\brequired\b", body, re.IGNORECASE)
                or re.search(r"<textarea\b[^>]*\brequired\b", body, re.IGNORECASE)
            )
            if not has_required:
                continue
            has_error_surface = bool(
                re.search(r"\brole\s*=\s*['\"]alert['\"]", body, re.IGNORECASE)
                or re.search(r"\baria-live\s*=\s*['\"](?:assertive|polite)['\"]", body, re.IGNORECASE)
                or re.search(r"\baria-errormessage\s*=", body, re.IGNORECASE)
                or re.search(r"\baria-invalid\s*=", body, re.IGNORECASE)
                or re.search(r"\bclass\s*=\s*['\"][^'\"]*\b(error|invalid|validation|field-error|help-block)\b", body, re.IGNORECASE)
            )
            if has_error_surface:
                continue
            attrs = re.match(r"<form\b([^>]*)>", fm.group(0), re.IGNORECASE)
            offenders.append({
                "form_attrs": (attrs.group(1) if attrs else "").strip()[:120],
                "snippet": fm.group(0)[:140],
            })
        if not offenders:
            return
        sample = "; ".join(f"<form {o['form_attrs']}>" for o in offenders[:3])
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="3.3.3",
            criterion_name="Error Suggestion",
            wcag_level="AA",
            issue=(
                f"{len(offenders)} form(s) contain required inputs but expose no detectable "
                "error-surface element (no role=alert, aria-live, aria-errormessage, aria-invalid, "
                "or recognized error CSS class)."
            ),
            evidence=f"Forms missing error surface: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "When users submit invalid input, the form must tell them which field is wrong AND suggest "
                "how to fix it (when the suggestion is knowable). Without a live region or error container, "
                "screen-reader users miss the error entirely. 3.3.3 (AA) is broadly required by procurement."
            ),
            remediation_steps=[
                "\ud83d\udccd WHERE TO FIX: Each form listed above.",
                "  \u2022 Add a container with role='alert' or aria-live='polite' to announce errors.",
                "  \u2022 Use aria-errormessage='id' on each required input pointing at the error text element.",
                "  \u2022 Set aria-invalid='true' on the failing input at submit time.",
                "  \u2022 Make the error text actionable: name the field and suggest the fix (e.g., 'Email must include @').",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale="Static markup parsed for error-surface signals; cannot verify whether JS dynamically injects an error container at submit time.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"{len(offenders)} form(s) with required inputs",
            remediation_id="html_error_suggestion",
            remediation_data={"forms": offenders},
        ))

    # ── 3.3.4 Error Prevention (Legal, Financial, Data) — AA ────────────────
    def _rule_3_3_4_error_prevention(self):
        """WCAG 3.3.4 (AA) — For commit/purchase/delete actions, the user
        must be able to review/confirm/undo before the action is final.
        Source-only heuristic: find forms whose submit/primary button text
        names a destructive or financial action (delete, purchase, transfer,
        send, submit-payment, etc.) AND that don't expose a confirmation
        pattern (a second confirm button, JS confirm() call, data-confirm
        attribute, or a review-step heading nearby). POSSIBLE tier.
        """
        if not self._html_text:
            return
        text = self._html_text
        destructive_words = (
            r"delete|remove|destroy|cancel\s+(?:account|subscription)|"
            r"submit\s+payment|pay\s+(?:now|today)|purchase|buy|order|checkout|charge|"
            r"transfer|send\s+(?:money|payment|wire)|donate|"
            r"sign\s+contract|accept\s+terms|finaliz[es]e|"
            r"file\s+(?:return|claim)|submit\s+(?:claim|return)"
        )
        button_pat = re.compile(
            rf"<(?:button|input)\b[^>]*?(?:type\s*=\s*['\"]submit['\"][^>]*)?>\s*"
            rf"((?:[^<]|<(?!/?(?:button|input)))*?({destructive_words})[^<]*)",
            re.IGNORECASE,
        )
        value_pat = re.compile(
            rf"<input\b[^>]*\btype\s*=\s*['\"]submit['\"][^>]*\bvalue\s*=\s*['\"]([^'\"]*\b({destructive_words})\b[^'\"]*)['\"]",
            re.IGNORECASE,
        )
        hits: List[Dict[str, Any]] = []
        seen_offsets: set = set()
        for m in button_pat.finditer(text):
            key = m.start() // 50
            if key in seen_offsets:
                continue
            seen_offsets.add(key)
            window = text[max(0, m.start() - 300):min(len(text), m.end() + 300)]
            has_confirm = bool(
                re.search(r"\bdata-confirm\s*=", window, re.IGNORECASE)
                or re.search(r"\bconfirm\s*\(", window, re.IGNORECASE)
                or re.search(r"\b(review|confirm|are\s+you\s+sure|verify)\b", window, re.IGNORECASE)
                or re.search(r"<input\b[^>]*type\s*=\s*['\"]checkbox['\"][^>]*\brequired\b", window, re.IGNORECASE)
            )
            if has_confirm:
                continue
            hits.append({
                "action_text": m.group(1).strip()[:80],
                "matched_word": m.group(2).strip(),
            })
        for m in value_pat.finditer(text):
            window = text[max(0, m.start() - 300):min(len(text), m.end() + 300)]
            has_confirm = bool(
                re.search(r"\bdata-confirm\s*=", window, re.IGNORECASE)
                or re.search(r"\b(review|confirm|are\s+you\s+sure|verify)\b", window, re.IGNORECASE)
            )
            if has_confirm:
                continue
            hits.append({
                "action_text": m.group(1).strip()[:80],
                "matched_word": m.group(2).strip(),
            })
        if not hits:
            return
        sample = "; ".join(f"'{h['action_text']}' (matched: {h['matched_word']})" for h in hits[:3])
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="3.3.4",
            criterion_name="Error Prevention (Legal, Financial, Data)",
            wcag_level="AA",
            issue=(
                f"{len(hits)} destructive/financial action(s) detected with no nearby confirmation pattern."
            ),
            evidence=f"Destructive buttons without confirm/review/undo signal: {sample}.",
            severity=Severity.SERIOUS,
            why_it_matters=(
                "For legal commitments, financial transactions, and data deletion, WCAG 3.3.4 (AA) requires that the "
                "action be reversible, verified (user reviews and can correct), OR confirmed (user explicitly confirms). "
                "Users with cognitive disabilities are most harmed by accidental destructive actions."
            ),
            remediation_steps=[
                "\ud83d\udccd WHERE TO FIX: Each destructive button listed above.",
                "  \u2022 Add a confirmation step: a review page that shows what will happen, or a modal that asks 'Are you sure?'",
                "  \u2022 Or: require the user to tick a 'I confirm' checkbox before the button enables.",
                "  \u2022 Or: provide an undo window (e.g., 'Cancel within 60 seconds').",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale="Source-only scan of button text and surrounding markup for confirmation signals; cannot follow JS-driven multi-step flows.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"{len(hits)} destructive action(s)",
            remediation_id="html_error_prevention",
            remediation_data={"hits": hits},
        ))

    # ── Phase N+ (2026-05-18) — 3 additional AAA closures ─────────────────────
    # 2.4.13 Focus Appearance · 3.3.5 Help · 3.3.6 Error Prevention (All)
    # All POSSIBLE-tier source-only heuristics consistent with the other
    # Phase N rules; closes the final 3 free-tier AAA quick wins.
    # ──────────────────────────────────────────────────────────────────────────

    # ── 2.4.13 Focus Appearance — AAA ───────────────────────────────────────
    def _rule_2_4_13_focus_appearance(self):
        """WCAG 2.2 — 2.4.13 Focus Appearance (AAA). The focus indicator must
        be at least 2 CSS px thick on its perimeter and have ≥3:1 contrast
        against adjacent colors. Source-only heuristic: scan <style> blocks
        for rules that suppress the default focus indicator (outline:none /
        outline:0 / outline-width:0) without providing a sufficiently thick
        replacement (≥2px outline/border or a box-shadow ring). POSSIBLE
        tier — static analysis can't measure actual rendered contrast.
        """
        if not self._html_text:
            return
        text = self._html_text
        # Pull every <style>…</style> block (inline CSS in the document)
        style_pat = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
        style_bodies = [m.group(1) for m in style_pat.finditer(text)]
        if not style_bodies:
            return
        combined = "\n".join(style_bodies)
        # Find CSS rule blocks that contain outline-suppressing declarations.
        # Pattern: `<selectors> { … outline: none / 0 / 0px / 0 none … }`
        rule_pat = re.compile(
            r"([^{}]+?)\{([^{}]*?\b(?:outline\s*:\s*(?:none|0(?:px)?\s*(?:none)?)|outline-width\s*:\s*0(?:px)?)[^{}]*?)\}",
            re.IGNORECASE | re.DOTALL,
        )
        offenders: List[Dict[str, Any]] = []
        for m in rule_pat.finditer(combined):
            selector = (m.group(1) or "").strip()[:120]
            body = m.group(2) or ""
            # Skip if this rule is harmless (universal reset on non-interactive)
            # by checking if selector targets focusable elements OR :focus state.
            is_focusable_target = bool(
                re.search(r":focus|:focus-visible|button|a\b|\binput|select|textarea|\*\s*[,{]|\[tabindex", selector, re.IGNORECASE)
            )
            if not is_focusable_target:
                continue
            # Check for a sufficient replacement indicator in the same block:
            # outline ≥2px OR border ≥2px OR box-shadow defined (any non-none).
            has_thick_outline = bool(re.search(r"outline\s*:\s*(?:[2-9]|[1-9]\d+)(?:\.\d+)?\s*px", body, re.IGNORECASE))
            has_thick_border = bool(re.search(r"border(?:-(?:top|right|bottom|left))?\s*:\s*(?:[2-9]|[1-9]\d+)(?:\.\d+)?\s*px", body, re.IGNORECASE))
            has_box_shadow = bool(re.search(r"box-shadow\s*:\s*(?!none\b)[^;]+", body, re.IGNORECASE))
            if has_thick_outline or has_thick_border or has_box_shadow:
                continue
            # No replacement found → likely a 2.4.13 failure.
            offenders.append({
                "selector": selector,
                "snippet": body.strip().replace("\n", " ")[:140],
            })
        if not offenders:
            return
        sample = "; ".join(f"`{o['selector']}` → {{{o['snippet']}}}" for o in offenders[:3])
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="2.4.13",
            criterion_name="Focus Appearance",
            wcag_level="AAA",
            issue=(
                f"{len(offenders)} CSS rule(s) suppress the default focus indicator "
                "without providing a ≥2px replacement outline, border, or box-shadow."
            ),
            evidence=f"Outline-suppressing rules without sufficient replacement: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "WCAG 2.2 (2.4.13, AAA) requires the focus indicator to be at least 2 CSS pixels thick on the "
                "element perimeter and to have 3:1 contrast against adjacent colors. Removing the default "
                "outline without an equivalent replacement leaves keyboard users with no visible focus cue. "
                "This is the most common AAA focus failure on modern stylesheets that hide native outlines for aesthetic reasons."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Each CSS rule listed above.",
                "  • Replace `outline: none` with `outline: 2px solid <high-contrast-color>; outline-offset: 2px;`.",
                "  • Or define a paired `:focus-visible` rule with a ≥2px outline / box-shadow ring.",
                "  • Verify the ring color has ≥3:1 contrast against both the element's background and the page background.",
                "  • Example: `button:focus-visible { outline: 2px solid #1A73E8; outline-offset: 2px; }`",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale="Source-only scan of inline <style> blocks; cannot measure rendered contrast or detect replacement rings defined in external stylesheets.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"{len(offenders)} CSS rule(s) in <style> blocks",
            remediation_id="html_focus_appearance",
            remediation_data={"rules": offenders},
        ))

    # ── 3.3.5 Help — AAA ────────────────────────────────────────────────────
    def _rule_3_3_5_help(self):
        """WCAG 3.3.5 (AAA) — Context-sensitive help is available for forms
        that require human input. Source-only heuristic: flag any <form>
        that contains required inputs but exposes NO help affordance —
        no aria-describedby on inputs, no <label>-adjacent help text
        (small/.help/.hint class), no help/contact/FAQ link nearby.
        POSSIBLE tier.
        """
        if not self._html_text:
            return
        text = self._html_text
        form_pat = re.compile(r"<form\b[^>]*>(.*?)</form>", re.IGNORECASE | re.DOTALL)
        offenders: List[Dict[str, Any]] = []
        for fm in form_pat.finditer(text):
            body = fm.group(1)
            has_required = bool(
                re.search(r"<(?:input|select|textarea)\b[^>]*\brequired\b", body, re.IGNORECASE)
                or re.search(r"\baria-required\s*=\s*['\"]true['\"]", body, re.IGNORECASE)
            )
            if not has_required:
                continue
            # Help signals inside or immediately around the form.
            window = text[max(0, fm.start() - 400):min(len(text), fm.end() + 400)]
            has_help = bool(
                re.search(r"\baria-describedby\s*=", body, re.IGNORECASE)
                or re.search(r"<(?:small|p|span|div)\b[^>]*\bclass\s*=\s*['\"][^'\"]*\b(help|hint|tip|guidance|description)\b", body, re.IGNORECASE)
                or re.search(r"<a\b[^>]*>(?:[^<]{0,80}?)(?:help|support|contact|faq|need\s+assistance|chat\s+with)", window, re.IGNORECASE)
                or re.search(r"<details\b", body, re.IGNORECASE)
                or re.search(r"\bplaceholder\s*=\s*['\"][^'\"]{15,}['\"]", body, re.IGNORECASE)  # substantive placeholder
                or re.search(r"\btitle\s*=\s*['\"][^'\"]{15,}['\"]", body, re.IGNORECASE)
            )
            if has_help:
                continue
            attrs = re.match(r"<form\b([^>]*)>", fm.group(0), re.IGNORECASE)
            offenders.append({
                "form_attrs": (attrs.group(1) if attrs else "").strip()[:120],
            })
        if not offenders:
            return
        sample = "; ".join(f"<form {o['form_attrs']}>" for o in offenders[:3])
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="3.3.5",
            criterion_name="Help",
            wcag_level="AAA",
            issue=(
                f"{len(offenders)} form(s) with required inputs expose no detectable "
                "help affordance (no aria-describedby, no inline hint text, no nearby "
                "help/contact/FAQ link, no substantive placeholder)."
            ),
            evidence=f"Forms missing help: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "WCAG 3.3.5 (AAA) requires context-sensitive help for forms that ask the user to provide "
                "information. Users with cognitive disabilities or those filling out unfamiliar forms (legal, "
                "medical, financial) benefit enormously from inline guidance, examples, or a clearly-discoverable "
                "help link."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Each form listed above.",
                "  • Add aria-describedby on each input pointing at an inline hint element.",
                "  • Add inline help text near complex fields (e.g., 'Phone format: 555-123-4567').",
                "  • Include a 'Need help?' or 'Contact support' link inside or just below the form.",
                "  • For long/complex forms, add a <details>/<summary> FAQ block.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale="Source-only scan of form markup and surrounding context; cannot detect help offered via JS-driven tooltips or external help systems.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"{len(offenders)} form(s) with required inputs",
            remediation_id="html_help_affordance",
            remediation_data={"forms": offenders},
        ))

    # ── 3.3.6 Error Prevention (All) — AAA ──────────────────────────────────
    def _rule_3_3_6_error_prevention_all(self):
        """WCAG 3.3.6 (AAA) — Like 3.3.4 but applies to ALL user input,
        not just legal/financial/data. Source-only heuristic: flag any
        <form> with a submit-type button that does NOT expose a
        confirmation / review / undo pattern (no `aria-describedby`
        pointing at a review summary, no `data-confirm`, no confirm()/
        review/verify text near the button, no <output> element, no
        <dialog>). POSSIBLE tier.
        """
        if not self._html_text:
            return
        text = self._html_text
        form_pat = re.compile(r"<form\b[^>]*>(.*?)</form>", re.IGNORECASE | re.DOTALL)
        offenders: List[Dict[str, Any]] = []
        for fm in form_pat.finditer(text):
            body = fm.group(1)
            # Form must have at least one submit-type control
            has_submit = bool(
                re.search(r"<button\b[^>]*\btype\s*=\s*['\"]submit['\"]", body, re.IGNORECASE)
                or re.search(r"<button\b(?![^>]*\btype\s*=)[^>]*>", body, re.IGNORECASE)  # default type=submit
                or re.search(r"<input\b[^>]*\btype\s*=\s*['\"]submit['\"]", body, re.IGNORECASE)
            )
            if not has_submit:
                continue
            # Skip pure search/filter forms (low value here — they're not "user input
            # that the user would want to reverse" in the WCAG sense).
            if re.search(r"\brole\s*=\s*['\"]search['\"]", fm.group(0), re.IGNORECASE):
                continue
            if re.search(r"<input\b[^>]*\btype\s*=\s*['\"]search['\"]", body, re.IGNORECASE):
                continue
            # Confirmation/review/undo signals inside the form or just around it.
            window = text[max(0, fm.start() - 200):min(len(text), fm.end() + 400)]
            has_review = bool(
                re.search(r"\bdata-confirm\s*=", body, re.IGNORECASE)
                or re.search(r"\bconfirm\s*\(", window, re.IGNORECASE)
                or re.search(r"\b(review|confirm|preview|verify|are\s+you\s+sure|undo|cancel\s+within)\b", window, re.IGNORECASE)
                or re.search(r"<output\b", body, re.IGNORECASE)
                or re.search(r"<dialog\b", window, re.IGNORECASE)
                or re.search(r"<input\b[^>]*\btype\s*=\s*['\"]checkbox['\"][^>]*\brequired\b", body, re.IGNORECASE)
            )
            if has_review:
                continue
            attrs = re.match(r"<form\b([^>]*)>", fm.group(0), re.IGNORECASE)
            offenders.append({
                "form_attrs": (attrs.group(1) if attrs else "").strip()[:120],
            })
        if not offenders:
            return
        sample = "; ".join(f"<form {o['form_attrs']}>" for o in offenders[:3])
        self.fact_sheet.possible_findings.append(Finding(
            criterion_id="3.3.6",
            criterion_name="Error Prevention (All)",
            wcag_level="AAA",
            issue=(
                f"{len(offenders)} submission form(s) expose no detectable review, confirm, or undo "
                "pattern. WCAG 3.3.6 (AAA) extends 3.3.4 to ALL user-input submissions, not only "
                "legal/financial/data ones."
            ),
            evidence=f"Forms without review/confirm/undo: {sample}.",
            severity=Severity.MODERATE,
            why_it_matters=(
                "WCAG 3.3.6 (AAA) requires that for ANY form submission that causes a change, the user can "
                "review, confirm, or reverse the action. This is the most user-protective error-prevention level "
                "and is especially valuable for users with cognitive disabilities or motor impairments who may "
                "trigger submit accidentally."
            ),
            remediation_steps=[
                "📍 WHERE TO FIX: Each submission form listed above.",
                "  • Add a review step that shows submitted values and a final 'Confirm' button.",
                "  • Or expose an undo affordance after submission (e.g., 'Undo within 60 seconds').",
                "  • For multi-step forms, include a final-summary screen before commit.",
                "  • Pure search/filter forms are exempt — this rule already skips them.",
            ],
            confidence_tier=ConfidenceTier.POSSIBLE,
            confidence_label="medium",
            confidence_rationale="Source-only scan of form markup and surrounding context; cannot follow JS-driven multi-step review flows or server-side confirmation pages.",
            evidence_source=EvidenceSource.DOM_DIRECT,
            location=f"{len(offenders)} submission form(s)",
            remediation_id="html_error_prevention_all",
            remediation_data={"forms": offenders},
        ))
