from __future__ import print_function

import os
import stat
import hashlib
import shutil
import subprocess
import pkg_resources
import tarfile

import yaml

from vr.common.utils import tmpdir, mkdir, file_md5, chowntree
from vr.builder.models import (BuildPack, update_buildpack, update_app,
                               lock_or_wait, CACHE_HOME)
from vr.common.models import ProcData
from vr.common.paths import get_container_path
from vr.builder.slugignore import clean_slug_dir


def cmd_build(build_data, runner_cmd='run', make_tarball=True):
    # runner_cmd may be 'run' or 'shell'.

    outfolder = os.getcwd()

    user = getattr(build_data, 'user', 'nobody')

    with tmpdir() as here:

        # Only bother pulling all the buildpacks if the build file doesn't specify
        # a particular one to build with.
        buildpack_url = getattr(build_data, 'buildpack_url', None)
        buildpack_folders = []
        if not buildpack_url:
            buildpack_folders = pull_buildpacks(build_data.buildpack_urls)

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

        if buildpack_url:
            folder = pull_buildpack(buildpack_url)
            env = {'BUILDPACK_DIR': '/' + folder}
            volumes.append([os.path.join(here, folder), '/' + folder])
        else:
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

        if build_data.image_url is None:
            runner = 'vrun_precise'
        else:
            runner = 'vrun'

        try:
            subprocess.check_call([runner, 'setup', 'buildproc.yaml'],
                                  stderr=subprocess.STDOUT)
            # copy the builder.sh script into place.
            script_src = pkg_resources.resource_filename('vr.builder',
                                                     'scripts/builder.sh')
            script_dst = os.path.join(get_container_path(buildproc),
                                      'builder.sh')
            shutil.copy(script_src, script_dst)
            # Make sure builder.sh is chmod a+x
            builder_st = os.stat(script_dst)
            os.chmod(script_dst, builder_st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

            # make /app/vendor
            slash_app = os.path.join(get_container_path(buildproc), 'app')
            mkdir(os.path.join(slash_app, 'vendor'))
            chowntree(slash_app, username=user)
            subprocess.check_call([runner, runner_cmd, 'buildproc.yaml'],
                                  stderr=subprocess.STDOUT)
            build_data.release_data = recover_release_data(app_folder)
            bp = recover_buildpack(app_folder)
            build_data.buildpack_url = bp.url + '#' + bp.version
            build_data.buildpack_version = bp.version
        finally:
            try:
                # Copy compilation log into outfolder
                if make_tarball:
                    compile_log_src = os.path.join(app_folder, '.compile.log')
                    if os.path.isfile(compile_log_src):
                        compile_log_dest = os.path.join(outfolder, 'compile.log')
                        shutil.copyfile(compile_log_src, compile_log_dest)
                    else:
                        print("No file at %s" % compile_log_src)
            finally:
                # Clean up container
                subprocess.check_call([runner, 'teardown', 'buildproc.yaml'],
                                      stderr=subprocess.STDOUT)

        with lock_or_wait(cachefolder):
            shutil.rmtree(cachefolder, ignore_errors=True)
            shutil.move('cache/buildpack_cache', cachefolder)

        if make_tarball:
            # slugignore
            clean_slug_dir(app_folder)

            # tar up the result
            with tarfile.open('build.tar.gz', 'w:gz') as tar:
                tar.add(app_folder, arcname='')
            build_data.build_md5 = file_md5('build.tar.gz')

            tardest = os.path.join(outfolder, 'build.tar.gz')
            shutil.move('build.tar.gz', tardest)

            build_data_path = os.path.join(outfolder, 'build_result.yaml')
            print("Writing", build_data_path)
            with open(build_data_path, 'wb') as f:
                f.write(build_data.as_yaml())


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
    just_url = url.partition('#')[0]
    with lock_or_wait(just_url):
        app = update_app(name, url, version, vcs_type=vcs_type)
        dest = os.path.join(parent_folder,
                            name + '-' + hashlib.md5(just_url).hexdigest())
        shutil.copytree(app.folder, dest)
    return dest


def pull_buildpack(url):
    """
    Update a buildpack in its shared location, then make a copy into the
    current directory, using an md5 of the url.
    """
    just_url = url.partition('#')[0]
    with lock_or_wait(just_url):
        bp = update_buildpack(url)
        dest = bp.basename + '-' + hashlib.md5(just_url).hexdigest()
        shutil.copytree(bp.folder, dest)
    return dest


def pull_buildpacks(urls):
    return [pull_buildpack(u) for u in urls]
