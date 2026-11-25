import pytest

from config import Config, GmailConfig
from db import Tracker


@pytest.fixture
def config(tmp_path):
    return Config(
        my_name="Carly",
        db_path=tmp_path / "t.db",
        shared_secret="s3cret",
        gmail=GmailConfig(
            subject_patterns=[
                r'^(?P<name>.+?) (?:just )?(?:sent you a (?:new )?message|messaged you)',
                r'^(?:New message|Message) from (?P<name>.+)$',
            ],
        ),
    )


@pytest.fixture
def tracker(config):
    return Tracker(config.db_path)
