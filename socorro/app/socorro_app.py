#! /usr/bin/env python
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""This module defines the class hierarchy for all Socorro applications.

The base of the hierarchy is "SocorroApp" which defines the interface and some
of the base methods.

Derived from the base "SocorroApp" is the "App" class.  This class adds logging
configuration requirements to the application.  App is the class from which
all the Socorro Apps derive.

"""

import logging
import logging.handlers
import functools
import signal
import os
import sys
import re
import threading

from configman import (
    ConfigurationManager,
    Namespace,
    RequiredConfig,
    ConfigFileFutureProxy,
    environment,
    command_line,
)
from configman.converters import py_obj_to_str, str_to_python_object


# for use with SIGHUP for apps that run as daemons
restart = True


def respond_to_SIGHUP(signal_number, frame, logger=None):
    """raise the KeyboardInterrupt which will cause the app to effectively
    shutdown, closing all it resources.  Then, because it sets 'restart' to
    True, the app will reread all the configuration information, rebuild all
    of its structures and resources and start running again"""
    global restart
    restart = True
    if logger:
        logger.info('detected SIGHUP')
    raise KeyboardInterrupt


def klass_to_pypath(klass):
    """when a class is defined within the module that is being executed as
    main, the module name will be specified as '__main__' even though the
    module actually had its own real name.  This ends up being very confusing
    to Configman as it tries to refer to a class by its proper module name.
    This function will convert a class into its properly qualified actual
    pathname."""
    if klass.__module__ == '__main__':
        module_path = (
            sys.modules['__main__']
            .__file__[:-3]
        )
        module_name = ''
        for a_python_path in sys.path:
            tentative_pathname = module_path.replace(a_python_path, '')
            if tentative_pathname != module_path:
                module_name = (
                    tentative_pathname.replace('/', '.').strip('.')
                )
                break
        if module_name == '':
            return py_obj_to_str(klass)
    else:
        module_name = klass.__module__
    return "%s.%s" % (module_name, klass.__name__)


class SocorroApp(RequiredConfig):
    """The base class for all Socorro applications"""
    app_name = 'SocorroAppBaseClass'
    app_version = "1.0"
    app_description = 'base class for app system'

    #: String containing a module import path. The module is used as a
    #: source for default configuration values. If None, this makes no
    #: changes to the configuration defaults.
    config_defaults = None

    required_config = Namespace()

    def __init__(self, config):
        self.config = config
        # give a name to this running instance of the program.
        self.app_instance_name = self._app_instance_name()

    def main(self):  # pragma: no cover
        """derived classes must override this function with business logic"""
        raise NotImplementedError(
            "A definition of 'main' in a derived class is required"
        )

    def _app_instance_name(self):
        # originally, only the processors had instance names.  By putting this
        # call here, all the apps have instance names that can be used to
        # tag output that is traceble back to an app/machine/process.
        return "%s_%s_%d" % (
            self.app_name,
            os.uname()[1].replace('.', '_'),
            os.getpid()
        )

    @classmethod
    def run(klass, config_path=None, values_source_list=None):
        global restart
        restart = True
        while restart:
            # the SIGHUP handler will change that back to True if it wants
            # the app to restart and run again.
            restart = False
            app_exit_code = klass._do_run(
                config_path=config_path,
                values_source_list=values_source_list
            )
        return app_exit_code

    @classmethod
    def _do_run(klass, config_path=None, values_source_list=None):
        # while this method is defined here, only derived classes are allowed
        # to call it.
        if klass is SocorroApp:
            raise NotImplementedError(
                "The SocorroApp class has no useable 'main' method"
            )

        if config_path is None:
            config_path = os.environ.get(
                'DEFAULT_SOCORRO_CONFIG_PATH',
                './config'
            )

        if values_source_list is None:
            values_source_list = [
                # pull in any configuration file
                ConfigFileFutureProxy,
                # get values from the environment
                environment,
                # use the command line to get the final overriding values
                command_line
            ]

        # Pull base set of defaults from the config module if it is specified
        if klass.config_defaults is not None:
            values_source_list.insert(0, klass.config_defaults)

        config_definition = klass.get_required_config()
        if 'application' not in config_definition:
            # FIXME(mkelly): We used to have a SocorroWelcomeApp that defined an
            # "application" option. We no longer have that. This section should
            # get reworked possibly as part of getting rid of application
            # defaults.
            application_config = Namespace()
            application_config.add_option(
                'application',
                doc=(
                    'the fully qualified classname of the app to run'
                ),
                default=klass_to_pypath(klass),
                # the following setting means this option will NOT be
                # commented out when configman generates a config file
                likely_to_be_changed=True,
                from_string_converter=str_to_python_object,
            )
            config_definition = application_config

        config_manager = ConfigurationManager(
            config_definition,
            app_name=klass.app_name,
            app_version=klass.app_version,
            app_description=klass.app_description,
            values_source_list=values_source_list,
            options_banned_from_help=[],
            config_pathname=config_path
        )

        def fix_exit_code(code):
            # some apps don't return a code so you might get None
            # which isn't good enough to send to sys.exit()
            if code is None:
                return 0
            return code

        with config_manager.context() as config:
            config.executor_identity = (
                lambda: threading.currentThread().getName()
            )
            try:
                config_manager.log_config(config.logger)
                respond_to_SIGHUP_with_logging = functools.partial(
                    respond_to_SIGHUP,
                    logger=config.logger
                )
                # install the signal handler with logging
                signal.signal(signal.SIGHUP, respond_to_SIGHUP_with_logging)
            except KeyError:
                # config apparently doesn't have 'logger'
                # install the signal handler without logging
                signal.signal(signal.SIGHUP, respond_to_SIGHUP)

            # we finally know what app to actually run, instantiate it
            app_to_run = klass(config)
            app_to_run.config_manager = config_manager
            # whew, finally run the app that we wanted

            return_code = fix_exit_code(app_to_run.main())
            return return_code


class LoggerWrapper(object):
    """This class wraps the standard logger object.  It changes the logged
    messages to display the 'executor_identity': the thread/greenlet/process
    that is currently running."""

    def __init__(self, logger, config):
        self.config = config
        self.logger = logger

    def executor_identity(self):
        try:
            return " - %s - " % self.config.executor_identity()
        except KeyError:
            return " - %s - " % threading.currentThread().getName()

    def debug(self, message, *args, **kwargs):
        self.logger.debug(self.executor_identity() + message, *args, **kwargs)

    def info(self, message, *args, **kwargs):
        self.logger.info(self.executor_identity() + message, *args, **kwargs)

    def error(self, message, *args, **kwargs):
        self.logger.error(self.executor_identity() + message, *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        self.logger.warning(
            self.executor_identity() + message,
            *args,
            **kwargs
        )

    def critical(self, message, *args, **kwargs):
        self.logger.critical(
            self.executor_identity() + message,
            *args,
            **kwargs
        )

    def exception(self, message, *args, **kwargs):
        kwargs['exc_info'] = True
        self.error(message, *args, **kwargs)


def setup_logger(config, local_unused, args_unused):
    """This method is sets up and initializes the logger objects.  It is a
    function in the form appropriate for a configiman aggregation.  When given
    to Configman, that library will setup and initialize the logging system
    automatically and then offer the logger as an object within the
    configuration object."""
    try:
        app_name = config.application.app_name
    except KeyError:
        app_name = 'a_socorro_app'
    logger = logging.getLogger(app_name)
    # if this is a restart, loggers must be removed before being recreated
    tear_down_logger(app_name)
    logger.setLevel(logging.DEBUG)
    stderr_log = logging.StreamHandler()
    stderr_log.setLevel(config.logging.stderr_error_logging_level)
    stderr_format = config.logging.stderr_line_format_string.replace(
        '{app_name}',
        app_name
    )
    stderr_log_formatter = logging.Formatter(
        _convert_format_string(stderr_format)
    )
    stderr_log.setFormatter(stderr_log_formatter)
    logger.addHandler(stderr_log)

    syslog = logging.handlers.SysLogHandler(
        facility=config.logging.syslog_facility_string
    )
    syslog.setLevel(config.logging.syslog_error_logging_level)
    syslog_format = config.logging.syslog_line_format_string.replace(
        '{app_name}',
        app_name
    )
    syslog_formatter = logging.Formatter(
        _convert_format_string(syslog_format)
    )
    syslog.setFormatter(syslog_formatter)
    logger.addHandler(syslog)

    wrapped_logger = LoggerWrapper(logger, config)
    return wrapped_logger


def setup_metrics(config, local_unused, args_unused):
    """Sets up metrics client which is either a DogStatsd client or a LoggingClient

    NOTE(willkg): This is a little gross, but that's mostly to keep all the code in one place and
    minimal which makes it easier to deal with for now. If this ever grows, then we'll probably want
    to split this out into its own module.

    This gets tossed in config (along with everything else), so you can use it like this::

        class SomeComponent(RequiredConfig):
            def something(self):
                self.config.metrics.increment('somekey')

    """
    # We do the import here in case there are parts of Socorro that are running in environments that
    # don't have this library installed.
    #
    # FIXME(willkg): We're using a *super* old version of dogstatsd-python which doesn't have the
    # DogStatsd class. We should upgrade.
    try:
        from datadog.dogstatsd import statsd
    except ImportError:
        statsd = None

    host = config.metricscfg.statsd_host
    port = config.metricscfg.statsd_port

    if host and statsd:
        statsd.host = host
        statsd.port = port
        return statsd

    class LoggingMetrics(object):
        """Logging-based metrics class that mimics DogStatsd class"""
        def __init__(self):
            self.logger = logging.getLogger('socorro.metrics')

        def _log(self, operation, metric, value, tags):
            self.logger.info('%s: %s=%s tags=%s', operation, metric, value, tags or [])

        def increment(self, metric, value=1, tags=None):
            self._log('increment', metric, value, tags)

        def gauge(self, metric, value, tags=None):
            self._log('gauge', metric, value, tags)

        def timing(self, metric, value, tags=None):
            self._log('timing', metric, value, tags)

        def histogram(self, metric, value, tags=None):
            self._log('histogram', metric, value, tags)

    return LoggingMetrics()


class App(SocorroApp):
    """The base class from which Socorro apps are based"""
    required_config = Namespace()
    required_config.namespace('logging')
    required_config.logging.add_option(
        'syslog_host',
        doc='syslog hostname',
        default='localhost',
        reference_value_from='resource.logging',
    )
    required_config.logging.add_option(
        'syslog_port',
        doc='syslog port',
        default=514,
        reference_value_from='resource.logging',
    )
    required_config.logging.add_option(
        'syslog_facility_string',
        doc='syslog facility string ("user", "local0", etc)',
        default='user',
        reference_value_from='resource.logging',
    )
    required_config.logging.add_option(
        'syslog_line_format_string',
        doc='python logging system format for syslog entries',
        default='{app_name} (pid {process}): '
                '{asctime} {levelname} - {threadName} - '
                '{message}',
        reference_value_from='resource.logging',
    )
    required_config.logging.add_option(
        'syslog_error_logging_level',
        doc='logging level for the log file (10 - DEBUG, 20 '
            '- INFO, 30 - WARNING, 40 - ERROR, 50 - CRITICAL)',
        default=40,
        reference_value_from='resource.logging',
    )
    required_config.logging.add_option(
        'stderr_line_format_string',
        doc='python logging system format for logging to stderr',
        default='{asctime} {levelname} - {app_name} - '
                '{message}',
        reference_value_from='resource.logging',
    )
    required_config.logging.add_option(
        'stderr_error_logging_level',
        doc='logging level for the logging to stderr (10 - '
            'DEBUG, 20 - INFO, 30 - WARNING, 40 - ERROR, '
            '50 - CRITICAL)',
        default=10,
        reference_value_from='resource.logging',
    )
    required_config.add_aggregation(
        'logger',
        setup_logger
    )

    required_config.namespace('metricscfg')
    required_config.metricscfg.add_option(
        'statsd_host',
        doc='host for statsd server',
        default='',
        reference_value_from='resource.statsd'
    )
    required_config.metricscfg.add_option(
        'statsd_port',
        doc='port for statsd server',
        default=8125,
        reference_value_from='resource.statsd'
    )
    required_config.add_aggregation(
        'metrics',
        setup_metrics
    )


def tear_down_logger(app_name):
    logger = logging.getLogger(app_name)
    # must have a copy of the handlers list since we cannot modify the original
    # list while we're deleting items from that list
    handlers = [x for x in logger.handlers]
    for x in handlers:
        logger.removeHandler(x)


def _convert_format_string(s):
    """return '%(foo)s %(bar)s' if the input is '{foo} {bar}'"""
    return re.sub('{(\w+)}', r'%(\1)s', s)
