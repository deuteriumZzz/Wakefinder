import os
import tempfile

from wakefinder.common import heartbeat


def test_write_and_is_stale():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "hb")

        assert heartbeat.is_stale(path, max_age_seconds=10)  # файла ещё нет

        heartbeat.write(path)
        beat = heartbeat.last_beat(path)
        assert beat is not None

        assert not heartbeat.is_stale(path, max_age_seconds=10, now=beat + 1)
        assert heartbeat.is_stale(path, max_age_seconds=10, now=beat + 11)


if __name__ == "__main__":
    test_write_and_is_stale()
    print("ok")
