import os

from wakefinder.common.killswitch import disengage, engage, is_engaged


def test_not_engaged_by_default(tmp_path):
    path = str(tmp_path / "kill")
    assert not is_engaged(path)


def test_engage_creates_file(tmp_path):
    path = str(tmp_path / "kill")
    engage(path, "test reason")
    assert is_engaged(path)
    with open(path) as f:
        assert "test reason" in f.read()


def test_disengage_removes_file(tmp_path):
    path = str(tmp_path / "kill")
    engage(path)
    disengage(path)
    assert not is_engaged(path)


def test_disengage_missing_file_is_noop(tmp_path):
    path = str(tmp_path / "nope")
    disengage(path)  # не должно бросать
    assert not os.path.exists(path)


if __name__ == "__main__":
    print("run via pytest (uses tmp_path fixture)")
