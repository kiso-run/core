""": CLI commands for knowledge management."""

from __future__ import annotations

import argparse

from cli._http import cli_delete, cli_get, cli_post
from cli.render import die


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
        die("content cannot be empty")
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
        die(f"could not remove fact {args.fact_id}")


def knowledge_export(args: argparse.Namespace) -> None:
    """Export knowledge facts to stdout (JSON or markdown)."""
    params: dict = {}
    if getattr(args, "category", None):
        params["category"] = args.category
    if getattr(args, "entity", None):
        params["entity"] = args.entity
    params["limit"] = "1000"
    resp = cli_get(args, "/knowledge", params=params)
    facts = resp.json().get("facts", [])

    fmt = getattr(args, "format", "json") or "json"
    output_file = getattr(args, "output", None)

    if fmt == "json":
        import json
        text = json.dumps(facts, indent=2, ensure_ascii=False)
    else:
        # Markdown format grouped by entity
        lines: list[str] = []
        by_entity: dict[str, list[dict]] = {}
        no_entity: list[dict] = []
        for f in facts:
            ename = f.get("entity_name")
            if ename:
                by_entity.setdefault(ename, []).append(f)
            else:
                no_entity.append(f)

        for ename, efacts in sorted(by_entity.items()):
            kind = efacts[0].get("entity_kind") or "concept"
            lines.append(f"## Entity: {ename} ({kind})\n")
            for f in efacts:
                tags = " ".join(f"#{t}" for t in f.get("tags", []))
                cat_note = f" [{f['category']}]" if f.get("category") not in ("general", None) else ""
                lines.append(f"- {f['content']}{cat_note} {tags}".rstrip())
            lines.append("")

        if no_entity:
            # Group by category
            behavior_facts = [f for f in no_entity if f.get("category") == "behavior"]
            other_facts = [f for f in no_entity if f.get("category") != "behavior"]
            if behavior_facts:
                lines.append("## Behaviors\n")
                for f in behavior_facts:
                    tags = " ".join(f"#{t}" for t in f.get("tags", []))
                    lines.append(f"- {f['content']} {tags}".rstrip())
                lines.append("")
            if other_facts:
                lines.append("## General\n")
                for f in other_facts:
                    tags = " ".join(f"#{t}" for t in f.get("tags", []))
                    cat_note = f" [{f['category']}]" if f.get("category") not in ("general", None) else ""
                    lines.append(f"- {f['content']}{cat_note} {tags}".rstrip())
                lines.append("")

        text = "\n".join(lines)

    if output_file:
        from pathlib import Path
        Path(output_file).write_text(text, encoding="utf-8")
        print(f"Exported {len(facts)} facts to {output_file}")
    else:
        print(text)


def knowledge_import(args: argparse.Namespace) -> None:
    """Import knowledge from a markdown file."""
    from pathlib import Path

    from cli.plugin_ops import require_admin
    require_admin()

    from kiso.knowledge_import import parse_knowledge_markdown

    path = Path(args.file)
    if not path.is_file():
        die(f"file not found: {path}")

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
