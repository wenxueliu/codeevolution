"""CLI entry point for CodeHistory."""

import argparse
import logging
import sys

import json

from .codegraph_reader import CodeGraphReader
from .config import Config
from .engine import EvolutionEngine
from .knowledge import KnowledgeExtractor
from .mcp_server import run_server
from .store import EvolutionStore


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_backfill(args):
    """Run a full backfill analysis from git history."""
    config = Config(repo_path=args.repo, db_path=args.db)
    engine = EvolutionEngine(config)
    logger = logging.getLogger("codehistory")

    def progress(current, total, msg):
        if current % 100 == 0 or current == total:
            logger.info(msg)

    logger.info(f"Starting backfill for {args.repo}")
    engine.backfill(progress_callback=progress)

    stats = engine.store.get_stats()
    print(f"\nBackfill complete. Stats:")
    print(f"  Commits processed: {stats['total_commits']}")
    print(f"  Features discovered: {stats['total_features']}")
    print(f"  Events generated: {stats['total_events']}")
    print(f"  Active features: {stats['active_features']}")
    engine.store.close()


def cmd_update(args):
    """Process new commits since last analysis."""
    config = Config(repo_path=args.repo, db_path=args.db)
    engine = EvolutionEngine(config)
    engine.update()
    stats = engine.store.get_stats()
    print(f"Update complete. Stats: {stats}")
    engine.store.close()


def cmd_serve(args):
    """Start the MCP server."""
    config = Config(repo_path=args.repo, db_path=args.db)
    store = EvolutionStore(config.db_path)
    stats = store.get_stats()
    print(f"Serving MCP on stdio. DB stats: {stats}")
    try:
        run_server(store, config, transport=args.transport)
    finally:
        store.close()


def cmd_web(args):
    """Start the web dashboard (multi-repo)."""
    from .api import serve
    print(f"Starting CodeHistory web server at http://{args.host}:{args.port}")
    serve(host=args.host, port=args.port)


def cmd_register(args):
    """Register a repo in the multi-repo registry."""
    from .registry import register_repo
    try:
        entry = register_repo(args.name, args.repo)
        print(f"Registered: {entry['name']} -> {entry['path']}")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_repos(args):
    """List registered repos."""
    from .registry import list_repos
    repos = list_repos()
    if not repos:
        print("No repos registered.")
        print("Use 'codehistory register --name <name> --repo <path>' to register one.")
        return
    for r in repos:
        print(f"  {r['name']}: {r['path']}")


def cmd_status(args):
    """Show current analysis status."""
    config = Config(
        repo_path=args.repo,
        db_path=args.db,
    )
    store = EvolutionStore(config.db_path)
    stats = store.get_stats()
    print(f"Repository: {config.repo_path}")
    print(f"DB: {config.db_path}")
    print(f"  Total commits:    {stats['total_commits']}")
    print(f"  Total features:   {stats['total_features']}")
    print(f"  Total snapshots:  {stats['total_snapshots']}")
    print(f"  Total events:     {stats['total_events']}")
    print(f"  Active features:  {stats['active_features']}")

    # Show features
    features = store.get_all_features()
    if features:
        print(f"\nFeatures:")
        for f in features[:20]:
            print(f"  [{f['entry_type']}] {f['canonical_name']} ({f['status']})")
        if len(features) > 20:
            print(f"  ... and {len(features) - 20} more")

    store.close()


def cmd_knowledge(args):
    """Extract business knowledge from code via CodeGraph."""
    config = Config(repo_path=args.repo)

    cg_db = config.codegraph_db_path
    from pathlib import Path as P
    if not P(cg_db).exists():
        print(f"Error: CodeGraph database not found at {cg_db}")
        print(f"Run: cd {args.repo} && codegraph init")
        sys.exit(1)

    reader = CodeGraphReader(cg_db)
    extractor = KnowledgeExtractor(reader)

    try:
        if args.section == "api" or args.section == "all":
            api = extractor.extract_api_contract()
            print(f"\n{'='*60}")
            print(f"API Contract: {len(api.endpoints)} endpoints")
            print(f"{'='*60}")
            for ep in api.endpoints:
                print(f"  {ep.method:6s} {ep.path:40s} → {ep.handler_name}")

        if args.section == "modules" or args.section == "all":
            mod = extractor.extract_module_topology()
            print(f"\n{'='*60}")
            print(f"Module Topology: {len(mod.modules)} modules, coupling={mod.coupling_score}")
            print(f"{'='*60}")
            for m in mod.modules:
                deps = mod.dependency_graph.get(m["id"], [])
                print(f"  {m['id']}: \"{m['name']}\" ({m['file_count']} files, {m['primary_language']}) → {deps}")

        if args.section == "entities" or args.section == "all":
            ents = extractor.extract_core_entities(20)
            print(f"\n{'='*60}")
            print(f"Core Entities (Top 20 by PageRank)")
            print(f"{'='*60}")
            for e in ents:
                print(f"  {e.pagerank:.4f}  {e.name:35s}  kind={e.kind:8s}  in={e.in_degree:3d}  out={e.out_degree:3d}")

        if args.section == "tests" or args.section == "all":
            cov = extractor.extract_test_coverage_stats()
            print(f"\n{'='*60}")
            print(f"Test Coverage")
            print(f"{'='*60}")
            print(f"  Test functions:       {cov['test_functions']}")
            print(f"  Production functions: {cov['production_functions']}")
            print(f"  Covered:              {cov['covered_functions']} ({cov['coverage_pct']}%)")
            print(f"  Uncovered (gaps):     {cov['gap_count']}")

            if args.section == "tests":
                gaps = extractor.extract_test_gaps()
                for g in gaps[:20]:
                    print(f"  [!] {g.qualified_name} ({g.file_path}:{g.line})")

        if args.section == "layers" or args.section == "all":
            viols = extractor.extract_layer_violations()
            print(f"\n{'='*60}")
            print(f"Layer Violations: {len(viols)}")
            print(f"{'='*60}")
            for v in viols:
                print(f"  [{v.source_layer}] {v.source_file} → [{v.target_layer}] {v.target_file}")
                print(f"    {v.source_name} → {v.target_name}")

        if args.section == "config" or args.section == "all":
            configs = extractor.extract_config_consumption()
            print(f"\n{'='*60}")
            print(f"Config Consumption: {len(configs)} config files with consumers")
            print(f"{'='*60}")
            for cf in configs:
                print(f"\n  [{cf['config_file']}] ({cf['key_count']} keys, {cf['consumed_keys']} consumed)")
                for c in cf["consumers"][:10]:
                    print(f"    key={c['config_key']:30s} → {c['consumer_name']}")

        if args.section == "deps" or args.section == "all":
            deps = extractor.extract_external_dependencies()
            print(f"\n{'='*60}")
            print(f"External Dependencies")
            print(f"{'='*60}")
            for cat in deps:
                print(f"\n  [{cat['category']}] ({cat['dependency_count']} dependencies)")
                for d in cat["dependencies"]:
                    print(f"    {d['label']:30s} → {d['file_count']} files")

        if args.section == "auth" or args.section == "all":
            auth = extractor.extract_authorization_model()
            print(f"\n{'='*60}")
            print(f"Authorization Model: {len(auth)} protected endpoints/middleware")
            print(f"{'='*60}")
            for a in auth[:20]:
                extras = ""
                if a["roles"]:
                    extras += f" roles={a['roles']}"
                if a["permissions"]:
                    extras += f" perms={a['permissions']}"
                print(f"  [{a['auth_level']:12s}] {a['function']}{extras}")

        if args.section == "heatmap" or args.section == "all":
            heat = extractor.extract_heat_map()
            counts = {"hot": 0, "warm": 0, "cold": 0}
            for h in heat:
                counts[h["heat"]] += 1
            print(f"\n{'='*60}")
            print(f"Heat Map: {len(heat)} functions (hot={counts['hot']} warm={counts['warm']} cold={counts['cold']})")
            print(f"{'='*60}")
            print(f"\n  {'HOT (top 10%)':-^50}")
            for h in heat[:15]:
                if h["heat"] != "hot":
                    break
                print(f"  [{h['heat']:4s}] {h['name']:35s} callers={h['callers']} callees={h['callees']} [{h.get('layer', '')}]")
            print(f"\n  {'COLD (bottom 40%)':-^50}")
            colds = [h for h in heat if h["heat"] == "cold"]
            for h in colds[:10]:
                print(f"  [{h['heat']:4s}] {h['name']:35s} callers={h['callers']} callees={h['callees']}")

        if args.output:
            result = extractor.extract_all()
            with open(args.output, "w") as f:
                json.dump(result, f, indent=2, default=str)
            print(f"\nFull report written to {args.output}")

    finally:
        reader.close()


def main():
    parser = argparse.ArgumentParser(
        prog="codehistory",
        description="CodeHistory — codebase feature evolution analysis",
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # backfill
    p = subparsers.add_parser("backfill", help="Full initial analysis from git history")
    p.add_argument("--repo", "-r", required=True, help="Path to git repository")
    p.add_argument("--db", "-d", default="", help="Path to database (default: .codehistory/evolution.db)")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    # update
    p = subparsers.add_parser("update", help="Process new commits since last analysis")
    p.add_argument("--repo", "-r", required=True, help="Path to git repository")
    p.add_argument("--db", "-d", default="", help="Path to database")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    # serve
    p = subparsers.add_parser("serve", help="Start MCP server")
    p.add_argument("--repo", "-r", required=True, help="Path to git repository")
    p.add_argument("--db", "-d", default="", help="Path to database")
    p.add_argument("--transport", "-t", default="stdio",
                   choices=["stdio", "sse", "streamable-http"],
                   help="MCP transport (default: stdio)")

    # web
    p = subparsers.add_parser("web", help="Start web dashboard (multi-repo)")
    p.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    p.add_argument("--port", type=int, default=8765, help="Port to bind (default: 8765)")

    # register
    p = subparsers.add_parser("register", help="Register a repo for multi-repo dashboard")
    p.add_argument("--name", "-n", required=True, help="Short name for the repo")
    p.add_argument("--repo", "-r", required=True, help="Path to git repository")

    # repos
    p = subparsers.add_parser("repos", help="List registered repos")

    # status
    p = subparsers.add_parser("status", help="Show current analysis status")
    p.add_argument("--repo", "-r", required=True, help="Path to git repository")
    p.add_argument("--db", "-d", default="", help="Path to database")

    # knowledge
    p = subparsers.add_parser("knowledge", help="Extract business knowledge from code (Phase 1)")
    p.add_argument("--repo", "-r", required=True, help="Path to git repository with CodeGraph initialized")
    p.add_argument("--output", "-o", default="", help="Output JSON file path (default: stdout)")
    p.add_argument("--section", "-s", default="all",
                   choices=["all", "api", "modules", "entities", "tests", "layers",
                            "config", "deps", "auth", "heatmap"],
                   help="Which knowledge section to extract (default: all)")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    setup_logging(getattr(args, "verbose", False))

    if args.command == "backfill":
        cmd_backfill(args)
    elif args.command == "update":
        cmd_update(args)
    elif args.command == "serve":
        cmd_serve(args)
    elif args.command == "web":
        cmd_web(args)
    elif args.command == "register":
        cmd_register(args)
    elif args.command == "repos":
        cmd_repos(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "knowledge":
        cmd_knowledge(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
