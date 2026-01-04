#    QPane - High-performance PySide6 image viewer
#    Copyright (C) 2025  Artificial Sweetener and contributors
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Tests covering catalog refactor behaviours and placeholders."""

import uuid
from pathlib import Path
import pytest
from PySide6.QtGui import QImage
from qpane.catalog import ImageCatalog
from qpane.types import CatalogEntry
from qpane import Config
from tests.helpers.executor_stubs import StubExecutor


class StubPyramidManager:
    def __init__(self):
        self.generated: list[tuple[uuid.UUID, QImage, Path | None]] = []
        self.removed: list[uuid.UUID] = []
        self.cleared = False
        self.apply_calls: list[Config] = []

    def generate_pyramid_for_image(
        self, image_id: uuid.UUID, image: QImage, source_path: Path | None
    ) -> None:
        self.generated.append((image_id, image, source_path))

    def apply_config(self, config: Config) -> None:
        self.apply_calls.append(config)

    def remove_pyramid(self, image_id: uuid.UUID) -> None:
        self.removed.append(image_id)

    def clear(self) -> None:
        self.cleared = True


def _make_image(fmt: QImage.Format) -> QImage:
    image = QImage(4, 4, fmt)
    image.fill(0)
    return image


def test_set_images_normalizes_and_uses_consistent_format(qapp):
    catalog = ImageCatalog(config=Config(), executor=StubExecutor())
    stub = StubPyramidManager()
    catalog.pyramid_manager = stub
    image_id = uuid.uuid4()
    image_map = {
        image_id: CatalogEntry(
            image=_make_image(QImage.Format_RGB32), path=Path("foo.png")
        ),
    }
    removed_ids, changed_ids = catalog.setImagesByID(image_map, image_id)
    stored_image = catalog.images_by_id[image_id]
    assert stored_image.format() == QImage.Format_ARGB32_Premultiplied
    assert removed_ids == set()
    assert changed_ids == {image_id}
    generated_id, generated_image, generated_path = stub.generated[-1]
    assert generated_id == image_id
    assert generated_path == Path("foo.png")
    assert generated_image.format() == QImage.Format_ARGB32_Premultiplied


def test_add_image_normalizes_before_storage_and_pyramid(qapp):
    catalog = ImageCatalog(config=Config(), executor=StubExecutor())
    stub = StubPyramidManager()
    catalog.pyramid_manager = stub
    image_id = uuid.uuid4()
    src_image = _make_image(QImage.Format_RGB32)
    catalog.addImage(image_id, src_image, Path("bar.png"))
    stored_image = catalog.images_by_id[image_id]
    assert stored_image.format() == QImage.Format_ARGB32_Premultiplied
    generated_id, generated_image, generated_path = stub.generated[-1]
    assert generated_id == image_id
    assert generated_path == Path("bar.png")
    assert generated_image.format() == QImage.Format_ARGB32_Premultiplied


def test_add_image_raises_on_null(qapp):
    catalog = ImageCatalog(config=Config(), executor=StubExecutor())
    with pytest.raises(ValueError):
        catalog.addImage(uuid.uuid4(), QImage(), None)
    assert catalog.image_ids == []


def test_update_current_entry_normalizes_and_refreshes_pyramid(qapp):
    catalog = ImageCatalog(config=Config(), executor=StubExecutor())
    stub = StubPyramidManager()
    catalog.pyramid_manager = stub
    image_id = uuid.uuid4()
    initial_image = _make_image(QImage.Format_ARGB32_Premultiplied)
    catalog.setImagesByID(
        {image_id: CatalogEntry(image=initial_image, path=Path("old.png"))}, image_id
    )
    replacement_image = _make_image(QImage.Format_RGB32)
    catalog.updateCurrentEntry(image=replacement_image, path=Path("new.png"))
    stored_image = catalog.images_by_id[image_id]
    assert stored_image.format() == QImage.Format_ARGB32_Premultiplied
    # ensure the old pyramid was removed and the new one uses normalized data
    assert stub.removed[-1] == image_id
    generated_id, generated_image, generated_path = stub.generated[-1]
    assert generated_id == image_id
    assert generated_path == Path("new.png")
    assert generated_image.format() == QImage.Format_ARGB32_Premultiplied


def test_apply_config_regenerates_current_image(qapp):
    catalog = ImageCatalog(config=Config(), executor=StubExecutor())
    stub = StubPyramidManager()
    catalog.pyramid_manager = stub
    image_id = uuid.uuid4()
    image = _make_image(QImage.Format_ARGB32_Premultiplied)
    path = Path("regen.png")
    catalog.setImagesByID({image_id: CatalogEntry(image=image, path=path)}, image_id)
    stub.generated.clear()
    updated = Config(cache={"pyramids": {"mb": 32}})
    catalog.apply_config(updated)
    assert stub.apply_calls[-1] is updated
    assert stub.generated
    generated_id, generated_image, generated_path = stub.generated[-1]
    assert generated_id == image_id
    assert generated_path == path
    assert generated_image.format() == QImage.Format_ARGB32_Premultiplied


def test_catalog_facade_rejects_null_image(qpane_core):
    catalog = qpane_core.catalog()
    with pytest.raises(ValueError):
        catalog.addImage(uuid.uuid4(), QImage(), None)


@pytest.mark.usefixtures("qapp")
def test_catalog_passes_executor_to_pyramid_manager() -> None:
    """ImageCatalog should supply the shared executor to PyramidManager."""
    executor = StubExecutor()
    catalog = ImageCatalog(config=Config(), executor=executor)
    assert getattr(catalog.pyramid_manager, "_executor") is executor
