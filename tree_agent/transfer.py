"""Export and import projects, conversations, or the whole workspace.

The portable unit is a zip holding one JSON manifest plus the image
attachments the exported conversations reference, because attachments live
outside the workspace file and a JSON-only export would lose them.

Two things deliberately do not travel well, and are reported rather than
papered over:

  * `thread_id` names a Codex session under `~/.codex/sessions` on the machine
    that created it. Importing onto the same machine keeps the conversation
    resumable; importing elsewhere gives a readable transcript that has to be
    reset before it can continue.
  * Node ids are regenerated on import, so importing the same archive twice
    creates two independent copies instead of colliding.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import zipfile
from datetime import datetime
from typing import Any

from . import store

FORMAT = "tree-agent-export"
VERSION = 1
MANIFEST = "workspace.json"
ATTACHMENT_DIR = "attachments"


class TransferError(Exception):
    pass


def _iter_subtree(node: dict[str, Any]):
    yield node
    if node["kind"] == store.PROJECT:
        for child in node["children"]:
            yield from _iter_subtree(child)


def _conversations(nodes: list[dict[str, Any]]):
    for node in nodes:
        for sub in _iter_subtree(node):
            if sub["kind"] == store.CONVERSATION:
                yield sub


# ------------------------------------------------------------------ export


def export_nodes(ws: store.Workspace, node_ids: list[str], path: str) -> dict[str, Any]:
    """Write the given nodes (with their subtrees) to a zip. Returns a summary."""
    roots = [ws.find(node_id) for node_id in node_ids]
    roots = [node for node in roots if node is not None]
    if not roots:
        raise TransferError("沒有可匯出的項目")

    payload = json.loads(json.dumps(roots, ensure_ascii=False))  # deep copy

    # Collect attachments and rewrite the references to archive-relative names.
    archived: dict[str, str] = {}
    missing: list[str] = []
    for conv in _conversations(payload):
        for message in conv.get("messages") or []:
            images = message.get("images")
            if not images:
                continue
            rewritten = []
            for original in images:
                if original in archived:
                    rewritten.append(archived[original])
                    continue
                if not os.path.isfile(original):
                    missing.append(original)
                    continue
                name = f"{ATTACHMENT_DIR}/{len(archived)}-{os.path.basename(original)}"
                archived[original] = name
                rewritten.append(name)
            message["images"] = rewritten or None

    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_host": socket.gethostname(),
        "defaults": dict(ws.defaults),
        "nodes": payload,
    }

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
        for original, name in archived.items():
            archive.write(original, name)

    return {
        "projects": sum(1 for n in payload for s in _iter_subtree(n)
                        if s["kind"] == store.PROJECT),
        "conversations": sum(1 for _ in _conversations(payload)),
        "attachments": len(archived),
        "missing_attachments": missing,
        "path": path,
    }


def export_workspace(ws: store.Workspace, path: str) -> dict[str, Any]:
    return export_nodes(ws, [node["id"] for node in ws.projects], path)


# ------------------------------------------------------------------ import


def read_manifest(path: str) -> dict[str, Any]:
    """Validate an archive and return its manifest."""
    if not os.path.isfile(path):
        raise TransferError(f"找不到檔案：{path}")
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                raw = archive.read(MANIFEST).decode("utf-8")
        else:  # a bare manifest is accepted, but carries no attachments
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
        manifest = json.loads(raw)
    except (OSError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransferError(f"無法讀取匯出檔：{exc}") from exc

    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise TransferError("這不是 Tree Agent 的匯出檔")
    if manifest.get("version", 0) > VERSION:
        raise TransferError(
            f"匯出檔版本 {manifest.get('version')} 比這個程式支援的 {VERSION} 新"
        )
    if not isinstance(manifest.get("nodes"), list) or not manifest["nodes"]:
        raise TransferError("匯出檔裡沒有任何專案或對話")
    return manifest


def _refresh_ids(node: dict[str, Any]) -> None:
    """Give the whole subtree new ids, so an import never collides."""
    for sub in _iter_subtree(node):
        sub["id"] = store.new_id()


def import_archive(
    ws: store.Workspace, path: str, parent_id: str | None
) -> dict[str, Any]:
    """Merge an archive into the workspace under `parent_id` (None = top level)."""
    manifest = read_manifest(path)
    nodes = json.loads(json.dumps(manifest["nodes"], ensure_ascii=False))

    parent = ws.find(parent_id) if parent_id else None
    if parent is not None and parent["kind"] != store.PROJECT:
        parent = ws.parent_of(parent["id"])

    attachment_dir = os.path.join(ws.home, ATTACHMENT_DIR)
    extracted = 0
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = {n for n in archive.namelist() if n.startswith(ATTACHMENT_DIR + "/")}
            if members:
                os.makedirs(attachment_dir, exist_ok=True)
            for conv in _conversations(nodes):
                for message in conv.get("messages") or []:
                    images = message.get("images")
                    if not images:
                        continue
                    local = []
                    for name in images:
                        if name not in members:
                            continue
                        target = os.path.join(attachment_dir, os.path.basename(name))
                        target = _free_path(target)
                        with archive.open(name) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        local.append(target)
                        extracted += 1
                    message["images"] = local or None
    else:
        # A bare manifest has no attachment bytes; drop the dangling references.
        for conv in _conversations(nodes):
            for message in conv.get("messages") or []:
                if message.get("images"):
                    message["images"] = None

    threads = 0
    for node in nodes:
        _refresh_ids(node)
        siblings = parent["children"] if parent is not None else ws.projects
        holder = parent if parent is not None else {"children": ws.projects}
        node["name"] = ws.unique_name(holder, node["name"])
        # Conversations cannot live at the top level.
        if node["kind"] == store.CONVERSATION and parent is None:
            wrapper = store.new_project(ws.unique_name(holder, "匯入的對話"))
            wrapper["children"].append(node)
            node = wrapper
        siblings.append(node)
        threads += sum(1 for c in _conversations([node]) if c.get("thread_id"))

    if parent is not None:
        parent["expanded"] = True
    ws.save()

    return {
        "projects": sum(1 for n in nodes for s in _iter_subtree(n)
                        if s["kind"] == store.PROJECT),
        "conversations": sum(1 for _ in _conversations(nodes)),
        "attachments": extracted,
        "threads": threads,
        "roots": [n["id"] for n in nodes],
        "source_host": manifest.get("source_host") or "",
        "foreign_host": (manifest.get("source_host") or "") != socket.gethostname(),
    }


def _free_path(path: str) -> str:
    """`path`, or the same name with a counter, so imports never overwrite."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    counter = 2
    while os.path.exists(f"{stem}-{counter}{ext}"):
        counter += 1
    return f"{stem}-{counter}{ext}"


# ---------------------------------------------------------------- markdown

_ROLE_HEADINGS = {
    "user": "你",
    "agent": "Codex",
    "reasoning": "思考",
    "tool": "工具",
    "error": "錯誤",
    "notice": "提示",
    "meta": "系統",
}


def export_markdown(ws: store.Workspace, node_id: str, path: str) -> dict[str, Any]:
    """Write a readable transcript. One-way: Markdown cannot be imported back."""
    node = ws.find(node_id)
    if node is None:
        raise TransferError("找不到要匯出的項目")

    lines: list[str] = [f"# {ws.path_of(node_id)}", ""]
    count = 0
    for conv in _conversations([node]):
        count += 1
        if node["kind"] == store.PROJECT:
            lines += [f"## {ws.path_of(conv['id'])}", ""]
        settings = ws.resolve(conv["id"])
        lines += [
            f"- 工作目錄：`{settings['cwd']}`",
            f"- 沙箱：`{settings.get('sandbox')}`",
            f"- Codex thread：`{conv.get('thread_id') or '尚未建立'}`",
            "",
        ]
        for message in conv.get("messages") or []:
            heading = _ROLE_HEADINGS.get(message["role"], message["role"])
            lines += [f"**{heading}**", ""]
            text = (message.get("text") or "").rstrip()
            if message["role"] == "tool":
                lines += ["```", text, "```", ""]
            elif text:
                lines += [text, ""]
            for image in message.get("images") or ():
                lines += [f"![{os.path.basename(image)}]({image.replace(os.sep, '/')})", ""]
        lines.append("---")
        lines.append("")

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return {"conversations": count, "path": path}
