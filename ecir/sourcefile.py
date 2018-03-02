"""
Module containing a set of classes to represent and manipuate a
Fortran source code file.
"""
import re
import time
import open_fortran_parser
from collections import Iterable

from ecir.subroutine import Section, Subroutine, Module
from ecir.tools import disk_cached

__all__ =['FortranSourceFile']


class FortranSourceFile(object):
    """
    Class to handle and manipulate Fortran source files.

    :param filename: Name of the input source file
    """

    def __init__(self, filename):
        self.name = filename

        # Import and store the raw file content
        with open(filename) as f:
            self._raw_source = f.read()

        # Parse the file content into a Fortran AST
        print("Parsing %s..." % filename)
        t0 = time.time()
        self._ast = self.parse_ast(filename=filename)
        t1 = time.time() - t0
        print("Parsing done! (time: %.2fs)" % t1)

        # Extract subroutines and pre/post sections from file
        self.subroutines = [Subroutine(ast=r, raw_source=self._raw_source)
                            for r in self._ast.findall('file/subroutine')]
        self.modules = [Module(ast=m, raw_source=self._raw_source)
                        for m in self._ast.findall('file/module')]

    @disk_cached(argname='filename')
    def parse_ast(self, filename):
        """
        Read and parse a source file usign the Open Fortran Parser.

        Note: The parsing is cached on disk in ``<filename>.cache``.
        """
        return open_fortran_parser.parse(filename)

    @property
    def source(self):
        content = self.modules + self.subroutines
        return '\n\n'.join(s.source for s in content)

    def write(self, filename=None):
        """
        Write content to file

        :param filename: Optional filename. If not provided, `self.name` is used
        """
        filename = filename or self.name
        with open(filename, 'w') as f:
            f.write(self.source)

    @property
    def lines(self):
        """
        Sanitizes source content into long lines with continuous statements.

        Note: This does not change the content of the file
        """
        return self._raw_source.splitlines(keepends=True)

    @property
    def longlines(self):
        return self.body.longlines

    def replace(self, mapping):
        """
        Performs a line-by-line string-replacement from a given mapping

        Note: The replacement is performed on each raw line. Might
        need to improve this later to unpick linebreaks in the search
        keys.
        """
        for section in self.sections:
            section.replace(mapping)