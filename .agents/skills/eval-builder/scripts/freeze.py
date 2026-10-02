#!/usr/bin/env python3
"""Freeze an eval, or verify a frozen one.

Usage
  freeze.py --task-dir DIR --engineer "Name"      write approval.json
  freeze.py --task-dir DIR --verify               exit 1 if anything frozen changed

Freezing checks the folder against the contract in contract.py, lints the hidden
test for source or git inspection, runs leak_check.py again on the final prompt
and hidden test and refuses on a hard flag. It also refuses unless gates.json
holds the engineer's yes at all three gates (suitability, proof, freeze), recorded
with gate.py. --one-shot REASON skips the gate check and records the reason in
approval.json for Cognition to see. A refusal writes rejected.json with the
reasons. approval.json holds the engineer name, the date, the build path and the
sha256 of every frozen artifact, so a folder built on a laptop and one built in a
Devin cloud session look the same to the evals command.
"""
import argparse
import base64
import binascii
import datetime as dt
import hashlib
import json
import os
import platform
import posixpath
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contract import GATES, check_folder, gates_problems, hash_all as hashes  # noqa: E402

LEAK_CHECK = Path(__file__).resolve().with_name("leak_check.py")

# Hidden tests must drive behaviour. These patterns mean the test reads the source or the git state instead.
# Pattern based, so a determined author can get past it, the engineer reading the test at the proof gate is
# the real check. It resolves the cheap disguises first (a path held in a variable, quotes or braces inside a
# path, a process substitution) and leaves the test's own files, its temp folder and the flags before a path
# alone. A flagged line the engineer judges fine ends with "# lint ok, <their reason>", freeze then records
# the line and the reason in approval.json instead of refusing.
SRC_EXT = r"(?i:py|pyi|js|mjs|cjs|ts|tsx|jsx|go|rs|java|kt|rb|php|c|cc|cpp|h|hpp|cs|swift|scala|ex|exs|erl|hs|lua|dart|vue|svelte)"  # case folded, a Mac reads CALC/__INIT__.PY
READERS = (r"(?:cat|grep|egrep|fgrep|rg|ag|ack|sed|awk|head|tail|less|more|diff|cmp|strings|wc|nl|tac|od|xxd|bat|base64|hexdump|uuencode|zcat|"
           r"tr|rev|sort|uniq|cut|fold|paste|md5|md5sum|shasum|sha1sum|sha256sum|sha512sum|b2sum|cksum|sum|openssl|iconv|expand|column|shuf|stat|file|dd)")
READ_CALLS = (r"(?:open|openSync|createReadStream|io\.open|codecs\.open|tokenize\.open|fileinput\.input|linecache\.getlines?|read_text|read_bytes|readFileSync|readFile|"
              r"read_to_string|ReadFile|File\.read|File\.readlines|File\.open|File\.foreach|IO\.read|IO\.readlines|file_get_contents|io\.lines|readTextFile)")
PATH_BUILDERS = r"(?:Path|PurePath|pathlib\.Path|os\.path\.join|path\.join|path\.resolve)"
OUTSIDE = r"(?:HIDDENTESTFILE(?![\w.])|\$\{?(?:TMP\w*|HOME|EVAL_TASK_DIR|HIDDEN\w*)\}?|/tmp/|/private/tmp/|~/)"  # the test's own space, not the repository
TREE = r"(?:\.|\./|\$\{?EVAL_REPO\}?/?|\$PWD/?|\$\(pwd\)/?)"
GIT_OPTS = r"(?:-C\s+\S+\s+|-c\s+\S+\s+|--git-dir(?:=\S+|\s+\S+)\s+|--work-tree(?:=\S+|\s+\S+)\s+|--[\w-]+(?:=\S+)?\s+|-[A-Za-z]+\s+)*"
GIT_CMDS = (r"(?:grep|log|diff|show|blame|status|rev-parse|describe|branch|ls-files|ls-tree|cat-file|rev-list|reflog|stash|checkout|switch|"
            r"reset|restore|tag|fetch|pull|clone|init|add|commit|merge|rebase|cherry-pick|worktree|archive|bundle|notes|whatchanged|shortlog|"
            r"for-each-ref|name-rev|show-ref|symbolic-ref|update-index|read-tree|write-tree|hash-object|config|remote|submodule|bisect|range-diff|format-patch)")
END = r"\s*(?:$|[|;&)>])"
WRAPPERS = ("sudo", "env", "command", "exec", "eval", "bash", "sh", "zsh", "dash", "ksh", "time", "nice", "nohup", "timeout", "xargs",
            "if", "elif", "while", "until", "then", "else", "do", "!", "{")
SRC_FILE = re.compile(rf"\.{SRC_EXT}$")
TESTDIR = (r"(?:\./|\$\{?EVAL_REPO\}?/)?(?:[\w.-]+/)*?(?i:tests?|__tests__|__fixtures__|__snapshots__|__mocks__|specs?|fixtures?|testdata|test[-_]data|mocks|snapshots|"
           r"dist|build|out|_site|public|coverage|htmlcov|target|node_modules|\.venv|venv|\.next)/")  # test material and build output anywhere in the tree, fair to read
TESTDIR_RX = re.compile(TESTDIR)
# underscore names that are documented public API, namedtuple, Enum, Django models, node-mocks-http
PUBLIC_UNDERSCORE = r"(?:asdict|replace|fields|field_defaults|make|value_|name_|missing_|meta|state|default_manager|base_manager|get[A-Z]\w*|is[A-Z]\w*|id)"
ACK = re.compile(r"(#|//)\s*lint ok\b[,:]?\s*(.*?)\s*$")
HASH_COMMENT = ("", ".sh", ".bash", ".zsh", ".py", ".rb", ".pl", ".pm", ".ps1", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".mk")
SLASH_COMMENT = (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".java", ".kt", ".kts", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs",
                 ".swift", ".scala", ".php", ".dart", ".groovy")
VAR_CAP = 24  # readings of one line kept when its names take more than one value
SHELL_SUFFIXES = ("", ".sh", ".bash", ".zsh", ".ksh")
# the test's own space written as an expression, the temp folder, an environment lookup of it, the folder beside the test,
# the home folder, an argument it was given
OWN_ENV = r"(?:TMP\w*|TEMP\w*|HOME|EVAL_TASK_DIR|HIDDEN\w*|RUNNER_TEMP|CI_TEMP\w*|XDG_(?:CACHE|RUNTIME|STATE)_HOME)"
OWN_DEFAULT = r"(?:\s*,\s*(?:\"\$TMP\"|[\"'](?:/tmp|/private/tmp|/var/tmp|~)[^\"'\n]*[\"']|None|null|undefined|\"\"|''))?"
OWN_SPACE = (r"(?:tempfile\.(?:mkdtemp|mkstemp|gettempdir|TemporaryDirectory|NamedTemporaryFile)\s*\((?:[^()\n]|\([^()\n]*\))*\)(?:\.name)?|fs\.mkdtemp(?:Sync)?\s*\((?:[^()\n]|\([^()\n]*\))*\)|"
             r"os\.tmpdir\s*\(\s*\)|os\.homedir\s*\(\s*\)|Path\.home\s*\(\s*\)|sys\.argv\s*\[\s*\d+\s*\]|process\.argv\s*\[\s*\d+\s*\]|__dirname\b|import\.meta\.dirname\b|"
             r"os\.path\.dirname\s*\(\s*(?:os\.path\.abspath\s*\(\s*)?__file__\s*\)?\s*\)|path\.dirname\s*\(\s*__filename\s*\)|"
             r"Path\s*\(\s*__file__\s*\)\s*\.(?:resolve\s*\(\s*\)\s*\.)?parent\b(?!\s*\.\s*parent)|"
             r"os\.path\.expanduser\s*\(\s*[\"']~[^\"'\n]*[\"']\s*\)|Path\s*\(\s*[\"']~[^\"'\n]*[\"']\s*\)\s*\.expanduser\s*\(\s*\)|"
             rf"(?<![\w.])(?:os\.)?environ(?:\.get)?\s*[\[(]\s*[\"']{OWN_ENV}[\"']{OWN_DEFAULT}\s*[\])]|(?<![\w.])(?:os\.)?getenv\s*\(\s*[\"']{OWN_ENV}[\"']{OWN_DEFAULT}\s*\)|"
             rf"process\.env(?:\.{OWN_ENV}\b|\[\s*[\"']{OWN_ENV}[\"']\s*\])|Deno\.env\.get\s*\(\s*[\"']{OWN_ENV}[\"']\s*\)|"
             r"(?<![\w.])(?:args|opts|options)\.(?:out|tmp|temp|dest|output|work|scratch|target)\w*\b)")
# a wrapper around the test's own space, a method that resolves it, a fallback chain between two own spaces
OWN_WRAP = re.compile(r"(?<![\w.])(?:Path|PurePath|PurePosixPath|pathlib\.Path|str|os\.fspath|os\.path\.(?:abspath|realpath|normpath|expanduser|expandvars)|path\.(?:resolve|normalize)|fs\.realpathSync)\s*\(\s*\"\$TMP\"\s*\)|"
                      r"\"\$TMP\"\s*\.\s*(?:resolve|expanduser|absolute)\s*\(\s*\)|"
                      r"\"\$TMP\"\s*(?:or|\|\||\?\?)\s*(?:\"\$TMP\"|[\"'](?:/tmp|/private/tmp|/var/tmp|~)[^\"'\n]*[\"'])")
REPO_EXPR = r"(?:os\.environ(?:\.get)?\s*[\[(]\s*[\"']EVAL_REPO[\"']\s*[\])]|process\.env\.EVAL_REPO\b)"


def own_space(code):
    """code with the repository root written as an expression folded to "$EVAL_REPO" and the test's own space
    folded to "$TMP", through the wrappers and fallbacks around it, Path(tempfile.mkdtemp()),
    os.environ.get("TMPDIR", "/tmp"), process.env.RUNNER_TEMP || os.tmpdir()."""
    code = re.sub(REPO_EXPR, '"$EVAL_REPO"', code)
    for _ in range(4):
        new = OWN_WRAP.sub('"$TMP"', re.sub(OWN_SPACE, '"$TMP"', code))
        if new == code:
            break
        code = new
    return code

class Fn:
    """A lint rule written as a function, with the search() shape of a compiled pattern."""

    def __init__(self, fn):
        self.fn = fn

    def search(self, text):
        return self.fn(text)


def bare(tok):
    return tok.strip("\"'()`").rsplit("/", 1)[-1]


def reads_with(is_src):
    """A READERS command given a source file. Reads the tokens of each command on the line, so the flags
    before a path, the test's own files and its temp folder do not count, and a path a pipe feeds to xargs does."""
    def fn(text):
        seen = False
        for seg in re.split(r"\|\||&&|[|;&]", text):
            toks = seg.split()
            xargs = False
            while toks and (re.match(r"[A-Za-z_]\w*=", toks[0]) or bare(toks[0]) in WRAPPERS):
                xargs = xargs or bare(toks[0]) == "xargs"
                toks = toks[1:]
                while toks and (toks[0].startswith("-") or toks[0].isdigit()):
                    toks = toks[1:]
            if not toks:
                continue
            args = [t.strip("\"'()`") for t in toks[1:]]
            args = [a.split("=", 1)[1] if "=" in a else a for a in args]
            srcs = [a for a in args if is_src(a) and not re.match(OUTSIDE, a)]
            if re.fullmatch(READERS, bare(toks[0])) and (srcs or (xargs and seen)):
                return True
            seen = seen or bool(srcs) or any(a.endswith("/*") and not re.match(OUTSIDE, a) for a in args)
        return False
    return Fn(fn)



# a copy, move or link whose source is the test's own file, where it lands in the tree is a fixture, not a source path built to read
COPY_FROM_OWN = re.compile(rf"\b(?:shutil\.(?:copy\w*|move)|fs\.(?:copyFile\w*|cp\w*|rename\w*)|copyFile\w*|cpSync|os\.(?:rename|replace|link|symlink))\s*\(\s*"
                           rf"(?:{PATH_BUILDERS}\s*\(\s*)?[\"'](?:{OUTSIDE})[^\"'\n]*[\"'](?:\s*,\s*[\"'][^\"'\n]*[\"'])*\s*\)?\s*,\s*(?:[^()\n]|\([^()\n]*\))*\)")


def builds_source_path(rx):
    """The rule for a source file path built from pieces, blind to the destination of a copy from the test's own space."""
    return Fn(lambda text: bool(rx.search(COPY_FROM_OWN.sub("copy_from_own_space()", text))))


BEHAVIOUR_LINT = [
    (re.compile(rf"(?<![\w.-])git\s+{GIT_OPTS}(?:apply|am)\b|\bpatch\s+-p\d"), "applies patches itself"),
    (re.compile(r"(?<![\w.])\w+=[\"']?(?:[\w./-]*/)?git[\"']?\s*(?:;|&&|$)|\$\((?:command\s+-v|which|type\s+-[pP])\s+git\s*\)"), "hides git in a variable"),
    (re.compile(rf"(?<![\w.-])(?:[\w./-]*/)?git\s+{GIT_OPTS}{GIT_CMDS}\b"), "runs git, a hidden test must not read or change git state"),
    (re.compile(r"[\"']git[\"']\s*,|\bgit\.(?:Repo|Git|cmd)\b|\bfrom\s+git\s+import\b|(?<![\w.])import\s+git\b|\bpygit2\b|\bdulwich\b|\bsimple-git\b|\bsimpleGit\b|\bisomorphic-git\b"),
     "runs git through a subprocess or a git library"),
    (re.compile(r"(?<![\w/-])HEAD(?:~\d*|\^+|@\{|:|\.\.)|\.\.HEAD\b"), "compares against HEAD"),
    (re.compile(r"(?<![\w-])(?<!exclude=)(?<!exclude )\.git(?:/|\\|[\"']|\s|$)"), "reads the .git folder"),
    (re.compile(rf"(?<![\w.])(?:grep|egrep|fgrep)\s+(?![^\n|;&]*\s[\"']?{OUTSIDE})(?:-[A-Za-z]*[rR][A-Za-z]*\b|--(?:dereference-)?recursive\b)"), "greps the repository source recursively"),
    (re.compile(rf"(?<![\w.])(?:grep|egrep|fgrep|rg|ag|ack)\s+(?:-\S+\s+)*[^\n|;&]*(?:\$EVAL_REPO|\$\{{EVAL_REPO\}}|\$PWD|\$\(pwd\)|\s\.{END}|\s\*{END})"),
     "greps the repository source"),
    (re.compile(rf"(?<![\w.])(?<!\|\s)(?<!\|)(?:grep|egrep|fgrep|rg|ag|ack)\s+(?:-\S+\s+)*(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s|;&\"']+)\s+(?![\"']?{OUTSIDE})(?![\"']?{TESTDIR})\S*/{END}"),
     "greps the repository source, a folder argument"),
    (re.compile(rf"(?<![\w.])(?<!\|\s)(?<!\|)(?:rg|ag|ack)\s+(?!(?:-\S+\s+)*(?:--version|--help|-V|-h|--type-list|--files|--pcre2-version)\b)(?:-\S+\s+)*(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s|;&\"']+){END}"),
     "greps the repository source, no file argument means the whole tree"),
    (re.compile(rf"(?<![\w.])find\s+(?![\"']?{OUTSIDE})[^\n|;&]*-exec\w*\s+(?:cat|grep|sed|awk|head|tail|strings|sh|bash|zsh|python[23]?|node|perl|ruby)\b|(?<![\w.])find\s+(?![\"']?{OUTSIDE})[^\n|;&]*\|\s*xargs\s+(?:-\S+\s+)*(?:cat|grep|sed|awk|head|tail|strings|sh|bash)\b"),
     "reads source files through find"),
    (re.compile(r"\bast\.(?:parse|walk|dump)\b|\binspect\.(?:getsource|getsourcefile|getsourcelines|getfile|getmodule|getcomments|findsource|getabsfile|getblock|getclosurevars)\b|\.__file__\b|[\"']__file__[\"']|\.__spec__\b|\.__loader__\b|\.__path__\b|\.__cached__\b|"
                r"\bimportlib\.resources\b|\bpkgutil\.get_data\b|\bpkg_resources\.resource_\w+\b|\b__code__\b|\.co_consts\b|\bdis\.(?:dis|get_instructions)\b|\bfind_spec\s*\([^)]*\)\s*\.(?:origin|submodule_search_locations)\b|"
                r"(?<![\w.])-m\s*(?:inspect|dis|ast|tokenize|pydoc|pyclbr|modulefinder|symtable|trace|pdb|cProfile|profile|tabnanny)\b|(?<![\w.])pydoc3?\s+[A-Za-z_]|\bgetattr\s*\([^)]*[\"']__(?:file|spec|code|loader|path)__[\"']|\bvars\s*\(\s*\w+\s*\)\s*\[\s*[\"']__file__|"
                r"\.get_source\s*\(|\.get_code\s*\(|\bget_loader\s*\(|\.source_location\b|\binstance_method\s*\(\s*:\w+\s*\)\.source\b"),
     "inspects the source structure"),
    (re.compile(rf"(?:\bbase64\s+(?:-d|-D|--decode)\b|\bxxd\s+-r|\bopenssl\s+(?:enc|base64)\b|\\x[0-9a-f]{{2}})[^\n]*\|\s*(?:(?:sh|bash|zsh|dash|ksh|source|\.|eval)\b|(?:python3?|perl|node|ruby)(?:\s+-)?{END})|"
                r"(?<![\w.])eval\s+[^\n]*(?:base64|xxd\s+-r|openssl\s+enc|\\x[0-9a-f]{2})|\bexec\s*\([^\n]*(?:b64decode|fromhex|zlib\.decompress|marshal\.loads|codecs\.decode|sys\.stdin)"),
     "runs decoded text, the test must be readable as written"),
    (re.compile(r"\b(?:readFileSync|readFile|createReadStream|openSync|open|readTextFile)\s*\(\s*(?:await\s+)?(?:require\.resolve|import\.meta\.resolve|createRequire\([^)]*\)\.resolve)\s*\("),
     "reads a module's source through require.resolve"),
    (re.compile(r"\b(?:require|import)\s*\(\s*[\"'][^\"'\n]+[\"']\s*\)\s*\)?(?:\.[\w$]+)*\s*(?:\.\s*(?:toString|toLocaleString)\s*\(|\[\s*[\"']toString[\"']\s*\]|\+\s*(?:''|\"\"|``))|"
                r"\bString\s*\(\s*(?:await\s+)?(?:require|import)\s*\(\s*[\"'][^\"'\n]+[\"']\s*\)\s*\)?(?:\.[\w$]+)*\s*\)|\$\{\s*(?:await\s+)?(?:require|import)\s*\(\s*[\"'][^\"'\n]+[\"']\s*\)\s*\)?(?:\.[\w$]+)*\s*\}|"
                r"(?:''|\"\"|``)\s*(?:\+|\.\s*concat\s*\()\s*(?:await\s+)?(?:require|import)\s*\(\s*[\"'][^\"'\n]+[\"']\s*\)\s*\)?(?:\.[\w$]+)+|"
                r"\bFunction\.prototype\.toString\b|(?<!Object\.prototype)(?<!Object\.prototype\s)\.toString\s*\.\s*(?:call|apply|bind)\s*\(|\brequire\.cache\b|\bmodule\.children\b"),
     "reads a function's source through toString"),
    (re.compile(rf"\b__(?:closure|globals|wrapped|func)__\b|\bpyclbr\.\w+\s*\(|\b(?:py_compile|compileall)\.compile\w*\s*\(|\bspec_from_file_location\s*\([^)\n]*[\"'](?!{OUTSIDE})[^\"'\n]*\.{SRC_EXT}[\"']"),
     "inspects the source structure"),
    (re.compile(rf"(?<![\w.])(?:python[23]?|node|ruby|perl|deno|bun)\s+(?:-\S+\s+)*(?:-[ce]\s+(?=[\"'][^\n]*?(?:\bopen\b|read|\bPath\b|\bfs\b|\bFile\b|\bIO\b|slurp|\bcat\b|\bload\w*|importlib|__file__|inspect|source|\bexec\b|compile|getattr|tokenize|linecache))"
                rf"(?:\"(?:[^\"\n]|\"\$TMP\")*\"|'(?:[^'\n]|'\$TMP')*')|-(?=\s))\s+[^\n|;&]*(?<![\w.$/-])(?![\"']?{OUTSIDE})(?!{TESTDIR})[\w./-]*\.{SRC_EXT}(?![\w.])"),
     "passes a source file to an inline program"),
    (re.compile(rf"<\(\s*(?!(?:python[23]?|node|ruby|perl|deno|bun|php|bash|sh|zsh)\s+(?:-[A-Za-z]\S*\s+)*[\w./-]*\.{SRC_EXT}(?![\w.]))[^()\n]*(?<![\w.$/-])(?!{OUTSIDE})(?!{TESTDIR})[\w./-]*\.{SRC_EXT}(?![\w.])"),
     "feeds a source file through a process substitution"),
    (re.compile(rf"\bfile://[^\s\"'\n]*\.{SRC_EXT}(?![\w.])"), "reads a source file through a file URL"),
    (re.compile(rf"(?<![\w.])-m\s+(?:http\.server|SimpleHTTPServer)\b(?![^\n|;&]*(?:-d|--directory)[= ]+[\"']?(?:{OUTSIDE}|dist|build|public|out|_site|site|www)\b)"),
     "serves the repository over HTTP"),
    (re.compile(rf"(?<![\w.])php\s+-S\s+\S+(?![^\n|;&]*\s-t\s+[\"']?(?:{OUTSIDE}|dist|build|public|out|_site|site|www|web|html)\b)|"
                rf"(?:(?<![\w.])npx\s+(?:-y\s+|--yes\s+)?(?:serve|http-server|sirv(?:-cli)?|live-server|static-server|superstatic)|"
                rf"(?<![\w.-])(?:http-server|live-server|static-server|superstatic|miniserve|darkhttpd|webfsd|caddy\s+file-server|busybox\s+httpd|ruby\s+-run\s+-e\s+httpd))(?![\w-])"
                rf"(?![^\n|;&]*\s[\"']?(?:{OUTSIDE}|dist|build|public|out|_site|site|www|storybook-static|htmlcov|coverage)\b)|"
                r"\bSimpleHTTPRequestHandler\b|\bhttp\.server\s+import\s+test\b|\bhttp\.server\.test\s*\("),
     "serves the repository over HTTP"),
    (re.compile(r"(?<![\w.])cpio\s+(?:-\S+\s+)*(?:-[A-Za-z]*o\b|--create\b)|(?<![\w.])pax\s+(?:-\S+\s+)*-[A-Za-z]*w\b|(?<![\w.])(?:tar|bsdtar|gtar)\s+[^\n|;&]*(?:-T\s*-|--files-from[= ]*-)(?![\w-])|"
                r"(?<![\w.])zip\s+[^\n|;&]*\s-@(?![\w-])|(?<![\w.])shar\s"),
     "archives files read from a list, the test must name what it reads"),
    (re.compile(r"(?<![\w.])coverage\s+(?:html|annotate)\b|--cov-report[= ]*(?:html|annotate)\b|\bcoverage\.(?:html_report|annotate)\s*\(|(?<![\w.])(?:nyc|c8)\s+[^\n|;&]*--reporter[= ]*html\b"),
     "copies the source into a coverage report"),
    (re.compile(r"\b(?:os\.walk|os\.listdir|os\.scandir|readdirSync|readdir|opendir|listdir|scandir|walkdir)\s*\(\s*[\"'`](?:\.|\./|\$EVAL_REPO/?|\$\{EVAL_REPO\}/?)[\"'`]\s*[,)]|"
                r"\b(?:Path|PurePath|pathlib\.Path)\s*\(\s*(?:[\"']\.?/?[\"']\s*)?\)\s*\.\s*(?:rglob|glob|walk)\s*\(|\bPath\.cwd\s*\(\s*\)\s*\.\s*(?:rglob|glob|walk)\s*\(|\b(?:glob\.glob|glob|globSync|fg|fastGlob|globby)\s*\(\s*[\"'`]\*\*/"),
     "lists the whole repository tree"),
    (re.compile(rf"^(?=[^\n]*\b(?:zipfile|tarfile|ZipFile|TarFile)\b)[^\n]*\.(?:add|write|writepy)\s*\(\s*[\"'](?!{OUTSIDE})(?!{TESTDIR})[^\"'\n]*\.{SRC_EXT}[\"']"), "archives a source file"),
    (re.compile(rf"(?<![\w.])from\s+[\w.]+\s+import\s+(?:[^#\n]*,\s*)?\(?\s*_[a-z]\w*|(?<![\w.$])import\s*\{{[^}}\n]*\b_[a-z][\w$]*|(?<!os)(?<!sys)(?<!self)(?<!this)(?<!cls)(?<=[\w$)\]])\._(?!{PUBLIC_UNDERSCORE}(?![\w$]))[a-z]\w*|"
                rf"\b(?:getattr|hasattr)\s*\(\s*(?!(?:os|sys|builtins|self|this|cls)\s*,)[\w.]+\s*,\s*[\"']_(?!{PUBLIC_UNDERSCORE}(?![\w$\"']))[a-z]|\b(?:vars\s*\(\s*[\w.]+\s*\)|\.__dict__)\s*\[\s*[\"']_[a-z]"),
     "touches a private name, a leading underscore marks it as not part of the interface"),
    (reads_with(lambda a: bool(SRC_FILE.search(a)) and not TESTDIR_RX.match(a)), "reads or edits a source file"),
    (re.compile(rf"(?<![<>A-Za-z_])<(?![<(])\s*[\"']?(?!{OUTSIDE})(?!{TESTDIR})[^\s\"'<>|;&()]*\.{SRC_EXT}(?![\w.])"), "reads a source file through a redirect"),
    (re.compile(rf"\b{READ_CALLS}\s*\(\s*(?!{PATH_BUILDERS}\s*\(\s*[\"']?{OUTSIDE})[^\n)]*[\"'`](?!{OUTSIDE})(?!{TESTDIR})[^\"'`\n]*\.{SRC_EXT}[\"'`]|(?<![\w.])perl\s+-[A-Za-z]*[npi][A-Za-z]*\s+[^\n|;&]*(?<![\w.$/-])(?!{OUTSIDE})(?!{TESTDIR})[\w./-]*\.{SRC_EXT}(?![\w.])"),
     "reads a source file"),
    (re.compile(rf"[\"'](?:cat|head|tail|grep|sed|awk|less|more|strings|od|xxd|base64|diff|cmp|sort|wc|nl|tac)[\"']\s*,\s*(?:[\"'][^\"'\n]*[\"']\s*,\s*)*[\"'](?!{OUTSIDE})(?!{TESTDIR})[^\"'\n]*\.{SRC_EXT}[\"']"),
     "reads a source file through a subprocess"),
    (builds_source_path(re.compile(rf"\b(?:(?:Path|PurePath|pathlib\.Path)\s*\(\s*(?:[\"'](?!{OUTSIDE})[^\"'\n]*[\"']\s*(?:,\s*[\"'][^\"'\n]*[\"']\s*)*|os\.getcwd\s*\(\s*\)\s*|)\)|\bPath\.cwd\s*\(\s*\))(?:\s*/\s*[\"'][^\"'\n]*[\"'])*\s*/\s*[\"'][^\"'\n]*\.{SRC_EXT}[\"']|"
                rf"\b(?:Path|PurePath|pathlib\.Path)\s*\(\s*__file__\s*\)(?:\.\w+(?:\[[^\]\n]*\])?)*(?:\.parents\s*\[[^\]\n]*\]|\.parent\s*\.\s*parent\b)(?:\.\w+(?:\[[^\]\n]*\])?)*(?:\s*/\s*[\"'][^\"'\n]*[\"'])*\s*/\s*[\"'][^\"'\n]*\.{SRC_EXT}[\"']|"
                rf"\.joinpath\s*\([^\n)]*[\"'][^\"'\n]*\.{SRC_EXT}[\"']|\b{PATH_BUILDERS}\s*\(\s*(?![A-Za-z_]\w*\s*[,)])(?![\"']?{OUTSIDE})[^\n)]*[\"'](?!{OUTSIDE})[^\"'\n]*\.{SRC_EXT}[\"']")),
     "builds a source file path"),
    (re.compile(rf"(?<![\w.]){READERS}\s+(?:-\S+\s+)*(?![\"']?{OUTSIDE})[^\s|;&]+/\*{END}"), "reads a folder of the repository"),
    (re.compile(rf"(?<![\w.])ln\s+-s\w*\s+(?![^\n|;&]*\$\{{?EVAL_REPO\}}?/(?:node_modules|\.venv|venv|vendor|\.cache|\.tox|target|dist|build)\b)"
                rf"(?:[^\n|;&]*(?:\$EVAL_REPO|\$\{{EVAL_REPO\}}|\$PWD|\$\(pwd\))|\S+\s+[\"']?{OUTSIDE})"), "links the repository into another path"),
    (re.compile(rf"(?<![\w.])(?:cp|mv|install|rsync|scp|ditto|ln)\s+(?:-\S+\s+)*(?![\"']?{OUTSIDE})(?![\"']?{TESTDIR})[\"']?[^\s|;&\"']*\.{SRC_EXT}[\"']?\s+"
                rf"(?:[\"']?(?:{OUTSIDE}|/|~|{TESTDIR})|(?![\"']?\S*(?:\.{SRC_EXT}|/)[\"']?(?:\s|$|[;&|)]))[\"']?\S+)|"
                rf"\b(?:shutil\.(?:copy\w*|move)|fs\.(?:copyFile\w*|cp\w*|link\w*|symlink\w*|rename\w*)|copyFile\w*|cpSync)\s*\(\s*[\"'](?!{OUTSIDE})(?!{TESTDIR})[^\"'\n]*\.{SRC_EXT}[\"']"),
     "copies a source file somewhere it can be read"),
    (re.compile(rf"(?<![\w.])(?:cp|rsync|scp|ditto)\s+[^\n|;&]*(?<![\w.$/-]){TREE}(?=[\s\"'])[^\n|;&]*\s[\"']?{OUTSIDE}|"
                rf"(?<![\w.])(?:(?:tar|bsdtar|gtar)\s+(?:-?[A-Za-z]*c[A-Za-z]*|--create)\b|zip|7za?\s+a)\s+[^\n]*(?<![\w.$/-]){TREE}(?=\s|$|[\"'|;&)])"),
     "copies the repository tree somewhere it can be read"),
    (re.compile(rf"\b(?:glob\.glob|glob|rglob|iglob)\s*\(\s*(?![\"']?{TESTDIR})[^\n)]*\.{SRC_EXT}\b"), "globs for source files"),
    (re.compile(rf"(?<![\w.])(?:(?:tar|bsdtar|gtar)\s+(?:-?[A-Za-z]*c[A-Za-z]*|--create)\b|zip|7za?\s+a)\s+[^\n|;&]*(?<![\w.$/-])(?![\"']?{OUTSIDE})(?!{TESTDIR})[\w./-]*\.{SRC_EXT}(?![\w.])"),
     "archives a source file"),
    (re.compile(r"(?:\$\{?(?:TMP\w*|HOME|EVAL_TASK_DIR|HIDDEN\w*)\}?|HIDDENTESTFILE|/tmp|/private/tmp|~)(?:/[^\s/\"'|;&]+)*/\.\.(?=/|\$|[\s\"'|;&)]|$)"),
     "walks back out of the test's own folder with .."),
]


def pr_file_lint(task):
    """One rule per file the PR touched, and one for the folders they sit in. A hidden test that reads one of
    them, or copies its folder out of the tree, checks the fix rather than the behaviour."""
    try:
        pr = json.loads((task / "pr.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = pr.get("files") or pr.get("changed_files") or []
    pats, dirs = [], set()
    for f in raw:
        path = f if isinstance(f, str) else (f.get("path") or f.get("filename") or "")
        if "/" in path:
            dirs.add(path.split("/")[0])
        names = {n for n in (path, Path(path).name) if len(n) >= 4}
        if not names:
            continue
        esc = "|".join(re.escape(n) for n in sorted(names))
        literal = re.compile(rf"\b{READ_CALLS}\s*\(\s*[^\n)]*[\"'](?!{OUTSIDE})[^\"'\n]*(?i:{esc})[\"']")
        lower = {n.lower() for n in names}
        by_name = reads_with(lambda a, names=lower: a.lower() in names or any(a.lower().endswith("/" + n) for n in names))
        pats.append((Fn(lambda v, lit=literal, tok=by_name: bool(lit.search(v)) or tok.search(v)), f"reads {path}, a file the PR changed"))
        pats.append((re.compile(rf"(?:(?<![\w.])(?:python[23]?|node|ruby|perl|bash|sh|zsh)\s+(?:-\S+\s+)*(?:HIDDENTESTFILE|\$TMP/[\w./-]+)|(?:^|[;&|(]\s*)(?:HIDDENTESTFILE|\$TMP/[\w./-]+\.(?:sh|py|js|rb|pl)))"
                                 rf"\s+(?:-\S+\s+)*[^\n|;&]*(?<![\w/.-])(?:\./)?(?i:{esc})(?![\w.-])"),
                     f"hands {path}, a file the PR changed, to a helper"))
        pats.append((re.compile(rf"(?<![\w.])(?:cp|mv|install|ln|rsync|scp|ditto)\s+(?:-\S+\s+)*[\"']?(?:\./)?(?i:{esc})[\"']?\s+\S|"
                                 rf"\b(?:shutil\.(?:copy\w*|move)|fs\.(?:copyFile\w*|cp\w*|link\w*|symlink\w*|rename\w*)|copyFile\w*|cpSync)\s*\(\s*[\"'](?:\./)?(?i:{esc})[\"']"),
                     f"copies {path}, a file the PR changed, where it can be read"))
    if dirs:
        d = "(?i:" + "|".join(re.escape(x) for x in sorted(dirs)) + ")"
        pats.append((re.compile(rf"(?<![\w.])(?:cp|rsync|scp|ditto)\s+[^\n|;&]*(?<![\w.$/-])(?:\./|\$\{{?EVAL_REPO\}}?/)?(?:{d})/?(?=[\s\"'])[^\n|;&]*\s[\"']?(?:{OUTSIDE}|/|~)|"
                                 rf"(?<![\w.])(?:(?:tar|bsdtar|gtar)\s+(?:-?[A-Za-z]*c[A-Za-z]*|--create)\b|zip|7za?\s+a)\s+[^\n]*(?<![\w.$/-])(?:\./|\$\{{?EVAL_REPO\}}?/)?(?:{d})/?(?=\s|$|[\"'|;&)])|"
                                 rf"\bshutil\.copytree\s*\(\s*[\"'](?:\./)?(?:{d})/?[\"']"), "copies a folder of the repository somewhere it can be read"))
        pats.append((re.compile(rf"(?<![\w.]){READERS}\s+(?:-\S+\s+)*[\"']?(?:\./)?(?:{d})/[^\s|;&\"']*[?*\[][^\s|;&\"']*"), "reads files of a folder the PR changed by pattern"))
        pats.append((re.compile(rf"(?<![\w.]){READERS}\s+(?:-\S+\s+)*[\"']?(?:\./)?(?:{d})/[^\s|;&\"']*\$\{{?[A-Za-z_!#]\w*\}}?[^\s|;&\"']*"), "reads a file of a folder the PR changed, named through a variable"))
        pats.append((re.compile(rf"\b(?:os\.walk|os\.listdir|os\.scandir|readdirSync|readdir|opendir|listdir|scandir)\s*\(\s*[\"'](?:\./)?(?:{d})/?[\"']|"
                                 rf"\b(?:Path|PurePath|pathlib\.Path)\s*\(\s*[\"'](?:\./)?(?:{d})/?[\"']\s*\)\s*\.(?:iterdir|rglob|glob|walk)\b|"
                                 rf"\bmake_archive\s*\([^\n)]*[\"'](?:\./)?(?:{d})/?[\"']|\b(?:fs\.cpSync|fs\.cp|cpSync|copytree)\s*\(\s*[\"'](?:\./)?(?:{d})/?[\"']|"
                                 rf"(?<![\w.])-m\s+(?:zipfile|tarfile)\s+-c\s+\S+\s+[^\n|;&]*(?<![\w.$/-])(?:\./)?(?:{d})(?=\s|$|[/;&|)])|"
                                 rf"^(?=[^\n]*\b(?:zipfile|tarfile|ZipFile|TarFile)\b)[^\n]*\.(?:add|write)\s*\(\s*[\"'](?:\./)?(?:{d})/?[\"']"),
                     "lists or packs a folder the PR changed"))
    return pats


def strip_comment(line, suffix):
    code = re.sub(r"(?:#|//)\s*lint ok\b.*$", "", line)
    return re.sub(r"(^|\s)#.*", "", code) if suffix in ("", ".sh", ".bash", ".py") else code


def read_value(code, i):
    """The value a shell assignment gives, starting at code[i], a quoted string or a $( ) kept whole."""
    if i < len(code) and code[i] in "\"'":
        j = code.find(code[i], i + 1)
        return code[i + 1:j] if j > 0 else code[i + 1:]
    if code.startswith("`", i):
        j = code.find("`", i + 1)
        return code[i:j + 1] if j > 0 else code[i:]
    if code.startswith("$(", i) or code.startswith("(", i):
        depth = 0
        for j in range(i, len(code)):
            if code[j] == "(":
                depth += 1
            elif code[j] == ")":
                depth -= 1
                if depth == 0:
                    rest = re.match(r"[^\s;&|)]*", code[j + 1:]).group(0) if code[i] == "$" else ""
                    return code[i:j + 1] + rest if code[i] == "$" else code[i + 1:j]
        return code[i:] if code[i] == "$" else code[i + 1:]
    return re.match(r"[^\s;&|)]*", code[i:]).group(0)


def shell_words(text):
    """text split into words the way a shell would, quotes and $( ) kept whole, stopping at ; | & or a newline."""
    words, cur, depth, quote = [], "", 0, None
    for ch in text:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            cur += ch
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth <= 0 and ch in ";|&\n":
            break
        if depth <= 0 and ch in " \t":
            if cur:
                words.append(cur)
            cur = ""
            continue
        cur += ch
    if cur:
        words.append(cur)
    return words


def decode_literal(text, how):
    """What a literal becomes through base64 -d, rev or xxd -r -p, or None when that is not printable text."""
    try:
        if how == "rev":
            out = text[::-1]
        elif how == "hex":
            out = bytes.fromhex(re.sub(r"\s", "", text)).decode("utf-8")
        else:
            out = base64.b64decode(text + "==", validate=False).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    return out if out.isprintable() or out.strip() == out.strip("\n") else None


def fold_commands(text):
    """$(echo x), $(printf '%s' x), $(dirname x), $(basename x), $(realpath x) over a literal x, and a literal
    decoded with base64 -d, xxd -r -p or reversed with rev, folded to what they print. $(dirname "$0") and
    friends keep their variable and stay the test's own space."""
    def decoded(m):
        how = "rev" if m.group(3).startswith("rev") else "hex" if m.group(3).startswith("xxd") else "b64"
        out = decode_literal(m.group(2), how)
        return out if out is not None else m.group(0)

    def filled(m):
        out = m.group(2)
        for arg in m.group(3).split():
            out = out.replace("%s", arg.strip("\"'"), 1)
        return out

    text = re.sub(r"\$\(\s*(?:which|command\s+-v|type\s+-[pP])\s+([\w./-]+)\s*\)", r"\1", text)
    text = re.sub(r"\$\(\s*(?:echo|printf)\s+(?:-\S+\s+)?([\"']?)([^()|;&$`\n]*?)\1\s*\|\s*(rev|xxd\s+-r\s+-p|xxd\s+-p\s+-r|base64\s+(?:-d|-D|--decode))\s*\)", decoded, text)
    text = re.sub(r"\$\(\s*printf\s+(?:--\s+)?([\"'])([^\"'\n]*%s[^\"'\n]*)\1((?:\s+[^\s()|;&]+)+)\s*\)", filled, text)
    text = re.sub(r"\$\(\s*echo\s+(?:-[neE]+\s+)?([\"']?)([^()|;&$`\n]*?)\1\s*\)", r"\2", text)
    text = re.sub(r"\$\(\s*printf\s+(?:--\s+)?[\"']%s[\"']\s+([\"']?)([^()|;&$`\n]*?)\1\s*\)", r"\2", text)
    text = re.sub(r"\$\(\s*(dirname|basename|realpath|readlink\s+-f)\s+([\"']?)([^()|;&$`\s]+)\2\s*\)",
                  lambda m: posixpath.dirname(m.group(3)) if m.group(1) == "dirname" else posixpath.basename(m.group(3)) if m.group(1) == "basename" else m.group(3), text)
    return text


def classify(value):
    """outside, the test's own space. literal, a value worth substituting. unknown, leave the variable alone."""
    if re.match(OUTSIDE, value) or re.match(r"(?:\./)?hidden-test(?:/|$)", value) or re.search(r"\$\(\s*(?:mktemp|dirname|basename|realpath)\b|\$\{?BASH_SOURCE", value):
        return "outside"
    if "$(" in value or "`" in value:
        return "unknown"
    return "literal"


EXPANSION = re.compile(r"\$\{([A-Za-z_]\w*|\d+|[@*])(\[[^\]]*\]|:[-=+?]|[-=+?]|##?|%%?|//?|\^\^?|,,?)?([^{}]*)\}")
PLAIN = re.compile(r"\$([A-Za-z_]\w*|\d|[@*])")
INDIRECT = re.compile(r"\$\{!([A-Za-z_]\w*)\}")


def ack_digest(acks):
    """sha256 over the lint acknowledgements, written into approval.json so an edit to that list shows."""
    return hashlib.sha256(json.dumps(acks, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def unique(items):
    seen, out = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def substitute(text, shell):
    """Every reading of text with the shell names replaced by the values the file binds them to, one reading
    per combination when a name takes more than one value, through ${x}, ${!x}, $x, $1, an array element or
    the whole array, a default or alternate word, a literal prefix or suffix strip and a case change."""
    def values(m):
        if m.re is INDIRECT:
            vals = [v for n in shell.get(m.group(1), []) for v in shell.get(n, [])]
            return vals or None
        if m.re is PLAIN:
            return shell.get(m.group(1)) or None
        name, op, word = m.group(1), m.group(2) or "", m.group(3) or ""
        vals = shell.get(name)
        if not op or op.startswith("["):
            return vals or None
        kind = op.lstrip(":")
        if kind in ("-", "="):
            return vals or [word]
        if kind == "?":
            return vals or [""]
        if kind == "+":
            return [word] if vals else [""]
        if not vals:
            return None
        out = []
        for val in vals:
            if op in ("#", "##", "%", "%%") and word and not re.search(r"[*?\[$]", word):
                if op[0] == "%" and val.endswith(word):
                    val = val[:-len(word)]
                elif op[0] == "#" and val.startswith(word):
                    val = val[len(word):]
            elif op == ",,":
                val = val.lower()
            elif op == ",":
                val = val[:1].lower() + val[1:]
            elif op == "^^":
                val = val.upper()
            elif op == "^":
                val = val[:1].upper() + val[1:]
            out.append(val)
        return out

    def once(t):
        matches = sorted([m for rx in (INDIRECT, EXPANSION, PLAIN) for m in rx.finditer(t)], key=lambda m: (m.start(), -m.end()))
        for m in matches:
            vals = values(m)
            if vals is None:
                continue
            vals = [v for v in vals if v != m.group(0)]
            if vals:
                return [t[:m.start()] + v + t[m.end():] for v in vals]
        return None

    readings = [fold_commands(text)]
    for _ in range(6):
        new, changed = [], False
        for t in readings:
            step = once(t)
            if step is None:
                new.append(t)
            else:
                changed = True
                new.extend(fold_commands(x) for x in step)
        readings = unique(new)[:VAR_CAP]
        if not changed:
            break
    return readings


def fold_literals(code, python=False):
    """Adjacent string literals joined with +, a %s or {} filled from one literal, a literal list joined with a
    literal separator, and in Python two literals side by side, folded into the one string they make."""
    code = re.sub(r"(?<![\w$])[frbuFRBU]{1,2}(?=[\"'][^\"'\n]*[\"'])", "", code)
    for _ in range(3):
        code = re.sub(r"([\"'])([^\"'`\n]*)\1\s*\+\s*([\"'])([^\"'`\n]*)\3", r"\1\2\4\1", code)
        if python:
            code = re.sub(r"([\"'])([^\"'`$\n]*)\1\s+([\"'])(?![/$])([^\"'`$\n]*)\3", r"\1\2\4\1", code)
    code = re.sub(r"([\"'])([^\"'\n]*)%s([^\"'\n]*)\1\s*%\s*\(?\s*([\"'])([^\"'\n]*)\4\s*,?\s*\)?", r"\1\2\5\3\1", code)
    code = re.sub(r"([\"'])([^\"'\n]*)\{\}([^\"'\n]*)\1\s*\.format\(\s*([\"'])([^\"'\n]*)\4\s*\)", r"\1\2\5\3\1", code)
    code = re.sub(r"([\"'])([^\"'`\n]*)\1\s*\.\s*join\s*\(\s*\[\s*((?:[\"'][^\"'\n]*[\"']\s*,?\s*)+)\]\s*\)",
                  lambda m: m.group(1) + m.group(2).join(re.findall(r"[\"']([^\"'\n]*)[\"']", m.group(3))) + m.group(1), code)
    return re.sub(r"\[\s*((?:[\"'][^\"'\n]*[\"']\s*,?\s*)+)\]\s*\.\s*join\s*\(\s*([\"'])([^\"'\n]*)\2\s*\)",
                  lambda m: m.group(2) + m.group(3).join(re.findall(r"[\"']([^\"'\n]*)[\"']", m.group(1))) + m.group(2), code)


def fold_joins(code):
    """os.path.join and path.join over literals, and a literal / a literal, folded to the one path they make."""
    code = re.sub(r"\b(?:os\.path\.join|path\.join|path\.resolve)\s*\(\s*((?:[\"'][^\"'\n]*[\"']\s*,\s*)+[\"'][^\"'\n]*[\"'])\s*\)",
                  lambda m: '"' + "/".join(re.findall(r"[\"']([^\"'\n]*)[\"']", m.group(1))) + '"', code)
    code = re.sub(r"(?<=[/+,=\s(\[])\(\s*([\"'][^\"'\n]*[\"'])\s*\)", r"\1", code)
    for _ in range(4):
        code = re.sub(r"([\"'])([^\"'\n]*)\1\s*/\s*([\"'])([^\"'\n]*)\3", r"\1\2/\4\1", code)
    return code


def substitute_idents(text, idents):
    """Every reading of text with the Python or JavaScript names replaced by the literals the file binds them
    to, the quoted literal in an argument position and the bare text inside an f-string or template brace."""
    readings = [text]
    for name, vals in idents.items():
        if not re.search(rf"(?<![\w.$]){name}(?![\w$])", text):
            continue
        new = []
        for t in readings:
            for v in vals:
                bare = v[1:-1] if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0] else v
                u = re.sub(rf"\$?\{{\s*{name}\s*\}}", lambda m, b=bare: b, t)
                new.append(re.sub(rf"(?<=[(,\[\s{{]){name}(?=\s*(?:[),\]}}.;+%|]|$))", lambda m, v=v: v, u))
        readings = unique(new)[:VAR_CAP]
    return readings


def split_statements(code):
    """(statement, separator) pairs, code split at the ; and ;; between statements, quotes, brackets and $( )
    kept whole."""
    parts, cur, depth, quote, i = [], "", 0, None, 0
    while i < len(code):
        ch = code[i]
        if quote:
            cur += ch
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote, cur = ch, cur + ch
        elif ch in "([{":
            depth, cur = depth + 1, cur + ch
        elif ch in ")]}":
            depth, cur = max(0, depth - 1), cur + ch
        elif ch == ";" and depth == 0:
            sep = ";;" if code.startswith(";;", i) else ";"
            parts.append((cur, sep))
            cur, i = "", i + len(sep) - 1
        else:
            cur += ch
        i += 1
    parts.append((cur, ""))
    return parts


SHELL_BLOCKS = {"if": 1, "case": 1, "for": 1, "while": 1, "until": 1, "select": 1, "fi": -1, "esac": -1, "done": -1}
SHELL_COND = re.compile(r"&&|\|\||\||[()]|(?<![\w$-])(?:if|elif|while|until|then|else|do|case|function|time)(?![\w-])")
CODE_COND = re.compile(r"&&|\|\||\?|=>|(?<![\w.$])(?:if|elif|else|for|while|with|try|except|catch|case|switch|function|def|lambda)(?![\w$])")
BRANCH_HEAD = re.compile(r"\}?\s*(?:else|elif|except|finally|catch)\b|case\s|default\s*:")


class Bindings:
    """What the file's names hold, read statement by statement in file order, so a use sees the values that
    reach it and none that a later statement gives. A plain assignment at the top level of the file or of a
    function body replaces what a name held. One inside an if, a case, a loop body or a sibling branch, or
    after && or ||, adds to it. Shell NAME=value and NAME+=value, for NAME in, printf -v, read, mapfile and
    readarray, set --, a ${NAME:=word} default, the name a while read loop fills from its pipe, and Python or
    JavaScript names bound to a string literal, a list, a module file lookup or the test's own space. Values
    in the test's own space collapse to $TMP."""

    def __init__(self, suffix):
        self.shell_file = suffix in SHELL_SUFFIXES
        self.shell, self.idents, self.refs = {}, {}, {}
        self.depth, self.tick, self.where, self.headers = 0, 0, {}, []

    def replaces(self, name, cond, indent):
        if cond:
            return False
        if self.shell_file:
            return self.depth == 0
        prev = self.where.get(name)
        return prev is None or (indent <= prev[1] and not any(t > prev[0] and h < indent for t, h in self.headers))

    def put(self, table, name, values, cond, indent):
        if self.replaces(name, cond, indent):
            table[name] = list(values)
            self.where[name] = (self.tick, indent)
            return
        table.setdefault(name, [])
        for v in values:
            if v not in table[name]:
                table[name].append(v)
        self.where.setdefault(name, (self.tick, indent))

    def resolve(self, value, nested=0):
        """The values a shell assignment gives, the test's own space as $TMP, a command substitution reduced to
        the path or the listing inside it, None when it cannot be read."""
        out = []
        for v in substitute(value, self.shell)[:VAR_CAP]:
            kind = classify(v)
            if kind == "outside":
                out.append("$TMP")
            elif kind == "literal":
                out.append(v)
            else:
                paths = [t for t in re.findall(r"[^\s()|;&'\"`]+", v)
                         if ("/" in t or "*" in t or SRC_FILE.search(t)) and not re.match(r"https?://|-|\$", t)]
                listing = re.match(r"\$\(\s*(?:find|ls|fd|fdfind)\s+(?:-\S+\s+)*([\w./-]+)?", v)
                if paths and nested < 2:
                    inner = self.resolve(paths[0], nested + 1)
                    if inner is None:
                        return None
                    out.extend(inner)
                elif listing:
                    out.append((listing.group(1) or ".").rstrip("/") + "/*")
                else:
                    return None
        return out

    def bind(self, name, value, cond, indent):
        vals = self.resolve(value)
        if vals is None:
            if self.replaces(name, cond, indent):
                self.shell.pop(name, None)
            return
        vals = unique(v for v in vals if v not in ("$" + name, "${" + name + "}"))
        if not vals:
            return
        self.put(self.shell, name, vals, cond, indent)
        for r, t in list(self.refs.items()):
            if t == name and r != name and self.shell.get(r) != vals:
                self.put(self.shell, r, vals, cond, indent)

    def ident(self, name, value, cond, indent):
        self.put(self.idents, name, [value], cond, indent)

    def apply(self, seg, python, indent):
        """The bindings one statement makes, in the order they are written."""
        self.tick += 1
        code = fold_joins(own_space(fold_literals(seg, python=python)))
        icode = fold_joins(fold_literals(substitute_idents(code, self.idents)[0], python=python)) if self.idents else code

        def cond(text, pos):
            if self.shell_file:
                return self.depth > 0 or bool(SHELL_COND.search(text[:pos]))
            return bool(CODE_COND.search(text[:pos]))

        for m in re.finditer(r"(?<![-\w.$])([A-Za-z_]\w*)=(?!=)", code):
            self.bind(m.group(1), read_value(code, m.end()), cond(code, m.start()), indent)
        for m in re.finditer(r"\b(?:declare|local|typeset)\s+-[A-Za-z]*n[A-Za-z]*\s+([A-Za-z_]\w*)=([A-Za-z_]\w*)", code):
            self.refs[m.group(1)] = m.group(2)
            if m.group(2) in self.shell:
                self.put(self.shell, m.group(1), self.shell[m.group(2)], cond(code, m.start()), indent)
        for m in re.finditer(r"\bfor\s+([A-Za-z_]\w*)\s+in\s+", code):
            words = []
            for word in shell_words(code[m.end():]):
                if word == "do":
                    break
                words.append(word)
            vals = []
            for word in words:
                got = self.resolve(re.sub(r"[\"']", "", word))
                if got is None:
                    vals = None
                    break
                vals.extend(got)
            if vals is None:
                if self.replaces(m.group(1), cond(code, m.start()), indent):
                    self.shell.pop(m.group(1), None)
            elif vals:
                self.put(self.shell, m.group(1), unique(vals), cond(code, m.start()), indent)
        for m in re.finditer(r"(?<![-\w.$])([A-Za-z_]\w*)\+=", code):
            held = self.shell.get(m.group(1)) or [""]
            more = read_value(code, m.end())
            self.put(self.shell, m.group(1), unique((h + " " + more).strip() for h in held), cond(code, m.start()), indent)
        for m in re.finditer(r"\bprintf\s+-v\s+([A-Za-z_]\w*)\s+([^\n;&|]+)", code):
            self.bind(m.group(1), m.group(2).split()[-1].strip("\"'"), cond(code, m.start()), indent)
        for m in re.finditer(r"(?:\bIFS=(\S*)\s+)?\bread\s+(?:-\S+\s+)*((?:[A-Za-z_]\w*\s+)*[A-Za-z_]\w*)\s*<<<\s*", code):
            names = m.group(2).split()
            sep = (m.group(1) or "").strip("\"'") or None
            whole = read_value(code, m.end())
            parts = whole.split(sep, len(names) - 1) if sep else whole.split(None, len(names) - 1)
            for name, part in zip(names, parts):
                self.bind(name, part, cond(code, m.start()), indent)
        for m in re.finditer(r"\b(?:mapfile|readarray)\s+(?:-\S+\s+)*([A-Za-z_]\w*)\s*(?:(<<<)\s*|<\s*(?:<\(([^()\n]*)\)|(\S+)))", code):
            if m.group(2):
                self.bind(m.group(1), read_value(code, m.end()), cond(code, m.start()), indent)
            else:
                self.bind(m.group(1), "$(" + (m.group(3) or "cat " + m.group(4)) + ")", cond(code, m.start()), indent)
        for m in re.finditer(r"\bset\s+--\s+", code):
            words = [read_value(w, 0) for w in shell_words(code[m.end():])]
            for n, word in enumerate(words[:9], 1):
                self.bind(str(n), word, cond(code, m.start()), indent)
            if words:
                self.bind("@", " ".join(words), cond(code, m.start()), indent)
                self.bind("*", " ".join(words), cond(code, m.start()), indent)
        for m in re.finditer(r"\$\{([A-Za-z_]\w*):?=([^{}]*)\}", code):
            if m.group(1) not in self.shell:
                self.bind(m.group(1), m.group(2), cond(code, m.start()), indent)
        for m in re.finditer(r"([^|\n]+)\|\s*while\s+(?:IFS=\S*\s+)?read\s+(?:-\S+\s+)*([A-Za-z_]\w*)", code):
            self.bind(m.group(2), "$(" + m.group(1).strip() + ")", cond(code, m.start()), indent)
        for m in re.finditer(r"(?<![\w.$])(?:const\s+|let\s+|var\s+)?([A-Za-z_]\w*)\s*=\s*(\[[^\]\n]*\])\s*;?\s*$", icode):
            self.ident(m.group(1), m.group(2), cond(icode, m.start()), indent)
        for m in re.finditer(r"(?<![\w.$])([A-Za-z_]\w*)\s*\.\s*(?:push|append|extend|unshift)\s*\(([^()\n]*)\)", icode):
            name, args = m.group(1), m.group(2).strip()
            if self.idents.get(name) and self.idents[name][-1].startswith("["):
                old = self.idents[name][-1][1:-1].strip()
                args = args[1:-1].strip() if args.startswith("[") and args.endswith("]") else args
                self.idents[name][-1] = "[" + ", ".join(x for x in (old, args) if x) + "]"
        for m in re.finditer(r"(?<![\w.$])(?:const\s+|let\s+|var\s+)?([A-Za-z_]\w*)\s*=\s*(?!=)([\"'])([^\"'\n]*)\2", icode):
            self.ident(m.group(1), m.group(2) + m.group(3) + m.group(2), cond(icode, m.start()), indent)
        for m in re.finditer(r"(?<![\w.$])(?:const\s+|let\s+|var\s+)?([A-Za-z_]\w*)\s*=\s*(?:await\s+)?(?:require\.resolve|import\.meta\.resolve)\s*\(", code):
            self.ident(m.group(1), '"MODULE_FILE_FROM_REQUIRE_RESOLVE.js"', cond(code, m.start()), indent)
        for m in re.finditer(r"(?<![\w.$])([A-Za-z_]\w*)\s*=\s*(?:importlib\.util\.)?find_spec\s*\([^)]*\)\s*\.origin\b", code):
            self.ident(m.group(1), '"MODULE_FILE_FROM_FIND_SPEC.py"', cond(code, m.start()), indent)
        if self.shell_file:
            clean = re.sub(r"\"[^\"]*\"|'[^']*'|\$\([^()]*\)|\$\{[^{}]*\}", " ", seg)
            for w in re.findall(r"(?<![\w$-])(?:if|fi|case|esac|for|while|until|select|done)(?![\w-])", clean):
                self.depth = max(0, self.depth + SHELL_BLOCKS[w])

    def readings(self, code, group, python):
        """Every reading of one statement with its names replaced by what they hold at that point, after the
        bindings the statement itself makes before each use."""
        indent = len(group[0]) - len(group[0].lstrip())
        if not self.shell_file and BRANCH_HEAD.match(group[0].strip()):
            self.headers.append((self.tick + 1, indent))
        out = []
        for seg, sep in split_statements(code):
            self.apply(seg, python, indent)
            segs = unique(r for base in substitute(seg, self.shell) for r in substitute_idents(base, self.idents))[:VAR_CAP]
            out.append((segs, sep))
        n = max(len(s) for s, _ in out)
        return unique("".join(s[i % len(s)] + sep for s, sep in out) for i in range(n))[:VAR_CAP]


def imported_names(lines):
    """The names a JavaScript or Python test file imports or requires, the product's own functions among them,
    plus a plain alias of one and the parameter a dynamic import's then callback receives."""
    names = set()
    for line in lines:
        for m in re.finditer(r"\bimport\s*\{([^}]*)\}\s*from|\b(?:const|let|var)\s*\{([^}]*)\}\s*=\s*(?:await\s+)?(?:require|import)\s*\(|"
                             r"\bimport\s+(?:\*\s+as\s+)?([A-Za-z_$][\w$]*)\s+from|\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*\(?\s*(?:await\s+)?(?:require|import)\s*\(|"
                             r"\bfrom\s+[\w.]+\s+import\s+([^#\n]+)|^\s*import\s+([\w., ]+)", line):
            for g in m.groups():
                for part in re.split(r"[,\s]+", g or ""):
                    part = part.split(":")[-1].strip("{}() ")
                    if re.fullmatch(r"[A-Za-z_$][\w$.]*", part) and part not in ("as", "default"):
                        names.add(part.split(".")[0])
        if re.search(r"\b(?:require|import)\s*\(", line):
            for m in re.finditer(r"\.then\s*\(\s*(?:async\s+)?\(?\s*(?:\{([^}]*)\}|([A-Za-z_$][\w$]*))\s*\)?\s*=>", line):
                for part in re.split(r"[,\s]+", m.group(1) or m.group(2) or ""):
                    if re.fullmatch(r"[A-Za-z_$][\w$]*", part):
                        names.add(part)
    for _ in range(2):
        for line in lines:
            for seg in line.split(";"):
                m = re.match(r"\s*(?:const|let|var)?\s*([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)\s*$", seg)
                if m and m.group(2) in names:
                    names.add(m.group(1))
    return names


def file_lint(lines):
    """Rules that depend on the file itself. A function the file imported, read as text through toString or
    its Python and Ruby equivalents, is the source again, and so is a module file found through an alias of
    require.resolve."""
    rules = []
    if any("find_spec" in line or "spec_from_file_location" in line for line in lines):
        rules.append((re.compile(r"\.(?:origin|submodule_search_locations|cached|loader)\b"), "inspects the source structure"))
    aliases = {m.group(1) for line in lines for m in [re.match(r"\s*(?:const|let|var)?\s*([A-Za-z_$][\w$]*)\s*=\s*require\.resolve\s*;?\s*$", line)] if m}
    if aliases:
        alt = "|".join(re.escape(n) for n in sorted(aliases))
        rules.append((re.compile(rf"\b{READ_CALLS}\s*\(\s*(?:{alt})\s*\("), "reads a module's source through require.resolve"))
    names = imported_names(lines)
    if not names:
        return rules
    alt = "|".join(re.escape(n) for n in sorted(names))
    ts = {m.group(1) for line in lines for seg in line.split(";")
          for m in [re.match(r"\s*(?:const|let|var)?\s*([A-Za-z_$][\w$]*)\s*=\s*Function\.prototype\.toString\s*$", seg)] if m}
    tsalt = "|".join(re.escape(n) for n in sorted(ts)) or "(?!x)x"
    rules.append((re.compile(rf"(?<![\w.$])(?:{alt})(?:\.[\w$]+)*\s*\.\s*(?:toString|toLocaleString)\s*\(\s*\)|(?<![\w.$])(?:{alt})(?:\.[\w$]+)*\s*\[\s*[\"'](?:toString|toLocaleString)[\"']\s*\]|\bString\s*\(\s*(?:{alt})(?:\.[\w$]+)*\s*\)|"
                             rf"\bFunction\.prototype\.toString\b[^\n]*(?<![\w.$])(?:{alt})\b|\$\{{\s*(?:{alt})\s*\}}|(?<![\w.$])(?:{alt})\s*\+\s*(?:''|\"\"|``)|(?:''|\"\"|``)\s*\+\s*(?:{alt})(?![\w$(])|"
                             rf"\[\s*(?:{alt})\s*\]\s*\.\s*join\s*\(|(?:''|\"\"|``)\s*\.\s*concat\s*\(\s*(?:{alt})\b|(?<!Object\.prototype)\.toString\s*\.\s*(?:call|apply)\s*\(\s*(?:{alt})\b|(?<![\w.$])(?:{tsalt})\s*\.\s*(?:call|apply)\s*\(\s*(?:{alt})\b|"
                             rf"(?<![\w.$])(?:{alt})(?:\.[\w$]+)*\.(?:source_location|source)\b|\bmethod\s*\(\s*:\w+\s*\)\.source\b"),
                  "reads a function's source through toString"))
    return rules


SHELLS = r"(?:bash|sh|zsh|dash|ksh)"
TRIPLE = re.compile("\"\"\"|'''")


def variants_of(code, python=False):
    """The forms of a line the rules look at. The line with each process substitution folded to a token and
    that substitution's command on its own, the shell inside a subprocess string, a subprocess argument list
    or a here string on its own, the test's own files folded to a token, its own space written as an
    expression folded to $TMP, path joins over literals folded to one path, and for each form a copy without
    quotes and a copy with braces expanded."""
    code = TRIPLE.sub(lambda m: m.group(0)[0], code)
    code = re.sub(r"\$\{\s*([\"'])([^\"'`\n]*)\1\s*\}", r"\2", code)
    code = re.sub(r"\$\(\s*(?:which|command\s+-v|type\s+-[pP])\s+([\w./-]+)\s*\)", r"\1", code)
    code = own_space(code)
    code = re.sub(r"\b(?:require|import)\s*\(\s*(?:path\.(?:join|resolve)|os\.path\.join)\s*\([^()\n]*\)\s*\)", "require(MODULE)", code)
    code = re.sub(r"(?<![^\s;|&(])\\(?=[A-Za-z])", "", code)
    code = re.sub(r"(?<![\w.])(?:busybox|toybox)\s+(?=[a-z])", "", code)
    code = fold_literals(code, python=python)
    forms = [re.sub(r"<\([^()\n]*\)", "<PROCSUB>", code)] + re.findall(r"<\(([^()\n]*)\)", code)
    forms += ["<(" + inner + ")" for inner in re.findall(r"<\(([^()\n]*)\)", code)]
    forms += re.findall(r"\$\(([^()\n]*)\)", re.sub(r"`(\s*[A-Za-z_][\w.-]*(?:\s[^`\n]*)?)`", r"$(\1)", code))
    forms += [m.group(2) for m in re.finditer(r"\b(?:execSync|execFileSync|exec|spawnSync|spawn|system|popen|run|check_output|check_call|call|Popen|getoutput|getstatusoutput|"
                                             r"create_subprocess_shell|create_subprocess_exec|shell_exec|passthru|proc_open)"
                                             r"\s*\(\s*(?:[frbu]{1,2})?([\"'`])(.*?)\1", code)]
    forms += [m.group(1) + " " + " ".join(re.findall(r"[\"'`]([^\"'`\n]*)[\"'`]", m.group(2)))
              for m in re.finditer(r"\b(?:execFileSync|execFile|spawnSync|spawn|run|check_output|check_call|call|Popen|create_subprocess_exec)\s*\(\s*[\"'`]([^\"'`\n]+)[\"'`]\s*,\s*\[([^\]\n]*)\]", code)]
    forms += [" ".join(re.findall(r"[\"'`]([^\"'`\n]*)[\"'`]", m.group(1)))
              for m in re.finditer(r"\b(?:run|check_output|check_call|call|Popen|create_subprocess_exec|execa|spawnSync|spawn)\s*\(\s*\[([^\]\n]*)\]", code)]
    forms += [m.group(2) for m in re.finditer(rf"\b{SHELLS}\s+<<<\s*([\"']?)(.*?)\1\s*(?:$|[;&|])", code)]
    forms += [m.group(2) for m in re.finditer(rf"\b(?:echo|printf)\s+(?:-\S+\s+)?([\"'])(.*?)\1\s*\|\s*(?:env\s+|command\s+)?{SHELLS}\b", code)]
    forms += [m.group(2)[::-1] for m in re.finditer(rf"\b(?:echo|printf)\s+(?:-\S+\s+)?([\"'])(.*?)\1\s*\|\s*rev\s*\|\s*{SHELLS}\b", code)]
    forms = [re.sub(r"`(\s*[A-Za-z_][\w.-]*(?:\s[^`\n]*)?)`", r"$(\1)", v) for v in forms]
    forms = [re.sub(r"(?<![\w.])(?:\./)?hidden-test/(?:(?!\.\.)[\w.-]+/)*(?!\.\.)[\w.-]*", "HIDDENTESTFILE", v) for v in forms]
    forms = [re.sub(r"\$\(\s*(?:cd\s+)?[^()\n]*\$\(\s*(?:dirname|realpath)\s+[^()\n]*\)[^()\n]*\)|\$\(\s*(?:dirname|realpath)\s+[^()\n]*\)|\$\{BASH_SOURCE[^}]*\}", "$TMP", v) for v in forms]
    joined = [j for j in (fold_joins(v) for v in forms) if j not in forms]
    out = []
    for v in forms + joined:
        out.append(v)
        dequoted = re.sub(r"[\"']", "", v)
        if dequoted != v:
            out.append(dequoted)
        braces = re.sub(r"\{([^{}\s,]*)(?:,[^{}\s]*)+\}", r"\1", v)
        if braces != v:
            out.append(braces)
    return out


def open_brackets(text):
    """Brackets opened and not closed in text."""
    return sum(text.count(a) - text.count(b) for a, b in (("(", ")"), ("[", "]"), ("{", "}")))


def logical_lines(lines, suffix):
    """(first line number, physical lines) for each statement. A line ending in a backslash continues on the
    next, and in Python or JavaScript so does a line that leaves a bracket open, six lines at most."""
    groups, i = [], 0
    brackets = suffix in (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".rb")
    while i < len(lines):
        j = i
        while j + 1 < len(lines) and j - i < 5:
            body = strip_comment(lines[j], suffix).rstrip()
            if body.endswith("\\") or (brackets and open_brackets(" ".join(strip_comment(x, suffix) for x in lines[i:j + 1])) > 0):
                j += 1
            else:
                break
        groups.append((i + 1, lines[i:j + 1]))
        i = j + 1
    return groups


def ack_of(group, rel, n, what, suffix):
    """(ack record, problem) for a statement that trips a rule. The marker has to be a comment the file type
    honours and carry a reason of a few words."""
    for line in group:
        m = ACK.search(line)
        if not m:
            continue
        style, reason = m.group(1), m.group(2)
        right = "//" if suffix in SLASH_COMMENT else "#"
        if style != right:
            return None, (f"{rel}:{n} {what}, and the lint ok marker is written as a {style} comment, which this file type does not treat as a comment, "
                          f"write it as {right} lint ok, <reason>, {line.strip()[:80]}")
        if len(re.findall(r"[A-Za-z]{2,}", reason)) < 2:
            return None, f"{rel}:{n} {what}, and the lint ok marker gives no reason, write why the line is fine in a few words, {line.strip()[:80]}"
        return {"file": rel, "line": n, "rule": what, "reason": reason, "text": line.strip()[:160]}, None
    return None, f"{rel}:{n} {what}, {group[0].strip()[:80]}"


def lint_text(lines, rel, suffix, lint):
    """(hits, acks) over the lines of one file. A statement is read as written and again with the values its
    names hold at that point of the file. Every rule it trips is named, the ones that only hold once a name is
    resolved say so."""
    hits, acks = [], []
    names = Bindings(suffix)
    lint = lint + file_lint(lines)
    for n, group in logical_lines(lines, suffix):
        code = " ".join(strip_comment(x, suffix).rstrip().rstrip("\\") for x in group)
        python = suffix == ".py" or "python" in code
        plain = variants_of(code, python)
        readings = names.readings(code, group, python)
        resolved = [v for r in readings if r != code for v in variants_of(r, python)]
        whats = unique(w for rx, w in lint if any(rx.search(v) for v in plain))
        if resolved:
            held = unique(w for rx, w in lint if any(rx.search(v) for v in resolved))
            whats = [w for w in whats if w in held] + [w + ", named through a variable" for w in held if w not in whats]
        whats = whats[:3]
        if not whats:
            continue
        ack, problem = ack_of(group, rel, n, " and ".join(whats), suffix)
        if ack:
            acks.append(ack)
        else:
            hits.append(problem)
    return hits, acks


def lint_lines(path, rel, lint):
    """(hits, acks) for one file, and for what it decodes to when the whole file is Base64 text."""
    text = path.read_text(encoding="utf-8", errors="replace")
    hits, acks = lint_text(text.splitlines(), rel, path.suffix, lint)
    if re.fullmatch(r"[A-Za-z0-9+/=\s]{16,}", text):
        decoded = decode_literal(re.sub(r"\s", "", text), "b64")
        if decoded and re.search(r"\s", decoded):
            h, a = lint_text(decoded.splitlines(), rel + " (decoded)", ".sh", lint)
            hits += h
            acks += a
    return hits, acks


def lint_hidden_test(task):
    """(problems, acknowledged lines) over every file that ships under hidden-test/. The control outputs under
    proof/ are not code and are not read, anything else placed there is refused."""
    lint = BEHAVIOUR_LINT + pr_file_lint(task)
    hits, acks = [], []
    for p in sorted((task / "hidden-test").rglob("*")):
        rel = p.relative_to(task).as_posix()
        if not p.is_file() or rel in ("hidden-test/proof.json", "hidden-test/wrong-fix.patch") or (rel.startswith("hidden-test/proof/") and p.suffix == ".txt"):
            continue
        if rel.startswith("hidden-test/proof/"):
            hits.append(f"{rel} sits in hidden-test/proof/, which holds the control outputs only, move it beside test.sh")
            continue
        h, a = lint_lines(p, rel, lint)
        hits += h
        acks += a
    return hits, acks


def detect_build_path(flag):
    """local or devin-cloud, plus what decided it. The signals are the DEVIN_DIR variable and /opt/.devin, present on Devin machines."""
    if flag:
        return flag, "--build-path flag"
    if os.environ.get("DEVIN_DIR") or Path("/opt/.devin").exists():
        return "devin-cloud", "DEVIN_DIR or /opt/.devin present in the environment"
    return "local", "default, no Devin machine signal in the environment"


def reject(task, reasons):
    """A freeze that fails leaves rejected.json behind, so the no is data too."""
    rec = {"rejected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "stage": "freeze", "reasons": reasons}
    (task / "rejected.json").write_text(json.dumps(rec, indent=2))
    sys.exit("cannot freeze, rejected.json written\n  " + "\n  ".join(reasons))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-dir", required=True)
    ap.add_argument("--build-path", choices=["local", "devin-cloud"], default=None,
                    help="where this task was built, metadata only. Default devin-cloud when the environment looks like a Devin machine, else local")
    ap.add_argument("--engineer")
    ap.add_argument("--one-shot", metavar="REASON", help="freeze without the three recorded gates. The reason goes in approval.json")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    task = Path(a.task_dir)
    approval = task / "approval.json"

    if a.verify:
        if not approval.exists():
            print("not frozen, no approval.json")
            sys.exit(1)
        saved = json.loads(approval.read_text())
        if "lint_acknowledged" not in saved or "lint_acknowledged_sha256" not in saved:
            print("approval.json carries no lint acknowledgement record, it was written by an older freeze or edited since, freeze again")
            sys.exit(1)
        if saved["lint_acknowledged_sha256"] != ack_digest(saved["lint_acknowledged"] or []):
            print("the lint acknowledgements in approval.json were edited after the freeze")
            sys.exit(1)
        now = hashes(task)
        changed = sorted(set(saved["sha256"]) ^ set(now) | {k for k in saved["sha256"] if now.get(k) != saved["sha256"][k]})
        if changed:
            print("frozen artifacts changed since approval, " + ", ".join(changed))
            sys.exit(1)
        print(f"frozen by {saved['engineer']} on {saved['approved_at'][:10]}, unchanged")
        return

    if not a.engineer:
        sys.exit("--engineer 'Full Name' is required")
    if approval.exists():
        sys.exit("already frozen. Delete approval.json on purpose if the engineer wants to change the eval, and note why in suitability.md.")
    gates_path = task / "gates.json"
    gates = json.loads(gates_path.read_text()) if gates_path.is_file() else {"gates": {}}
    if a.one_shot:
        if not a.one_shot.strip():
            sys.exit("--one-shot needs a reason")
        gates["one_shot"] = {"reason": a.one_shot.strip(), "by": a.engineer, "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        gates_path.write_text(json.dumps(gates, indent=2) + "\n")
        print("one shot freeze, the three gates were not asked for one by one, recorded in approval.json")
    gate_issues = gates_problems(task)
    if gate_issues:
        reject(task, gate_issues + ["ask the engineer at each gate and record the yes with gate.py, or pass --one-shot REASON"])
    problems = check_folder(task, need_approval=False)
    if problems:
        reject(task, problems)
    lint, lint_acks = lint_hidden_test(task)
    if lint:
        problems.append("the hidden test looks at implementation details, not behaviour\n    " + "\n    ".join(lint))
    leak = subprocess.run([sys.executable, str(LEAK_CHECK), "--task-dir", str(task)], text=True, capture_output=True)
    print(leak.stdout, end="")
    if leak.returncode and "HARD " in leak.stdout:
        problems.append("the leak check has hard flags on the final prompt and hidden test, they are listed above")
    elif leak.returncode:
        problems.append("the leak check could not run, " + (leak.stderr.strip().splitlines() or ["it printed no error"])[-1])
    if problems:
        reject(task, problems)
    (task / "rejected.json").unlink(missing_ok=True)

    setup_proof = json.loads((task / "setup.json").read_text()).get("setup_proof") or {}
    build_path, detected = detect_build_path(a.build_path)
    data = {
        "engineer": a.engineer,
        "approved_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "build_path": build_path,
        "build_path_detected_from": detected,
        "lint_acknowledged": lint_acks,
        "lint_acknowledged_sha256": ack_digest(lint_acks),
        "build_host": setup_proof.get("host") or platform.node(),
        "build_os": setup_proof.get("os") or platform.platform(),
        "approves": "prompt, base commit and setup, hidden test, grading criteria, isolation rules",
        "runs_and_grades": a.engineer,
        "gates": {g: {k: (gates.get("gates") or {}).get(g, {}).get(k) for k in ("approved_by", "at")} for g in GATES},
        "one_shot": gates.get("one_shot"),
        "sha256": hashes(task),
    }
    approval.write_text(json.dumps(data, indent=2))
    print(f"frozen. approval.json written with {len(data['sha256'])} hashes. Nothing in this eval changes from here.")
    for ack in lint_acks:
        print(f"  lint ok at {ack['file']}:{ack['line']}, {ack['rule']}, the engineer's reason recorded, {ack['reason']}")


if __name__ == "__main__":
    main()
