from uuid import uuid4

from codex_gateway.worker_names import archived_worker_name


def test_archived_worker_name_uses_requested_prefix_and_is_idempotent():
    worker_id = uuid4()
    occupied = set()
    assert archived_worker_name("favorite-worker", worker_id, occupied) == "deleteed-favorite-worker"
    assert archived_worker_name("deleteed-old-worker", worker_id, occupied) == "deleteed-old-worker"


def test_archived_worker_name_handles_collisions_and_length_limit():
    worker_id = uuid4()
    occupied = {"deleteed-favorite-worker"}
    collision = archived_worker_name("favorite-worker", worker_id, occupied)
    assert collision == f"deleteed-favorite-worker-{worker_id}"

    long_name = "x" * 180
    archived = archived_worker_name(long_name, uuid4(), set())
    assert archived.startswith("deleteed-")
    assert len(archived) == 180
