"""Build the fixed scientific-engine payload and the stdin worker program."""
import base64
from importlib.resources import files
from pathlib import Path

from vasp_agent.remote.remote_worker import ENGINE_MODULES, SOURCE_FILES, WORKER_UNITS, release_version


# Lines between these markers exist only for the package import path: they
# import names from earlier worker units, which the stdin program already
# defines because it concatenates those units ahead of the importer.
PACKAGE_ONLY = ('# <bundle:package-only>', '# </bundle:package-only>')


def engine_sources():
    root = files('vasp_agent')
    result = {}
    for name in SOURCE_FILES:
        if '/' in name:
            data = root.joinpath(name.removeprefix('vasp_agent/')).read_bytes()
        else:
            module = ENGINE_MODULES[name.removesuffix('.py')]
            # Flat entry names are part of the recorded remote-job contract.
            data = (f'"""Legacy engine entry for {module}."""\n'
                    'import importlib, runpy, sys\n'
                    "if __name__ == '__main__':\n"
                    f'    runpy.run_module({module!r}, run_name="__main__")\n'
                    'else:\n'
                    f'    sys.modules[__name__] = importlib.import_module({module!r})\n').encode('utf-8')
        # Every source is Python text; a CRLF checkout (core.autocrlf) must not
        # read as a different release than the same commit checked out with LF.
        data = data.replace(b'\r\n', b'\n')
        result[name] = base64.b64encode(data).decode('ascii')
    return result


def local_version():
    """The release name deploying this checkout would produce (the remote _deploy formula)."""
    return release_version(engine_sources())


def unit_source(unit):
    """One worker unit as it runs inside the stdin program."""
    text = Path(__file__).with_name(unit + '.py').read_text(encoding='utf-8')
    kept, skipping = [], False
    for line in text.splitlines(keepends=True):
        marker = line.rstrip('\r\n')
        if marker in PACKAGE_ONLY:
            # Fail here rather than ship a program that silently lost or kept code.
            if skipping != (marker == PACKAGE_ONLY[1]):
                raise ValueError(f'{unit}.py 的 package-only 标记不成对。')
            skipping = not skipping
        elif not skipping:
            kept.append(line)
    if skipping:
        raise ValueError(f'{unit}.py 的 package-only 标记未闭合。')
    return ''.join(kept)


_WORKER_PROGRAM_CACHE: dict[tuple[str, ...], str] = {}


def clear_worker_cache():
    """Drop the in-memory worker_program() cache (primarily for tests)."""
    _WORKER_PROGRAM_CACHE.clear()


def worker_program():
    """The worker as one self-contained program: units in dependency order, entry last.

    The program travels on stdin so multiline Python never crosses the Windows
    argv -> SSH -> shell quoting chain, and the remote side has no package to
    import from before the first deploy -- so it has to be a single source.
    Cached in memory for the lifetime of the process so repeated RPCs do not
    re-read and strip the unit files from disk on every call.
    """
    key = tuple(WORKER_UNITS)
    cached = _WORKER_PROGRAM_CACHE.get(key)
    if cached is None:
        cached = '\n\n'.join(unit_source(unit) for unit in (*key, 'remote_worker'))
        _WORKER_PROGRAM_CACHE[key] = cached
    return cached


def stdin_program(payload):
    """Prepend the request so Python reads the whole program before running it."""
    return 'import io, sys\nsys.stdin = io.StringIO(' + repr(payload) + ')\n' + worker_program()
