import pytest

from vidshrink import db
from vidshrink.config import Config


@pytest.fixture
def cfg(tmp_path):
    drive = tmp_path / "drive"
    drive.mkdir()
    return Config(
        remote=str(drive) + "/",
        db_path=tmp_path / "state.db",
        workdir=tmp_path / "work",
        excluded=("Torrent/",),
        min_size=0,
        rclone_args=(),
        retry_base_delay=0.0,
        disk_reserve=0,
        disk_factor=0.0,
        probe_workers=2,
    )


@pytest.fixture
def conn(cfg):
    c = db.connect(cfg.db_path)
    yield c
    c.close()
