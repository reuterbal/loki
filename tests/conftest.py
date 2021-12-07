from pathlib import Path
import contextlib
import os
import io
import pytest

from loki import (
    Sourcefile, Module, Subroutine, fgen, OFP, compile_and_load, FindNodes, CallStatement,
    as_tuple, Frontend, frontend
)
from loki.build import Builder, Lib, Obj
from loki.tools import gettempdir, filehash


__all__ = ['generate_identity', 'jit_compile', 'jit_compile_lib', 'clean_test',
           'stdchannel_redirected', 'stdchannel_is_captured']


def generate_identity(refpath, routinename, modulename=None, frontend=OFP):
    """
    Generate the "identity" of a single subroutine with a frontend-specific suffix.
    """
    testname = refpath.parent/('%s_%s_%s.f90' % (refpath.stem, routinename, frontend))
    source = Sourcefile.from_file(refpath, frontend=frontend)

    if modulename:
        module = [m for m in source.modules if m.name == modulename][0]
        module.name += '_%s_%s' % (routinename, frontend)
        for routine in source.all_subroutines:
            routine.name += '_%s' % frontend
            for call in FindNodes(CallStatement).visit(routine.body):  # pylint: disable=no-member
                call.name += '_%s' % frontend
        source.write(path=testname, source=fgen(module))
    else:
        routine = [r for r in source.subroutines if r.name == routinename][0]
        routine.name += '_%s' % frontend
        source.write(path=testname, source=fgen(routine))

    pymod = compile_and_load(testname, cwd=str(refpath.parent), use_f90wrap=True)

    if modulename:
        # modname = '_'.join(s.capitalize() for s in refpath.stem.split('_'))
        return getattr(pymod, testname.stem)
    return pymod


def jit_compile(source, filepath=None, objname=None):
    """
    Generate, Just-in-Time compile and load a given item
    for interactive execution.

    Parameters
    ----------
    source : :any:`Sourcefile` or :any:`Module` or :any:`Subroutine`
        The item to compile and load
    filepath : str or :any:`Path`, optional
        Path of the source file to write (default: hashed name in :any:`gettempdir()`)
    objname : str, optional
        Return a specific object (module or subroutine) in :attr:`source`
    """
    if isinstance(source, Sourcefile):
        filepath = source.filepath if filepath is None else Path(filepath)
        source.write(path=filepath)
    else:
        source = fgen(source)
        if filepath is None:
            filepath = gettempdir()/filehash(source, prefix='', suffix='.f90')
        else:
            filepath = Path(filepath)
        Sourcefile(filepath).write(source=source)

    pymod = compile_and_load(filepath, cwd=str(filepath.parent), use_f90wrap=True)

    if objname:
        return getattr(pymod, objname)
    return pymod


def jit_compile_lib(sources, path, name, wrap=None, builder=None):
    """
    Generate, just-in-time compile and load a set of items into a
    library and import dynamically into the Python runtime.

    Parameters
    ----------
    source : list
        Source items or filepaths to compile and add to lib
    path : str or :any:`Path`
        Basepath for on-the-fly creation of source files
    name : str
        Name of created lib
    wrap : list, optional
        File names to pass to ``f90wrap``. Defaults to list of source files.
    builder : :any:`Builder`, optional
        Builder object to use for lib compilation and linking
    """
    builder = builder or Builder(source_dirs=path, build_dir=path)
    sourcefiles = []

    for source in sources:
        if isinstance(source, (str, Path)):
            sourcefiles.append(source)

        if isinstance(source, Sourcefile):
            filepath = source.path or path/'{}.f90'.format(source.name)
            source.write(path=filepath)
            sourcefiles.append(source.path)

        elif isinstance(source, Module):
            filepath = path/'{}.f90'.format(source.name)
            source = Sourcefile(filepath, modules=[source])
            source.write(path=filepath)
            sourcefiles.append(source.path)

        elif isinstance(source, Subroutine):
            filepath = path/'{}.f90'.format(source.name)
            source = Sourcefile(filepath, routines=[source])
            source.write(path=filepath)
            sourcefiles.append(source.path)

    objects = [Obj(source_path=s) for s in sourcefiles]
    lib = Lib(name=name, objs=objects, shared=False)
    lib.build(builder=builder)
    wrap = wrap or sourcefiles
    return lib.wrap(modname=name, sources=wrap, builder=builder)


def clean_test(filepath):
    """
    Clean test directory based on JIT'ed source file.
    """
    file_list = [filepath.with_suffix('.f90'), filepath.with_suffix('.o'),
                 filepath.with_suffix('.py'), filepath.parent/'f90wrap_toplevel.f90']
    for f in file_list:
        if f.exists():
            f.unlink()
    for sofile in filepath.parent.glob('_%s.*.so' % filepath.stem):
        sofile.unlink()
    f90wrap_path = filepath.parent/'f90wrap_{}'.format(filepath.name)
    if f90wrap_path.exists():
        f90wrap_path.unlink()


@contextlib.contextmanager
def stdchannel_redirected(stdchannel, dest_filename):
    """
    A context manager to temporarily redirect stdout or stderr

    e.g.:

    ```
    with stdchannel_redirected(sys.stderr, os.devnull):
        if compiler.has_function('clock_gettime', libraries=['rt']):
            libraries.append('rt')
    ```

    Source: https://stackoverflow.com/a/17753573

    Note, that this only works when pytest is invoked with '--show-capture' (or '-s').
    This can be checked using `stdchannel_is_captured(capsys)`.
    Additionally, capturing of sys.stdout/sys.stderr needs to be disabled explicitly,
    i.e., use the fixture `capsys` and wrap the above:

    ```
    with capsys.disabled():
        with stdchannel_redirected(sys.stdout, 'stdout.log'):
            function()
    ```
    """

    def try_dup(fd):
        try:
            oldfd = os.dup(fd.fileno())
        except io.UnsupportedOperation:
            oldfd = None
        return oldfd

    def try_dup2(fd, fd2, fd_fileno=True):
        try:
            if fd_fileno:
                os.dup2(fd.fileno(), fd2.fileno())
            else:
                os.dup2(fd, fd2.fileno())
        except io.UnsupportedOperation:
            pass

    oldstdchannel, dest_file = None, None
    try:
        oldstdchannel = try_dup(stdchannel)
        dest_file = open(dest_filename, 'w')
        try_dup2(dest_file, stdchannel)

        yield
    finally:
        if oldstdchannel is not None:
            try_dup2(oldstdchannel, stdchannel, fd_fileno=False)
        if dest_file is not None:
            dest_file.close()


def stdchannel_is_captured(capsys):
    """
    Utility function to verify if pytest captures stdout/stderr.

    This hinders redirecting stdout/stderr for f2py/f90wrap functions.

    Parameters
    ----------
    capsys :
        The capsys fixture of the test.

    Returns
    -------
    bool
        `True` if pytest captures output, otherwise `False`.
    """

    capturemanager = capsys.request.config.pluginmanager.getplugin("capturemanager")
    return capturemanager._global_capturing.out is not None


def parametrize_frontend(test, xfail=None, skip=None):
    """
    Decorator to parameterize tests with a :attr:`frontend` argument to run
    them with all available frontends

    For any unavailable frontends where ``HAVE_<frontend>`` is `False` (e.g.
    because required dependencies are not installed), :attr:`test` is marked as
    skipped.

    Use as

    ..code-block::
        @parametrize_frontend
        def my_test(frontend):
            source = Sourcefile.from_file('some.F90', frontend=frontend)
            # ...

    Parameters
    ----------
    test :
        The function implementing the test.
    xfail : list, optional
        Provide frontends that are expected to fail, optionally as tuple with reason
        provided as string. By default `None`
    skip : list, optional
        Provide frontends that are always skipped, optionally as tuple with reason
        provided as string. By default `None`
    """
    if xfail:
        xfail = dict((as_tuple(f) + (None,))[:2] for f in xfail)
    else:
        xfail = ()

    if skip:
        skip = dict((as_tuple(f) + (None,))[:2] for f in skip)
    else:
        skip = {}

    # Unavailable frontends
    unavailable_frontends = {
        f: f'{f} is not available' for f in Frontend
        if not getattr(frontend, f'HAVE_{str(f).upper}')
    }
    skip.update(unavailable_frontends)

    params = []
    for f in Frontend:
        if f in skip:
            params += [pytest.param(f, marks=pytest.mark.skip(reason=skip[f]))]
        elif f in xfail:
            params += [pytest.param(f, marks=pytest.mark.xfail(reason=xfail[f]))]
        else:
            params += [f]

    return pytest.mark.parametrize(test, 'frontend', params)
