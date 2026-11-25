"""CLI entry point for CodeEvolution."""

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .application.snapshot_runtime import SnapshotRuntime
from .domain.topology import canonical_json
from .mcp_server import run_server
from .paths import analysis_data_dir
from .platform import (
    PlatformCapabilityError,
    atomic_replace,
    fsync_directory,
    fsync_file,
    set_private_permissions,
)
from .registry import (
    check_services,
    discover_repos,
    register_repo,
    repository_members,
)


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_serve(args):
    """Start the snapshot-only MCP server."""
    runtime = SnapshotRuntime(analysis_data_dir())
    print("Serving snapshot MCP on stdio.")
    try:
        run_server(runtime, transport=args.transport)
    finally:
        runtime.close()


def cmd_web(args):
    """Start the web dashboard (multi-repo)."""
    from .api import serve

    print(f"Starting CodeEvolution web server at http://{args.host}:{args.port}")
    serve(host=args.host, port=args.port)


def cmd_register(args):
    """Register a repo in the multi-repo registry."""

    try:
        entry = register_repo(args.name, args.repo)
        print(f"Registered service: {entry['name']}")
        for member in repository_members(entry):
            print(f"  - {member['name']}: {member['path']}")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_repos(args):
    """List registered repos."""
    from .registry import list_repos

    repos = list_repos()
    if not repos:
        print("No repos registered.")
        print("Use 'codeevolution register --name <name> --repo <path>' to register one.")
        return
    for r in repos:
        members = repository_members(r)
        print(f"  {r['name']}: {len(members)} repository(s)")
        for member in members:
            print(f"    - {member['name']}: {member['path']}")


def cmd_knowledge(args):
    """Project knowledge facts from one immutable repository snapshot."""
    runtime = SnapshotRuntime(analysis_data_dir())
    try:
        result = runtime.snapshot_queries.knowledge(
            args.snapshot_id, section=None if args.section == "all" else args.section
        )
        rendered = json.dumps(result, indent=2, ensure_ascii=False, default=str)
        if args.output:
            Path(args.output).write_text(rendered + "\n", encoding="utf-8")
            print(f"Knowledge report written to {args.output}")
        else:
            print(rendered)
    except KeyError:
        print(f"Error: snapshot or section not found: {args.snapshot_id}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    finally:
        runtime.close()


def _adr_member(runtime, snapshot_id: str, member_id: str):
    if member_id:
        member = runtime.store.get_member(member_id)
        if member is None:
            member = next(
                (item for item in runtime.store.list_members() if item.display_name == member_id),
                None,
            )
        if member is None:
            _fail(f"repository member not found: {member_id}", 2)
        return member
    if not snapshot_id:
        _fail("--snapshot-id or --member-id is required", 2)
    snapshot = runtime.store.get_snapshot(snapshot_id)
    if snapshot is None:
        _fail(f"snapshot not found: {snapshot_id}", 2)
    member = runtime.store.get_member(snapshot.member_id)
    if member is None:
        _fail(f"repository member not found for snapshot: {snapshot_id}", 2)
    return member


def _print_json(value) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def cmd_adr(args):
    """Generate, inspect and export Architecture Decision Records."""
    from .adr.service import AdrService, AdrServiceError
    from .adr.store import AdrStore

    runtime = SnapshotRuntime(analysis_data_dir())
    store = None
    try:
        store = AdrStore(analysis_data_dir() / "adr.db")
        service = AdrService(store, runtime.store.get_member)
        command = args.adr_command

        if command == "gen":
            member = _adr_member(runtime, args.snapshot_id, args.member_id)
            if getattr(args, "commits", ""):
                shas = [item.strip() for item in args.commits.split(",") if item.strip()]
                batch = service.batch_generate(member, shas, force=args.force)
                generations = [
                    service.run(item["id"]) if item["status"] == "queued" else item
                    for item in batch["generations"]
                ]
                result = {"batch_id": batch["batch_id"], "generations": generations}
                if args.json:
                    _print_json(result)
                else:
                    for item in generations:
                        _print_adr_generation(item, store)
                return
            if args.history:
                batch = service.start_history(
                    member,
                    since=args.since,
                    until=args.until,
                    limit=args.limit,
                    include_merges=args.include_merges,
                    force=args.force,
                )
                generations = [
                    service.run(item["id"]) if item["status"] == "queued" else item
                    for item in batch["generations"]
                ]
                result = {"batch_id": batch["batch_id"], "generations": generations}
                if args.json:
                    _print_json(result)
                else:
                    for item in generations:
                        _print_adr_generation(item, store)
                return

            kind, source = _adr_source_from_args(args)
            generation = service.generate(member, source, force=args.force)
            detail = service.get_generation(generation["id"])
            if args.json:
                _print_json(detail)
            else:
                _print_adr_generation(detail["generation"], store, detail["draft"])
            return

        if command == "list":
            member = _adr_member(runtime, args.snapshot_id, args.member_id)
            generations = service.list_generations(member.id, limit=args.limit)
            drafts = service.list_drafts(member.id, limit=args.limit)
            if args.json:
                _print_json({"generations": generations, "drafts": drafts})
            else:
                for draft in drafts:
                    backtest = draft.get("backtest") or {}
                    confidence = backtest.get("confidence")
                    print(
                        f"{draft['id']}  {draft['status']:<10} {draft['title']}"
                        f"  [{draft['source_kind']}:{draft['source_ref'][:12]}]"
                        f"  review={draft.get('review_status') or 'pending'}"
                        + (f"  backtest={backtest.get('verdict')}:{confidence}" if confidence is not None else "")
                        + (f"  exported:{draft['exported_path']}" if draft.get("exported_path") else "")
                    )
                if not drafts:
                    print("no ADR drafts yet")
                for generation in generations:
                    if generation["status"] in {"failed", "skipped", "queued", "running"}:
                        print(
                            f"(generation) {generation['id']}  {generation['status']:<10} "
                            f"{generation.get('skip_reason') or generation.get('error_message') or ''}"
                        )
            return

        if command == "show":
            detail = service.get_draft(args.draft_id)
            if args.json:
                _print_json(detail)
            else:
                print(detail["draft"]["markdown"])
            return

        if command == "debug":
            detail = service.get_generation(args.generation_id)
            generation = detail["generation"]
            mapping = {
                "generation_id": generation["id"],
                "status": generation["status"],
                "source": {
                    "kind": generation["source_kind"],
                    "ref": generation["source_ref"],
                    "digest": generation["source_digest"],
                    "payload": generation["source_payload"],
                },
                "prompt": {
                    "origin": "member" if generation["prompt_id"] else "default",
                    "version": generation["prompt_version"],
                    "digest": generation["prompt_digest"],
                    "template": generation["prompt_text"],
                    "rendered": generation.get("prompt_rendered", ""),
                },
                "model": generation["model"],
                "raw_response": generation["raw_response"],
                "parsed": generation.get("parsed"),
                "draft": detail.get("draft"),
                "error": generation.get("error_message") or generation.get("skip_reason") or "",
                "duration_ms": generation["duration_ms"],
            }
            _print_json(mapping)
            return

        if command == "export":
            result = service.export_draft(args.draft_id)
            if args.json:
                _print_json(result)
            else:
                print(f"wrote {result['path']}")
                print(f"index {result['index_path']}")
            return

        if command == "commits":
            member = _adr_member(runtime, args.snapshot_id, args.member_id)
            if args.scan:
                result = service.scan_commits(
                    member,
                    since=args.since,
                    until=args.until,
                    limit=args.limit,
                    include_merges=args.include_merges,
                    refresh=args.refresh,
                )
            else:
                result = service.list_commits(
                    member,
                    since=args.since,
                    until=args.until,
                    limit=args.limit,
                    include_merges=args.include_merges,
                    stage=getattr(args, "stage", "") or "",
                )
            if args.json:
                _print_json(result)
            else:
                progress = result.get("progress") or {}
                print(
                    f"progress: {progress.get('processed', 0)}/{progress.get('total', 0)}"
                    f" ({progress.get('percent', 0)}%), active={progress.get('active', 0)}"
                )
                for item in result["items"]:
                    reason = item["skip_reason"] or item.get("change_type") or ""
                    draft = f" -> {item['draft_title']}" if item.get("draft_title") else ""
                    print(
                        f"{item['sha'][:10]}  {item['stage']:<10} {item['subject'][:56]:56s}"
                        f"  {reason}{draft}"
                    )
            return

        if command == "template":
            member = _adr_member(runtime, args.snapshot_id, args.member_id)
            if args.template_command == "show":
                result = service.template(member.id)
                if args.json:
                    _print_json(result)
                else:
                    current = result["template"]
                    print(f"template: {'member v' + str(current['version']) if current else 'default'}")
                    print(current["template_text"] if current else result["default_template"])
                return
            if args.template_command == "set":
                text = args.text or ""
                if args.file:
                    text = Path(args.file).read_text(encoding="utf-8")
                template = service.save_template(member.id, text)
                result = {"template": template, **service.template(member.id)}
                _print_json(result) if args.json else print(f"saved template v{template['version']}")
                return
            if args.template_command == "reset":
                reset = service.reset_template(member.id)
                result = {"reset": reset, **service.template(member.id)}
                _print_json(result) if args.json else print(
                    f"reset template: {'ok' if reset else 'already default'}"
                )
                return
            _fail("unknown adr template command", 2)

        if command == "backtest":
            result = service.backtest(args.draft_id, force=args.force)
            if args.json:
                _print_json(result)
            else:
                print(f"verdict={result['verdict']} confidence={result['confidence']}")
                for reason in result.get("reasons") or []:
                    print(f"- {reason}")
                if result.get("error_message"):
                    print(f"error: {result['error_message']}")
            return

        if command == "review":
            action = "approve" if args.approve else "reject"
            result = service.review(
                args.draft_id,
                action=action,
                rating=args.rating,
                comment=args.comment,
            )
            if args.json:
                _print_json(result)
            else:
                print(f"draft {result['draft']['id']} -> {result['draft']['status']}")
            return

        if command == "mapping":
            member = _adr_member(runtime, args.snapshot_id, args.member_id)
            result = service.source_mapping(member.id)
            if args.json:
                _print_json(result)
            else:
                for row in result["sources"]:
                    latest = row["latest_draft"]
                    suffix = f" -> {latest['title']}" if latest else ""
                    print(
                        f"{row['source_kind']:<8} prompt={row['prompt_origin']} v{row['prompt_version']}"
                        f" generations={row['generation_count']}{suffix}"
                    )
            return

        if command == "prompts":
            member = _adr_member(runtime, args.snapshot_id, args.member_id)
            if args.prompts_command == "list":
                result = service.prompts(member.id)
                if args.json:
                    _print_json(result)
                else:
                    for item in result["kinds"]:
                        prompt = item["prompt"]
                        origin = "member" if prompt else "default"
                        version = prompt["version"] if prompt else 0
                        print(f"{item['source_kind']:<8} {origin:<8} v{version}")
                return
            if args.prompts_command == "set":
                text = args.text or ""
                if args.file:
                    text = Path(args.file).read_text(encoding="utf-8")
                prompt = service.save_prompt(member.id, args.kind, text)
                result = {"prompt": prompt, "kinds": service.prompts(member.id)["kinds"]}
                _print_json(result) if args.json else print(
                    f"saved {args.kind} prompt v{prompt['version']}"
                )
                return
            if args.prompts_command == "reset":
                reset = service.reset_prompt(member.id, args.kind)
                result = {"reset": reset, "kinds": service.prompts(member.id)["kinds"]}
                _print_json(result) if args.json else print(
                    f"reset {args.kind} prompt: {'ok' if reset else 'already default'}"
                )
                return
            _fail("unknown adr prompts command", 2)

        _fail("unknown adr command", 2)
    except AdrServiceError as error:
        _fail(str(error), 2)
    finally:
        if store is not None:
            store.close()
        runtime.close()


def _adr_source_from_args(args) -> tuple[str, dict]:
    if args.commit:
        return "commit", {"kind": "commit", "ref": args.commit}
    if args.file:
        return "file", {"kind": "file", "path": args.file}
    if args.text:
        return "text", {"kind": "text", "text": args.text}
    _fail("one of --commit, --file, --text or --history is required", 2)


def _print_adr_generation(generation: dict, store, draft: dict | None = None) -> None:
    print(
        f"[{generation['status']}] {generation['source_kind']}:{generation['source_ref'][:20]}"
        f" model={generation['model'] or '-'} prompt=V{generation['prompt_version'] or 'default'}"
    )
    if draft is None and generation.get("draft_id"):
        draft = store.get_draft(generation["draft_id"])
    if draft:
        print(f"  {draft['title']}")
        print(f"  draft: {draft['id']}")
        print(f"  path:  {draft['exported_path'] or '(not exported)'}")
    if generation.get("skip_reason"):
        print(f"  skipped: {generation['skip_reason']}")
    if generation.get("error_message"):
        print(f"  error: {generation['error_message']}")


def _layers_graph(args) -> tuple[Path, Path]:
    """Resolve the CodeGraph database and the repository root that owns it."""
    db_path = Path(args.db) if args.db else Path(".codegraph") / "codegraph.db"
    db_path = db_path.resolve()
    if not db_path.exists():
        _fail(f"code graph not found: {db_path} (run 'codegraph init' in the target repo)", 2)
    return db_path, db_path.parent.parent


def cmd_layers_init(args):
    """Derive a layer declaration from the architecture the repository already has."""
    from .analysis.knowledge.layer_config import CONFIG_RELPATH
    from .application.layer_config_service import LayerConfigService
    from .infrastructure.codegraph_sqlite import SQLiteCodeGraphRepository

    db_path, repo_root = _layers_graph(args)
    target = Path(args.out).resolve() if args.out else repo_root / CONFIG_RELPATH
    if target.exists() and not args.force:
        _fail(f"{target} already exists; pass --force to overwrite", 2)

    repository = SQLiteCodeGraphRepository(str(db_path))
    try:
        payload = LayerConfigService(repository).generate()
    finally:
        repository.close()

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    generated = payload["generated"]
    print(f"Layers config written to {target}")
    print(f"  strategy         : {generated['strategy']}")
    print(
        f"  files classified : {generated['classified_files']}/{generated['file_count']}"
        f" ({generated['coverage']:.0%})"
    )
    print(
        f"  edges classified : {generated['classified_edges']}/{generated['edge_count']}"
    )
    print(f"  layers           : {', '.join(payload['layers'])}")
    print(f"  allowed pairs    : {len(payload['rules']['allow'])}")

    candidates = payload["ratchet"]["remove_candidates"]
    if candidates:
        print("\nRatchet candidates (convention-breaking first, then cheapest):")
        for item in candidates[:8]:
            mark = "break" if item["against_convention"] else "keep "
            print(
                f"  [{mark}] {item['from']:15s} -> {item['to']:15s} {item['edges']:5d} edge(s)"
            )
    print("\nNext: drop a pair from rules.allow to start flagging it.")


def cmd_layers_check(args):
    """Check a code graph against its layer declaration, honouring a baseline."""
    from .analysis.knowledge.layer_config import (
        BASELINE_RELPATH,
        CONFIG_RELPATH,
        builtin_layers_config,
        load_layers_config,
    )
    from .application.layer_config_service import (
        LayerConfigService,
        read_baseline,
        write_baseline,
    )
    from .infrastructure.codegraph_sqlite import SQLiteCodeGraphRepository

    db_path, repo_root = _layers_graph(args)
    config_path = Path(args.config).resolve() if args.config else repo_root / CONFIG_RELPATH
    baseline_path = Path(args.baseline).resolve() if args.baseline else repo_root / BASELINE_RELPATH

    try:
        if config_path.exists():
            config = load_layers_config(config_path)
        else:
            print(
                f"No layers config at {config_path}; using built-in defaults. "
                "Run 'codeevolution layers init' to declare this repository's own layers.",
                file=sys.stderr,
            )
            config = builtin_layers_config()
    except ValueError as error:
        _fail(str(error), 2)

    repository = SQLiteCodeGraphRepository(str(db_path))
    try:
        service = LayerConfigService(repository)
        report = service.check(config, read_baseline(baseline_path))
        if args.write_baseline:
            count = write_baseline(baseline_path, report["known"] + report["new"])
            print(f"Baseline written to {baseline_path} ({count} pair(s))")
            return
    finally:
        repository.close()

    if args.output:
        _canonical_output(_layer_report_payload(report), args.output)
    print(f"config: {config.origin}")
    print(
        f"{len(report['new'])} new violation(s), "
        f"{report['baseline']} baselined, {report['total']} total"
    )
    if not report["new"]:
        return
    for group in report["groups"]:
        print(f"\n  {group['from']} -> {group['to']}  ({group['count']})")
        for item in group["items"][: max(args.max_items, 0)]:
            line = f":{item.call_line}" if item.call_line else ""
            print(f"    {item.source_file}{line}  ->  {item.target_file}")
    raise SystemExit(1)


def _layer_report_payload(report: dict) -> dict:
    def rows(items):
        return [
            {
                "source": item.source_name,
                "source_file": item.source_file,
                "source_layer": item.source_layer,
                "target": item.target_name,
                "target_file": item.target_file,
                "target_layer": item.target_layer,
                "call_line": item.call_line,
            }
            for item in items
        ]

    return {
        "total": report["total"],
        "baselined": report["baseline"],
        "new_count": len(report["new"]),
        "new": rows(report["new"]),
        "baselined_violations": rows(report["known"]),
    }


def cmd_api_contract(args):
    """Export one contract document per endpoint from an immutable snapshot."""
    runtime = None
    explanation_store = None
    try:
        if args.server:
            _status, report, _headers = _request_json(
                args.server,
                "GET",
                "/api/knowledge?" + urlencode({
                    "snapshot_id": args.snapshot_id,
                    "section": "api",
                    "complete": "true",
                    "include_llm": "false",
                }),
            )
            endpoints = (report.get("api_contract") or {}).get("endpoints", [])

            def explanation_loader(endpoint):
                return _remote_main_node_explanation(args.server, args.snapshot_id, endpoint)

        else:
            runtime = SnapshotRuntime(analysis_data_dir())
            report = {"api_contract": runtime.snapshot_queries.api_contract(args.snapshot_id)}
            endpoints = (report.get("api_contract") or {}).get("endpoints", [])
            explanation_path = analysis_data_dir() / "api-explanations.db"
            if explanation_path.exists():
                from .infrastructure.explanation_snapshot_store import ExplanationSnapshotStore

                explanation_store = ExplanationSnapshotStore(explanation_path)

            def explanation_loader(endpoint):
                return _local_main_node_explanation(explanation_store, args.snapshot_id, endpoint)

        if not isinstance(endpoints, list):
            raise CLIContractError("snapshot API contract is invalid", 5)

        documents = [
            _api_contract_document(endpoint, explanation_loader(endpoint))
            for endpoint in endpoints
            if isinstance(endpoint, dict)
        ]
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        extension = "json" if args.format == "json" else "md"
        for index, document in enumerate(documents, start=1):
            filename = _endpoint_filename(document, index, extension)
            target = output_dir / filename
            content = (
                json.dumps(document, indent=2, ensure_ascii=False) + "\n"
                if args.format == "json"
                else _render_api_contract_markdown(document)
            )
            _write_text_output(target, content)
        print(f"Exported {len(documents)} API contract endpoint(s) to {output_dir}")
    except CLIContractError as error:
        _fail(str(error), error.code)
    except (KeyError, RuntimeError, ValueError, OSError) as error:
        _fail(str(error), 3)
    finally:
        if explanation_store is not None:
            explanation_store.close()
        if runtime is not None:
            runtime.close()


def _api_contract_document(endpoint: dict, main_explanation: dict | None) -> dict:
    """Keep the exported contract deliberately narrower than the full report."""
    return {
        "url": endpoint.get("url") or endpoint.get("path") or "",
        "method": str(endpoint.get("method") or "").upper(),
        "request_headers": endpoint.get("request_headers") or [],
        "request_body": endpoint.get("request_body"),
        "path_params": endpoint.get("path_params") or [],
        "query_params": endpoint.get("query_params") or [],
        "response_body": endpoint.get("response_body"),
        "call_chain": _contract_call_chain(endpoint.get("call_chain")),
        "main_node_explanation": main_explanation,
    }


def _contract_call_chain(value) -> list[dict]:
    """Copy call-chain facts while excluding explanation/source-shaped fields."""
    if not isinstance(value, list):
        return []
    excluded = {"source", "source_snippet", "snippet", "explanation", "local_explanation", "aggregate_explanation"}
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        result.append({key: value for key, value in item.items() if key not in excluded})
    return result


def _local_main_node_explanation(store, snapshot_id: str, endpoint: dict) -> dict | None:
    if store is None:
        return None
    from .infrastructure.explanation_source import api_key

    current = store.get_current_for_repository_snapshot(
        snapshot_id,
        api_key(endpoint.get("method", ""), endpoint.get("path") or endpoint.get("url", ""), endpoint.get("handler", "")),
    )
    if current is None:
        return None
    node = store.get_node(current.id, current.entry_node_key)
    if node is None:
        return {
            "snapshot_id": current.id,
            "node_key": current.entry_node_key,
            "status": current.status,
            "local_explanation": None,
            "aggregate_explanation": None,
        }
    return {
        "snapshot_id": current.id,
        "node_key": node.node_key,
        "status": node.status,
        "local_explanation": node.local_explanation,
        "aggregate_explanation": node.aggregate_explanation,
    }


def _remote_main_node_explanation(server: str, snapshot_id: str, endpoint: dict) -> dict | None:
    from .infrastructure.explanation_source import api_key

    query = urlencode({
        "repository_snapshot_id": snapshot_id,
        "api_key": api_key(
            endpoint.get("method", ""),
            endpoint.get("path") or endpoint.get("url", ""),
            endpoint.get("handler", ""),
        ),
    })
    _status, result, _headers = _request_json(
        server, "GET", "/api/api-explanations/current?" + query
    )
    snapshot = result.get("snapshot")
    if not isinstance(snapshot, dict):
        return None
    entry_key = snapshot.get("entry_node_key", "")
    node = next(
        (
            item for item in snapshot.get("nodes", [])
            if isinstance(item, dict) and item.get("node_key") == entry_key
        ),
        None,
    )
    return {
        "snapshot_id": snapshot.get("id", ""),
        "node_key": entry_key,
        "status": (node or {}).get("status") or snapshot.get("status", ""),
        "local_explanation": (node or {}).get("local_explanation"),
        "aggregate_explanation": (node or {}).get("aggregate_explanation"),
    }


def _endpoint_filename(document: dict, index: int, extension: str) -> str:
    method = _filename_part(document.get("method", "endpoint"))
    url = _filename_part(document.get("url", "endpoint"))
    return f"{index:04d}-{method}-{url}.{extension}"


def _filename_part(value: object) -> str:
    value = re.sub(r"[^\w.-]+", "-", str(value or "").strip(), flags=re.UNICODE)
    return value.strip(".-")[:100] or "endpoint"


def _explanation_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _explanation_fact_text(value) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return str(value)
    text = (
        value.get("rule") or value.get("action") or value.get("detail")
        or value.get("summary") or value.get("description")
        or json.dumps(value, ensure_ascii=False)
    )
    lines = [str(line) for line in (value.get("evidence_lines") or []) if isinstance(line, int)]
    return f"{text} (source lines {', '.join(lines)})" if lines else str(text)


def _business_sections(aggregate, local) -> str:
    facts = _explanation_dict(aggregate) or _explanation_dict(local)
    description = str(
        facts.get("summary") or facts.get("business_purpose_zh") or facts.get("business_purpose_en") or ""
    ).strip()
    rules = "\n".join(
        f"- {_explanation_fact_text(rule)}" for rule in (facts.get("business_rules") or [])
    )
    return (
        "## Business Description\n\n"
        f"{description or 'Not generated.'}\n\n"
        "## Business Rules\n\n"
        f"{rules or 'Not generated.'}\n"
    )


def _render_api_contract_markdown(document: dict) -> str:
    def block(value) -> str:
        return "```json\n" + json.dumps(value, indent=2, ensure_ascii=False) + "\n```"

    title = f"{document['method']} {document['url']}".strip()
    explanation = document.get("main_node_explanation")
    local = explanation.get("local_explanation") if explanation else None
    aggregate = explanation.get("aggregate_explanation") if explanation else None
    status = explanation.get("status", "missing") if explanation else "missing"
    return (
        f"# {title}\n\n"
        "## Endpoint Contract\n\n"
        f"- URL: `{document['url']}`\n"
        f"- Method: `{document['method']}`\n\n"
        "### Request Headers\n\n"
        f"{block(document['request_headers'])}\n\n"
        "### Request Body\n\n"
        f"{block(document['request_body'])}\n\n"
        "### Path Parameters\n\n"
        f"{block(document['path_params'])}\n\n"
        "### Query Parameters\n\n"
        f"{block(document['query_params'])}\n\n"
        "### Response Body\n\n"
        f"{block(document['response_body'])}\n\n"
        "## Call Chain\n\n"
        f"{block(document['call_chain'])}\n\n"
        "## Main Node Explanation\n\n"
        f"- Status: `{status}`\n\n"
        "### Local Explanation\n\n"
        f"{block(local)}\n\n"
        "### Aggregate Explanation\n\n"
        f"{block(aggregate)}\n\n"
        f"{_business_sections(aggregate, local)}"
    )


def _terms_local_context():
    from .application.term_service import TermRecognitionService
    from .infrastructure.term_store import TermStore

    runtime = SnapshotRuntime(analysis_data_dir())
    store = TermStore(analysis_data_dir() / "terms.db")
    return runtime, store, TermRecognitionService(store)


def cmd_terms(args):
    """Extract, inspect, review, or export snapshot terminology."""
    try:
        if args.terms_action == "extract":
            payload = {"snapshot_id": args.snapshot_id, "types": args.types.split(",") if args.types else []}
            if args.server:
                _status, result, _headers = _request_json(args.server, "POST", "/api/terms/extract", payload)
            else:
                runtime, store, service = _terms_local_context()
                try:
                    snapshot = runtime.store.get_snapshot(args.snapshot_id)
                    if snapshot is None:
                        raise CLIContractError("repository snapshot not found", 3)
                    result = service.extract(
                        args.snapshot_id, snapshot.member_id,
                        runtime.snapshot_queries.knowledge(args.snapshot_id), args.types.split(",") if args.types else None,
                    )
                finally:
                    store.close()
                    runtime.close()
            _canonical_output(result, args.output)
            return

        if args.terms_action == "review":
            payload = {
                "snapshot_id": args.snapshot_id, "action": args.action,
                "value": json.loads(args.value) if args.value else {},
                "reason": args.reason, "author": args.author,
            }
            if args.server:
                _status, result, _headers = _request_json(
                    args.server, "POST", f"/api/terms/{args.term_id}/review", payload
                )
            else:
                runtime, store, service = _terms_local_context()
                try:
                    result = {"term": service.review(
                        args.snapshot_id, args.term_id, args.action, payload["value"], args.reason, args.author
                    )}
                finally:
                    store.close()
                    runtime.close()
            _canonical_output(result, args.output)
            return

        if args.terms_action == "manual":
            payload = {
                "snapshot_id": args.snapshot_id, "canonical_name": args.canonical_name,
                "term_type": args.term_type, "bounded_context": args.bounded_context,
                "definition": args.definition, "aliases": args.aliases.split(",") if args.aliases else [],
                "reason": args.reason, "author": args.author,
            }
            if args.server:
                _status, result, _headers = _request_json(args.server, "POST", "/api/terms/manual", payload)
            else:
                runtime, store, service = _terms_local_context()
                try:
                    snapshot = runtime.store.get_snapshot(args.snapshot_id)
                    if snapshot is None:
                        raise CLIContractError("repository snapshot not found", 3)
                    result = {"term": service.add_manual(args.snapshot_id, snapshot.member_id, payload)}
                finally:
                    store.close()
                    runtime.close()
            _canonical_output(result, args.output)
            return

        if args.terms_action == "align":
            payload = {"view_id": args.view_id, "service_ids": args.service_ids.split(",") if args.service_ids else []}
            if args.server:
                _status, result, _headers = _request_json(args.server, "POST", "/api/terms/align", payload)
            else:
                runtime, store, service = _terms_local_context()
                try:
                    view = runtime.store.get_view(args.view_id)
                    if view is None:
                        raise CLIContractError("graph view not found", 3)
                    selected = set(payload["service_ids"])
                    reports = []
                    for member in view.members:
                        if (selected and member.member_id not in selected) or not member.snapshot_id:
                            continue
                        snapshot = runtime.store.get_snapshot(member.snapshot_id)
                        if snapshot is None:
                            continue
                        report = service.extract(member.snapshot_id, snapshot.member_id, runtime.snapshot_queries.knowledge(member.snapshot_id))
                        reports.append({**report, "service_id": member.member_id, "display_name": member.display_name or member.member_id})
                    from .application.snapshot_topology_service import SnapshotTopologyService
                    topology = SnapshotTopologyService(runtime.store, runtime.snapshot_queries).topology(args.view_id)
                    alignment = service.align(reports, service_edges=topology.get("edges", []))
                    result = {"view_id": args.view_id, **alignment, "persisted": service.save_alignments(args.view_id, alignment)}
                finally:
                    store.close()
                    runtime.close()
            _canonical_output(result, args.output)
            return

        query = {"snapshot_id": args.snapshot_id, "limit": args.limit, "offset": args.offset}
        if getattr(args, "term_type", ""):
            query["type"] = args.term_type
        if getattr(args, "status", ""):
            query["status"] = args.status
        if getattr(args, "confidence_band", ""):
            query["confidence_band"] = args.confidence_band
        if getattr(args, "term", ""):
            query["name"] = args.term
        if args.server:
            if args.terms_action == "explain":
                _status, listing, _headers = _request_json(args.server, "GET", "/api/terms?" + urlencode(query))
                items = listing.get("terms", [])
                if not items:
                    raise CLIContractError("term not found", 3)
                _status, result, _headers = _request_json(
                    args.server, "GET", f"/api/terms/{items[0]['id']}?{urlencode({'snapshot_id': args.snapshot_id})}"
                )
                _status, evidence, _headers = _request_json(
                    args.server, "GET", f"/api/terms/{items[0]['id']}/evidence?{urlencode({'snapshot_id': args.snapshot_id})}"
                )
                result["evidence"] = evidence.get("evidence", [])
            else:
                _status, result, _headers = _request_json(args.server, "GET", "/api/terms?" + urlencode(query))
                if args.terms_action == "export" and not args.output:
                    result = _remote_term_export(args.server, args.snapshot_id, result)
        else:
            runtime, store, service = _terms_local_context()
            try:
                listing = service.list(**query)
                if args.terms_action == "explain":
                    if not listing["terms"]:
                        raise CLIContractError("term not found", 3)
                    term = listing["terms"][0]
                    result = {"term": term, "evidence": service.evidence(args.snapshot_id, term["id"])}
                elif args.terms_action == "export" and not args.output:
                    result = _local_term_export(args.snapshot_id, listing, service)
                else:
                    result = listing
            finally:
                store.close()
                runtime.close()
        if args.terms_action == "export" and not args.output:
            _write_term_export(result, args.output_dir, args.format)
        else:
            _canonical_output(result, args.output)
    except CLIContractError:
        raise
    except (KeyError, RuntimeError, ValueError, OSError) as error:
        raise CLIContractError(str(error), 3) from error


class CLIContractError(RuntimeError):
    """A mapped public CLI error with a documented process exit code."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def _fail(message: str, code: int) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(code)


def _canonical_output(value: object, output: str) -> None:
    """Print canonical JSON or atomically replace a caller-owned output file."""
    rendered = canonical_json(value) + "\n"
    if not output:
        print(rendered, end="")
        return
    _write_text_output(Path(output), rendered)


def _write_text_output(target: Path, content: str) -> None:
    """Atomically write one UTF-8 CLI artifact with private permissions."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            fsync_file(handle)
        set_private_permissions(temporary)
        atomic_replace(temporary, target)
        fsync_directory(target.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _local_term_export(snapshot_id: str, listing: dict, service) -> list[dict]:
    return [
        _term_export_document(snapshot_id, term, service.evidence(snapshot_id, term["id"]))
        for term in listing.get("terms", [])
        if isinstance(term, dict) and term.get("id")
    ]


def _remote_term_export(server: str, snapshot_id: str, listing: dict) -> list[dict]:
    documents = []
    for term in listing.get("terms", []):
        if not isinstance(term, dict) or not term.get("id"):
            continue
        _status, evidence, _headers = _request_json(
            server,
            "GET",
            f"/api/terms/{term['id']}/evidence?{urlencode({'snapshot_id': snapshot_id})}",
        )
        documents.append(_term_export_document(snapshot_id, term, evidence.get("evidence", [])))
    return documents


def _term_export_document(snapshot_id: str, term: dict, evidence: list) -> dict:
    """Keep one exported artifact self-contained and limited to term facts/evidence."""
    return {"snapshot_id": snapshot_id, **term, "evidence": evidence}


def _term_filename(document: dict, index: int, extension: str) -> str:
    name = _filename_part(document.get("canonical_name") or document.get("id"))
    term_type = _filename_part(document.get("term_type", "term"))
    return f"{index:04d}-{name}-{term_type}.{extension}"


def _render_term_markdown(document: dict) -> str:
    def block(value) -> str:
        return "```json\n" + json.dumps(value, indent=2, ensure_ascii=False) + "\n```"

    evidence = document.get("evidence") or []
    aliases = document.get("aliases") or []
    risk_flags = document.get("risk_flags") or []
    alias_lines = [f"- `{item}`" for item in aliases] or ["_None._"]
    risk_flag_lines = [f"- `{item}`" for item in risk_flags] or ["_None._"]
    lines = [
        f"# {document.get('canonical_name') or document.get('id') or 'Term'}",
        "",
        "## Term",
        "",
        f"- Snapshot ID: `{document.get('snapshot_id', '')}`",
        f"- ID: `{document.get('id', '')}`",
        f"- Type: `{document.get('term_type', '')}`",
        f"- Bounded Context: `{document.get('bounded_context', '')}`",
        f"- Status: `{document.get('status', '')}`",
        f"- Confidence: `{document.get('confidence_score', '')}` ({document.get('confidence_band', '')})",
        f"- Domain Score: `{document.get('domain_score', '')}`",
        f"- Source: `{document.get('source', '')}`",
        "",
        "## Definition",
        "",
        document.get("definition") or "_No definition provided._",
        "",
        "## Normalized Name",
        "",
        f"`{document.get('normalized_name', '')}`",
        "",
        "## Aliases",
        "",
        *alias_lines,
        "",
        "## Risk Flags",
        "",
        *risk_flag_lines,
        "",
        "## Evidence",
        "",
    ]
    if not evidence:
        lines.append("_No evidence recorded._")
    else:
        for index, item in enumerate(evidence, start=1):
            lines.extend([
                f"### {index}. {item.get('evidence_type', 'evidence')}",
                "",
                f"- Value: `{item.get('evidence_value', '')}`",
                f"- Weight: `{item.get('weight', '')}`",
                f"- Node ID: `{item.get('node_id') or ''}`",
                f"- Rule ID: `{item.get('rule_id') or ''}`",
                "",
                "Source location:",
                "",
                block(item.get("source_location") or {}),
                "",
            ])
    return "\n".join(lines) + "\n"


def _write_term_export(documents: list[dict], output_dir: str, file_format: str) -> None:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    extension = "json" if file_format == "json" else "md"
    for index, document in enumerate(documents, start=1):
        target = output_path / _term_filename(document, index, extension)
        content = (
            json.dumps(document, indent=2, ensure_ascii=False) + "\n"
            if file_format == "json"
            else _render_term_markdown(document)
        )
        _write_text_output(target, content)
    print(f"Exported {len(documents)} term(s) to {output_path}")


def _parse_channels(value: str) -> tuple[str, ...]:
    channels = tuple(item.strip() for item in value.split(",") if item.strip())
    allowed = {"http", "message", "grpc"}
    if not channels or any(item not in allowed for item in channels):
        raise CLIContractError("--channels must be a non-empty subset of http,message,grpc")
    return channels


def _request_json(server: str, method: str, path: str, payload: dict | None = None) -> tuple[int, dict, dict[str, str]]:
    """Perform one small JSON HTTP request without introducing a CLI SDK."""
    base = server.rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise CLIContractError("--server must be an http(s) URL")
    body = None if payload is None else canonical_json(payload).encode("utf-8")
    request = Request(
        base + path, data=body, method=method,
        headers={"Accept": "application/json", **({"Content-Type": "application/json"} if body else {})},
    )
    try:
        with urlopen(request, timeout=30) as response:  # nosec B310 - explicit CLI endpoint
            raw = response.read()
            try:
                parsed = json.loads(raw or b"{}")
            except json.JSONDecodeError as error:
                raise CLIContractError("server returned invalid JSON", 5) from error
            if not isinstance(parsed, dict):
                raise CLIContractError("server returned invalid JSON", 5)
            return response.status, parsed, dict(response.headers.items())
    except HTTPError as error:
        raw = error.read()
        try:
            detail = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            detail = {}
        message = detail.get("detail") or detail.get("error", {}).get("code") or error.reason
        raise CLIContractError(str(message), _http_exit_code(error.code, str(message))) from error
    except (URLError, TimeoutError, OSError) as error:
        raise CLIContractError(f"cannot reach server: {error}", 3) from error


def _http_exit_code(status: int, message: str) -> int:
    if status in {404, 410, 424}:
        return 5 if status in {410, 424} else 3
    if status == 409 and message in {"artifact_generation_failed", "unsupported_artifact_builder_version"}:
        return 4
    if status >= 500:
        return 4
    return 2


def _payload_from_cache(runtime, cached: dict) -> dict:
    payload_json = cached.get("payload_json")
    if not payload_json and cached.get("payload_storage") == "artifact":
        try:
            lease_factory = getattr(runtime.store, "artifact_cache_reader", None)
            lease = (
                lease_factory(cached["cache_key_digest"])
                if callable(lease_factory) and cached.get("cache_key_digest")
                else nullcontext()
            )
            with lease:
                root = runtime.artifacts.open(cached["artifact_key"])
                from .infrastructure.artifact_store_fs import directory_digest

                if directory_digest(root) != str(cached["artifact_key"]).removeprefix("sha256:"):
                    raise ValueError("artifact directory digest mismatch")
                payload_json = (root / "payload.json").read_text(encoding="utf-8")
        except (KeyError, OSError, ValueError, RuntimeError) as error:
            raise CLIContractError("snapshot_artifact_corrupt", 5) from error
    if not payload_json:
        raise CLIContractError("topology artifact payload is unavailable", 5)
    try:
        value = json.loads(payload_json)
    except json.JSONDecodeError as error:
        raise CLIContractError("snapshot_artifact_corrupt", 5) from error
    if not isinstance(value, dict):
        raise CLIContractError("invalid topology artifact payload", 5)
    expected_digest = cached.get("payload_digest")
    if expected_digest:
        from .domain.topology import canonical_digest

        digest_payload = dict(value)
        digest_payload.pop("payload_digest", None)
        if canonical_digest(digest_payload) != expected_digest:
            raise CLIContractError("snapshot_artifact_corrupt", 5)
    return value


def _local_topology(args, *, generate: bool) -> dict:
    runtime = SnapshotRuntime(analysis_data_dir())
    owned_job_id = None
    try:
        service = runtime.graph_artifacts
        if not generate:
            cached = service.get(args.view_id, "topology", {})
            if cached is None:
                raise CLIContractError("artifact_not_generated; run `codeevolution topology --view-id ...` first", 3)
            return _payload_from_cache(runtime, cached)
        if args.no_wait:
            raise CLIContractError("--no-wait requires --server", 2)
        job = service.create_job(args.view_id, "topology", {})
        if not job.get("reused"):
            owned_job_id = job.get("id")
        if job.get("status") == "pending":
            job = service.run_job(job["id"])
        deadline = time.monotonic() + args.timeout
        while job.get("status") in {"pending", "running"}:
            if time.monotonic() >= deadline:
                raise CLIContractError("topology job is still running", 3)
            time.sleep(args.poll_interval)
            current = runtime.store.get_artifact_job(job["id"])
            if current is None:
                raise CLIContractError("job_not_found", 3)
            job = current
        if job.get("status") != "completed":
            raise CLIContractError(job.get("error_code") or job.get("error_message") or "artifact_generation_failed", 4)
        cached = service.get(args.view_id, "topology", {})
        if cached is None:
            raise CLIContractError("artifact_not_generated", 3)
        return _payload_from_cache(runtime, cached)
    except KeyboardInterrupt:
        if owned_job_id:
            try:
                runtime.store.cancel_artifact_job(owned_job_id)
            except (KeyError, RuntimeError):
                pass
        raise
    except CLIContractError:
        raise
    except KeyError as error:
        raise CLIContractError("view_not_found", 2) from error
    except ValueError as error:
        code = 5 if str(error) in {"snapshot_unavailable", "snapshot_artifact_corrupt"} else 2
        raise CLIContractError(str(error), code) from error
    finally:
        runtime.close()


def _remote_topology(args, *, generate: bool) -> dict | None:
    if not generate:
        _status, envelope, _headers = _request_json(
            args.server, "GET", f"/api/graph-views/{args.view_id}/artifacts/topology"
        )
        return _artifact_from_envelope(envelope)
    status, response, headers = _request_json(
        args.server, "POST", f"/api/graph-views/{args.view_id}/artifact-jobs",
        {"artifact_kind": "topology", "params": {}},
    )
    if status == 200 and "artifact" in response:
        return _artifact_from_envelope(response)
    job = response.get("job", response)
    job_id = job.get("id") or job.get("job_id")
    if not job_id:
        raise CLIContractError("server returned no graph artifact job", 4)
    if args.no_wait:
        _canonical_output({"job_id": job_id, "status": job.get("status"), "status_url": f"/api/graph-artifact-jobs/{job_id}"}, args.output)
        return None
    retry_after = float(headers.get("Retry-After", args.poll_interval))
    deadline = time.monotonic() + args.timeout
    while job.get("status") in {"pending", "running"}:
        if time.monotonic() >= deadline:
            raise CLIContractError("topology job is still running", 3)
        time.sleep(max(0.01, retry_after))
        _status, status_response, headers = _request_json(args.server, "GET", f"/api/graph-artifact-jobs/{job_id}")
        job = status_response.get("job", status_response)
        retry_after = float(headers.get("Retry-After", args.poll_interval))
    if job.get("status") != "completed":
        raise CLIContractError(job.get("error_code") or job.get("error_message") or "artifact_generation_failed", 4)
    _status, envelope, _headers = _request_json(
        args.server, "GET", f"/api/graph-views/{args.view_id}/artifacts/topology"
    )
    return _artifact_from_envelope(envelope)


def _artifact_from_envelope(value: dict) -> dict:
    artifact = value.get("artifact", value)
    if not isinstance(artifact, dict):
        raise CLIContractError("invalid topology artifact response", 5)
    return artifact


def _topology_for_query(args) -> dict:
    return _remote_topology(args, generate=False) if args.server else _local_topology(args, generate=False)


def _resolve_member_id(artifact: dict, selector: str) -> str:
    services = artifact.get("services", [])
    ids = {item.get("member_id") for item in services if item.get("member_id")}
    if selector in ids:
        return selector
    matches = [item["member_id"] for item in services if item.get("display_name") == selector]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise CLIContractError("service display name is ambiguous; use member ID")
    raise CLIContractError("member_not_in_view")


def main():
    parser = argparse.ArgumentParser(
        prog="codeevolution",
        description="CodeEvolution — codebase feature evolution analysis",
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # serve
    p = subparsers.add_parser("serve", help="Start snapshot-only MCP server")
    p.add_argument(
        "--transport",
        "-t",
        default="stdio",
        choices=["stdio", "sse", "streamable-http"],
        help="MCP transport (default: stdio)",
    )
    p.set_defaults(handler=cmd_serve)

    # web
    p = subparsers.add_parser("web", help="Start web dashboard (multi-repo)")
    p.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    p.add_argument("--port", type=int, default=8765, help="Port to bind (default: 8765)")
    p.add_argument("--instance-nonce", default="", help=argparse.SUPPRESS)
    p.set_defaults(handler=cmd_web)

    # register
    p = subparsers.add_parser("register", help="Register a repo for multi-repo dashboard")
    p.add_argument("--name", "-n", required=True, help="Short name for the repo")
    p.add_argument(
        "--repo",
        "-r",
        required=True,
        action="append",
        help="Path to git repository (repeat to group repositories as one service)",
    )
    p.set_defaults(handler=cmd_register)

    # repos
    p = subparsers.add_parser("repos", help="List registered repos")
    p.set_defaults(handler=cmd_repos)

    # knowledge
    p = subparsers.add_parser("knowledge", help="Read knowledge from an immutable snapshot")
    p.add_argument("--snapshot-id", required=True, help="Published repository snapshot ID")
    p.add_argument("--output", "-o", default="", help="Output JSON file path (default: stdout)")
    p.add_argument(
        "--section",
        "-s",
        default="all",
        choices=[
            "all",
            "api",
            "adr",
            "interface",
            "modules",
            "entities",
            "layers",
            "config",
            "deps",
            "auth",
            "heatmap",
            "business",
            "rules",
            "errors",
            "states",
        ],
        help="Which knowledge section to extract (default: all)",
    )
    p.set_defaults(handler=cmd_knowledge)

    # adr
    p = subparsers.add_parser("adr", help="Generate and export Architecture Decision Records")
    adr_sub = p.add_subparsers(dest="adr_command", help="ADR sub-commands")

    gen = adr_sub.add_parser("gen", help="Generate an ADR draft from a commit, file or text")
    gen.add_argument("--snapshot-id", default="", help="Snapshot used to resolve the repository service")
    gen.add_argument("--member-id", default="", help="Repository member id or display name")
    source = gen.add_mutually_exclusive_group()
    source.add_argument("--commit", nargs="?", const="HEAD", default="", help="Commit ref (default HEAD)")
    source.add_argument("--file", default="", help="Repository-relative file path")
    source.add_argument("--text", default="", help="One-sentence decision description")
    source.add_argument("--history", action="store_true", help="Generate from recent commits")
    source.add_argument("--commits", default="", help="Comma-separated commit SHAs to generate")
    gen.add_argument("--since", default="", help="History lower bound (git --since)")
    gen.add_argument("--until", default="", help="History upper bound (git --until)")
    gen.add_argument("--limit", type=int, default=10, help="History candidate limit (default 10)")
    gen.add_argument("--include-merges", action="store_true", help="Include merge commits")
    gen.add_argument("--force", action="store_true", help="Bypass the docs/tests/lockfile signal filter")
    gen.add_argument("--json", action="store_true", help="Machine-readable output")
    gen.set_defaults(handler=cmd_adr)

    listing = adr_sub.add_parser("list", help="List ADR drafts and recent generations")
    listing.add_argument("--snapshot-id", default="")
    listing.add_argument("--member-id", default="")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(handler=cmd_adr)

    show = adr_sub.add_parser("show", help="Print one ADR draft as MADR Markdown")
    show.add_argument("draft_id")
    show.add_argument("--json", action="store_true")
    show.set_defaults(handler=cmd_adr)

    debug = adr_sub.add_parser("debug", help="Show the source -> prompt -> model -> ADR mapping")
    debug.add_argument("generation_id")
    debug.set_defaults(handler=cmd_adr)

    export = adr_sub.add_parser("export", help="Write a draft into the repository ADR directory")
    export.add_argument("draft_id")
    export.add_argument("--json", action="store_true")
    export.set_defaults(handler=cmd_adr)

    commits = adr_sub.add_parser("commits", help="List commits with AI triage and draft status")
    commits.add_argument("--snapshot-id", default="")
    commits.add_argument("--member-id", default="")
    commits.add_argument("--since", default="")
    commits.add_argument("--until", default="")
    commits.add_argument("--limit", type=int, default=50)
    commits.add_argument("--include-merges", action="store_true")
    commits.add_argument(
        "--stage",
        default="",
        choices=["", "pending", "queued", "generating", "generated", "backtested",
                 "approved", "rejected", "exported", "skipped", "failed"],
        help="Only print commits at this lifecycle stage",
    )
    commits.add_argument("--scan", action="store_true", help="Run the AI triage before printing")
    commits.add_argument("--refresh", action="store_true", help="Re-run triage for cached commits")
    commits.add_argument("--json", action="store_true")
    commits.set_defaults(handler=cmd_adr)

    template = adr_sub.add_parser("template", help="Inspect or override the MADR output template")
    template_sub = template.add_subparsers(dest="template_command", help="Template sub-commands")
    for name in ("show", "set", "reset"):
        sub = template_sub.add_parser(name)
        sub.add_argument("--snapshot-id", default="")
        sub.add_argument("--member-id", default="")
        if name == "set":
            sub.add_argument("--file", default="", help="Read the template from a file")
            sub.add_argument("--text", default="", help="Template text")
        sub.add_argument("--json", action="store_true")
        sub.set_defaults(handler=cmd_adr)

    backtest = adr_sub.add_parser("backtest", help="Backtest a draft against the current checkout")
    backtest.add_argument("draft_id")
    backtest.add_argument("--force", action="store_true")
    backtest.add_argument("--json", action="store_true")
    backtest.set_defaults(handler=cmd_adr)

    review = adr_sub.add_parser("review", help="Approve or reject an ADR draft")
    review.add_argument("draft_id")
    group = review.add_mutually_exclusive_group(required=True)
    group.add_argument("--approve", action="store_true")
    group.add_argument("--reject", action="store_true")
    review.add_argument("--rating", type=int, choices=[1, 2, 3, 4, 5], default=None)
    review.add_argument("--comment", default="")
    review.add_argument("--json", action="store_true")
    review.set_defaults(handler=cmd_adr)

    mapping = adr_sub.add_parser("mapping", help="Show source -> prompt -> ADR mapping")
    mapping.add_argument("--snapshot-id", default="")
    mapping.add_argument("--member-id", default="")
    mapping.add_argument("--json", action="store_true")
    mapping.set_defaults(handler=cmd_adr)

    prompts = adr_sub.add_parser("prompts", help="Inspect or override per-service ADR prompts")
    prompts_sub = prompts.add_subparsers(dest="prompts_command", help="Prompt sub-commands")
    for name in ("list", "set", "reset"):
        sub = prompts_sub.add_parser(name)
        sub.add_argument("--snapshot-id", default="")
        sub.add_argument("--member-id", default="")
        if name != "list":
            sub.add_argument("--kind", required=True, choices=["commit", "file", "text", "triage", "backtest"])
        if name == "set":
            sub.add_argument("--file", default="", help="Read the prompt from a file")
            sub.add_argument("--text", default="", help="Prompt text")
        sub.add_argument("--json", action="store_true")
        sub.set_defaults(handler=cmd_adr)

    # layers
    p = subparsers.add_parser("layers", help="Generate or check architecture-layer rules")
    layer_parsers = p.add_subparsers(dest="layers_command", help="Layers sub-commands")

    init = layer_parsers.add_parser(
        "init", help="Derive layers.json from the code graph the repository already has"
    )
    init.add_argument("--db", default="", help="CodeGraph SQLite path (default: ./.codegraph/codegraph.db)")
    init.add_argument("--out", "-o", default="", help="Output path (default: <repo>/.codeevolution/layers.json)")
    init.add_argument("--force", action="store_true", help="Overwrite an existing layers.json")
    init.set_defaults(handler=cmd_layers_init)

    check = layer_parsers.add_parser("check", help="Check a code graph against layers.json")
    check.add_argument("--db", default="", help="CodeGraph SQLite path (default: ./.codegraph/codegraph.db)")
    check.add_argument("--config", default="", help="Layers config (default: <repo>/.codeevolution/layers.json)")
    check.add_argument("--baseline", default="", help="Baseline path (default: <repo>/.codeevolution/layers.baseline.json)")
    check.add_argument("--write-baseline", action="store_true", help="Accept current violations as the baseline")
    check.add_argument("--max-items", type=int, default=10, help="Violations to print per layer pair (default: 10)")
    check.add_argument("--output", "-o", default="", help="Write the full report as JSON")
    check.set_defaults(handler=cmd_layers_check)

    # API contract export
    p = subparsers.add_parser(
        "api-contract",
        help="Export one Markdown or JSON contract file per endpoint from a snapshot",
    )
    p.add_argument("--snapshot-id", required=True, help="Published repository snapshot ID")
    p.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="File format (default: markdown)",
    )
    p.add_argument(
        "--output-dir",
        "-o",
        default="api-contract",
        help="Directory for one file per endpoint (default: ./api-contract)",
    )
    p.add_argument("--server", default="", help="Optional CodeEvolution HTTP server URL")
    p.set_defaults(handler=cmd_api_contract)

    # evidence-backed terminology
    p = subparsers.add_parser("terms", help="Extract and review terminology from a snapshot")
    term_parsers = p.add_subparsers(dest="terms_action", required=True)

    extract = term_parsers.add_parser("extract", help="Extract ranked terms")
    extract.add_argument("--snapshot-id", required=True)
    extract.add_argument("--types", default="", help="Comma-separated term types")
    extract.add_argument("--server", default="", help="Optional CodeEvolution HTTP server URL")
    extract.add_argument("--output", "-o", default="")
    extract.set_defaults(handler=cmd_terms)

    for action, help_text in (("list", "List persisted terms"), ("explain", "Show a term and its evidence")):
        item = term_parsers.add_parser(action, help=help_text)
        item.add_argument("--snapshot-id", required=True)
        item.add_argument("--term", default="", help="Term name or normalized name")
        item.add_argument("--term-type", default="")
        item.add_argument("--status", default="")
        item.add_argument("--confidence-band", default="")
        item.add_argument("--limit", type=int, default=100)
        item.add_argument("--offset", type=int, default=0)
        item.add_argument("--server", default="", help="Optional CodeEvolution HTTP server URL")
        item.add_argument("--output", "-o", default="")
        item.set_defaults(handler=cmd_terms)

    export = term_parsers.add_parser(
        "export", help="Export one Markdown or JSON file per persisted term"
    )
    export.add_argument("--snapshot-id", required=True)
    export.add_argument("--term", default="", help="Term name or normalized name")
    export.add_argument("--term-type", default="")
    export.add_argument("--status", default="")
    export.add_argument("--confidence-band", default="")
    export.add_argument("--limit", type=int, default=100)
    export.add_argument("--offset", type=int, default=0)
    export.add_argument("--format", choices=("markdown", "json"), default="markdown")
    export.add_argument("--output-dir", "-d", default="terms")
    export.add_argument("--server", default="", help="Optional CodeEvolution HTTP server URL")
    export.add_argument(
        "--output", "-o", default="",
        help="Legacy aggregate JSON output file; omit to write one file per term",
    )
    export.set_defaults(handler=cmd_terms)

    review = term_parsers.add_parser("review", help="Review a term candidate")
    review.add_argument("--snapshot-id", required=True)
    review.add_argument("--term-id", required=True)
    review.add_argument("--action", required=True, choices=["accept", "reject", "rename", "reclassify", "alias", "relate"])
    review.add_argument("--value", default="", help="JSON action payload")
    review.add_argument("--reason", default="")
    review.add_argument("--author", default="")
    review.add_argument("--server", default="")
    review.add_argument("--output", "-o", default="")
    review.set_defaults(handler=cmd_terms)

    manual = term_parsers.add_parser("manual", help="Add a manually defined term")
    manual.add_argument("--snapshot-id", required=True)
    manual.add_argument("--canonical-name", required=True)
    manual.add_argument("--term-type", default="entity")
    manual.add_argument("--bounded-context", default="")
    manual.add_argument("--definition", default="")
    manual.add_argument("--aliases", default="")
    manual.add_argument("--reason", default="")
    manual.add_argument("--author", default="")
    manual.add_argument("--server", default="")
    manual.add_argument("--output", "-o", default="")
    manual.set_defaults(handler=cmd_terms)

    align = term_parsers.add_parser("align", help="Align accepted entity terms across a Graph View")
    align.add_argument("--view-id", required=True)
    align.add_argument("--service-ids", default="", help="Comma-separated member IDs")
    align.add_argument("--server", default="")
    align.add_argument("--output", "-o", default="")
    align.set_defaults(handler=cmd_terms)

    # cross-repo topology
    p = subparsers.add_parser(
        "topology", help="Generate or read the immutable topology for a Graph View"
    )
    p.add_argument("--view-id", required=True, help="Immutable single-Scope Graph View ID")
    p.add_argument("--server", default="", help="Optional CodeEvolution HTTP server URL")
    wait_group = p.add_mutually_exclusive_group()
    wait_group.add_argument("--wait", dest="no_wait", action="store_false", help="Wait for a remote job (default)")
    wait_group.add_argument("--no-wait", action="store_true", help="Create/reuse a remote job and print its ID")
    p.set_defaults(no_wait=False)
    p.add_argument("--timeout", type=float, default=300.0, help="Maximum wait time in seconds (default: 300)")
    p.add_argument("--poll-interval", type=float, default=2.0, help="Polling interval in seconds (default: 2)")
    p.add_argument("--output", "-o", default="", help="Atomically write canonical JSON to this file")
    p.set_defaults(handler=cmd_topology)

    # cross-repo impact
    p = subparsers.add_parser("impact", help="Query dependency impact from a generated topology")
    p.add_argument("--view-id", required=True, help="Immutable single-Scope Graph View ID")
    p.add_argument("--service", "-s", required=True, help="Member ID or unique exact frozen display name")
    p.add_argument("--server", default="", help="Optional CodeEvolution HTTP server URL")
    p.add_argument("--direction", choices=("upstream", "downstream", "both"), default="both")
    p.add_argument("--max-depth", type=int, default=5)
    p.add_argument("--channels", default="http,message,grpc")
    p.add_argument("--no-resources", dest="include_resources", action="store_false", help="Exclude subject resources")
    p.add_argument("--include-shared-resource-risks", action="store_true")
    p.add_argument("--include-candidates", action="store_true")
    p.add_argument("--output", "-o", default="", help="Atomically write canonical JSON to this file")
    p.set_defaults(include_resources=True)
    p.set_defaults(handler=cmd_impact)

    # discover
    p = subparsers.add_parser(
        "discover", help="Scan directory for git repos and suggest registrations"
    )
    p.add_argument("--dir", "-d", default=".", help="Root directory to scan (default: current)")
    p.set_defaults(handler=cmd_discover)

    # check
    p = subparsers.add_parser("check", help="Health check all registered services")
    p.set_defaults(handler=cmd_check)

    p = subparsers.add_parser(
        "flow", help="Query a rooted static possible flow from a generated topology"
    )
    p.add_argument("--view-id", required=True, help="Immutable single-Scope Graph View ID")
    p.add_argument("--service", "-s", required=True, help="Member ID or unique exact frozen display name")
    entry_group = p.add_mutually_exclusive_group(required=True)
    entry_group.add_argument("--entry-id", default="", help="Stable entry ID")
    entry_group.add_argument("--method", default="", help="HTTP method; requires --path")
    p.add_argument("--path", default="", help="HTTP route path; requires --method")
    p.add_argument("--server", default="", help="Optional CodeEvolution HTTP server URL")
    p.add_argument("--max-depth", type=int, default=8)
    p.add_argument("--max-nodes", type=int, default=500)
    p.add_argument("--max-edges", type=int, default=1000)
    p.add_argument("--channels", default="http,message,grpc")
    p.add_argument("--include-resources", action="store_true")
    p.add_argument("--include-candidates", action="store_true")
    p.add_argument("--output", "-o", default="", help="Atomically write canonical JSON to this file")
    p.set_defaults(handler=cmd_flow)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    setup_logging(getattr(args, "verbose", False))

    args.handler(args)


def cmd_topology(args):
    """Explicitly generate (or reuse) a Graph View's topology artifact."""
    try:
        if args.timeout < 0 or args.poll_interval <= 0:
            raise CLIContractError("--timeout must be non-negative and --poll-interval must be positive")
        artifact = _remote_topology(args, generate=True) if args.server else _local_topology(args, generate=True)
        if artifact is not None:
            _canonical_output(artifact, args.output)
    except KeyboardInterrupt:
        _fail("interrupted while waiting; the shared job was not cancelled", 130)
    except CLIContractError as error:
        _fail(str(error), error.code)


def cmd_impact(args):
    """Synchronously query dependency impact; never generate topology implicitly."""
    try:
        channels = _parse_channels(args.channels)
        if args.max_depth < 0 or args.max_depth > 20:
            raise CLIContractError("--max-depth must be between 0 and 20")
        if args.server:
            member_id = _resolve_member_id(_topology_for_query(args), args.service)
            params = urlencode({
                "member_id": member_id, "direction": args.direction, "max_depth": args.max_depth,
                "channels": ",".join(channels), "include_resources": str(args.include_resources).lower(),
                "include_shared_resource_risks": str(args.include_shared_resource_risks).lower(),
                "include_candidates": str(args.include_candidates).lower(),
            })
            _status, result, _headers = _request_json(args.server, "GET", f"/api/graph-views/{args.view_id}/impact?{params}")
        else:
            artifact = _topology_for_query(args)
            member_id = _resolve_member_id(artifact, args.service)
            from .application.topology_query_service import TopologyQueryService

            result = TopologyQueryService().impact(
                artifact, member_id, direction=args.direction, max_depth=args.max_depth,
                channels=channels, include_resources=args.include_resources,
                include_shared_resource_risks=args.include_shared_resource_risks,
                include_candidates=args.include_candidates, view_id=args.view_id,
            )
        _canonical_output(result, args.output)
    except CLIContractError as error:
        _fail(str(error), error.code)
    except ValueError as error:
        _fail(str(error), 2)


def cmd_discover(args):
    """Scan for git repos and suggest registrations."""
    root = str(Path(args.dir).resolve())
    print(f"Scanning {root} ...")
    try:
        repos = discover_repos(root)
    except PlatformCapabilityError as error:
        _fail(str(error), 2)
    except ValueError as error:
        _fail(str(error), 2)
    if not repos:
        print("No unregistered git repositories found.")
        return

    print(f"\nFound {len(repos)} unregistered repo(s):\n")
    for r in repos:
        print(f"  [{r['role']:10s}] {r['name']:30s} ({r['language']})")
        print(f"         Path: {r['path']}")
        print(f"         {r['suggestion']}")
        print(f"         To register: codeevolution register -n {r['name']} -r {r['path']}")
        print()


def cmd_check(args):
    """Health check all registered services."""
    results = check_services()
    if not results:
        print("No repos registered.")
        return

    ok = sum(1 for r in results if r["status"] == "ok")
    warn = sum(1 for r in results if r["status"] == "warning")
    err = sum(1 for r in results if r["status"] == "error")

    print(f"\nService Health: {ok} OK, {warn} warning, {err} error\n")
    for r in results:
        icon = {"ok": "[OK]", "warning": "[!!]", "error": "[XX]", "info": "[i ]"}[r["status"]]
        print(
            f"  {icon} {r['name']:25s} | lang={r['language']:10s} role={r['role']:10s} "
            f"symbols={r['cg_symbols']:5d} edges={r['cg_edges']:5d}"
        )
        if r["db_types"]:
            print(f"       DB: {', '.join(r['db_types'])}")
        if r["mq_types"]:
            print(f"       MQ: {', '.join(r['mq_types'])}")
        for issue in r["issues"]:
            print(f"       → {issue}")
    print()


def cmd_flow(args):
    """Synchronously query rooted StaticPossibleFlow from existing topology."""
    try:
        channels = _parse_channels(args.channels)
        if bool(args.method) != bool(args.path):
            raise CLIContractError("--method and --path must be supplied together")
        if not args.entry_id and not (args.method and args.path):
            raise CLIContractError("provide --entry-id or --method with --path")
        if args.max_depth < 0 or args.max_depth > 20:
            raise CLIContractError("--max-depth must be between 0 and 20")
        if args.max_nodes < 1 or args.max_nodes > 5000 or args.max_edges < 1 or args.max_edges > 10000:
            raise CLIContractError("--max-nodes must be 1..5000 and --max-edges must be 1..10000")
        if args.server:
            member_id = _resolve_member_id(_topology_for_query(args), args.service)
            params = urlencode({
                "member_id": member_id, "entry_id": args.entry_id, "method": args.method,
                "path": args.path, "max_depth": args.max_depth, "max_nodes": args.max_nodes,
                "max_edges": args.max_edges, "channels": ",".join(channels),
                "include_resources": str(args.include_resources).lower(),
                "include_candidates": str(args.include_candidates).lower(),
            })
            _status, result, _headers = _request_json(args.server, "GET", f"/api/graph-views/{args.view_id}/flow?{params}")
        else:
            artifact = _topology_for_query(args)
            member_id = _resolve_member_id(artifact, args.service)
            from .application.topology_query_service import TopologyQueryService

            result = TopologyQueryService().flow(
                artifact, member_id, entry_id=args.entry_id or None, method=args.method or None,
                path=args.path or None, max_depth=args.max_depth, max_nodes=args.max_nodes,
                max_edges=args.max_edges, channels=channels, include_resources=args.include_resources,
                include_candidates=args.include_candidates, view_id=args.view_id,
            )
        _canonical_output(result, args.output)
    except CLIContractError as error:
        _fail(str(error), error.code)
    except ValueError as error:
        _fail(str(error), 2)


if __name__ == "__main__":
    main()
