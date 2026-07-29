"""CLI entry point for CodeHistory."""

import argparse
import logging
import sys

from .config import Config
from .engine import EvolutionEngine
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
    else:
        parser.print_help()
        sys.exit(1)
