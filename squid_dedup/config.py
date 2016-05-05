#!/usr/bin/env python3
# -*- coding: utf8 -*
"""
Synopsis:
    %(appname)s is a squid proxy helper, helping to reduce cache misses
    when identical content is accessed using different URLs (aka CDNs)

Usage: %(appname)s [-%(_cmdlin_options)s]%(_cmdlin_parmsg)s
       -h, --help           this text
       -V, --version        print version and exit
       -v, --verbose        raises log level (cumulative)
       -q, --quiet          lowers log level (cumulative)
       -l, --logfile=file   log to file or with '-' on console
                            [default: %(logfile)s]
       -L, --loglevel=level specify a certain log level directly
                            [default: %(_loglevel_str)s]
       -s, --syslog=level   specify syslog log level
                            [default: %(_sysloglevel_str)s]
       -c, --cfgfile=file   primary config file
       -i, --include=match  comma separated list of additional config files
       -I, --intdomain=dom  internal domain [default: %(intdomain)s]
       -p, --profile        enable profiling code
       -x, --extract        extract config file

Description:
URL patterns, specified in config files, are rewritten to an presumably
unique internal address. Further accesses, modified in the same way, map
to the already stored object, even if it is using a different URL.

This helper implements the squid StoreID protocol.

If a primary config file is specified, this file must exist, otherwise built-
in defaults are used. For additional _sections_, a list of globbing args
is evaluated: %(_include_list)s.

By default, only errors and warnings are logged.
Available log levels are: %(_loglevel_list)s

Profiling data is written to %(profiledir)s

Installation:

Add similar values to a squid config file
Note: these parameter store deduplicated objects aggressively:

# refresh pattern for StoreID deduplicated objects
# 10080 min: 7 days
# 525600 min: 1 year
refresh_pattern ^http://([a-zA-Z0-9\-\.]+)\.squid\.internal/.*  10080 80% 525600 \
                override-expire override-lastmod ignore-reload ignore-no-store \
                ignore-private refresh-ims ignore-must-revalidate

store_id_program %(appdir)s/%(appname)s
store_id_children 10 startup=5 idle=3 concurrency=0
"""

__version__ = '0.0.1'
__verdate__ = '2016-05-04'
__author__ = 'Hans-Peter Jansen <hpj@urpla.net>'
__copyright__ = '(c)2016 ' + __author__
__license__ = 'GNU GPL 2 - see http://www.gnu.org/licenses/gpl2.txt for details'


__builtin_cfg__ = """\
# Config file for %(appname)s v[%(version)s/%(verdate)s]

[global]

# internal squid domain
intdomain: %(intdomain)s

# proxy server
http_proxy: %(http_proxy)s
https_proxy: %(https_proxy)s

# url fetcher thread count
fetch_threads: %(fetch_threads)s

# fetch delay (in seconds)
fetch_delay: %(fetch_delay)s

# Comma separated list of additional config file patterns
include: %(_include_list)s

# Log level (one of: %(_loglevel_list)s)
loglevel: %(_loglevel_str)s

# Log to this file (or to console, if unset or '-')
logfile: %(logfile)s

# Log to syslog with this log level
sysloglevel: %(_sysloglevel_str)s

# profiling
profile: %(profile)s
profiledir: %(profiledir)s

#[CDN]
## match a list of of urls
#match: http:\/\/url-regex-1/(.*)
#       http:\/\/url-regex-2/(.*)
#       http:\/\/url-regex-3/(.*)
## replace with an internal url: must result in a unique address
#replace: http:\/\/url-repl.%%(intdomain)s/\\1
## fetch URLs (optional, default: False)
## useful for clients, that fetch byte ranges only from multiple sources
#fetch: false

#[sourceforge]
#match: http:\/\/[a-zA-Z0-9\-\_\.]+\.dl\.sourceforge\.net\/(.*)
#replace: http://dl.sourceforge.net.%%(intdomain)s/\\1
#fetch: true
"""

import os
import re
import sys
import glob
import queue
import getopt
import socket
import logging

from collections import OrderedDict

# local imports
from lib import configfile, logsetup, record, frec


# setup logging
log = logging.getLogger('config')

stderr = lambda *s: print(*s, file = sys.stderr, flush = True)

def exit(ret = 0, msg = None):
    if msg:
        stderr(msg)
    sys.exit(ret)


def strsplit(msg, splitter = ','):
    return [s for s in map(lambda s: s.strip(), msg.split(splitter)) if s]


def strlist(list, joiner = ', '):
    return joiner.join(list)


class Config:
    """Central configuration class"""
    # internal
    if __name__ == '__main__':
        # for testing purposes
        appdir, appname = '.', 'config'
    else:
        appdir, appname = os.path.split(sys.argv[0])
    if appdir in ('', '.'):
        appdir = os.getcwd()
    if appname.endswith('.py'):
        appname = appname[:-3]
    version = __version__
    verdate = __verdate__
    author = __author__
    copyright = __copyright__
    license = __license__

    # internal domain
    intdomain = 'squid.internal'

    # proxy server
    http_proxy = 'localhost:3128'
    https_proxy = 'localhost:3128'

    # number of fetcher threads
    fetch_threads = 5

    # fetch delay in seconds
    fetch_delay = 15

    pid = os.getpid()
    hostname = socket.getfqdn()
    if hostname in ('xrated.lisa.loc', 'pitu5.lisa.loc'):
        TESTING = True
    else:
        TESTING = False
    PRODUCTION = not TESTING

    # additional config files
    if TESTING:
        include = [os.path.join('.', 'conf', '*.conf'),]
    else:
        include = [os.path.join('~', '.' + appname, '*.conf'),
                   os.path.join(os.sep, 'etc', 'squid', 'dedup', '*.conf'),]

    # logging
    if TESTING:
        logfile = '-'
        loglevel = logging.TRACE
        sysloglevel = None
    else:
        logfile = os.path.join(os.sep, 'var', 'log', 'squid', 'dedup.log')
        loglevel = logging.INFO
        sysloglevel = logging.ERROR

    # profiling
    profile = False
    profiledir = os.path.join(appdir, 'profiles')

    # internal
    primary_section = 'global'
    section_dict = OrderedDict()
    fetch_queue = queue.Queue()

    _cfgfile = None
    _loglevel_str = None
    _sysloglevel_str = None
    _include_list = None
    _loglevel_list = None

    # command line parameter
    _cmdlin_options = 'hVvqpx'
    _cmdlin_paropt = 'l:L:s:c:i:'
    _cmdlin_parmsg = '[-l log][-L loglvl][-s sysloglvl][-c cfg][-i inc,..]'
    _cmdlin_longopt = (
        'help', 'version', 'verbose', 'quiet', 'logfile=', 'loglevel=', 'syslog=',
        'cfgfile=', 'include=', 'profile', 'extract',
    )


    def __init__(self):
        """load config files and process command line parameter"""
        super().__init__()

        # transfer class vars to instance __dict__
        # for __repr__ and ConfigParser interpolation
        for attr, value in Config.__dict__.items():
            if not attr.startswith('__') and not callable(value):
                self.__dict__[attr] = value

        # process command line
        try:
            optlist, args = getopt.getopt(sys.argv[1:],
                self._cmdlin_options + self._cmdlin_paropt, self._cmdlin_longopt)
        except getopt.error as msg:
            exit(1, '%s: %s' % (self.appname, msg))

        # reset any pre configured includes, if includes are specified
        for opt, par in optlist:
            if opt in ('-i', '--include'):
                self.include = []

        if self.TESTING:
            logsetup.logsetup(logging.TRACE)

        # process command line parameter
        for opt, par in optlist:
            if opt in ('-h', '--help'):
                exit(0, self.usage())
            elif opt in ('-V', '--version'):
                exit(0, 'version %s/%s' % (self.version, self.verdate))
            elif opt in ('-v', '--verbose'):
                if self.loglevel > logging.DEBUG:
                    self.loglevel -= 10
                else:
                    if self.loglevel == logging.DEBUG:
                        self.loglevel = logging.TRACE
            elif opt in ('-q', '--quiet'):
                if self.loglevel == logging.TRACE:
                    self.loglevel = logging.DEBUG
                elif self.loglevel < logging.CRITICAL:
                    self.loglevel += 10
            elif opt in ('-l', '--logfile'):
                self.logfile = par
            elif opt in ('-L', '--loglevel'):
                ll = logsetup.loglevel(par)
                if ll is None:
                    exit(1, '%s: invalid loglevel <%s>' % par)
                else:
                    self.loglevel = ll
            elif opt in ('-s', '--syslog'):
                ll = logsetup.loglevel(par)
                if ll is None:
                    exit(1, '%s: invalid syslog level <%s>' % par)
                else:
                    self.sysloglevel = ll
            elif opt in ('-c', '--cfgfile'):
                # load primary config file immediately
                self.load_primary_config(par)
            elif opt in ('-i', '--include'):
                for arg in strsplit(par):
                    self.include.append(arg)
            elif opt in ('-I', '--intdomain'):
                self.intdomain = par
            elif opt in ('-p', '--profile'):
                self.profile = True
            elif opt in ('-x', '--extract'):
                self.write_cfgfile(self.appname + '.conf', self.builtin_cfg())
                exit(0)

        if self.profile and not os.path.exists(self.profiledir):
            os.makedirs(self.profiledir)
        logsetup.logsetup(self.loglevel, self.logfile, self.sysloglevel)
        log.trace('logsetup(logfile: %s, loglevel: %s, sysloglevel: %s)',
                  self.logfile, self.loglevel, self.sysloglevel)
        self.load_aux_config()

    def reload(self):
        self.section_dict = OrderedDict()
        if self._cfgfile is not None:
            self.load_primary_config(self._cfgfile)
        self.load_aux_config()

    def load_primary_config(self, cfgfile):
        self._cfgfile = cfgfile
        log.trace('load_primary_config(%s)', cfgfile)
        try:
            cf = configfile.ConfigFile(self.defaults(), cfgfile)
        except configfile.ConfigFileError as e:
            log.critical(e)
            exit(2)
        self.process_primary_section(cf)
        self.process_aux_sections(cf, primary = True)

    def process_primary_section(self, cf):
        log.trace('process_primary_section(%s)', cf.filename)
        # internal domain
        self.intdomain = cf.get(self.primary_section, 'intdomain', self.intdomain)
        # proxy server
        self.http_proxy = cf.get(self.primary_section, 'http_proxy', self.http_proxy)
        self.https_proxy = cf.get(self.primary_section, 'https_proxy', self.https_proxy)
        # number of fetcher threads
        self.fetch_threads = cf.getint(self.primary_section, 'fetch_threads',
                                       self.fetch_threads)
        # fetch delay in seconds
        self.fetch_delay = cf.getint(self.primary_section, 'fetch_delay',
                                     self.fetch_delay)
        # includes
        self.include = cf.getlist(self.primary_section, 'include', self.include)
        # logging
        self.logfile = cf.get(self.primary_section, 'logfile', self.logfile)
        self.loglevel = logsetup.loglevel(cf.get(self.primary_section, 'loglevel',
                                                 self.loglevel))
        self.sysloglevel = logsetup.loglevel(cf.get(self.primary_section, 'sysloglevel',
                                                    self.sysloglevel))
        # profiling
        self.profile = cf.getbool(self.primary_section, 'profile', self.profile)
        self.profiledir = cf.get(self.primary_section, 'profiledir', self.profiledir)
        # reset logging setup
        logsetup.logsetup(self.loglevel, self.logfile, self.sysloglevel)
        # check mandatory parameter
        #for var, msg in (
        #    ('attrib', 'no valid attrib defined'),
        #):
        #    if not getattr(self, var):
        #        exit(2, '%s: %s' % (self.appname, msg))

    def load_aux_config(self):
        # load auxiliary config files
        for include in self.include:
            log.trace('include(%s)', include)
            for cfgfile in sorted(glob.glob(include)):
                log.trace('read(%s)', cfgfile)
                try:
                    cf = configfile.ConfigFile(self.defaults(), cfgfile)
                except configfile.ConfigFileError as e:
                    log.error(e)
                self.process_aux_sections(cf)

    def process_aux_sections(self, cf, primary = False):
        log.trace('process_aux_sections(%s, primary = %s)', cf.filename, primary)
        # sections
        for section in cf.sections():
            log.trace('process_aux_section(section: %s)', section)
            if section != self.primary_section:
                self.process_section(cf, section)
            elif not primary:
                log.error('primary section [%s] is only allowed once: ignored',
                          self.primary_section)

    def process_section(self, cf, section):
        log.trace('process_section(%s: %s)', section, cf.items(section))
        if section in self.section_dict:
            log.error('section [%s] already processed from %s: ignored',
                      section, self.section_dict[section].cfgfile)
            return
        match = cf.getlist(section, 'match', splitter = '\n', vars = self.defaults())
        match = [(arg, re.compile(arg, re.IGNORECASE)) for arg in match]
        replace = cf.get(section, 'replace', vars = self.defaults())
        fetch = cf.getbool(section, 'fetch', False)
        if match and replace:
            par = dict(match = match,
                       replace = replace,
                       fetch = fetch,
                       cfgfile = cf.filename)
            rec = record.recordfactory('Section', **par)
            self.section_dict[section] = rec
        else:
            log.error('invalid match/replace parameter in section [%s] of %s',
                      section, cf.filename)

    def create_special_vars(self):
        self._include_list = strlist(self.include)
        self._loglevel_list = strlist(logsetup.loglevel_list)
        self._loglevel_str = logsetup.loglevel_str(self.loglevel)
        self._sysloglevel_str = logsetup.loglevel_str(self.sysloglevel)

    def defaults(self):
        d = {}
        for k, v in self.__dict__.items():
            if not k.startswith('_') and isinstance(v, str):
                d[k] = v
        return d

    def write_cfgfile(self, cfgfile, cfg):
        if os.path.exists(cfgfile):
            os.rename(cfgfile, cfgfile + '~')
            stderr('keep old %s as %s~' % (cfgfile, cfgfile))
        open(cfgfile, 'w').write(cfg)
        stderr('config written to: %s' % cfgfile)

    def builtin_cfg(self):
        self.create_special_vars()
        return __builtin_cfg__ % self.__dict__

    def usage(self):
        self.create_special_vars()
        return __doc__ % self.__dict__

    def __repr__(self):
        self.create_special_vars()
        return '%s(\n%s\n)' % (self.__class__.__name__,
                               frec.frec(self.__dict__, withunderscores = False))


# main section, only for config debugging purposes
if __name__ == '__main__':
    Config.loglevel = logging.TRACE
    Config.logfile = '-'
    config = Config()
    log.debug(config)
