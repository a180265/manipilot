"""monkeypatch 补丁目标存活扫描（2026-10-15，自 WX arm_fleet #11 双向移植；
原型=sales_bot test_patch_targets，#5 复盘固化）。

背景：模块拆分/常量外迁时，字符串路径补丁打在旧模块路径上会静默失效——
setattr 不报错、消费代码读的是新模块自己的全局名、测试直接假红，只能靠人工
grep 兜底。实爆案例（#7 会话）：TASK_STOP_TIMEOUT_SECONDS 随唯一消费者从
runtime 主模块外迁到 runtime.plugin_capabilities，测试字符串补丁须人工跟随。
本测试把「补丁目标必须被目标模块自己读取」变成常驻护栏。

判定规则（保守，宁可漏报不误报）：
- 目标名在目标模块**模块级定义**（def/class/赋值）→ 存活；
- 目标名是模块级 import 进来的 re-export，但模块自身代码至少读取过一次 → 存活；
- 目标名只是被 re-export 且模块零读取、或目标模块根本没这个名字（setattr 会
  凭空创建无人读的属性）→ **死亡**，测试失败。修法 = 补丁跟随定义点，如
  `runtime.runtime.TASK_STOP_TIMEOUT_SECONDS` → `runtime.plugin_capabilities.
  TASK_STOP_TIMEOUT_SECONDS`。

覆盖三种调用形态：
- 字符串路径：monkeypatch.setattr("runtime.plugin_capabilities.X", ...)
- 模块别名：  monkeypatch.setattr(plugin_capabilities_module, "X", ...)
- 属性链：    monkeypatch.setattr(runtime.plugin_capabilities, "X", ...)

与 sales_bot 版的移植差异（WX 侧 25 判红逐案定性后固化，惯用法豁免，
均为保守方向=跳过或跟随，不放松判定本身）：
1. **纯路径解析 + 多树根归属**：arm_fleet 补丁目标横跨 runtime/drivers/
   adapters/scripts/... 多个顶层包根，范围判定不按单一包名前缀，改为「目标
   模块文件必须落在本测试树根之下」。模块存在性/源码一律按点分路径在树根下
   直取 .py 或 __init__.py，**不经 find_spec**——find_spec 对点分名会导入
   父包（真实导入副作用，连带执行隔离手术 shim 的 sys.modules 自替换），且
   会被已执行 shim 的 sys.modules 影子与 venv 可编辑安装带偏到别处；路径直
   取对三者全部免疫，错树时补丁点归零、被样本量门咬住；
2. **根名遮蔽豁免**（arm_fleet 高频）：测试惯用 `runtime = env["runtime"]`
   拿 app 实例再 `setattr(runtime.store, "get_task", ...)` 打实例属性——局部
   变量遮蔽了模块名，链式首参不再是模块。文件内任意位置出现根名的本地绑定
   （赋值/参数/for-with-except 目标/def 类名）即豁免该根的链式补丁点；
3. **raising=False 豁免**：`setattr(m, "名", v, raising=False)` 是测试自建名
   的两步补丁舞（先建后打），名字缺席是预期行为，不判死；
4. **sys.modules 自替换 shim 跟随**（WX 隔离手术的兼容 shim 形态，本仓发行
   原则上不含；逻辑保留以防同类形态）：这类 shim 在 import 时把
   sys.modules[__name__] 换成真实模块，运行期补丁落在真实模块上——shim 一律
   从磁盘文件识别（与导入态无关），活性对跟随后的真实模块判定；shim 解析
   不出真实名则跳过该补丁点。

范围与豁免（其余）：
- 只扫描本目录（tests/）顶层 *.py，只检查模块文件落在本树根下的目标；
- 补丁目标解析后是**嵌套对象**（最长可导入前缀之后链长>1，如
  `...ArmFleetClient.__init__`）按对象身份操作、与模块全局无关，跳过；
- 非模块别名（如 fixture 包装的模块对象）、drill 到非模块的属性链（如
  `socket.socket` 类）、无法静态解析的目标、相对导入，一律跳过（保守）；
- 纯静态：只读源码做 AST 解析，绝不导入/执行任何目标模块。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
TREE_ROOT = TESTS_DIR.parent

# 样本量门下限：可解析补丁点数的保守基线（2026-10-15 实测 272，取 ~81% 余量；
# 见 test_scanner_sees_the_suite）。骤降说明扫描器坏了或解析错树，先核实再调。
MIN_PATCH_SITES = 220

# sys.modules[__name__] = ... 自替换 shim 的两种文理（2026-10-12 隔离手术形态）
_SHIM_ASSIGN_RE = re.compile(r"^\s*[\w.]+\.modules\[__name__\]\s*=", re.M)
_SHIM_TARGET_RE = re.compile(r"import_module\(\s*[\"']([\w.]+)[\"']")


def _tree_module_file(mod_name: str, root: Path):
    """点分模块名 → 本树根下的源码文件；纯路径直取，无任何导入副作用。"""
    base = root.joinpath(*mod_name.split("."))
    for cand in (base.parent / (base.name + ".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def _load_module_source(mod_name: str, root: Path):
    """返回本树内模块源码文本；树外/不存在的名字返回 None。"""
    path = _tree_module_file(mod_name, root)
    if path is None:
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _canonical(mod_name: str, root: Path, _chain=frozenset()):
    """sys.modules 自替换 shim 单跳跟随；非 shim 原样返回，解析不出 → None。

    隔离手术的兼容 shim（如 adapters.wechat.navigation_templates →
    adapters.uinav.navigation_templates）在 import 时整体替换 sys.modules
    条目：运行期属性访问与 monkeypatch 都落在真实模块上，活性须对真实模块
    判定。shim 从磁盘源码识别，与当前导入态无关。
    """
    if mod_name in _chain:
        return None
    src = _load_module_source(mod_name, root)
    if src is None:
        return None  # 树内无此模块文件（如类对象的 import 别名）→ 不可判，豁免
    if not _SHIM_ASSIGN_RE.search(src):
        return mod_name
    m = _SHIM_TARGET_RE.search(src)
    if not m:
        return None
    real = m.group(1)
    if _tree_module_file(real, root) is None:
        return None
    return _canonical(real, root, _chain | {mod_name})


def _module_liveness(mod_name: str, root: Path, cache={}):
    """(defined, imported, loaded)：模块级定义名 / import 名 / 全部 Load 名。"""
    key = (str(root), mod_name)
    if key in cache:
        return cache[key]
    result = (set(), set(), set(), False)
    src = _load_module_source(mod_name, root)
    if src is not None:
        try:
            tree = ast.parse(src)
        except SyntaxError:
            tree = None
        if tree is not None:
            defined, imported, loaded = set(), set(), set()

            def bind_import(node):
                for a in node.names:
                    imported.add((a.asname or a.name).split(".")[0])

            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    bind_import(node)
                elif isinstance(node, ast.Assign):
                    for t in node.targets:
                        for n in ast.walk(t):
                            if isinstance(n, ast.Name):
                                defined.add(n.id)
                elif isinstance(node, ast.AnnAssign) \
                        and isinstance(node.target, ast.Name):
                    defined.add(node.target.id)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                       ast.ClassDef)):
                    defined.add(node.name)
            for n in ast.walk(tree):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    loaded.add(n.id)
            result = (defined, imported, loaded, True)
    cache[key] = result
    return result


def _is_live(mod_name: str, name: str, root: Path) -> bool:
    defined, imported, loaded, _ = _module_liveness(mod_name, root)
    return name in defined or (name in imported and name in loaded)


def _shadow_bindings(tree):
    """文件内出现本地绑定的名字（赋值/参数/for-with-except 目标/def 类名）。

    链式补丁首参的根名一旦在此集合中（如 `runtime = env["runtime"]` 遮蔽模块
    名 runtime），该文件里以它为根的属性链不再视为模块引用——豁免而非判死。
    import 绑定不算（那正是合法的模块引用）。
    """
    bound = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            for x in ast.walk(n.target):
                if isinstance(x, ast.Name):
                    bound.add(x.id)
        elif isinstance(n, ast.withitem) and n.optional_vars is not None:
            for x in ast.walk(n.optional_vars):
                if isinstance(x, ast.Name):
                    bound.add(x.id)
        elif isinstance(n, ast.NamedExpr):
            for x in ast.walk(n.target):
                if isinstance(x, ast.Name):
                    bound.add(x.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
    return bound


def _collect_aliases(tree):
    """(alias→dotted, root→{full dotted})：供别名/属性链两种形态解析。"""
    aliases, plain = {}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    aliases[a.asname] = a.name
                else:
                    plain.setdefault(a.name.split(".")[0], set()).add(a.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for a in node.names:
                aliases[a.asname or a.name] = f"{node.module}.{a.name}"
    return aliases, plain


def _resolve_string(dotted: str, root: Path):
    """最长树内可解析前缀 → (模块, 余链)；树外目标（标准库/第三方）→ None。"""
    parts = dotted.split(".")
    for cut in range(len(parts), 0, -1):
        prefix = ".".join(parts[:cut])
        if _tree_module_file(prefix, root) is not None:
            return prefix, parts[cut:]
    return None


def _resolve_attribute_chain(first: ast.Attribute, root: Path, shadowed):
    """属性链首参逐段下钻到树内模块对象；根名被遮蔽、drill 断链或出树 → None。"""
    chain = []
    cur = first
    while isinstance(cur, ast.Attribute):
        chain.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name) or cur.id in shadowed:
        return None
    if _tree_module_file(cur.id, root) is None:
        return None
    mod = cur.id
    for part in chain:
        mod = f"{mod}.{part}"
        if _tree_module_file(mod, root) is None:
            return None
    return mod


def _collect_sites(tests_dir: Path | None = None, root: Path | None = None):
    tests_dir = tests_dir or TESTS_DIR
    root = root or TREE_ROOT
    sites = []
    for path in sorted(tests_dir.glob("*.py")):
        if path.name == "test_patch_targets.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            sites.append({"file": path.name, "line": 0, "target": "<整文件语法错误>",
                          "kind": "parse", "live": False})
            continue
        aliases, plain = _collect_aliases(tree)
        shadowed = _shadow_bindings(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("setattr", "delattr") and node.args):
                continue
            recv = node.func.value
            if not (isinstance(recv, ast.Name) and recv.id == "monkeypatch"):
                continue
            # raising=False = 测试自建名的两步补丁舞，名字缺席是预期 → 不判
            if any(kw.arg == "raising" and isinstance(kw.value, ast.Constant)
                   and kw.value.value is False for kw in node.keywords):
                continue
            first, rest = node.args[0], node.args[1:]
            target = None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                resolved = _resolve_string(first.value, root)
                if resolved and len(resolved[1]) == 1:
                    owner = _canonical(resolved[0], root)
                    if owner:
                        target = (owner, resolved[1][0])
            elif isinstance(first, ast.Name) and first.id in aliases and rest \
                    and isinstance(rest[0], ast.Constant) \
                    and isinstance(rest[0].value, str):
                owner = _canonical(aliases[first.id], root)
                if owner:
                    target = (owner, rest[0].value)
            elif isinstance(first, ast.Attribute):
                # 属性链首参（如 import runtime.plugin_capabilities 后写
                # runtime.plugin_capabilities）：逐段下钻到模块对象，第二参为补丁名
                if rest and isinstance(rest[0], ast.Constant) \
                        and isinstance(rest[0].value, str):
                    mod = _resolve_attribute_chain(first, root, shadowed)
                    if mod:
                        owner = _canonical(mod, root)
                        if owner:
                            target = (owner, rest[0].value)
            if target:
                mod, name = target
                sites.append({"file": path.name, "line": node.lineno,
                              "target": f"{mod}.{name}", "kind": "patch",
                              "live": _is_live(mod, name, root)})
    return sites


SITES = _collect_sites()
PATCH_SITES = [s for s in SITES if s["kind"] == "patch"]


def _dead_message(site) -> str:
    if site["kind"] == "parse":
        return f"{site['file']} 整文件语法错误，补丁扫描无法进行，先修复语法。"
    return (f"{site['file']}:{site['line']} 补丁目标 {site['target']} 是死补丁："
            f"目标模块不定义该名字，或只是 re-export 且自身从不读取——"
            f"补丁会静默失效。请把补丁路径改到名字的定义模块（参考 #5/#7 复盘）。")


def test_scanner_sees_the_suite():
    """扫描器自身护栏：补丁点样本量骤降说明解析坏了（防扫描器静默变 no-op）。

    常见坏法：测试树根定位错（复制/移动文件）让路径解析全部落空，树内补丁
    点被范围过滤归零。
    """
    assert len(PATCH_SITES) >= MIN_PATCH_SITES, (
        f"补丁点扫描数异常（当前 {len(PATCH_SITES)}，下限 {MIN_PATCH_SITES}）："
        "扫描器或测试形态变化，或测试树根定位错，先核实再调阈值")


@pytest.mark.parametrize("site", PATCH_SITES,
                         ids=lambda s: f"{s['file']}:{s['line']}:{s['target']}")
def test_patch_target_is_read_by_its_module(site):
    assert site["live"], _dead_message(site)


def test_self_proof_dead_targets_are_bitten(tmp_path, monkeypatch):
    """红绿自证：合成包里的死补丁被精确咬住、活补丁与豁免形态各归其位
    （不放脏样本进真实树）。"""
    pkg = tmp_path / "fakepkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    # 死亡形态：re-export 且模块零读取；名字根本不存在
    (pkg / "dead_mod.py").write_text(
        "from collections import OrderedDict\n", encoding="utf-8")
    # 存活形态：模块级定义；re-export 且模块自读
    (pkg / "live_mod.py").write_text(
        "from collections import OrderedDict\n"
        "TIMEOUT = 5.0\n"
        "\n"
        "def reader():\n"
        "    return OrderedDict\n",
        encoding="utf-8")
    # sys.modules 自替换 shim → live_mod：补丁活性须对真实模块判定
    (pkg / "shim_old.py").write_text(
        "import importlib as _importlib\n"
        "import sys as _sys\n"
        '_real = _importlib.import_module("fakepkg.live_mod")\n'
        "_sys.modules[__name__] = _real\n",
        encoding="utf-8")
    # 自替换但目标不存在：shim 解析不出 → 该补丁点豁免
    (pkg / "shim_broken.py").write_text(
        "import importlib as _importlib\n"
        "import sys as _sys\n"
        '_real = _importlib.import_module("fakepkg.nothere")\n'
        "_sys.modules[__name__] = _real\n",
        encoding="utf-8")
    tests_dir = tmp_path / "fake_tests"
    tests_dir.mkdir()
    (tests_dir / "test_sample.py").write_text(
        "import fakepkg\n"
        "import fakepkg.dead_mod\n"
        "import fakepkg.live_mod\n"
        "import fakepkg.dead_mod as dead_alias\n"
        "import fakepkg.live_mod as live_alias\n"
        "\n"
        "def fake_tests(monkeypatch):\n"
        # 14 个样本行=字符串×6（活2/死2/shim跟随/shim死链）+别名×2+属性链×5+
        # raising=False×1；嵌套对象与非字符串名两行豁免。期望编码在下方断言。
        '    monkeypatch.setattr("fakepkg.live_mod.TIMEOUT", 1)\n'
        '    monkeypatch.setattr("fakepkg.live_mod.OrderedDict", 1)\n'
        '    monkeypatch.setattr("fakepkg.dead_mod.OrderedDict", 1)\n'
        '    monkeypatch.setattr("fakepkg.dead_mod.NOSUCH", 1)\n'
        '    monkeypatch.setattr("fakepkg.shim_old.TIMEOUT", 1)\n'
        '    monkeypatch.setattr("fakepkg.shim_broken.NOSUCH", 1)\n'
        '    monkeypatch.setattr(live_alias, "TIMEOUT", 1)\n'
        '    monkeypatch.setattr(dead_alias, "NOSUCH", 1)\n'
        '    monkeypatch.setattr(fakepkg.live_mod, "TIMEOUT", 1)\n'
        '    monkeypatch.setattr(fakepkg.dead_mod, "NOSUCH", 1)\n'
        '    monkeypatch.setattr(fakepkg.live_mod, "OrderedReader", 1)\n'
        '    monkeypatch.setattr(fakepkg.live_mod.reader, "f", 1)\n'
        '    monkeypatch.setattr(fakepkg.live_mod, 123, 1)\n'
        '    monkeypatch.setattr("fakepkg.dead_mod.GHOST", 1, raising=False)\n'
        "\n",
        encoding="utf-8")
    # 根名遮蔽豁免：本文件内 fakepkg 被本地绑定，链式补丁全部豁免
    (tests_dir / "test_shadowed.py").write_text(
        "import fakepkg.live_mod\n"
        "\n"
        "def fake_tests_shadow(monkeypatch):\n"
        "    fakepkg = object()\n"
        '    monkeypatch.setattr(fakepkg.live_mod, "NOSUCH", 1)\n'
        "\n",
        encoding="utf-8")
    # 回归陷阱（#11 实测）：先把 shim 真的导入执行，让 sys.modules 出现被替换
    # 的影子条目——解析若经 find_spec/sys.modules 会被带偏，路径直取必须免疫
    monkeypatch.syspath_prepend(str(tmp_path))
    import importlib

    importlib.import_module("fakepkg.shim_old")

    sites = _collect_sites(tests_dir=tests_dir, root=tmp_path)
    by_target = {}
    for s in sites:
        by_target.setdefault(s["target"], []).append(s)

    # 字符串4活死各半 + shim跟随1 + 别名2 + 属性链3 = 10 个补丁点、5 个去重目标；
    # shim死链/嵌套对象/非字符串名/raising=False/根名遮蔽 5 类豁免全部不入册
    assert len(sites) == 10, f"自证样本解析数异常：{sorted(s['target'] for s in sites)}"
    assert sorted(by_target) == [
        "fakepkg.dead_mod.NOSUCH",
        "fakepkg.dead_mod.OrderedDict",
        "fakepkg.live_mod.OrderedDict",
        "fakepkg.live_mod.OrderedReader",
        "fakepkg.live_mod.TIMEOUT",
    ]
    # shim 跟随后的活目标与字符串/别名/属性链同目标合流（4 处全活），
    # 且 shim 已被执行（sys.modules 影子在场）仍须报到真实模块名下
    assert len(by_target["fakepkg.live_mod.TIMEOUT"]) == 4
    assert all(s["live"] for s in by_target["fakepkg.live_mod.TIMEOUT"])
    assert all(s["live"] for s in by_target["fakepkg.live_mod.OrderedDict"])
    for tgt in ("fakepkg.dead_mod.OrderedDict", "fakepkg.dead_mod.NOSUCH",
                "fakepkg.live_mod.OrderedReader"):
        assert all(not s["live"] for s in by_target[tgt]), tgt
    # 同一死目标被字符串/别名/属性链三形态分别咬住
    assert len(by_target["fakepkg.dead_mod.NOSUCH"]) == 3
    # 失败报文带 file:line 与修法指引
    dead_site = by_target["fakepkg.dead_mod.OrderedDict"][0]
    msg = _dead_message(dead_site)
    assert f"{dead_site['file']}:{dead_site['line']}" in msg
    assert "定义模块" in msg
