"""Finders try to find right section for passed module name
"""
import inspect
import os
import os.path
import re
import sys
import sysconfig
from abc import ABCMeta, abstractmethod
from fnmatch import fnmatch
from glob import glob
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Pattern, Sequence, Tuple, Type

from .utils import chdir, exists_case_sensitive

try:
    from pipreqs import pipreqs
except ImportError:
    pipreqs = None

try:
    # pip>=10
    from pip._internal.download import PipSession
    from pip._internal.req import parse_requirements
except ImportError:
    try:
        from pip.download import PipSession
        from pip.req import parse_requirements
    except ImportError:
        parse_requirements = None

try:
    from requirementslib import Pipfile
except ImportError:
    Pipfile = None


KNOWN_SECTION_MAPPING = {
    'STDLIB': 'STANDARD_LIBRARY',
    'FUTURE': 'FUTURE_LIBRARY',
    'FIRSTPARTY': 'FIRST_PARTY',
    'THIRDPARTY': 'THIRD_PARTY',
}


class BaseFinder(metaclass=ABCMeta):
    def __init__(self, config: Mapping[str, Any], sections: Any) -> None:
        self.config = config
        self.sections = sections

    @abstractmethod
    def find(self, module_name: str) -> Optional[str]:
        raise NotImplementedError


class ForcedSeparateFinder(BaseFinder):
    def find(self, module_name: str) -> Optional[str]:
        for forced_separate in self.config['forced_separate']:
            # Ensure all forced_separate patterns will match to end of string
            path_glob = forced_separate
            if not forced_separate.endswith('*'):
                path_glob = '%s*' % forced_separate

            if fnmatch(module_name, path_glob) or fnmatch(module_name, '.' + path_glob):
                return forced_separate


class LocalFinder(BaseFinder):
    def find(self, module_name: str) -> Optional[str]:
        if module_name.startswith("."):
            return self.sections.LOCALFOLDER


class KnownPatternFinder(BaseFinder):
    def __init__(self, config: Mapping[str, Any], sections: Any) -> None:
        super().__init__(config, sections)

        self.known_patterns = []  # type: List[Tuple[Pattern, str]]
        for placement in reversed(self.sections):
            known_placement = KNOWN_SECTION_MAPPING.get(placement, placement)
            config_key = 'known_{0}'.format(known_placement.lower())
            known_patterns = self.config.get(config_key, [])
            known_patterns = [
                pattern
                for known_pattern in known_patterns
                for pattern in self._parse_known_pattern(known_pattern)
            ]
            for known_pattern in known_patterns:
                regexp = '^' + known_pattern.replace('*', '.*').replace('?', '.?') + '$'
                self.known_patterns.append((re.compile(regexp), placement))

    def _parse_known_pattern(self, pattern: str) -> List[str]:
        """
        Expand pattern if identified as a directory and return found sub packages
        """
        if pattern.endswith(os.path.sep):
            patterns = [
                filename
                for filename in os.listdir(pattern)
                if os.path.isdir(os.path.join(pattern, filename))
            ]
        else:
            patterns = [pattern]

        return patterns

    def find(self, module_name: str) -> Optional[str]:
        # Try to find most specific placement instruction match (if any)
        parts = module_name.split('.')
        module_names_to_check = ('.'.join(parts[:first_k]) for first_k in range(len(parts), 0, -1))
        for module_name_to_check in module_names_to_check:
            for pattern, placement in self.known_patterns:
                if pattern.match(module_name_to_check):
                    return placement


class PathFinder(BaseFinder):
    def __init__(self, config: Mapping[str, Any], sections: Any) -> None:
        super().__init__(config, sections)

        # Use a copy of sys.path to avoid any unintended modifications
        # to it - e.g. `+=` used below will change paths in place and
        # if not copied, consequently sys.path, which will grow unbounded
        # with duplicates on every call to this method.
        self.paths = list(sys.path)
        # restore the original import path (i.e. not the path to bin/isort)
        self.paths[0] = os.getcwd()

        # virtual env
        self.virtual_env = self.config.get('virtual_env') or os.environ.get('VIRTUAL_ENV')
        self.virtual_env_src = ''
        if self.virtual_env:
            self.virtual_env_src = '{0}/src/'.format(self.virtual_env)
            for path in glob('{0}/lib/python*/site-packages'.format(self.virtual_env)):
                if path not in self.paths:
                    self.paths.append(path)
            for path in glob('{0}/lib/python*/*/site-packages'.format(self.virtual_env)):
                if path not in self.paths:
                    self.paths.append(path)
            for path in glob('{0}/src/*'.format(self.virtual_env)):
                if os.path.isdir(path):
                    self.paths.append(path)

        # handle case-insensitive paths on windows
        self.stdlib_lib_prefix = os.path.normcase(sysconfig.get_paths()['stdlib'])
        if self.stdlib_lib_prefix not in self.paths:
            self.paths.append(self.stdlib_lib_prefix)

        # handle compiled libraries
        self.ext_suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"

    def find(self, module_name: str) -> Optional[str]:
        for prefix in self.paths:
            package_path = "/".join((prefix, module_name.split(".")[0]))
            is_module = (exists_case_sensitive(package_path + ".py") or
                         exists_case_sensitive(package_path + ".so") or
                         exists_case_sensitive(package_path + self.ext_suffix))
            is_package = exists_case_sensitive(package_path) and os.path.isdir(package_path)
            if is_module or is_package:
                if 'site-packages' in prefix:
                    return self.sections.THIRDPARTY
                if 'dist-packages' in prefix:
                    return self.sections.THIRDPARTY
                if self.virtual_env and self.virtual_env_src in prefix:
                    return self.sections.THIRDPARTY
                if os.path.normcase(prefix).startswith(self.stdlib_lib_prefix):
                    return self.sections.STDLIB
                return self.config['default_section']


class ReqsBaseFinder(BaseFinder):
    enabled = False

    def __init__(self, config: Mapping[str, Any], sections: Any, path: str = '.') -> None:
        super().__init__(config, sections)
        self.path = path
        if self.enabled:
            self.mapping = self._load_mapping()
            self.names = self._load_names()

    @abstractmethod
    def _get_names(self, path: str) -> Iterator[str]:
        raise NotImplementedError

    @abstractmethod
    def _get_files_from_dir(self, path: str) -> Iterator[str]:
        raise NotImplementedError

    @staticmethod
    def _load_mapping() -> Optional[Dict[str, str]]:
        """Return list of mappings `package_name -> module_name`

        Example:
            django-haystack -> haystack
        """
        if not pipreqs:
            return None
        path = os.path.dirname(inspect.getfile(pipreqs))
        path = os.path.join(path, 'mapping')
        with open(path) as f:
            # pypi_name: import_name
            mappings = {}  # type: Dict[str, str]
            for line in f:
                import_name, _, pypi_name = line.strip().partition(":")
                mappings[pypi_name] = import_name
            return mappings
            # return dict(tuple(line.strip().split(":")[::-1]) for line in f)

    def _load_names(self) -> List[str]:
        """Return list of thirdparty modules from requirements
        """
        names = []
        for path in self._get_files():
            for name in self._get_names(path):
                names.append(self._normalize_name(name))
        return names

    @staticmethod
    def _get_parents(path: str) -> Iterator[str]:
        prev = ''
        while path != prev:
            prev = path
            yield path
            path = os.path.dirname(path)

    def _get_files(self) -> Iterator[str]:
        """Return paths to all requirements files
        """
        path = os.path.abspath(self.path)
        if os.path.isfile(path):
            path = os.path.dirname(path)

        for path in self._get_parents(path):
            for file_path in self._get_files_from_dir(path):
                yield file_path

    def _normalize_name(self, name: str) -> str:
        """Convert package name to module name

        Examples:
            Django -> django
            django-haystack -> django_haystack
            Flask-RESTFul -> flask_restful
        """
        if self.mapping:
            name = self.mapping.get(name, name)
        return name.lower().replace('-', '_')

    def find(self, module_name: str) -> Optional[str]:
        # required lib not installed yet
        if not self.enabled:
            return None

        module_name, _sep, _submodules = module_name.partition('.')
        module_name = module_name.lower()
        if not module_name:
            return None

        for name in self.names:
            if module_name == name:
                return self.sections.THIRDPARTY


class RequirementsFinder(ReqsBaseFinder):
    exts = ('.txt', '.in')
    enabled = bool(parse_requirements)

    def _get_files_from_dir(self, path: str) -> Iterator[str]:
        """Return paths to requirements files from passed dir.
        """
        for fname in os.listdir(path):
            if 'requirements' not in fname:
                continue
            full_path = os.path.join(path, fname)

            # *requirements*/*.{txt,in}
            if os.path.isdir(full_path):
                for subfile_name in os.listdir(path):
                    for ext in self.exts:
                        if subfile_name.endswith(ext):
                            yield os.path.join(path, subfile_name)
                continue

            # *requirements*.{txt,in}
            if os.path.isfile(full_path):
                for ext in self.exts:
                    if fname.endswith(ext):
                        yield full_path
                        break

    def _get_names(self, path: str) -> Iterator[str]:
        """Load required packages from path to requirements file
        """
        with chdir(os.path.dirname(path)):
            requirements = parse_requirements(path, session=PipSession())
            for req in requirements:
                if req.name:
                    yield req.name


class PipfileFinder(ReqsBaseFinder):
    enabled = bool(Pipfile)

    def _get_names(self, path: str) -> Iterator[str]:
        with chdir(path):
            project = Pipfile.load(path)
            for req in project.packages:
                yield req.name

    def _get_files_from_dir(self, path: str) -> Iterator[str]:
        if 'Pipfile' in os.listdir(path):
            yield path


class DefaultFinder(BaseFinder):
    def find(self, module_name: str) -> Optional[str]:
        return self.config['default_section']


class FindersManager(object):
    _default_finders_classes = (
        ForcedSeparateFinder,
        LocalFinder,
        KnownPatternFinder,
        PathFinder,
        PipfileFinder,
        RequirementsFinder,
        DefaultFinder,
    )

    def __init__(self, config: Mapping[str, Any], sections: Any, finders: Optional[Iterable[BaseFinder]]=None):
        self.verbose = config.get('verbose', False)

        finders = self.finders if finders is None else finders
        self.finders = []
        for finder in finders:
            try:
                self.finders.append(finder(config, sections))
            except Exception as exception:
                # if one finder fails to instantiate isort can continue using the rest
                if self.verbose:
                    print('{} encountered an error ({}) during instantiation and cannot be used'.format(finder.__name__,
                                                                                                        str(exception)))
        self.finders = tuple(self.finders)

    def find(self, module_name: str) -> Optional[str]:
        for finder in self.finders:
            try:
                section = finder.find(module_name)
            except Exception as exception:
                # isort has to be able to keep trying to identify the correct import section even if one approach fails
                if self.verbose:
                    print('{} encountered an error ({}) while trying to identify the {} module'.format(finder.__name__,
                                                                                                       str(exception),
                                                                                                       module_name))
            if section is not None:
                return section
