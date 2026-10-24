"""A lost create response must not grant ownership by a guessed name."""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from docker.errors import NotFound

from faultline.artifacts import Bundle
from faultline.environment import Environment
from faultline.locking import execution_lock
from faultline.recovery import read_ownership, recover


@pytest.fixture
def pending(tmp_path, scenario):
    bundle = Bundle(tmp_path, scenario, "correct")
    client = Mock()
    env = Environment(client, bundle, {"runtime": "runtime"})
    intent = env.begin_creation("container", "application")
    resource = Mock(
        id="a" * 64,
        attrs={"Name": "/" + intent["name"], "Config": {"Labels": env.creation_labels(intent)}},
    )
    client.containers.get.return_value = resource
    return bundle, env, intent, resource


def test_intent_is_on_disk_before_create_and_failed_write_prevents_create(tmp_path, scenario):
    bundle = Bundle(tmp_path, scenario, "correct")
    env = Environment(Mock(), bundle, {})

    def create(name, **kwargs):
        saved = read_ownership(tmp_path, bundle.run_id)
        assert not saved["resources"]
        intent = saved["creation_intents"][0]
        assert intent["name"] == name
        assert kwargs["labels"]["io.faultline.creation"] == intent["token"]
        raise RuntimeError("lost response")

    env.client.networks.create.side_effect = create
    with pytest.raises(RuntimeError, match="lost response"):
        env.provision()
    env.client.networks.create.assert_called_once()
    other = Bundle(tmp_path, scenario, "correct")
    other.save_manifest = Mock(side_effect=OSError("disk full"))
    failed = Environment(Mock(), other, {})
    with pytest.raises(OSError):
        failed.provision()
    failed.client.networks.create.assert_not_called()
    assert not other.manifest["creation_intents"]


def test_live_cleanup_records_reconciled_id_before_deletion(tmp_path, pending):
    bundle, env, _intent, resource = pending

    def remove(**kwargs):
        saved = read_ownership(tmp_path, bundle.run_id)
        assert saved["resources"] == [
            {"kind": "container", "role": "application", "id": resource.id}
        ]

    resource.remove.side_effect = remove
    assert env.cleanup()["complete"]
    resource.remove.assert_called_once()


def test_recovery_binds_before_deletion_and_never_rebinds_reused_name(tmp_path, pending):
    bundle, env, intent, resource = pending
    original = (bundle.path / "manifest.json").read_bytes()
    journal = tmp_path / "cleanup/bindings" / f"{bundle.run_id}.json"

    def remove(**kwargs):
        assert json.loads(journal.read_text())["resources"][0]["id"] == resource.id

    resource.remove.side_effect = remove
    with execution_lock(tmp_path):
        results, paths = recover(tmp_path, [read_ownership(tmp_path, bundle.run_id)], env.client)
    assert results[0]["complete"]
    assert results[0]["reconciled_resources"][0]["id"] == resource.id
    assert (bundle.path / "manifest.json").read_bytes() == original
    assert json.loads(paths[0].read_text())["creation_intents"] == [intent]
    env.client.containers.get.reset_mock()
    env.client.containers.get.side_effect = NotFound("original ID absent")
    with execution_lock(tmp_path):
        again, _paths = recover(tmp_path, [read_ownership(tmp_path, bundle.run_id)], env.client)
    assert again[0]["complete"]
    env.client.containers.get.assert_called_once_with(resource.id)
    env.client.containers.list.assert_not_called()


@pytest.mark.parametrize("mismatch", ["name", "token", "role", "kind", "run", "nonce", "project"])
def test_name_or_labels_alone_cannot_authorize_a_binding(tmp_path, pending, mismatch):
    bundle, env, intent, resource = pending
    if mismatch == "name":
        resource.attrs["Name"] = "/unrelated"
    else:
        key = "creation" if mismatch == "token" else mismatch
        resource.attrs["Config"]["Labels"][f"io.faultline.{key}"] = "wrong"
    with execution_lock(tmp_path):
        results, _paths = recover(tmp_path, [bundle.manifest], env.client)
    assert not results[0]["complete"]
    assert results[0]["remaining_resources"] == [intent]
    resource.remove.assert_not_called()
    assert not (tmp_path / "cleanup/bindings" / f"{bundle.run_id}.json").exists()


def test_missing_pending_creation_is_uncertain_but_late_creation_can_be_recovered(
    tmp_path, pending
):
    bundle, env, intent, resource = pending
    env.client.containers.get.side_effect = NotFound("daemon may still create it")
    with execution_lock(tmp_path):
        result, _paths = recover(tmp_path, [bundle.manifest], env.client)
    assert result[0]["remaining_resources"] == [intent]
    assert not result[0]["complete"]
    env.client.containers.get.side_effect = None
    with execution_lock(tmp_path):
        result, _paths = recover(tmp_path, [bundle.manifest], env.client)
    assert result[0]["complete"]
    resource.remove.assert_called_once()


def test_failed_binding_write_never_grants_mutation_rights(monkeypatch, tmp_path, pending):
    from faultline.artifacts import atomic_json

    bundle, env, intent, resource = pending

    def write(path, value):
        if path.parent.name == "bindings":
            raise OSError("disk full")
        atomic_json(path, value)

    monkeypatch.setattr("faultline.recovery.atomic_json", write)
    with execution_lock(tmp_path):
        result, _paths = recover(tmp_path, [bundle.manifest], env.client)
    assert result[0]["remaining_resources"] == [intent]
    assert not bundle.manifest["resources"]
    resource.remove.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"name": "production"},
        {"kind": "volume"},
        {"role": "foreign"},
        {"token": "short"},
        {"extra": True},
        {"role": []},
    ],
)
def test_invalid_intents_refused_during_manifest_validation(tmp_path, pending, change):
    bundle, _env, _intent, _resource = pending
    bundle.manifest["creation_intents"][0].update(change)
    bundle.save_manifest()
    with pytest.raises(ValueError):
        read_ownership(tmp_path, bundle.run_id)


def test_invalid_binding_prevents_entire_batch_before_deletion(tmp_path, scenario, pending):
    bundle, env, _intent, resource = pending
    with execution_lock(tmp_path):
        recover(tmp_path, [bundle.manifest], env.client)
    other = Bundle(tmp_path, scenario, "correct")
    journal = tmp_path / "cleanup/bindings" / f"{bundle.run_id}.json"
    data = json.loads(journal.read_text())
    data["manifest_hash"] = "0" * 64
    journal.write_text(json.dumps(data))
    resource.remove.reset_mock()
    env.client.containers.get.reset_mock()
    with execution_lock(tmp_path), pytest.raises(ValueError, match="does not match"):
        recover(tmp_path, [other.manifest, bundle.manifest], env.client)
    env.client.containers.get.assert_not_called()
    resource.remove.assert_not_called()


def test_duplicate_intents_and_unknown_journal_roles_are_refused(tmp_path, pending):
    bundle, env, intent, _resource = pending
    bundle.manifest["creation_intents"].append(deepcopy(intent))
    bundle.save_manifest()
    with pytest.raises(ValueError, match="Duplicate"):
        read_ownership(tmp_path, bundle.run_id)
    bundle.manifest["creation_intents"].pop()
    bundle.save_manifest()
    with execution_lock(tmp_path):
        recover(tmp_path, [bundle.manifest], env.client)
    journal = tmp_path / "cleanup/bindings" / f"{bundle.run_id}.json"
    data = json.loads(journal.read_text())
    data["resources"][0]["role"] = "worker"
    journal.write_text(json.dumps(data))
    with execution_lock(tmp_path), pytest.raises(ValueError, match="lacks a recorded"):
        recover(tmp_path, [bundle.manifest], env.client)


def test_binding_symlink_is_refused(tmp_path, pending):
    bundle, env, _intent, _resource = pending
    root = tmp_path / "cleanup/bindings"
    root.mkdir(parents=True)
    (root / f"{bundle.run_id}.json").symlink_to(bundle.path / "manifest.json")
    with execution_lock(tmp_path), pytest.raises(OSError):
        recover(tmp_path, [bundle.manifest], env.client)
    env.client.containers.get.assert_not_called()
