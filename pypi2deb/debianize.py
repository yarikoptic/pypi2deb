# Copyright © 2015 Piotr Ożarowski <piotr@debian.org>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import logging
from configparser import ConfigParser
from datetime import datetime
from os import listdir, makedirs, walk
from os.path import exists, join
from shutil import copy

from pypi2deb import VERSION, OVERRIDES_PATH, PROFILES_PATH, TEMPLATES_PATH
from pypi2deb.tools import execute

from jinja2 import Environment, FileSystemLoader
from debian.changelog import Changelog, Version, get_maintainer
try:
    from simplejson import load
except ImportError:
    from json import load

from dhpython import PKG_PREFIX_MAP
from dhpython.pydist import guess_dependency, parse_pydep


log = logging.getLogger('pypi2deb')
SPHINX_DIR = {'docs', 'doc', 'doc/build'}
INTERPRETER_MAP = {value: key for key, value in PKG_PREFIX_MAP.items()}
VERSIONED_I_MAP = {'python': 'python2'}


def _copy_static_files(src_dir, debian_dir):
    """Copy static templates/overrides from source to destination"""
    for root, dirs, file_names in walk(src_dir):
        for fn in file_names:
            if fn.endswith(('.tpl', '.swp')):
                continue
            dst_dir = root.replace(src_dir, debian_dir)
            if not exists(dst_dir):
                makedirs(dst_dir)
            if not exists(join(dst_dir, fn)):
                copy(join(root, fn), join(dst_dir, fn))


def debianize(dpath, ctx, profile=None):
    update_ctx(dpath, ctx)

    setupcfg_fpath = join(dpath, 'setup.cfg')
    if exists(setupcfg_fpath):
        upstream_cfg = ConfigParser()
        upstream_cfg.read(setupcfg_fpath)
        if 'py2dsp' in upstream_cfg:
            ctx.update(upstream_cfg['py2dsp'].items())

    if profile:
        if exists(profile):
            with open(profile) as fp:
                ctx.update(load(fp))
        else:
            profile_fpath = join(PROFILES_PATH, profile, 'ctx.json')
            if exists(profile_fpath):
                with open(profile_fpath) as fp:
                    ctx.update(load(fp))

    override_fpath = join(OVERRIDES_PATH, ctx['name'].lower(), 'ctx.json')
    if exists(override_fpath):
        with open(override_fpath) as fp:
            ctx.update(load(fp))

    for key in ('vcs_src', 'vcs_browser', 'uploaders'):
        if key in ctx:
            ctx[key] = ctx[key].format(**ctx)

    # copy static files
    debian_dir = join(dpath, 'debian')
    override_path = join(OVERRIDES_PATH, ctx['name'].lower())
    override_dpath = join(override_path, 'debian')
    _copy_static_files(override_dpath, debian_dir)
    _copy_static_files(join(TEMPLATES_PATH, 'debian'), debian_dir)
    if profile:
        _copy_static_files(join(PROFILES_PATH, profile, 'debian'), debian_dir)

    env = Environment(loader=FileSystemLoader([dpath, override_path,
                                               TEMPLATES_PATH]))

    # render debian dir files (note that order matters)
    docs(dpath, ctx, env)
    control(dpath, ctx, env)
    rules(dpath, ctx, env)
    initial_release = yield from changelog(dpath, ctx, env)
    if initial_release:
        itp_mail(dpath, ctx, env)
    copyright(dpath, ctx, env)
    watch(dpath, ctx, env)
    clean(dpath, ctx, env)


def update_ctx(dpath, ctx):
    ctx.setdefault('exports', {})
    ctx.setdefault('build_depends', set())
    maintainer, email = get_maintainer()
    ctx['creator'] = '{} <{}>'.format(maintainer, email)
    if 'maintainer' not in ctx:
        ctx['maintainer'] = '{} <{}>'.format(maintainer, email)
    if 'debian_revision' not in ctx:
        ctx['debian_revision'] = '0~pypi2deb'

    ctx['binary_arch'] = 'all'
    ctx.setdefault('clean_files', set())
    for root, dirs, file_names in walk(dpath):
        if any(fname.endswith(('.c', '.cpp', '.pyx')) for fname in file_names):
            ctx['binary_arch'] = 'any'

            for fname in file_names:
                if fname.endswith('.pyx'):
                    if 'python' in ctx['interpreters']:
                        ctx['build_depends'].add('cython')
                    if 'python3' in ctx['interpreters']:
                        ctx['build_depends'].add('cython3')
                    for ext in ('c', 'cpp'):
                        fname_c = fname[:-3] + ext
                        if fname_c in file_names:
                            ctx['clean_files'].add(join(root.replace(dpath, '.'), fname_c))


def docs(dpath, ctx, env):
    docs = ctx.setdefault('docs', {})
    for path in SPHINX_DIR:
        if exists(join(dpath, path, 'Makefile')) and exists(join(dpath, path, 'conf.py')):
            docs['sphinx_dir'] = path
            ctx['build_depends'].add('python3-sphinx')
            docs.setdefault('files', []).append('.pybuild/docs/*')
    for fn in listdir(dpath):
        if fn.lower().startswith('readme'):
            docs.setdefault('files', []).append(fn)
        if fn.lower() == 'examples':
            docs['examples_dir'] = fn
    if docs:
        if 'sphinx_dir' in docs:
            # i.e. we have binary packages with docs
            docs_pkg = 'python-{}-doc'
        elif 'python3' in ctx['interpreters']:
            docs_pkg = 'python3-{}'
        else:
            docs_pkg = 'python-{}'
        docs_pkg = docs_pkg.format(ctx['src_name'])
        if 'examples_dir' in docs:
            # TODO: should we extend this file only if it exists?
            with open(join(dpath, 'debian', docs_pkg + '.examples'), 'w') as fp:
                fp.write("{}/*\n".format(docs['examples_dir']))
        if 'files' in docs:
            # TODO: should we extend this file only if it exists?
            with open(join(dpath, 'debian', docs_pkg + '.docs'), 'w') as fp:
                for fn in docs['files']:
                    fp.write("{}\n".format(fn))


def _render_template(func):
    name = func.__name__

    def _template(dpath, ctx, env, *args, **kwargs):
        fpath = join(dpath, 'debian', name)
        if exists(fpath):
            log.debug('debian/%s already exist, skipping', name)
            return
        ctx = func(dpath, ctx, env, *args, **kwargs)
        tpl = env.get_template('debian/{}.tpl'.format(name))
        with open(fpath, 'w', encoding='utf-8') as fp:
            fp.write(tpl.render(ctx))
    return _template


@_render_template
def control(dpath, ctx, env):
    desc = []
    code_line = False
    for line in ctx['description'].split('\n'):
        if not line.strip():
            desc.append(' .')
        else:
            if line.startswith(('* ', '>>> ', '... ', '.. ', '$ ')) \
               or code_line or line == '...':
                if line.startswith('>>> '):
                    # next line should get extra space char as well
                    code_line = True
                elif code_line:
                    code_line = False
                line = ' ' + line
            line = line.replace('\t', '    ')
            desc.append(' ' + line)
    for key, value in {
        'short_desc': ctx['summary'][:80],
        'long_desc': '\n'.join(desc),
    }.items():
        if key not in ctx:
            ctx[key] = value

    req = set()
    if 'requires' in ctx:
            for impl in ctx['interpreters']:
                impl = INTERPRETER_MAP.get(impl, impl)
                try:
                    dependency = guess_dependency(impl, line)
                    if dependency:
                        ctx['build_depends'].add(dependency)
                except Exception as err:
                    log.warn('cannot parse build dependency: %s', err)
    else:
        for i in listdir(dpath):
            if i.endswith('.egg-info') and exists(join(dpath, i, 'requires.txt')):
                req.add(join(dpath, i, 'requires.txt'))
            if i == 'requirements.txt':
                req.add(join(dpath, 'requirements.txt'))

        for fpath in req:
            for impl in ctx['interpreters']:
                impl = INTERPRETER_MAP.get(impl, impl)
                try:
                    for i in parse_pydep(impl, fpath):
                        ctx['build_depends'].add(i)
                except Exception as err:
                    log.warn('cannot parse build dependency: %s', err)

    if exists(join(dpath, 'setup.py')):
        with open(join(dpath, 'setup.py')) as fp:
            for line in fp:
                if line.startswith('#'):
                    continue
                if 'setuptools' in line:
                    for interpreter in ctx['interpreters']:
                        ctx['build_depends'].add('{}-setuptools'.format(interpreter))

    if 'python' in ctx['interpreters']:
        ctx['build_depends'].add(
            'python-all%s' % ('-dev' if ctx['binary_arch'] == 'any' else ''))
    if 'python3' in ctx['interpreters']:
        ctx['build_depends'].add(
            'python3-all%s' % ('-dev' if ctx['binary_arch'] == 'any' else ''))
    if 'pypy' in ctx['interpreters']:
        ctx['build_depends'].add('pypy')

    return ctx


@_render_template
def rules(dpath, ctx, env):
    ctx['with'] = ','.join(VERSIONED_I_MAP.get(i, i) for i in ctx['interpreters'])
    if ctx.get('docs', {}).get('sphinx_dir'):
        ctx['with'] += ',sphinxdoc'

    # if package install a script in /usr/bin/ - ship it only in python3-foo package
    if exists(join(dpath, 'setup.py')) and len(ctx['interpreters']) > 1:
        with open(join(dpath, 'setup.py')) as fp:
            for line in fp:
                if 'console_scripts' in line:
                    for interpreter in ctx['interpreters']:
                        if interpreter == 'python3':
                            continue
                        ipreter = VERSIONED_I_MAP.get(interpreter, interpreter)
                        ctx['exports']['PYBUILD_AFTER_INSTALL_{}'.format(ipreter)] = 'rm -rf {destdir}/usr/bin/'
                    break

    return ctx


def changelog(dpath, ctx, env):
    change = ctx.get('message', 'Autogenerated by py2dsp v{}'.format(VERSION))
    version = "{}-{}".format(ctx['version'], ctx['debian_revision'])
    distribution = ctx.get('distribution', 'UNRELEASED')

    fpath = join(dpath, 'debian', 'changelog')
    if exists(fpath):
        with open(fpath, encoding='utf-8') as fp:
            line = fp.readline()
            if ctx['version'] in line or 'UNRELEASED' in line:
                log.debug('changelog doesn\'t need an update')
                return
            else:
                yield from execute(['dch', '--force-distribution', '--distribution', distribution,
                                    '--newversion', version, '-m', change], cwd=dpath)
        return

    now = datetime.utcnow()
    changelog = Changelog()
    changelog.new_block(package=ctx['src_name'],
                        version=Version(version),
                        distributions=distribution,
                        urgency='low',
                        author=ctx['creator'],
                        date=now.strftime('%a, %d %b %Y %H:%M:%S +0000'))
    changelog.add_change('')
    changelog.add_change('  * {}'.format(change))
    changelog.add_change('')

    with open(fpath, 'w', encoding='utf-8') as fp:
        changelog.write_to_open_file(fp)
    return True


@_render_template
def copyright(dpath, ctx, env):
    ctx['deb_copyright'] = "2015 © {}".format(ctx['creator'])
    ctx['deb_license_name'] = ctx['license_name']

    if exists('/usr/share/common-licenses/{}'.format(ctx['license_name'])):
        pass  # FIXME
    ctx.setdefault('copyright', '')

    if not ctx.get('license'):
        license = []
        for fn in listdir(dpath):
            if not fn.lower().startswith('license'):
                continue
            with open(join(dpath, fn), 'r') as fp:
                for line in fp:
                    line = line.strip('\n')
                    if not line.strip():
                        license.append(' .')
                    else:
                        license.append(' ' + line)
                        if not ctx['copyright'] and line.lower().startswith('copyright '):
                            ctx['copyright'] = line[10:]
            if license:
                ctx['license'] = '\n'.join(license)
            break
    if not ctx.get('copyright'):
        ctx['copyright'] = ctx['author']
    return ctx


@_render_template
def watch(dpath, ctx, env):
    return ctx


def itp_mail(dpath, ctx, env):
    fpath = join(ctx['root'], '{src_name}_{version}-{debian_revision}.mail'.format(**ctx))
    if exists(fpath):
        return
    tpl = env.get_template('itp.mail')
    with open(fpath, 'w', encoding='utf-8') as fp:
        fp.write(tpl.render(ctx))


def clean(dpath, ctx, env):
    old = set()
    fpath = join(dpath, 'debian', 'clean')
    if exists(fpath):
        with open(fpath) as fp:
            for line in fp:
                old.add(line.strip())

    new = set()
    for i in ctx['clean_files']:
        if i not in old:
            new.add(i)

    if new:
        with open(fpath, 'a') as fp:
            for fn in new:
                fp.write("\n{}".format(fn))
