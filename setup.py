#!/usr/bin/python
from setuptools import setup, find_packages

setup(
    name='vr.builder',
    description=('Command line tools to build apps in containers.'),
    namespace_packages=['vr'],
    version='1.2.3',
    author='Brent Tubbs',
    author_email='brent.tubbs@gmail.com',
    url='https://bitbucket.org/yougov/vr.builder',
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        'vr.runners>=2.4.4,<3',
        'path.py>=7.1',
        'yg.lockfile',
        'jaraco.itertools',
        'vr.common>=4.2.0',
    ],
    entry_points={
        'console_scripts': [
            'vbuild = vr.builder.main:main',
        ],
    },
    setup_requires=[
        'pytest-runner',
    ],
    tests_require=[
        'pytest',
    ],
)
