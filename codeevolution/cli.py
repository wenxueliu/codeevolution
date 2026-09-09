"""CLI entry point for CodeEvolution."""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .application.snapshot_runtime import SnapshotRuntime
from .application.snapshot_topology_service import SnapshotTopologyService
from .config import Config
from .mcp_server import run_server
from .paths import analysis_data_dir
from .registry import (
    check_services,
    discover_repos,
    list_repos,
    refresh_meta,
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


def cmd_backfill(args):
    """Run a full backfill analysis from git history."""
    raise SystemExit("backfill was removed; use `codeevolution analyze --member-id ...`")
    config = Config(repo_path=args.repo, db_path=args.db)
    service = EvolutionCommandService.from_config(config)
    logger = logging.getLogger("codeevolution")

    def progress(current, total, msg):
        if current % 100 == 0 or current == total:
            logger.info(msg)

    logger.info(f"Starting backfill for {args.repo}")
    try:
        stats = service.backfill(progress_callback=progress)
        print("\nBackfill complete. Stats:")
        print(f"  Commits processed: {stats['total_commits']}")
        print(f"  Features discovered: {stats['total_features']}")
        print(f"  Events generated: {stats['total_events']}")
        print(f"  Active features: {stats['active_features']}")
    finally:
        service.close()


def cmd_update(args):
    """Process new commits since last analysis."""
    raise SystemExit("update was removed; use `codeevolution analyze --member-id ...`")
    config = Config(repo_path=args.repo, db_path=args.db)
    service = EvolutionCommandService.from_config(config)
    try:
        stats = service.update()
        print(f"Update complete. Stats: {stats}")
    finally:
        service.close()


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


def cmd_status(args):
    """Show current analysis status."""
    raise SystemExit("status was removed; use `codeevolution runs` or `snapshots`")
    config = Config(
        repo_path=args.repo,
        db_path=args.db,
    )
    service = EvolutionQueryService.from_database(config.db_path)
    try:
        stats = service.stats()
        print(f"Repository: {config.repo_path}")
        print(f"DB: {config.db_path}")
        print(f"  Total commits:    {stats['total_commits']}")
        print(f"  Total features:   {stats['total_features']}")
        print(f"  Total snapshots:  {stats['total_snapshots']}")
        print(f"  Total events:     {stats['total_events']}")
        print(f"  Active features:  {stats['active_features']}")

        result = service.list_features(limit=20)
        if result["features"]:
            print("\nFeatures:")
            for feature in result["features"]:
                print(
                    f"  [{feature['entry_type']}] {feature['canonical_name']} ({feature['status']})"
                )
            if result["total"] > 20:
                print(f"  ... and {result['total'] - 20} more")
    finally:
        service.close()


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


def _snapshot_topology_service():
    runtime = SnapshotRuntime(analysis_data_dir())
    return runtime, SnapshotTopologyService(runtime.store, runtime.snapshot_queries)


def cmd_snapshot_topology(args):
    runtime, service = _snapshot_topology_service()
    try:
        if args.command == "topology": result = service.topology(args.view_id)
        elif args.command == "impact": result = service.impact(args.view_id, args.service)
        elif args.command in {"flow", "trace"}: result = service.flow(args.view_id, args.service, getattr(args, "path", ""))
        else: result = service.entities(args.view_id)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        runtime.close()


def _cmd_legacy_knowledge(args):
    """Extract business knowledge from code via CodeGraph."""
    config = Config(repo_path=args.repo)

    cg_db = config.codegraph_db_path
    from pathlib import Path as P

    if not P(cg_db).exists():
        print(f"Error: CodeGraph database not found at {cg_db}")
        print(f"Run: cd {args.repo} && codegraph init")
        sys.exit(1)

    service = KnowledgeService.from_codegraph(cg_db)

    try:
        if args.section == "api" or args.section == "all":
            api = service.extract_api_contract()
            print(f"\n{'=' * 60}")
            print(f"API Contract: {len(api.endpoints)} endpoints")
            print(f"{'=' * 60}")
            for ep in api.endpoints:
                print(f"  {ep.method:6s} {ep.path:40s} → {ep.handler_name}")

        if args.section == "modules" or args.section == "all":
            mod = service.extract_module_topology()
            print(f"\n{'=' * 60}")
            print(f"Module Topology: {len(mod.modules)} modules, coupling={mod.coupling_score}")
            print(f"{'=' * 60}")
            for m in mod.modules:
                deps = mod.dependency_graph.get(m["id"], [])
                print(
                    f'  {m["id"]}: "{m["name"]}" ({m["file_count"]} files, {m["primary_language"]}) → {deps}'
                )

        if args.section == "entities" or args.section == "all":
            ents = service.extract_core_entities(20)
            print(f"\n{'=' * 60}")
            print("Core Entities (Top 20 by PageRank)")
            print(f"{'=' * 60}")
            for e in ents:
                print(
                    f"  {e.pagerank:.4f}  {e.name:35s}  kind={e.kind:8s}  in={e.in_degree:3d}  out={e.out_degree:3d}"
                )

        if args.section == "tests" or args.section == "all":
            cov = service.extract_test_coverage_stats()
            print(f"\n{'=' * 60}")
            print("Test Coverage")
            print(f"{'=' * 60}")
            print(f"  Test functions:       {cov['test_functions']}")
            print(f"  Production functions: {cov['production_functions']}")
            print(f"  Covered:              {cov['covered_functions']} ({cov['coverage_pct']}%)")
            print(f"  Uncovered (gaps):     {cov['gap_count']}")

            if args.section == "tests":
                gaps = service.extract_test_gaps()
                for g in gaps[:20]:
                    print(f"  [!] {g.qualified_name} ({g.file_path}:{g.line})")

        if args.section == "layers" or args.section == "all":
            viols = service.extract_layer_violations()
            print(f"\n{'=' * 60}")
            print(f"Layer Violations: {len(viols)}")
            print(f"{'=' * 60}")
            for v in viols:
                print(f"  [{v.source_layer}] {v.source_file} → [{v.target_layer}] {v.target_file}")
                print(f"    {v.source_name} → {v.target_name}")

        if args.section == "config" or args.section == "all":
            configs = service.extract_config_consumption()
            print(f"\n{'=' * 60}")
            print(f"Config Consumption: {len(configs)} config files with consumers")
            print(f"{'=' * 60}")
            for cf in configs:
                print(
                    f"\n  [{cf['config_file']}] ({cf['key_count']} keys, {cf['consumed_keys']} consumed)"
                )
                for c in cf["consumers"][:10]:
                    print(f"    key={c['config_key']:30s} → {c['consumer_name']}")

        if args.section == "deps" or args.section == "all":
            deps = service.extract_external_dependencies()
            print(f"\n{'=' * 60}")
            print("External Dependencies")
            print(f"{'=' * 60}")
            for cat in deps:
                print(f"\n  [{cat['category']}] ({cat['dependency_count']} dependencies)")
                for d in cat["dependencies"]:
                    print(f"    {d['label']:30s} → {d['file_count']} files")

        if args.section == "auth" or args.section == "all":
            auth = service.extract_authorization_model()
            print(f"\n{'=' * 60}")
            print(f"Authorization Model: {len(auth)} protected endpoints/middleware")
            print(f"{'=' * 60}")
            for a in auth[:20]:
                extras = ""
                if a["roles"]:
                    extras += f" roles={a['roles']}"
                if a["permissions"]:
                    extras += f" perms={a['permissions']}"
                print(f"  [{a['auth_level']:12s}] {a['function']}{extras}")

        if args.section == "heatmap" or args.section == "all":
            heat = service.extract_heat_map()
            counts = {"hot": 0, "warm": 0, "cold": 0}
            for h in heat:
                counts[h["heat"]] += 1
            print(f"\n{'=' * 60}")
            print(
                f"Heat Map: {len(heat)} functions (hot={counts['hot']} warm={counts['warm']} cold={counts['cold']})"
            )
            print(f"{'=' * 60}")
            print(f"\n  {'HOT (top 10%)':-^50}")
            for h in heat[:15]:
                if h["heat"] != "hot":
                    break
                print(
                    f"  [{h['heat']:4s}] {h['name']:35s} callers={h['callers']} callees={h['callees']} [{h.get('layer', '')}]"
                )
            print(f"\n  {'COLD (bottom 40%)':-^50}")
            colds = [h for h in heat if h["heat"] == "cold"]
            for h in colds[:10]:
                print(
                    f"  [{h['heat']:4s}] {h['name']:35s} callers={h['callers']} callees={h['callees']}"
                )

        if args.section == "business" or (args.section == "all" and args.llm):
            print(f"\n{'=' * 60}")
            print("Business Descriptions (LLM)")
            print(f"{'=' * 60}")
            descs = service.extract_business_descriptions(limit=20)
            for d in descs:
                if "error" in d:
                    print(f"  Error: {d['error']}")
                    break
                print(f"\n  [{d.get('role', '?')}] {d['function_name']}")
                print(f"    EN: {d.get('summary_en', '')}")
                print(f"    ZH: {d.get('summary_zh', '')}")
                if d.get("key_responsibilities"):
                    for r in d["key_responsibilities"]:
                        print(f"    - {r}")

        if args.section == "rules" or (args.section == "all" and args.llm):
            print(f"\n{'=' * 60}")
            print("Business Rules (LLM)")
            print(f"{'=' * 60}")
            rules = service.extract_business_rules_llm(limit=15)
            for r in rules:
                if "error" in r:
                    print(f"  Error: {r['error']}")
                    break
                print(f"  [{r['rule_type']}] {r['function']}")
                print(f"    EN: {r.get('description_en', '')}")
                print(f"    Condition: {r.get('condition', '')}")
                print(f"    On failure: {r.get('failure_mode', '')}")

        if args.section == "errors" or (args.section == "all" and args.llm):
            print(f"\n{'=' * 60}")
            print("Error Catalog (LLM)")
            print(f"{'=' * 60}")
            errors = service.extract_error_catalog(limit=20)
            for e in errors:
                if "error" in e:
                    print(f"  Error: {e['error']}")
                    break
                print(f"  [{e['error_type']}] in {e['function']}")
                print(f"    Trigger: {e.get('trigger_condition', '')}")
                print(
                    f"    Handling: {e.get('handling', '')} | User-facing: {e.get('user_facing', False)}"
                )

        if args.section == "states" or (args.section == "all" and args.llm):
            print(f"\n{'=' * 60}")
            print("State Machines (LLM)")
            print(f"{'=' * 60}")
            machines = service.extract_state_machines()
            for sm in machines:
                if "error" in sm:
                    print(f"  Error: {sm['error']}")
                    break
                print(f"\n  Entity: {sm['entity']}")
                print(f"  States: [{', '.join(sm['states'])}]")
                print(f"  Initial: {sm['initial_state']} → Terminal: {sm['terminal_states']}")
                print("  Transitions:")
                for t in sm.get("transitions", []):
                    print(f"    {t.get('from', '?')} → {t.get('to', '?')}: {t.get('trigger', '?')}")

        if args.output:
            result = service.report(include_llm=args.llm)
            with open(args.output, "w") as f:
                json.dump(result, f, indent=2, default=str)
            print(f"\nFull report written to {args.output}")

    finally:
        service.close()


def main():
    parser = argparse.ArgumentParser(
        prog="codeevolution",
        description="CodeEvolution — codebase feature evolution analysis",
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # backfill
    p = subparsers.add_parser("backfill", help="Full initial analysis from git history")
    p.add_argument("--repo", "-r", required=True, help="Path to git repository")
    p.add_argument(
        "--db", "-d", default="", help="Path to database (default: .codeevolution/evolution.db)"
    )
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    p.set_defaults(handler=cmd_backfill)

    # update
    p = subparsers.add_parser("update", help="Process new commits since last analysis")
    p.add_argument("--repo", "-r", required=True, help="Path to git repository")
    p.add_argument("--db", "-d", default="", help="Path to database")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    p.set_defaults(handler=cmd_update)

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

    # status
    p = subparsers.add_parser("status", help="Show current analysis status")
    p.add_argument("--repo", "-r", required=True, help="Path to git repository")
    p.add_argument("--db", "-d", default="", help="Path to database")
    p.set_defaults(handler=cmd_status)

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
            "modules",
            "entities",
            "tests",
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

    # cross-repo topology
    p = subparsers.add_parser(
        "topology", help="Build unified multi-service topology from registered repos"
    )
    p.add_argument(
        "--service",
        "-s",
        default="",
        help="Single service name to analyze (default: all registered)",
    )
    p.add_argument(
        "--no-cache", action="store_true", help="Force rebuild topology (don't use cache)"
    )
    p.add_argument("--view-id", default="", help="Immutable Graph View selector")
    p.set_defaults(handler=cmd_topology)

    # cross-repo impact
    p = subparsers.add_parser("impact", help="Cross-service change impact analysis")
    p.add_argument("--service", "-s", required=True, help="Service name to analyze impact for")
    p.add_argument(
        "--no-cache", action="store_true", help="Force rebuild topology (don't use cache)"
    )
    p.add_argument("--view-id", default="", help="Immutable Graph View selector")
    p.set_defaults(handler=cmd_impact)

    # cross-repo trace
    p = subparsers.add_parser("trace", help="Trace end-to-end flow across services")
    p.add_argument("--service", "-s", required=True, help="Starting service name")
    p.add_argument("--path", "-p", default="", help="Starting API path (optional)")
    p.add_argument(
        "--no-cache", action="store_true", help="Force rebuild topology (don't use cache)"
    )
    p.add_argument("--view-id", default="", help="Immutable Graph View selector")
    p.set_defaults(handler=cmd_trace)

    # discover
    p = subparsers.add_parser(
        "discover", help="Scan directory for git repos and suggest registrations"
    )
    p.add_argument("--dir", "-d", default=".", help="Root directory to scan (default: current)")
    p.set_defaults(handler=cmd_discover)

    # check
    p = subparsers.add_parser("check", help="Health check all registered services")
    p.set_defaults(handler=cmd_check)

    # init-all: batch CodeGraph init
    p = subparsers.add_parser("init-all", help="Initialize CodeGraph on all registered services")
    p.set_defaults(handler=cmd_init_all)

    # P2: enhanced flow trace
    p = subparsers.add_parser(
        "flow", help="End-to-end flow trace across all channels (HTTP+MQ+gRPC+DB)"
    )
    p.add_argument("--service", "-s", required=True, help="Starting service name")
    p.add_argument("--path", "-p", default="", help="Starting API path (optional)")
    p.add_argument("--no-cache", action="store_true", help="Force rebuild topology")
    p.add_argument("--view-id", default="", help="Immutable Graph View selector")
    p.set_defaults(handler=cmd_flow)

    # P2: entity alignment
    p = subparsers.add_parser(
        "entities", help="Cross-service entity alignment (same concept, different names)"
    )
    p.add_argument(
        "--llm", action="store_true", help="Use LLM to verify and explain entity mappings"
    )
    p.add_argument("--view-id", default="", help="Immutable Graph View selector")
    p.set_defaults(handler=cmd_entities)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    setup_logging(getattr(args, "verbose", False))

    args.handler(args)


def cmd_topology(args):
    """Build and display unified multi-service topology."""
    if args.view_id:
        return cmd_snapshot_topology(args)
    raise SystemExit("topology requires --view-id")


def cmd_impact(args):
    """Cross-service change impact analysis."""
    if args.view_id:
        return cmd_snapshot_topology(args)
    raise SystemExit("impact requires --view-id")


def cmd_trace(args):
    """Trace end-to-end flow across services."""
    if args.view_id:
        return cmd_snapshot_topology(args)
    raise SystemExit("trace requires --view-id")


def cmd_discover(args):
    """Scan for git repos and suggest registrations."""
    root = str(Path(args.dir).resolve())
    print(f"Scanning {root} ...")
    repos = discover_repos(root)
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


def cmd_init_all(args):
    raise SystemExit("init-all was removed; create an analysis run for selected members")
    """Initialize CodeGraph on all registered services."""
    import subprocess as sp

    entries = list_repos()
    if not entries:
        print("No repos registered.")
        sys.exit(1)

    for e in entries:
        name = e["name"]
        for member in repository_members(e):
            member_name, path = member["name"], member["path"]
            cg_db = Path(path) / ".codegraph" / "codegraph.db"
            if cg_db.exists():
                print(f"  [skip] {name}/{member_name}: already initialized")
                continue

            print(f"  [{name}/{member_name}] codegraph init {path} ...")
            try:
                result = sp.run(
                    ["codegraph.cmd" if os.name == "nt" else "codegraph", "init", path],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if result.returncode == 0:
                    print(f"  [ OK ] {name}/{member_name}")
                else:
                    last_line = (
                        result.stderr.strip().split("\n")[-1]
                        if result.stderr
                        else "unknown error"
                    )
                    print(f"  [FAIL] {name}/{member_name}: {last_line[:100]}")
            except sp.TimeoutExpired:
                print(f"  [FAIL] {name}/{member_name}: timed out after 120s")
            except FileNotFoundError:
                print(
                    "  [FAIL] codegraph not found in PATH. "
                    "Install: npm i -g @colbymchenry/codegraph"
                )
                return
        refresh_meta(name)

    print("\nDone. Run 'codeevolution check' to verify status.")


def cmd_flow(args):
    if args.view_id:
        return cmd_snapshot_topology(args)
    raise SystemExit("flow requires --view-id")


def cmd_entities(args):
    if args.view_id:
        return cmd_snapshot_topology(args)
    raise SystemExit("entities requires --view-id")


if __name__ == "__main__":
    main()
