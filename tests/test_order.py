from claude_instances.order import apply_order, load_order, move_name, save_order


def test_apply_order_ignores_unknown_and_duplicates():
    assert apply_order(["B", "A", "C"], ["C", "Zed", "C", "A"]) == ["C", "A", "B"]


def test_apply_order_empty_is_codepoint_sorted():
    assert apply_order(["b", "B", "A"], []) == ["A", "B", "b"]


def test_move_name_clamps():
    assert move_name(["A", "B", "C"], "C", -1) == ["A", "C", "B"]
    assert move_name(["A", "B", "C"], "A", -1) == ["A", "B", "C"]


def test_roundtrip_dedup(tmp_path):
    save_order(tmp_path / "cfg/order.txt", ["B", "A", "B"])
    assert (tmp_path / "cfg/order.txt").read_text() == "B\nA\n"
    assert load_order(tmp_path / "cfg/order.txt") == ["B", "A"]


def test_load_without_trailing_newline(tmp_path):
    (tmp_path / "o.txt").write_text("B\nA")
    assert load_order(tmp_path / "o.txt") == ["B", "A"]


def test_load_missing_is_empty(tmp_path):
    assert load_order(tmp_path / "none") == []
