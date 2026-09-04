"""The import allowlist as a contract (ADR-002 §4 table): every admitted
name imports and works under the REAL kernel import gate, every proxied
module exposes its pure surface and refuses its ambient part, every
refused name fails with the pointer that names where the capability
lives. Host-side CPython under the kernel's curated builtins — the
same gate the wasm guest applies (the wasm-level determinism of the
archive modules is `runner::allowlist_tests`)."""

import pytest
from kernelenv import load_kernel


def guest_exec(src):
    app = load_kernel(lambda name, payload: {"epoch": 0.0, "offset_s": 0})
    ns = {"__builtins__": app._SAFE_BUILTINS}
    exec(src, ns)
    return ns


# every tier-1 entry of the table, with one line of real use each
USES = {
    "json": "json.loads('{\"a\": 1}')['a']",
    "re": "re.sub('a', 'b', 'aa')",
    "string": "string.ascii_lowercase[:3]",
    "textwrap": "textwrap.dedent('  x')",
    "unicodedata": "unicodedata.name('é')",
    "difflib": "difflib.SequenceMatcher(None, 'ab', 'ac').ratio()",
    "csv": "list(csv.reader(['a,b']))",
    "html": "html.unescape('&amp;')",
    "email": "email.utils.parseaddr('A <a@b.c>')",
    "xml.etree": "xml.etree.ElementTree.fromstring('<a><b/></a>').tag",
    "urllib.parse": "urllib.parse.urlparse('https://a.b/c').netloc",
    "tomllib": "tomllib.loads('a = 1')",
    "configparser": "configparser.ConfigParser().sections()",
    "shlex": "shlex.split('a \"b c\"')",
    "fnmatch": "fnmatch.fnmatch('a.md', '*.md')",
    "pprint": "pprint.pformat({'a': 1})",
    "quopri": "quopri.decodestring(b'a=3Db')",
    "plistlib": "plistlib.dumps({'a': 1})",
    "math": "math.sqrt(4)",
    "cmath": "cmath.sqrt(-1)",
    "decimal": "decimal.Decimal('1.1') + 1",
    "fractions": "fractions.Fraction(1, 3)",
    "statistics": "statistics.mean([1, 2, 3])",
    "ipaddress": "ipaddress.ip_address('10.0.0.1').is_private",
    "colorsys": "colorsys.rgb_to_hsv(1, 0, 0)",
    "calendar": "calendar.monthrange(2026, 9)",
    "itertools": "list(itertools.islice(itertools.count(), 3))",
    "functools": "functools.reduce(lambda a, b: a + b, [1, 2])",
    "collections": "collections.Counter('aab')",
    "contextlib": "contextlib.suppress(KeyError)",
    "heapq": "heapq.nsmallest(1, [3, 1, 2])",
    "bisect": "bisect.bisect([1, 3], 2)",
    "graphlib": "list(graphlib.TopologicalSorter({'b': {'a'}}).static_order())",
    "operator": "operator.itemgetter(1)([1, 2])",
    "copy": "copy.deepcopy({'a': [1]})",
    "dataclasses": "dataclasses.make_dataclass('P', ['x'])(1)",
    "enum": "enum.Enum('E', 'A B').A",
    "typing": "typing.Optional[int]",
    "abc": "abc.ABC",
    "traceback": "traceback.format_exc()",
    "base64": "base64.b64encode(b'x')",
    "binascii": "binascii.hexlify(b'x')",
    "struct": "struct.unpack('<I', struct.pack('<I', 7))",
    "array": "array.array('i', [1, 2]).tobytes()",
    "zlib": "zlib.decompress(zlib.compress(b'x'))",
    "gzip": "gzip.decompress(gzip.compress(b'x'))",
    "zipfile": "zipfile.ZipFile(io.BytesIO(), 'w').close()",
    "tarfile": "tarfile.open(fileobj=io.BytesIO(), mode='w').close()",
    "hashlib": "hashlib.sha256(b'x').hexdigest()",
    "hmac": "hmac.new(b'k', b'm', 'sha256').hexdigest()",
    "uuid": "uuid.uuid5(uuid.NAMESPACE_DNS, 'a.b')",
    "random": "random.Random(1).random()",
    "secrets": "secrets.token_hex(4)",
    "inspect": "inspect.signature(lambda a: a)",
    "ast": "ast.parse('x = 1')",
    "bs4": "bs4.BeautifulSoup('<b>x</b>', 'html.parser').text",
    "markdownify": "markdownify.markdownify('<b>x</b>')",
}


def test_every_admitted_name_imports_and_works():
    app = load_kernel(lambda name, payload: {})
    untested = app._ALLOWED - set(USES) - {"soupsieve"}
    assert not untested, f"table entries without a use line: {untested}"
    for mod, use in USES.items():
        top = mod.split(".")[0]
        src = "import io\n" + f"import {mod}\n"
        if mod == "xml.etree":
            src += "import xml.etree.ElementTree\n"
        src += f"out = {use}\n"
        ns = guest_exec(src)
        assert top in ns and "out" in ns, mod


def test_dotted_entries_admit_the_submodule_only():
    ns = guest_exec("from urllib.parse import urlparse\nimport xml.etree.ElementTree as ET\n"
                    "u = urlparse('https://a.b/c').netloc\nt = ET.fromstring('<a/>').tag\n")
    assert ns["u"] == "a.b" and ns["t"] == "a"
    with pytest.raises(ImportError, match="http.get"):
        guest_exec("import urllib.request\n")
    with pytest.raises(ImportError, match="http.get"):
        guest_exec("from urllib.request import urlopen\n")
    with pytest.raises(ImportError, match="effect boundary"):
        guest_exec("import xml.sax\n")


def test_module_internal_imports_use_the_real_importer():
    # tarfile imports shutil at module level; shutil is refused to CELL
    # code — the gate is the cell namespace's __import__, not a finder
    ns = guest_exec("import io\nimport tarfile\n"
                    "ok = tarfile.open(fileobj=io.BytesIO(), mode='w') is not None\n")
    assert ns["ok"]
    with pytest.raises(ImportError, match="ADR-024"):
        guest_exec("import shutil\n")


REFUSED = {
    "pathlib": "ADR-024", "shutil": "ADR-024", "glob": "ADR-024", "os.path": "ADR-024",
    "socket": "http", "select": "http", "subprocess": "sh.run", "threading": "http.get_many",
    "multiprocessing": "http.get_many", "asyncio": "http.get_many", "signal": "interrupt",
    "urllib.request": "http.get", "http.client": "http.get", "ftplib": "http",
    "smtplib": "connector",
    "pickle": "json",
}


@pytest.mark.parametrize("mod,pointer", sorted(REFUSED.items()))
def test_refused_names_point_at_the_capability(mod, pointer):
    with pytest.raises(ImportError, match=pointer):
        guest_exec(f"import {mod}\n")


@pytest.mark.parametrize("mod", ["bz2", "lzma", "ssl", "ctypes"])
def test_not_in_image_says_so(mod):
    with pytest.raises(ImportError, match="not compiled into the kernel image"):
        guest_exec(f"import {mod}\n")


def test_io_proxy_has_streams_not_openers():
    ns = guest_exec("import io\nb = io.BytesIO(b'xy').read()\ns = io.StringIO('t').read()\n"
                    "has_open = hasattr(io, 'open') or hasattr(io, 'open_code') "
                    "or hasattr(io, 'FileIO')\n")
    assert ns["b"] == b"xy" and ns["s"] == "t" and not ns["has_open"]
    with pytest.raises(ImportError, match="proxied"):
        guest_exec("import io.something\n")


def test_sqlite3_proxy_is_memory_only():
    ns = guest_exec("import sqlite3\ncon = sqlite3.connect(':memory:')\n"
                    "con.execute('create table t(x)')\n"
                    "con.execute('insert into t values (1),(2)')\n"
                    "n = con.execute('select sum(x) from t').fetchone()[0]\n"
                    "has_conn = hasattr(sqlite3, 'Connection')\nrow = sqlite3.Row\n")
    assert ns["n"] == 3 and not ns["has_conn"]
    with pytest.raises(Exception, match="only ':memory:'"):
        guest_exec("import sqlite3\nsqlite3.connect('x.db')\n")
    with pytest.raises(Exception, match="only ':memory:'"):
        guest_exec("import sqlite3\nsqlite3.connect('file::memory:?cache=shared', uri=True)\n")


def test_mimetypes_proxy_is_the_builtin_table():
    ns = guest_exec("import mimetypes\nt = mimetypes.guess_type('photo.png')[0]\n"
                    "e = mimetypes.guess_extension('application/pdf')\n"
                    "has_init = hasattr(mimetypes, 'init')\n")
    assert ns["t"] == "image/png" and ns["e"] == ".pdf" and not ns["has_init"]


def test_os_proxy_exposes_fspath_and_pathlike():
    ns = guest_exec("import os\np = os.fspath('a/b')\nk = os.PathLike\n")
    assert ns["p"] == "a/b"
    with pytest.raises(ImportError, match="ADR-024"):
        guest_exec("import os.path\n")
