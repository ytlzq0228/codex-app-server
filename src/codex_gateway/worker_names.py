"""Worker name lifecycle helpers."""

DELETED_WORKER_PREFIX = "deleteed-"
WORKER_NAME_MAX_LENGTH = 180


def archived_worker_name(value: str, worker_id: object, occupied: set[str]) -> str:
    """Return a unique archived name while keeping the deleted prefix visible."""
    if value.startswith(DELETED_WORKER_PREFIX):
        occupied.add(value)
        return value

    base = DELETED_WORKER_PREFIX + value
    if len(base) > WORKER_NAME_MAX_LENGTH:
        suffix = f"-{worker_id}"
        candidate = base[: WORKER_NAME_MAX_LENGTH - len(suffix)] + suffix
    else:
        candidate = base
    if candidate not in occupied:
        occupied.add(candidate)
        return candidate

    identity = str(worker_id)
    counter = 0
    while True:
        suffix = f"-{identity}" if counter == 0 else f"-{identity}-{counter}"
        candidate = base[: WORKER_NAME_MAX_LENGTH - len(suffix)] + suffix
        if candidate not in occupied:
            occupied.add(candidate)
            return candidate
        counter += 1
