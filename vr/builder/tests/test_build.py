import tempfile
from os.path import join, isdir, dirname
from shutil import rmtree
from unittest import TestCase

from vr.common.utils import tmpdir
from vr.common.tests import tmprepo
from vr.builder.main import BuildData
from vr.builder.models import BuildPack
from vr.builder.build import NullSaver, OutputSaver, create_saver


def test_version_in_fragment():
    rev = '16c1dba07ee78d5dbee1f965d91d3d61942ccb67'
    url = 'https://github.com/btubbs/vr_python_example.git#' + rev
    with tmpdir():
        bp = BuildPack('bp', url, 'git')
        bp.clone()
        bp.update()
        assert bp.version == rev


def test_buildpack_update_norev():
    with tmprepo('buildpack_hello.tar.gz', 'git', BuildPack) as r:
        rev = 'd0b1df4838d51c694b6bba9b6c3779a5e2a17775'
        # Unlike the Repo superclass, buildpacks can call .update() without
        # passing in a revision, since we don't want to make users think about
        # buildpack versions if they don't have to.
        r.update()
        assert r.version == rev


def test_buildpack_update_rev():
    with tmprepo('buildpack_hello.tar.gz', 'git', BuildPack) as r:
        rev = '410a52780f6fd9d10d09d1da54088c03a0e2933f'
        # But passing in a rev needs to be supported still
        r.update(rev)
        assert r.version == rev


class SaverTestCase(TestCase):
    def setUp(self):
        self.base_log_dir = join(tempfile.tempdir, 'vr', 'build')

    def get_build_data(self):
        return BuildData({
            'app_name': 'some_app',
            'app_repo_url': 'some_app_url',
            'buildpack_url': 'some_buildpack_url',
            'app_repo_type': 'git',
            'version': 0.1,
        })

    def tearDown(self):
        rmtree(self.base_log_dir, ignore_errors=True)


class SaverTest(SaverTestCase):
    def test_creates_saver_without_tarball(self):
        saver = create_saver(self.get_build_data(), make_tarball=False)

        self.assertIsInstance(saver, NullSaver)

    def test_creates_saver_with_tarball(self):
        saver = create_saver(self.get_build_data(), make_tarball=True)

        self.assertIsInstance(saver, OutputSaver)


class OutputSaverTest(SaverTestCase):
    def setUp(self):
        super(OutputSaverTest, self).setUp()

        self.build_data = self.get_build_data()
        self.saver = OutputSaver(self.build_data)
        self.app_folder = join(dirname(__file__), 'fixtures')

    def test_instantiates_with_log_directory(self):
        app_logs = '{}-{}'.format(
            self.build_data.app_name, self.build_data.version)
        self.assertEqual(self.saver.log_dir, join(self.base_log_dir, app_logs))

    def test_saves_a_log_file(self):
        self.saver.save_log_file(self.app_folder, 'some.log', 'other.log')

        with open(join(self.saver.log_dir, 'other.log')) as f:
            content = f.read().decode('utf-8').strip()

        self.assertEqual(content, 'test')
