from __future__ import annotations

from pathlib import Path

import pytest

from traceforge.config import Settings
from traceforge.storage import Storage
from traceforge.workspace import Workspace


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return Settings(
        workspace=workspace,
        data_dir=data_dir,
        api_key="test-key",
        base_url="http://model.test/v1",
        model="test-model",
    )


@pytest.fixture
def storage(settings: Settings) -> Storage:
    repository = Storage(settings.data_dir / "test.db")
    yield repository
    repository.close()


@pytest.fixture
def workspace(settings: Settings, storage: Storage) -> Workspace:
    return Workspace(settings.workspace, storage)

