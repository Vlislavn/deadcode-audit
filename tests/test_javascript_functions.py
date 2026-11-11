"""Real Tree-sitter function extraction and clone-normalization contracts."""

import pytest

from deadcode_audit.javascript_functions import collect_functions


def test_function_record_preserves_exact_utf8_source_and_declaration_symbol(tmp_path):
    path = tmp_path / "unicode.js"
    source = "const café = async function inner(value) { return await client.fetch(value); };\n"

    [(record, snippet)] = collect_functions(source, path)

    assert record.file_path == path
    assert record.line == 1
    assert record.symbol == "café"
    assert record.language == "javascript"
    assert (
        snippet == "async function inner(value) { return await client.fetch(value); }"
    )
    assert record.tokens


def test_declarations_generators_and_qualified_methods_have_stable_symbols(tmp_path):
    source = (
        "async function* stream(items) { yield* items; }\n"
        "const handlers = { onOpen(event) { return event.type; } };\n"
        "class Widget { render(ctx) { return ctx.app; } }\n"
    )

    rows = collect_functions(source, tmp_path / "app.js")

    assert [(record.symbol, record.line) for record, _ in rows] == [
        ("stream", 1),
        ("handlers.onOpen", 2),
        ("Widget.render", 3),
    ]
    assert rows[0][1] == "async function* stream(items) { yield* items; }"
    assert "MODE:async" in rows[0][0].tokens
    assert "MODE:generator" in rows[0][0].tokens
    assert all(record.language == "javascript" for record, _ in rows)


def test_same_line_anonymous_callbacks_get_distinct_location_symbols(tmp_path):
    source = "invoke(a => a.ok, b => b.ok);"

    rows = collect_functions(source, tmp_path / "callbacks.js")

    assert [record.symbol for record, _ in rows] == ["anonymous@1:8", "anonymous@1:19"]
    assert [snippet for _, snippet in rows] == ["a => a.ok", "b => b.ok"]


def test_commonjs_assignment_targets_are_retained_as_symbols(tmp_path):
    source = (
        "module.exports = function (ctx) { return ctx.app; };\n"
        "exports.named = async (arg) => api(arg);\n"
    )

    rows = collect_functions(source, tmp_path / "entry.cjs")

    assert [record.symbol for record, _ in rows] == ["module.exports", "exports.named"]
    assert [record.line for record, _ in rows] == [1, 2]
    assert rows[0][1] == "function (ctx) { return ctx.app; }"
    assert rows[1][1] == "async (arg) => api(arg)"


def test_typescript_tsx_and_bodyless_signatures_use_real_grammars(tmp_path):
    ts_path = tmp_path / "service.ts"
    ts_source = (
        "declare function external(x: number): void;\n"
        "export function service(input: Input): Output { return transport.send(input); }\n"
    )
    ts_rows = collect_functions(ts_source, ts_path)
    js_rows = collect_functions(
        "function service(input) { return transport.send(input); }\n",
        tmp_path / "service.js",
    )
    tsx_rows = collect_functions(
        "const View = (props: Props) => <Widget>{props.name}</Widget>;\n",
        tmp_path / "view.tsx",
    )
    signatures = collect_functions(
        "declare function onlyType(x: number): void;\ninterface Contract { run(): void; }\n",
        tmp_path / "types.ts",
    )

    assert [record.symbol for record, _ in ts_rows] == ["service"]
    assert ts_rows[0][0].language == "javascript"
    assert ts_rows[0][0].tokens == js_rows[0][0].tokens
    assert tsx_rows[0][0].symbol == "View"
    assert tsx_rows[0][0].language == "javascript"
    assert tsx_rows[0][1] == "(props: Props) => <Widget>{props.name}</Widget>"
    assert signatures == []


def test_static_computed_api_names_are_preserved_but_other_literals_are_abstracted(
    tmp_path,
):
    source = (
        'function readOne(ctx) { return ctx["read"](ctx.value, "payload"); }\n'
        'function writeOne(input) { return input["write"](input.value, "different"); }\n'
    )

    rows = collect_functions(source, tmp_path / "computed.js")
    read, write = [record for record, _ in rows]

    assert read.tokens != write.tokens
    assert read.semantic != write.semantic
    assert "<string>" in read.tokens


def test_valid_source_without_executable_functions_is_legitimately_empty(tmp_path):
    assert collect_functions("const value = 1;\n", tmp_path / "empty.js") == []


@pytest.mark.parametrize(
    "markup",
    [
        "<p>a & b</p>",
        "<p>a && b</p>",
        "<p>a & b & c</p>",
        "<p>&b</p>",
        "<>R & C \u2192 O</>",
        "<li><b>x</b> & {y && z} & w</li>",
        "<p>a &nbsp b &amp; c</p>",
    ],
)
def test_bare_ampersand_in_jsx_text_is_valid_jsx_and_keeps_exact_offsets(tmp_path, markup):
    source = f"const A = () => {markup};\nfunction after() {{ return 1; }}\n"

    records = collect_functions(source, tmp_path / "view.jsx")

    raw = source.encode("utf-8")
    spans = [raw[r.start_byte : r.end_byte].decode("utf-8") for r, _ in records]
    assert spans == [f"() => {markup}", "function after() { return 1; }"]
    assert [snippet for _, snippet in records] == spans  # source text is the original bytes, not the repair
    assert [r.symbol for r, _ in records] == ["A", "after"]


def test_ampersand_repair_never_hides_a_real_jsx_syntax_error(tmp_path):
    with pytest.raises(SyntaxError, match=r"bad\.jsx"):
        collect_functions("const A = () => <p>a & b {</p>;\n", tmp_path / "bad.jsx")


def test_the_bitwise_and_operator_is_not_rewritten(tmp_path):
    [(record, snippet)] = collect_functions("const f = (a, b) => a & b;\n", tmp_path / "ops.js")
    assert snippet == "(a, b) => a & b"
    assert record.tokens


def test_parse_error_refuses_the_entire_file_before_returning_partial_functions(
    tmp_path,
):
    path = tmp_path / "broken.js"
    source = "function earlier() { return 1; }\nfunction broken( {\n"

    with pytest.raises(SyntaxError, match=r"broken\.js:2:"):
        collect_functions(source, path)


def test_renamed_local_bindings_and_literal_values_share_body_fingerprint(tmp_path):
    source = (
        "function calculateTax(amount) { const subtotal = amount + 3; "
        "return calculator.round(subtotal); }\n"
        "function computeFee(value) { const base = value + 99; "
        "return calculator.round(base); }\n"
    )

    rows = collect_functions(source, tmp_path / "calculations.js")

    assert [record.symbol for record, _ in rows] == ["calculateTax", "computeFee"]
    assert rows[0][0].tokens == rows[1][0].tokens
    assert rows[0][0].semantic == rows[1][0].semantic


def test_api_names_and_operators_remain_distinguishing_signals(tmp_path):
    source = (
        "function readOne(value) { return service.read(value); }\n"
        "function writeOne(input) { return service.write(input); }\n"
        "function add(left, right) { return left + right; }\n"
        "function subtract(a, b) { return a - b; }\n"
        "function loadData(item) { return loadRecord(item); }\n"
        "function saveData(value) { return saveRecord(value); }\n"
    )

    rows = collect_functions(source, tmp_path / "near-misses.js")
    read, write, add, subtract, load, save = [record for record, _ in rows]

    assert read.tokens != write.tokens
    assert read.semantic != write.semantic
    assert add.tokens != subtract.tokens
    assert add.semantic == subtract.semantic
    assert load.tokens != save.tokens
    assert load.semantic != save.semantic


def test_destructuring_aliases_and_nested_shadow_bindings_normalize_safely(tmp_path):
    source = (
        "function left({ payload: item }, ...rest) { "
        "const { value: alias } = convert(item); "
        "return [alias, rest[0], (() => { const item = fetcher(); return item; })()]; }\n"
        "function right({ payload: input }, ...items) { "
        "const { value: result } = convert(input); "
        "return [result, items[0], (() => { const input = fetcher(); return input; })()]; }\n"
    )

    rows = collect_functions(source, tmp_path / "nested.js")
    by_symbol = {
        record.symbol: record
        for record, _ in rows
        if record.symbol in {"left", "right"}
    }

    assert sum(record.symbol.startswith("anonymous@") for record, _ in rows) == 2
    left, right = by_symbol["left"], by_symbol["right"]
    assert left.tokens == right.tokens
    assert left.semantic == right.semantic


def test_method_property_is_not_a_local_binding_but_named_function_expression_is(
    tmp_path,
):
    rows = collect_functions(
        "class Reader { load() { return load(); } }\n"
        "const writer = { save() { return save(); } };\n"
        "const holder = function* recursive() { yield recursive(); };\n",
        tmp_path / "binding-kinds.js",
    )
    records = {record.symbol: record for record, _ in rows}
    assert "#load" in records["Reader.load"].semantic
    assert "#save" in records["writer.save"].semantic
    assert "#recursive" not in records["holder"].semantic


@pytest.mark.parametrize("suffix", ["js", "ts"])
def test_static_block_var_scope_does_not_capture_enclosing_free_apis(tmp_path, suffix):
    rows = collect_functions(
        "function first() { class C { static { var fetch = 1; } } return fetch(); }\n"
        "function second() { class D { static { var request = 1; } } return request(); }\n"
        "function hoisted() { { var fetch = 1; } return fetch(); }\n",
        tmp_path / f"static-scope.{suffix}",
    )
    records = {record.symbol: record for record, _ in rows}
    assert "#fetch" in records["first"].semantic
    assert "#request" in records["second"].semantic
    assert records["first"].tokens != records["second"].tokens
    assert records["first"].semantic != records["second"].semantic
    assert "#fetch" not in records["hoisted"].semantic


def test_large_row_and_column_points_keep_exact_source_across_garbage_collection(
    tmp_path,
):
    import gc

    prefix = "\n" * 300 + " " * 300
    source = prefix + "function longPoint(value) { return api.echo(value); }\n"
    for _ in range(3):
        [(record, snippet)] = collect_functions(source, tmp_path / "large-point.js")
        assert record.line == 301 and record.column == 301
        assert source.encode()[record.start_byte : record.end_byte] == snippet.encode()
        gc.collect()


def test_unsafe_parser_version_is_rejected_before_native_point_getters(
    tmp_path, monkeypatch
):
    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "0.26.0")
    with pytest.raises(RuntimeError):
        collect_functions("function safe() { return 1; }", tmp_path / "version.js")


def test_nested_named_generator_expression_retains_its_lexical_binding(tmp_path):
    rows = collect_functions(
        "function outer() { const iter = function* recursive() { yield recursive(); }; return iter; }\n",
        tmp_path / "nested-generator.js",
    )
    record = next(record for record, _ in rows if record.symbol == "outer")
    assert "#recursive" not in record.semantic
