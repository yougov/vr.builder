from __future__ import print_function

import os
import hashlib
import shutil
import subprocess
import pkg_resources
import tarfile
import sys
import collections
import functools
import contextlib

from six.moves import urllib

import yaml
import path

from vr.common.utils import tmpdir, mkdir, file_md5, chowntree
from vr.builder.models import (BuildPack, update_buildpack, update_app,
                               lock_or_wait, CACHE_HOME)
from vr.common.models import ProcData
from vr.common.paths import get_container_path
from vr.builder.slugignore import clean_slug_dir


pkg_filename = functools.partial(pkg_resources.resource_filename, 'vr.builder')


class NullSaver(object):
    def save_compile_log(self, app_folder):
        pass

    def make_tarball(self, app_folder, build_data):
        pass


class OutputSaver(object):
    def __init__(self):
        self.outfolder = os.getcwd()

    def save_compile_log(self, app_folder):
        "Copy compilation log into outfolder"
        compile_log_src = os.path.join(app_folder, '.compile.log')
        if os.path.isfile(compile_log_src):
            compile_log_dest = os.path.join(self.outfolder, 'compile.log')
            shutil.copyfile(compile_log_src, compile_log_dest)
        else:
            print("No file at %s" % compile_log_src)

    def make_tarball(self, app_folder, build_data):
        """
        Following a successful build, create a tarball and build result.
        """
        # slugignore
        clean_slug_dir(app_folder)

        # tar up the result
        with tarfile.open('build.tar.gz', 'w:gz') as tar:
            tar.add(app_folder, arcname='')
        build_data.build_md5 = file_md5('build.tar.gz')

        tardest = os.path.join(self.outfolder, 'build.tar.gz')
        shutil.move('build.tar.gz', tardest)

        build_data_path = os.path.join(self.outfolder, 'build_result.yaml')
        print("Writing", build_data_path)
        with open(build_data_path, 'wb') as f:
            f.write(build_data.as_yaml())


def cmd_build(build_data, runner_cmd='run', make_tarball=True):
    # runner_cmd may be 'run' or 'shell'.

    saver = OutputSaver() if make_tarball else NullSaver()

    with tmpdir():
        app_folder = _cmd_build(build_data, runner_cmd, saver)
        saver.make_tarball(app_folder, build_data)


def _cmd_build(build_data, runner_cmd, saver):
    here = os.getcwd()
    user = getattr(build_data, 'user', 'nobody')

    # clone/pull repo to latest
    build_folder = os.path.join(here, 'build')
    mkdir(build_folder)
    app_folder = pull_app(build_folder,
                          build_data.app_name,
                          build_data.app_repo_url,
                          build_data.version,
                          vcs_type=build_data.app_repo_type)
    app_basename = os.path.basename(app_folder)
    chowntree(build_folder, username=user)

    app_folder_inside = os.path.join('/build', app_basename)

    volumes = [
        [build_folder, '/build']
    ]

    buildpack_url = getattr(build_data, 'buildpack_url', None)
    if buildpack_url:
        buildpack_folders = []
        folder = pull_buildpack(buildpack_url)
        env = {'BUILDPACK_DIR': '/' + folder}
        volumes.append([os.path.join(here, folder), '/' + folder])
    else:
        # Pull the buildpacks only if one is not specified.
        buildpack_folders = pull_buildpacks(build_data.buildpack_urls)
        buildpacks_env = ':'.join('/' + bp for bp in buildpack_folders)
        env = {'BUILDPACK_DIRS': buildpacks_env}
        for folder in buildpack_folders:
            volumes.append([os.path.join(here, folder), '/' + folder])

    # Some buildpacks (Node) like to rm -rf the whole cache folder they're
    # given.  They can't do that to a mountpoint, so we have to provide a
    # buildpack_cache folder nested inside the /cache mountpoint.
    cachefolder = os.path.join(CACHE_HOME, app_basename)
    if os.path.isdir(cachefolder):
        with lock_or_wait(cachefolder):
            mkdir('cache')
            shutil.copytree(cachefolder, 'cache/buildpack_cache', symlinks=True)
    else:
        mkdir('cache/buildpack_cache')
        # Maybe we're on a brand new host that's never had CACHE_HOME
        # created.  Ensure that now.
        mkdir(CACHE_HOME)
    chowntree('cache', username=user)
    volumes.append([os.path.join(here, 'cache'), '/cache'])


    cmd = '/builder.sh %s /cache/buildpack_cache' % app_folder_inside

    container_path = _write_buildproc_yaml(build_data, env, user, cmd, volumes)

    runner = 'vrun' if build_data.image_url else 'vrun_precise'

    def run(run_cmd):
        cmd = runner, run_cmd, 'buildproc.yaml'
        return subprocess.check_call(cmd, stderr=subprocess.STDOUT)

    with _setup_container(run):
        try:
            with _prepare_build(container_path, user, build_data, app_folder):
                run(runner_cmd)
        finally:
            saver.save_compile_log(app_folder)

    with lock_or_wait(cachefolder):
        shutil.rmtree(cachefolder, ignore_errors=True)
        shutil.move('cache/buildpack_cache', cachefolder)


@contextlib.contextmanager
def _setup_container(run):
    try:
        run('setup')
        yield
    finally:
        run('teardown')


@contextlib.contextmanager
def _prepare_build(container_path, user, build_data, app_folder):
    # copy the builder.sh script into place.
    script_src = pkg_filename('scripts/builder.sh')
    script_dst = path.Path(container_path) / 'builder.sh'
    shutil.copy(script_src, script_dst)
    # Make sure builder.sh is chmod a+x
    script_dst.chmod('a+x')

    # make /app/vendor
    slash_app = os.path.join(container_path, 'app')
    mkdir(os.path.join(slash_app, 'vendor'))
    chowntree(slash_app, username=user)
    yield
    build_data.release_data = recover_release_data(app_folder)
    bp = recover_buildpack(app_folder)
    build_data.buildpack_url = bp.url + '#' + bp.version
    build_data.buildpack_version = bp.version


def _write_buildproc_yaml(build_data, env, user, cmd, volumes):
    """
    Write a proc.yaml for the container and return the container path
    """

    buildproc = ProcData({
        'app_name': build_data.app_name,
        'app_repo_url': '',
        'app_repo_type': '',
        'buildpack_url': '',
        'buildpack_version': '',
        'config_name': 'build',
        'env': env,
        'host': '',
        'port': 0,
        'version': build_data.version,
        'release_hash': '',
        'settings': {},
        'user': user,
        'cmd': cmd,
        'volumes': volumes,
        'proc_name': 'build',
        'image_name': build_data.image_name,
        'image_url': build_data.image_url,
        'image_md5': build_data.image_md5,
    })

    # write a proc.yaml for the container.
    with open('buildproc.yaml', 'wb') as f:
        f.write(buildproc.as_yaml())
    return get_container_path(buildproc)


def recover_release_data(app_folder):
    """
    Given the path to an app folder where an app was just built, return a
    dictionary containing the data emitted from running the buildpack's release
    script.

    Relies on the builder.sh script storing the release data in ./.release.yaml
    inside the app folder.
    """
    with open(os.path.join(app_folder, '.release.yaml'), 'rb') as f:
        return yaml.safe_load(f)


def recover_buildpack(app_folder):
    """
    Given the path to an app folder where an app was just built, return a
    BuildPack object pointing to the dir for the buildpack used during the
    build.

    Relies on the builder.sh script storing the buildpack location in
    /.buildpack inside the container.
    """
    filepath = os.path.join(app_folder, '.buildpack')
    with open(filepath, 'rb') as f:
        buildpack_picked = f.read()
    buildpack_picked = buildpack_picked.lstrip('/')
    buildpack_picked = buildpack_picked.rstrip('\n')
    buildpack_picked = os.path.join(os.getcwd(), buildpack_picked)
    return BuildPack(buildpack_picked)


def pull_app(parent_folder, name, url, version, vcs_type):
    defrag = _defrag_compat(urllib.parse.urldefrag(url))
    with lock_or_wait(defrag.url):
        app = update_app(name, url, version, vcs_type=vcs_type)
        dest_name = name + '-' + hashlib.md5(defrag.url).hexdigest()
        dest = os.path.join(parent_folder, dest_name)
        shutil.copytree(app.folder, dest)
    return dest


PY32 = sys.version_info >= (3, 2)
DefragResult = collections.namedtuple('DefragResult', 'url fragment')
_defrag_compat = (lambda x: x) if PY32 else lambda x: DefragResult(*x)
"""
Return a Python 3.2 compatible result from urldefrag.
TODO: replace with python-futures invocation.
"""


def pull_buildpack(url):
    """
    Update a buildpack in its shared location, then make a copy into the
    current directory, using an md5 of the url.
    """
    defrag = _defrag_compat(urllib.parse.urldefrag(url))
    with lock_or_wait(defrag.url):
        bp = update_buildpack(url)
        dest = bp.basename + '-' + hashlib.md5(defrag.url).hexdigest()
        shutil.copytree(bp.folder, dest)
    return dest


def pull_buildpacks(urls):
    return [pull_buildpack(u) for u in urls]
