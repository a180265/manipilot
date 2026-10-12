import types

from runtime.build_info import capture_runtime_build, snapshot_modules


def test_snapshot_reports_loaded_code_separately_from_later_disk_changes(tmp_path):
    path = tmp_path / "probe.py"
    path.write_text("def value():\n    return 1\n", encoding="utf-8")
    module = types.ModuleType("probe")
    module.__file__ = str(path)
    exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
    before = snapshot_modules([module], tmp_path)["probe.py"]
    path.write_text("def value():\n    return 2\n", encoding="utf-8")
    changed_disk = snapshot_modules([module], tmp_path)["probe.py"]
    assert before["source_sha256_at_start"] != changed_disk["source_sha256_at_start"]
    assert before["loaded_functions_sha256"] == changed_disk["loaded_functions_sha256"]
    exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
    reloaded = snapshot_modules([module], tmp_path)["probe.py"]
    assert before["loaded_functions_sha256"] != reloaded["loaded_functions_sha256"]


def test_class_methods_and_properties_are_in_the_loaded_fingerprint(tmp_path):
    path = tmp_path / "probe.py"
    source = (
        "class Example:\n"
        "    @staticmethod\n"
        "    def a(): return 1\n"
        "    @classmethod\n"
        "    def b(cls): return 2\n"
        "    @property\n"
        "    def c(self): return 3\n"
    )
    path.write_text(source)
    module = types.ModuleType("probe")
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    assert snapshot_modules([module], tmp_path)["probe.py"]["function_count"] == 3


def test_runtime_snapshot_is_stable_without_starting_services():
    first = capture_runtime_build()
    second = capture_runtime_build()
    assert first["modules"] == second["modules"]
    assert first["manifest_sha256"] == second["manifest_sha256"]
    assert first["process_id"] == second["process_id"]
    assert "drivers/screen_edge.py" in first["modules"]
    assert first["modules"]["drivers/screen_edge.py"]["function_count"] > 0
