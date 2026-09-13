"""CLI entry point for CodeEvolution."""

import argparse
import json
import logging
import os
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
        else:
            runtime, store, service = _terms_local_context()
            try:
                listing = service.list(**query)
                if args.terms_action == "explain":
                    if not listing["terms"]:
                        raise CLIContractError("term not found", 3)
                    term = listing["terms"][0]
                    result = {"term": term, "evidence": service.evidence(args.snapshot_id, term["id"])}
                else:
                    result = listing
            finally:
                store.close()
                runtime.close()
        _canonical_output(result, args.output)
    except CLIContractError:
        raise
    except (KeyError, RuntimeError, ValueError) as error:
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
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(rendered)
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

    # evidence-backed terminology
    p = subparsers.add_parser("terms", help="Extract and review terminology from a snapshot")
    term_parsers = p.add_subparsers(dest="terms_action", required=True)

    extract = term_parsers.add_parser("extract", help="Extract ranked terms")
    extract.add_argument("--snapshot-id", required=True)
    extract.add_argument("--types", default="", help="Comma-separated term types")
    extract.add_argument("--server", default="", help="Optional CodeEvolution HTTP server URL")
    extract.add_argument("--output", "-o", default="")
    extract.set_defaults(handler=cmd_terms)

    for action, help_text in (("list", "List persisted terms"), ("export", "Export persisted terms"), ("explain", "Show a term and its evidence")):
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
