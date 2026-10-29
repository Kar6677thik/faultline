from unittest.mock import Mock

from faultline.measure import leaked, working_set


def test_working_set_excludes_reclaimable_file_cache():
    cgroup_v2 = {"memory_stats": {"usage": 100, "stats": {"inactive_file": 30}}}
    cgroup_v1 = {"memory_stats": {"usage": 100, "stats": {"total_inactive_file": 40}}}
    assert working_set(cgroup_v2) == 70
    assert working_set(cgroup_v1) == 60
    assert working_set({"memory_stats": {}}) == 0  # a stopped container reports nothing
    assert working_set({"memory_stats": {"usage": 5, "stats": {"inactive_file": 9}}}) == 0


def test_leak_check_counts_only_owned_resources():
    client = Mock()
    client.containers.list.return_value = [Mock()]
    client.networks.list.return_value = []
    assert leaked(client) == {"containers": 1, "networks": 0}
    filters = client.containers.list.call_args.kwargs["filters"]
    assert filters == {"label": "io.faultline.managed=true"}
    assert client.containers.list.call_args.kwargs["all"] is True
