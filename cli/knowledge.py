"""M673: CLI commands for knowledge management."""

from __future__ import annotations

import argparse
import sys

from cli._http import cli_delete, cli_get, cli_post


def knowledge_list(args: argparse.Namespace) -> None:
    """List knowledge facts with optional filters."""
    params: dict = {}
    if getattr(args, "category", None):
        params["category"] = args.category
    if getattr(args, "entity", None):
        params["entity"] = args.entity
    if getattr(args, "tag", None):
        params["tag"] = args.tag
    if getattr(args, "limit", None):
        params["limit"] = str(args.limit)
    resp = cli_get(args, "/knowledge", params=params)
    facts = resp.json().get("facts", [])
    if not facts:
        print("No knowledge facts found.")
        return
    for f in facts:
        entity = f.get("entity_name") or ""
        tags = ", ".join(f.get("tags", []))
        cat = f.get("category", "")
        content = f["content"]
        if len(content) > 80:
            content = content[:77] + "..."
        parts = [f"  [{f['id']}]", f"({cat})" if cat else ""]
        if entity:
            parts.append(f"[{entity}]")
        parts.append(content)
        if tags:
            parts.append(f"  #{tags}")
        print(" ".join(p for p in parts if p))


def knowledge_add(args: argparse.Namespace) -> None:
    """Add a knowledge fact."""
    from cli.plugin_ops import require_admin
    require_admin()
    content = args.content
    if not content.strip():
        print("error: content cannot be empty", file=sys.stderr)
        sys.exit(1)
    body: dict = {"content": content}
    if getattr(args, "category", None):
        body["category"] = args.category
    if getattr(args, "entity", None):
        body["entity_name"] = args.entity
    if getattr(args, "entity_kind", None):
        body["entity_kind"] = args.entity_kind
    if getattr(args, "tags", None):
        body["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
    resp = cli_post(args, "/knowledge", json_body=body)
    data = resp.json()
    print(f"Knowledge added (id={data['id']}, category={data['category']}): {data['content']}")


def knowledge_search(args: argparse.Namespace) -> None:
    """Search knowledge facts via FTS5."""
    resp = cli_get(args, "/knowledge", params={"search": args.query, "limit": "20"})
    facts = resp.json().get("facts", [])
    if not facts:
        print("No results found.")
        return
    for f in facts:
        entity = f.get("entity_name") or ""
        content = f["content"]
        if len(content) > 100:
            content = content[:97] + "..."
        prefix = f"  [{f['id']}] ({f.get('category', '')})"
        if entity:
            prefix += f" [{entity}]"
        print(f"{prefix} {content}")


def knowledge_remove(args: argparse.Namespace) -> None:
    """Remove a knowledge fact by ID."""
    from cli.plugin_ops import require_admin
    require_admin()
    resp = cli_delete(args, f"/knowledge/{args.fact_id}")
    data = resp.json()
    if data.get("deleted"):
        print(f"Knowledge fact {args.fact_id} removed.")
    else:
        print(f"error: could not remove fact {args.fact_id}", file=sys.stderr)
        sys.exit(1)


def knowledge_import(args: argparse.Namespace) -> None:
    """Import knowledge from a markdown file."""
    from pathlib import Path

    from cli.plugin_ops import require_admin
    require_admin()

    from kiso.knowledge_import import parse_knowledge_markdown

    path = Path(args.file)
    if not path.is_file():
        print(f"error: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    text = path.read_text(encoding="utf-8")
    default_category = getattr(args, "category", None) or "general"
    facts = parse_knowledge_markdown(text, default_category=default_category)

    if not facts:
        print("No facts found in the file.")
        return

    dry_run = getattr(args, "dry_run", False)
    if dry_run:
        print(f"Dry run — {len(facts)} facts would be imported:\n")
        for f in facts:
            entity = f"[{f.entity_name} ({f.entity_kind})]" if f.entity_name else ""
            tags = " ".join(f"#{t}" for t in f.tags)
            print(f"  ({f.category}) {entity} {f.content} {tags}".strip())
        return

    entities_seen: set[str] = set()
    tags_seen: set[str] = set()
    imported = 0
    for f in facts:
        body: dict = {"content": f.content, "category": f.category}
        if f.entity_name:
            body["entity_name"] = f.entity_name
            entities_seen.add(f.entity_name)
        if f.entity_kind:
            body["entity_kind"] = f.entity_kind
        if f.tags:
            body["tags"] = f.tags
            tags_seen.update(f.tags)
        cli_post(args, "/knowledge", json_body=body)
        imported += 1

    print(f"Imported {imported} facts ({len(entities_seen)} entities, {len(tags_seen)} tags)")
