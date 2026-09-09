"""Cross-member projections over immutable Graph View snapshots."""

from __future__ import annotations


class SnapshotTopologyService:
    def __init__(self, store, queries):
        self.store, self.queries = store, queries

    def _view(self, view_id):
        view = self.store.get_view(view_id)
        if view is None:
            raise KeyError(view_id)
        return view

    def topology(self, view_id: str) -> dict:
        view = self._view(view_id)
        services, edges = [], []
        facts_by_member = {}
        for member in view.members:
            if not member.snapshot_id:
                services.append({"member_id": member.member_id, "availability": member.availability.value})
                continue
            facts = self.queries.knowledge(member.snapshot_id)
            facts_by_member[member.member_id] = facts
            services.append({"member_id": member.member_id, "snapshot_id": member.snapshot_id,
                             "endpoints": facts.get("api_contract", {}).get("endpoints", [])})
            for dependency in facts.get("external_dependencies", {}).get("by_category", []):
                edges.append({"source_member_id": member.member_id,
                              "source_snapshot_id": member.snapshot_id,
                              "kind": "dependency", "dependency": dependency})
        # Resolve endpoint call-chain evidence against the immutable endpoint
        # inventories in this View. No registered path or live graph is used.
        endpoint_index = []
        for member in view.members:
            facts = facts_by_member.get(member.member_id)
            if not facts or not member.snapshot_id:
                continue
            for endpoint in facts.get("api_contract", {}).get("endpoints", []):
                endpoint_index.append((member, endpoint))
        for source in view.members:
            facts = facts_by_member.get(source.member_id)
            if not facts or not source.snapshot_id:
                continue
            for endpoint in facts.get("api_contract", {}).get("endpoints", []):
                for call in endpoint.get("call_chain", []) or []:
                    target_path = str(call.get("path") or call.get("url") or call.get("endpoint") or "")
                    if not target_path:
                        continue
                    path_only = target_path.split("?", 1)[0].split("#", 1)[0]
                    matches = [(m, e) for m, e in endpoint_index
                               if m.member_id != source.member_id and str(e.get("path", "")) == path_only]
                    if len(matches) == 1:
                        target, target_endpoint = matches[0]
                        edges.append({"source_member_id": source.member_id,
                                      "source_snapshot_id": source.snapshot_id,
                                      "target_member_id": target.member_id,
                                      "target_snapshot_id": target.snapshot_id,
                                      "source_endpoint": endpoint.get("path"),
                                      "target_endpoint": target_endpoint.get("path"),
                                      "kind": "http", "confidence": "exact",
                                      "evidence": call})
                    elif matches:
                        edges.append({"source_member_id": source.member_id,
                                      "source_snapshot_id": source.snapshot_id,
                                      "kind": "http", "confidence": "ambiguous",
                                      "target_candidates": [m.member_id for m, _ in matches],
                                      "source_endpoint": endpoint.get("path"), "target_path": path_only,
                                      "evidence": call})
                    else:
                        edges.append({"source_member_id": source.member_id,
                                      "source_snapshot_id": source.snapshot_id,
                                      "kind": "http", "confidence": "external",
                                      "source_endpoint": endpoint.get("path"), "target_path": path_only,
                                      "evidence": call})
        return {"view_id": view.id, "view_digest": view.digest, "services": services, "edges": edges,
                "completeness": view.completeness}

    def entities(self, view_id: str) -> dict:
        view = self._view(view_id)
        members = [{"member_id": item.member_id, "snapshot_id": item.snapshot_id,
                    "entities": self.queries.knowledge(item.snapshot_id).get("core_entities", [])}
                   for item in view.members if item.snapshot_id]
        return {"view_id": view.id, "view_digest": view.digest, "members": members}

    def impact(self, view_id: str, service: str) -> dict:
        topology = self.topology(view_id)
        affected_ids = {service}
        frontier = [service]
        while frontier:
            current = frontier.pop()
            for edge in topology["edges"]:
                target = edge.get("target_member_id")
                if edge.get("source_member_id") == current and target and target not in affected_ids:
                    affected_ids.add(target)
                    frontier.append(target)
        return {"view_id": view_id, "service": service,
                "affected": [item for item in topology["services"] if item["member_id"] in affected_ids],
                "edges": [edge for edge in topology["edges"]
                          if edge.get("source_member_id") in affected_ids]}

    def flow(self, view_id: str, service: str, path: str = "") -> dict:
        topology = self.topology(view_id)
        steps = [item for item in topology["services"] if item["member_id"] == service]
        for edge in topology["edges"]:
            if edge.get("source_member_id") == service and edge.get("target_member_id"):
                steps.extend(item for item in topology["services"]
                             if item["member_id"] == edge["target_member_id"])
        return {"view_id": view_id, "service": service, "path": path,
                "steps": steps}
