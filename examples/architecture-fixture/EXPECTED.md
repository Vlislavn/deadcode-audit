# architecture-fixture: expected findings (acceptance oracle)

Written from the frozen requirements of the three architecture rules, **not** from any implementation.
Every smell here is deliberate; do not "fix" the fixture. If a rule's semantics change, the lead updates this
file (no silent drift).

Run from the repository root:

```
DEADCODE_REPO_ROOT=examples/architecture-fixture uv run --no-sync deadcode cycles --json
DEADCODE_REPO_ROOT=examples/architecture-fixture uv run --no-sync deadcode scan . --json
uv run --no-sync deadcode scan examples/architecture-fixture --json     # same files, root config absent
```

Layout (`.deadcode.yml`: `source_roots`/`import_roots` = `cyc shal shalmod app widedirs shalid shalcls`, `test_roots` = `tests`).
Dotted module names are relative to those roots (`cyc/cheap/m_a.py` is `cheap.m_a`). Paths below are
relative to the fixture root. Group dirs `cyc/ shal/ shalmod/ app/ widedirs/` are not packages (no `__init__.py`); each
has fewer than 15 members (cyc 9, shal 13, shalmod 2, app 14, widedirs 4, shalid 11, shalcls 6), so none of them may be reported as wide.

`.deadcode.yml` `allowed_cycles`: `shrink.a <-> shrink.b <-> shrink.c`, `grow.a <-> grow.b`.

Anything not listed as FIRE below must produce **no** `architecture/*` finding or cycle (that includes every
`app/use_*.py` consumer, every `shalid/use_*.py` / `shalcls/use_*.py` consumer, every `widedirs/*/mod_*.py` file and all of `tests/`).

## 1. Cycles (`deadcode cycles --json`)

Cycle id = member set. "ties" = import edges with imported names; cost = distinct imported names.

| Case | Rule | Path (members) | Result | Reason |
|---|---|---|---|---|
| (a) cheap | cycles | `cyc/cheap/m_a.py` <-> `cyc/cheap/m_b.py` (`cheap.m_a`, `cheap.m_b`) | FIRE (hard, not allowed) | `m_a -> m_b` imports 1 name (`b_one`), `m_b -> m_a` imports 3 (`a_one a_two a_three`). Expected line: `cheapest cut: cheap.m_a -> cheap.m_b (1 name) cyc/cheap/m_a.py:2` (wording of the count as the implementation prints it; cut must be a->b at m_a.py:2). |
| (b) shrink | cycles | `cyc/shrink/a.py` <-> `cyc/shrink/b.py` | FIRE as a cycle but **covered** (`allowed: true`, no exit-1 contribution) | Actual member set {a,b} is a SUBSET of the entry `shrink.a <-> shrink.b <-> shrink.c`. `c` only imports `a` (not in the cycle). |
| (c) grow | cycles | `cyc/grow/a.py` -> `b.py` -> `c.py` -> `a.py` (`grow.a`, `grow.b`, `grow.c`) | FIRE (hard, **not** covered) | Entry `grow.a <-> grow.b` covers only {a,b}; the cycle grew to {a,b,c}, which is not a subset of the entry. |
| (d) package cycle, no file cycle | cycles | `cyc/pkgp/x/m1.py` -> `cyc/pkgp/y/m2.py`; `cyc/pkgp/y/m3.py` -> `cyc/pkgp/x/m4.py` | FIRE: exactly one cycle, `kind: package`, members {`pkgp.x`, `pkgp.y`}; NO file cycle | m2 and m4 import nothing back, so no file SCC. Lifted edges `pkgp.x -> pkgp.y` and `pkgp.y -> pkgp.x` (LCA `pkgp`) close a package cycle. Advisory: never exit 1 by itself. |
| (e) file cycle in one package | cycles | `cyc/pkgq/a.py` <-> `cyc/pkgq/b.py` | FIRE: file cycle {`pkgq.a`,`pkgq.b`} (hard) and NO package cycle | Lifted package edges have member set {`pkgq.a`,`pkgq.b`} = the file cycle's member set, so the package cycle is dropped. |
| (f) cross-package file cycle | cycles | `cyc/pkgr/x/m.py` <-> `cyc/pkgr/y/n.py` | FIRE twice: file cycle {`pkgr.x.m`,`pkgr.y.n`} (hard) AND package cycle {`pkgr.x`,`pkgr.y`} (`kind: package`) | Package member set differs from the file member set, so it is kept. |
| (g1) soft cycle | cycles | `cyc/soft/a.py` <-> `cyc/soft/b.py` | FIRE as `kind: soft` (not blocking) | `b -> a` is a function-local import. |
| (g2) TYPE_CHECKING only | cycles | `cyc/tc/a.py`, `cyc/tc/b.py` | NO-FIRE | `a -> b` exists only under `if TYPE_CHECKING:`; no runtime cycle (and no package cycle). |
| (v) LCA is an endpoint (extra) | cycles | `cyc/pkgs/__init__.py` <-> `cyc/pkgs/sub.py` (`pkgs`, `pkgs.sub`) | FIRE as file cycle (hard); NO package cycle | Lifted edge is skipped because the LCA (`pkgs`) is one of the endpoints. |

Total expected after the work lands: 7 file cycles listed in the table (a, b-covered, c, e, f, g1-soft, v) plus
2 package cycles (d, f). The pre-change baseline lists the same 7 file-level cycles (see report) without
`ties`, `circle`, subset coverage or package cycles.

## 2. architecture/shallow-module (`deadcode scan`, warning)

Semantics (updated): only FUNCTIONS are working items. Classes are not counted (neither in `shown` nor in
`total`); methods of top-level classes count toward `total` only (dunders, property/setters, thin wrappers and
methods of classes with an external base excluded). A class only matters as the head of a list: a shown free
function is on the list of EVERY in-unit class it references (counted once in `shown`), a class being its defining
(module, name) — own class or a from-import of one — so same-named classes in different files stay apart. `shown` = free functions USED by runtime
code outside the unit (the consumer must load a name that resolves, through re-exports, to that very definition: an unused import, an `__all__` entry, a homonym elsewhere or a test never counts; a package `__init__` counts only if it uses the name); `hidden = total - shown`. FIRE iff `shown > 7` AND (`lists > 7` OR some list `> 7`) AND
`hidden <= shown`. Tests never count.

Units: a package dir (reported at `__init__.py:1`) or a file module (`file:1`). **A package unit is skipped when
its working-item set equals one of its modules' (facade package + sole module); then only the module is
reported.** Every package below except (h2) is such a facade (`__init__.py` re-exports from a single
module, `core.py` (`engine.py` in (n))), so its finding, if any, is at that module's `:1`, **never** at `__init__.py:1`. "NO-FIRE" means no finding
at either path. Consumers are `app/use_<case>.py` (case t: `tests/test_shaltest.py`).

| Case | Rule | Path (report location) | Result | shown / lists / hidden and reason |
|---|---|---|---|---|
| (h) shallow | shallow-module | `shal/shallow/core.py:1` (package `shal/shallow/` has a real `__init__.py` re-exporting from `core.py`; NO finding at `shal/shallow/__init__.py`) | FIRE | 9 / 9 / 6. 15 functions (9 public shown, 3 private + 3 public unused outside); no class, so each shown function is its own list. Package set == module set, so only the module is reported. |
| (h2) multi-module package (extra) | shallow-module | `shal/multi/__init__.py:1` | FIRE (package); NO finding at `shal/multi/a.py` / `b.py` | Package 10 / 10 / 4 (modules `a`, `b` each 5 shown + 2 private functions). Package set differs from either module's set, so the package is not skipped. Each module alone is 5 / 5 / 2: shown not > 7. |
| (i) deep | shallow-module | `shal/deep/core.py:1` | NO-FIRE | 3 / 3 / 17. shown not > 7. |
| (j) padded | shallow-module | `shal/padded/core.py:1` | NO-FIRE | 1 / 1 / 8. 9 functions are imported from outside but 8 are thin wrappers (`return _implN(a, b)`), excluded from working items. Counted naively it would be 9 / 9 / 8 = FIRE. |
| (k) private | shallow-module | `shal/private/core.py:1` | NO-FIRE | 8 / 8 / 9. 9 underscore helpers (used only inside) are working items, so hidden 9 > shown 8. If private helpers were ignored it would be 8 / 8 / 0 = FIRE. |
| (l) grouped | shallow-module | `shal/grouped/core.py:1` | NO-FIRE | 9 / 4 / 6. Lists: 4 functions return in-unit `Ledger` (1 list of 4), 3 build in-unit `Journal` in the body (1 list of 3), 2 plain functions (2 lists) = 4 lists, largest 4. Total 15 = 11 functions + 4 methods (`Ledger.add/reset`, `Journal.record/clear`); classes not counted. See OPEN QUESTION 1. |
| (l2) grouped_big (extra) | shallow-module | `shal/grouped_big/core.py:1` | FIRE | 9 / 1 / 4. All 9 shown functions return in-unit `Account` -> one list of 9 > 7 ("some list > 7"). Total 13 = 12 functions + `Account.deposit`. See OPEN QUESTION 1. |
| (m1) boundary 8/8 | shallow-module | `shal/bound_fire/core.py:1` | FIRE | 8 / 8 / 8. shown 8 > 7, lists 8 > 7, hidden 8 <= shown 8. |
| (m2) boundary 8/9 | shallow-module | `shal/bound_hidden/core.py:1` | NO-FIRE | 8 / 8 / 9. hidden 9 > shown 8. |
| (m3) boundary 7/0 | shallow-module | `shal/bound_seven/core.py:1` | NO-FIRE | 7 / 7 / 0. shown 7 is not > 7. |
| (n) methods | shallow-module | `shal/methods/engine.py:1` (sole module of the facade package) | NO-FIRE | 2 / 2 / 20. `Engine` has 20 methods and `app/use_methods.py` calls every one, but methods never count as shown; only `make_engine` and `describe` are imported (the class is not). Total 22 = 20 methods + 2 functions. |
| (s) file module (extra) | shallow-module | `shalmod/flat.py:1` | FIRE | 9 / 9 / 6. Unit = file module. Same shape as (h). |
| (t) tests only (extra) | shallow-module | `shal/shaltest/core.py:1` | NO-FIRE | 0 / 0 / 15. Same shape as (h) but the only importer is `tests/test_shaltest.py`; tests never count. |
| (u) excluded kinds (extra) | shallow-module | `shal/excluded/core.py:1` | FIRE | 8 / 8 / 4. Hidden = 2 private functions + `Plain.bump`/`Plain.drop`. Excluded (not counted): 4 dunders + 4 property/setter defs in `Plain`, 8 methods of `Worker(threading.Thread)` (external base). If they were counted, hidden would be 20 > 8 and it would not fire. |
| (w) data declaration (extra) | shallow-module | `shalmod/records.py:1` | NO-FIRE | 0 / 0 / 0. 17 classes (9 dataclasses, 4 NamedTuple, 4 TypedDict), all imported by `app/use_records.py`, no functions, no methods. Classes are not working items. If classes counted it would be 17 / 17 / 0 = FIRE. |
| (x1) homonym via re-export | shallow-module | `shalid/hom_a.py:1` FIRE; `shalid/hom_b.py:1` NO-FIRE | FIRE (hom_a only) | `hom_a` and `hom_b` both define `f1..f8`; package `hom_p` re-exports `hom_a`'s; `use_hom.py` imports them from `hom_p` and calls them. 8 / 8 / 0 for `hom_a`. `hom_b` is never used, so the same spelling must not credit it. |
| (x2) unused import | shallow-module | `shalid/unu.py:1` | NO-FIRE | `use_unu.py` does `from unu import h1..h8` and never loads them: an import is not a use. |
| (x3) real calls from another package's `__init__` | shallow-module | `shalid/rin.py:1` | FIRE | `rpk/__init__.py` imports `s1..s8` and calls all of them (it has its own function, so it is no facade). 8 / 8 / 0. A package `__init__` is a consumer iff it uses the name. |
| (x4) module alias | shallow-module | `shalid/ali.py:1` | FIRE | `use_ali.py` does `import ali as x` and calls `x.a1()..x.a8()`. 8 / 8 / 0. |
| (x5) test-only consumer | shallow-module | `shalid/tonly.py:1` | NO-FIRE | `e1..e8` are used only by `tests/test_ident.py`; tests never count. |
| (y1) function on every class list | shallow-module | `shalcls/zcls.py:1` | FIRE | 8 / 2 / 0. `g1..g7` use `Z`, `g8` uses `A` and `Z`: `Z`'s list has 8 (> 7), `A`'s list has 1. A function joining only one list (the smallest class name) would give lists 7 + 1 = silent. |
| (y2) homonym classes | shallow-module | `shalcls/kpk/__init__.py:1` | NO-FIRE | Package 10 / 2 / 0: `kpk.m1` and `kpk.m2` each define their own `K` and 5 functions using it: two lists of 5, not one list of 10 under the spelling `K`. Each module alone is 5 shown. |
| (y3) class imported from another module of the unit | shallow-module | `shalcls/kpi/__init__.py:1` | FIRE (package) | Package 10 / 1 / 0: `kpi.m2` does `from kpi.m1 import K`, so its 5 functions and `m1`'s 5 share `K`'s list of 10. |

## 3. architecture/wide-directory (`deadcode scan`, warning)

Members = direct non-test source files + subdirectories containing source. More than 15 fires.

| Case | Rule | Path | Result | Reason |
|---|---|---|---|---|
| (o) 16 files | wide-directory | `widedirs/w16/` (no `__init__.py`: reported at the directory) | FIRE | 16 source files `mod_01..16.py`. |
| (p) 15 files | wide-directory | `widedirs/w15/` | NO-FIRE | 15 files, not > 15. |
| (q) tests dir | wide-directory | `tests/` | NO-FIRE | 20 test files (19 `test_basic_NN.py` + `test_shaltest.py`); the tests dir never counts. |
| (r) mixed | wide-directory | `widedirs/wmix/` (no `__init__.py`: reported at the directory) | FIRE | 10 files `mod_01..10.py` + 6 subdirs `sub1..6/` each with `leaf.py` = 16 members. |

JS/TS (not in this fixture: a `.js` file would make every fixture scan need a local Oxlint). Verified in a scratch git
repo (`src/lib/` with 16 `.js` files, one changed on a branch): `deadcode scan --changed` FIRES wide-directory for `src/lib`
(a JS-only change is a trigger). With 13 `.js` + 3 `*.test.js` (or `*.test.ts`/`*.spec.ts`/`*.spec.js`) the 3 test files are not
members: 13 members, NO-FIRE. Test files are also not triggers.

## OPEN QUESTIONS (not asserted by the rows above)

0. **Facade skip and shown.** For the module unit `core.py` of a facade package, the re-export in `__init__.py`
   and the consumers in `app/` are both "outside" the module; rows assume consumers via the package re-export
   count as outside uses of `core.py` functions (shown 9 in (h)).

1. **(l) wording.** The brief's "9 shown funcs all referencing ONE in-unit class ... lists<=7 and none >7 -> NO-FIRE"
   contradicts the frozen rule: 9 functions grouped under one class form ONE list of 9, which is `> 7`, i.e. FIRE.
   I built (l) as 4 / 3 / 1 / 1 lists (NO-FIRE) and (l2) as the literal one-class-of-9 shape (FIRE). Lead to confirm
   that (l2) = FIRE is intended ("some list > 7").
2. **`__init__.py` as a directory member.** `widedirs/open_init16/` has `__init__.py` + 15 other files. It is 16
   members if `__init__.py` counts, 15 if not. Deliberately NOT asserted; record what the implementation does.
3. **Directory report path without `__init__.py`.** (o) and (r) have none; the rows assume the finding is
   reported at the directory path. Confirm the exact path form.
4. **Underscore function imported from outside.** Not built: whether `from pkg import _helper` makes `_helper`
   "shown" is not settled (shown names "public classes" but "free functions" without qualification).
5. **`@overload` / `@abstractmethod`.** Not built: stub-vs-implementation counting for `@overload` (same name
   defined several times) and `@abstractmethod` in a class that has no external base are not isolated here.
6. **Cheapest-cut tie-break and soft cycles.** (b), (c), (f), (g1), (v) have equal-cost ties on purpose; the
   cut choice among equal costs is not asserted. For the soft cycle (g1) it is not settled whether the lazy edge
   must be preferred as the cut.
7. **Count wording.** The brief prints `(N names)`; for N=1 the implementation may print `1 name`/`1 names`.

## Lead decisions (2026-11-24, after the open questions above)

- `__init__.py` is NOT a directory member: `widedirs/open_init16/` (`__init__` + 15 files) is NO-FIRE.
- (l2) grouped_big (one class heading a list of 9) is FIRE.
- A covered cycle such as (b) is listed only by `deadcode cycles --advisory` (`allowed: true`); blocking mode hides it.
- A wide directory without `__init__.py` is reported at the directory path, line 1.

Verified on the integrated branch: cycles (a)(c)(e)(f)(g1)(v) hard/soft as listed, (b) covered, package cycles (d)(f) only;
shallow-module FIRE for h, h2, l2, m1, s, u; wide-directory FIRE for o, r.
