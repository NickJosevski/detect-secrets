from contextlib import contextmanager
from copy import deepcopy
from functools import lru_cache
from importlib import import_module
from typing import Any
from typing import Dict
from typing import Generator
from typing import List
from urllib.parse import urlparse

from .exceptions import InvalidFile
from .util.importlib import import_file_as_module


@lru_cache(maxsize=1)
def get_settings() -> 'Settings':
    """
    This is essentially a singleton pattern, that allows for (controlled) global access
    to common variables.
    """
    return Settings()


def configure_settings_from_baseline(baseline: Dict[str, Any], filename: str = '') -> 'Settings':
    """
    :raises: KeyError
    """
    settings = get_settings()

    if 'plugins_used' in baseline:
        settings.configure_plugins(baseline['plugins_used'])

    if 'filters_used' in baseline:
        settings.configure_filters(baseline['filters_used'])

        if 'detect_secrets.filters.wordlist.should_exclude_secret' in settings.filters:
            config = settings.filters['detect_secrets.filters.wordlist.should_exclude_secret']

            from detect_secrets import filters
            filters.wordlist.initialize(
                wordlist_filename=config['file_name'],
                min_length=config['min_length'],
                file_hash=config['file_hash'],
            )

        if 'detect_secrets.filters.gibberish.should_exclude_secret' in settings.filters:
            config = settings.filters['detect_secrets.filters.gibberish.should_exclude_secret']

            from detect_secrets import filters
            filters.gibberish.initialize(
                model_path=config.get('model'),
                limit=config['limit'],
            )

    if filename:
        settings.filters['detect_secrets.filters.common.is_baseline_file'] = {
            'filename': filename,
        }

    return settings


@contextmanager
def default_settings() -> Generator['Settings', None, None]:
    """Convenience function to enable all plugins and default filters."""
    from .core.plugins.util import get_mapping_from_secret_type_to_class

    with transient_settings({
        'plugins_used': [
            {'name': plugin_type.__name__}
            for plugin_type in get_mapping_from_secret_type_to_class().values()
        ],
    }) as settings:
        yield settings


@contextmanager
def transient_settings(config: Dict[str, Any]) -> Generator['Settings', None, None]:
    """Allows the customizability of non-global settings per invocation."""
    original_settings = get_settings().json()

    cache_bust()
    try:
        yield configure_settings_from_baseline(config)
    finally:
        cache_bust()
        configure_settings_from_baseline(original_settings)


def cache_bust() -> None:
    get_settings.cache_clear()
    get_filters.cache_clear()
    get_plugins.cache_clear()


class Settings:
    DEFAULT_FILTERS = {
        'detect_secrets.filters.common.is_invalid_file',
        'detect_secrets.filters.heuristic.is_non_text_file',
    }

    def __init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        # mapping of class names to initialization variables
        self.plugins: Dict[str, Dict[str, Any]] = {}

        # mapping of python import paths to configuration variables
        self.filters: Dict[str, Dict[str, Any]] = {
            path: {}
            for path in {
                *self.DEFAULT_FILTERS,
                'detect_secrets.filters.allowlist.is_line_allowlisted',
                'detect_secrets.filters.heuristic.is_sequential_string',
                'detect_secrets.filters.heuristic.is_potential_uuid',
                'detect_secrets.filters.heuristic.is_likely_id_string',
                'detect_secrets.filters.heuristic.is_templated_secret',
                'detect_secrets.filters.heuristic.is_prefixed_with_dollar_sign',
                'detect_secrets.filters.heuristic.is_indirect_reference',
                'detect_secrets.filters.heuristic.is_lock_file',
                'detect_secrets.filters.heuristic.is_swagger_file',
            }
        }

    def set(self, other: 'Settings') -> None:
        self.plugins = other.plugins
        self.filters = other.filters

    def configure_plugins(self, config: List[Dict[str, Any]]) -> 'Settings':
        """
        :param config: e.g.
            [
                {'name': 'AWSKeyDetector'},
                {'limit': 4.5, 'name': 'Base64HighEntropyString'}
            ]
        """
        for plugin in config:
            plugin = {**plugin}
            name = plugin.pop('name')
            self.plugins[name] = plugin

        get_plugins.cache_clear()
        return self

    def disable_plugins(self, *plugin_names: str) -> 'Settings':
        for name in plugin_names:
            try:
                self.plugins.pop(name)
            except KeyError:
                pass

        get_plugins.cache_clear()
        return self

    def configure_filters(self, config: List[Dict[str, Any]]) -> 'Settings':
        """
        :param config: e.g.
            [
                {'path': 'detect_secrets.filters.heuristic.is_sequential_string'},
                {
                    'path': 'detect_secrets.filters.regex.should_exclude_files',
                    'pattern': '^test.*',
                }
            ]
        """
        self.filters = {
            path: {}
            for path in self.DEFAULT_FILTERS
        }

        # Make a copy, so we don't affect the original.
        filter_configs = deepcopy(config)
        for filter_config in filter_configs:
            path = filter_config['path']
            self.filters[path] = filter_config

        get_filters.cache_clear()
        return self

    def disable_filters(self, *filter_paths: str) -> 'Settings':
        for filter_path in filter_paths:
            self.filters.pop(filter_path, None)

        get_filters.cache_clear()
        return self

    def json(self) -> Dict[str, Any]:
        plugins_used = []
        for plugin in get_plugins():
            # NOTE: We use the initialized plugin's JSON representation (rather than using
            # the configured settings) to deal with cases where plugins define their own
            # default variables, that is not necessarily carried through through the
            # settings object.
            serialized_plugin = plugin.json()

            plugins_used.append({
                # We want this to appear first.
                'name': serialized_plugin['name'],

                # NOTE: We still need to use the saved settings configuration though, since
                # there are keys specifically in the settings object that we need to carry over
                # (e.g. `path` for custom plugins).
                **self.plugins[serialized_plugin['name']],

                # Finally, this comes last so that it overrides any values that are saved in
                # the settings object.
                **serialized_plugin,
            })

        return {
            'plugins_used': sorted(
                plugins_used,
                key=lambda x: str(x['name'].lower()),
            ),
            'filters_used': sorted(
                [
                    {
                        'path': path,
                        **config,
                    }
                    for path, config in self.filters.items()
                    if path not in self.DEFAULT_FILTERS
                ],
                key=lambda x: str(x['path'].lower()),
            ),
        }


@lru_cache(maxsize=1)
def get_plugins() -> List:
    # We need to import this here, otherwise it will result in a circular dependency.
    from .core import plugins

    return [
        plugins.initialize.from_plugin_classname(classname)
        for classname in get_settings().plugins
    ]


@lru_cache(maxsize=1)
def get_filters() -> List:
    from .core.log import log
    from .util.inject import get_injectable_variables

    output = []
    for path, config in get_settings().filters.items():
        parts = urlparse(path)
        if not parts.scheme:
            module_path, function_name = path.rsplit('.', 1)
            try:
                function = getattr(import_module(module_path), function_name)
            except (ModuleNotFoundError, AttributeError):
                log.warning(f'Invalid filter: {path}')
                continue

        elif parts.scheme == 'file':
            file_path, function_name = path[len('file://'):].split('::')

            try:
                function = getattr(import_file_as_module(file_path), function_name)
            except (FileNotFoundError, InvalidFile, AttributeError):
                log.warning(f'Invalid filter: {path}')
                continue

        else:
            log.warning(f'Invalid filter: {path}')
            continue

        # We attach this metadata to the function itself, so that we don't need to
        # compute it everytime. This will allow for dependency injection for filters.
        function.injectable_variables = set(get_injectable_variables(function))
        output.append(function)

        # This is for better logging.
        function.path = path

    return output
