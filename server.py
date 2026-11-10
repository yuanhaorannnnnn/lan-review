import argparse
import base64
import binascii
import fcntl
import hashlib
from contextlib import closing
from datetime import datetime, timezone
from ipaddress import IPv4Address, IPv4Network, ip_address
import json
import math
import os
from pathlib import Path
import posixpath
import re
import secrets
import sqlite3
import stat
import subprocess
import tempfile
import threading
from io import BytesIO
from unicodedata import normalize
from urllib.parse import unquote, urlsplit
from xml.etree.ElementTree import Element

import bleach
from flask import Flask, Response, abort, g, jsonify, request, send_file, url_for
from markdown import Markdown
from markdown.treeprocessors import Treeprocessor
from werkzeug.exceptions import HTTPException
from werkzeug.serving import make_server


FILE_TYPES = {
    ".md": ("markdown", "text/plain"),
    ".markdown": ("markdown", "text/plain"),
    ".html": ("html", "text/html"),
    ".htm": ("html", "text/html"),
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".webp": ("image", "image/webp"),
    ".gif": ("image", "image/gif"),
    ".avif": ("image", "image/avif"),
    ".mp4": ("video", "video/mp4"),
    ".m4v": ("video", "video/mp4"),
    ".webm": ("video", "video/webm"),
    ".mov": ("video", "video/quicktime"),
}
PRIVATE_NETWORKS = tuple(map(IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
MAX_TEXT_BYTES = 16 * 1024**2
MAX_UPLOAD_BYTES = 2 * 1024**3
MAX_ATTACHMENTS = 20
MAX_ATTACHMENT_BYTES = 32 * 1024**2
MAX_LOCAL_ATTACHMENTS = 500
MAX_LOCAL_ATTACHMENT_BYTES = 512 * 1024**2
MAX_THUMBNAIL_BYTES = 128 * 1024
MAX_THUMBNAIL_DIMENSION = 640
BITMAP_DATA_URL = re.compile(r"^data:image/(?:png|jpeg|webp|gif|avif);base64,", re.I)
SHARE_ID = re.compile(r"^[A-Za-z0-9_-]{32}$")
COMMENT_KEY = re.compile(r"^[A-Za-z0-9_-]{22,64}$")
FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)(?m:^(?:---|\.\.\.)[ \t]*$)(?:\n|$)", re.S)
TASK_MARKER = re.compile(r"^\[([ xX])\](?:[ \t]+|$)")
UNSAFE_FILENAME = re.compile(r"[/\\:\x00-\x1f\x7f]")

APP_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
    "media-src 'self' blob:; frame-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'; object-src 'none'"
)
HTML_CSP = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline' blob:; "
    "style-src 'unsafe-inline'; img-src data: blob:; font-src data: blob:; "
    "media-src data: blob:; worker-src blob:; frame-ancestors 'self'; "
    "base-uri 'none'; form-action 'none'; object-src 'none'; frame-src 'none'"
)
HTML_BRIDGE = r"""
<script>
(() => {
    const serverOrigin = new URL(location.href).origin;
    const contextLength = 40;
    let programmaticSelection = null;
    let activeSelection = null, selectionSequence = 0, scrollScheduled = false;
    // selectionchange is asynchronous; suppress only the exact range we set.
    // A real pointer or keyboard gesture releases suppression immediately.
    let pointerGesture = null;
    document.addEventListener("pointerdown", event => {
        programmaticSelection = null;
        pointerGesture = event.isPrimary === false || (event.pointerType === "mouse" && event.button !== 0)
            ? null : {id:event.pointerId, x:event.clientX, y:event.clientY, moved:false};
    }, true);
    document.addEventListener("pointermove", event => {
        if (pointerGesture && event.pointerId === pointerGesture.id &&
            Math.hypot(event.clientX - pointerGesture.x, event.clientY - pointerGesture.y) > 10) pointerGesture.moved = true;
    }, true);
    document.addEventListener("pointercancel", () => { pointerGesture = null; }, true);
    document.addEventListener("keydown", () => { programmaticSelection = null; }, true);
    const style = document.createElement("style");
    style.textContent = "::highlight(lanreview-anchor) {background:#ffd468;color:#20170b}";
    document.head.append(style);

    function readPageText() {
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
            acceptNode(node) {
                return node.parentElement.closest("script,style,noscript,template,[hidden]")
                    ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
            }
        });
        const parts = [];
        let text = "";
        while (walker.nextNode()) {
            const node = walker.currentNode;
            parts.push({node, offset: text.length});
            text += node.data;
        }
        return {parts, text};
    }

    function textOffset(parts, container, offset, endOfText) {
        const part = parts.find(candidate => candidate.node === container);
        if (part) return part.offset + offset;
        const point = new Range();
        point.setStart(container, offset);
        point.collapse(true);
        return parts.find(({node}) => point.comparePoint(node, 0) >= 0)?.offset ?? endOfText;
    }

    // Compute visible fragments rather than just the union bounding box: a
    // multi-line selection can straddle the viewport with nothing on screen.
    // Clip each text fragment to each of its overflow ancestors as well as
    // this iframe's viewport. The parent separately clips to its own viewport.
    function selectionGeometry(tracked) {
        const fallback = tracked.range.getBoundingClientRect();
        const viewport = {left:0, top:0, right:globalThis.innerWidth ?? document.documentElement?.clientWidth ?? 0,
                          bottom:globalThis.innerHeight ?? document.documentElement?.clientHeight ?? 0};
        const visible = [];
        for (const segment of tracked.segments) {
            if (segment.node.isConnected === false) continue;
            const range = new Range();
            try {
                range.setStart(segment.node, segment.start);
                range.setEnd(segment.node, segment.end);
            } catch { continue; }
            const fragments = range.getClientRects ? Array.from(range.getClientRects()) : [range.getBoundingClientRect()];
            for (const fragment of fragments) {
                let left = Math.max(viewport.left, fragment.left), top = Math.max(viewport.top, fragment.top);
                let right = Math.min(viewport.right, fragment.right ?? fragment.left + fragment.width);
                let bottom = Math.min(viewport.bottom, fragment.bottom ?? fragment.top + fragment.height);
                for (let element = segment.node.parentElement; element && right > left && bottom > top; element = element.parentElement) {
                    const css = getComputedStyle(element);
                    if (css.display === 'none' || css.visibility === 'hidden') { right = left; break; }
                    const clipX = /^(?:auto|scroll|hidden|clip)$/.test(css.overflowX);
                    const clipY = /^(?:auto|scroll|hidden|clip)$/.test(css.overflowY);
                    if (!clipX && !clipY) continue;
                    const box = element.getBoundingClientRect();
                    const contentLeft = box.left + (element.clientLeft || 0), contentTop = box.top + (element.clientTop || 0);
                    const contentRight = contentLeft + (element.clientWidth ?? box.width);
                    const contentBottom = contentTop + (element.clientHeight ?? box.height);
                    if (clipX) { left = Math.max(left, contentLeft); right = Math.min(right, contentRight); }
                    if (clipY) { top = Math.max(top, contentTop); bottom = Math.min(bottom, contentBottom); }
                }
                if (right > left && bottom > top) visible.push({left, top, right, bottom});
            }
        }
        const box = visible.length ? {
            left:Math.min(...visible.map(r=>r.left)), top:Math.min(...visible.map(r=>r.top)),
            right:Math.max(...visible.map(r=>r.right)), bottom:Math.max(...visible.map(r=>r.bottom))
        } : {left:fallback.left, top:fallback.top, right:fallback.right ?? fallback.left + fallback.width,
             bottom:fallback.bottom ?? fallback.top + fallback.height};
        return {visible:visible.length > 0, rect:{...box, width:box.right-box.left, height:box.bottom-box.top}};
    }

    function reportSelectionOnScroll() {
        if (scrollScheduled || !activeSelection) return;
        const current = activeSelection;
        scrollScheduled = true;
        requestAnimationFrame(() => {
            scrollScheduled = false;
            if (activeSelection !== current) return;
            const geometry = selectionGeometry(current);
            parent.postMessage({type:"lanreview:selection-visibility", selectionId:current.id, ...geometry}, serverOrigin);
        });
    }
    addEventListener("scroll", reportSelectionOnScroll, true);
    addEventListener("resize", reportSelectionOnScroll);

    // A collapsed selection alone can also be caused by programmatic navigation.
    // Notify the parent only for a deliberate pointer release in document content.
    document.addEventListener("pointerup", event => {
        const gesture = pointerGesture;
        pointerGesture = null;
        if (!gesture || gesture.id !== event.pointerId || gesture.moved ||
            Math.hypot(event.clientX - gesture.x, event.clientY - gesture.y) > 10) return;
        if (event.target instanceof Element && event.target.closest("input,textarea,select,button,[contenteditable]:not([contenteditable='false'])")) return;
        const selection = getSelection();
        if (!selection || selection.isCollapsed || !selection.rangeCount) {
            activeSelection = null;
            parent.postMessage({type:"lanreview:selection-cleared"}, serverOrigin);
        }
    });
    document.addEventListener("keydown", event => {
        if (event.key === "Escape" && !event.defaultPrevented && !event.isComposing &&
            !(event.target instanceof Element && event.target.closest("input,textarea,select,[contenteditable]:not([contenteditable='false'])"))) {
            parent.postMessage({type:"lanreview:escape"}, serverOrigin);
        }
    });
    document.addEventListener("selectionchange", () => {
        const selection = getSelection();
        if (programmaticSelection) {
            if (!selection || selection.isCollapsed || !selection.rangeCount) return;
            const current = selection.getRangeAt(0), expected = programmaticSelection;
            if (current.startContainer === expected.startContainer && current.startOffset === expected.startOffset &&
                current.endContainer === expected.endContainer && current.endOffset === expected.endOffset) return;
            programmaticSelection = null;
        }
        if (!selection || selection.isCollapsed || !selection.rangeCount) {
            parent.postMessage({type:"lanreview:selection", quote:""}, serverOrigin);
            return;
        }
        const range = selection.getRangeAt(0);
        const {parts, text} = readPageText();
        const start = textOffset(parts, range.startContainer, range.startOffset, text.length);
        const end = textOffset(parts, range.endContainer, range.endOffset, text.length);
        const quote = text.slice(start, end);
        if (!quote.trim() || (quote.length > 2000 && Array.from(quote).length > 2000)) return;
        const before = Array.from(text.slice(Math.max(0, start - 2 * contextLength), start));
        const after = Array.from(text.slice(end, end + 2 * contextLength));
        const segments = parts.filter(({node, offset}) => offset < end && offset + node.length > start)
            .map(({node, offset}) => ({node, start:Math.max(0, start-offset), end:Math.min(node.length, end-offset)}));
        const current = {id:++selectionSequence, range:range.cloneRange ? range.cloneRange() : range, segments};
        activeSelection = current;
        const {rect} = selectionGeometry(current);
        parent.postMessage({
            type:"lanreview:selection", quote, selectionId:current.id,
            prefix: before.slice(-contextLength).join(""),
            suffix: after.slice(0, contextLength).join(""),
            rect
        }, serverOrigin);
    });

    addEventListener("message", event => {
        if (event.source !== parent || event.origin !== serverOrigin) return;
        const {type, quote, prefix, suffix, selectionId} = event.data ?? {};
        if (type === "lanreview:clear-selection") {
            // Parent only requests this for the matching empty draft. An old
            // message must never clear a newer user selection or the browser
            // fallback selection used to view a historic comment.
            const tracked = activeSelection;
            if (!tracked || tracked.id !== selectionId || programmaticSelection || pointerGesture) return;
            const selection = getSelection();
            if (!selection || selection.isCollapsed || !selection.rangeCount) return;
            const current = selection.getRangeAt(0), expected = tracked.range;
            if (current.startContainer !== expected.startContainer || current.startOffset !== expected.startOffset ||
                current.endContainer !== expected.endContainer || current.endOffset !== expected.endOffset) return;
            activeSelection = null;
            selection.removeAllRanges();
            return;
        }
        if (type !== "lanreview:locate" || typeof quote !== "string" || !quote) return;
        activeSelection = null;
        const report = ok => parent.postMessage({type:"lanreview:locate-result", ok}, serverOrigin);
        const highlights = globalThis.CSS?.highlights;
        highlights?.delete("lanreview-anchor");
        if (!highlights) {
            programmaticSelection = {startContainer:null, startOffset:0, endContainer:null, endOffset:0};
            getSelection()?.removeAllRanges();
        }
        const {parts, text} = readPageText();
        let position = -1;
        for (let start = text.indexOf(quote); start !== -1; start = text.indexOf(quote, start + 1)) {
            if (prefix && (start < prefix.length || !text.startsWith(prefix, start - prefix.length))) continue;
            if (suffix && !text.startsWith(suffix, start + quote.length)) continue;
            if (position !== -1) {
                report(false);
                return;
            }
            position = start;
        }
        if (position === -1) {
            report(false);
            return;
        }
        const end = position + quote.length;
        const first = parts.find(({node, offset}) => offset <= position && position < offset + node.length);
        const last = parts.find(({node, offset}) => offset < end && end <= offset + node.length);
        if (!first || !last) {
            report(false);
            return;
        }
        const range = new Range();
        range.setStart(first.node, position - first.offset);
        range.setEnd(last.node, end - last.offset);
        for (const node of [first.node, last.node]) {
            for (let details = node.parentElement.closest("details"); details; details = details.parentElement?.closest("details")) {
                if (!details.querySelector(":scope > summary")?.contains(node)) details.open = true;
            }
        }
        const box = range.getBoundingClientRect();
        const visible = [first.node, last.node].every(node => {
            const element = node.parentElement;
            return element.getClientRects().length && getComputedStyle(element).visibility === "visible";
        });
        if (!box.width || !box.height || !visible) {
            report(false);
            return;
        }
        if (highlights && typeof Highlight !== "undefined") {
            highlights.set("lanreview-anchor", new Highlight(range));
        } else {
            const selection = getSelection();
            programmaticSelection = {startContainer:range.startContainer, startOffset:range.startOffset,
                endContainer:range.endContainer, endOffset:range.endOffset};
            selection.removeAllRanges();
            selection.addRange(range);
        }
        first.node.parentElement.scrollIntoView({block:"center", inline:"nearest"});
        report(true);
    });
})();
</script>
"""


SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    mimetype TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES reviews (id),
    parent_id INTEGER REFERENCES comments (id),
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    anchor TEXT,
    created_at TEXT NOT NULL,
    owner_hash TEXT,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS comments_by_review ON comments (review_id, id);
CREATE TABLE IF NOT EXISTS comment_requests (
    review_id TEXT NOT NULL REFERENCES reviews (id),
    key TEXT NOT NULL,
    comment_id INTEGER NOT NULL REFERENCES comments (id),
    anonymous INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (review_id, key)
);
CREATE TABLE IF NOT EXISTS comment_thumbnails (
    comment_id INTEGER PRIMARY KEY REFERENCES comments (id),
    mimetype TEXT NOT NULL,
    data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS review_attachments (
    review_id TEXT NOT NULL REFERENCES reviews (id),
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mimetype TEXT NOT NULL,
    UNIQUE (review_id, name)
);
"""


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def markdown_image_reference(value):
    try:
        parts = urlsplit(value)
        path = normalize("NFC", unquote(parts.path, errors="strict"))
    except (ValueError, UnicodeError):
        return None
    if parts.scheme or parts.netloc or not path or path.startswith("/") or re.search(r"[\\:\x00-\x1f\x7f]", path):
        return None
    # This is a document reference key. It is never used to open a filesystem path.
    return posixpath.normpath(path)


def external_http_link(value):
    if re.search(r"[\\\x00-\x20\x7f]", value):
        return False
    try:
        parts = urlsplit(value)
        return parts.scheme in {"https", "http"} and bool(parts.hostname) and parts.username is None
    except ValueError:
        return False


class MarkdownPreview(Treeprocessor):
    def __init__(self, renderer, attachments):
        super().__init__(renderer)
        self.attachments = attachments

    def run(self, root):
        for link in root.iter("a"):
            if external_http_link(link.get("href", "")):
                link.set("rel", "noopener noreferrer")
                link.set("target", "_blank")

        for item in root.iter("li"):
            paragraph = item.find("p")
            node = item if paragraph is None else paragraph
            match = TASK_MARKER.match(node.text or "")
            if match is None:
                continue
            marker = Element("input", {"type": "checkbox", "disabled": "disabled"})
            if match[1].lower() == "x":
                marker.set("checked", "checked")
            marker.tail = " " + node.text[match.end():]
            node.text = None
            node.insert(0, marker)

        images = [
            (node, markdown_image_reference(node.get("src", "")))
            for node in root.iter("img")
            if not BITMAP_DATA_URL.match(node.get("src", ""))
        ]
        references = {}
        for _, reference in images:
            if reference is not None:
                references.setdefault(posixpath.basename(reference), set()).add(reference)
        for node, reference in images:
            name = posixpath.basename(reference) if reference is not None else ""
            # Local imports store the whole reference; older manually uploaded
            # attachments use a basename and only match when unambiguous.
            exact = "local:" + reference if reference is not None else ""
            key = exact if exact in self.attachments else name
            if key in self.attachments and (key == exact or len(references[name]) == 1):
                node.set("src", self.attachments[key])
                continue
            description = node.get("alt") or name or "未提供说明"
            node.tag = "span"
            node.attrib.clear()
            node.set("class", "missing-image")
            node.text = f"图片不可用：{description}"


def render_markdown(source, attachments):
    metadata = None
    front = FRONT_MATTER.match(source)
    if front is not None:
        first = next((line for line in front[1].splitlines() if line.strip() and not line.lstrip().startswith("#")), "")
        if re.match(r"[A-Za-z_][\w.-]*[ \t]*:", first):
            metadata = front[1].rstrip("\n")
            source = source[front.end():]
    renderer = Markdown(
        extensions=["fenced_code", "tables", "sane_lists", "toc"],
        extension_configs={"tables": {"use_align_attribute": True}},
    )
    renderer.preprocessors.deregister("html_block")
    renderer.inlinePatterns.deregister("html")
    renderer.treeprocessors.register(MarkdownPreview(renderer, attachments), "review-preview", 15)
    asset_urls = set(attachments.values())

    def allow_attribute(tag, name, value):
        if tag == "a":
            if name == "href":
                return value.startswith("#") or external_http_link(value)
            return (name, value) in {("rel", "noopener noreferrer"), ("target", "_blank")}
        if tag == "img" and name == "src":
            return value in asset_urls or bool(BITMAP_DATA_URL.match(value))
        if tag == "input":
            return (name, value) in {("type", "checkbox"), ("disabled", ""), ("checked", "")} or (
                name in {"disabled", "checked"} and value == name
            )
        if tag == "span":
            return name == "class" and value == "missing-image"
        if tag == "ol":
            return name == "start" and value.isascii() and value.isdecimal()
        return (tag, name) in {("img", "alt"), ("img", "title"), ("th", "align"), ("td", "align")} or (
            tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and name == "id"
        )

    html = bleach.clean(
        renderer.convert(source),
        tags={
            "a", "blockquote", "br", "code", "em", "h1", "h2", "h3", "h4", "h5", "h6",
            "hr", "img", "input", "li", "ol", "p", "pre", "span", "strong", "table", "tbody", "td", "th",
            "thead", "tr", "ul",
        },
        attributes=allow_attribute,
        protocols={"data", "http", "https"},
        strip=True,
    )
    return html, metadata


def markdown_local_images(source):
    """Collect rendered Markdown image references without fetching anything."""
    found = []

    class CollectImages(Treeprocessor):
        def run(self, root):
            for image in root.iter("img"):
                raw = image.get("src", "")
                if not BITMAP_DATA_URL.match(raw):
                    found.append((raw, markdown_image_reference(raw)))

    renderer = Markdown(extensions=["fenced_code", "tables", "sane_lists", "toc"])
    renderer.preprocessors.deregister("html_block")
    renderer.inlinePatterns.deregister("html")
    renderer.treeprocessors.register(CollectImages(renderer), "collect-images", 15)
    renderer.convert(source)
    return found


def bitmap_mimetype(prefix):
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return "image/webp"
    if prefix[4:8] == b"ftyp" and any(
        prefix[index:index + 4] in (b"avif", b"avis") for index in range(8, len(prefix), 4)
    ):
        return "image/avif"


def validate_thumbnail(value):
    """Return a bounded, decoded video thumbnail; never trust client MIME alone."""
    if value is None:
        return None
    if not isinstance(value, str):
        abort(400, "缩略图格式无效。")
    match = re.fullmatch(r"data:(image/(?:jpeg|webp));base64,([A-Za-z0-9+/]*={0,2})", value)
    if not match or len(match.group(2)) > ((MAX_THUMBNAIL_BYTES + 2) // 3) * 4:
        abort(400, "缩略图格式无效或超过大小上限。")
    try:
        image_data = base64.b64decode(match.group(2), validate=True)
    except (ValueError, binascii.Error):
        abort(400, "缩略图格式无效。")
    if not image_data or len(image_data) > MAX_THUMBNAIL_BYTES or bitmap_mimetype(image_data[:128]) != match.group(1):
        abort(400, "缩略图格式无效或超过大小上限。")
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        abort(503, "缺少图片处理依赖，无法保存缩略图。")
    try:
        with Image.open(BytesIO(image_data)) as image:
            if (image.format not in ("JPEG", "WEBP") or image.width < 1 or image.height < 1
                    or image.width > MAX_THUMBNAIL_DIMENSION or image.height > MAX_THUMBNAIL_DIMENSION):
                abort(400, "缩略图尺寸或格式无效。")
            image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        abort(400, "缩略图无法解码。")
    return match.group(1), image_data


def validate_region(region):
    """Validate coordinates relative to the actual media content, not the viewport."""
    if not isinstance(region, dict) or region.get("type") not in ("point", "rect", "polygon"):
        abort(400, "画面区域格式无效。")
    kind = region["type"]
    if kind == "polygon":
        points = region.get("points")
        if not isinstance(points, list) or not 3 <= len(points) <= 256:
            abort(400, "画面轮廓须包含 3 至 256 个顶点。")
        for point in points:
            if not isinstance(point, list) or len(point) != 2:
                abort(400, "画面轮廓坐标无效。")
            if any(type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value)
                   for value in point):
                abort(400, "画面轮廓坐标无效。")
        twice_area = sum(a[0] * b[1] - b[0] * a[1]
                         for a, b in zip(points, points[1:] + points[:1]))
        if twice_area == 0:
            abort(400, "画面轮廓必须有面积。")
        return {"type": "polygon", "points": points}
    keys = ("x", "y") if kind == "point" else ("x", "y", "w", "h")
    result = {"type": kind}
    for key in keys:
        value = region.get(key)
        if type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value):
            abort(400, "画面区域坐标无效。")
        result[key] = value
    if kind == "rect" and (
        result["w"] <= 0 or result["h"] <= 0
        or result["x"] + result["w"] > 1
        or result["y"] + result["h"] > 1
    ):
        abort(400, "画面区域必须在画面内且有宽高。")
    return result


def validate_anchor(anchor, review_type):
    if anchor is None:
        return None
    if not isinstance(anchor, dict):
        abort(400, "评论定位格式无效。")
    anchor_type = anchor.get("type")
    if anchor_type == "text" and review_type in ("markdown", "html"):
        quote = anchor.get("quote")
        if not isinstance(quote, str) or not quote.strip() or len(quote) > 2000:
            abort(400, "引用文字须为 1 至 2000 字。")
        result = {"type": "text", "quote": quote}
        for key in ("prefix", "suffix"):
            value = anchor.get(key, "")
            if not isinstance(value, str) or len(value) > 200:
                abort(400, "引用上下文过长或格式无效。")
            result[key] = value
        return result

    if anchor_type == "image" and review_type == "image":
        if "region" in anchor:
            return {"type": "image", "region": validate_region(anchor["region"])}
        keys, upper = ("x", "y"), 1
    elif anchor_type == "video" and review_type == "video":
        keys, upper = ("time",), 10**9
    else:
        abort(400, "评论定位与文件类型不匹配。")
    result = {"type": anchor_type}
    for key in keys:
        value = anchor.get(key)
        if type(value) not in (int, float) or not 0 <= value <= upper or not math.isfinite(value):
            abort(400, "评论定位数值无效。")
        result[key] = value
    if anchor_type == "video" and "end_time" in anchor:
        end_time = anchor["end_time"]
        if (type(end_time) not in (int, float) or not result["time"] < end_time <= 10**9
                or not math.isfinite(end_time)):
            abort(400, "视频时间段的结束时间必须晚于开始时间。")
        result["end_time"] = end_time
    if anchor_type == "video" and "region" in anchor:
        result["region"] = validate_region(anchor["region"])
    return result


def persistent_owner_token(data_dir):
    """Keep one local owner credential across restarts, including concurrent starts."""
    path = data_dir / "owner-token"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("发布者凭据文件必须由当前用户拥有，且权限为 0600。")
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.read(fd, 256)
        if not raw:
            token = secrets.token_urlsafe(32)
            content = (token + "\n").encode("ascii")
            if os.write(fd, content) != len(content):
                raise OSError("发布者凭据写入不完整。")
            os.fsync(fd)
            return token
        try:
            token = raw.decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise ValueError("发布者凭据文件无效。") from error
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token) or len(raw) > 44:
            raise ValueError("发布者凭据文件无效。")
        return token
    finally:
        os.close(fd)


def create_app(data_dir, lan_ip=None, lan_cidr=None, port=8765, owner_token=None):
    if (lan_ip is None) != (lan_cidr is None):
        raise ValueError("--lan-ip 和 --lan-cidr 必须同时设置。")
    network = None
    if lan_ip is not None:
        lan_ip = IPv4Address(lan_ip)
        network = IPv4Network(lan_cidr, strict=False)
        if lan_ip not in network or not any(network.subnet_of(private) for private in PRIVATE_NETWORKS):
            raise ValueError("LAN IP 必须位于指定的私有 IPv4 网段内。")
    if not 1 <= port <= 65535:
        raise ValueError("端口须为 1 至 65535。")

    data_dir = Path(data_dir).expanduser().resolve()
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES + 65536,
        MAX_FORM_PARTS=MAX_ATTACHMENTS + 5,
        TRUSTED_HOSTS=["localhost", "127.0.0.1"] + ([str(lan_ip)] if lan_ip else []),
    )
    share_origin = f"http://{lan_ip}:{port}" if lan_ip else ""
    media_dir = data_dir / "files"
    media_dir.mkdir(mode=0o700, exist_ok=True)
    database = data_dir / "reviews.sqlite3"
    picker_lock = threading.Lock()
    segment_lock = threading.BoundedSemaphore(1)

    def connect():
        connection = sqlite3.connect(database, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    with closing(connect()) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA)
        if "anonymous" not in {row["name"] for row in connection.execute("PRAGMA table_info(comment_requests)")}:
            connection.execute("ALTER TABLE comment_requests ADD COLUMN anonymous INTEGER NOT NULL DEFAULT 0")
        comment_columns = {row["name"] for row in connection.execute("PRAGMA table_info(comments)")}
        if "owner_hash" not in comment_columns:
            connection.execute("ALTER TABLE comments ADD COLUMN owner_hash TEXT")
        if "deleted" not in comment_columns:
            connection.execute("ALTER TABLE comments ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")

    @app.before_request
    def enforce_network():
        try:
            address = ip_address(request.remote_addr or "")
        except ValueError:
            abort(403, "客户端地址无效。")
        g.local_device = address.is_loopback and request.host.split(":", 1)[0] in {
            "127.0.0.1", "localhost"
        }
        if not address.is_loopback and (network is None or address not in network):
            abort(403, "访问来源不在已配置的局域网内。")
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin is not None and origin != request.host_url.rstrip("/"):
                abort(403, "拒绝跨来源提交。")
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                abort(403, "拒绝跨来源提交。")

    @app.after_request
    def security_headers(response):
        if getattr(g, "new_visitor_id", None) and response.status_code < 400:
            response.set_cookie("lanreview_visitor", g.new_visitor_id, max_age=31536000,
                                httponly=True, samesite="Lax", path="/")
        response.headers.setdefault("Content-Security-Policy", APP_CSP)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-DNS-Prefetch-Control"] = "off"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        response = error.get_response()
        response.set_data(app.json.dumps({"error": error.description}))
        response.content_type = "application/json"
        return response

    def require_local_device():
        if not g.local_device:
            abort(403, "只能从托管服务的本机选择或读取本机文件。")

    def read_review(review_id):
        if not SHARE_ID.fullmatch(review_id):
            abort(404, "评审不存在。")
        with closing(connect()) as connection:
            row = connection.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
        if row is None:
            abort(404, "评审不存在。")
        return row

    def review_summary(row):
        return {
            "id": row["id"],
            "name": row["name"],
            "type": row["type"],
            "created_at": row["created_at"],
            "url": share_origin + url_for("review_page", review_id=row["id"]),
            "share_scope": "lan" if share_origin else "local",
        }

    @app.get("/")
    def home_page():
        return app.send_static_file("index.html")

    @app.get("/review/<review_id>")
    def review_page(review_id):
        read_review(review_id)
        return app.send_static_file("index.html")

    @app.get("/api/reviews")
    def list_reviews():
        with closing(connect()) as connection:
            rows = connection.execute("SELECT * FROM reviews ORDER BY created_at DESC, rowid DESC")
            return jsonify(reviews=[review_summary(row) for row in rows])

    @app.post("/api/reviews/<review_id>/segment")
    def segment_review_frame(review_id):
        # Only an explicit uploaded frame is accepted; never fetch URLs or resolve
        # client-provided paths on the server. Manual annotation remains separate.
        review = read_review(review_id)
        if review["type"] not in ("image", "video"):
            abort(400, "仅图片或视频画面支持智能选物。")
        request.max_content_length = 12 * 1024**2 + 65536
        files = request.files.getlist("image")
        if len(files) != 1:
            abort(400, "请选择一个画面。")
        try:
            x, y = float(request.form["x"]), float(request.form["y"])
        except (KeyError, ValueError, OverflowError):
            abort(400, "选点坐标无效。")
        if not all(math.isfinite(value) and 0 <= value <= 1 for value in (x, y)):
            abort(400, "选点坐标必须在画面范围内。")
        frame = files[0].stream.read(12 * 1024**2 + 1)
        if not frame or len(frame) > 12 * 1024**2:
            abort(413, "画面为空或超过 12 MiB。")
        if bitmap_mimetype(frame[:128]) not in ("image/png", "image/jpeg", "image/webp"):
            abort(415, "智能选物仅支持 PNG、JPEG 或 WebP 画面。")
        if not segment_lock.acquire(blocking=False):
            abort(429, "智能选物正在处理其他画面，请稍后重试。")
        try:
            try:
                from smart_select import segment_image, SmartSelectError
            except (ImportError, OSError):
                abort(503, "本机智能选物组件不可用，请改用手动选区。")
            try:
                result = segment_image(frame, x, y)
            except SmartSelectError as error:
                abort(503, str(error))
            except Exception:
                abort(503, "智能选物暂不可用，请改用手动选区。")
            if not isinstance(result, dict) or not isinstance(result.get("candidates"), list) or len(result["candidates"]) > 3:
                abort(503, "智能选物结果无效，请改用手动选区。")
            candidates = []
            for candidate in result["candidates"]:
                if not isinstance(candidate, dict):
                    abort(503, "智能选物结果无效，请改用手动选区。")
                try:
                    region = validate_region({"type": "polygon", "points": candidate.get("points")})
                except HTTPException:
                    abort(503, "智能选物结果无效，请改用手动选区。")
                score = candidate.get("score")
                if type(score) not in (int, float) or not -1e9 <= score <= 1e9 or not math.isfinite(score):
                    abort(503, "智能选物结果无效，请改用手动选区。")
                candidates.append({"points": region["points"], "score": score})
            return jsonify(candidates=candidates)
        finally:
            segment_lock.release()

    def markdown_bundle_file():
        files = request.files.getlist("file")
        if len(files) != 1 or not files[0].filename:
            abort(400, "请选择一个 Markdown 文件。")
        upload = files[0]
        name = re.sub(r"[\x00-\x1f\x7f]", "", re.split(r"[/\\]", upload.filename)[-1])
        if Path(name).suffix.lower() not in (".md", ".markdown") or len(name) > 255:
            abort(415, "请选择 UTF-8 Markdown 文件。")
        source = upload.stream.read(MAX_TEXT_BYTES + 1)
        if not source:
            abort(400, "文件为空。")
        if len(source) > MAX_TEXT_BYTES:
            abort(413, "Markdown 文件超出大小上限。")
        try:
            content = source.decode("utf-8-sig")
        except UnicodeError:
            abort(415, "Markdown 需使用 UTF-8 编码。")
        images = markdown_local_images(content)
        if len({ref for _, ref in images if ref is not None}) > MAX_LOCAL_ATTACHMENTS:
            abort(413, f"本地图片引用超过 {MAX_LOCAL_ATTACHMENTS} 个。")
        return name, source, images

    @app.post("/api/markdown/images")
    def markdown_images():
        # Inspect only the uploaded Markdown; never resolve paths or fetch URLs.
        request.max_content_length = MAX_TEXT_BYTES + 65536
        _, _, images = markdown_bundle_file()
        return jsonify(references=[{"raw": raw, "reference": ref} for raw, ref in images])

    @app.post("/api/reviews/bundle")
    def upload_markdown_bundle():
        # Each attachment is explicitly paired with an actual document reference;
        # browser filenames and their directory paths are never trusted as paths.
        request.max_form_parts = MAX_LOCAL_ATTACHMENTS * 2 + 5
        request.max_content_length = MAX_TEXT_BYTES + MAX_LOCAL_ATTACHMENT_BYTES + 1024 * 1024
        name, source, images = markdown_bundle_file()
        references = request.form.getlist("references")
        uploads = request.files.getlist("attachments")
        if len(references) != len(uploads):
            abort(400, "图片和引用必须一一对应。")
        if len(uploads) > MAX_LOCAL_ATTACHMENTS:
            abort(413, f"图片附件最多 {MAX_LOCAL_ATTACHMENTS} 张。")
        allowed = {ref for _, ref in images if ref is not None}
        if len(set(references)) != len(references) or any(ref not in allowed for ref in references):
            abort(400, "附件引用必须是文档中唯一存在的图片引用。")
        review_id = secrets.token_urlsafe(24)
        row = dict(id=review_id, name=name, type="markdown", mimetype="text/plain", created_at=timestamp())
        staged = []
        placed = []
        attached = []
        warnings = []
        remaining = MAX_LOCAL_ATTACHMENT_BYTES
        supplied = set(references)
        for raw, ref in images:
            if ref is None:
                warnings.append(f"未导入图片（非本地相对路径）：{raw}")
            elif ref not in supplied:
                warnings.append(f"未导入图片（未在所选文件夹中找到）：{raw}")
        try:
            with tempfile.NamedTemporaryFile(prefix=".upload-", dir=media_dir, delete=False) as output:
                temporary = Path(output.name)
                staged.append((temporary, media_dir / review_id))
                output.write(source)
            for ref, upload in zip(references, uploads):
                if FILE_TYPES.get(Path(ref).suffix.lower(), (None, None))[0] != "image":
                    abort(415, "附件引用仅支持 PNG、JPEG、GIF、WebP 或 AVIF 图片。")
                image_id = secrets.token_urlsafe(24)
                with tempfile.NamedTemporaryFile(prefix=".upload-", dir=media_dir, delete=False) as output:
                    temporary = Path(output.name)
                    staged.append((temporary, media_dir / f"attachment-{image_id}"))
                    size = 0
                    while chunk := upload.stream.read(1024 * 1024):
                        size += len(chunk)
                        if size > remaining:
                            abort(413, "图片附件合计不能超过 512 MiB。")
                        output.write(chunk)
                if not size:
                    abort(400, "图片附件为空。")
                remaining -= size
                with temporary.open("rb") as image:
                    mimetype = bitmap_mimetype(image.read(128))
                if mimetype is None:
                    abort(415, "未识别出受支持的图片格式。")
                attached.append((review_id, image_id, "local:" + ref, mimetype))
            with closing(connect()) as connection, connection:
                connection.execute("INSERT INTO reviews VALUES (:id, :name, :type, :mimetype, :created_at)", row)
                connection.executemany("INSERT INTO review_attachments VALUES (?, ?, ?, ?)", attached)
                for temporary, destination in staged:
                    temporary.replace(destination)
                    placed.append(destination)
        except Exception:
            for destination in placed:
                destination.unlink(missing_ok=True)
            raise
        finally:
            for temporary, _ in staged:
                temporary.unlink(missing_ok=True)
        return jsonify(**review_summary(row), warnings=warnings), 201

    @app.post("/api/reviews")
    def upload_review():
        files = request.files.getlist("file")
        if len(files) != 1 or not files[0].filename:
            abort(400, "请选择一个主文件。")
        upload = files[0]
        name = re.sub(r"[\x00-\x1f\x7f]", "", re.split(r"[/\\]", upload.filename)[-1])
        extension = Path(name).suffix.lower()
        if extension not in FILE_TYPES:
            abort(415, "此文件格式无法预览。")
        if len(name) > 255:
            abort(400, "文件名不能超过 255 字。")
        review_type, mimetype = FILE_TYPES[extension]
        attachments = request.files.getlist("attachments")
        if attachments and review_type != "markdown":
            abort(400, "只有 Markdown 支持上传图片附件。")
        if len(attachments) > MAX_ATTACHMENTS:
            abort(413, f"图片附件最多 {MAX_ATTACHMENTS} 张。")
        review_id = secrets.token_urlsafe(24)
        row = dict(id=review_id, name=name, type=review_type, mimetype=mimetype, created_at=timestamp())
        staged_files = []
        placed_files = []

        def stage_file(source, destination, limit, oversize):
            with tempfile.NamedTemporaryFile(prefix=".upload-", dir=media_dir, delete=False) as file:
                temporary = Path(file.name)
                staged_files.append((temporary, destination))
                size = 0
                while chunk := source.stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        abort(413, oversize)
                    file.write(chunk)
            if size == 0:
                abort(400, "文件为空。")
            return temporary, size

        def detect_bitmap(temporary):
            with temporary.open("rb") as file:
                actual_type = bitmap_mimetype(file.read(128))
            if actual_type is None:
                abort(415, "未识别出受支持的图片格式。")
            return actual_type

        try:
            limit = MAX_TEXT_BYTES if review_type in {"html", "markdown"} else MAX_UPLOAD_BYTES
            temporary, _ = stage_file(upload, media_dir / review_id, limit, "文件超出大小上限。")
            if review_type in {"html", "markdown"}:
                try:
                    temporary.read_text(encoding="utf-8-sig")
                except UnicodeError:
                    abort(415, "Markdown 和 HTML 需使用 UTF-8 编码。")
            if review_type == "image":
                row["mimetype"] = detect_bitmap(temporary)

            attached = []
            attachment_names = set()
            remaining = MAX_ATTACHMENT_BYTES
            for attachment in attachments:
                attachment_name = normalize("NFC", attachment.filename or "")
                if not attachment_name or len(attachment_name) > 255 or UNSAFE_FILENAME.search(attachment_name):
                    abort(400, "附件必须使用文件名，不能包含目录或特殊字符。")
                if attachment_name in attachment_names:
                    abort(400, "同一评审的图片附件不能重名。")
                attachment_names.add(attachment_name)
                if FILE_TYPES.get(Path(attachment_name).suffix.lower(), (None, None))[0] != "image":
                    abort(415, "附件仅支持 PNG、JPEG、GIF、WebP 或 AVIF 图片。")
                attachment_id = secrets.token_urlsafe(24)
                temporary, size = stage_file(
                    attachment, media_dir / f"attachment-{attachment_id}", remaining,
                    "图片附件合计不能超过 32 MiB。",
                )
                remaining -= size
                attached.append((review_id, attachment_id, attachment_name, detect_bitmap(temporary)))

            with closing(connect()) as connection, connection:
                connection.execute(
                    "INSERT INTO reviews VALUES (:id, :name, :type, :mimetype, :created_at)", row
                )
                connection.executemany("INSERT INTO review_attachments VALUES (?, ?, ?, ?)", attached)
                for temporary, destination in staged_files:
                    temporary.replace(destination)
                    placed_files.append(destination)
        except Exception:
            for destination in placed_files:
                destination.unlink(missing_ok=True)
            raise
        finally:
            for temporary, _ in staged_files:
                temporary.unlink(missing_ok=True)
        return jsonify(review_summary(row)), 201

    @app.post("/api/reviews/import")
    def import_local_review():
        require_local_device()
        request.max_content_length = 16 * 1024
        payload = request.get_json()
        value = payload.get("path") if isinstance(payload, dict) else None
        return import_local_path(value)

    @app.post("/api/reviews/pick-local")
    def pick_local_review():
        require_local_device()
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            abort(503, "本机图形文件选择器不可用，请输入文件绝对路径。")
        if not picker_lock.acquire(blocking=False):
            abort(409, "文件选择窗口已打开，请先完成或取消。")
        try:
            try:
                selected = subprocess.run(
                    ["/usr/bin/zenity", "--file-selection", "--title=选择要评审的文件"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    check=False, timeout=180,
                )
            except (OSError, subprocess.TimeoutExpired):
                abort(503, "本机文件选择器不可用，请输入文件绝对路径。")
            if selected.returncode == 1:
                return jsonify(cancelled=True)
            if selected.returncode != 0:
                abort(503, "本机文件选择器启动失败，请输入文件绝对路径。")
            try:
                value = selected.stdout.decode("utf-8").removesuffix("\n")
            except UnicodeDecodeError:
                abort(400, "所选文件路径不是有效的 UTF-8。")
            return import_local_path(value)
        finally:
            picker_lock.release()

    def import_local_path(value):
        if not isinstance(value, str) or not value or "\x00" in value or not os.path.isabs(value):
            abort(400, "请提供本机文件的绝对路径。")
        source = Path(os.path.abspath(value))
        extension = source.suffix.lower()
        if extension not in FILE_TYPES:
            abort(415, "此文件格式无法预览。")
        if len(source.name) > 255:
            abort(400, "文件名不能超过 255 字。")
        review_type, mimetype = FILE_TYPES[extension]
        review_id = secrets.token_urlsafe(24)
        row = dict(id=review_id, name=source.name, type=review_type, mimetype=mimetype, created_at=timestamp())
        staged = []
        placed = []
        warnings = []

        def stage_local(path, destination, limit):
            # Refuse symlinks and special files, including FIFO and device files.
            # O_NONBLOCK prevents a raced FIFO from hanging the service.
            if Path(os.path.abspath(path)) != path.resolve():
                raise ValueError("不能通过符号链接读取文件")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as incoming:
                if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                    raise ValueError("只支持普通文件")
                with tempfile.NamedTemporaryFile(prefix=".import-", dir=media_dir, delete=False) as output:
                    temporary = Path(output.name)
                    staged.append((temporary, destination))
                    size = 0
                    while chunk := incoming.read(1024 * 1024):
                        size += len(chunk)
                        if size > limit:
                            raise ValueError("文件超过大小上限")
                        output.write(chunk)
                    if not size:
                        raise ValueError("文件为空")
            return temporary, size

        def bitmap_type(path):
            with path.open("rb") as file:
                return bitmap_mimetype(file.read(128))

        try:
            try:
                temporary, _ = stage_local(source, media_dir / review_id,
                    MAX_TEXT_BYTES if review_type in {"markdown", "html"} else MAX_UPLOAD_BYTES)
                if review_type in {"markdown", "html"}:
                    content = temporary.read_text(encoding="utf-8-sig")
                if review_type == "image":
                    row["mimetype"] = bitmap_type(temporary)
                    if row["mimetype"] is None:
                        abort(415, "未识别出受支持的图片格式。")
            except (OSError, ValueError, UnicodeError) as error:
                abort(400, f"无法读取主文件：{error}")

            attached = []
            if review_type == "markdown":
                remaining = MAX_LOCAL_ATTACHMENT_BYTES
                seen = set()
                for raw, reference in markdown_local_images(content):
                    if reference is None:
                        warnings.append(f"未导入图片（非本地相对路径）：{raw}")
                        continue
                    if reference in seen:
                        continue
                    seen.add(reference)
                    if len(seen) > MAX_LOCAL_ATTACHMENTS:
                        abort(413, f"本地图片引用超过 {MAX_LOCAL_ATTACHMENTS} 个。")
                    image_path = Path(os.path.abspath(source.parent / reference))
                    if FILE_TYPES.get(image_path.suffix.lower(), (None, None))[0] != "image":
                        warnings.append(f"未导入图片（格式不支持）：{raw}")
                        continue
                    image_id = secrets.token_urlsafe(24)
                    try:
                        temporary, size = stage_local(image_path, media_dir / f"attachment-{image_id}", remaining)
                        actual_type = bitmap_type(temporary)
                        if actual_type is None:
                            raise ValueError("图片格式无法识别")
                        remaining -= size
                        attached.append((review_id, image_id, "local:" + reference, actual_type))
                    except (OSError, ValueError) as error:
                        # Discard a partial staged copy on a missing/invalid dependency.
                        destination = media_dir / f"attachment-{image_id}"
                        for temp, dest in list(staged):
                            if dest == destination:
                                temp.unlink(missing_ok=True)
                                staged.remove((temp, dest))
                        warnings.append(f"未导入图片 {raw}：{error}")
            elif review_type == "html":
                warnings.append("HTML 的外部样式、脚本和图片不会自动导入，请使用自包含 HTML。")

            with closing(connect()) as connection, connection:
                connection.execute("INSERT INTO reviews VALUES (:id, :name, :type, :mimetype, :created_at)", row)
                connection.executemany("INSERT INTO review_attachments VALUES (?, ?, ?, ?)", attached)
                for temporary, destination in staged:
                    temporary.replace(destination)
                    placed.append(destination)
        except Exception:
            for destination in placed:
                destination.unlink(missing_ok=True)
            raise
        finally:
            for temporary, _ in staged:
                temporary.unlink(missing_ok=True)
        return jsonify(**review_summary(row), warnings=warnings), 201

    def visitor_id(key=None):
        value = request.cookies.get("lanreview_visitor", "")
        if SHARE_ID.fullmatch(value):
            return value
        # A cookie-less retry with the same request key must resolve to the same
        # author even if the first response (including Set-Cookie) was lost.
        value = hashlib.sha256(("lanreview-visitor:" + key).encode()).hexdigest()[:32] if key else secrets.token_urlsafe(24)
        g.new_visitor_id = value
        return value

    def comment_owner(value):
        return hashlib.sha256(("lanreview-comment-owner:" + value).encode()).hexdigest()

    def public_comment(row, owner, review_id):
        result = dict(row)
        stored_owner = result.pop("owner_hash", None)
        has_thumbnail = result.pop("has_thumbnail", 0)
        deleted = bool(result.get("deleted"))
        result["deleted"] = deleted
        result["can_delete"] = bool(stored_owner and owner and secrets.compare_digest(stored_owner, owner) and not deleted)
        result["thumbnail_url"] = url_for("comment_thumbnail", review_id=review_id, comment_id=result["id"]) if has_thumbnail and not deleted else None
        result["anchor"] = json.loads(result["anchor"])
        if deleted:
            result["author"] = "评论已删除"
            result["body"] = ""
        return result

    @app.get("/api/reviews/<review_id>")
    def review_detail(review_id):
        row = read_review(review_id)
        owner = comment_owner(visitor_id())
        with closing(connect()) as connection:
            rows = connection.execute(
                "SELECT id, parent_id, author, body, anchor, created_at, owner_hash, deleted, "
                "EXISTS(SELECT 1 FROM comment_thumbnails t WHERE t.comment_id = comments.id) AS has_thumbnail "
                "FROM comments WHERE review_id = ? ORDER BY id",
                (review_id,),
            )
            comments = [public_comment(comment, owner, review_id) for comment in rows]
        result = review_summary(row)
        result.update(content_url=url_for("review_media", review_id=review_id), comments=comments)
        if row["type"] == "markdown":
            result["rendered_url"] = url_for("rendered_review", review_id=review_id)
        return jsonify(result)

    @app.post("/api/reviews/<review_id>/comments")
    def add_comment(review_id):
        row = read_review(review_id)
        key = request.headers.get("Idempotency-Key")
        if key is not None and not COMMENT_KEY.fullmatch(key):
            abort(400, "评论请求键格式无效。")
        request.max_content_length = 256 * 1024
        payload = request.get_json()
        if not isinstance(payload, dict):
            abort(400, "评论格式无效。")
        author, body = payload.get("author", ""), payload.get("body")
        if not isinstance(author, str) or len(author.strip()) > 64:
            abort(400, "署名不能超过 64 字。")
        if not isinstance(body, str) or not 1 <= len(body.strip()) <= 4000:
            abort(400, "评论须为 1 至 4000 字。")
        author, body = author.strip(), body.strip()
        identity = visitor_id(key)
        owner = comment_owner(identity)
        anonymous = not author
        if anonymous:
            author = "匿名-" + hashlib.sha256(identity.encode()).hexdigest()[:10]
        try:
            author.encode("utf-8")
            body.encode("utf-8")
        except UnicodeError:
            abort(400, "评论包含无效字符。")
        anchor = validate_anchor(payload.get("anchor"), row["type"])
        parent_id = payload.get("parent_id")
        if parent_id is not None:
            if type(parent_id) is not int or not 1 <= parent_id < 2**63:
                abort(400, "父评论 ID 无效。")
            if anchor is not None:
                abort(400, "回复使用父评论的定位。")
        thumbnail_value = payload.get("thumbnail")
        if thumbnail_value is not None and (row["type"] != "video" or not anchor or parent_id is not None):
            abort(400, "缩略图仅支持有时间定位的视频评论。")
        thumbnail = validate_thumbnail(thumbnail_value)
        created_at = timestamp()
        content = {"author": author, "body": body, "anchor": anchor, "parent_id": parent_id}
        with closing(connect()) as connection, connection:
            if key is not None:
                # Serialize the replay check with the insert across concurrent requests.
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT c.id, c.author, c.body, c.anchor, c.parent_id, c.created_at, c.owner_hash, c.deleted,
                           r.anonymous AS request_anonymous, t.mimetype AS thumbnail_mimetype, t.data AS thumbnail_data
                    FROM comment_requests AS r JOIN comments AS c ON c.id = r.comment_id
                    LEFT JOIN comment_thumbnails AS t ON t.comment_id = c.id
                    WHERE r.review_id = ? AND r.key = ?
                    """,
                    (review_id, key),
                ).fetchone()
                if existing is not None:
                    stored = dict(existing)
                    stored_anonymous = bool(stored.pop("request_anonymous"))
                    if stored["deleted"]:
                        abort(409, "原评论已删除。")
                    stored["anchor"] = json.loads(stored["anchor"])
                    thumbnail_data = stored.pop("thumbnail_data")
                    thumbnail_mimetype = stored.pop("thumbnail_mimetype")
                    stored_thumbnail = (thumbnail_mimetype, thumbnail_data) if thumbnail_data is not None else None
                    if stored_anonymous != anonymous or any(
                        stored[field] != value for field, value in content.items()
                        if field != "author" or not anonymous
                    ) or stored_thumbnail != thumbnail:
                        abort(409, "这次提交已使用，请先查看原评论。")
                    stored_owner = stored.pop("owner_hash", None)
                    stored["deleted"] = False
                    stored["can_delete"] = bool(stored_owner and secrets.compare_digest(stored_owner, owner))
                    stored["thumbnail_url"] = url_for("comment_thumbnail", review_id=review_id, comment_id=stored["id"]) if thumbnail else None
                    return jsonify(stored), 201
            if parent_id is not None:
                parent = connection.execute(
                    "SELECT 1 FROM comments WHERE id = ? AND review_id = ? AND parent_id IS NULL AND deleted = 0",
                    (parent_id, review_id),
                ).fetchone()
                if parent is None:
                    abort(400, "找不到这条评审中的父评论。")
            result = connection.execute(
                "INSERT INTO comments (review_id, parent_id, author, body, anchor, created_at, owner_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (review_id, parent_id, author, body, json.dumps(anchor), created_at, owner),
            )
            if thumbnail is not None:
                connection.execute("INSERT INTO comment_thumbnails VALUES (?, ?, ?)",
                                   (result.lastrowid, thumbnail[0], thumbnail[1]))
            if key is not None:
                connection.execute(
                    "INSERT INTO comment_requests (review_id, key, comment_id, anonymous) VALUES (?, ?, ?, ?)",
                    (review_id, key, result.lastrowid, int(anonymous)),
                )
        thumbnail_url = url_for("comment_thumbnail", review_id=review_id, comment_id=result.lastrowid) if thumbnail else None
        return jsonify(id=result.lastrowid, created_at=created_at, deleted=False, can_delete=True,
                       thumbnail_url=thumbnail_url, **content), 201

    @app.post("/api/reviews/<review_id>/comments/<int:comment_id>/delete")
    def delete_comment(review_id, comment_id):
        read_review(review_id)
        identity = request.cookies.get("lanreview_visitor", "")
        if not SHARE_ID.fullmatch(identity):
            abort(403, "只能删除本浏览器发表的评论。")
        owner = comment_owner(identity)
        with closing(connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            comment = connection.execute(
                "SELECT owner_hash, deleted FROM comments WHERE review_id = ? AND id = ?", (review_id, comment_id)
            ).fetchone()
            if comment is None:
                abort(404, "评论不存在。")
            if not comment["owner_hash"] or not secrets.compare_digest(comment["owner_hash"], owner):
                abort(403, "只能删除本浏览器发表的评论。")
            if not comment["deleted"]:
                connection.execute("UPDATE comments SET deleted = 1, author = '', body = '' WHERE review_id = ? AND id = ?", (review_id, comment_id))
                connection.execute("DELETE FROM comment_thumbnails WHERE comment_id = ?", (comment_id,))
        return jsonify(ok=True)

    @app.get("/api/reviews/<review_id>/comments/<int:comment_id>/thumbnail")
    def comment_thumbnail(review_id, comment_id):
        read_review(review_id)
        with closing(connect()) as connection:
            thumbnail = connection.execute(
                "SELECT t.mimetype, t.data FROM comment_thumbnails t JOIN comments c ON c.id = t.comment_id "
                "WHERE c.review_id = ? AND c.id = ? AND c.deleted = 0", (review_id, comment_id)
            ).fetchone()
        if thumbnail is None:
            abort(404, "缩略图不存在。")
        return Response(thumbnail["data"], mimetype=thumbnail["mimetype"])

    @app.get("/api/reviews/<review_id>/rendered")
    def rendered_review(review_id):
        row = read_review(review_id)
        if row["type"] != "markdown":
            abort(404)
        path = media_dir / review_id
        if not path.is_file():
            abort(404, "原文件不存在。")
        with closing(connect()) as connection:
            rows = connection.execute(
                "SELECT id, name FROM review_attachments WHERE review_id = ?", (review_id,)
            )
            attachments = {
                row["name"]: url_for("review_attachment", review_id=review_id, attachment_id=row["id"])
                for row in rows if (media_dir / f"attachment-{row['id']}").is_file()
            }
        html, metadata = render_markdown(path.read_text(encoding="utf-8-sig"), attachments)
        return jsonify(html=html, metadata=metadata)

    @app.get("/media/<review_id>/attachments/<attachment_id>")
    def review_attachment(review_id, attachment_id):
        if not SHARE_ID.fullmatch(review_id) or not SHARE_ID.fullmatch(attachment_id):
            abort(404, "图片附件不存在。")
        with closing(connect()) as connection:
            attachment = connection.execute(
                "SELECT name, mimetype FROM review_attachments WHERE review_id = ? AND id = ?",
                (review_id, attachment_id),
            ).fetchone()
        path = media_dir / f"attachment-{attachment_id}"
        if attachment is None or not path.is_file():
            abort(404, "图片附件不存在。")
        return send_file(path, mimetype=attachment["mimetype"], download_name=attachment["name"])

    @app.get("/media/<review_id>")
    def review_media(review_id):
        row = read_review(review_id)
        path = media_dir / review_id
        if not path.is_file():
            abort(404, "原文件不存在。")
        if row["type"] == "html":
            if request.headers.get("Sec-Fetch-Dest") != "iframe":
                return send_file(path, mimetype=row["mimetype"], download_name=row["name"], as_attachment=True)
            content = path.read_text(encoding="utf-8-sig")
            response = Response(content + HTML_BRIDGE, mimetype="text/html")
            response.headers["Content-Security-Policy"] = HTML_CSP
            return response
        return send_file(path, mimetype=row["mimetype"], download_name=row["name"], conditional=True)

    return app


def main():
    parser = argparse.ArgumentParser(description="本机与局域网文件评审")
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    parser.add_argument("--data-dir", type=Path, default=data_home / "lanreview")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--lan-ip", help="分享链接使用的本机私有 IPv4 地址")
    parser.add_argument("--lan-cidr", help="允许访问的私有 IPv4 网段，如 192.168.1.0/24")
    args = parser.parse_args()

    try:
        app = create_app(args.data_dir, args.lan_ip, args.lan_cidr, args.port)
    except ValueError as error:
        parser.error(str(error))
    bind = "0.0.0.0" if args.lan_ip else "127.0.0.1"
    server = make_server(bind, args.port, app, threaded=True)
    print(f"本机访问地址 http://127.0.0.1:{args.port}/", flush=True)
    if args.lan_ip:
        print(f"局域网分享地址 http://{args.lan_ip}:{args.port}（允许 {args.lan_cidr}）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
