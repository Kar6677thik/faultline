"""Small shared ownership contracts; a name alone never authorizes mutation."""

import re
from uuid import UUID

ROLES = {
    "container": {"application", "database", "observer", "worker", "proxy"},
    "network": {"client", "storage", "dependency", "control"},
}


def canonical_uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Expected a canonical run/ownership UUID")
    return value


def validate_resources(resources):
    if not isinstance(resources, list) or len(resources) > 9:
        raise ValueError("Invalid milestone1 resource inventory")
    ids, roles = set(), set()
    for record in resources:
        if not isinstance(record, dict) or set(record) != {"kind", "id", "role"}:
            raise ValueError("Malformed resource ownership record")
        kind, resource_id, role = record["kind"], record["id"], record["role"]
        if not all(isinstance(value, str) for value in (kind, resource_id, role)):
            raise ValueError("Resource ownership fields must be strings")
        if kind not in ROLES or role not in ROLES[kind]:
            raise ValueError("Unsupported resource kind or role")
        if not re.fullmatch(r"[0-9a-f]{64}", resource_id):
            raise ValueError("Recovery requires full immutable Docker resource IDs")
        if resource_id in ids or (kind, role) in roles:
            raise ValueError("Duplicate resource identity or role")
        ids.add(resource_id)
        roles.add((kind, role))


def validate_intents(intents, run_id):
    if not isinstance(intents, list) or len(intents) > 9:
        raise ValueError("Invalid creation intent inventory")
    roles, tokens = set(), set()
    for intent in intents:
        if not isinstance(intent, dict) or set(intent) != {"kind", "role", "name", "token"}:
            raise ValueError("Malformed creation intent")
        if not all(isinstance(value, str) for value in intent.values()):
            raise ValueError("Creation intent fields must be strings")
        kind, role = intent["kind"], intent["role"]
        if kind not in ROLES or role not in ROLES[kind]:
            raise ValueError("Unsupported creation kind or role")
        canonical_uuid(intent["token"])
        if intent["name"] != f"fl-{run_id}-{role}":
            raise ValueError("Creation name does not match the recorded run and role")
        if (kind, role) in roles or intent["token"] in tokens:
            raise ValueError("Duplicate creation role or token")
        roles.add((kind, role))
        tokens.add(intent["token"])
