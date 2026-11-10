"""Empaque consolidado de piezas en cajas (shipping_packing.build_packages)."""
from decimal import Decimal

import pytest

from app.services.package_dimensions_service import PackageDimensions
from app.services.shipping_packing import MAX_BOX_VOLUME_CM3, MAX_BOX_WEIGHT_KG, PADDING_FACTOR, build_packages


def dims(kg, l, w, h) -> PackageDimensions:
    return PackageDimensions(Decimal(str(kg)), Decimal(str(l)), Decimal(str(w)), Decimal(str(h)))


def _assert_box_contains(box, pieces):
    box_sides = sorted((box.length_cm, box.width_cm, box.height_cm), reverse=True)
    assert box.length_cm >= box.width_cm >= box.height_cm
    for p in pieces:
        piece_sides = sorted((p.length_cm, p.width_cm, p.height_cm), reverse=True)
        assert all(b >= s for b, s in zip(box_sides, piece_sides))


def test_single_piece_keeps_its_dimensions():
    [box] = build_packages([(dims(5.75, 22, 19, 36), 1)])
    assert (box.weight_kg, box.length_cm, box.width_cm, box.height_cm, box.pieces) == (Decimal("5.750"), Decimal("22"), Decimal("19"), Decimal("36"), 1)


def test_small_pieces_consolidate_in_one_padded_box():
    piece = dims(0.1, 10, 5, 2)
    [box] = build_packages([(piece, 10)])
    assert box.pieces == 10
    assert box.weight_kg == Decimal("1.000")
    volume = box.length_cm * box.width_cm * box.height_cm
    assert volume >= Decimal(10 * 100) * PADDING_FACTOR
    _assert_box_contains(box, [piece])


def test_weight_cap_opens_new_box():
    boxes = build_packages([(dims(10, 20, 20, 20), 4)])
    assert sorted(b.pieces for b in boxes) == [1, 3]
    assert all(b.weight_kg <= MAX_BOX_WEIGHT_KG for b in boxes)


def test_oversized_piece_travels_alone_and_others_consolidate():
    compressor = dims(250, 102, 83, 192)
    small = dims(0.5, 10, 10, 10)
    boxes = build_packages([(small, 3), (compressor, 1)])
    alone = [b for b in boxes if b.weight_kg == Decimal("250")]
    assert len(alone) == 1 and (alone[0].length_cm, alone[0].width_cm, alone[0].height_cm) == (Decimal("102"), Decimal("83"), Decimal("192"))
    rest = [b for b in boxes if b is not alone[0]]
    assert len(rest) == 1 and rest[0].pieces == 3


def test_mixed_cart_boxes_respect_caps_and_contain_pieces():
    lines = [(dims(2.06, 16, 66, 4), 2), (dims(0.6, 6.5, 6.5, 24), 12), (dims(5.75, 22, 19, 36), 3)]
    boxes = build_packages(lines)
    assert sum(b.pieces for b in boxes) == 17
    total_weight = sum(b.weight_kg for b in boxes)
    assert total_weight == Decimal("2.06") * 2 + Decimal("0.6") * 12 + Decimal("5.75") * 3
    for box in boxes:
        assert box.weight_kg <= MAX_BOX_WEIGHT_KG
        if box.pieces > 1:
            # Redondeo hacia arriba a cm entero: puede pasar el tope por menos de un cm por lado.
            assert box.length_cm * box.width_cm * box.height_cm <= (Decimal(60) + 1) ** 3
            assert box.length_cm >= box.width_cm >= box.height_cm
    assert MAX_BOX_VOLUME_CM3 == Decimal(216000)


def test_rejects_non_positive_quantity():
    with pytest.raises(ValueError):
        build_packages([(dims(1, 1, 1, 1), 0)])
